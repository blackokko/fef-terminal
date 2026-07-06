"""Premium-selling structure for H7: a near-ATM 0DTE iron condor, profits when price
stays in range (the 84%-flat majority). Sell short strikes at ATM +/- short_off, buy
wings at +/- (short_off+wing). Realistic near-mid fills on entry; cash-settled at the
close (SPXW PM-settle), so no exit trade. Defined risk = wing - credit.
"""
from __future__ import annotations

from .data import BAD

COMM_LEG = 0.65  # per contract per leg


def _mid(sess, ts, strike, right):
    q = sess.quote(ts, strike, right)
    if not q or q[2] in BAD:
        return None
    return (q[0] + q[1]) / 2


def simulate_iron_condor(sess, entry_ts, atm, *, short_off=10, wing=10, qty=3, slip=0.05):
    def near(x):
        return min(sess.strikes, key=lambda k: abs(k - x))
    sc, lc = near(atm + short_off), near(atm + short_off + wing)
    sp, lp = near(atm - short_off), near(atm - short_off - wing)
    if len({sc, lc, sp, lp}) < 4:
        return None
    scm = _mid(sess, entry_ts, sc, "C"); lcm = _mid(sess, entry_ts, lc, "C")
    spm = _mid(sess, entry_ts, sp, "P"); lpm = _mid(sess, entry_ts, lp, "P")
    if None in (scm, lcm, spm, lpm):
        return None
    # realistic fills: sell shorts at mid-slip, buy wings at mid+slip
    credit = (scm - slip) + (spm - slip) - (lcm + slip) - (lpm + slip)
    if credit <= 0.05:
        return None
    wing_w = min(abs(lc - sc), abs(sp - lp))
    s_close = sess.spot(sess.minutes[-1])
    if s_close is None:
        return None
    call_payout = max(0.0, min(s_close - sc, wing_w))
    put_payout = max(0.0, min(sp - s_close, wing_w))
    payout = call_payout + put_payout
    pnl = (credit - payout) * qty * 100 - COMM_LEG * 4 * qty   # 4 legs entry; cash-settled
    return {"credit": round(credit, 2), "payout": round(payout, 2), "max_loss": round(wing_w - credit, 2),
            "pnl": pnl, "pnl_per_contract": pnl / qty, "spot_close": round(s_close, 1),
            "in_range": abs(s_close - atm) <= short_off}


def simulate_iron_condor_managed(sess, entry_ts, atm, *, short_off=15, wing=10, qty=3,
                                 profit_target=0.5, stop_mult=2.0, slip=0.05):
    """Iron condor with intraday management: close at profit_target of credit captured, or
    cut at stop_mult x credit loss (before max loss), else PM-settle. Real near-mid fills."""
    def near(x):
        return min(sess.strikes, key=lambda k: abs(k - x))
    sc, lc = near(atm + short_off), near(atm + short_off + wing)
    sp, lp = near(atm - short_off), near(atm - short_off - wing)
    if len({sc, lc, sp, lp}) < 4:
        return None
    legs = [(sc, "C"), (lc, "C"), (sp, "P"), (lp, "P")]

    def mids(ts):
        out = []
        for k, r in legs:
            v = _mid(sess, ts, k, r)
            if v is None:
                return None
            out.append(v)
        return out

    em = mids(entry_ts)
    if em is None:
        return None
    scm, lcm, spm, lpm = em
    credit = (scm - slip) + (spm - slip) - (lcm + slip) - (lpm + slip)
    if credit <= 0.05:
        return None
    wing_w = min(abs(lc - sc), abs(sp - lp))
    target_val = credit * (1 - profit_target)
    stop_val = credit * (1 + stop_mult)
    comm = COMM_LEG * 4 * qty
    for ts in sess.minutes:
        if ts <= entry_ts:
            continue
        m = mids(ts)
        if m is None:
            continue
        scm2, lcm2, spm2, lpm2 = m
        cval = (scm2 - lcm2) + (spm2 - lpm2)            # mid cost-to-close
        close_cost = (scm2 + slip) - (lcm2 - slip) + (spm2 + slip) - (lpm2 - slip)
        if cval <= target_val:
            pnl = (credit - close_cost) * qty * 100 - 2 * comm
            return {"credit": round(credit, 2), "pnl": pnl, "pnl_per_contract": pnl / qty, "reason": "target"}
        if cval >= stop_val:
            pnl = (credit - close_cost) * qty * 100 - 2 * comm
            return {"credit": round(credit, 2), "pnl": pnl, "pnl_per_contract": pnl / qty, "reason": "stop"}
    s_close = sess.spot(sess.minutes[-1])
    if s_close is None:
        return None
    payout = max(0.0, min(s_close - sc, wing_w)) + max(0.0, min(sp - s_close, wing_w))
    pnl = (credit - payout) * qty * 100 - comm
    return {"credit": round(credit, 2), "pnl": pnl, "pnl_per_contract": pnl / qty, "reason": "settle"}
