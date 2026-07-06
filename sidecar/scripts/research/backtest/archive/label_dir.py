"""Shared directional-success label so every pattern-hunt agent scores identically.

A candidate says "go LONG CALLS (or PUTS) on this day". Success = the managed long's
first tier (T1 = entry +/- 1.5R) is reached before the stop (entry -/+ 1R) after the
decision minute. This is the money-relevant 'did it go my way first' label.
"""
from __future__ import annotations


def decision_ts(sess, decision_min=30):
    if sess.empty():
        return None
    return sess.minute_at_or_before(sess.minutes[0] + decision_min * 60000)


def t1_before_stop(sess, dec_ts, direction, R=8.0, t1_mult=1.5):
    """1 if T1 reached before stop after dec_ts, 0 otherwise (incl. neither by close),
    None if the spot is undeterminable at the decision minute. direction in CALL/PUT."""
    s0 = sess.spot(dec_ts)
    if s0 is None:
        return None
    sgn = 1.0 if direction == "CALL" else -1.0
    t1 = s0 + sgn * t1_mult * R
    stop = s0 - sgn * R
    for ts in sess.minutes:
        if ts <= dec_ts:
            continue
        s = sess.spot(ts)
        if s is None:
            continue
        if sgn > 0:
            if s <= stop:
                return 0
            if s >= t1:
                return 1
        else:
            if s >= stop:
                return 0
            if s <= t1:
                return 1
    return 0


def close_dir_correct(sess, dec_ts, direction):
    """Secondary label: did the close finish on the signaled side of the decision spot."""
    s0 = sess.spot(dec_ts)
    sc = sess.spot(sess.minutes[-1]) if not sess.empty() else None
    if s0 is None or sc is None:
        return None
    return 1 if ((direction == "CALL" and sc > s0) or (direction == "PUT" and sc < s0)) else 0
