"""Historical engine-replay backfill — the PRODUCER half.

Goal
----
Replay 2022-2026 historical market data through the ACTUAL ``FEFEngine`` to
manufacture ``SignalIter``-equivalent verdict rows, so the research-tab tools
(walk-forward / tournament / ML / tuning) can run on years of data instead of
the ~12 live sessions currently in production.

This script does NOT modify any engine or live code. It:
  1. reads the production DB **READ-ONLY** (``~/.fef-terminal/signal_log.db``),
  2. reconstructs a ``FEFInputs`` per 2-min iter from historical tables,
  3. calls ``FEFEngine.push(inputs)`` on a real engine instance,
  4. writes the verdict as a ``signal_iter`` row to a **SEPARATE** DB
     (``replay_signals.db`` — never the production ``signal_iter``).

Faithfulness (see the module-level FEASIBILITY notes and the final report):
  * ``spx_price``        REAL SPX 1-min bars (historical_bar) — NOT parity spot.
  * ``drift_cum_net_m``  REAL, from option_flow_minute.net_signed_prem cumsum/1e6.
  * ``drift_bias``       REAL logic reproduced (parser DOMINANCE_RATIO=0.15).
  * ``ntm/raw_max_pain`` REAL OI via the engine's own compute_ntm_max_pain().
  * ``top_gex/dex_walls`` RECONSTRUCTED from real OI + real option NBBO via BSM.
        There is NO real per-strike historical exposure chain for 2022-2025
        (exposure_chain_point only covers 2026-05-18+). Reconstruction quality
        vs the real QuantData chain (9 overlap sessions): ~58% top-5 strike
        overlap, 71% sign agreement, magnitude ~5.5x low (calibrated below).
  * aggression/iv_rank/cex/vex/by_expiry/orders  APPROX or STUB — but the engine
        does NOT gate on any of these (verified in engine/fef.py), so the
        DECISION is unaffected. See FIELD_MAP for the full field-by-field verdict.

Outcome scoring is intentionally NOT done here — see ``score_outcome`` (a
documented SEAM). It MUST later use tick-accurate real option $ via
scripts/research/backtest/tick_fetch.py (buy the ask / sell the bid, no
lookahead), never R x $500.

Usage
-----
    PY=C:/Users/diant/projects/fef-terminal/sidecar/.venv/Scripts/python.exe
    $PY scripts/research/replay_engine_backfill.py --from 2024-05-01 --to 2024-05-31
    $PY scripts/research/replay_engine_backfill.py --from 2022-01-03 --to 2026-06-26  # full
    $PY scripts/research/replay_engine_backfill.py --limit-sessions 3                 # quick demo
    $PY scripts/research/replay_engine_backfill.py --calibrate                        # print scale probe

Rerunnable: rows for the covered session_date range are deleted from the replay
DB before insert (idempotent per range).
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.special import erf

# --- make the sidecar `src` package importable when run as a bare script ---
_SIDECAR_ROOT = Path(__file__).resolve().parents[2]
_SRC = _SIDECAR_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from fef_sidecar.db.models import FEFConfidence, FEFDirection, SignalIter  # noqa: E402
from fef_sidecar.engine.fef import FEFEngine  # noqa: E402
from fef_sidecar.engine.types import FEFInputs  # noqa: E402
from fef_sidecar.engine.max_pain import compute_ntm_max_pain  # noqa: E402
from fef_sidecar.engine.types import ExposureRow, OIRow  # noqa: E402

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

PROD_DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
DEFAULT_OUT = str(Path(os.environ["TEMP"]) / "claude" / "replay_signals.db") if os.environ.get("TEMP") \
    else str(_SIDECAR_ROOT / "replay_signals.db")

ET = timezone(timedelta(hours=-4))
BAD_QUALITY = {"crossed", "locked", "zero"}  # quote qualities we must not use
GREEK_SCALE_BASE = 0.01 * 100  # 1% underlying move x 100 contract multiplier

# Empirical GEX magnitude calibration (median |recon|/|real| = 0.182 over the 9
# overlap sessions vs the REAL QuantData exposure_chain_point GAMMA chain ->
# multiply reconstructed net_m by ~5.5 to land in QuantData's $M ballpark).
# This mainly rescues the ABSOLUTE wall_collapse_floor_m gate (5.0 $M); the
# ranking-based gates (nearest-dominant-wall, collapse loss-%) are scale-free.
# DEX is not independently calibrated (no clean real DELTA intraday reference);
# it uses the same multiplier so GEX/DEX stay comparable in magnitude.
GEX_CALIBRATION = 5.5
DEX_CALIBRATION = 5.5

_S2 = math.sqrt(2.0)
_S2PI = math.sqrt(2 * math.pi)
_YEAR_MIN = 365.0 * 24 * 60


# ---------------------------------------------------------------------------
# Field-by-field FEFInputs reconstruction map (drives the feasibility report)
# ---------------------------------------------------------------------------
# status: REAL | REAL-LOGIC | RECON | APPROX | STUB | MISSING
# gates:  True if the live engine's DECISION logic reads this field
FIELD_MAP = [
    # field,                    source,                                     status,      gates
    ("timestamp_ms",           "2-min iter grid (matches live cadence)",   "REAL",      True),
    ("spx_price",              "historical_bar SPX 1-min close (REAL)",    "REAL",      True),
    ("drift_cum_net_m",        "option_flow_minute net_signed_prem cumsum/1e6", "REAL", True),
    ("drift_bias",             "parser DOMINANCE_RATIO=0.15 on cum call/put", "REAL-LOGIC", True),
    ("top_gex_walls",          "RECON: OI + NBBO -> BSM gamma per strike",  "RECON",     True),
    ("top_dex_walls",          "RECON: OI + NBBO -> BSM delta per strike",  "RECON",     True),
    ("ntm_max_pain",           "compute_ntm_max_pain(real OI, real spot)",  "REAL",      True),
    ("raw_max_pain",           "chain-wide max pain on real OI",            "REAL",      False),
    ("aggression_call_pct",    "option_flow_minute buy/(buy+sell) proxy",   "APPROX",    False),
    ("aggression_put_pct",     "option_flow_minute buy/(buy+sell) proxy",   "APPROX",    False),
    ("iv_rank_call_pct",       "not stored historically",                   "STUB",      False),
    ("iv_rank_put_pct",        "not stored historically",                   "STUB",      False),
    ("top_cex_walls",          "RECON: OI + NBBO -> BSM charm per strike",  "RECON",     False),
    ("top_vex_walls",          "RECON: OI + NBBO -> BSM vanna per strike",  "RECON",     False),
    ("top_gex_by_expiry",      "0DTE-only chain; no multi-expiry buckets",  "STUB",      False),
    ("top_dex_by_expiry",      "0DTE-only chain; no multi-expiry buckets",  "STUB",      False),
    ("zero_dte_gex_net_m",     "sum of reconstructed 0DTE GEX walls",       "RECON",     False),
    ("zero_dte_dex_net_m",     "sum of reconstructed 0DTE DEX walls",       "RECON",     False),
    ("recent_orders",          "no historical order-flow stream",           "MISSING",   False),
    ("recent_whale_count_30m", "no historical order-flow stream",           "MISSING",   False),
    ("recent_golden_sweep_count_30m", "no historical order-flow stream",    "MISSING",   False),
]


# ---------------------------------------------------------------------------
# Black-Scholes helpers (r = q = 0, 0DTE) — same construction as
# scripts/research/backtest/dealer_exposure.py, extended with delta.
# ---------------------------------------------------------------------------

def _cdf(x):
    return 0.5 * (1 + erf(x / _S2))


def _pdf(x):
    return np.exp(-0.5 * x * x) / _S2PI


def _implied_vol(price, S, K, T, is_call, iters=24):
    """Vectorized Newton IV solve over a strike vector."""
    sig = np.full(K.shape, 0.6)
    sqT = math.sqrt(T)
    for _ in range(iters):
        srt = sig * sqT
        d1 = (np.log(S / K) + 0.5 * sig * sig * T) / srt
        d2 = d1 - srt
        p = np.where(is_call, S * _cdf(d1) - K * _cdf(d2), K * _cdf(-d2) - S * _cdf(-d1))
        vega = S * _pdf(d1) * sqT
        sig = np.clip(sig - (p - price) / np.maximum(vega, 1e-6), 1e-3, 5.0)
    return sig


def _greeks(S, K, T, sig):
    """Return (gamma, delta_call, delta_put, charm, vanna) per strike."""
    sqT = math.sqrt(T)
    srt = sig * sqT
    d1 = (np.log(S / K) + 0.5 * sig * sig * T) / srt
    d2 = d1 - srt
    pdf = _pdf(d1)
    gamma = pdf / (S * srt)
    delta_c = _cdf(d1)
    delta_p = delta_c - 1.0
    charm = pdf * d2 / (2 * T)        # per-year, r=q=0
    vanna = -pdf * d2 / sig
    return gamma, delta_c, delta_p, charm, vanna


# ---------------------------------------------------------------------------
# Read-only source-DB access
# ---------------------------------------------------------------------------

class SourceDB:
    """Read-only view of the production DB, with per-session loaders."""

    def __init__(self, path: str = PROD_DB):
        self.path = path
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def close(self):
        self.con.close()

    def reconstructable_sessions(self, start: str, end: str) -> list[str]:
        """Sessions in [start, end] that have quotes + OI + flow + SPX bars.

        SPX bars are stored by timestamp_ms (not session_date), so we derive the
        set of SPX bar dates once and intersect.
        """
        q = {r[0] for r in self.con.execute(
            "SELECT DISTINCT session_date FROM option_quote_history "
            "WHERE session_date BETWEEN ? AND ?", (start, end))}
        oi = {r[0] for r in self.con.execute(
            "SELECT DISTINCT session_date FROM option_open_interest "
            "WHERE session_date BETWEEN ? AND ?", (start, end))}
        fl = {r[0] for r in self.con.execute(
            "SELECT DISTINCT session_date FROM option_flow_minute "
            "WHERE session_date BETWEEN ? AND ?", (start, end))}
        spx = self._spx_bar_dates(start, end)
        return sorted(q & oi & fl & spx)

    def _spx_bar_dates(self, start: str, end: str) -> set[str]:
        d0 = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        d1 = int((datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                  + timedelta(days=1)).timestamp() * 1000)
        out: set[str] = set()
        for (ms,) in self.con.execute(
            "SELECT DISTINCT timestamp_ms FROM historical_bar "
            "WHERE symbol='SPX' AND timestamp_ms BETWEEN ? AND ?", (d0, d1)):
            # session date in ET
            out.add(datetime.fromtimestamp(ms / 1000, ET).strftime("%Y-%m-%d"))
        return out

    def load_spx_bars(self, date: str) -> dict[int, float]:
        """{minute_ms -> close} for the session's SPX 1-min bars (REAL spot)."""
        d0 = int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=ET).timestamp() * 1000)
        d1 = d0 + 86_400_000
        out: dict[int, float] = {}
        for ms, close in self.con.execute(
            "SELECT timestamp_ms, close FROM historical_bar "
            "WHERE symbol='SPX' AND timestamp_ms BETWEEN ? AND ? ORDER BY timestamp_ms",
            (d0, d1)):
            out[int(ms)] = float(close)
        return out

    def load_oi(self, date: str):
        """Return (per_side dict {(strike,right):oi}, list[OIRow] for max-pain)."""
        per_side: dict[tuple[float, str], int] = {}
        agg: dict[float, list[int]] = {}
        for k, r, oi in self.con.execute(
            "SELECT strike, right, open_interest FROM option_open_interest WHERE session_date=?",
            (date,)):
            per_side[(float(k), r)] = int(oi)
            slot = agg.setdefault(float(k), [0, 0])
            if r == "C":
                slot[0] += int(oi)
            else:
                slot[1] += int(oi)
        oi_rows = [OIRow(strike=k, call_oi=v[0], put_oi=v[1]) for k, v in sorted(agg.items())]
        return per_side, oi_rows

    def load_flow(self, date: str) -> list[tuple[int, float, float, float]]:
        """Sorted [(ts_ms, call_net, put_net, net_signed)] for the session."""
        rows = []
        for ts, cb, cs, pb, ps, ns in self.con.execute(
            "SELECT ts_ms, call_buy_prem, call_sell_prem, put_buy_prem, put_sell_prem, "
            "net_signed_prem FROM option_flow_minute WHERE session_date=? ORDER BY ts_ms",
            (date,)):
            rows.append((int(ts), float(cb) - float(cs), float(pb) - float(ps), float(ns)))
        return rows

    def load_quotes(self, date: str):
        """Return (quote index {(ts,strike,right):(bid,ask,qual)}, strikes, minutes)."""
        q: dict[tuple[int, float, str], tuple[float, float, str]] = {}
        strikes, minutes = set(), set()
        for ts, k, r, bid, ask, qual in self.con.execute(
            "SELECT timestamp_ms, strike, right, bid, ask, quote_quality "
            "FROM option_quote_history WHERE session_date=?", (date,)):
            q[(int(ts), float(k), r)] = (float(bid), float(ask), qual)
            strikes.add(float(k))
            minutes.add(int(ts))
        return q, sorted(strikes), sorted(minutes)


def _mid(q, ts, k, r):
    v = q.get((ts, k, r))
    if not v or v[2] in BAD_QUALITY:
        return None
    return (v[0] + v[1]) / 2.0


# ---------------------------------------------------------------------------
# Per-iter reconstruction
# ---------------------------------------------------------------------------

def reconstruct_walls(spot, minute_ms, quotes, strikes, oi_per_side, minutes_to_close):
    """Reconstruct per-strike GEX/DEX/CEX/VEX walls at one minute.

    Returns (gex_rows, dex_rows, cex_rows, vex_rows), each a list[ExposureRow]
    sorted by abs(net_m) desc and truncated to the top-8 (engine takes top-5).
    Empty lists if too close to expiry / too few strikes.
    """
    if minutes_to_close <= 1.0:
        return [], [], [], []
    T = minutes_to_close / _YEAR_MIN

    Ks, cm, pm, coi, poi = [], [], [], [], []
    for k in strikes:
        c = _mid(quotes, minute_ms, k, "C")
        p = _mid(quotes, minute_ms, k, "P")
        if c is None or p is None:
            continue
        oc = oi_per_side.get((k, "C"), 0)
        op = oi_per_side.get((k, "P"), 0)
        if oc <= 0 and op <= 0:
            continue
        Ks.append(k); cm.append(c); pm.append(p); coi.append(oc); poi.append(op)
    if len(Ks) < 6:
        return [], [], [], []

    Ks = np.array(Ks, float); cm = np.array(cm, float); pm = np.array(pm, float)
    coi = np.array(coi, float); poi = np.array(poi, float)

    ivc = _implied_vol(cm, spot, Ks, T, True)
    ivp = _implied_vol(pm, spot, Ks, T, False)
    gc, dcc, dpc, chc, vnc = _greeks(spot, Ks, T, ivc)
    gp, dcp, dpp, chp, vnp = _greeks(spot, Ks, T, ivp)

    gscale = (spot * spot * GREEK_SCALE_BASE) / 1e6 * GEX_CALIBRATION   # $M
    dscale = (spot * GREEK_SCALE_BASE) / 1e6 * DEX_CALIBRATION          # $M
    # charm/vanna are research-only (engine never gates on them) — keep the raw
    # gamma-style scale so magnitudes are self-consistent for pattern discovery.
    cscale = (spot * spot * GREEK_SCALE_BASE) / 1e6 * GEX_CALIBRATION
    vscale = (spot * spot * GREEK_SCALE_BASE) / 1e6 * GEX_CALIBRATION

    def rows_for(call_contrib, put_contrib):
        """Build ExposureRow list. call_m/put_m signed so net_m = call_m + put_m,
        side=CALL when net_m>0 (matches ExposureRow.side + engine convention)."""
        out = []
        for i, k in enumerate(Ks):
            call_m = float(call_contrib[i])
            put_m = float(put_contrib[i])
            net = call_m + put_m
            out.append(ExposureRow(strike=float(k), call_m=round(call_m, 4),
                                   put_m=round(put_m, 4), net_m=round(net, 4)))
        out.sort(key=lambda r: abs(r.net_m), reverse=True)
        return out[:8]

    # GEX: dealer long call gamma (+), short put gamma (-) — SqueezeMetrics naive.
    gex = rows_for(gc * coi * gscale, -gp * poi * gscale)
    # DEX: call delta (+) vs put delta magnitude (-); call-heavy strike -> net>0.
    dex = rows_for(dcc * coi * dscale, dpc * poi * dscale)  # dpc = delta_put (negative)
    cex = rows_for(chc * coi * cscale, -chp * poi * cscale)
    vex = rows_for(vnc * coi * vscale, -vnp * poi * vscale)
    return gex, dex, cex, vex


def _drift_bias(cum_call: float, cum_put: float) -> str:
    """Reproduce quantdata/parser.py bias logic (DOMINANCE_RATIO = 0.15)."""
    total_net = cum_call - cum_put
    total_abs = abs(cum_call) + abs(cum_put)
    if total_abs <= 0:
        return "NEUTRAL"
    if total_net > 0.15 * total_abs:
        return "BULLISH"
    if total_net < -0.15 * total_abs:
        return "BEARISH"
    return "NEUTRAL"


# ---------------------------------------------------------------------------
# Session replay
# ---------------------------------------------------------------------------

def run_session(db: SourceDB, engine: FEFEngine, date: str, interval_sec: int):
    """Replay one session through the engine. Returns list[SignalIter] rows."""
    bars = db.load_spx_bars(date)
    if not bars:
        return []
    oi_per_side, oi_rows = db.load_oi(date)
    if not oi_rows:
        return []
    flow = db.load_flow(date)
    if not flow:
        return []
    quotes, strikes, qminutes = db.load_quotes(date)
    if not qminutes:
        return []

    engine.reset()  # matches live new-session reset

    # Cumulative drift/flow, indexed by minute (for bias + drift_cum_net_m).
    cum_call = cum_put = cum_net = 0.0
    cum_by_min: dict[int, tuple[float, float, float]] = {}
    cum_buy_call = cum_sell_call = cum_buy_put = cum_sell_put = 0.0
    aggr_by_min: dict[int, tuple[float, float]] = {}
    for ts, call_net, put_net, net_signed in flow:
        cum_call += call_net
        cum_put += put_net
        cum_net += net_signed
        cum_by_min[ts] = (cum_call, cum_put, cum_net)

    # Aggression proxy needs raw buy/sell; re-read once (cheap) — buy premium as
    # aggressive-lift proxy. Non-gating, informational only.
    for ts, cb, cs, pb, ps in db.con.execute(
        "SELECT ts_ms, call_buy_prem, call_sell_prem, put_buy_prem, put_sell_prem "
        "FROM option_flow_minute WHERE session_date=? ORDER BY ts_ms", (date,)):
        cum_buy_call += float(cb); cum_sell_call += float(cs)
        cum_buy_put += float(pb); cum_sell_put += float(ps)
        ct = cum_buy_call + cum_sell_call
        pt = cum_buy_put + cum_sell_put
        aggr_by_min[int(ts)] = (
            (cum_buy_call / ct * 100.0) if ct > 0 else None,
            (cum_buy_put / pt * 100.0) if pt > 0 else None,
        )

    # Build the 2-min iter grid over the RTH minute spine (flow minutes).
    spine = [ts for ts, *_ in flow]
    step = max(1, interval_sec // 60)
    grid = spine[::step]

    def nearest_leq(sorted_keys, target):
        prev = None
        for k in sorted_keys:
            if k > target:
                break
            prev = k
        return prev

    bar_minutes = sorted(bars.keys())
    raw_mp = compute_ntm_max_pain(oi_rows, 0.0, window=1e12)  # chain-wide (spot-independent strike set)

    rows: list[SignalIter] = []
    for iter_num, gts in enumerate(grid, start=1):
        # REAL spot: SPX 1-min close at (or just before) this grid minute.
        bmin = nearest_leq(bar_minutes, gts) or bar_minutes[0]
        spot = bars[bmin]

        # Drift + bias from cumulative flow up to this minute.
        cc, cp, cn = cum_by_min.get(gts, (cum_call, cum_put, cum_net))
        drift_cum_net_m = cn / 1e6
        bias = _drift_bias(cc, cp)

        # Max pain (engine's own function on REAL OI at REAL spot).
        ntm = compute_ntm_max_pain(oi_rows, spot).max_pain
        raw = compute_ntm_max_pain(oi_rows, spot, window=1e12).max_pain

        # Reconstructed per-strike walls.
        qmin = nearest_leq(qminutes, gts) or qminutes[0]
        now = datetime.fromtimestamp(gts / 1000, ET)
        mins_close = (now.replace(hour=16, minute=0, second=0, microsecond=0) - now).total_seconds() / 60.0
        gex, dex, cex, vex = reconstruct_walls(spot, qmin, quotes, strikes, oi_per_side, mins_close)

        aggr = aggr_by_min.get(gts, (None, None))
        zero_dte_gex = sum(r.net_m for r in gex) if gex else None
        zero_dte_dex = sum(r.net_m for r in dex) if dex else None

        inputs = FEFInputs(
            timestamp_ms=gts,
            spx_price=spot,
            drift_cum_net_m=drift_cum_net_m,
            drift_bias=bias,
            top_gex_walls=gex[:5],
            top_dex_walls=dex[:5],
            ntm_max_pain=ntm,
            raw_max_pain=raw,
            aggression_call_pct=aggr[0],
            aggression_put_pct=aggr[1],
            iv_rank_call_pct=None,   # STUB — not stored historically; non-gating
            iv_rank_put_pct=None,
            top_cex_walls=cex[:5],
            top_vex_walls=vex[:5],
            top_gex_by_expiry=[],    # STUB — 0DTE-only chain
            top_dex_by_expiry=[],
            zero_dte_gex_net_m=zero_dte_gex,
            zero_dte_dex_net_m=zero_dte_dex,
            recent_orders=[],        # MISSING — no historical order-flow stream
            recent_whale_count_30m=0,
            recent_golden_sweep_count_30m=0,
        )

        verdict = engine.push(inputs)

        extras = verdict.to_log_extras()
        extras["replay"] = {
            "source": "replay_engine_backfill",
            "spx_source": "historical_bar SPX 1min (REAL)",
            "walls_source": "RECON OI+NBBO BSM",
            "gex_calibration": GEX_CALIBRATION,
            "iter_ts_ms": gts,
        }

        def _wall_json(rows_):
            return [{"strike": r.strike, "call_m": r.call_m, "put_m": r.put_m,
                     "net_m": r.net_m, "side": r.side} for r in rows_]

        rows.append(SignalIter(
            session_date=date,
            iter_num=iter_num,
            timestamp=datetime.fromtimestamp(gts / 1000, timezone.utc),
            spx_price=spot,
            direction=verdict.direction,
            confidence=verdict.confidence,
            reasoning=verdict.reasoning,
            drift_cum_net_m=drift_cum_net_m,
            drift_bias=bias,
            ntm_max_pain=ntm,
            raw_max_pain=raw,
            top_gex_walls=_wall_json(gex[:5]),
            top_dex_walls=_wall_json(dex[:5]),
            top_cex_walls=_wall_json(cex[:5]),
            top_vex_walls=_wall_json(vex[:5]),
            top_gex_by_expiry=[],
            top_dex_by_expiry=[],
            zero_dte_gex_net_m=zero_dte_gex,
            zero_dte_dex_net_m=zero_dte_dex,
            recent_orders=[],
            recent_whale_count_30m=0,
            recent_golden_sweep_count_30m=0,
            aggression_call_pct=aggr[0],
            aggression_put_pct=aggr[1],
            iv_rank_call_pct=None,
            iv_rank_put_pct=None,
            extras=extras,
            provider_mode="replay",       # lets tools filter synthetic rows
            engine_version=2,
        ))
    return rows


# ---------------------------------------------------------------------------
# Outcome-scoring SEAM (intentionally NOT implemented here)
# ---------------------------------------------------------------------------

def score_outcome(signal: SignalIter, source_db: SourceDB) -> dict:
    """SEAM for forward-outcome scoring — DO NOT implement with R x $500.

    This is the clean hand-off point for the CONSUMER half. Whoever wires
    scoring MUST value each synthetic verdict in **real option dollars** using
    tick-accurate historical NBBO — buy the ASK on entry, sell the BID on exit,
    with NO lookahead — via scripts/research/backtest/tick_fetch.py against
    option_quote_history (34M rows, 2022-2026). A parallel audit is verifying
    the correct $/R; do NOT bake any dollar conversion in here.

    Expected shape when implemented:
        {"entry_px": float, "exit_px": float, "exit_reason": str,
         "pnl_per_contract": float, "r_multiple": float, "mfe": float, "mae": float}

    Left as an explicit failure so nobody silently scores with a fake ruler.
    """
    raise NotImplementedError(
        "score_outcome is a documented seam — wire tick-accurate real-option-$ "
        "scoring (tick_fetch.py, buy-ask/sell-bid, no lookahead), NOT R x $500."
    )


# ---------------------------------------------------------------------------
# Replay-DB writer (SEPARATE DB — never production)
# ---------------------------------------------------------------------------

class ReplayWriter:
    """Writes synthetic SignalIter rows to a SEPARATE sqlite DB via SQLModel,
    so the schema matches production exactly and the research tools can read it
    (point FEF_DB_URL at this file, or ATTACH it)."""

    def __init__(self, out_path: str):
        from sqlmodel import SQLModel, create_engine
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        assert os.path.abspath(out_path) != os.path.abspath(PROD_DB), \
            "refusing to write into the production DB"
        self.out_path = out_path
        self.engine = create_engine(f"sqlite:///{out_path}")
        SQLModel.metadata.create_all(self.engine)

    def replace_range(self, dates: list[str], rows: list[SignalIter]) -> int:
        from sqlmodel import Session as SQLSession, delete
        from sqlalchemy import text
        with SQLSession(self.engine) as s:
            for d in dates:
                s.exec(delete(SignalIter).where(SignalIter.session_date == d))
            s.add_all(rows)
            s.commit()
            n = s.exec(text("SELECT COUNT(*) FROM signal_iter")).one()[0]
        return int(n)


# ---------------------------------------------------------------------------
# Calibration probe (standalone diagnostic)
# ---------------------------------------------------------------------------

def calibrate(db: SourceDB):
    """Compare reconstructed per-strike GEX vs the REAL exposure_chain_point
    GAMMA chain over the 9 overlap sessions. Prints sign agreement + magnitude
    ratio + top-5 strike overlap (the honest faithfulness numbers)."""
    overlap = [r[0] for r in db.con.execute(
        "SELECT DISTINCT ecp.session_date FROM exposure_chain_point ecp "
        "WHERE ecp.greek_type='GAMMA' AND EXISTS "
        "(SELECT 1 FROM option_quote_history q WHERE q.session_date=ecp.session_date) "
        "ORDER BY ecp.session_date")]
    print(f"Overlap sessions with real per-strike GAMMA + quotes: {overlap}\n")
    ratios, sign_hits, sign_tot, ov_fracs = [], 0, 0, []
    for d in overlap:
        oi_per_side, _ = db.load_oi(d)
        quotes, strikes, minutes = db.load_quotes(d)
        if not minutes or not oi_per_side:
            continue
        tgt = datetime.strptime(d, "%Y-%m-%d").replace(hour=15, minute=30, tzinfo=ET)
        tms = int(tgt.timestamp() * 1000)
        qmin = min(minutes, key=lambda m: abs(m - tms))
        now = datetime.fromtimestamp(qmin / 1000, ET)
        mins_close = (now.replace(hour=16, minute=0, second=0, microsecond=0) - now).total_seconds() / 60.0
        # parity spot for the probe (real SPX bars also fine; parity keeps it self-contained)
        fwds = []
        for k in strikes:
            c = _mid(quotes, qmin, k, "C"); p = _mid(quotes, qmin, k, "P")
            if c is not None and p is not None:
                fwds.append((abs(c - p), k + c - p))
        if not fwds:
            continue
        fwds.sort()
        spot = statistics.median([f for _, f in fwds[:5]])
        gex, _, _, _ = reconstruct_walls(spot, qmin, quotes, strikes, oi_per_side, mins_close)
        recon = {r.strike: r.net_m for r in reconstruct_full_gex(spot, qmin, quotes, strikes, oi_per_side, mins_close)}
        real = {float(k): float(nm) for k, nm in db.con.execute(
            "SELECT strike, net_m FROM exposure_chain_point WHERE session_date=? AND greek_type='GAMMA'", (d,))}
        if not real:
            continue
        near = lambda dd: sorted(({k: v for k, v in dd.items() if abs(k - spot) <= 60}).items(),
                                 key=lambda kv: abs(kv[1]), reverse=True)[:5]
        rset = {k for k, _ in near(recon)}; aset = {k for k, _ in near(real)}
        ov_fracs.append(len(rset & aset) / 5.0)
        for k in [x for x in recon if x in real and abs(x - spot) <= 60 and abs(real[x]) > 1e-6]:
            sign_tot += 1
            if (recon[k] > 0) == (real[k] > 0):
                sign_hits += 1
            ratios.append(abs(recon[k]) / abs(real[k]))
        print(f"  {d}: spot~{spot:.1f} top5_overlap={len(rset & aset)/5:.0%}")
    print(f"\nSign agreement: {sign_hits}/{sign_tot} = {sign_hits/max(sign_tot,1):.0%}")
    if ratios:
        print(f"Magnitude |recon|/|real|: median={statistics.median(ratios):.3g} "
              f"(current GEX_CALIBRATION={GEX_CALIBRATION})")
    if ov_fracs:
        print(f"Top-5 wall strike overlap: mean={statistics.mean(ov_fracs):.0%}")


def reconstruct_full_gex(spot, minute_ms, quotes, strikes, oi_per_side, minutes_to_close):
    """Full (untruncated) GEX rows — used only by the calibration probe."""
    if minutes_to_close <= 1.0:
        return []
    T = minutes_to_close / _YEAR_MIN
    Ks, cm, pm, coi, poi = [], [], [], [], []
    for k in strikes:
        c = _mid(quotes, minute_ms, k, "C"); p = _mid(quotes, minute_ms, k, "P")
        if c is None or p is None:
            continue
        oc = oi_per_side.get((k, "C"), 0); op = oi_per_side.get((k, "P"), 0)
        if oc <= 0 and op <= 0:
            continue
        Ks.append(k); cm.append(c); pm.append(p); coi.append(oc); poi.append(op)
    if len(Ks) < 6:
        return []
    Ks = np.array(Ks, float); cm = np.array(cm, float); pm = np.array(pm, float)
    coi = np.array(coi, float); poi = np.array(poi, float)
    ivc = _implied_vol(cm, spot, Ks, T, True); ivp = _implied_vol(pm, spot, Ks, T, False)
    gc, *_ = _greeks(spot, Ks, T, ivc); gp, *_ = _greeks(spot, Ks, T, ivp)
    gscale = (spot * spot * GREEK_SCALE_BASE) / 1e6 * GEX_CALIBRATION
    net = (gc * coi - gp * poi) * gscale
    return [ExposureRow(strike=float(k), call_m=0.0, put_m=0.0, net_m=float(v))
            for k, v in zip(Ks, net)]


# ---------------------------------------------------------------------------
# Engine factory — mirrors state.build_engine() (same defaults / env vars)
# ---------------------------------------------------------------------------

def build_replay_engine(interval_sec: int, flow_surge_gate: bool) -> FEFEngine:
    threshold_scale = max(0.1, interval_sec / 120.0)
    return FEFEngine(
        wall_proximity_pts=float(os.environ.get("FEF_WALL_PROXIMITY_PTS", "5.0")),
        wall_collapse_pct=float(os.environ.get("FEF_WALL_COLLAPSE_PCT", "0.20")),
        wall_collapse_floor_m=float(os.environ.get("FEF_WALL_COLLAPSE_FLOOR_M", "5.0")),
        absorption_fe_ratio_min=float(os.environ.get("FEF_ABSORPTION_FE_RATIO_MIN", "5.0")),
        drift_sanity_ceiling_m=float(os.environ.get("FEF_DRIFT_SANITY_CEILING_M", "250.0")),
        flow_surge_gate_enabled=flow_surge_gate,
        threshold_scale=threshold_scale,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="from_d", help="Range start YYYY-MM-DD")
    ap.add_argument("--to", dest="to_d", help="Range end YYYY-MM-DD")
    ap.add_argument("--out", default=DEFAULT_OUT, help="Replay DB path (SEPARATE from prod)")
    ap.add_argument("--interval-sec", type=int, default=120, help="Iter cadence (live default 120)")
    ap.add_argument("--limit-sessions", type=int, default=0, help="Cap sessions (0 = all) for a quick demo")
    ap.add_argument("--flow-surge-gate", dest="gate", action="store_true", default=True,
                    help="Enable the flow-surge directional gate (DEFAULT for replay so the "
                         "engine emits actionable verdicts the research tools analyze).")
    ap.add_argument("--no-flow-surge-gate", dest="gate", action="store_false",
                    help="Disable the gate = match live's post-2026-06-30 config (edge retired). "
                         "Produces ~0 actionable rows.")
    ap.add_argument("--calibrate", action="store_true", help="Run the wall-reconstruction calibration probe and exit")
    args = ap.parse_args()

    db = SourceDB()

    if args.calibrate:
        calibrate(db)
        db.close()
        return 0

    if not (args.from_d and args.to_d):
        ap.error("provide --from and --to (or --calibrate)")

    sessions = db.reconstructable_sessions(args.from_d, args.to_d)
    if args.limit_sessions:
        sessions = sessions[:args.limit_sessions]
    if not sessions:
        print(f"No reconstructable sessions in {args.from_d}..{args.to_d}")
        db.close()
        return 1

    print(f"Replay: {len(sessions)} sessions {sessions[0]}..{sessions[-1]}  "
          f"(interval={args.interval_sec}s, flow_surge_gate={args.gate})")
    print(f"Out DB: {args.out}\n")

    engine = build_replay_engine(args.interval_sec, args.gate)
    writer = ReplayWriter(args.out)

    all_rows: list[SignalIter] = []
    t0 = time.time()
    actionable = 0
    for i, d in enumerate(sessions, 1):
        srows = run_session(db, engine, d, args.interval_sec)
        act = sum(1 for r in srows if r.direction != FEFDirection.WAIT)
        actionable += act
        all_rows.extend(srows)
        if i % 25 == 0 or i == len(sessions):
            print(f"  [{i}/{len(sessions)}] {d}: {len(srows)} iters, {act} actionable "
                  f"({time.time()-t0:.0f}s)", flush=True)

    total = writer.replace_range(sessions, all_rows)
    dur = time.time() - t0
    print(f"\nDONE: wrote {len(all_rows)} synthetic signal_iter rows "
          f"({actionable} actionable, {len(all_rows)-actionable} WAIT) across "
          f"{len(sessions)} sessions in {dur:.0f}s.")
    print(f"Replay DB now holds {total} signal_iter rows: {args.out}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
