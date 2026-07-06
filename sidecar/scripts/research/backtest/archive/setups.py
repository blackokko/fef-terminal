"""Concrete OI/price-based setups, crystallized from the full-day precursor study.

Each setup scans its time-of-day window and fires the first qualifying entry (one
trade/session max), returning (entry_ts, direction). Thresholds are simple/round
(median-split style, NOT optimized) so the OOS test stays honest. Flow-based setups
(the open) come once flow finishes.

  am_structure : 10:00-11:30 - position-in-range + max-pain pull   (or_pos / max_pain_dist)
  mid_trend    : 11:30-13:30 - day trend + max-pain                (ret_open / max_pain_dist)
  pm_vol       : 13:30-15:00 - skew tilt                           (skew_ratio)
"""
from __future__ import annotations

from datetime import datetime

from .data import ET


def _et(ts):
    d = datetime.fromtimestamp(ts / 1000, ET)
    return d.hour + d.minute / 60.0


def _bucket_minutes(sess, lo, hi, step=5):
    out, last = [], None
    for ts in sess.minutes:
        if lo <= _et(ts) < hi and (last is None or ts - last >= step * 60000):
            out.append(ts)
            last = ts
    return out


def price_oi_features(sess, t, oi):
    s0 = sess.spot(t)
    if s0 is None:
        return None
    f = {}
    s_open = sess.spot(sess.minutes[0])
    f["ret_open"] = (s0 - s_open) if s_open else None
    pre = [x for x in (sess.spot(ts) for ts in sess.minutes if ts <= t) if x is not None]
    if pre:
        hi, lo = max(pre), min(pre)
        f["or_pos"] = (s0 - lo) / (hi - lo) if hi > lo else 0.5
    atm = sess.atm(t)
    pm = sess.mid(t, atm - 25, "P") if atm else None
    cm = sess.mid(t, atm + 25, "C") if atm else None
    f["skew_ratio"] = (pm / cm) if (pm and cm and cm > 0) else None
    call_oi = {k: o for (k, r), o in oi.items() if r == "C"}
    put_oi = {k: o for (k, r), o in oi.items() if r == "P"}
    strikes = sorted(set(call_oi) | set(put_oi))
    if strikes:
        mp = min(strikes, key=lambda P: sum(
            call_oi.get(k, 0) * max(0.0, P - k) + put_oi.get(k, 0) * max(0.0, k - P) for k in strikes))
        f["max_pain_dist"] = mp - s0
    return f


def setup_am(sess, oi):
    for ts in _bucket_minutes(sess, 10.0, 11.5):
        f = price_oi_features(sess, ts, oi)
        if not f or f.get("or_pos") is None or f.get("max_pain_dist") is None:
            continue
        if f["or_pos"] >= 0.60 and f["max_pain_dist"] >= 0:
            return (ts, "CALL")
        if f["or_pos"] <= 0.40 and f["max_pain_dist"] <= 0:
            return (ts, "PUT")
    return None


def setup_mid(sess, oi):
    for ts in _bucket_minutes(sess, 11.5, 13.5):
        f = price_oi_features(sess, ts, oi)
        if not f or f.get("ret_open") is None or f.get("max_pain_dist") is None:
            continue
        if f["ret_open"] >= 0 and f["max_pain_dist"] >= 5:
            return (ts, "CALL")
        if f["ret_open"] <= 0 and f["max_pain_dist"] <= -5:
            return (ts, "PUT")
    return None


def setup_pm(sess, oi):
    for ts in _bucket_minutes(sess, 13.5, 15.0):
        f = price_oi_features(sess, ts, oi)
        if not f or f.get("skew_ratio") is None:
            continue
        if f["skew_ratio"] <= 1.35:
            return (ts, "CALL")
        if f["skew_ratio"] >= 1.55:
            return (ts, "PUT")
    return None


SETUPS = {"am_structure": setup_am, "mid_trend": setup_mid, "pm_vol": setup_pm}
