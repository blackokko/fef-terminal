"""Does the EXIT method change the verdict? Run identical entries through the operator's
fixed-dollar exit ($300/$500/$1000, stop -$500) vs the SPX-points R-ladder vs gross.
Baselines: always-CALL / always-PUT (no directional edge) and hindsight-CORRECT (real edge).
If a real-edge entry is profitable under both exits but no-edge entries are not, the exit is
faithfully capturing edge when it exists -> the directional nulls are real, not exit artifacts.
"""
from __future__ import annotations
import datetime as dt
from zoneinfo import ZoneInfo
import numpy as np
from .data import Session, connect, sessions
from .exits import simulate_fixed_dollar, simulate_be_ladder
from .holdout import dev_range

ET = ZoneInfo("America/New_York")
FD = dict(qty=3, d_targets=(300.0, 500.0, 1000.0), d_stop=-500.0)   # operator's live settings


def emin(s, hh):
    for ts in s.minutes:
        h = dt.datetime.fromtimestamp(ts / 1000, ET)
        if h.hour + h.minute / 60 >= hh:
            return ts
    return None


def pc(t):
    return None if not t else t["pnl"] / 3.0


def run(side_fn, label):
    con = connect(); start, end = dev_range()
    fd, fdg, bl = [], [], []
    for d in sessions(con, start, end):
        s = Session(con, d)
        if s.empty():
            continue
        ets = emin(s, 14.0)
        if ets is None:
            continue
        atm = s.atm(ets); spot = s.spot(ets)
        if atm is None or spot is None:
            continue
        side = side_fn(s, ets, spot)
        if side is None:
            continue
        a = simulate_fixed_dollar(s, ets, side, atm, **FD)                  # your exit, net
        g = simulate_fixed_dollar(s, ets, side, atm, slip=0.0, **FD)        # your exit, gross
        b = simulate_be_ladder(s, ets, side, atm, qty=3, stop_pts=8)        # SPX-points, net
        if a: fd.append(pc(a))
        if g: fdg.append(pc(g))
        if b: bl.append(pc(b))
    f = np.array(fd); G = np.array(fdg); B = np.array(bl)
    print(f"\n{label} (n={len(f)}, near-ATM, qty=3):")
    print(f"  YOUR fixed-dollar  NET:   ${f.mean():+6.1f}/ct   win {100*(f>0).mean():3.0f}%")
    print(f"  YOUR fixed-dollar  GROSS: ${G.mean():+6.1f}/ct   win {100*(G>0).mean():3.0f}%  (no friction = directional ceiling)")
    print(f"  SPX-points R-ladder NET:  ${B.mean():+6.1f}/ct   win {100*(B>0).mean():3.0f}%")


def main():
    print("EXIT-METHOD PROOF — same entries, three exits. dev set, near-ATM, 3ct.")
    run(lambda s, t, sp: "C", "always-CALL (no edge)")
    run(lambda s, t, sp: "P", "always-PUT (no edge)")
    # hindsight: enter the side the session actually closes toward (a REAL directional edge)
    def hind(s, t, sp):
        close = s.spot(s.minutes[-1])
        if close is None:
            return None
        return "C" if close > sp else "P"
    run(hind, "hindsight-CORRECT direction (real edge)")


if __name__ == "__main__":
    main()
