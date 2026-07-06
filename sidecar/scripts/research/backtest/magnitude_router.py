"""MAGNITUDE ROUTER — actionable vol-state -> (buy convexity / sell premium / sit out) table.
(READ-ONLY spike.)  Turns the OOS-validated magnitude predictors (VIX prior-close, realized-vol-
so-far, capitulation-event flag) into concrete routing buckets with their forward-|move30|
statistics, DEV vs HOLDOUT.  Descriptive (no bootstrap) — significance established in
magnitude_ambient.py.  Ruler = independent IBKR SPX 1-min; target |move| at +30min, no lookahead.

Run:  .venv/Scripts/python.exe -m scripts.research.backtest.magnitude_router
"""
from __future__ import annotations

import os
import sys

import numpy as np

from . import confluence_expectancy as CE
from . import magnitude_ambient as MA
from . import magnitude_filter as MF

sys.stdout.reconfigure(encoding="utf-8")


def stat(a):
    a = np.asarray(a, float); a = a[np.isfinite(a)]
    if len(a) == 0:
        return None
    return dict(n=len(a), mean=float(a.mean()), med=float(np.median(a)),
                p5=100 * np.mean(a >= 5), p12=100 * np.mean(a >= 12), small=100 * np.mean(a < 3))


def cell(s):
    return (f"{s['mean']:5.1f}/{s['med']:4.1f} P>=5:{s['p5']:3.0f}% P>=12:{s['p12']:3.0f}% "
            f"calm<3:{s['small']:3.0f}% (n{s['n']})") if s else "(none)"


def main():
    print("=" * 116)
    print("MAGNITUDE ROUTER — forward |move30| by VOL-STATE bucket (OOS-validated predictors). DEV vs HOLDOUT.")
    print("  E|move|/med  P>=5(convexity wins large)  P>=12  calm<3pt(premium wins).  Ruler=IBKR SPX, no lookahead.")
    print("=" * 116)

    amb = MA.build_ambient()
    # rvol quartile edges from DEV ambient
    rv_dev = np.concatenate([np.asarray(r["rvol"], float) for r in amb if r["split"] == "DEV"])
    rv_dev = rv_dev[np.isfinite(rv_dev)]
    q = np.percentile(rv_dev, [25, 50, 75])
    print(f"\n  realized-vol-so-far DEV quartile edges (pt/min): "
          f"Q1<={q[0]:.2f}  Q2<={q[1]:.2f}  Q3<={q[2]:.2f}")

    # ---- ambient by rvol quartile ----
    print("\n  AMBIENT (any in-window entry) forward-|move30| by realized-vol-so-far quartile:")
    for split in ("DEV", "HOLD"):
        print(f"    {split}:")
        for lbl, lo, hi in (("Q1(calm)", -1e9, q[0]), ("Q2", q[0], q[1]),
                            ("Q3", q[1], q[2]), ("Q4(hot)", q[2], 1e9)):
            vals = []
            for r in amb:
                if r["split"] != split:
                    continue
                rv = np.asarray(r["rvol"], float); ab = np.asarray(r["abs30"], float)
                m = np.isfinite(rv) & (rv > lo) & (rv <= hi)
                vals.append(ab[m])
            print(f"      {lbl:9s} {cell(stat(np.concatenate(vals) if vals else []))}")

    # ---- combined router: VIX x event-flag ----
    events = MF.build_events()
    ev_abs = {"DEV": [], "HOLD": []}
    for e in events:
        if e["abs30"] is not None and np.isfinite(e["abs30"]):
            ev_abs[e["split"]].append(e["abs30"])

    print("\n" + "=" * 116)
    print("  ROUTING BUCKETS (mutually exclusive priority: EVENT > VIX>=25 > VIX<15-calm > middle):")
    print("    route            definition                                   DEV                          HOLD")
    for split in ("DEV", "HOLD"):
        # BIG-A: at a capitulation event (any VIX)
        big_ev = stat(ev_abs[split])
        # BIG-B: no event, VIX>=25 (high-vol regime), ambient entries
        big_vix, calm, mid = [], [], []
        for r in amb:
            if r["split"] != split:
                continue
            v = r["vix"]; ab = np.asarray(r["abs30"], float); ab = ab[np.isfinite(ab)]
            if not np.isfinite(v):
                mid.append(ab); continue
            if v >= 25:
                big_vix.append(ab)
            elif v < 15:
                calm.append(ab)
            else:
                mid.append(ab)
        rows = [
            ("BUY CONVEXITY", "at capitulation EVENT (any VIX)", big_ev),
            ("BUY CONVEXITY", "no event, VIX>=25 regime", stat(np.concatenate(big_vix) if big_vix else [])),
            ("SELL PREMIUM", "no event, VIX<15 regime (calm)", stat(np.concatenate(calm) if calm else [])),
            ("SIT OUT", "no event, VIX 15-25 (middle)", stat(np.concatenate(mid) if mid else [])),
        ]
        print(f"  --- {split} ---")
        for route, defn, s in rows:
            print(f"    {route:15s} {defn:44s} {cell(s)}")
    print("\nDONE.")


if __name__ == "__main__":
    main()
