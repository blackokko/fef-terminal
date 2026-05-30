"""Re-test VWAP signals on CORRECT ES RTH bars (the v1/v2 runs used overnight ES
with VWAP mis-anchored to the 6pm open). Same-bar entry + v2 signal-matched
exits. Reversion (band-reject) -> target session VWAP, stop beyond the touch
extreme. Momentum (reclaim/loss) -> ATR 2:1. R in risk units, friction in pts.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics as stats
from collections import defaultdict

from fef_sidecar.engine.signals import dale, vwap
from fef_sidecar.engine.signals.dale import Bar, CALLS
from fef_sidecar.engine.signals.vwap import anchored_vwap, session_key
from scripts.research.dale_vwap_backtest import DB, et_date, stat_block, sharpe
from scripts.research.dale_vwap_backtest_v2 import (
    atr_series, walk, MOMENTUM, REVERSION, STOP_BUF, ATR_STOP_MULT, ATR_TARGET_MULT,
    MIN_RISK_PTS, MAX_RR,
)

BAR_SIZE = "1 min RTH"


def load_es_rth(c):
    rows = c.execute(
        "SELECT timestamp_ms,open,high,low,close,volume FROM historical_bar "
        "WHERE symbol='ES' AND bar_size=? ORDER BY timestamp_ms", (BAR_SIZE,),
    ).fetchall()
    return [Bar(ts_ms=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5] or 0) for r in rows]


def run(tf, bars1m, friction_pts):
    bars = dale.resample(bars1m, tf)
    vw = anchored_vwap(bars, session_key)
    atr = atr_series(bars, 14)
    imap = {b.ts_ms: i for i, b in enumerate(bars)}
    sigs = vwap.detect(bars, timeframe_min=tf, hold_bars=1, reject_hold_bars=0)
    out = defaultdict(list)
    for s in sigs:
        i = imap.get(s.ts_ms)
        if i is None:
            continue
        is_long = s.direction == CALLS
        entry = s.price
        bar = bars[i]
        if s.kind in REVERSION:
            mean = vw[i].vwap
            stop = (bar.low - STOP_BUF) if is_long else (bar.high + STOP_BUF)
            target = mean
        else:  # momentum
            a = atr[i]
            if a is None or a <= 0:
                continue
            stop = entry - ATR_STOP_MULT * a if is_long else entry + ATR_STOP_MULT * a
            target = entry + ATR_TARGET_MULT * a if is_long else entry - ATR_TARGET_MULT * a
        if is_long and target <= entry:
            continue
        if (not is_long) and target >= entry:
            continue
        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk < MIN_RISK_PTS or reward <= 0 or reward / risk > MAX_RR:
            continue
        horizon = et_date(s.ts_ms)
        p = walk(entry, target, stop, is_long, bars1m, s.ts_ms + tf * 60_000, horizon)
        if p is None:
            continue
        rr = reward / risk
        r = p * rr + (1 - p) * (-1.0) - friction_pts / risk
        out[s.kind].append(r)
        out["__ALL__"].append(r)
        out["__" + ("MOM" if s.kind in MOMENTUM else "REV")].append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--friction-pts", type=float, default=1.5)
    args = ap.parse_args()
    c = sqlite3.connect(DB)
    bars1m = load_es_rth(c)
    sess = sorted({et_date(b.ts_ms) for b in bars1m})
    print(f"\n{'='*78}\nVWAP RE-TEST on ES RTH bars (sessions: {sess})")
    print(f"same-bar entry, signal-matched exits, friction {args.friction_pts}pt\n{'='*78}")
    for tf in (5, 30):
        out = run(tf, bars1m, args.friction_pts)
        print(f"\n#### {tf}-min")
        srs = [sharpe(v) for k, v in out.items() if not k.startswith('__') and v]
        var_sr = stats.pvariance(srs) if len(srs) > 1 else 0.0
        for k in sorted(out):
            if k.startswith("__"):
                continue
            st = stat_block(out[k], tf_min=tf, n_trials=8, var_sr=var_sr)
            if st.get("n"):
                print(f"  {k:<24} n={st['n']:>4} win%={st['win_rate']*100:>5.1f} "
                      f"E[R]={st['expectancy_r']:+.3f} DSR={st['dsr']:.3f}")
        for grp, lbl in (("__REV", "REVERSION (all)"), ("__MOM", "MOMENTUM (all)"), ("__ALL__", "ALL")):
            v = out.get(grp, [])
            if v:
                print(f"  {lbl:<24} n={len(v):>4} win%={sum(1 for r in v if r>0)/len(v)*100:>5.1f} E[R]={stats.fmean(v):+.3f}")


if __name__ == "__main__":
    main()
