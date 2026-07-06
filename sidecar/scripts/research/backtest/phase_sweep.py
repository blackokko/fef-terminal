"""Threshold sweep for the PhaseDetector (READ-ONLY research; engine code untouched).

Sweeps the key thresholds and reports, per config, signal FREQUENCY vs QUALITY (hit% /
mean fav pt / %big / session-clustered t) so the looser=more-signals-lower-quality
trade-off is explicit, then recommends the config that maximizes quality while keeping
frequency sane. Uses the same reconstruction + IBKR ruler as phase_backtest.

Grid (others fixed at engine defaults): vel_ignite {8,12,16} · min_move {8,12,18} ·
bounce_pt {3,5,8} · fade_min {3,4,5} · ride_min {3,4}  = 162 configs.

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.phase_sweep
"""
from __future__ import annotations

import itertools
import os
import pickle
import sys
import time

import numpy as np

from .data import connect
from .ibkr_ruler import ibkr_open
from .phase_backtest import DEF_END, DEF_START, aggregate, build_all, eval_config
from .phase_pillars import dev_sessions

sys.stdout.reconfigure(encoding="utf-8")
CACHE_PKL = os.path.join(os.environ.get("TEMP", "."), "phase_cache.pkl")

VEL_IGNITE = [8.0, 12.0, 16.0]
MIN_MOVE = [8.0, 12.0, 18.0]
BOUNCE_PT = [3.0, 5.0, 8.0]
FADE_MIN = [3, 4, 5]
RIDE_MIN = [3, 4]
FIXED = dict(window_sec=300, vel_sec=75, vel_stall=3.0, iv_delta=0.006, mp_overshoot=8.0)


def load_cache():
    if os.path.exists(CACHE_PKL):
        try:
            cache = pickle.load(open(CACHE_PKL, "rb"))
            print(f"  loaded cache: {len(cache)} sessions from {CACHE_PKL}")
            return cache
        except Exception:
            pass
    con, ic = connect(), ibkr_open()
    dates = dev_sessions(con, DEF_START, DEF_END)
    print(f"  building cache for {len(dates)} sessions ...", flush=True)
    cache = build_all(con, ic, dates)
    pickle.dump(cache, open(CACHE_PKL, "wb"))
    return cache


def summarize(records, counts):
    ns = np.array([c["n_sig"] for c in counts], float)
    tot = int(ns.sum())
    n_ride = sum(c["n_ride"] for c in counts)
    n_fade = sum(c["n_fade"] for c in counts)
    d = dict(sig_sess=ns.mean(), tot=tot, n_ride=n_ride, n_fade=n_fade,
             pct_fade=(100.0 * n_fade / tot) if tot else float("nan"))
    for hmin in (10, 30):
        ov = aggregate(records, hmin, lambda r: True)
        fa = aggregate(records, hmin, lambda r: r["state"] == "FADE")
        ri = aggregate(records, hmin, lambda r: r["state"] == "RIDE")
        d[f"ov{hmin}"] = ov
        d[f"fa{hmin}"] = fa
        d[f"ri{hmin}"] = ri
    return d


def main():
    print("=" * 118)
    print("PHASE DETECTOR THRESHOLD SWEEP — IBKR ruler, 1-min reconstructed pillars, DEV 2022..2025-08")
    print("=" * 118)
    cache = load_cache()
    grid = list(itertools.product(VEL_IGNITE, MIN_MOVE, BOUNCE_PT, FADE_MIN, RIDE_MIN))
    print(f"  {len(grid)} configs; {len(cache)} sessions each\n", flush=True)

    rows = []
    t0 = time.time()
    for i, (vi, mm, bp, fm, rm) in enumerate(grid):
        cfg = dict(FIXED, vel_ignite=vi, min_move=mm, bounce_pt=bp, fade_min=fm, ride_min=rm)
        recs, cnts = eval_config(cache, cfg)
        s = summarize(recs, cnts)
        rows.append(dict(vi=vi, mm=mm, bp=bp, fm=fm, rm=rm, **s))
        if (i + 1) % 20 == 0:
            print(f"  ...{i+1}/{len(grid)} ({time.time()-t0:.0f}s)", flush=True)

    # ---- CSV dump (full grid) ------------------------------------------------
    csv = os.path.join(os.environ.get("TEMP", "."), "phase_sweep.csv")
    with open(csv, "w") as f:
        f.write("vi,mm,bp,fm,rm,sig_sess,pct_fade,n_ride,"
                "ov30_n,ov30_hit,ov30_fav,ov30_big,ov30_t,"
                "fa30_hit,fa30_fav,fa30_big,fa30_t,ri30_n,ri30_hit,ri30_fav,ri30_big,"
                "ov10_hit,ov10_fav,ov10_t\n")
        for r in rows:
            ov, fa, ri, o1 = r["ov30"], r["fa30"], r["ri30"], r["ov10"]
            f.write(f"{r['vi']},{r['mm']},{r['bp']},{r['fm']},{r['rm']},{r['sig_sess']:.2f},"
                    f"{r['pct_fade']:.0f},{r['n_ride']},"
                    f"{ov['n']},{ov['hit']:.1f},{ov['fav']:.2f},{ov['big']:.0f},{ov['tcl']:.2f},"
                    f"{fa['hit']:.1f},{fa['fav']:.2f},{fa['big']:.0f},{fa['tcl']:.2f},"
                    f"{ri['n'] if ri else 0},{ri['hit'] if ri else float('nan'):.1f},"
                    f"{ri['fav'] if ri else float('nan'):.2f},{ri['big'] if ri else float('nan'):.0f},"
                    f"{o1['hit']:.1f},{o1['fav']:.2f},{o1['tcl']:.2f}\n")
    print(f"\n  full grid -> {csv}\n")

    # ---- helpers to print a table -------------------------------------------
    def line(r):
        ov, fa, ri = r["ov30"], r["fa30"], r["ri30"]
        rin = ri["n"] if ri else 0
        rihit = f"{ri['hit']:4.0f}" if ri and ri["n"] >= 20 else "  — "
        rifav = f"{ri['fav']:+5.1f}" if ri and ri["n"] >= 20 else "   — "
        return (f"  vi{r['vi']:>4.0f} mm{r['mm']:>4.0f} bp{r['bp']:>3.0f} fm{r['fm']} rm{r['rm']} | "
                f"{r['sig_sess']:5.2f}/s {r['pct_fade']:3.0f}%F | "
                f"ov {ov['hit']:4.1f}% {ov['fav']:+5.2f} {ov['big']:2.0f}%b t{ov['tcl']:+4.1f} | "
                f"FADE {fa['hit']:4.1f}% {fa['fav']:+5.2f} | RIDE n{rin:<4d} {rihit}% {rifav}")

    hdr = ("  config                    | freq        | +30 OVERALL              | "
           "+30 FADE       | +30 RIDE")

    # ---- ranking: best QUALITY at SANE frequency ----------------------------
    # sane = sig/session <= 6 (not firing every bar). Score = fav30 + 0.05*(hit30-50),
    # among configs with enough signals to be meaningful.
    def score(r):
        ov = r["ov30"]
        return ov["fav"] + 0.05 * (ov["hit"] - 50.0)
    eligible = [r for r in rows if r["sig_sess"] <= 6.0 and r["ov30"]["n"] >= 300]
    eligible.sort(key=score, reverse=True)

    print("=" * 118)
    print("TOP 20 CONFIGS BY QUALITY AT SANE FREQUENCY (sig/session <= 6, n>=300) — sorted by fav30+hit")
    print("=" * 118)
    print(hdr)
    for r in eligible[:20]:
        print(line(r))

    print("\n" + "=" * 118)
    print("FULL GRID — TOP 15 BY +30 OVERALL fav (any frequency)")
    print("=" * 118)
    print(hdr)
    for r in sorted(rows, key=lambda r: r["ov30"]["fav"], reverse=True)[:15]:
        print(line(r))

    print("\n" + "=" * 118)
    print("BEST FADE-ONLY QUALITY (fade is 97% of signals) — TOP 12 BY +30 FADE fav, n>=300")
    print("=" * 118)
    print(hdr)
    for r in sorted([r for r in rows if r["fa30"] and r["fa30"]["n"] >= 300],
                    key=lambda r: r["fa30"]["fav"], reverse=True)[:12]:
        print(line(r))

    # ---- frequency vs quality trade-off (binned over ALL 162) ---------------
    print("\n" + "=" * 118)
    print("FREQUENCY -> QUALITY TRADE-OFF (mean over all configs in each sig/session bin)")
    print("=" * 118)
    print(f"  {'sig/session bin':16s} {'#cfg':>4s} {'avg sig/s':>9s} {'ov hit%':>8s} "
          f"{'ov favPt':>8s} {'ov %big':>7s} {'fade hit%':>9s} {'fade favPt':>10s}")
    bins = [("<=1", 0, 1), ("1-3", 1, 3), ("3-6", 3, 6), ("6-12", 6, 12), (">12", 12, 1e9)]
    for lbl, lo, hi in bins:
        sub = [r for r in rows if lo < r["sig_sess"] <= hi] if lo > 0 else [r for r in rows if r["sig_sess"] <= hi]
        if not sub:
            continue
        oh = np.mean([r["ov30"]["hit"] for r in sub])
        of = np.mean([r["ov30"]["fav"] for r in sub])
        ob = np.mean([r["ov30"]["big"] for r in sub])
        fh = np.mean([r["fa30"]["hit"] for r in sub if r["fa30"]])
        ff = np.mean([r["fa30"]["fav"] for r in sub if r["fa30"]])
        print(f"  {lbl:16s} {len(sub):4d} {np.mean([r['sig_sess'] for r in sub]):9.2f} "
              f"{oh:7.1f}% {of:+8.2f} {ob:6.1f}% {fh:8.1f}% {ff:+10.2f}")

    print("\n" + "=" * 118)
    if eligible:
        b = eligible[0]
        print(f"RECOMMENDED (best quality at sane freq): vel_ignite={b['vi']:.0f} min_move={b['mm']:.0f} "
              f"bounce_pt={b['bp']:.0f} fade_min={b['fm']} ride_min={b['rm']}")
        print(f"  -> {b['sig_sess']:.2f} sig/session, +30 overall {b['ov30']['hit']:.1f}% hit "
              f"{b['ov30']['fav']:+.2f}pt fav t{b['ov30']['tcl']:+.1f}")
    print("=" * 118)


if __name__ == "__main__":
    main()
