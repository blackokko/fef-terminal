"""COMPREHENSIVE (2022-2026, INDEPENDENT IBKR ruler) re-run of the put-fade MECHANISM test
(dealer short-put cover?). Same wall-proximity + GEX-regime buckets as mechanism_putfade.py but the
ruler is IBKR SPX 1-min over the full history, reported BY YEAR. Crucially, dealer GEX
(option_dealer_exposure, 2022-2025-08) now covers the 2022 BEAR + 2023/24 — so the dealer-cover and
gamma-regime mechanism is finally testable outside the 2025 dip-buy epoch.
Run:  .venv/Scripts/python.exe -m scripts.research.backtest.mechanism_ibkr [START END]
"""
from __future__ import annotations

import sys

import numpy as np

from .data import connect
from .direction_confluence_realspot import surges, trail_range
from .ibkr_ruler import ibkr_open, ibkr_series
from .mechanism_putfade import NEAR_PT, gex_at, gex_series, report, walls
from .thetadata_real_spot_validate import MOVE_GLITCH_FRAC, real_at

sys.stdout.reconfigure(encoding="utf-8")
HORIZONS = [("+10min", 10), ("+30min", 30)]
TRAIL_MIN = 15
YEARS = ["2022", "2023", "2024", "2025", "2026", "ALL"]


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 100)
    print("PUT-FADE MECHANISM (dealer short-put cover?) — INDEPENDENT IBKR ruler, 2022-2026 BY YEAR")
    print(f"  {start}..{end} ({len(dates)} sess); NEAR=|spot-wall|<={NEAR_PT}pt; LOW hit = fade wins")
    print("=" * 100)

    recs = []
    n_sess = n_noib = n_gex = 0
    for k, d in enumerate(dates):
        sg = surges(con, d)
        if not sg:
            continue
        w = walls(con, d)
        if w is None:
            continue
        put_wall, call_wall = w
        rs = ibkr_series(ic, d)
        if rs is None:
            n_noib += 1
            continue
        rt, rp = rs
        g = gex_series(con, d)
        if g is not None:
            n_gex += 1
        pts = [(ts_i, dirn, trail_range(rt, rp, ts_i - TRAIL_MIN * 60_000, ts_i)) for ts_i, dirn in sg]
        trs = [p[2] for p in pts if p[2] is not None]
        if not trs:
            continue
        tr_med = float(np.median(trs))
        year = d[:4]
        used = False
        for hname, hmin in HORIZONS:
            for ts_i, dirn, tr in pts:
                s0 = real_at(rt, rp, ts_i)
                sH = real_at(rt, rp, ts_i + hmin * 60_000)
                if s0 is None or sH is None:
                    continue
                mv = sH - s0
                if abs(mv) > MOVE_GLITCH_FRAC * s0:
                    continue
                side = "CALL" if dirn > 0 else "PUT"
                dist = s0 - (put_wall if side == "PUT" else call_wall)
                recs.append(dict(
                    date=d, year=year, h=hname, side=side,
                    vol=("EXPAND" if (tr is not None and tr > tr_med) else "CONTRACT"),
                    near=(abs(dist) <= NEAR_PT), gex=gex_at(g, ts_i),
                    fav=float(dirn * mv), hit=1.0 if (dirn * mv) > 0 else 0.0))
                used = True
        if used:
            n_sess += 1
        if (k + 1) % 100 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d}) sess={n_sess} gex={n_gex} no_ibkr={n_noib}", flush=True)

    print(f"\n  usable sessions: {n_sess}  gex-covered: {n_gex}  missing IBKR ruler: {n_noib}\n")

    for hname, _ in HORIZONS:
        for yr in YEARS:
            yp = (lambda r, y=yr: True) if yr == "ALL" else (lambda r, y=yr: r["year"] == y)
            base = lambda r, hn=hname, yf=yp: r["h"] == hn and yf(r)
            if not any(base(r) for r in recs):
                continue
            print("=" * 100)
            print(f"HORIZON {hname}   YEAR {yr}")
            print("  -- PUT-FADE x put-wall proximity --")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT", "PUT all")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT" and r["near"], "PUT NEAR put-wall")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT" and not r["near"], "PUT FAR from wall")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT" and r["vol"] == "EXPAND" and r["near"],
                   "PUTxEXPAND NEAR")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT" and r["vol"] == "EXPAND" and not r["near"],
                   "PUTxEXPAND FAR")
            print("  -- PUT-FADE x GEX regime --")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT" and r["gex"] == "LONG_GAMMA",
                   "PUT LONG_GAMMA")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT" and r["gex"] == "SHORT_GAMMA",
                   "PUT SHORT_GAMMA")
            print("  -- CALL symmetry (near call-wall -> DOWN?) --")
            report(recs, lambda r, b=base: b(r) and r["side"] == "CALL", "CALL all")
            report(recs, lambda r, b=base: b(r) and r["side"] == "CALL" and r["near"], "CALL NEAR call-wall")
        print()
    print("=" * 100)


if __name__ == "__main__":
    main()
