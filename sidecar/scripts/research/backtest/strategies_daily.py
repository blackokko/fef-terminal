"""H8 (momentum) + H10 (mean-reversion) DAILY on real SPY (IBKR, 2011-07..), step-3
diagnostic: does the strategy beat BUY-AND-HOLD SPY on Sharpe, and what's the max
drawdown? DEV set only (chronological 80%). Realistic slippage; H10 stop modeled with
next-open gap fill. Reported BEFORE the full stack; STOP after.
"""
from __future__ import annotations

import os
import sqlite3

import numpy as np

DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
DEV_END = "2023-06-28"
SLIP = 0.0003   # per transition


def load_dev():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = c.execute("SELECT date,open,high,low,close FROM spy_daily WHERE date < ? ORDER BY date",
                     (DEV_END,)).fetchall()
    c.close()
    return rows


def perf(rets):
    a = np.asarray(rets, float)
    a = a[~np.isnan(a)]
    if len(a) < 2 or a.std() == 0:
        return None
    eq = np.cumprod(1 + a)
    dd = eq / np.maximum.accumulate(eq) - 1
    return {"sharpe": a.mean() / a.std() * np.sqrt(252),
            "cagr": eq[-1] ** (252 / len(a)) - 1,
            "tot": eq[-1] - 1, "maxdd": dd.min(), "tim": np.mean(a != 0)}


def show(tag, p, bh=None):
    if not p:
        print(f"  {tag:24s} (n/a)")
        return
    beat = ""
    if bh:
        beat = "  BEATS B&H Sharpe" if p["sharpe"] >= bh["sharpe"] else "  loses to B&H Sharpe"
    print(f"  {tag:24s} Sharpe={p['sharpe']:+.2f}  CAGR={100*p['cagr']:+5.1f}%  "
          f"maxDD={100*p['maxdd']:+5.1f}%  time-in-mkt={100*p['tim']:3.0f}%{beat}")


def main():
    rows = load_dev()
    close = np.array([r[4] for r in rows])
    op = np.array([r[1] for r in rows])
    n = len(close)
    print(f"SPY DAILY step-3 diagnostic — DEV {rows[0][0]}..{rows[-1][0]} ({n} bars)\n")

    ret = np.zeros(n)
    ret[1:] = close[1:] / close[:-1] - 1
    bh = perf(ret[1:])

    # H8 momentum: long when prior close > prior SMA50
    sma = np.full(n, np.nan)
    for i in range(49, n):
        sma[i] = close[i - 49:i + 1].mean()
    pos = np.zeros(n)
    for i in range(51, n):
        pos[i] = 1.0 if close[i - 1] > sma[i - 1] else 0.0
    trans = np.abs(np.diff(np.concatenate([[0.0], pos])))
    h8 = perf((pos * ret - trans * SLIP)[51:])

    # H10 mean-reversion: enter z<-1, exit z>=0 / 10d / -8% (gap next-open)
    mean20 = np.full(n, np.nan); std20 = np.full(n, np.nan)
    for i in range(19, n):
        w = close[i - 19:i + 1]
        mean20[i], std20[i] = w.mean(), w.std()
    z = np.where(std20 > 0, (close - mean20) / std20, 0.0)
    pos10 = np.zeros(n)
    fills = np.zeros(n)
    in_pos, entry_px, entry_i = False, 0.0, 0
    trades = []
    for i in range(20, n - 1):
        if not in_pos and z[i] < -1.0:
            in_pos, entry_px, entry_i = True, close[i] * (1 + SLIP), i
            fills[i] -= SLIP
            pos10[i + 1] = 1.0
            continue
        if in_pos:
            pos10[i + 1] = 1.0
            stop_hit = close[i] <= entry_px * 0.92
            if stop_hit or z[i] >= 0 or (i - entry_i) >= 10:
                exit_px = op[i + 1] * (1 - SLIP) if stop_hit else close[i] * (1 - SLIP)
                trades.append(exit_px / entry_px - 1)
                pos10[i + 1] = 0.0
                fills[i] -= SLIP
                in_pos = False
    h10 = perf((pos10 * ret + fills)[20:])

    print("--- BUY-AND-HOLD SPY (the bar to beat) ---")
    show("buy-and-hold", bh)
    print("\n--- H8 MOMENTUM (long > SMA50) ---")
    show("H8 momentum", h8, bh)
    print("\n--- H10 MEAN-REVERSION (z<-1, dip buyer) ---")
    show("H10 mean-rev", h10, bh)
    if trades:
        t = np.array(trades)
        print(f"  H10 per-trade: {len(t)} trades  win={100*(t>0).mean():.0f}%  "
              f"avg={100*t.mean():+.2f}%  5 worst={['%+.1f%%' % (100*x) for x in sorted(t)[:5]]}")


if __name__ == "__main__":
    main()
