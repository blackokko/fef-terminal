"""CONTROL for convexity_regime_exit: is the +$/contract from the BREAKOUT ENTRY, or would
ANY long 0DTE option held +30min in this sample be dollar-positive? (READ-ONLY)

If a RANDOM-minute / RANDOM-direction entry with IDENTICAL mechanics (buy ATM ask, sell bid,
+30min hold & scale-out) is ALSO ~+$170/contract, the "edge" is a structural property of the
instrument/sample (theta underpriced vs realized, or a data artifact) and the breakout adds
nothing. If the random baseline is ~0 or negative while the breakout is strongly positive, the
breakout entry specifically SELECTS high-realized-vol windows = a real long-gamma timing edge.

Controls (ATM, matched per-session entry COUNT so session-clustering matches):
  RAND  = random minute in 09:50-15:30 ET, random call/put.
  FLIP  = the ACTUAL breakout timestamps, direction FLIPPED (opposite side).
Compare mean$/contract & session-clustered t to the breakout's (b) hold and (d) scale-out.

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.convexity_control
"""
from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np

from . import convexity_regime_exit as CX
from . import trend_breakout_entry as TB

sys.stdout.reconfigure(encoding="utf-8")
REPS = 5
RNG = np.random.default_rng(12345)
WIN_LO, WIN_HI = TB.WIN_LO, TB.WIN_HI


def score_one(by_kr, strikes, rt, rp, t0, dr):
    """Return (hold$, scaleout$) for an ATM 0DTE entry at bar-time t0, direction dr. None if bad.
    NO-LOOKAHEAD: fill + hold anchored at t0+FILL_LAG_MS (the bar close), matching the fixed
    convexity_regime_exit harness (bars are START-stamped; see CX docstring)."""
    tf = t0 + CX.FILL_LAG_MS
    s0 = CX.TB.real_at(rt, rp, tf)
    if s0 is None:
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


def clustered(vals_by_sess):
    means = [float(np.mean(v)) for v in vals_by_sess.values() if v]
    a = np.array(means, float)
    if len(a) < 2:
        return float("nan")
    se = a.std(ddof=1) / np.sqrt(len(a))
    return float(a.mean() / se) if se > 0 else float("nan")


def run(setname, sessions, spx, con, brk_counts, brk_ts):
    hold_rand, so_rand = {}, {}
    hold_flip, so_flip = {}, {}
    for d in sessions:
        if d not in spx:
            continue
        rt, rp = spx[d]
        win_ts = [int(t) for t in rt if WIN_LO <= CX.et_h(int(t)) <= WIN_HI]
        if len(win_ts) < 5:
            continue
        by_kr, strikes = CX.load_option_session(con, d)
        if by_kr is None or strikes is None or len(strikes) == 0:
            continue
        nent = brk_counts.get(d, 0)
        # RAND: matched count, random minute+dir, REPS reps
        if nent > 0:
            hr, sr = [], []
            for _ in range(REPS):
                for _ in range(nent):
                    for _try in range(6):
                        t0 = int(RNG.choice(win_ts))
                        dr = 1 if RNG.random() < 0.5 else -1
                        r = score_one(by_kr, strikes, rt, rp, t0, dr)
                        if r is not None:
                            hr.append(r[0]); sr.append(r[1]); break
            if hr:
                hold_rand[d] = hr; so_rand[d] = sr
        # FLIP: actual breakout timestamps, opposite direction
        for (t0, dr) in brk_ts.get(d, []):
            r = score_one(by_kr, strikes, rt, rp, t0, -dr)
            if r is not None:
                hold_flip.setdefault(d, []).append(r[0])
                so_flip.setdefault(d, []).append(r[1])
    def rep(name, hb, sb):
        allh = [x for v in hb.values() for x in v]
        alls = [x for v in sb.values() for x in v]
        if not allh:
            print(f"    {name:26s} (empty)"); return
        print(f"    {name:26s} n={len(allh):5d} sess={len(hb):3d}  "
              f"hold mean {np.mean(allh):+8.1f} med {np.median(allh):+7.1f} win {100*np.mean(np.array(allh)>0):4.0f}% t {clustered(hb):+5.2f}   "
              f"|  scaleout mean {np.mean(alls):+8.1f} med {np.median(alls):+7.1f} win {100*np.mean(np.array(alls)>0):4.0f}% t {clustered(sb):+5.2f}")
    print(f"\n  [{setname}] ATM controls (matched per-session count; REPS={REPS} for RAND):")
    rep("RAND minute+dir", hold_rand, so_rand)
    rep("FLIP (opposite side)", hold_flip, so_flip)


def main():
    con = sqlite3.connect(f"file:{CX.DB}?mode=ro", uri=True)
    print("=" * 110)
    print("CONTROL — random-entry vs breakout-entry (identical ATM option mechanics)")
    print("=" * 110)
    spx = CX.load_spx_rulers(con)
    _, _, vixprior = CX.load_vix_priorclose(con)
    dev = sorted([d for d in spx if CX.DEV_START <= d <= CX.DEV_END])
    hold = sorted([d for d in spx if CX.HOLD_START <= d <= CX.HOLD_END])

    # rebuild the actual breakout entries (timestamps + counts) per session
    def brk(sessions):
        counts, tss = {}, {}
        for d in sessions:
            if d not in spx:
                continue
            rt, rp = spx[d]
            feat = CX.precompute_price(d, d[:4], rt, rp)
            ents = TB.entries_for_config(feat, CX.CFG)
            if ents:
                counts[d] = len(ents)
                tss[d] = [(int(rt[i]), dr) for (i, dr) in ents]
        return counts, tss

    print("rebuilding breakout entries ...", flush=True)
    dc, dts = brk(dev)
    hc, hts = brk(hold)
    print(f"  DEV breakout sessions {len(dc)}  HOLDOUT {len(hc)}")

    # reference: breakout ATM numbers from the main harness (recompute quickly for hold & scaleout)
    print("\n  REFERENCE — actual BREAKOUT entry, ATM (for comparison):")
    for setname, sessions, counts, tss in [("DEV", dev, dc, dts), ("HOLDOUT", hold, hc, hts)]:
        hb, sb = {}, {}
        for d in sessions:
            if d not in spx or d not in tss:
                continue
            rt, rp = spx[d]
            by_kr, strikes = CX.load_option_session(con, d)
            if by_kr is None or strikes is None or len(strikes) == 0:
                continue
            for (t0, dr) in tss[d]:
                r = score_one(by_kr, strikes, rt, rp, t0, dr)
                if r is not None:
                    hb.setdefault(d, []).append(r[0]); sb.setdefault(d, []).append(r[1])
        allh = [x for v in hb.values() for x in v]; alls = [x for v in sb.values() for x in v]
        print(f"    [{setname}] BREAKOUT  n={len(allh):5d} sess={len(hb):3d}  "
              f"hold mean {np.mean(allh):+8.1f} med {np.median(allh):+7.1f} win {100*np.mean(np.array(allh)>0):4.0f}% t {clustered(hb):+5.2f}   "
              f"|  scaleout mean {np.mean(alls):+8.1f} med {np.median(alls):+7.1f} win {100*np.mean(np.array(alls)>0):4.0f}% t {clustered(sb):+5.2f}")

    run("DEV", dev, spx, con, dc, dts)
    run("HOLDOUT", hold, spx, con, hc, hts)
    con.close()
    print("\nVERDICT KEY: if RAND ~= BREAKOUT -> instrument/sample artifact (breakout adds nothing).")
    print("             if RAND ~0/neg while BREAKOUT strongly + -> breakout times real vol bursts.")


if __name__ == "__main__":
    main()
