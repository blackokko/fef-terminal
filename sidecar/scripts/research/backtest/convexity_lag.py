"""LOOKAHEAD robustness: SPX 1-min bars appear start-stamped (bar[t].close ~ spot at t+60s),
while option quotes at ts=t are the t snapshot. Filling the option at ts=t when the breakout is
confirmed by bar[t].close (~t+60s) is a ~1-min lookahead. Re-price the breakout entry filling at
the NEXT minute's quote (t0+LAG) with the +30min hold and scale-out anchored to the fill — the
conservative, no-lookahead entry. If the edge survives, it is not a fill-timing artifact.  (READ-ONLY)

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.convexity_lag
"""
from __future__ import annotations

import sqlite3
import sys

import numpy as np

from . import convexity_regime_exit as CX
from . import trend_breakout_entry as TB

sys.stdout.reconfigure(encoding="utf-8")


def clustered(by):
    means = [float(np.mean(v)) for v in by.values() if v]
    a = np.array(means, float)
    if len(a) < 2:
        return float("nan")
    se = a.std(ddof=1) / np.sqrt(len(a))
    return float(a.mean() / se) if se > 0 else float("nan")


def price_at(by_kr, strikes, rt, rp, t0, dr, lag):
    tf = t0 + lag
    s0 = CX.TB.real_at(rt, rp, tf)
    if s0 is None or not (TB.SANE_LO <= s0 <= TB.SANE_HI):
        return None
    right = "C" if dr > 0 else "P"
    k = float(strikes[int(np.argmin(np.abs(strikes - s0)))])
    arr = by_kr.get((k, right))
    if arr is None:
        return None
    ts, bd, ak, qq = arr
    j0 = CX._nearest_idx(ts, tf)
    if j0 is None or not CX._valid(bd[j0], ak[j0], qq[j0]):
        return None
    a0 = float(ak[j0])
    if a0 < CX.MIN_ASK:
        return None
    xh, _ = CX.exit_hold(ts, bd, qq, j0, a0, tf, CX.HOLD_MS)
    xs = CX.exit_scaleout(ts, bd, qq, j0, a0, tf, CX.SO_R1, CX.SO_R2, CX.SO_GIVE, CX.SO_CAP_H)
    if xh is None:
        return None
    return (xh - a0) * CX.MULT, (xs - a0) * CX.MULT


def main():
    con = sqlite3.connect(f"file:{CX.DB}?mode=ro", uri=True)
    print("=" * 104)
    print("LOOKAHEAD ROBUSTNESS — breakout ATM option-$ at entry fill lag 0 vs +60s (no-lookahead)")
    print("=" * 104)
    spx = CX.load_spx_rulers(con)
    for setname, lo, hi in [("DEV", CX.DEV_START, CX.DEV_END), ("HOLDOUT", CX.HOLD_START, CX.HOLD_END)]:
        sess = sorted([d for d in spx if lo <= d <= hi])
        # cache entries + option sessions once
        rows = []
        for d in sess:
            rt, rp = spx[d]
            feat = CX.precompute_price(d, d[:4], rt, rp)
            ents = TB.entries_for_config(feat, CX.CFG)
            if not ents:
                continue
            by_kr, strikes = CX.load_option_session(con, d)
            if by_kr is None or strikes is None or len(strikes) == 0:
                continue
            rows.append((d, rt, rp, by_kr, strikes, [(int(rt[i]), dr) for i, dr in ents]))
        for lag, lbl in [(0, "lag 0   (as-run, potential +1min lookahead)"),
                         (60_000, "lag +60s (fill next minute — NO lookahead)")]:
            hb, sb = {}, {}
            for d, rt, rp, by_kr, strikes, ents in rows:
                for t0, dr in ents:
                    r = price_at(by_kr, strikes, rt, rp, t0, dr, lag)
                    if r is not None:
                        hb.setdefault(d, []).append(r[0]); sb.setdefault(d, []).append(r[1])
            allh = np.array([x for v in hb.values() for x in v], float)
            alls = np.array([x for v in sb.values() for x in v], float)
            print(f"  [{setname}] {lbl}:")
            print(f"      hold  n={len(allh):5d} sess={len(hb):3d} mean ${allh.mean():+7.1f} med ${np.median(allh):+7.1f} "
                  f"win {100*np.mean(allh>0):3.0f}% t {clustered(hb):+5.2f}   |  "
                  f"scaleout mean ${alls.mean():+7.1f} med ${np.median(alls):+7.1f} win {100*np.mean(alls>0):3.0f}% t {clustered(sb):+5.2f}")
    con.close()
    print("\n  READ: if lag+60s stays strongly positive, the edge is NOT a 1-min fill-timing lookahead artifact.")


if __name__ == "__main__":
    main()
