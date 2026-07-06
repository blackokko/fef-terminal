"""H4 drag diagnostic (Chat's decisive first number): the SAME confirmed H2 charm signals,
expressed as a naked ATM long vs an ATM debit spread (w=10, w=20). Does the cheaper
structure cut the theta+spread drag enough to let the real ~$30-50 edge survive --
before committing to the full control stack?
"""
from __future__ import annotations

import numpy as np

from .data import Session, connect, sessions
from .exits import simulate
from .h1_test import line, stat
from .h2_test import CEX_P70, _ph_rows
from .holdout import assert_dev, dev_range
from .spreads import simulate_debit_spread

QTY = 3


def main():
    con = connect()
    start, end = dev_range()
    assert_dev(end)
    naked, sp10, sp20 = [], [], []
    entries, debits = [], []
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
        direction = "CALL" if cex > 0 else "PUT"
        right = "C" if direction == "CALL" else "P"
        tn = simulate("bcd", s, ts, right, atm, qty=QTY)
        if tn:
            naked.append(tn["pnl"])
            entries.append(tn["entry"])
        for w, bucket in ((10, sp10), (20, sp20)):
            td = simulate_debit_spread(s, ts, direction, atm, w, qty=QTY)
            if td:
                bucket.append(td["pnl"])
                if w == 20:
                    debits.append(td["debit"])
    print(f"H4 drag diagnostic - {len(naked)} H2 charm signals (DEV)")
    if entries and debits:
        print(f"avg naked entry premium: ${np.mean(entries):.2f}/ct   "
              f"avg 20-wide debit (cost-at-risk): ${np.mean(debits):.2f}/ct   "
              f"-> spread risks {np.mean(debits)/np.mean(entries):.0%} of the naked premium")
    print("\n--- net P&L, 3 contracts, net friction ---")
    line("naked ATM long (bcd)", stat(naked))
    line("debit spread w=10", stat(sp10))
    line("debit spread w=20", stat(sp20))


if __name__ == "__main__":
    main()
