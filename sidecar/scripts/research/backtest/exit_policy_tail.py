"""Tail / giveback / capture-efficiency companion to exit_policy_sweep (READ-ONLY research).

Adds the pieces the mean/median table hides:
  * ORACLE ceiling  : mean of the max-favorable point reachable on each path (perfect exit) and
                      each policy's capture efficiency (mean / oracle) -> how much is left on the table.
  * both TAILS      : right tail (P90, %>20, %>40) AND left tail (P10, %<-20) -> ride policies fatten
                      the giveback too, which is why the net mean doesn't move.
  * quantile ladder : full captured-point quantiles for the key policies.
  * example trades  : the 12 biggest red->green (oracle) days, showing what +30 / hold / EMA20 / trail
                      actually captured on each -> concretizes "the tail is real but unconditional
                      riding gives it back on the next chop day".

Run: .venv/Scripts/python.exe -m scripts.research.backtest.exit_policy_tail [--preset big_move|balanced]
"""
from __future__ import annotations

import argparse
import datetime as dt
import pickle
from zoneinfo import ZoneInfo

import numpy as np

from .exit_policy_sweep import (CACHE_PKL, PRESETS, collect_entries, ex_fixed,
                                ex_target_stop, ex_trail, ex_trend_break, _clustered_t)

ET = ZoneInfo("America/New_York")


def oracle(e):
    """Best favorable point reachable on the forward path (perfect-exit ceiling)."""
    return float(np.max(e["fav"]))


def ex_scaleout(e, w1, w2, K1, K2, J, span):
    """3-leg scale-out (the user's style): bank w1 at +K1, w2 at +K2, ride the (1-w1-w2)
    runner to a trend-break EMA{span}; a -J close flushes all open legs. Weighted captured pt."""
    fav, fpx, ds = e["fav"], e["fpx"], e["dir_sign"]
    ema = e["ema20"] if span == 20 else e["ema30"]
    w3 = 1.0 - w1 - w2
    o1 = o2 = o3 = True                     # legs still open
    c1 = c2 = c3 = None
    confirmed = False
    for k in range(len(fav)):
        x = float(fav[k])
        if x <= -J:                         # hard stop flushes everything
            if o1: c1 = x
            if o2: c2 = x
            if o3: c3 = x
            o1 = o2 = o3 = False
            break
        if o1 and x >= K1:
            c1, o1 = x, False
        if o2 and x >= K2:
            c2, o2 = x, False
        fav_side = (fpx[k] > ema[k]) if ds > 0 else (fpx[k] < ema[k])
        if fav_side:
            confirmed = True
        elif confirmed and o3:              # runner trend-break
            c3, o3 = x, False
    last = float(fav[-1])
    if o1: c1 = last
    if o2: c2 = last
    if o3: c3 = last
    return w1 * c1 + w2 * c2 + w3 * c3


KEY = [
    ("fixed +30 (BASE)", lambda e: ex_fixed(e, 30)),
    ("hold to close",    lambda e: ex_fixed(e, 10_000)),
    ("tgt+15/stop-12",   lambda e: ex_target_stop(e, 15, 12)),
    ("trail A0/give12",  lambda e: ex_trail(e, 0, 12)),
    ("trail A5/give8",   lambda e: ex_trail(e, 5, 8)),
    ("trend-break EMA20", lambda e: ex_trend_break(e, 20)),
    ("trend-break EMA30", lambda e: ex_trend_break(e, 30)),
    # scale-out (bank most, ride a small runner) — the recommendation candidate
    ("scale 50/30@8/20 r20", lambda e: ex_scaleout(e, 0.5, 0.3, 8, 20, 15, 20)),
    ("scale 40/30@8/20 r30", lambda e: ex_scaleout(e, 0.4, 0.3, 8, 20, 18, 30)),
]


def run(preset):
    cache = pickle.load(open(CACHE_PKL, "rb"))
    ents = collect_entries(cache, PRESETS[preset])
    dates = [e["date"] for e in ents]
    orc = np.array([oracle(e) for e in ents])

    print("=" * 100)
    print(f"TAIL / GIVEBACK / CAPTURE-EFFICIENCY  —  preset='{preset}'  n={len(ents)}  "
          f"sessions={len(set(dates))}")
    print(f"  ORACLE ceiling (perfect exit): mean max-fav {orc.mean():+.2f}pt  median {np.median(orc):+.2f}  "
          f"P90 {np.percentile(orc,90):+.1f}  (this is the most any exit could capture)")
    print("=" * 100)

    print(f"\n  {'policy':20s} {'mean':>7s} {'eff%':>5s} {'P10':>6s} {'%<-20':>6s} | "
          f"{'P90':>6s} {'%>20':>5s} {'%>40':>5s} {'t_clu':>6s}")
    print("  " + "-" * 86)
    for name, fn in KEY:
        cap = np.array([fn(e) for e in ents], float)
        eff = 100.0 * cap.mean() / orc.mean() if orc.mean() else float("nan")
        print(f"  {name:20s} {cap.mean():+7.2f} {eff:4.0f}% {np.percentile(cap,10):+6.1f} "
              f"{100.0*(cap<-20).mean():5.1f}% | {np.percentile(cap,90):+6.1f} "
              f"{100.0*(cap>20).mean():4.1f}% {100.0*(cap>40).mean():4.1f}% "
              f"{_clustered_t(cap.tolist(), dates):+6.2f}")

    # quantile ladder
    print(f"\n  QUANTILE LADDER of captured pts")
    qs = [5, 10, 25, 50, 75, 90, 95, 99]
    print(f"  {'policy':20s} " + " ".join(f"{('p'+str(q)):>7s}" for q in qs))
    for name, fn in KEY:
        cap = np.array([fn(e) for e in ents], float)
        print(f"  {name:20s} " + " ".join(f"{np.percentile(cap,q):+7.1f}" for q in qs))

    # biggest red->green (oracle) example days
    print(f"\n  TOP 12 BIGGEST-OPPORTUNITY TRADES (by oracle max-fav) — what each policy actually caught")
    print(f"  {'date':11s} {'dir':4s} {'ET':>5s} {'oracle':>7s} {'+30':>7s} {'hold':>7s} "
          f"{'EMA20':>7s} {'trailA0g12':>10s}")
    order = np.argsort(-orc)[:12]
    for k in order:
        e = ents[k]
        et = dt.datetime.fromtimestamp(e["ts0"] / 1000, ET)
        d = "CALL" if e["dir_sign"] > 0 else "PUT"
        print(f"  {e['date']:11s} {d:4s} {et.strftime('%H:%M'):>5s} {orc[k]:+7.1f} "
              f"{ex_fixed(e,30):+7.1f} {ex_fixed(e,10_000):+7.1f} {ex_trend_break(e,20):+7.1f} "
              f"{ex_trail(e,0,12):+10.1f}")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", choices=list(PRESETS), default="big_move")
    a = ap.parse_args()
    run(a.preset)
