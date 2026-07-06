"""Naked-long 0DTE lifecycle in real option-dollars.

Direction/exits are evaluated on the parity-reconstructed spot; fills come from real
option bid/ask: entry = ask + slip, exit = bid - slip. This is the single source of
execution truth every strategy variant shares.
"""
from __future__ import annotations

from .data import BAD

SLIP = 0.05  # $/contract adverse slippage beyond the touch (per leg)


def _buy(bid, ask, slip):
    return ask + slip


def _sell(bid, ask, slip):
    return max(0.0, bid - slip)


def simulate_naked_long(sess, entry_ts, right, strike, *, hold_min=30,
                        und_target=None, und_stop=None, opt_stop_pct=None, slip=SLIP):
    """Buy one naked call/put at `strike`, manage to exit, return the trade in $.

    Exits (checked each minute, first to trigger wins):
      und_target  - favorable underlying move in points (call: spot up, put: down)
      und_stop    - adverse underlying move in points
      opt_stop_pct- option mid falls to (1-pct) of entry (premium stop)
      hold_min    - hard time stop (always present)
    """
    q = sess.quote(entry_ts, strike, right)
    if not q or q[2] in BAD:
        return None
    entry = _buy(q[0], q[1], slip)
    if entry <= 0.05:
        return None
    spot0 = sess.spot(entry_ts)
    end_ts = entry_ts + hold_min * 60000
    sgn = 1.0 if right == "C" else -1.0
    exit_px = exit_t = reason = None
    for ts in sess.minutes:
        if ts <= entry_ts:
            continue
        if ts > end_ts:
            break
        qq = sess.quote(ts, strike, right)
        if not qq or qq[2] in BAD:
            continue
        bid, ask, _ = qq
        s = sess.spot(ts)
        fav = sgn * (s - spot0) if (s is not None and spot0 is not None) else None
        if und_target is not None and fav is not None and fav >= und_target:
            exit_px, exit_t, reason = _sell(bid, ask, slip), ts, "target"
            break
        if und_stop is not None and fav is not None and fav <= -und_stop:
            exit_px, exit_t, reason = _sell(bid, ask, slip), ts, "stop"
            break
        if opt_stop_pct is not None and (bid + ask) / 2 <= entry * (1 - opt_stop_pct):
            exit_px, exit_t, reason = _sell(bid, ask, slip), ts, "opt_stop"
            break
    if exit_px is None:  # time stop -> exit at last quote on/before end
        m = sess.minute_at_or_before(end_ts) or sess.minutes[-1]
        qq = sess.quote(m, strike, right)
        if not qq:
            return None
        exit_px, exit_t, reason = _sell(qq[0], qq[1], slip), m, "time"
    return {"right": right, "strike": strike, "entry_ts": entry_ts, "exit_ts": exit_t,
            "entry": round(entry, 2), "exit": round(exit_px, 2),
            "pnl": (exit_px - entry) * 100, "reason": reason}


# Engine constants (counterfactual.py / spx_target_watcher.py) — the REAL exit model.
T_MULTS = (1.5, 2.5, 5.0)                       # T1/T2/T3 at these R-multiples of stop
SPLITS = (0.30, 0.40, 0.30)                     # partial scale-out (auto_trader)
BE_LADDER = [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)]  # MFE_R -> lock stop at this R


def simulate_managed_long(sess, entry_ts, right, strike, *, stop_pts, qty=15,
                          splits=SPLITS, t_mults=T_MULTS, runner_protect=True,
                          dollar_cap=None, trail_give_back_R=None, slip=SLIP):
    """Faithful replica of the production spx_points exit system, settled in real
    option-dollars from the historical chain.

    Risk R = stop_pts (SPX). Three partial tiers fire as SPX crosses entry +/- R*mult
    (30/40/30 qty), each filled at the real option bid-slip. A break-even ladder
    ratchets the stop up on favorable MFE; runner-protect dumps remaining size if SPX
    retraces to where T1 fired; an optional dollar cap backstops the whole position.
    Anything still open at the close settles at the last real bid.
    """
    q = sess.quote(entry_ts, strike, right)
    if not q or q[2] in BAD:
        return None
    entry_px = _buy(q[0], q[1], slip)
    if entry_px <= 0.05:
        return None
    spot0 = sess.spot(entry_ts)
    if spot0 is None:
        return None
    R = float(stop_pts)
    if R <= 0:
        return None
    sgn = 1.0 if right == "C" else -1.0
    t1q = max(1, round(qty * splits[0]))
    t2q = max(1, round(qty * splits[1]))
    t3q = max(1, qty - t1q - t2q)
    total_q = t1q + t2q + t3q
    qtys = [t1q, t2q, t3q]
    levels = [spot0 + sgn * t_mults[i] * R for i in range(3)]
    fired = [False, False, False]
    dyn_stop = spot0 - sgn * R
    mfe_r_max = 0.0
    t1_at_spx = None
    t1_fired_ts = None
    remaining = total_q
    proceeds = 0.0
    entry_cost = entry_px * total_q * 100
    fills = []
    last_q = None
    reason = "eod"

    def _sell_n(n, bid, ask, tag, ts):
        nonlocal proceeds, remaining
        px = _sell(bid, ask, slip)
        proceeds += px * n * 100
        remaining -= n
        fills.append((tag, n, round(px, 2)))

    for ts in sess.minutes:
        if ts <= entry_ts:
            continue
        s = sess.spot(ts)
        qq = sess.quote(ts, strike, right)
        if qq is None or qq[2] in BAD or s is None:
            continue
        bid, ask, _ = qq
        last_q = (bid, ask)
        # BE ladder: ratchet the dynamic stop on favorable MFE
        mfe_r_max = max(mfe_r_max, sgn * (s - spot0) / R)
        for trig, lock in BE_LADDER:
            if mfe_r_max >= trig:
                new_stop = spot0 + sgn * lock * R
                if (sgn > 0 and new_stop > dyn_stop) or (sgn < 0 and new_stop < dyn_stop):
                    dyn_stop = new_stop
        # continuous trailing profit-lock: once MFE >= give_back, trail the stop at
        # (mfe - give_back)R, ratcheting only favorably -> captures gains between the
        # coarse BE-ladder rungs (the live exit-leak fix being validated).
        if trail_give_back_R is not None and mfe_r_max >= trail_give_back_R:
            tstop = spot0 + sgn * (mfe_r_max - trail_give_back_R) * R
            if (sgn > 0 and tstop > dyn_stop) or (sgn < 0 and tstop < dyn_stop):
                dyn_stop = tstop
        # dollar cap on open size (mid-based, mirrors the watcher)
        if dollar_cap is not None and remaining > 0:
            if ((bid + ask) / 2 - entry_px) * remaining * 100 <= dollar_cap:
                _sell_n(remaining, bid, ask, "DOLLAR", ts)
                reason = "dollar"
                break
        # stop (dynamic) -> dump all remaining
        if remaining > 0 and ((sgn > 0 and s <= dyn_stop) or (sgn < 0 and s >= dyn_stop)):
            _sell_n(remaining, bid, ask, "STOP", ts)
            reason = "stop"
            break
        # profit tiers
        for i in range(3):
            if not fired[i] and ((sgn > 0 and s >= levels[i]) or (sgn < 0 and s <= levels[i])):
                _sell_n(qtys[i], bid, ask, f"T{i+1}", ts)
                fired[i] = True
                if i == 0:
                    t1_at_spx = s
                    t1_fired_ts = ts
        # runner protect: on a LATER minute, retrace to T1-fire level dumps runners
        if (runner_protect and fired[0] and t1_at_spx is not None
                and t1_fired_ts is not None and ts > t1_fired_ts
                and (not fired[1] or not fired[2]) and remaining > 0):
            if (sgn > 0 and s <= t1_at_spx) or (sgn < 0 and s >= t1_at_spx):
                _sell_n(remaining, bid, ask, "RUNNER", ts)
                fired[1] = fired[2] = True
                reason = "runner"
                break
        if remaining <= 0:
            reason = "targets"
            break
    if remaining > 0 and last_q is not None:
        _sell_n(remaining, last_q[0], last_q[1], "EOD", None)

    pnl = proceeds - entry_cost
    return {"right": right, "strike": strike, "entry_ts": entry_ts, "entry": round(entry_px, 2),
            "spot0": round(spot0, 1), "R": R, "qty": total_q, "pnl": pnl,
            "pnl_per_contract": pnl / total_q, "reason": reason,
            "mfe_R": round(mfe_r_max, 2), "fills": fills}
