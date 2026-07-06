"""Momentum-confirmation entries: get in only when a directional thrust is ALREADY
underway (the user's actual style -- "the move needs to be happening when I get in"),
not predicting one. CALL on an up-thrust, PUT on a down-thrust. First trigger/session.
"""
from __future__ import annotations


def roc_thrust(sess, thrust_pts, lookback_min, start_min=5, end_min=360):
    """Enter the first minute spot has moved >= thrust_pts over the last lookback_min
    (momentum confirmed), in the thrust direction."""
    t_open = sess.minutes[0]
    for ts in sess.minutes:
        dm = (ts - t_open) / 60000
        if dm < start_min:
            continue
        if dm > end_min:
            break
        s = sess.spot(ts)
        if s is None:
            continue
        back = sess.minute_at_or_before(ts - lookback_min * 60000)
        sb = sess.spot(back) if back else None
        if sb is None:
            continue
        if s - sb >= thrust_pts:
            return (ts, "CALL")
        if s - sb <= -thrust_pts:
            return (ts, "PUT")
    return None


def breakout(sess, or_min=15, end_min=360):
    """Opening-range breakout: enter on the first break of the first or_min-minute
    high/low (the move confirming itself out of the range)."""
    t_open = sess.minutes[0]
    cut = t_open + or_min * 60000
    pre = [x for x in (sess.spot(ts) for ts in sess.minutes if ts <= cut) if x is not None]
    if not pre:
        return None
    hi, lo = max(pre), min(pre)
    for ts in sess.minutes:
        dm = (ts - t_open) / 60000
        if dm <= or_min:
            continue
        if dm > end_min:
            break
        s = sess.spot(ts)
        if s is None:
            continue
        if s > hi:
            return (ts, "CALL")
        if s < lo:
            return (ts, "PUT")
    return None


ENTRIES = {
    "roc8/10": lambda s: roc_thrust(s, 8, 10),
    "roc5/5": lambda s: roc_thrust(s, 5, 5),
    "roc12/15": lambda s: roc_thrust(s, 12, 15),
    "orb15": lambda s: breakout(s, 15),
}
