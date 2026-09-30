"""FROZEN definition of the vwap_loss_short shadow candidate (2026-05-29).

This is the out-of-sample forward-validation contract. The parameters below are
FROZEN as of the selection date — DO NOT tune them. Forward sessions (after
IN_SAMPLE_CUTOFF) are scored on this exact definition; if the edge survives only
on forward data it's real, if it evaporates it was selection (winner's curse).

Candidate (selected from 8 cells, survived: random / opposite / sell-the-open /
beta-adjust / time-matched-random / ES->SPX instrument-consistency):
  detect vwap_loss_short on ES RTH 5-min bars (same-bar entry: hold_bars=1,
  reject_hold_bars=0); exit = momentum ATR 2:1 (stop 1xATR, target 2xATR);
  friction 1.5pt round-trip; R in the trade's own risk units. In-sample n=14,
  effective ~5 sessions, E[R] +0.62 (EXPECT LOWER forward per winner's curse).

The detector modules (dale.resample, vwap.detect) are frozen-by-policy for this
shadow — changing them invalidates the forward comparison; bump DEFINITION_VERSION
if they ever must change.
"""

from __future__ import annotations

import bisect
import hashlib

from fef_sidecar import et_time
from fef_sidecar.engine.signals import dale, vwap
from fef_sidecar.engine.signals.dale import Bar

# ---- FROZEN PARAMETERS ----
# v2 (2026-05-30): tightened per the ChatGPT-5.5 realism review — resolves the
# four spec ambiguities BEFORE forward data accrues (costless re-baseline):
#   - ENTRY: next 1-min OPEN after the 5-min signal bar closes (no same-bar-close
#     optimism; a 5-min close is not an executable fill price).
#   - OVERLAP: one trade at a time — signals while a position is open are ignored
#     (an extended move can't masquerade as several independent wins).
#   - AMBIGUOUS BARS: adverse-first (both target & stop in one bar => STOP), not a
#     Brownian-bridge proxy — the proxy is for exploration, not a validation gate.
#   - STOP: max(1xATR, MIN_STOP_PTS) so friction/ATR stays bounded when ATR
#     compresses (1xATR alone hit 3pt -> 1.5pt friction = 0.5R).
# Realism re-baseline (in-sample, next-1m-open): n=12, 58.3% win, E[R] +0.49
# (down from the optimistic +0.62; +0.23 at +60s latency, ~0 by +5min — execution
# speed is load-bearing, which is why option-dollar validation is the next gate).
SIGNAL_NAME = "vwap_loss_short"
TF = 5
HOLD_BARS = 1
REJECT_HOLD_BARS = 0
ATR_PERIOD = 14
ATR_STOP_MULT = 1.0
ATR_TARGET_MULT = 2.0
MIN_STOP_PTS = 4.0                 # floor on the ATR stop (friction-vs-compression guard)
ENTRY = "next_1m_open"             # NOT same-bar close
OVERLAP = "one_trade_at_a_time"
AMBIGUOUS = "adverse_first"
FRICTION_PTS = 1.5
IN_SAMPLE_CUTOFF = "2026-05-29"   # sessions <= this are in-sample (selection); > is forward
ES_BAR_SIZE = "1 min RTH"

DEFINITION_VERSION = "v2-2026-05-30"

# Pre-registered evaluation checkpoints (avoid optional-stopping: evaluate the
# frozen strategy ONCE at each, do not promote early on a temporarily good curve).
CHECKPOINTS = {20: "collect-more-only", 40: "one-contract-pilot-eligible", 80: "scale-eligible"}


def definition_hash() -> str:
    payload = "|".join(str(x) for x in (
        SIGNAL_NAME, TF, HOLD_BARS, REJECT_HOLD_BARS, ATR_PERIOD,
        ATR_STOP_MULT, ATR_TARGET_MULT, MIN_STOP_PTS, ENTRY, OVERLAP, AMBIGUOUS,
        FRICTION_PTS, ES_BAR_SIZE, DEFINITION_VERSION,
    ))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _atr(bars: list[Bar], period: int = ATR_PERIOD) -> list[float | None]:
    trs = []
    for i, b in enumerate(bars):
        trs.append(b.high - b.low if i == 0 else
                   max(b.high - b.low, abs(b.high - bars[i-1].close), abs(b.low - bars[i-1].close)))
    out: list[float | None] = [None] * len(bars)
    for i in range(len(bars)):
        if i + 1 >= period:
            out[i] = sum(trs[i-period+1:i+1]) / period
    return out


def _et_date(ts_ms: int) -> str:
    # DST-correct ET date. Identical to the old fixed UTC-4 on the ES RTH bars
    # this definition scores (see vwap._ET), so the frozen outputs do not move.
    return et_time.session_date_et(ts_ms)


def _walk_short(entry, target, stop, bars1m, ts1, start_ts, horizon):
    """Forward-walk a SHORT trade (target<entry<stop) on 1-min bars from start_ts.
    ADVERSE-FIRST: if a bar touches both, the STOP wins (conservative). Returns
    (realized_r_in_risk_units, exit_ms). Timeout = mark-to-last-close."""
    i = bisect.bisect_left(ts1, start_ts)
    risk = stop - entry
    last = None
    for k in range(i, len(bars1m)):
        b = bars1m[k]
        if _et_date(b.ts_ms) > horizon:
            break
        last = b
        if b.high >= stop:                 # adverse-first
            return -1.0, b.ts_ms
        if b.low <= target:
            return (entry - target) / risk, b.ts_ms
    if last is None:
        return None, None
    return (entry - last.close) / risk, last.ts_ms


def score_session(es_rth_1m: list[Bar]) -> list[dict]:
    """Detect + score vwap_loss_short on ES RTH 1-min bars (may span sessions),
    under the v2 frozen spec: next-1m-OPEN entry, ONE-TRADE-AT-A-TIME, adverse-
    first, ATR stop floored at MIN_STOP_PTS. One dict per TAKEN signal."""
    es5 = dale.resample(es_rth_1m, TF)
    atr = _atr(es5)
    imap = {b.ts_ms: i for i, b in enumerate(es5)}
    ts1 = [b.ts_ms for b in es_rth_1m]
    sigs = sorted(
        (s for s in vwap.detect(es5, timeframe_min=TF, hold_bars=HOLD_BARS,
                                 reject_hold_bars=REJECT_HOLD_BARS) if s.kind == SIGNAL_NAME),
        key=lambda s: s.ts_ms,
    )
    out: list[dict] = []
    busy_until = 0  # one-trade-at-a-time gate (ms)
    for s in sigs:
        i = imap.get(s.ts_ms)
        a = atr[i] if i is not None else None
        if a is None or a <= 0:
            continue
        # realistic entry: open of the first 1-min bar at/after the 5-min close
        ei = bisect.bisect_left(ts1, s.ts_ms + TF * 60_000)
        if ei >= len(es_rth_1m):
            continue
        entry_ts = ts1[ei]
        if entry_ts < busy_until:          # position still open -> ignore
            continue
        entry = es_rth_1m[ei].open
        risk = max(ATR_STOP_MULT * a, MIN_STOP_PTS)
        stop = entry + risk                # short
        target = entry - ATR_TARGET_MULT * a
        sd = _et_date(s.ts_ms)
        r, exit_ms = _walk_short(entry, target, stop, es_rth_1m, ts1, entry_ts, sd)
        if r is None:
            continue
        busy_until = exit_ms or busy_until
        r -= FRICTION_PTS / risk
        out.append({
            "signal_name": SIGNAL_NAME, "definition_version": DEFINITION_VERSION,
            "session_date": sd, "timestamp_ms": s.ts_ms,
            "in_sample": sd <= IN_SAMPLE_CUTOFF,
            "direction": "PUTS", "entry": entry, "stop": stop, "target": target,
            "rr": (entry - target) / risk, "r_outcome": r, "win": 1.0 if r > 0 else 0.0,
            "friction_pts": FRICTION_PTS,
        })
    return out


# ---------------------------------------------------------------------------
# Persistence + ledger integration (shared by the nightly chain and the manual
# runner — single source of truth, no duplicated DB code).
# ---------------------------------------------------------------------------

VARIANT_ID = f"{SIGNAL_NAME}@{DEFINITION_VERSION}"


async def load_es_rth_bars() -> list[Bar]:
    """ES RTH 1-min bars from historical_bar ('ES'/'1 min RTH')."""
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import HistoricalBar
    async with get_session() as s:
        rows = (await s.exec(
            select(HistoricalBar).where(HistoricalBar.symbol == "ES")
            .where(HistoricalBar.bar_size == ES_BAR_SIZE)
            .order_by(HistoricalBar.timestamp_ms)  # type: ignore[arg-type]
        )).all()
    return [Bar(ts_ms=r.timestamp_ms, open=r.open, high=r.high, low=r.low,
                close=r.close, volume=r.volume) for r in rows]


async def persist_outcomes(outcomes: list[dict]) -> int:
    """Idempotent insert into shadow_forward_signal (unique signal+timestamp)."""
    from sqlalchemy.exc import IntegrityError
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import ShadowForwardSignal
    added = 0
    for o in outcomes:
        try:
            async with get_session() as s:
                s.add(ShadowForwardSignal(**o))
                await s.flush()
            added += 1
        except IntegrityError:
            continue
    return added


async def register_variant() -> None:
    """Idempotent upsert into variant_ledger so this candidate counts toward the
    project-wide honest N_trials (rather than a hardcoded number)."""
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import VariantLedger, _utcnow
    async with get_session() as s:
        existing = (await s.exec(
            select(VariantLedger).where(VariantLedger.variant_id == VARIANT_ID)
        )).first()
        if existing is not None:
            existing.last_seen_at = _utcnow()
            s.add(existing)
        else:
            s.add(VariantLedger(
                variant_id=VARIANT_ID, family="signal_source", sub_family="vwap",
                description="vwap_loss_short directional-short forward shadow candidate",
                hypothesis="VWAP loss from above = intraday distribution continuation (short).",
                tested_in="forward_shadow", sample_window=f"{IN_SAMPLE_CUTOFF}.. (forward)",
                outcome_summary="pending", still_active=True,
                variant_params={"definition_hash": definition_hash(), "tf": TF,
                                "exit": "atr_2to1", "friction_pts": FRICTION_PTS,
                                "selected_from_cells": 8},
            ))


async def forward_n_trials() -> int:
    """Honest N_trials = active variants in the ledger, floored at the 8-cell
    selection penalty for this candidate (per the methodology review)."""
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import VariantLedger
    async with get_session() as s:
        n = len((await s.exec(
            select(VariantLedger).where(VariantLedger.still_active == True)  # noqa: E712
        )).all())
    return max(8, n)
