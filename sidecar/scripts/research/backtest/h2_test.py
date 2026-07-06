"""Phase 3 - H2: charm power-hour flow. DEV ONLY. Pre-registered (HYPOTHESES.md):
window 14:30-16:00 ET, threshold |CEX| >= dev-p70 (2.868e9), direction CEX>0->CALL /
CEX<0->PUT. ATM real-option P&L via the theta-aware bcd exit at 3ct. Must beat
regime-blind (no threshold), opposite-side, random, drift-neutral, and survive >1 year.
"""
from __future__ import annotations

import datetime as dt
import math
import random
from zoneinfo import ZoneInfo

import numpy as np

from .data import Session, connect, sessions
from .exits import simulate
from .h1_test import line, stat
from .holdout import assert_dev, dev_range

ET = ZoneInfo("America/New_York")
CEX_P70 = 2.868e9   # LOCKED: dev-set p70 of |CEX| in the 14:30-16:00 ET window
QTY, EXIT = 3, "bcd"


def _ph_rows(con, d):
    out = []
    for ts, cex, spot in con.execute(
        "SELECT ts_ms,cex,spot FROM option_dealer_exposure WHERE session_date=? ORDER BY ts_ms", (d,)):
        if cex is None:
            continue
        h = dt.datetime.fromtimestamp(ts / 1000, ET)
        hh = h.hour + h.minute / 60
        if 14.5 <= hh < 16.0:
            out.append((ts, cex, spot))
    return out


def build_trades():
    con = connect()
    start, end = dev_range()
    assert_dev(end)
    rng = random.Random(42)
    recs = []
    for d in sessions(con, start, end):
        rows = _ph_rows(con, d)
        if not rows:
            continue
        s = Session(con, d)
        if s.empty():
            continue
        o = s.spot(s.minutes[0])
        c = s.spot(s.minutes[-1])
        drift = (c - o) if (o and c) else 0.0

        def trade(ts, direction):
            atm = s.atm(ts)
            if atm is None:
                return None
            t = simulate(EXIT, s, ts, "C" if direction == "CALL" else "P", atm, qty=QTY)
            return t["pnl"] if t else None

        rec = {"date": d, "year": d[:4], "drift": drift}
        trig = next(((ts, cex) for ts, cex, sp in rows if abs(cex) >= CEX_P70), None)
        if trig:
            ts, cex = trig
            direction = "CALL" if cex > 0 else "PUT"
            opp = "PUT" if direction == "CALL" else "CALL"
            rec["h2"] = trade(ts, direction)
            rec["opp"] = trade(ts, opp)
            rec["rnd"] = trade(ts, rng.choice(["CALL", "PUT"]))
        # regime-blind: window start, CEX-sign direction, NO threshold gate
        bts, bcex, _ = rows[0]
        rec["blind"] = trade(bts, "CALL" if bcex > 0 else "PUT")
        recs.append(rec)
    return recs


def main():
    np.random.seed(0)
    recs = build_trades()
    h2 = [r for r in recs if r.get("h2") is not None]
    print(f"\nH2 DEV: {len(h2)} sessions fired (|CEX|>=p70 in power hour) of {len(recs)} with power-hour data")
    print("\n--- HYPOTHESIS vs CONTROLS (ATM bcd P&L, 3ct, net friction) ---")
    line("H2 (|CEX|>=p70)", stat([r["h2"] for r in h2]), "  <-- must be > 0 and beat all below")
    line("regime-blind (no thr)", stat([r["blind"] for r in recs]))
    line("opposite-side", stat([r["opp"] for r in h2]))
    line("random-dir", stat([r["rnd"] for r in h2]))

    if len(h2) > 5:
        x = np.array([r["drift"] for r in h2], float)
        y = np.array([r["h2"] for r in h2], float)
        A = np.vstack([np.ones_like(x), x]).T
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        resid = y - A @ coef
        se = math.sqrt((resid @ resid) / (len(x) - 2)) * math.sqrt(np.linalg.inv(A.T @ A)[0, 0])
        a0 = coef[0]
        print(f"\ndrift-neutral: intercept=${a0:+.0f} (t={a0/se:+.1f})  beta_drift={coef[1]:+.2f}  "
              f"-> {'SURVIVES' if a0 > 0 and a0 / se >= 1.5 else 'fails'}")

    print("\n--- H2 by year ---")
    for yr in sorted({r["year"] for r in h2}):
        line(f"  {yr}", stat([r["h2"] for r in h2 if r["year"] == yr]))


if __name__ == "__main__":
    main()
