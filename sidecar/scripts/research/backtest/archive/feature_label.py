"""Build the directional-conviction dataset: per-session entry-time-knowable FEATURES
at a fixed decision time, plus realized-outcome LABELS after it.

Features use ONLY data up to the decision minute (no lookahead). Labels describe what
the underlying did afterward. This is the substrate for mining directional conviction.
"""
from __future__ import annotations

from .data import Session, connect, sessions
from .greeks import implied_vol
from .gex_features import gex_features
from .flow_features import flow_features

_YEAR_MIN = 365.0 * 24 * 60


def _iv_at(sess, ts, strike, right):
    from datetime import datetime
    from .data import ET
    mid = sess.mid(ts, strike, right)
    S = sess.spot(ts)
    if mid is None or S is None:
        return None
    now = datetime.fromtimestamp(ts / 1000, ET)
    mins = (now.replace(hour=16, minute=0, second=0, microsecond=0) - now).total_seconds() / 60
    if mins <= 0:
        return None
    return implied_vol(mid, S, strike, mins / _YEAR_MIN, right)


def build_dataset(con, start, end, decision_min=30):
    rows = []
    prev_close = None
    for d in sessions(con, start, end):
        s = Session(con, d)
        if s.empty():
            rows_prev_close = prev_close
            continue
        t_open = s.minutes[0]
        dec_ts = s.minute_at_or_before(t_open + decision_min * 60000)
        if dec_ts is None:
            continue
        spot_open = s.spot(t_open)
        spot_dec = s.spot(dec_ts)
        if spot_open is None or spot_dec is None:
            prev_close = s.spot(s.minutes[-1])
            continue
        # opening range up to decision
        opening = [x for x in (s.spot(ts) for ts in s.minutes if ts <= dec_ts) if x is not None]
        or_hi, or_lo = max(opening), min(opening)
        or_size = or_hi - or_lo
        or_pos = (spot_dec - or_lo) / or_size if or_size > 0 else 0.5
        atm = s.atm(dec_ts)
        # vol / greeks features at decision
        iv_atm = _iv_at(s, dec_ts, atm, "C")
        iv_put_otm = _iv_at(s, dec_ts, atm - 25, "P")
        iv_call_otm = _iv_at(s, dec_ts, atm + 25, "C")
        iv_skew = (iv_put_otm - iv_call_otm) if (iv_put_otm and iv_call_otm) else None
        c_atm = s.mid(dec_ts, atm, "C")
        p_atm = s.mid(dec_ts, atm, "P")
        straddle = (c_atm + p_atm) if (c_atm and p_atm) else None
        # labels (after decision)
        spot_close = s.spot(s.minutes[-1])
        post = [x for x in (s.spot(ts) for ts in s.minutes if ts > dec_ts) if x is not None]
        mfe_up = max((x - spot_dec for x in post), default=0.0)
        mfe_dn = max((spot_dec - x for x in post), default=0.0)
        row = {
            "date": d,
            # base features (knowable at dec_ts)
            "gap": (spot_open - prev_close) if prev_close else None,
            "ret_open": spot_dec - spot_open,
            "or_size": or_size,
            "or_pos": or_pos,                     # 0=at low, 1=at high of opening range
            "iv_atm": iv_atm,
            "iv_skew": iv_skew,                   # put-OTM IV minus call-OTM IV (>0 = downside fear)
            "straddle": straddle,                 # implied move ($)
            "dow": __import__("datetime").datetime.fromisoformat(d).weekday(),  # 0=Mon
            # labels (realized after dec_ts)
            "ret_close": (spot_close - spot_dec) if spot_close else None,
            "dir_up": 1 if (spot_close and spot_close > spot_dec) else 0,
            "mfe_up": mfe_up,
            "mfe_dn": mfe_dn,
            "dom_up": 1 if mfe_up >= mfe_dn else 0,   # which side had the bigger excursion
        }
        # merge dealer-GEX + signed-flow features (None-safe: absent when OI/flow missing)
        g = gex_features(con, s, dec_ts)
        if g:
            row.update(g)
        fl = flow_features(con, d, dec_ts)
        if fl:
            row.update(fl)
        rows.append(row)
        prev_close = spot_close
    return rows


if __name__ == "__main__":
    con = connect()
    rows = build_dataset(con, "2022-01-01", "2023-03-01")
    print(f"built {len(rows)} sessions")
    r = rows[len(rows) // 2]
    print("sample row:", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()})
