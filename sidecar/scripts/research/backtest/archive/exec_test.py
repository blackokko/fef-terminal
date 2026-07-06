"""Execution-quality sensitivity on the confirmed H2 charm signal. Same signal, same
trailing exit -- only the FILL assumption changes:
  cross   : entry ask+0.10, exit bid-0.10  (worst case, what we've been using)
  midslip : entry mid+0.05, exit mid-0.05  (work a limit, capture most of the spread)
  mid     : entry mid,      exit mid        (perfect mid fills, best case)
If the edge lives in the spread we've been crossing, mid flips it.
"""
from __future__ import annotations

import numpy as np

from .data import BAD, Session, connect, sessions
from .h1_test import line, stat
from .h2_test import CEX_P70, _ph_rows
from .holdout import assert_dev, dev_range

QTY, COMM, PEAK_DD = 3, 1.30, 0.15


def naked_pnl(sess, ts, right, atm, fill):
    q = sess.quote(ts, atm, right)
    if not q or q[2] in BAD:
        return None
    b0, a0, _ = q
    m0 = (b0 + a0) / 2
    entry = a0 + 0.10 if fill == "cross" else (m0 + 0.05 if fill == "midslip" else m0)
    if entry <= 0.05:
        return None
    peak, exit_px, last = m0, None, None
    for t in sess.minutes:
        if t <= ts:
            continue
        qq = sess.quote(t, atm, right)
        if not qq or qq[2] in BAD:
            continue
        b, a, _ = qq
        m = (b + a) / 2
        last = (b, m)
        peak = max(peak, m)
        if m <= peak * (1 - PEAK_DD):
            exit_px = max(0.0, b - 0.10) if fill == "cross" else (max(0.0, m - 0.05) if fill == "midslip" else m)
            break
    if exit_px is None and last is not None:
        b, m = last
        exit_px = max(0.0, b - 0.10) if fill == "cross" else (max(0.0, m - 0.05) if fill == "midslip" else m)
    if exit_px is None:
        return None
    return (exit_px - entry) * QTY * 100 - COMM * QTY


def main():
    con = connect()
    start, end = dev_range()
    assert_dev(end)
    res = {"cross": [], "midslip": [], "mid": []}
    spreads = []
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
        q = s.quote(ts, atm, right)
        if q and q[2] not in BAD:
            spreads.append(q[1] - q[0])
        for fill in res:
            p = naked_pnl(s, ts, right, atm, fill)
            if p is not None:
                res[fill].append(p)
    print(f"H2 charm signal, execution sensitivity ({len(res['cross'])} signals, 3ct)")
    if spreads:
        print(f"avg entry bid-ask spread: ${np.mean(spreads):.2f}/ct  (median ${np.median(spreads):.2f})  "
              f"-> full cross costs ~${np.mean(spreads)*100:.0f}/ct round trip\n")
    line("cross spread (worst)", stat(res["cross"]))
    line("mid + small slip", stat(res["midslip"]))
    line("perfect mid (best)", stat(res["mid"]))


if __name__ == "__main__":
    main()
