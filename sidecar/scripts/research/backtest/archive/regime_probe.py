"""Does an entry-time-knowable regime filter concentrate naked-long edge?

Takes the only non-losing base (OTM hold-to-close) and buckets its trades by two
filters known AT ENTRY: opening-range size (first 30m high-low, a realized-vol/trend
proxy) and the 09:31 ATM straddle (implied expected move). If a high bucket is clearly
positive (t well above 0), there's a lead worth a rigorous Workflow search; if every
bucket hugs zero, naked longs have no concentratable edge here.
"""
from __future__ import annotations

import math
import statistics

from .data import Session, connect, sessions
from .families import FAMILIES
from .executor import simulate_naked_long

BASES = [
    ("trend_ema", {"ema_len": 20}, 20),
    ("trend_ema", {"ema_len": 20}, 10),
    ("momentum", {"lookback_min": 15, "thresh_pts": 8.0}, 20),
]


def stats(pnls):
    n = len(pnls)
    if n == 0:
        return 0.0, 0.0, 0.0, 0
    m = sum(pnls) / n
    sd = statistics.pstdev(pnls) if n > 1 else 0.0
    t = (m / sd * math.sqrt(n)) if sd > 0 else 0.0
    win = 100 * sum(1 for p in pnls if p > 0) / n
    return sum(pnls), t, win, n


def terciles(recs, key, label):
    recs = [r for r in recs if r[key] is not None]
    recs.sort(key=lambda r: r[key])
    n = len(recs)
    third = n // 3
    groups = [("low ", recs[:third]), ("mid ", recs[third:2 * third]), ("high", recs[2 * third:])]
    for tag, grp in groups:
        if not grp:
            continue
        tot, t, win, k = stats([r["pnl"] for r in grp])
        bound = f"{grp[0][key]:.1f}..{grp[-1][key]:.1f}"
        print(f"     {label} {tag}  ({bound:>14})  n={k:3d}  total={tot:8.0f}  t={t:5.2f}  win={win:4.1f}")


def main():
    con = connect()
    for fam, fp, off in BASES:
        recs = []
        for d in sessions(con, "2022-01-01", "2023-03-01"):
            s = Session(con, d)
            if s.empty():
                continue
            sigs = FAMILIES[fam](s, **fp)
            if not sigs:
                continue
            entry_ts, right = sigs[0]
            atm = s.atm(entry_ts)
            if atm is None:
                continue
            soff = off if right == "C" else -off
            strike = min(s.strikes, key=lambda k: abs(k - (atm + soff)))
            t = simulate_naked_long(s, entry_ts, right, strike, hold_min=390)
            if not t:
                continue
            t0 = s.minutes[0]
            cut = t0 + 30 * 60000
            spots = [x for x in (s.spot(ts) for ts in s.minutes if ts <= cut) if x is not None]
            or30 = (max(spots) - min(spots)) if spots else None
            atm0 = s.atm(t0)
            c0 = s.mid(t0, atm0, "C") if atm0 else None
            p0 = s.mid(t0, atm0, "P") if atm0 else None
            strad = (c0 + p0) if (c0 is not None and p0 is not None) else None
            recs.append({"pnl": t["pnl"], "or30": or30, "strad": strad})
        tot, t, win, n = stats([r["pnl"] for r in recs])
        print(f"\n{fam} OTM+{off} hold-to-close   n={n}  total={tot:.0f}  t={t:.2f}  win={win:.1f}")
        terciles(recs, "or30", "OR30 ")
        terciles(recs, "strad", "strad")


if __name__ == "__main__":
    main()
