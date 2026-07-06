"""Directional entry-signal generators for naked longs.

Each family returns a list of (entry_ts, right) signals for one session, computed on
the parity-spot minute series. v1 takes the FIRST qualifying signal per session
(one trade/session) to keep overlap accounting trivial; re-entry is a later knob.
"""
from __future__ import annotations


def _spot_series(sess):
    out = []
    for ts in sess.minutes:
        s = sess.spot(ts)
        if s is not None:
            out.append((ts, s))
    return out


def trend_ema(sess, ema_len=20, warmup_min=15, **_):
    """Long the direction of an EMA trend: spot above a rising EMA -> call, below a
    falling EMA -> put. First trigger after warmup."""
    ser = _spot_series(sess)
    if len(ser) < ema_len + 2:
        return []
    k = 2 / (ema_len + 1)
    ema = ser[0][1]
    start_ts = ser[0][0] + warmup_min * 60000
    for i, (ts, s) in enumerate(ser):
        prev_ema = ema
        ema = s * k + ema * (1 - k) if i else s
        if ts < start_ts:
            continue
        if s > ema and ema > prev_ema:
            return [(ts, "C")]
        if s < ema and ema < prev_ema:
            return [(ts, "P")]
    return []


def orb(sess, or_min=30, **_):
    """Opening-range breakout: break above the first or_min-minute high -> call,
    below the low -> put."""
    ser = _spot_series(sess)
    if not ser:
        return []
    t0 = ser[0][0]
    cut = t0 + or_min * 60000
    opening = [s for ts, s in ser if ts <= cut]
    if not opening:
        return []
    hi, lo = max(opening), min(opening)
    for ts, s in ser:
        if ts <= cut:
            continue
        if s > hi:
            return [(ts, "C")]
        if s < lo:
            return [(ts, "P")]
    return []


def momentum(sess, lookback_min=15, thresh_pts=8.0, start_after_min=15, **_):
    """Rate-of-change momentum: spot up >= thresh over lookback -> call, down -> put."""
    ser = _spot_series(sess)
    if not ser:
        return []
    t0 = ser[0][0]
    start = t0 + start_after_min * 60000
    for ts, s in ser:
        if ts < start:
            continue
        back_ts = ts - lookback_min * 60000
        prev = None
        for tts, ss in ser:
            if tts <= back_ts:
                prev = ss
            else:
                break
        if prev is None:
            continue
        roc = s - prev
        if roc >= thresh_pts:
            return [(ts, "C")]
        if roc <= -thresh_pts:
            return [(ts, "P")]
    return []


FAMILIES = {"trend_ema": trend_ema, "orb": orb, "momentum": momentum}
