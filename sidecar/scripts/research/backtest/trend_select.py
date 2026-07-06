"""Selectivity pass on the trend gate: does requiring a STRONG, confirmed trend (not every
30-min sample) push the trade into net-positive under the operator's fixed-dollar exit?
Stratify trend-aligned entries by trend strength |spot-vwap| and momentum-confirmation;
report directional hit% AND net $/ct + win% (the operator's metric = banked money).
"""
from __future__ import annotations
import datetime as dt
import os
import sqlite3
import sys
from zoneinfo import ZoneInfo
import numpy as np
from .data import Session, connect, sessions
from .exits import simulate_fixed_dollar
from .holdout import dev_range

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")
FD = dict(qty=3, d_targets=(300.0, 500.0, 1000.0), d_stop=-500.0)
DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
TIERS = [(0, 5), (5, 10), (10, 20), (20, 1e9)]   # |spot-vwap| pts


def main():
    con = connect(); start, end = dev_range()
    dx = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    # buckets: key = (tier_idx, momentum_confirmed) -> list of (hit, pnl_per_ct)
    buckets = {}
    for d in sessions(con, start, end):
        rows = dx.execute("SELECT ts_ms,spot FROM option_dealer_exposure WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
        if len(rows) < 90:
            continue
        ts = [r[0] for r in rows]
        spot = np.array([r[1] for r in rows], float)
        vwap = np.cumsum(spot) / np.arange(1, len(spot) + 1)
        sess = None
        for i in range(len(rows)):
            h = dt.datetime.fromtimestamp(ts[i] / 1000, ET); hr = h.hour + h.minute / 60
            if hr < 10.0 or hr > 14.5 or (h.minute % 30) != 0 or i < 30:
                continue
            dist = spot[i] - vwap[i]
            tdir = "C" if dist > 0 else "P"
            mom = spot[i] - spot[i - 30]
            mom_confirm = (tdir == "C" and mom > 0) or (tdir == "P" and mom < 0)
            strength = abs(dist)
            tier = next(k for k, (lo, hi) in enumerate(TIERS) if lo <= strength < hi)
            fwd = min(i + 60, len(rows) - 1)
            up = spot[fwd] > spot[i]
            hit = (tdir == "C") == up
            if sess is None:
                sess = Session(con, d)
                if sess.empty(): sess = False
            pc = None
            if sess:
                ets = sess.minute_at_or_before(ts[i])
                atm = sess.atm(ets) if ets else None
                if atm:
                    t = simulate_fixed_dollar(sess, ets, tdir, atm, **FD)
                    if t: pc = t["pnl"] / 3.0
            buckets.setdefault((tier, mom_confirm), []).append((hit, pc))

    print("SELECTIVITY PASS — trend-aligned, by trend strength |spot-vwap| x momentum-confirm")
    print(f"  {'strength':12s} {'mom?':5s} {'n':>5s} {'hit%':>6s} {'net$/ct':>9s} {'win%':>6s}")
    for k in range(len(TIERS)):
        for mc in (True, False):
            b = buckets.get((k, mc), [])
            if not b:
                continue
            hits = [x[0] for x in b]
            pnls = [x[1] for x in b if x[1] is not None]
            lo, hi = TIERS[k]
            lbl = f"{lo}-{'inf' if hi > 1e8 else hi}pt"
            net = f"${np.mean(pnls):+.0f}" if pnls else "-"
            win = f"{100*np.mean([p>0 for p in pnls]):.0f}%" if pnls else "-"
            print(f"  {lbl:12s} {'yes' if mc else 'no':5s} {len(hits):5d} {100*np.mean(hits):5.1f}% {net:>9s} {win:>6s}")


if __name__ == "__main__":
    main()
