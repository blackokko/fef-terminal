"""8/21 EMA CROSSOVER backtest on SPX 1-min (2022-2026, IBKR ruler) — the user's actual style.

Momentum, not mean-reversion: does an 8/21 EMA cross precede a tradeable DIRECTIONAL move? Tests the
raw cross + two rescues (daily-trend alignment, options-flow confluence), because raw index EMA
crosses usually whipsaw. dir = +1 bull cross (8 over 21) / -1 bear cross. Forward move on SPX at
+15/+30/+60 min; fav = dir*move; report hit%, meanFav, %big8(fav>+8) BY YEAR per variant.

  RAW        - every 8/21 cross on 5-min candles, in ET 09:45-15:00
  TREND      - only crosses aligned with the DAILY 8/21 trend (streaming, no lookahead)
  FLOW       - only crosses where the 0DTE aggressor flow (sign of 5-roll net_signed_prem) agrees
  TREND+FLOW - both filters

Read-only. Run:  .venv/Scripts/python.exe -m scripts.research.backtest.ema_cross_hunt [START END]
"""
from __future__ import annotations

import sys

import numpy as np

from .data import connect
from .ibkr_ruler import ibkr_open, ibkr_series
from .thetadata_real_spot_validate import MOVE_GLITCH_FRAC, WIN_HI, WIN_LO, _et_h, _roll, real_at

sys.stdout.reconfigure(encoding="utf-8")
HORIZONS = [15, 30, 60]
CANDLE_MIN = 5
FAST, SLOW = 8, 21
BIG8 = 8.0


def ema(x, span):
    a = 2.0 / (span + 1)
    out = np.empty(len(x))
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def resample(rt, rp, minutes):
    """(bucket_ts[], last_close[]) at `minutes` resolution."""
    step = minutes * 60_000
    buck = {}
    for t, p in zip(rt, rp):
        b = (int(t) // step) * step
        buck[b] = (int(t), float(p))       # last wins (rt sorted)
    ks = sorted(buck)
    return np.array(ks, np.int64), np.array([buck[k][1] for k in ks], float), np.array([buck[k][0] for k in ks], np.int64)


def crosses(bc, actual_ts):
    """indices+dir where FAST EMA crosses SLOW EMA. Returns [(ts_ms, dir)]."""
    if len(bc) < SLOW + 2:
        return []
    ef, es = ema(bc, FAST), ema(bc, SLOW)
    diff = ef - es
    out = []
    for i in range(1, len(diff)):
        if diff[i - 1] <= 0 < diff[i]:
            out.append((int(actual_ts[i]), 1.0))
        elif diff[i - 1] >= 0 > diff[i]:
            out.append((int(actual_ts[i]), -1.0))
    return out


def flow_dir_fn(con, d):
    rows = con.execute("SELECT ts_ms, net_signed_prem FROM option_flow_minute "
                       "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
    if len(rows) < 20:
        return None
    ts = np.array([r[0] for r in rows], np.int64)
    roll = _roll(np.array([r[1] for r in rows], float))
    def at(t):
        j = int(np.searchsorted(ts, t)) - 1
        if 0 <= j < len(roll) and not np.isnan(roll[j]):
            return 1.0 if roll[j] > 0 else -1.0
        return 0.0
    return at


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 100)
    print(f"{FAST}/{SLOW} EMA CROSSOVER on {CANDLE_MIN}-min SPX candles — momentum, IBKR ruler 2022-2026")
    print(f"  {start}..{end} ({len(dates)} sess); variants: RAW / TREND / FLOW / TREND+FLOW; big8=fav>+{BIG8}pt")
    print("=" * 100)

    # streaming daily 8/21 trend (no lookahead: trend known from PRIOR sessions only)
    recs = {}     # (variant, horizon, year) -> list fav
    dema_f = dema_s = None
    for k, d in enumerate(dates):
        rs = ibkr_series(ic, d)
        if rs is None:
            continue
        rt, rp = rs
        day_close = float(rp[-1])
        daily_trend = (1.0 if (dema_f is not None and dema_f > dema_s) else
                       -1.0 if (dema_f is not None and dema_f < dema_s) else 0.0)
        # update daily EMAs with today's close AFTER deciding trend
        if dema_f is None:
            dema_f = dema_s = day_close
        else:
            dema_f = (2 / (FAST + 1)) * day_close + (1 - 2 / (FAST + 1)) * dema_f
            dema_s = (2 / (SLOW + 1)) * day_close + (1 - 2 / (SLOW + 1)) * dema_s

        bk, bc, bts = resample(rt, rp, CANDLE_MIN)
        fdir = flow_dir_fn(con, d)
        year = d[:4]
        for cts, cdir in crosses(bc, bts):
            h = _et_h(cts)
            if h < WIN_LO or h > WIN_HI:
                continue
            fl = fdir(cts) if fdir else 0.0
            variants = ["RAW"]
            if daily_trend == cdir:
                variants.append("TREND")
            if fl == cdir:
                variants.append("FLOW")
            if daily_trend == cdir and fl == cdir:
                variants.append("TREND+FLOW")
            s0 = real_at(rt, rp, cts)
            if s0 is None:
                continue
            for hm in HORIZONS:
                sH = real_at(rt, rp, cts + hm * 60_000)
                if sH is None:
                    continue
                mv = sH - s0
                if abs(mv) > MOVE_GLITCH_FRAC * s0:
                    continue
                fav = cdir * mv
                for v in variants:
                    recs.setdefault((v, hm, year), []).append(fav)
                    recs.setdefault((v, hm, "ALL"), []).append(fav)
        if (k + 1) % 200 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d})", flush=True)

    YEARS = ["2022", "2023", "2024", "2025", "2026", "ALL"]
    for v in ("RAW", "TREND", "FLOW", "TREND+FLOW"):
        print("\n" + "=" * 100)
        print(f"VARIANT {v}")
        for hm in HORIZONS:
            print(f"  +{hm}min:")
            print(f"    {'year':5s} {'#cross':>7s} {'hit%':>7s} {'meanFav':>8s} {'t':>6s} {'%big8':>7s}")
            for yr in YEARS:
                a = np.array(recs.get((v, hm, yr), []), float)
                if len(a) < 20:
                    continue
                hit = 100.0 * np.mean(a > 0)
                m = float(a.mean())
                t = m / (a.std(ddof=1) / np.sqrt(len(a))) if a.std(ddof=1) > 0 else float("nan")
                print(f"    {yr:5s} {len(a):7d} {hit:6.1f}% {m:+8.3f} {t:+6.2f} {100.0*np.mean(a>BIG8):6.1f}%")
    print("\n" + "=" * 100)
    print("READ: hit>>50 + fav>0 + high %big8 = the cross predicts a tradeable directional move.")
    print("  RAW ~50/whipsaw is expected; TREND / FLOW / TREND+FLOW lifting it = the filter is the edge.")
    print("=" * 100)


if __name__ == "__main__":
    main()
