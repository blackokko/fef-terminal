"""The three exit variants from the shadow/counterfactual system, ported to settle on
REAL ThetaData option fills with the friction model (commission + slippage), PLUS a
fast time-exit matching how the user actually trades ("the move's happening or I'm
out"): cut the whole position if it's not green within cut_if_flat_min, or hard-cap
the hold with max_hold_min.

  be_ladder    - SPX R-multiple targets (1.5/2.5/5R) + BE-ladder + runner-protect.
  bcd          - option-% ladder (+30%/+75%, time-decay-scaled) + trailing peak (peak x 0.85).
  fixed_dollar - $ profit targets + $ loss stop.

Each returns net-of-friction option-dollar P&L + hold_min. Default qty=3 (live size).
"""
from __future__ import annotations

from datetime import datetime

from .data import BAD, ET

SLIP = 0.10
COMMISSION_RT = 1.30
SPLITS = (0.30, 0.40, 0.30)
BE_LADDER = [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)]
T_MULTS = (1.5, 2.5, 5.0)


def _buy(ask, slip):
    return ask + slip


def _sell(bid, slip):
    return max(0.0, bid - slip)


def _et_hour(ts):
    d = datetime.fromtimestamp(ts / 1000, ET)
    return d.hour + d.minute / 60.0


def _qtys(qty, splits=SPLITS):
    a = max(1, round(qty * splits[0]))
    b = max(1, round(qty * splits[1]))
    c = qty - a - b
    if c < 0:
        a = max(1, qty - 1)
        b = qty - a
        c = 0
    return [a, b, c]


def _result(right, strike, entry_ts, entry_px, total_q, proceeds, fills, reason, exit_ts=None, extra=None):
    gross = proceeds - entry_px * total_q * 100
    commission = COMMISSION_RT * total_q
    pnl = gross - commission
    out = {"right": right, "strike": strike, "entry_ts": entry_ts, "entry": round(entry_px, 2),
           "qty": total_q, "pnl": pnl, "pnl_per_contract": pnl / total_q if total_q else 0.0,
           "reason": reason, "fills": fills, "commission": commission,
           "hold_min": round((exit_ts - entry_ts) / 60000) if exit_ts else None}
    if extra:
        out.update(extra)
    return out


def _time_exit(held_min, mid, entry_px, remaining, max_hold_min, cut_if_flat_min, prem_stop=None):
    """Fast loss/management exits matching how the user trades.
      prem_stop       - exit if option premium drops this many $ below cost basis (e.g. 1.00)
      cut_if_flat_min - exit if not green within this many minutes (the move isn't happening)
      max_hold_min    - hard hold cap
    """
    if remaining <= 0:
        return None
    if prem_stop is not None and mid <= entry_px - prem_stop:
        return "prem_stop"
    if cut_if_flat_min is not None and held_min >= cut_if_flat_min and mid <= entry_px:
        return "no_move"
    if max_hold_min is not None and held_min >= max_hold_min:
        return "time"
    return None


def _entry(sess, entry_ts, right, strike, slip):
    q = sess.quote(entry_ts, strike, right)
    if not q or q[2] in BAD:
        return None
    px = _buy(q[1], slip)
    return px if px > 0.05 else None


def simulate_be_ladder(sess, entry_ts, right, strike, *, qty=3, stop_pts=8, initial_stop=False,
                       dollar_cap=-600.0, runner_protect=True, max_hold_min=None,
                       cut_if_flat_min=None, prem_stop=None, slip=SLIP):
    entry_px = _entry(sess, entry_ts, right, strike, slip)
    if entry_px is None:
        return None
    spot0 = sess.spot(entry_ts)
    if spot0 is None:
        return None
    R = float(stop_pts)
    sgn = 1.0 if right == "C" else -1.0
    qtys = _qtys(qty)
    total_q = sum(qtys)
    levels = [spot0 + sgn * T_MULTS[i] * R for i in range(3)]
    fired = [False, False, False]
    dyn_stop = (spot0 - sgn * R) if initial_stop else None
    mfe = 0.0
    t1_at = t1_ts = None
    remaining = total_q
    proceeds = 0.0
    fills = []
    last = None
    exit_ts = cur_ts = entry_ts

    def sell(n, bid):
        nonlocal proceeds, remaining, exit_ts
        px = _sell(bid, slip)
        proceeds += px * n * 100
        remaining -= n
        exit_ts = cur_ts
        fills.append((n, round(px, 2)))

    reason = "eod"
    for ts in sess.minutes:
        if ts <= entry_ts:
            continue
        s = sess.spot(ts)
        qq = sess.quote(ts, strike, right)
        if qq is None or qq[2] in BAD or s is None:
            continue
        bid, ask, _ = qq
        last = bid
        cur_ts = ts
        mid = (bid + ask) / 2
        te = _time_exit((ts - entry_ts) / 60000, mid, entry_px, remaining, max_hold_min, cut_if_flat_min, prem_stop)
        if te:
            sell(remaining, bid); reason = te; break
        mfe = max(mfe, sgn * (s - spot0) / R)
        for trig, lock in BE_LADDER:
            if mfe >= trig:
                ns = spot0 + sgn * lock * R
                if dyn_stop is None or (sgn > 0 and ns > dyn_stop) or (sgn < 0 and ns < dyn_stop):
                    dyn_stop = ns
        if dollar_cap is not None and remaining > 0 and (mid - entry_px) * remaining * 100 <= dollar_cap:
            sell(remaining, bid); reason = "dollar"; break
        if dyn_stop is not None and remaining > 0 and ((sgn > 0 and s <= dyn_stop) or (sgn < 0 and s >= dyn_stop)):
            sell(remaining, bid); reason = "stop"; break
        for i in range(3):
            if not fired[i] and qtys[i] > 0 and ((sgn > 0 and s >= levels[i]) or (sgn < 0 and s <= levels[i])):
                sell(qtys[i], bid); fired[i] = True
                if i == 0:
                    t1_at, t1_ts = s, ts
        if (runner_protect and fired[0] and t1_at is not None and t1_ts is not None and ts > t1_ts
                and (not fired[1] or not fired[2]) and remaining > 0):
            if (sgn > 0 and s <= t1_at) or (sgn < 0 and s >= t1_at):
                sell(remaining, bid); fired[1] = fired[2] = True; reason = "runner"; break
        if remaining <= 0:
            reason = "targets"; break
    if remaining > 0 and last is not None:
        sell(remaining, last)
    return _result(right, strike, entry_ts, entry_px, total_q, proceeds, fills, reason,
                   exit_ts, {"mfe_R": round(mfe, 2)})


def simulate_bcd(sess, entry_ts, right, strike, *, qty=3, peak_drawdown=0.15,
                 max_hold_min=None, cut_if_flat_min=None, prem_stop=None, slip=SLIP):
    entry_px = _entry(sess, entry_ts, right, strike, slip)
    if entry_px is None:
        return None
    qtys = _qtys(qty)
    total_q = sum(qtys)
    fired = [False, False]
    remaining = total_q
    peak = entry_px
    proceeds = 0.0
    fills = []
    last = None
    exit_ts = cur_ts = entry_ts

    def sell(n, bid, tag):
        nonlocal proceeds, remaining, exit_ts
        px = _sell(bid, slip)
        proceeds += px * n * 100
        remaining -= n
        exit_ts = cur_ts
        fills.append((tag, n, round(px, 2)))

    reason = "eod"
    for ts in sess.minutes:
        if ts <= entry_ts:
            continue
        qq = sess.quote(ts, strike, right)
        if qq is None or qq[2] in BAD:
            continue
        bid, ask, _ = qq
        last = bid
        cur_ts = ts
        mid = (bid + ask) / 2
        peak = max(peak, mid)
        te = _time_exit((ts - entry_ts) / 60000, mid, entry_px, remaining, max_hold_min, cut_if_flat_min, prem_stop)
        if te:
            sell(remaining, bid, te); reason = te; break
        h = _et_hour(ts)
        t1_gain = 0.15 if h >= 15 else (0.20 if h >= 14 else 0.30)
        if not fired[0] and qtys[0] > 0 and mid >= entry_px * (1 + t1_gain):
            sell(qtys[0], bid, "T1"); fired[0] = True
        if not fired[1] and qtys[1] > 0 and mid >= entry_px * 1.75:
            sell(qtys[1], bid, "T2"); fired[1] = True
        if remaining > 0 and mid <= peak * (1 - peak_drawdown):
            sell(remaining, bid, "TRAIL"); reason = "trail"; break
        if remaining <= 0:
            reason = "targets"; break
    if remaining > 0 and last is not None:
        sell(remaining, last, "EOD")
    return _result(right, strike, entry_ts, entry_px, total_q, proceeds, fills, reason,
                   exit_ts, {"peak": round(peak, 2)})


def simulate_fixed_dollar(sess, entry_ts, right, strike, *, qty=3,
                          d_targets=(200.0, 500.0, 1000.0), d_stop=-300.0,
                          max_hold_min=None, cut_if_flat_min=None, prem_stop=None, slip=SLIP):
    entry_px = _entry(sess, entry_ts, right, strike, slip)
    if entry_px is None:
        return None
    qtys = _qtys(qty)
    total_q = sum(qtys)
    fired = [False, False, False]
    remaining = total_q
    proceeds = 0.0
    fills = []
    last = None
    exit_ts = cur_ts = entry_ts

    def sell(n, bid, tag):
        nonlocal proceeds, remaining, exit_ts
        px = _sell(bid, slip)
        proceeds += px * n * 100
        remaining -= n
        exit_ts = cur_ts
        fills.append((tag, n, round(px, 2)))

    reason = "eod"
    for ts in sess.minutes:
        if ts <= entry_ts:
            continue
        qq = sess.quote(ts, strike, right)
        if qq is None or qq[2] in BAD:
            continue
        bid, ask, _ = qq
        last = bid
        cur_ts = ts
        mid = (bid + ask) / 2
        te = _time_exit((ts - entry_ts) / 60000, mid, entry_px, remaining, max_hold_min, cut_if_flat_min, prem_stop)
        if te:
            sell(remaining, bid, te); reason = te; break
        open_pnl = (mid - entry_px) * remaining * 100
        if remaining > 0 and open_pnl <= d_stop:
            sell(remaining, bid, "STOP"); reason = "stop"; break
        realized = proceeds - entry_px * (total_q - remaining) * 100
        total_open = realized + open_pnl
        for i in range(3):
            if not fired[i] and qtys[i] > 0 and total_open >= d_targets[i]:
                sell(qtys[i], bid, f"T{i+1}"); fired[i] = True
        if remaining <= 0:
            reason = "targets"; break
    if remaining > 0 and last is not None:
        sell(remaining, last, "EOD")
    return _result(right, strike, entry_ts, entry_px, total_q, proceeds, fills, reason, exit_ts)


def simulate_prempct(sess, entry_ts, right, strike, *, qty=3,
                     tp_pct=(0.08, 0.15, 0.25), stop_pct=0.30, peak_drawdown=0.20,
                     max_hold_min=None, cut_if_flat_min=None, prem_stop=None, slip=SLIP):
    """ADDED 2026-06-30 for flow_monetize_test (research-only): a premium-% scale-out
    ladder SIZED TO THE THIN ~+1pt flow-surge edge. Three partial targets at +tp_pct[i]
    of entry premium, a hard -stop_pct premium stop, and a trailing peak-drawdown runner
    stop once T1 has fired. Same fill/friction mechanics (_buy/_sell/_time_exit/_result)
    as simulate_bcd, but with edge-sized single-digit-% targets instead of bcd's fixed
    +30/+75%. This is a NEW function; no existing function's defaults/behavior change."""
    entry_px = _entry(sess, entry_ts, right, strike, slip)
    if entry_px is None:
        return None
    qtys = _qtys(qty)
    total_q = sum(qtys)
    fired = [False, False, False]
    remaining = total_q
    peak = entry_px
    proceeds = 0.0
    fills = []
    last = None
    exit_ts = cur_ts = entry_ts

    def sell(n, bid, tag):
        nonlocal proceeds, remaining, exit_ts
        px = _sell(bid, slip)
        proceeds += px * n * 100
        remaining -= n
        exit_ts = cur_ts
        fills.append((tag, n, round(px, 2)))

    reason = "eod"
    for ts in sess.minutes:
        if ts <= entry_ts:
            continue
        qq = sess.quote(ts, strike, right)
        if qq is None or qq[2] in BAD:
            continue
        bid, ask, _ = qq
        last = bid
        cur_ts = ts
        mid = (bid + ask) / 2
        peak = max(peak, mid)
        te = _time_exit((ts - entry_ts) / 60000, mid, entry_px, remaining, max_hold_min, cut_if_flat_min, prem_stop)
        if te:
            sell(remaining, bid, te); reason = te; break
        if stop_pct is not None and remaining > 0 and mid <= entry_px * (1 - stop_pct):
            sell(remaining, bid, "STOP"); reason = "stop"; break
        for i in range(3):
            if not fired[i] and qtys[i] > 0 and mid >= entry_px * (1 + tp_pct[i]):
                sell(qtys[i], bid, f"T{i+1}"); fired[i] = True
        if fired[0] and remaining > 0 and peak_drawdown is not None and mid <= peak * (1 - peak_drawdown):
            sell(remaining, bid, "TRAIL"); reason = "trail"; break
        if remaining <= 0:
            reason = "targets"; break
    if remaining > 0 and last is not None:
        sell(remaining, last, "EOD")
    return _result(right, strike, entry_ts, entry_px, total_q, proceeds, fills, reason, exit_ts,
                   {"peak": round(peak, 2)})


VARIANTS = {"be_ladder": simulate_be_ladder, "bcd": simulate_bcd,
            "fixed_dollar": simulate_fixed_dollar, "prempct": simulate_prempct}


def simulate(variant, sess, entry_ts, right, strike, **kw):
    return VARIANTS[variant](sess, entry_ts, right, strike, **kw)
