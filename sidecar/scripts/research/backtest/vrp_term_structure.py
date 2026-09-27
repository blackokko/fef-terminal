"""PART 1 - DIRECT VARIANCE-RISK-PREMIUM MEASUREMENT ACROSS THE TERM STRUCTURE.

The cleanest test that the premium EXISTS (needs no long-DTE options): compare each
implied-vol tenor to the SUBSEQUENTLY-REALIZED SPX vol over the matching horizon.

  VIX    (30 calendar d ~ 21 trading d)  vs  forward 21-trading-day realized vol
  VIX9D  ( 9 calendar d ~  6 trading d)  vs  forward  6-trading-day realized vol
  VIX3M  (~93 calendar d ~ 63 trading d) vs  forward 63-trading-day realized vol

VRP_t = implied_t - realized_{t->t+h}  (annualized vol points, %).  A positive mean =
sellers are, on average, paid.  Reports mean / median / %(implied>realized) overall,
by YEAR, and by VIX-regime bucket, 2022-2026.  Contrast with 0DTE (~0, proven prior).

Realized vol = zero-mean (variance-swap) convention, annualized x sqrt(252).
Independent SPX 1-min ruler for realized; VIX-family daily closes for implied.

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.vrp_term_structure
CSV -> scratchpad/vrp_term_structure.csv
"""
from __future__ import annotations

import csv
import os
import sys

import numpy as np

from . import vrp_data as V

sys.stdout.reconfigure(encoding="utf-8")
SCRATCH = (r"D:\fef-data")

# (label, implied symbol, forward trading-day window, calendar-day tenor)
TENORS = [
    ("VIX9D  9d", "VIX9D", 6, 9),
    ("VIX   30d", "VIX", 21, 30),
    ("VIX3M 93d", "VIX3M", 63, 93),
]


def stats(vrp):
    a = np.array(vrp, float)
    a = a[np.isfinite(a)]
    if len(a) == 0:
        return None
    # Newey-West-lite: overlapping windows -> naive t is inflated; report a
    # cluster-robust-ish t using non-overlapping block std (block = window length h).
    return dict(n=len(a), mean=float(a.mean()), median=float(np.median(a)),
                pct_pos=100.0 * np.mean(a > 0), std=float(a.std(ddof=1)),
                p5=float(np.percentile(a, 5)), p95=float(np.percentile(a, 95)),
                worst=float(a.min()), best=float(a.max()))


def block_t(dates_vals, h):
    """t-stat of the mean VRP using NON-overlapping h-day blocks (kills the overlap-
    induced autocorrelation that would inflate a naive t on daily overlapping windows)."""
    dates_vals = sorted(dates_vals)
    vals = [v for _, v in dates_vals]
    blocks = [np.mean(vals[i:i + h]) for i in range(0, len(vals) - h + 1, h)]
    b = np.array(blocks, float)
    b = b[np.isfinite(b)]
    if len(b) < 2 or b.std(ddof=1) == 0:
        return float("nan"), len(b)
    return float(b.mean() / (b.std(ddof=1) / np.sqrt(len(b)))), len(b)


def main():
    con = V.connect()
    print("=" * 108)
    print("PART 1 — DIRECT VRP ACROSS THE TERM STRUCTURE  (implied - subsequently-realized SPX vol, %)")
    print("  implied = VIX-family daily close; realized = forward zero-mean annualized SPX vol; 2022-06/2026")
    print("=" * 108)

    daily, _ = V.load_spx_daily(con)
    print(f"  SPX daily closes: {len(daily)}  ({daily[0][0]}..{daily[-1][0]})")

    rows_csv = []
    per_tenor = {}
    for label, sym, h, cal in TENORS:
        implied = V.load_vix(con, sym)
        rvol = V.forward_rvol(daily, h)
        pairs = []   # (date, implied, realized, vrp)
        for d in sorted(implied):
            if d in rvol:
                iv = implied[d]
                rv = rvol[d]
                pairs.append((d, iv, rv, iv - rv))
        per_tenor[label] = (h, pairs)
        for d, iv, rv, vp in pairs:
            rows_csv.append(dict(tenor=label, date=d, split=V.split_of(d), year=d[:4],
                                 implied=iv, realized=rv, vrp=vp))
        print(f"  {label}: {len(pairs)} paired obs (implied {sym} vs fwd {h}td realized ~ {cal}cal-d)")

    # ---------- headline: overall + by split ----------
    print("\n" + "=" * 108)
    print("OVERALL VRP by tenor  (mean = avg vol-points sellers are paid; %pos = windows implied>realized)")
    print(f"  {'tenor':10s} {'split':5s} {'n':>5s} {'meanVRP':>8s} {'medVRP':>7s} {'%pos':>5s} "
          f"{'blk-t':>6s} {'nBlk':>5s} {'mImpl':>6s} {'mReal':>6s} {'p5':>7s} {'worst':>7s}")
    for label, sym, h, cal in TENORS:
        h, pairs = per_tenor[label]
        for split in ("ALL", "DEV", "HOLD"):
            sub = [(d, iv, rv, vp) for (d, iv, rv, vp) in pairs
                   if split == "ALL" or V.split_of(d) == split]
            s = stats([vp for _, _, _, vp in sub])
            if not s:
                continue
            bt, nb = block_t([(d, vp) for d, _, _, vp in sub], h)
            mi = np.mean([iv for _, iv, _, _ in sub]); mr = np.mean([rv for _, _, rv, _ in sub])
            print(f"  {label:10s} {split:5s} {s['n']:5d} {s['mean']:8.2f} {s['median']:7.2f} "
                  f"{s['pct_pos']:4.0f}% {bt:6.2f} {nb:5d} {mi:6.1f} {mr:6.1f} {s['p5']:7.1f} {s['worst']:7.1f}")
        print()

    # ---------- by year ----------
    print("=" * 108)
    print("VRP by YEAR  (2022 = bear/high-vol tail stress; mean vol-points)")
    years = ["2022", "2023", "2024", "2025", "2026"]
    print(f"  {'tenor':10s} " + " ".join(f"{y:>13s}" for y in years))
    for label, sym, h, cal in TENORS:
        h, pairs = per_tenor[label]
        cells = []
        for y in years:
            sub = [vp for (d, iv, rv, vp) in pairs if d[:4] == y]
            s = stats(sub)
            cells.append(f"{s['mean']:+5.1f}/{s['pct_pos']:2.0f}%" if s else f"{'·':>9s}")
        print(f"  {label:10s} " + " ".join(f"{c:>13s}" for c in cells))
    print("  (cell = mean VRP / % windows implied>realized)")

    # ---------- by VIX-regime bucket ----------
    print("\n" + "=" * 108)
    print("VRP by VIX-REGIME at entry  (does the premium survive in high-vol regimes?)")
    vix_daily = V.load_vix(con, "VIX")
    buckets = [("calm  VIX<15", lambda v: v < 15), ("norm 15-20", lambda v: 15 <= v < 20),
               ("elev 20-30", lambda v: 20 <= v < 30), ("stress VIX>=30", lambda v: v >= 30)]
    print(f"  {'tenor':10s} " + " ".join(f"{b[0]:>15s}" for b in buckets))
    for label, sym, h, cal in TENORS:
        h, pairs = per_tenor[label]
        cells = []
        for bname, bfn in buckets:
            sub = [vp for (d, iv, rv, vp) in pairs if d in vix_daily and bfn(vix_daily[d])]
            s = stats(sub)
            cells.append(f"{s['mean']:+5.1f}/{s['pct_pos']:2.0f}%/{s['n']:>3d}" if s else f"{'·':>13s}")
        print(f"  {label:10s} " + " ".join(f"{c:>15s}" for c in cells))
    print("  (cell = mean VRP / %pos / n days)")

    # ---------- CSV ----------
    path = os.path.join(SCRATCH, "vrp_term_structure.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["tenor", "date", "split", "year", "implied", "realized", "vrp"])
        w.writeheader()
        for r in rows_csv:
            w.writerow({**r, "implied": f"{r['implied']:.3f}", "realized": f"{r['realized']:.3f}",
                        "vrp": f"{r['vrp']:.3f}"})
    print(f"\n  CSV -> {path}  ({len(rows_csv)} rows)")
    print("\nREAD: positive mean VRP with %pos>>50 and |blk-t|>2 = a real, persistent premium at that tenor.")
    print("DONE.")


if __name__ == "__main__":
    main()
