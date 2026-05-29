"""Close the momentum question: is dale_bull_cross (+0.205 E[R]) a real timing
edge, or just beta to an up-trending tape? Two controls (Chat 2026-05-29):

  (a) Beta-adjust: regress bull_cross trade R on the session's own drift
      (SPX close-open). If expectancy is explained by drift (high corr, ~0
      intercept), it's beta.
  (b) Buy-the-open control: go long at every session's RTH open with the SAME
      ATR 2:1 exit. If bull_cross can't beat buy-the-open, it has no timing edge.

Valid data only: Dale runs on SPX RTH bars (the ES bars in historical_bar are
overnight — VWAP signals excluded here).
"""

from __future__ import annotations

import sqlite3
import statistics as stats
from collections import defaultdict

from fef_sidecar.engine.signals import dale
from scripts.research.dale_vwap_backtest import DB, load_bars, et_date, _t1_prob
from scripts.research.dale_vwap_backtest_v2 import atr_series, walk, ATR_STOP_MULT, ATR_TARGET_MULT

FRICTION_PTS = 1.5


def bull_cross_outcomes(spx, spx5, atr):
    imap = {b.ts_ms: i for i, b in enumerate(spx5)}
    sigs = [s for s in dale.detect(spx5, timeframe_min=5) if s.kind == "dale_bull_cross"]
    out = []
    for s in sigs:
        i = imap.get(s.ts_ms)
        a = atr[i] if i is not None else None
        if not a or a <= 0:
            continue
        entry = s.price
        stop = entry - ATR_STOP_MULT * a
        target = entry + ATR_TARGET_MULT * a
        sd = et_date(s.ts_ms)
        p = walk(entry, target, stop, True, spx, s.ts_ms + 5 * 60_000, sd)
        if p is None:
            continue
        risk = entry - stop
        r = p * (target - entry) / risk + (1 - p) * (-1.0) - FRICTION_PTS / risk
        out.append((sd, r, p))
    return out


def buy_the_open(spx, spx5, atr):
    """Long at each session's first RTH bar; ATR 2:1 exit, same friction."""
    imap = {b.ts_ms: i for i, b in enumerate(spx5)}
    by_sess = defaultdict(list)
    for b in spx5:
        by_sess[et_date(b.ts_ms)].append(b)
    out = []
    for sd in sorted(by_sess):
        first = by_sess[sd][0]
        i = imap.get(first.ts_ms)
        a = atr[i] if i is not None else None
        if not a or a <= 0:
            continue
        entry = first.open
        stop = entry - ATR_STOP_MULT * a
        target = entry + ATR_TARGET_MULT * a
        p = walk(entry, target, stop, True, spx, first.ts_ms, sd)
        if p is None:
            continue
        risk = entry - stop
        r = p * (target - entry) / risk + (1 - p) * (-1.0) - FRICTION_PTS / risk
        out.append((sd, r, p))
    return out


def session_drift(spx):
    by = defaultdict(list)
    for b in spx:
        by[et_date(b.ts_ms)].append(b)
    return {sd: g[-1].close - g[0].open for sd, g in by.items()}


def summarize(name, rs):
    if not rs:
        print(f"  {name}: (none)"); return
    n = len(rs)
    print(f"  {name:<28} n={n:>4}  win%={sum(1 for r in rs if r>0)/n*100:>5.1f}  E[R]={stats.fmean(rs):+.3f}")


def main():
    c = sqlite3.connect(DB)
    spx = load_bars(c, "SPX")
    spx5 = dale.resample(spx, 5)
    atr = atr_series(spx5)

    bc = bull_cross_outcomes(spx, spx5, atr)
    bto = buy_the_open(spx, spx5, atr)
    drift = session_drift(spx)

    print(f"\n{'='*70}\nMOMENTUM CONTROL — dale_bull_cross vs being-long (SPX RTH)\n{'='*70}")
    summarize("dale_bull_cross", [r for _, r, _ in bc])
    summarize("buy_the_open (ctrl)", [r for _, r, _ in bto])

    # (a) beta-adjust regression: r ~ drift
    xs = [drift.get(sd, 0.0) for sd, _, _ in bc]
    ys = [r for _, r, _ in bc]
    if len(xs) > 2 and stats.pstdev(xs) > 0:
        mx, my = stats.fmean(xs), stats.fmean(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs)
        slope = cov / stats.pvariance(xs)
        intercept = my - slope * mx
        corr = cov / (stats.pstdev(xs) * stats.pstdev(ys)) if stats.pstdev(ys) > 0 else 0.0
        print(f"\n  Beta-adjust  r ~ session_drift:")
        print(f"    corr(r, drift) = {corr:+.3f}   slope = {slope:+.4f}/pt")
        print(f"    intercept (drift-neutral alpha) = {intercept:+.3f} R")
        print(f"    -> {'BETA (alpha vanishes drift-neutral)' if intercept <= 0.02 else 'residual alpha survives — investigate'}")

    # verdict
    bc_e = stats.fmean(ys) if ys else float('nan')
    bto_e = stats.fmean([r for _, r, _ in bto]) if bto else float('nan')
    print(f"\n  VERDICT: bull_cross E[R]={bc_e:+.3f} vs buy-the-open E[R]={bto_e:+.3f}")
    print(f"  -> {'BETA: bull_cross does NOT beat being long' if bc_e <= bto_e + 0.02 else 'bull_cross beats buy-the-open — timing edge candidate'}")


if __name__ == "__main__":
    main()
