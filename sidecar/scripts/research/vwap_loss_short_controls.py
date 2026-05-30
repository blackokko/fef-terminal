"""Controls for the vwap_loss_short candidate (71% win / +0.62 E[R] / DSR 0.497
on ES RTH). Is it a real directional/timing edge, or beta to intraday downside?

Controls:
  - random_dir : same fire times, random call/put (the floor)
  - opposite   : take the OPPOSITE side (long) at the same signals — if loss_short
                 is real, longs should lose
  - sell_open  : short at each session's RTH open, same exit — if loss_short can't
                 beat sell-the-open it has no timing edge
  - beta-adjust: regress R on session drift (ES close-open). If the edge vanishes
                 drift-neutral, it's "shorts win on down sessions" (beta).
"""
from __future__ import annotations

import sqlite3
import statistics as stats
from collections import defaultdict

from fef_sidecar.engine.signals import dale, vwap
from fef_sidecar.engine.signals.dale import Bar, CALLS, PUTS
from scripts.research.dale_vwap_backtest import DB, et_date, stat_block, sharpe
from scripts.research.dale_vwap_backtest_v2 import atr_series, walk, ATR_STOP_MULT, ATR_TARGET_MULT
from scripts.research.vwap_rth_retest import load_es_rth

FRICTION = 1.5
TF = 5
import random
RNG = random.Random(11)


def score(entry, is_long, atr, bars1m, close_time, horizon):
    if atr is None or atr <= 0:
        return None
    stop = entry - ATR_STOP_MULT * atr if is_long else entry + ATR_STOP_MULT * atr
    target = entry + ATR_TARGET_MULT * atr if is_long else entry - ATR_TARGET_MULT * atr
    p = walk(entry, target, stop, is_long, bars1m, close_time, horizon)
    if p is None:
        return None
    rr = abs(target - entry) / abs(entry - stop)
    return p * rr + (1 - p) * (-1.0) - FRICTION / abs(entry - stop)


def main():
    c = sqlite3.connect(DB)
    bars1m = load_es_rth(c)
    bars = dale.resample(bars1m, TF)
    atr = atr_series(bars, 14)
    imap = {b.ts_ms: i for i, b in enumerate(bars)}
    sigs = [s for s in vwap.detect(bars, timeframe_min=TF, hold_bars=1, reject_hold_bars=0)
            if s.kind == "vwap_loss_short"]

    drift = {}
    bysess = defaultdict(list)
    for b in bars1m:
        bysess[et_date(b.ts_ms)].append(b)
    for sd, g in bysess.items():
        drift[sd] = g[-1].close - g[0].open

    base, rnd, opp, betas = [], [], [], []
    for s in sigs:
        i = imap.get(s.ts_ms)
        a = atr[i] if i is not None else None
        ct = s.ts_ms + TF * 60_000
        hz = et_date(s.ts_ms)
        rb = score(s.price, False, a, bars1m, ct, hz)   # short (the signal)
        if rb is None:
            continue
        base.append(rb); betas.append((drift[hz], rb))
        ro = score(s.price, True, a, bars1m, ct, hz)     # opposite = long
        if ro is not None:
            opp.append(ro)
        rr = score(s.price, RNG.random() < 0.5, a, bars1m, ct, hz)
        if rr is not None:
            rnd.append(rr)

    # sell-the-open control
    sopen = []
    for sd, g in bysess.items():
        first = g[0]
        i = imap.get(dale.resample(g, TF)[0].ts_ms) if g else None
        # atr at session start (use first 5-min bar index in full series)
        idx = next((k for k, b in enumerate(bars) if et_date(b.ts_ms) == sd), None)
        a = atr[idx] if idx is not None else None
        r = score(first.open, False, a, bars1m, first.ts_ms, sd)
        if r is not None:
            sopen.append(r)

    def line(name, rs):
        if not rs:
            print(f"  {name:<18} (none)"); return
        st = stat_block(rs, tf_min=TF, n_trials=5, var_sr=0.05)
        print(f"  {name:<18} n={st['n']:>3} win%={st['win_rate']*100:>5.1f} E[R]={st['expectancy_r']:+.3f} DSR={st['dsr']:.3f}")

    print(f"\n{'='*60}\nvwap_loss_short CONTROLS (ES RTH 5min)\n{'='*60}")
    line("loss_short (sig)", base)
    line("random_dir", rnd)
    line("opposite(long)", opp)
    line("sell_the_open", sopen)
    # beta-adjust
    xs = [d for d, _ in betas]; ys = [r for _, r in betas]
    if len(xs) > 2 and stats.pstdev(xs) > 0:
        mx, my = stats.fmean(xs), stats.fmean(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs)
        slope = cov / stats.pvariance(xs)
        intercept = my - slope * mx
        corr = cov / (stats.pstdev(xs) * stats.pstdev(ys))
        print(f"\n  beta-adjust R~session_drift: corr={corr:+.2f} slope={slope:+.4f}/pt "
              f"intercept(drift-neutral)={intercept:+.3f}")
        print(f"  -> {'BETA (edge vanishes drift-neutral)' if intercept <= 0.05 else 'residual edge survives drift control'}")
    print(f"\n  verdict: loss_short E[R]={stats.fmean(base):+.3f} must beat random "
          f"({stats.fmean(rnd) if rnd else float('nan'):+.3f}), opposite-long should be negative "
          f"({stats.fmean(opp) if opp else float('nan'):+.3f}), and beat sell-open "
          f"({stats.fmean(sopen) if sopen else float('nan'):+.3f}).")


if __name__ == "__main__":
    main()
