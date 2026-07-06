"""Phase 3 - H1: short-gamma continuation. DEV SET ONLY. Pre-registered in HYPOTHESES.md
(thresholds NOT changed here). ATM real-option P&L via the theta-aware bcd exit at 3 ct.

Hypothesis: in SHORT_GAMMA, an established move (>=8pt/10min) continues; long the thrust
side. Must beat: regime-contrast (LONG_GAMMA), regime-blind (all), opposite-side, random,
and survive a drift-neutral regression + >1 year.
"""
from __future__ import annotations

import math
import random

import numpy as np

from .data import Session, connect, load_oi, roc_thrust, sessions
from .dealer_exposure import exposures_at
from .exits import simulate
from .holdout import assert_dev, dev_range

THRUST, LOOKBACK, CONT, REV, END_MIN, QTY, EXIT = 8, 10, 8, 4, 330, 3, "bcd"


def continuation(sess, ts, direction):
    s0 = sess.spot(ts)
    if s0 is None:
        return None, None
    sgn = 1 if direction == "CALL" else -1
    for t in sess.minutes:
        if t <= ts:
            continue
        s = sess.spot(t)
        if s is None:
            continue
        fav = sgn * (s - s0)
        if fav >= CONT:
            return 1, (t - ts) // 60000
        if fav <= -REV:
            return 0, None
    return 0, None


def build_trades():
    con = connect()
    start, end = dev_range()
    assert_dev(end)
    rng = random.Random(42)
    recs = []
    for d in sessions(con, start, end):
        s = Session(con, d)
        if s.empty():
            continue
        sig = roc_thrust(s, THRUST, LOOKBACK, end_min=END_MIN)
        if not sig:
            continue
        ts, direction = sig
        atm = s.atm(ts)
        if atm is None:
            continue
        e = exposures_at(s, load_oi(con, d), ts)
        if not e:
            continue
        right = "C" if direction == "CALL" else "P"
        t = simulate(EXIT, s, ts, right, atm, qty=QTY)
        if not t:
            continue
        cont, mins = continuation(s, ts, direction)
        opp_right = "P" if direction == "CALL" else "C"
        topp = simulate(EXIT, s, ts, opp_right, atm, qty=QTY)
        rdir = rng.choice(["C", "P"])
        trnd = simulate(EXIT, s, ts, rdir, atm, qty=QTY)
        o = s.spot(s.minutes[0])
        c = s.spot(s.minutes[-1])
        recs.append({"date": d, "year": d[:4], "regime": e["regime"], "gex": e["gex"],
                     "pnl": t["pnl"], "opp_pnl": topp["pnl"] if topp else None,
                     "rnd_pnl": trnd["pnl"] if trnd else None, "continued": cont,
                     "mins_to_cont": mins, "drift": (c - o) if (o and c) else 0.0,
                     "hold": t.get("hold_min")})
    return recs


def stat(pnls):
    a = np.array([p for p in pnls if p is not None], float)
    n = len(a)
    if n == 0:
        return None
    m = a.mean()
    sd = a.std(ddof=1) if n > 1 else 0.0
    t = (m / sd * math.sqrt(n)) if sd > 0 else 0.0
    boot = np.array([np.random.choice(a, n, replace=True).mean() for _ in range(2000)])
    return {"n": n, "win": 100 * (a > 0).mean(), "mean": m, "total": a.sum(), "t": t,
            "lo": np.percentile(boot, 2.5), "hi": np.percentile(boot, 97.5)}


def line(tag, st, extra=""):
    if not st:
        print(f"  {tag:22s} (no trades)")
        return
    print(f"  {tag:22s} n={st['n']:4d} win={st['win']:4.0f}% mean=${st['mean']:+6.0f} "
          f"tot=${st['total']:+8.0f} t={st['t']:+4.1f} CI[${st['lo']:+5.0f},${st['hi']:+5.0f}]{extra}")


def main():
    np.random.seed(0)
    recs = build_trades()
    short = [r for r in recs if r["regime"] == "SHORT_GAMMA"]
    longg = [r for r in recs if r["regime"] == "LONG_GAMMA"]
    trans = [r for r in recs if r["regime"] == "TRANSITION"]
    print(f"\nH1 DEV trades: {len(recs)}  (SHORT={len(short)} LONG={len(longg)} TRANS={len(trans)})")

    sc = sum(1 for r in short if r["continued"]) / len(short) if short else 0
    lc = sum(1 for r in longg if r["continued"]) / len(longg) if longg else 0
    print(f"\ncontinuation rate (spot extends >=8pt before >=4 reversal):  SHORT={100*sc:.0f}%  LONG={100*lc:.0f}%")

    print("\n--- HYPOTHESIS vs CONTROLS (ATM bcd P&L, 3ct, net friction) ---")
    line("SHORT_GAMMA (H1)", stat([r["pnl"] for r in short]), "  <-- must be > 0 and beat all below")
    line("LONG_GAMMA (contrast)", stat([r["pnl"] for r in longg]))
    line("regime-blind (all)", stat([r["pnl"] for r in recs]))
    line("opposite-side (SHORT)", stat([r["opp_pnl"] for r in short]))
    line("random-dir (SHORT)", stat([r["rnd_pnl"] for r in short]))

    # drift-neutral regression on SHORT trades: pnl = a + b*drift
    if len(short) > 5:
        x = np.array([r["drift"] for r in short], float)
        y = np.array([r["pnl"] for r in short], float)
        A = np.vstack([np.ones_like(x), x]).T
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        resid = y - A @ coef
        se = math.sqrt((resid @ resid) / (len(x) - 2)) * math.sqrt(np.linalg.inv(A.T @ A)[0, 0])
        a0 = coef[0]
        print(f"\ndrift-neutral: intercept=${a0:+.0f} (t={a0/se:+.1f})  beta_drift={coef[1]:+.2f}  "
              f"-> {'SURVIVES' if a0>0 and a0/se>=1.5 else 'fails'} (edge {'not ' if a0>0 and a0/se>=1.5 else ''}just drift)")

    print("\n--- SHORT_GAMMA by year ---")
    for y in sorted({r["year"] for r in short}):
        line(f"  {y}", stat([r["pnl"] for r in short if r["year"] == y]))

    wins = [r["mins_to_cont"] for r in short if r["continued"] and r["mins_to_cont"] is not None]
    if wins:
        wins.sort()
        print(f"\nspeed-of-move (SHORT continued): median {wins[len(wins)//2]}min to +8pt, "
              f"p25={wins[len(wins)//4]} p75={wins[3*len(wins)//4]}min")


if __name__ == "__main__":
    main()
