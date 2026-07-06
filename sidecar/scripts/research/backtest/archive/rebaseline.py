"""Realistic-execution re-baseline of the directional charm/vanna signal, with
selectivity. Near-mid fills (enter mid+0.05, exit mid-0.05) instead of crossing the
spread. Question: as we demand a STRONGER signal (higher |CEX|/|VEX| percentile), does
the edge climb above breakeven? If the strongest readings clear under realistic fills,
the directional long is alive after all.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np

from .data import BAD, Session, connect, sessions
from .h1_test import line, stat
from .holdout import assert_dev, dev_range

ET = ZoneInfo("America/New_York")
QTY, COMM, PEAK_DD = 3, 1.30, 0.15


def naked_real(sess, ts, right, atm):
    q = sess.quote(ts, atm, right)
    if not q or q[2] in BAD:
        return None
    entry = (q[0] + q[1]) / 2 + 0.05
    if entry <= 0.05:
        return None
    peak = (q[0] + q[1]) / 2
    exit_px = last = None
    for t in sess.minutes:
        if t <= ts:
            continue
        qq = sess.quote(t, atm, right)
        if not qq or qq[2] in BAD:
            continue
        m = (qq[0] + qq[1]) / 2
        last = m
        peak = max(peak, m)
        if m <= peak * (1 - PEAK_DD):
            exit_px = max(0.0, m - 0.05)
            break
    if exit_px is None and last is not None:
        exit_px = max(0.0, last - 0.05)
    if exit_px is None:
        return None
    return (exit_px - entry) * QTY * 100 - COMM * QTY


def ph_rows(con, d):
    out = []
    for ts, cex, vex, spot in con.execute(
        "SELECT ts_ms,cex,vex,spot FROM option_dealer_exposure WHERE session_date=? ORDER BY ts_ms", (d,)):
        if cex is None or vex is None:
            continue
        h = dt.datetime.fromtimestamp(ts / 1000, ET)
        if 14.5 <= h.hour + h.minute / 60 < 16.0:
            out.append((ts, cex, vex, spot))
    return out


def run_subset(con, sess_rows, predicate):
    pnls = []
    for d, r in sess_rows.items():
        trig = next((row for row in r if predicate(row)), None)
        if not trig:
            continue
        s = Session(con, d)
        if s.empty():
            continue
        ts, cex = trig[0], trig[1]
        atm = s.atm(ts)
        if atm is None:
            continue
        pl = naked_real(s, ts, "C" if cex > 0 else "P", atm)
        if pl is not None:
            pnls.append(pl)
    return pnls


def main():
    con = connect()
    start, end = dev_range()
    assert_dev(end)
    sess_rows = {}
    allc, allv = [], []
    for d in sessions(con, start, end):
        r = ph_rows(con, d)
        if r:
            sess_rows[d] = r
            allc += [abs(x[1]) for x in r]
            allv += [abs(x[2]) for x in r]
    cps = {p: np.percentile(allc, p) for p in (70, 80, 90, 95)}
    vthr80 = np.percentile(allv, 80)

    print("charm directional, REALISTIC near-mid fills, escalating selectivity (3ct):")
    for p in (70, 80, 90, 95):
        thr = cps[p]
        line(f"  charm |CEX|>=p{p}", stat(run_subset(con, sess_rows, lambda row, t=thr: abs(row[1]) >= t)))
    print("\ncharm+vanna AGREE (both >=p80, same sign), realistic:")
    line("  charm&vanna p80", stat(run_subset(
        con, sess_rows,
        lambda row: abs(row[1]) >= cps[80] and abs(row[2]) >= vthr80 and np.sign(row[1]) == np.sign(row[2]))))


if __name__ == "__main__":
    main()
