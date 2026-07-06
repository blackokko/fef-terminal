"""Dealer-positioning features from start-of-day Open Interest + intraday greeks.

GEX (gamma exposure), gamma-flip level, max pain, and call/put walls — the standard
0DTE positioning/regime inputs that the first hunt lacked. OI is start-of-day
(option_open_interest); per-strike gamma is recomputed at the decision minute from the
live quote IV, so GEX reflects the current spot and time-to-expiry.

Sign convention: dealers long calls / short puts -> call gamma adds +, put gamma adds -.
GEX > 0 = positive-gamma (pinning / mean-reverting / low realized vol);
GEX < 0 = negative-gamma (trending / momentum / high realized vol).
"""
from __future__ import annotations

from datetime import datetime

from .data import ET
from .greeks import greeks, implied_vol

_YEAR_MIN = 365.0 * 24 * 60


def load_oi(con, session_date):
    out = {}
    for k, r, oi in con.execute(
        "SELECT strike,right,open_interest FROM option_open_interest WHERE session_date=?",
        (session_date,)):
        out[(float(k), r)] = int(oi)
    return out


def _ttm_years(ts):
    now = datetime.fromtimestamp(ts / 1000, ET)
    mins = (now.replace(hour=16, minute=0, second=0, microsecond=0) - now).total_seconds() / 60
    return max(mins, 0.0) / _YEAR_MIN


def gex_features(con, sess, dec_ts):
    oi = load_oi(con, sess.date)
    if not oi:
        return None
    S = sess.spot(dec_ts)
    if S is None:
        return None
    T = _ttm_years(dec_ts)
    if T <= 0:
        return None
    # per-strike IV at decision (call & put separately captures skew)
    ivs = {}  # (K,right) -> (iv, oi)
    for (K, right), o in oi.items():
        if o <= 0:
            continue
        mid = sess.mid(dec_ts, K, right)
        if mid is None:
            continue
        iv = implied_vol(mid, S, K, T, right)
        if iv:
            ivs[(K, right)] = (iv, o)
    if not ivs:
        return None

    def gex_at(spot):
        tot = 0.0
        for (K, right), (iv, o) in ivs.items():
            g = greeks(spot, K, T, iv, right)
            if not g:
                continue
            c = g["gamma"] * o * 100 * spot * spot * 0.01
            tot += c if right == "C" else -c
        return tot

    gex_now = gex_at(S)
    # gamma-flip: zero-crossing of GEX scanning spot +/-60 (IV held per-strike)
    flip = None
    prev = None
    s2 = S - 60
    while s2 <= S + 60:
        v = gex_at(s2)
        if prev is not None and prev[1] != v and ((prev[1] <= 0 <= v) or (prev[1] >= 0 >= v)):
            flip = prev[0] + (s2 - prev[0]) * (0 - prev[1]) / (v - prev[1])  # linear interp
            break
        prev = (s2, v)
        s2 += 5
    # max pain: settlement strike minimizing total option payout
    call_oi = {K: o for (K, r), o in oi.items() if r == "C"}
    put_oi = {K: o for (K, r), o in oi.items() if r == "P"}
    strikes = sorted(set(call_oi) | set(put_oi))
    max_pain = None
    if strikes:
        max_pain = min(strikes, key=lambda P: sum(
            call_oi.get(K, 0) * max(0.0, P - K) + put_oi.get(K, 0) * max(0.0, K - P) for K in strikes))
    call_wall = max((K for K in call_oi if K >= S), key=lambda K: call_oi[K], default=None)
    put_wall = max((K for K in put_oi if K <= S), key=lambda K: put_oi[K], default=None)
    tot_c = sum(call_oi.values())
    tot_p = sum(put_oi.values())
    return {
        "gex": gex_now,                                   # >0 pin / <0 trend
        "gex_norm": gex_now / (S * S) if S else None,
        "gamma_flip": flip,
        "flip_dist": (S - flip) if flip is not None else None,   # spot above flip = pos-gamma
        "max_pain": max_pain,
        "max_pain_dist": (max_pain - S) if max_pain is not None else None,  # >0 = pull up
        "pc_oi_ratio": (tot_p / tot_c) if tot_c else None,
        "call_wall_dist": (call_wall - S) if call_wall is not None else None,
        "put_wall_dist": (S - put_wall) if put_wall is not None else None,
    }


if __name__ == "__main__":
    from .data import Session, connect
    from .label_dir import decision_ts
    con = connect()
    for d in ("2022-06-15", "2022-09-13", "2023-01-25"):
        s = Session(con, d)
        if s.empty():
            print(d, "no quote data")
            continue
        dec = decision_ts(s)
        f = gex_features(con, s, dec)
        if not f:
            print(d, "no OI/GEX")
            continue
        print(f"\n{d}  spot~{s.spot(dec):.0f}")
        for k, v in f.items():
            print(f"   {k:16s} {v:,.4f}" if isinstance(v, float) else f"   {k:16s} {v}")
