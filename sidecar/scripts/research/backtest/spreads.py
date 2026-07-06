"""Directional DEBIT-SPREAD expression (H4): buy ATM, sell OTM same-expiry 0DTE in the
signal direction. The short leg's decay offsets the long leg's, roughly halving theta
drag while keeping the directional bet (gain capped at the strike width). Real fills on
both legs (long: ask in / bid out; short: bid in / ask out), commission on both legs.
"""
from __future__ import annotations

from .data import BAD
from .exits import COMMISSION_RT, SLIP


def _q(sess, ts, strike, right):
    v = sess.quote(ts, strike, right)
    if not v or v[2] in BAD:
        return None
    return v  # (bid, ask, quality)


def _close_value(lq, sq, slip):
    """Net received to close: sell long at bid-slip, buy back short at ask+slip."""
    return max(0.0, lq[0] - slip) - (sq[1] + slip)


def simulate_debit_spread(sess, entry_ts, direction, atm, width, *, qty=3,
                          peak_drawdown=0.15, max_hold_min=None, cut_if_flat_min=None, slip=SLIP):
    right = "C" if direction == "CALL" else "P"
    long_strike = atm
    target_short = atm + width if right == "C" else atm - width
    short_strike = min(sess.strikes, key=lambda k: abs(k - target_short))
    if short_strike == long_strike:
        return None
    actual_width = abs(short_strike - long_strike)
    lq = _q(sess, entry_ts, long_strike, right)
    sq = _q(sess, entry_ts, short_strike, right)
    if not lq or not sq:
        return None
    long_px = lq[1] + slip            # buy long at ask + slip
    short_px = max(0.0, sq[0] - slip)  # sell short at bid - slip
    net_debit = long_px - short_px
    if net_debit <= 0.05:
        return None

    peak = net_debit
    exit_val = None
    reason = "eod"
    exit_ts = entry_ts
    last_lq = last_sq = None
    for ts in sess.minutes:
        if ts <= entry_ts:
            continue
        lq2 = _q(sess, ts, long_strike, right)
        sq2 = _q(sess, ts, short_strike, right)
        if not lq2 or not sq2:
            continue
        last_lq, last_sq, last_ts = lq2, sq2, ts
        lmid = (lq2[0] + lq2[1]) / 2
        smid = (sq2[0] + sq2[1]) / 2
        mark = lmid - smid
        peak = max(peak, mark)
        held = (ts - entry_ts) / 60000
        if cut_if_flat_min is not None and held >= cut_if_flat_min and mark <= net_debit:
            exit_val = _close_value(lq2, sq2, slip); reason = "no_move"; exit_ts = ts; break
        if max_hold_min is not None and held >= max_hold_min:
            exit_val = _close_value(lq2, sq2, slip); reason = "time"; exit_ts = ts; break
        if mark <= peak * (1 - peak_drawdown):
            exit_val = _close_value(lq2, sq2, slip); reason = "trail"; exit_ts = ts; break
    if exit_val is None:
        if last_lq is None:
            return None
        exit_val = _close_value(last_lq, last_sq, slip)
        exit_ts = last_ts
    commission = COMMISSION_RT * 2 * qty   # two legs, round trip
    pnl = (exit_val - net_debit) * qty * 100 - commission
    return {"direction": direction, "long": long_strike, "short": short_strike,
            "width": actual_width, "debit": round(net_debit, 2), "exit_val": round(exit_val, 2),
            "pnl": pnl, "pnl_per_contract": pnl / qty, "reason": reason,
            "hold_min": round((exit_ts - entry_ts) / 60000), "net_debit_cost": net_debit}
