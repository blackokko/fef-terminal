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

from fef_sidecar.engine.signals import dale, vwap
from fef_sidecar.engine.signals.dale import Bar

# ---- FROZEN PARAMETERS ----
SIGNAL_NAME = "vwap_loss_short"
TF = 5
HOLD_BARS = 1
REJECT_HOLD_BARS = 0
ATR_PERIOD = 14
ATR_STOP_MULT = 1.0
ATR_TARGET_MULT = 2.0
FRICTION_PTS = 1.5
IN_SAMPLE_CUTOFF = "2026-05-29"   # sessions <= this are in-sample (selection); > is forward
ES_BAR_SIZE = "1 min RTH"

DEFINITION_VERSION = "v1-2026-05-29"


def definition_hash() -> str:
    payload = "|".join(str(x) for x in (
        SIGNAL_NAME, TF, HOLD_BARS, REJECT_HOLD_BARS, ATR_PERIOD,
        ATR_STOP_MULT, ATR_TARGET_MULT, FRICTION_PTS, ES_BAR_SIZE, DEFINITION_VERSION,
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
    import datetime as dt
    return dt.datetime.fromtimestamp(ts_ms/1000, dt.timezone(dt.timedelta(hours=-4))).strftime("%Y-%m-%d")


def _walk_prob(entry, target, stop, is_long, bars1m, close_time, horizon_session):
    """Order-preserving forward walk -> P(target before stop). t1-prob proxy on
    ambiguous bars; soft progress on timeout. (Frozen copy.)"""
    ts = [b.ts_ms for b in bars1m]
    si = bisect.bisect_left(ts, close_time)
    last = None
    for k in range(si, len(bars1m)):
        b = bars1m[k]
        if _et_date(b.ts_ms) > horizon_session:
            break
        last = b
        hit_t = (b.high >= target) if is_long else (b.low <= target)
        hit_s = (b.low <= stop) if is_long else (b.high >= stop)
        if hit_t and hit_s:
            ds = abs(b.open - stop); dt_ = abs(b.open - target)
            return ds / (ds + dt_) if (ds + dt_) > 0 else 0.5
        if hit_t:
            return 1.0
        if hit_s:
            return 0.0
    if last is None:
        return None
    prog = ((last.close - entry) if is_long else (entry - last.close)) / abs(target - entry)
    return max(0.0, min(1.0, 0.5 + 0.5 * prog))


def score_session(es_rth_1m: list[Bar]) -> list[dict]:
    """Detect + score all vwap_loss_short signals present in the given ES RTH
    1-min bars (may span multiple sessions). Returns one dict per signal."""
    es5 = dale.resample(es_rth_1m, TF)
    atr = _atr(es5)
    imap = {b.ts_ms: i for i, b in enumerate(es5)}
    sigs = [s for s in vwap.detect(es5, timeframe_min=TF, hold_bars=HOLD_BARS,
                                   reject_hold_bars=REJECT_HOLD_BARS)
            if s.kind == SIGNAL_NAME]
    out: list[dict] = []
    for s in sigs:
        i = imap.get(s.ts_ms)
        a = atr[i] if i is not None else None
        if a is None or a <= 0:
            continue
        entry = s.price              # short (PUTS)
        stop = entry + ATR_STOP_MULT * a
        target = entry - ATR_TARGET_MULT * a
        sd = _et_date(s.ts_ms)
        p = _walk_prob(entry, target, stop, False, es_rth_1m, s.ts_ms + TF * 60_000, sd)
        if p is None:
            continue
        risk = abs(entry - stop)
        rr = abs(target - entry) / risk
        r = p * rr + (1 - p) * (-1.0) - FRICTION_PTS / risk
        out.append({
            "signal_name": SIGNAL_NAME, "definition_version": DEFINITION_VERSION,
            "session_date": sd, "timestamp_ms": s.ts_ms,
            "in_sample": sd <= IN_SAMPLE_CUTOFF,
            "direction": "PUTS", "entry": entry, "stop": stop, "target": target,
            "rr": rr, "r_outcome": r, "win": 1.0 if r > 0 else 0.0,
            "friction_pts": FRICTION_PTS,
        })
    return out
