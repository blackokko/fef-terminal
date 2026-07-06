"""BEAR TEST: does the put-fade hold in 2022 (bear), using an INDEPENDENT IBKR SPX 1-min ruler?

The QuantData ruler only reaches 2025-26 (a dip-buy epoch). This re-runs the trend-conditioned
put-fade across the FULL ThetaData history (2022-2026, ~1093 sess) using IBKR SPX 1-min cash bars
(scratch DB, independent of the ThetaData chain -> no artifact) as the forward-move ruler, reported
BY YEAR. 2022 = the sustained bear the QuantData tests could never see.

hit% = price went the option's way; for PUT dir=-1 so LOW hit = the FADE wins (price bounced up).
Read-only. Run:  .venv/Scripts/python.exe -m scripts.research.backtest.bear_test_putfade [START END]
"""
from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

from .data import connect
from .direction_confluence_realspot import surges, trail_range
from .holdout import HOLDOUT_START
from .ibkr_ruler import ibkr_open, ibkr_series
from .thetadata_real_spot_validate import MOVE_GLITCH_FRAC, _et_h, real_at

HORIZONS = [("+10min", 10), ("+30min", 30)]
TRAIL_MIN = 15
TREND_MIN = 30


def cl(vals, center=0.0):
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], float)
    n = len(a)
    if n < 2 or a.std(ddof=1) == 0:
        return (float(a.mean()) if n else float("nan")), float("nan"), n
    return float(a.mean()), float((a.mean() - center) / (a.std(ddof=1) / np.sqrt(n))), n


def line(recs, filt, label):
    rr = [r for r in recs if filt(r)]
    if not rr:
        print(f"    {label:20s}   (none)")
        return
    ph, pf = defaultdict(list), defaultdict(list)
    for r in rr:
        ph[r["date"]].append(100.0 * r["hit"]); pf[r["date"]].append(r["fav"])
    hit = cl([np.mean(v) for v in ph.values()], 50.0)
    fav = cl([np.mean(v) for v in pf.values()], 0.0)
    amb = cl([r["mv"] for r in rr], 0.0)
    print(f"    {label:20s} {hit[2]:4d}s {len(rr):5d}p  hit={hit[0]:5.1f}% (t{hit[1]:+5.2f})  "
          f"fav={fav[0]:+6.2f}pt (t{fav[1]:+5.2f})  ambMove={amb[0]:+6.2f}")


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 96)
    print("BEAR TEST — put-fade across 2022-2026 on INDEPENDENT IBKR SPX 1-min ruler")
    print(f"  {start}..{end} ({len(dates)} sess); LOW hit = fade wins; by YEAR (2022=bear)")
    print("=" * 96)

    recs = []
    n_sess = n_noib = 0
    for k, d in enumerate(dates):
        sg = surges(con, d)
        if not sg:
            continue
        rs = ibkr_series(ic, d)
        if rs is None:
            n_noib += 1
            continue
        rt, rp = rs
        pts = []
        for ts_i, dirn in sg:
            tr = trail_range(rt, rp, ts_i - TRAIL_MIN * 60_000, ts_i)
            pts.append((ts_i, dirn, tr))
        trs = [p[2] for p in pts if p[2] is not None]
        if not trs:
            continue
        tr_med = float(np.median(trs))
        year, split = d[:4], ("DEV" if d < HOLDOUT_START else "HOLD")
        used = False
        for hname, hmin in HORIZONS:
            for ts_i, dirn, tr in pts:
                s0 = real_at(rt, rp, ts_i)
                sH = real_at(rt, rp, ts_i + hmin * 60_000)
                sB = real_at(rt, rp, ts_i - TREND_MIN * 60_000)
                if s0 is None or sH is None or sB is None:
                    continue
                mv = sH - s0
                if abs(mv) > MOVE_GLITCH_FRAC * s0:
                    continue
                recs.append(dict(
                    date=d, year=year, split=split, h=hname,
                    side=("CALL" if dirn > 0 else "PUT"),
                    vol=("EXPAND" if (tr is not None and tr > tr_med) else "CONTRACT"),
                    trend=("TUP" if (s0 - sB) > 0 else "TDN"),
                    fav=float(dirn * mv), hit=1.0 if (dirn * mv) > 0 else 0.0, mv=float(mv)))
                used = True
        if used:
            n_sess += 1
        if (k + 1) % 100 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d}) sess={n_sess} no_ibkr={n_noib}", flush=True)

    print(f"\n  usable sessions: {n_sess}   sessions missing IBKR ruler: {n_noib}\n")

    YEARS = ["2022", "2023", "2024", "2025", "2026"]
    for hname, _ in HORIZONS:
        print("=" * 96)
        print(f"HORIZON {hname}")
        for yr in YEARS:
            yrecs = [r for r in recs if r["year"] == yr and r["h"] == hname]
            if not yrecs:
                continue
            ns = len({r["date"] for r in yrecs})
            print(f"  --- {yr}  ({ns} sessions) ---")
            line(recs, lambda r, y=yr, h=hname: r["year"] == y and r["h"] == h and r["side"] == "PUT",
                 "PUT all")
            line(recs, lambda r, y=yr, h=hname: r["year"] == y and r["h"] == h and r["side"] == "PUT"
                 and r["vol"] == "EXPAND", "PUTxEXPAND")
            line(recs, lambda r, y=yr, h=hname: r["year"] == y and r["h"] == h and r["side"] == "PUT"
                 and r["trend"] == "TDN", "PUT/TDN")
            line(recs, lambda r, y=yr, h=hname: r["year"] == y and r["h"] == h and r["side"] == "CALL",
                 "CALL all")
        print()
    print("=" * 96)
    print("VERDICT: PUT fade hit<50% in 2022 (bear) too => robust across regimes.")
    print("  PUT fade hit>=50% in 2022 (puts WORK in the bear) => it's a dip-buy-regime effect, gate by trend.")
    print("=" * 96)


if __name__ == "__main__":
    main()
