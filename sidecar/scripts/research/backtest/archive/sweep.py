"""Deterministic lever sweep over the naked-long space, using the FAITHFUL managed
exit system (3 partials at 1.5R/2.5R/5R, BE-ladder trailing, runner-protect) settled
in real option-dollars. Loads each session + signal once; evaluates every
(strike_offset x stop_R) combo. Ranks by total $ and t-stat.
"""
from __future__ import annotations

import argparse
import math
import statistics

from .data import Session, connect, sessions
from .executor import simulate_managed_long
from .families import FAMILIES

FAM_PARAMS = {
    "orb": {"or_min": 30},
    "trend_ema": {"ema_len": 20},
    "momentum": {"lookback_min": 15, "thresh_pts": 8.0},
}
OFFSETS = [-20, -10, 0, 10, 20]      # -ITM ... +OTM (points)
STOPS = [5, 8, 12, 18]               # stop distance R (SPX points)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2022-01-01")
    ap.add_argument("--to", dest="end", default="2023-03-01")
    ap.add_argument("--dollar-cap", type=float, default=None)
    a = ap.parse_args()
    con = connect()
    acc = {}        # (fam, off, R) -> list of per-contract pnl
    reasons = {}    # (fam, off, R) -> {reason: count}
    for d in sessions(con, a.start, a.end):
        s = Session(con, d)
        if s.empty():
            continue
        for fam, fp in FAM_PARAMS.items():
            sigs = FAMILIES[fam](s, **fp)
            if not sigs:
                continue
            entry_ts, right = sigs[0]
            atm = s.atm(entry_ts)
            if atm is None:
                continue
            for off in OFFSETS:
                soff = off if right == "C" else -off
                strike = min(s.strikes, key=lambda k: abs(k - (atm + soff)))
                for R in STOPS:
                    t = simulate_managed_long(s, entry_ts, right, strike, stop_pts=R,
                                              dollar_cap=a.dollar_cap)
                    if t:
                        key = (fam, off, R)
                        acc.setdefault(key, []).append(t["pnl_per_contract"])
                        rd = reasons.setdefault(key, {})
                        rd[t["reason"]] = rd.get(t["reason"], 0) + 1

    rows = []
    for key, pcs in acc.items():
        n = len(pcs)
        mean = sum(pcs) / n
        sd = statistics.pstdev(pcs) if n > 1 else 0.0
        tstat = (mean / sd * math.sqrt(n)) if sd > 0 else 0.0
        win = 100 * sum(1 for p in pcs if p > 0) / n
        total15 = sum(pcs) * 15  # 15-lot $ (per-contract * 15)
        rows.append((total15, tstat, mean, win, n, key, reasons[key]))
    rows.sort(reverse=True)

    print(f"range {a.start}..{a.end}  dollar_cap={a.dollar_cap}  configs={len(rows)}")
    print(f"{'$/15lot':>9} {'t':>6} {'$/ctr':>7} {'win%':>5} {'n':>4}  config           exit-mix")
    print("-- TOP 15 --")
    for total, t, mean, win, n, key, rd in rows[:15]:
        fam, off, R = key
        mix = ",".join(f"{k}:{v}" for k, v in sorted(rd.items(), key=lambda x: -x[1])[:3])
        print(f"{total:9.0f} {t:6.2f} {mean:7.1f} {win:5.1f} {n:4d}  {fam:10s} o{off:+3d} R{R:<2d}  {mix}")
    print("-- BOTTOM 5 --")
    for total, t, mean, win, n, key, rd in rows[-5:]:
        fam, off, R = key
        print(f"{total:9.0f} {t:6.2f} {mean:7.1f} {win:5.1f} {n:4d}  {fam:10s} o{off:+3d} R{R:<2d}")


if __name__ == "__main__":
    main()
