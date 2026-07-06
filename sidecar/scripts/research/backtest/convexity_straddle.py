"""STRADDLE confirmation of the vol-timing interpretation (READ-ONLY).

If the breakout's dollar edge is pure long-gamma / realized-vol timing (direction-independent —
already shown: win~50% in every |move| bucket, both CALL and PUT legs positive), then a
DIRECTION-NEUTRAL ATM STRADDLE (buy call AND put at the entry minute, +30min hold, sell both at
bid) at the SAME entries should also be positive, and with a HIGHER session-clustered t (the
direction coin-flip noise is removed). This both confirms the mechanism and gives a market-
neutral way to harvest it. Buy ask / sell bid on BOTH legs (two full spreads paid).

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.convexity_straddle
"""
from __future__ import annotations

import sqlite3
import sys

import numpy as np

from . import convexity_regime_exit as CX
from . import trend_breakout_entry as TB

sys.stdout.reconfigure(encoding="utf-8")


def leg(by_kr, k, right, t0):
    arr = by_kr.get((k, right))
    if arr is None:
        return None
    ts, bd, ak, qq = arr
    j0 = CX._nearest_idx(ts, t0)
    if j0 is None or not CX._valid(bd[j0], ak[j0], qq[j0]):
        return None
    a0 = float(ak[j0])
    if a0 < CX.MIN_ASK:
        return None
    xh, _ = CX.exit_hold(ts, bd, qq, j0, a0, t0, CX.HOLD_MS)
    if xh is None:
        return None
    return a0, xh


def clustered(by):
    means = [float(np.mean(v)) for v in by.values() if v]
    a = np.array(means, float)
    if len(a) < 2:
        return float("nan")
    se = a.std(ddof=1) / np.sqrt(len(a))
    return float(a.mean() / se) if se > 0 else float("nan")


def main():
    con = sqlite3.connect(f"file:{CX.DB}?mode=ro", uri=True)
    print("=" * 100)
    print("STRADDLE (direction-neutral) at the breakout entries — vol-timing confirmation")
    print("=" * 100)
    spx = CX.load_spx_rulers(con)
    _, _, vixprior = CX.load_vix_priorclose(con)
    for setname, lo, hi in [("DEV", CX.DEV_START, CX.DEV_END), ("HOLDOUT", CX.HOLD_START, CX.HOLD_END)]:
        sess = sorted([d for d in spx if lo <= d <= hi])
        by = {}          # date -> list of straddle $ per contract-pair
        n = 0
        for d in sess:
            rt, rp = spx[d]
            feat = CX.precompute_price(d, d[:4], rt, rp)
            ents = TB.entries_for_config(feat, CX.CFG)
            if not ents:
                continue
            by_kr, strikes = CX.load_option_session(con, d)
            if by_kr is None or strikes is None or len(strikes) == 0:
                continue
            for i, dr in ents:
                t0 = int(rt[i]); s0 = float(rp[i])
                if not (TB.SANE_LO <= s0 <= TB.SANE_HI):
                    continue
                # NO-LOOKAHEAD: fill both legs at the bar CLOSE (t0+FILL_LAG_MS), strike centered there.
                tf = t0 + CX.FILL_LAG_MS
                s_fill = CX.TB.real_at(rt, rp, tf)
                if s_fill is None:
                    continue
                k = float(strikes[int(np.argmin(np.abs(strikes - s_fill)))])
                lc = leg(by_kr, k, "C", tf)
                lp = leg(by_kr, k, "P", tf)
                if lc is None or lp is None:
                    continue
                pnl = ((lc[1] - lc[0]) + (lp[1] - lp[0])) * CX.MULT   # both legs, +30min hold
                by.setdefault(d, []).append(pnl)
                n += 1
        allv = np.array([x for v in by.values() for x in v], float)
        if len(allv):
            print(f"\n  [{setname}] ATM STRADDLE +30min hold (buy call+put ask, sell bid):")
            print(f"     n={len(allv)} sess={len(by)}  mean ${allv.mean():+.1f}/pair  med ${np.median(allv):+.1f}  "
                  f"win {100*np.mean(allv>0):.0f}%  t_clustered {clustered(by):+.2f}")
    con.close()
    print("\n  READ: positive straddle with t >= single-leg t confirms direction-neutral long-gamma/vol-timing.")


if __name__ == "__main__":
    main()
