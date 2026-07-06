"""Pillar reconstruction for the PhaseDetector backtest (READ-ONLY research).

Rebuilds, per session at 1-min resolution, the six PhaseSample inputs the live
`fef_sidecar.engine.phase_detector.PhaseDetector` consumes, aligned to the INDEPENDENT
IBKR SPX 1-min ruler (never a parity-reconstructed spot — see MEMORY
[[validate-against-independent-price]]).

Pillar sources & proxy choices (documented honestly — live uses finer 1s data + real
drift/IV; these are 1-min historical proxies):

  1. PRICE (spx)      IBKR SPX 1-min close (ibkr_ruler). Also the forward-move ruler.
  2. AGGRESSION       option_flow_minute. "Aggressive $" = premium that LIFTS THE OFFER
                      (the initiating/aggressor side): call_buy_prem for calls, put_buy_prem
                      for puts. call_pct = 100*call_buy/(call_buy+put_buy); put_pct the analog.
                      spread=call_pct-put_pct is net directional aggression. None when both 0.
  3. ATM IV (atm_iv)  option_dealer_exposure.iv_atm (nearest within TOL). Bounds the usable
                      window to 2022..2025-08 (the DEV set) -> holdout-safe by construction.
  4. DRIFT (drift_bias) sign of a CAUSAL z-scored trailing 5-min sum of net_signed_prem:
                      z = roll5 / (trailing-30 std(net)*sqrt5); BULLISH z>=+0.5, BEARISH z<=-0.5,
                      else NEUTRAL. Deadband 0.5 keeps a real NEUTRAL band; fully causal.
  5. MAX_PAIN         static per session = strike minimizing total ITM payout to holders over
                      start-of-day per-strike OI (option_open_interest). argmin_S sum_k
                      call_oi[k]*max(0,S-k) + put_oi[k]*max(0,k-S).

Everything here is pure reconstruction; no engine code is modified.
"""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

import numpy as np

from fef_sidecar.engine.phase_detector import PhaseSample

from .ibkr_ruler import ibkr_series

ET = ZoneInfo("America/New_York")
TOL_MS = 90_000            # nearest-observation match tolerance for pillar alignment
RTH_LO, RTH_HI = 9.5, 16.0  # ET hours kept for the sample spine
DRIFT_WIN = 30             # trailing bars for the causal net-flow scale
DRIFT_ROLL = 5             # trailing bars summed for the drift signal
DRIFT_Z = 0.5              # |z| deadband separating BULLISH/BEARISH from NEUTRAL


def _et_h(ts_ms: int) -> float:
    x = dt.datetime.fromtimestamp(ts_ms / 1000, ET)
    return x.hour + x.minute / 60.0


def _nearest(ts_arr: np.ndarray, target: int, tol: int = TOL_MS):
    """Index of the ts in ts_arr nearest `target` within tol, else None."""
    if len(ts_arr) == 0:
        return None
    j = int(np.searchsorted(ts_arr, target))
    best, bd = None, tol + 1
    for cnd in (j - 1, j):
        if 0 <= cnd < len(ts_arr):
            dd = abs(int(ts_arr[cnd]) - target)
            if dd <= tol and dd < bd:
                bd, best = dd, cnd
    return best


def _drift_series(net: np.ndarray) -> list[str]:
    """Causal BULLISH/BEARISH/NEUTRAL from net_signed_prem (see module docstring)."""
    n = len(net)
    if n == 0:
        return []
    cs = np.concatenate(([0.0], np.cumsum(net)))
    roll5 = np.array([cs[i + 1] - cs[max(0, i + 1 - DRIFT_ROLL)] for i in range(n)])
    out = []
    for i in range(n):
        if i < 9:                       # warm-up: not enough history for a scale
            out.append("NEUTRAL"); continue
        seg = net[max(0, i - DRIFT_WIN + 1):i + 1]
        sd = float(seg.std(ddof=1)) if len(seg) > 1 else 0.0
        scale = sd * math.sqrt(DRIFT_ROLL)
        if scale <= 0:
            out.append("NEUTRAL"); continue
        z = roll5[i] / scale
        out.append("BULLISH" if z >= DRIFT_Z else "BEARISH" if z <= -DRIFT_Z else "NEUTRAL")
    return out


def _max_pain(con, d: str):
    """Static per-session max-pain strike from start-of-day per-strike OI. None if sparse."""
    coi: dict[float, int] = {}
    poi: dict[float, int] = {}
    for k, r, oi in con.execute(
            "SELECT strike,right,open_interest FROM option_open_interest WHERE session_date=?", (d,)):
        (coi if r == "C" else poi)[float(k)] = int(oi or 0)
    strikes = sorted(set(coi) | set(poi))
    if len(strikes) < 6:
        return None
    ks = np.array(strikes, float)
    c = np.array([coi.get(k, 0) for k in strikes], float)
    p = np.array([poi.get(k, 0) for k in strikes], float)
    best_s, best_pay = None, None
    for s in ks:                                   # candidate settlement = each listed strike
        pay = float(np.sum(c * np.maximum(0.0, s - ks)) + np.sum(p * np.maximum(0.0, ks - s)))
        if best_pay is None or pay < best_pay:
            best_pay, best_s = pay, float(s)
    return best_s


def _flow_arrays(con, d: str):
    """(ts[], call_pct[], put_pct[], drift_bias[]) on the flow-minute grid. None if sparse."""
    rows = con.execute(
        "SELECT ts_ms,call_buy_prem,put_buy_prem,net_signed_prem FROM option_flow_minute "
        "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
    if len(rows) < 20:
        return None
    ts = np.array([r[0] for r in rows], np.int64)
    cb = np.array([r[1] or 0.0 for r in rows], float)
    pb = np.array([r[2] or 0.0 for r in rows], float)
    net = np.array([r[3] or 0.0 for r in rows], float)
    tot = cb + pb
    call_pct = np.where(tot > 0, 100.0 * cb / np.maximum(tot, 1e-9), np.nan)
    put_pct = np.where(tot > 0, 100.0 * pb / np.maximum(tot, 1e-9), np.nan)
    drift = _drift_series(net)
    return ts, call_pct, put_pct, drift


def _iv_arrays(con, d: str):
    """(ts[], iv_atm[]) on the dealer-exposure grid. None if absent."""
    rows = con.execute(
        "SELECT ts_ms,iv_atm FROM option_dealer_exposure WHERE session_date=? AND iv_atm IS NOT NULL "
        "ORDER BY ts_ms", (d,)).fetchall()
    if len(rows) < 10:
        return None
    return np.array([r[0] for r in rows], np.int64), np.array([r[1] for r in rows], float)


def build_session(con, ic, d: str):
    """Assemble the aligned 1-min PhaseSample spine + IBKR ruler for one session.

    Returns dict(date, year, rt, rp, samples, max_pain) or None if any required
    source (IBKR ruler / flow) is missing.
    """
    rs = ibkr_series(ic, d)
    if rs is None:
        return None
    rt, rp = rs
    keep = np.array([RTH_LO <= _et_h(int(t)) <= RTH_HI for t in rt])
    rt, rp = rt[keep], rp[keep]
    if len(rt) < 30:
        return None
    fa = _flow_arrays(con, d)
    if fa is None:
        return None
    ft, call_pct, put_pct, drift = fa
    iva = _iv_arrays(con, d)
    vt, iv = iva if iva is not None else (np.array([], np.int64), np.array([], float))
    mp = _max_pain(con, d)

    samples: list[PhaseSample] = []
    for i in range(len(rt)):
        t = int(rt[i])
        jf = _nearest(ft, t)
        cp = pp = None
        db = "NEUTRAL"
        if jf is not None:
            cpv, ppv = call_pct[jf], put_pct[jf]
            cp = float(cpv) if not math.isnan(cpv) else None
            pp = float(ppv) if not math.isnan(ppv) else None
            db = drift[jf]
        jv = _nearest(vt, t) if len(vt) else None
        atm = float(iv[jv]) if jv is not None else None
        samples.append(PhaseSample(ts_ms=t, spx=float(rp[i]), drift_bias=db,
                                   call_pct=cp, put_pct=pp, atm_iv=atm, max_pain=mp))
    return dict(date=d, year=d[:4], rt=rt, rp=rp, samples=samples, max_pain=mp)


def ruler_at(rt: np.ndarray, rp: np.ndarray, target: int, tol: int = 120_000):
    """Nearest IBKR price to `target` within tol, else None."""
    idx = _nearest(rt, target, tol)
    return float(rp[idx]) if idx is not None else None


def dev_sessions(con, start: str, end: str) -> list[str]:
    """Sessions with flow + dealer-exposure(iv) + OI in [start,end] (the IV-covered DEV set)."""
    fm = {r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ?",
        (start, end))}
    dx = {r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_dealer_exposure WHERE session_date BETWEEN ? AND ?",
        (start, end))}
    oi = {r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_open_interest WHERE session_date BETWEEN ? AND ?",
        (start, end))}
    return sorted(fm & dx & oi)
