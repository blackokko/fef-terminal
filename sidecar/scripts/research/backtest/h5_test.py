"""H5: charm/vanna naked long with a CONVEXITY-harvesting exit (let winners run).
Same H2 charm signals; exit at a large premium multiple (3x) or hold to expiry, full loss
on losers -- no profit cap. Tests whether the fat tail pays when uncapped. Logs the
winner-size distribution explicitly (the whole question).
"""
from __future__ import annotations

import numpy as np

from .data import BAD, Session, connect, sessions
from .exits import COMMISSION_RT, simulate
from .h1_test import line, stat
from .h2_test import CEX_P70, _ph_rows
from .holdout import assert_dev, dev_range

QTY, SLIP, MULT = 3, 0.10, 3.0


def convexity(sess, ts, right, atm):
    q = sess.quote(ts, atm, right)
    if not q or q[2] in BAD:
        return None
    entry = q[1] + SLIP
    if entry <= 0.05:
        return None
    target = entry * MULT
    exit_px = last = None
    for t in sess.minutes:
        if t <= ts:
            continue
        qq = sess.quote(t, atm, right)
        if not qq or qq[2] in BAD:
            continue
        last = qq[0]
        if (qq[0] + qq[1]) / 2 >= target:
            exit_px = max(0.0, qq[0] - SLIP)
            break
    if exit_px is None and last is not None:
        exit_px = max(0.0, last - SLIP)   # hold to expiry, take whatever (incl ~0)
    if exit_px is None:
        return None
    pnl = (exit_px - entry) * QTY * 100 - COMMISSION_RT * QTY
    return pnl, exit_px / entry


def main():
    con = connect()
    start, end = dev_range()
    assert_dev(end)
    pnls, mults = [], []
    for d in sessions(con, start, end):
        rows = _ph_rows(con, d)
        if not rows:
            continue
        trig = next(((ts, cex) for ts, cex, sp in rows if abs(cex) >= CEX_P70), None)
        if not trig:
            continue
        s = Session(con, d)
        if s.empty():
            continue
        ts, cex = trig
        atm = s.atm(ts)
        if atm is None:
            continue
        right = "C" if cex > 0 else "P"
        r = convexity(s, ts, right, atm)
        if r:
            pnls.append(r[0])
            mults.append(r[1])
    print(f"H5 convexity exit (3x target / hold to expiry) - {len(pnls)} H2 charm signals (DEV)")
    print("\n--- net P&L, 3 contracts ---")
    line("convexity naked long", stat(pnls))
    m = np.array(mults)
    print(f"\nwinner-size distribution (exit / entry premium):")
    print(f"  fraction >=3x: {(m>=3).mean():.0%}   >=2x: {(m>=2).mean():.0%}   >=1x(green): {(m>=1).mean():.0%}")
    print(f"  median {np.median(m):.2f}x   p90 {np.percentile(m,90):.2f}x   max {m.max():.1f}x")
    print(f"  -> need ~1 big winner to pay for ~{1/max((m>=3).mean(),1e-9):.0f} full losers; "
          f"avg multiple {m.mean():.2f}x")


if __name__ == "__main__":
    main()
