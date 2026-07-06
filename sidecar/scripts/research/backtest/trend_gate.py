"""Trend-alignment gate test. The engine picks direction from FLOW (cex sign). Does
PRICE-TREND predict direction better, and does gating flow by trend help? For each dev
session we read per-minute (spot, cex) from option_dealer_exposure, and at decision points
compare direction rules on (A) forward 60-min hit rate and (B) net $/ct under the operator's
fixed-dollar exit (near-ATM, 3ct). If trend / flow-AND-trend beat flow and clear ~50%+ with
better P&L, the gate is worth implementing.
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
RULES = ["trend_vwap", "mom_30", "flow_cex", "flow_AND_trend", "counter_trend"]


def main():
    con = connect(); start, end = dev_range()
    dx = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    hit = {k: [] for k in RULES}
    pnl = {k: [] for k in ["trend_vwap", "flow_cex", "flow_AND_trend", "counter_trend"]}
    for d in sessions(con, start, end):
        rows = dx.execute("SELECT ts_ms,spot,cex FROM option_dealer_exposure WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
        if len(rows) < 90:
            continue
        ts = [r[0] for r in rows]
        spot = np.array([r[1] for r in rows], float)
        cex = np.array([r[2] for r in rows], float)
        vwap = np.cumsum(spot) / np.arange(1, len(spot) + 1)
        sess = None
        for i in range(len(rows)):
            h = dt.datetime.fromtimestamp(ts[i] / 1000, ET); hr = h.hour + h.minute / 60
            if hr < 10.0 or hr > 14.5 or (h.minute % 30) != 0:
                continue
            fwd = min(i + 60, len(rows) - 1)
            up = spot[fwd] > spot[i]
            tdir = "C" if spot[i] > vwap[i] else "P"
            mdir = ("C" if spot[i] > spot[i - 30] else "P") if i >= 30 else None
            fdir = "C" if cex[i] > 0 else "P"
            cdir = "P" if tdir == "C" else "C"
            def H(x): return None if x is None else ((x == "C") == up)
            hit["trend_vwap"].append(H(tdir)); hit["flow_cex"].append(H(fdir))
            hit["counter_trend"].append(H(cdir))
            if mdir: hit["mom_30"].append(H(mdir))
            if fdir == tdir: hit["flow_AND_trend"].append(H(fdir))
            # P&L under fixed-dollar exit
            if sess is None:
                sess = Session(con, d)
                if sess.empty(): sess = False
            if sess:
                ets = sess.minute_at_or_before(ts[i])
                atm = sess.atm(ets) if ets else None
                if atm:
                    for rule, rd in (("trend_vwap", tdir), ("flow_cex", fdir), ("counter_trend", cdir)):
                        t = simulate_fixed_dollar(sess, ets, rd, atm, **FD)
                        if t: pnl[rule].append(t["pnl"] / 3.0)
                    if fdir == tdir:
                        t = simulate_fixed_dollar(sess, ets, fdir, atm, **FD)
                        if t: pnl["flow_AND_trend"].append(t["pnl"] / 3.0)

    print("=== A) DIRECTIONAL HIT RATE (forward 60 min; 50% = coin flip) ===")
    for k in RULES:
        v = [x for x in hit[k] if x is not None]
        if v: print(f"  {k:16s} n={len(v):5d}  hit={100*np.mean(v):.1f}%")
    print("\n=== B) NET $/ct under YOUR fixed-dollar exit (near-ATM, 3ct) ===")
    for k in ["flow_cex", "trend_vwap", "flow_AND_trend", "counter_trend"]:
        v = pnl[k]
        if v: print(f"  {k:16s} n={len(v):5d}  mean=${np.mean(v):+6.1f}/ct  win={100*np.mean([x>0 for x in v]):3.0f}%")


if __name__ == "__main__":
    main()
