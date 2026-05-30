"""Stress-test vwap_loss_short under ChatGPT-5.5's realism critiques, BEFORE any
forward data accrues (costless re-baseline):
  (1) entry latency — same-bar-close is optimistic; test next-1m-open / +60s / +300s
  (2) one-trade-at-a-time — suppress overlapping signals while a position is open
  (3) adverse-first on ambiguous bars (both target & stop in one bar => STOP)
Also report the ATR-stop distribution (friction-vs-compression risk).
If the +0.62 edge survives realism, it's stronger; if it collapses, it was optimism.
"""
from __future__ import annotations

import bisect
import sqlite3
import statistics as stats
from collections import defaultdict

from fef_sidecar.engine.signals import dale, vwap
from fef_sidecar.engine.signals.dale import Bar
from fef_sidecar.engine.signals.shadow_vwap_loss_short import (
    TF, ATR_STOP_MULT, ATR_TARGET_MULT, FRICTION_PTS, _atr, _et_date,
)
from scripts.research.dale_vwap_backtest import DB
from scripts.research.vwap_rth_retest import load_es_rth


def walk_adverse_first(entry, target, stop, bars1m, ts, start_ts, horizon):
    """Short trade (target<entry<stop). Returns (r_in_risk_units, exit_ms).
    Adverse-first on ambiguous bars; timeout = mark-to-last-close."""
    i = bisect.bisect_left(ts, start_ts)
    risk = stop - entry
    last = None
    for k in range(i, len(bars1m)):
        b = bars1m[k]
        if _et_date(b.ts_ms) > horizon:
            break
        last = b
        hit_t = b.low <= target
        hit_s = b.high >= stop
        if hit_s:                      # adverse-first: stop wins ties
            return -1.0, b.ts_ms
        if hit_t:
            return (entry - target) / risk, b.ts_ms
    if last is None:
        return None, None
    return (entry - last.close) / risk, last.ts_ms


def entry_at(bars1m, ts, after_ms):
    i = bisect.bisect_left(ts, after_ms)
    return (bars1m[i].open, bars1m[i].ts_ms) if i < len(bars1m) else (None, None)


def main():
    c = sqlite3.connect(DB)
    es1 = load_es_rth(c)
    ts1 = [b.ts_ms for b in es1]
    es5 = dale.resample(es1, TF)
    atr = _atr(es5)
    imap = {b.ts_ms: i for i, b in enumerate(es5)}
    sigs = [s for s in vwap.detect(es5, timeframe_min=TF, hold_bars=1, reject_hold_bars=0)
            if s.kind == "vwap_loss_short"]

    variants = {"close(optimistic)": -1, "next-1m-open": 0, "+60s": 60_000, "+300s": 300_000}
    # one-trade-at-a-time uses the realistic (next-1m-open) entry/exit for the busy gate
    results = {v: [] for v in variants}
    atr_pts = []
    busy_until = 0
    taken = 0
    for s in sorted(sigs, key=lambda x: x.ts_ms):
        i = imap.get(s.ts_ms)
        a = atr[i] if i is not None else None
        if not a or a <= 0:
            continue
        close_time = s.ts_ms + TF * 60_000
        # realistic entry time for the busy gate
        e0_px, e0_ts = entry_at(es1, ts1, close_time)
        if e0_ts is None or e0_ts < busy_until:   # one-trade-at-a-time
            continue
        atr_pts.append(a)
        horizon = _et_date(s.ts_ms)
        # score each entry-latency variant
        for v, off in variants.items():
            if off < 0:
                entry, ets = s.price, close_time          # optimistic: the 5m close
            else:
                entry, ets = entry_at(es1, ts1, close_time + off)
            if entry is None:
                continue
            stop = entry + ATR_STOP_MULT * a
            target = entry - ATR_TARGET_MULT * a
            r, exit_ms = walk_adverse_first(entry, target, stop, es1, ts1, ets, horizon)
            if r is None:
                continue
            r -= FRICTION_PTS / (ATR_STOP_MULT * a)
            results[v].append((s.ts_ms, r))
            if v == "next-1m-open" and exit_ms:
                busy_until = exit_ms
        taken += 1

    print(f"\n{'='*70}\nvwap_loss_short REALISM RE-BASELINE (one-trade-at-a-time, adverse-first)")
    print(f"signals detected={len(sigs)}  taken after overlap-suppression={taken}\n{'='*70}")
    print(f"  ATR(5m) stop distance pts: min={min(atr_pts):.1f} med={stats.median(atr_pts):.1f} "
          f"max={max(atr_pts):.1f}  (friction {FRICTION_PTS}pt = {FRICTION_PTS/stats.median(atr_pts):.2f}R at median)")
    print(f"  {'entry':<18}{'n':>4}{'win%':>7}{'E[R]':>9}{'sessions':>10}")
    for v in variants:
        rs = [r for _, r in results[v]]
        if not rs:
            print(f"  {v:<18} (none)"); continue
        sess = len({_et_date(t) for t, _ in results[v]})
        print(f"  {v:<18}{len(rs):>4}{sum(1 for r in rs if r>0)/len(rs)*100:>6.1f}{stats.fmean(rs):>9.3f}{sess:>10}")
    print("\n  prior (optimistic same-bar, overlap-allowed, t1-prob ambiguous): n=14 win71.4% E[R]+0.620")


if __name__ == "__main__":
    main()
