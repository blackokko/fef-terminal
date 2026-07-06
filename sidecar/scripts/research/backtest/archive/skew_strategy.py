"""Does the IV-skew directional edge make money? Gate naked LONG CALLS on high
put-over-call skew (the signal that held OOS at ~65% up), run the faithful managed
exits, settle in real option-dollars. Compare to an ungated long-call control.
"""
from __future__ import annotations

import argparse
import math
import statistics

from .data import Session, connect, sessions
from .executor import simulate_managed_long
from .feature_label import _iv_at


def run(start, end, thresh, R=8, offset=0, decision_min=30):
    con = connect()
    pcs, wins, total = [], 0, 0
    for d in sessions(con, start, end):
        s = Session(con, d)
        if s.empty():
            continue
        total += 1
        dec = s.minute_at_or_before(s.minutes[0] + decision_min * 60000)
        if dec is None:
            continue
        atm = s.atm(dec)
        if atm is None:
            continue
        ivp = _iv_at(s, dec, atm - 25, "P")
        ivc = _iv_at(s, dec, atm + 25, "C")
        if ivp is None or ivc is None:
            continue
        if (ivp - ivc) < thresh:        # gate: only high-skew days
            continue
        strike = min(s.strikes, key=lambda k: abs(k - (atm + offset)))
        t = simulate_managed_long(s, dec, "C", strike, stop_pts=R)
        if not t:
            continue
        pcs.append(t["pnl_per_contract"])
        wins += 1 if t["pnl"] > 0 else 0
    n = len(pcs)
    if n == 0:
        return f"{start}..{end} skew>={thresh:+.2f}: NO TRADES"
    mean = statistics.mean(pcs)
    sd = statistics.pstdev(pcs) if n > 1 else 0.0
    t = (mean / sd * math.sqrt(n)) if sd > 0 else 0.0
    return (f"{start}..{end} skew>={thresh:+.2f}: trades={n:3d}/{total:3d}  "
            f"win={100*wins/n:4.1f}%  ${mean:+6.0f}/ctr  total15=${sum(pcs)*15:+8.0f}  t={t:+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--R", type=int, default=8)
    ap.add_argument("--offset", type=float, default=0)
    a = ap.parse_args()
    print(f"LONG CALLS, managed exits (R={a.R}, offset={a.offset:+.0f})")
    for lbl, (start, end) in [("IN-SAMPLE 2022", ("2022-01-01", "2022-12-31")),
                              ("OOS 2023..24", ("2023-01-01", "2024-01-12"))]:
        print(f"\n{lbl}")
        for thresh in (-9.0, 0.02, 0.03, 0.04):   # -9 = ungated control
            tag = "control(all)" if thresh < 0 else f"skew gate {thresh:+.2f}"
            print(f"  {tag:16s} {run(start, end, thresh, R=a.R, offset=a.offset)}")


if __name__ == "__main__":
    main()
