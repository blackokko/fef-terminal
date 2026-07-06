"""Phase 3 - H3: vanna on vol-moving days. DEV ONLY. Pre-registered (HYPOTHESES.md):
gate |ATM-IV change from 09:30| >= 0.03 (3 vol pts, VIX-proxy); threshold |VEX| >=
dev-p70 on gated minutes; direction VEX>0->CALL / VEX<0->PUT. ATM bcd, 3ct. Must beat
regime-blind (no VEX threshold), opposite-side, random, drift-neutral; survive >1 year.
"""
from __future__ import annotations

import math
import random

import numpy as np

from .data import Session, connect, sessions
from .exits import simulate
from .h1_test import line, stat
from .holdout import assert_dev, dev_range

DIV_GATE, QTY, EXIT = 0.03, 3, "bcd"


def _rows(con, d):
    return con.execute("SELECT ts_ms,vex,iv_atm,spot FROM option_dealer_exposure "
                       "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()


def threshold(con, dates):
    vals = []
    for d in dates:
        rows = _rows(con, d)
        if not rows or rows[0][2] is None:
            continue
        iv0 = rows[0][2]
        for ts, vex, iv, sp in rows:
            if iv is not None and vex is not None and abs(iv - iv0) >= DIV_GATE:
                vals.append(abs(vex))
    return float(np.percentile(vals, 70)) if vals else None


def build_trades(con, dates, vex_p70):
    rng = random.Random(42)
    recs = []
    for d in dates:
        rows = _rows(con, d)
        if not rows or rows[0][2] is None:
            continue
        iv0 = rows[0][2]
        gated = [(ts, vex) for ts, vex, iv, sp in rows
                 if iv is not None and vex is not None and abs(iv - iv0) >= DIV_GATE]
        if not gated:
            continue
        trig = next(((ts, vex) for ts, vex in gated if abs(vex) >= vex_p70), None)
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
        # regime-blind: first gated minute, no VEX threshold, VEX-sign direction
        bts, bvex = gated[0]
        rec["blind"] = trade(bts, "CALL" if bvex > 0 else "PUT")
        if trig:
            ts, vex = trig
            direction = "CALL" if vex > 0 else "PUT"
            opp = "PUT" if direction == "CALL" else "CALL"
            rec["h3"] = trade(ts, direction)
            rec["opp"] = trade(ts, opp)
            rec["rnd"] = trade(ts, rng.choice(["CALL", "PUT"]))
        recs.append(rec)
    return recs


def main():
    np.random.seed(0)
    con = connect()
    start, end = dev_range()
    assert_dev(end)
    dates = sessions(con, start, end)
    vex_p70 = threshold(con, dates)
    print(f"H3 locked threshold: |VEX| p70 on gated minutes = {vex_p70:.3e}")
    recs = build_trades(con, dates, vex_p70)
    h3 = [r for r in recs if r.get("h3") is not None]
    print(f"H3 DEV: {len(h3)} sessions fired (|dIV|>=0.03 AND |VEX|>=p70) of {len(recs)} gated")
    print("\n--- HYPOTHESIS vs CONTROLS (ATM bcd P&L, 3ct, net friction) ---")
    line("H3 (vanna)", stat([r["h3"] for r in h3]), "  <-- must be > 0 and beat all below")
    line("regime-blind (no thr)", stat([r["blind"] for r in recs]))
    line("opposite-side", stat([r["opp"] for r in h3]))
    line("random-dir", stat([r["rnd"] for r in h3]))

    if len(h3) > 5:
        x = np.array([r["drift"] for r in h3], float)
        y = np.array([r["h3"] for r in h3], float)
        A = np.vstack([np.ones_like(x), x]).T
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        resid = y - A @ coef
        se = math.sqrt((resid @ resid) / (len(x) - 2)) * math.sqrt(np.linalg.inv(A.T @ A)[0, 0])
        a0 = coef[0]
        print(f"\ndrift-neutral: intercept=${a0:+.0f} (t={a0/se:+.1f}) -> "
              f"{'SURVIVES' if a0 > 0 and a0 / se >= 1.5 else 'fails'}")

    print("\n--- H3 by year ---")
    for yr in sorted({r["year"] for r in h3}):
        line(f"  {yr}", stat([r["h3"] for r in h3 if r["year"] == yr]))


if __name__ == "__main__":
    main()
