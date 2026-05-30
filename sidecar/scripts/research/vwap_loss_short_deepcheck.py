"""Deeper controls on vwap_loss_short before any shadow promotion (Chat 2026-05-29):
  1. PER-SESSION CONCENTRATION (do first): if the 14 trades cluster in 2-3
     sessions, effective n << 14 and the DSR is overstated.
  2. TIME-OF-DAY clustering + time-matched random short control.
  3. INSTRUMENT CONSISTENCY: detect on ES, forward-walk on SPX (the instrument
     actually traded). If the edge vanishes on SPX it's an ES/SPX-basis phantom.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import statistics as stats
from collections import defaultdict

from fef_sidecar.engine.signals import dale, vwap
from fef_sidecar.engine.signals.dale import Bar
from scripts.research.dale_vwap_backtest import DB, et_date, load_bars, stat_block
from scripts.research.dale_vwap_backtest_v2 import atr_series, walk, ATR_STOP_MULT, ATR_TARGET_MULT
from scripts.research.vwap_rth_retest import load_es_rth

FRICTION = 1.5
TF = 5


def short_R(entry, atr, bars1m, ct, hz):
    if atr is None or atr <= 0:
        return None
    stop = entry + ATR_STOP_MULT * atr
    target = entry - ATR_TARGET_MULT * atr
    p = walk(entry, target, stop, False, bars1m, ct, hz)
    if p is None:
        return None
    rr = abs(target - entry) / abs(entry - stop)
    return p * rr + (1 - p) * (-1.0) - FRICTION / abs(entry - stop)


def et_minute(ts):
    d = dt.datetime.fromtimestamp(ts / 1000, dt.timezone(dt.timedelta(hours=-4)))
    return d.hour * 60 + d.minute, d.strftime("%H:%M")


def main():
    c = sqlite3.connect(DB)
    es1 = load_es_rth(c)
    es5 = dale.resample(es1, TF)
    es_atr = atr_series(es5, 14)
    es_imap = {b.ts_ms: i for i, b in enumerate(es5)}
    sigs = [s for s in vwap.detect(es5, timeframe_min=TF, hold_bars=1, reject_hold_bars=0)
            if s.kind == "vwap_loss_short"]

    # ES baseline trades w/ session + time
    trades = []
    for s in sigs:
        i = es_imap.get(s.ts_ms)
        a = es_atr[i] if i is not None else None
        r = short_R(s.price, a, es1, s.ts_ms + TF * 60_000, et_date(s.ts_ms))
        if r is None:
            continue
        mod, hhmm = et_minute(s.ts_ms)
        trades.append({"ts": s.ts_ms, "sd": et_date(s.ts_ms), "r": r, "mod": mod, "hhmm": hhmm})

    print(f"\n{'='*64}\nvwap_loss_short DEEP CHECK  (n={len(trades)})\n{'='*64}")

    # 1. per-session concentration
    bysess = defaultdict(list)
    for t in trades:
        bysess[t["sd"]].append(t["r"])
    print("1. PER-SESSION CONCENTRATION:")
    for sd in sorted(bysess):
        rs = bysess[sd]
        print(f"   {sd}: {len(rs)} trades, {sum(1 for r in rs if r>0)} wins, sumR={sum(rs):+.2f}")
    wins = [t for t in trades if t["r"] > 0]
    win_sess = defaultdict(int)
    for t in wins:
        win_sess[t["sd"]] += 1
    top2 = sum(sorted(win_sess.values(), reverse=True)[:2])
    print(f"   distinct sessions={len(bysess)}  wins={len(wins)} across {len(win_sess)} sessions  "
          f"top-2 sessions hold {top2}/{len(wins)} wins")
    print(f"   -> effective independent obs ~ {len(bysess)} sessions, NOT {len(trades)} trades")

    # 2. time-of-day
    print("\n2. TIME-OF-DAY (ET) of signals:")
    byhr = defaultdict(int)
    for t in trades:
        byhr[t["mod"] // 60] += 1
    for h in sorted(byhr):
        print(f"   {h:02d}:xx ET  {'#'*byhr[h]} ({byhr[h]})")
    # time-matched random short: short at each signal's time-of-day on EVERY session
    tmr = []
    for t in trades:
        for sd, g in [(sd, [b for b in es1 if et_date(b.ts_ms) == sd]) for sd in bysess]:
            bar = next((b for b in g if et_minute(b.ts_ms)[0] >= t["mod"]), None)
            if bar is None:
                continue
            i = es_imap.get(dale.resample(g, TF)[0].ts_ms) if g else None
            idx = next((k for k, b in enumerate(es5) if et_date(b.ts_ms) == sd and et_minute(b.ts_ms)[0] >= t["mod"]), None)
            a = es_atr[idx] if idx is not None else None
            r = short_R(bar.close, a, es1, bar.ts_ms + 60_000, sd)
            if r is not None:
                tmr.append(r)
    print(f"   time-matched random short (same times, all sessions): n={len(tmr)} E[R]={stats.fmean(tmr) if tmr else float('nan'):+.3f}")
    print(f"   signal E[R]={stats.fmean([t['r'] for t in trades]):+.3f}  -> {'beats time-matched' if stats.fmean([t['r'] for t in trades]) > (stats.fmean(tmr) if tmr else 9) else 'does NOT beat time-matched (time-of-day confound)'}")

    # 3. instrument consistency: forward-walk same signal times on SPX
    spx1 = load_bars(c, "SPX")
    spx5 = dale.resample(spx1, TF)
    spx_atr = atr_series(spx5, 14)
    spx5_idx = sorted(b.ts_ms for b in spx5)
    spx5_map = {b.ts_ms: i for i, b in enumerate(spx5)}
    spx1_sorted = sorted(spx1, key=lambda b: b.ts_ms)
    spx1_ts = [b.ts_ms for b in spx1_sorted]
    import bisect
    spx_R = []
    for t in trades:
        # SPX price at signal ts
        j = bisect.bisect_right(spx1_ts, t["ts"]) - 1
        if j < 0:
            continue
        entry = spx1_sorted[j].close
        # SPX 5min ATR at nearest 5min bar <= ts
        k = bisect.bisect_right(spx5_idx, t["ts"]) - 1
        a = spx_atr[k] if 0 <= k < len(spx_atr) else None
        r = short_R(entry, a, spx1_sorted, t["ts"] + 60_000, t["sd"])
        if r is not None:
            spx_R.append(r)
    print("\n3. INSTRUMENT CONSISTENCY (detect on ES, forward-walk on SPX):")
    if spx_R:
        st = stat_block(spx_R, tf_min=TF, n_trials=8, var_sr=0.05)
        print(f"   SPX-walk: n={st['n']} win%={st['win_rate']*100:.1f} E[R]={st['expectancy_r']:+.3f} DSR={st['dsr']:.3f}")
        print(f"   ES E[R]={stats.fmean([t['r'] for t in trades]):+.3f} vs SPX E[R]={stats.fmean(spx_R):+.3f}  "
              f"-> {'edge SURVIVES on SPX' if stats.fmean(spx_R) > 0.1 else 'edge WEAK/GONE on SPX (possible ES-SPX phantom)'}")


if __name__ == "__main__":
    main()
