"""OPTION-DOLLAR translation diagnostic for vwap_loss_short (ChatGPT #1 priority).

We validate the signal in SPX points, but trade a 0DTE SPX PUT. A real underlying
edge can be economically untradeable (small/slow move + theta + spread + low
delta). This converts each in-sample signal's SPX path into the actual put's P&L
via Black-Scholes, picking a delta<=0.15 put (the live picker rule), pricing at
entry and exit, applying a round-trip spread haircut.

IMPORTANT: this is a MODEL ESTIMATE, not executable truth. It assumes a single
representative IV (no skew/term surface), flat IV over the hold, and a modeled
spread. The HEADLINE economic validation still requires real IBKR bid/ask quotes
(pending TWS). But it gives the first directional read on whether the nonlinearity
destroys the edge. Reported both mid-to-mid (optimistic) and spread-haircut.

Option underlying = SPX (instrument actually traded); detection times from ES.
"""
from __future__ import annotations

import bisect
import math
import sqlite3
import statistics as stats

from fef_sidecar.engine.signals import dale, vwap
from fef_sidecar.engine.signals.dale import Bar
from fef_sidecar.engine.signals.shadow_vwap_loss_short import (
    TF, ATR_TARGET_MULT, MIN_STOP_PTS, ATR_STOP_MULT, _atr, _et_date,
)
from scripts.research.dale_vwap_backtest import DB, load_bars

REP_IV = 0.16          # representative 0DTE SPX IV (median observed put IV 5/27-29)
SKEW_BUMP = 0.03       # OTM puts trade at higher IV than ATM (rough skew add)
SPREAD_HALF = 0.06     # half of round-trip spread as fraction of mid (0DTE OTM ~12% rt)
DELTA_MAX = 0.15
MIN_MID = 0.70
EXPIRY_UTC_H = 20      # 16:00 ET = 20:00 UTC 0DTE settlement


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bsm_put(S, K, T, sigma):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * _phi(-d2) - S * _phi(-d1)   # r=0


def put_delta(S, K, T, sigma):
    if T <= 0 or sigma <= 0:
        return -1.0 if S < K else 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return _phi(d1) - 1.0


def T_years(ts_ms):
    import datetime as dt
    d = dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC)
    exp = d.replace(hour=EXPIRY_UTC_H, minute=0, second=0, microsecond=0)
    secs = max((exp - d).total_seconds(), 60)
    return secs / (365 * 24 * 3600)


def pick_put_strike(S, T, sigma, target_delta=0.12):
    """5pt strike whose put delta is closest to -target_delta (the strike-bucket
    experiment: does a higher-delta / nearer-money put translate better?)."""
    best = None
    K = int(S // 5) * 5 + 25
    for _ in range(80):
        if K <= 0:
            break
        d = put_delta(S, K, T, sigma)
        err = abs(abs(d) - target_delta)
        if best is None or err < best[0]:
            best = (err, K, bsm_put(S, K, T, sigma), d)
        K -= 5
    _, K, mid, d = best
    return K, mid, d


def main():
    c = sqlite3.connect(DB)
    # detection times from ES; underlying path from SPX
    es1 = [Bar(*r) for r in c.execute(
        "SELECT timestamp_ms,open,high,low,close,volume FROM historical_bar "
        "WHERE symbol='ES' AND bar_size='1 min RTH' ORDER BY timestamp_ms")]
    es5 = dale.resample(es1, TF)
    spx1 = load_bars(c, "SPX")
    spx_ts = [b.ts_ms for b in spx1]
    spx5 = dale.resample(spx1, TF)
    spx_atr = _atr(spx5)
    spx5_ts = [b.ts_ms for b in spx5]
    sigs = sorted((s for s in vwap.detect(es5, timeframe_min=TF, hold_bars=1, reject_hold_bars=0)
                   if s.kind == "vwap_loss_short"), key=lambda s: s.ts_ms)

    sigma = REP_IV + SKEW_BUMP
    rows = []
    busy = 0
    for s in sigs:
        close_t = s.ts_ms + TF * 60_000
        ei = bisect.bisect_left(spx_ts, close_t)
        if ei >= len(spx1) or spx1[ei].ts_ms < busy:
            continue
        S0 = spx1[ei].open
        entry_ts = spx1[ei].ts_ms
        ki = bisect.bisect_left(spx5_ts, s.ts_ms)
        atr = spx_atr[ki] if ki < len(spx_atr) else None
        if not atr:
            continue
        risk = max(ATR_STOP_MULT * atr, MIN_STOP_PTS)
        stop = S0 + risk
        target = S0 - ATR_TARGET_MULT * atr
        horizon = _et_date(s.ts_ms)
        # walk SPX adverse-first to get exit S + ts
        exit_S, exit_ts = None, None
        for k in range(ei, len(spx1)):
            b = spx1[k]
            if _et_date(b.ts_ms) > horizon:
                exit_S, exit_ts = b.close, b.ts_ms; break
            if b.high >= stop:
                exit_S, exit_ts = stop, b.ts_ms; break
            if b.low <= target:
                exit_S, exit_ts = target, b.ts_ms; break
            exit_S, exit_ts = b.close, b.ts_ms
        busy = exit_ts
        spx_r = (S0 - exit_S) / risk     # short R in SPX points
        rows.append((_et_date(s.ts_ms), spx_r, S0, exit_S, T_years(entry_ts), T_years(exit_ts)))

    print(f"\n{'='*78}\nOPTION-DOLLAR STRIKE-SELECTION EXPERIMENT (BSM est., NOT executable truth)")
    print(f"IV~{sigma:.2f}, spread {SPREAD_HALF*200:.0f}% rt, commission $1.30. Underlying=SPX.")
    print(f"n={len(rows)} signals, SPX-point win%={sum(1 for x in rows if x[1]>0)/len(rows)*100:.0f} "
          f"(mean SPX_R {stats.fmean(x[1] for x in rows):+.2f})\n{'='*78}")
    print(f"  {'delta bucket':<16}{'avg|d|':>8}{'avg$entry':>11}{'opt win%':>10}{'mid $/ct':>10}{'net $/ct':>10}")
    for label, td in (("0.10-0.15 (live)", 0.12), ("0.20-0.25", 0.22),
                      ("0.30-0.40", 0.35), ("~ATM 0.45-0.50", 0.48)):
        nets, mids, ds, pes = [], [], [], []
        for sd, r, S0, exitS, T0, T1 in rows:
            K, p_entry, dlt = pick_put_strike(S0, T0, sigma, target_delta=td)
            if p_entry <= 0:
                continue
            p_exit = bsm_put(exitS, K, T1, sigma)
            mids.append((p_exit - p_entry) * 100)
            fill_in = p_entry * (1 + SPREAD_HALF)
            fill_out = max(p_exit * (1 - SPREAD_HALF), 0.0)
            nets.append((fill_out - fill_in) * 100 - 1.30)
            ds.append(abs(dlt)); pes.append(p_entry)
        win = sum(1 for x in nets if x > 0) / len(nets) * 100
        print(f"  {label:<16}{stats.fmean(ds):>8.2f}{stats.fmean(pes):>10.2f}{win:>9.0f}%"
              f"{stats.fmean(mids):>10.0f}{stats.fmean(nets):>10.0f}")
    print("\n  Reads: does ANY strike bucket make the SPX-point edge economically positive in")
    print("  0DTE option-$? CAVEAT: single-IV BSM + modeled spread; real IBKR bid/ask pending TWS.")


if __name__ == "__main__":
    main()
