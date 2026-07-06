"""TASK: is the PUT-FADE a DEALER short-put-COVER / hedge-unwind effect? (mechanism test)

User hypothesis: aggressive 0DTE put buying -> SPX up because dealers/premium-sellers are BUYING
BACK short puts to close -> unwinding short-underlying hedges -> mechanical buying -> bounce.
Symmetric prediction: aggressive call buying near the call wall -> dealers cover short calls ->
sell hedge -> DOWN (a bearish signal hidden in aggregate call noise).

Direct positioning tests (SOD OI per strike from option_open_interest; intraday dealer GEX from
option_dealer_exposure):
  WALL proximity : dist = spot_at_surge - max-OI strike on that side. Dealer short-X cover fires
                   when spot sits ON the X wall (those options are live/tested). NEAR=|dist|<=25pt.
  GEX regime     : LONG_GAMMA (dealers stabilize, buy dips) vs SHORT_GAMMA at the surge ts.
                   Put-fade bounce should concentrate in LONG_GAMMA if it's dealer-hedging-driven.

Outcome = forward SIGNED move on REAL spot, fav = dir*move (PUT dir=-1 so fav<0 = fade/bounce wins,
hit = price went the option's way; LOW hit = fade wins). Session-clustered, DEV/HOLDOUT.
GEX coverage is DEV-mostly (option_dealer_exposure ends 2025-08-12); wall coverage is full.

Read-only DB + network-read API. Run:
  .venv/Scripts/python.exe -m scripts.research.backtest.mechanism_putfade [START END]
"""
from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

from .data import connect
from .direction_confluence_realspot import surges, trail_range
from .directional_validate_0dte_netdrift import _api, _post
from .holdout import HOLDOUT_START
from .thetadata_real_spot_validate import MOVE_GLITCH_FRAC, _et_h, real_at, real_series

sys.stdout.reconfigure(encoding="utf-8")

HORIZONS = [("+10min", 10), ("+30min", 30)]
TRAIL_MIN = 15
NEAR_PT = 25.0
GEX_TOL_MS = 600_000


def walls(con, d):
    """(put_wall_strike, call_wall_strike) = max-OI strike each side at SOD. None if no OI."""
    rows = con.execute(
        "SELECT strike, right, open_interest FROM option_open_interest WHERE session_date=?", (d,)).fetchall()
    if not rows:
        return None
    puts = [(s, oi) for s, rt, oi in rows if rt == "P" and oi]
    calls = [(s, oi) for s, rt, oi in rows if rt == "C" and oi]
    if not puts or not calls:
        return None
    return max(puts, key=lambda x: x[1])[0], max(calls, key=lambda x: x[1])[0]


def gex_series(con, d):
    rows = con.execute(
        "SELECT ts_ms, regime, flip_dist FROM option_dealer_exposure WHERE session_date=? ORDER BY ts_ms",
        (d,)).fetchall()
    if not rows:
        return None
    return (np.array([r[0] for r in rows], np.int64),
            [r[1] for r in rows], np.array([r[2] if r[2] is not None else np.nan for r in rows], float))


def gex_at(g, ts):
    if g is None:
        return None
    tarr, regs, _ = g
    j = int(np.searchsorted(tarr, ts))
    best, bd = None, GEX_TOL_MS + 1
    for cdt in (j - 1, j):
        if 0 <= cdt < len(tarr):
            dd = abs(int(tarr[cdt]) - ts)
            if dd <= bd:
                bd, best = dd, cdt
    return regs[best] if (best is not None and bd <= GEX_TOL_MS) else None


def cl(vals, center=0.0):
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], float)
    n = len(a)
    if n < 2 or a.std(ddof=1) == 0:
        return (float(a.mean()) if n else float("nan")), float("nan"), n
    return float(a.mean()), float((a.mean() - center) / (a.std(ddof=1) / np.sqrt(n))), n


def report(recs, filt, label):
    rr = [r for r in recs if filt(r)]
    if not rr:
        print(f"    {label:26s}   (none)")
        return
    perh = defaultdict(list); perf = defaultdict(list)
    for r in rr:
        perh[r["date"]].append(100.0 * r["hit"]); perf[r["date"]].append(r["fav"])
    hit = cl([np.mean(v) for v in perh.values()], 50.0)
    fav = cl([np.mean(v) for v in perf.values()], 0.0)
    print(f"    {label:26s} {hit[2]:4d}s {len(rr):5d}p  hit={hit[0]:5.1f}% (t{hit[1]:+5.2f})  "
          f"fav={fav[0]:+6.3f}pt (t{fav[1]:+5.2f})")


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 100)
    print("PUT-FADE MECHANISM — dealer short-put cover? (wall proximity + GEX regime, REAL spot)")
    print(f"  {start}..{end} ({len(dates)} sess); NEAR=|spot-wall|<={NEAR_PT}pt; LOW hit = fade wins")
    print("=" * 100)

    recs = []
    n_sess = n_fail = n_gex = 0
    with _api() as cli:
        for k, d in enumerate(dates):
            sg = surges(con, d)
            if not sg:
                continue
            w = walls(con, d)
            if w is None:
                continue
            put_wall, call_wall = w
            data = _post(cli, {"sessionDate": d, "filter": {"ticker": "SPX", "expirationDate": d}})
            if not data:
                n_fail += 1
                continue
            rs = real_series(data)
            if rs is None:
                continue
            rt, rp = rs
            g = gex_series(con, d)
            if g is not None:
                n_gex += 1
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
                    if s0 is None or sH is None:
                        continue
                    mv = sH - s0
                    if abs(mv) > MOVE_GLITCH_FRAC * s0:
                        continue
                    side = "CALL" if dirn > 0 else "PUT"
                    wall = put_wall if side == "PUT" else call_wall
                    dist = s0 - wall
                    recs.append(dict(
                        date=d, year=year, split=split, h=hname, side=side,
                        vol=("EXPAND" if (tr is not None and tr > tr_med) else "CONTRACT"),
                        near=(abs(dist) <= NEAR_PT), above=(dist > 0),
                        gex=gex_at(g, ts_i),
                        fav=float(dirn * mv), hit=1.0 if (dirn * mv) > 0 else 0.0))
                    used = True
            if used:
                n_sess += 1
            if (k + 1) % 25 == 0:
                print(f"  ...{k+1}/{len(dates)} ({d}) sess={n_sess} gex_sess={n_gex}", flush=True)

    print(f"\n  usable sessions: {n_sess}  gex-covered: {n_gex}  api_fail: {n_fail}\n")

    for hname, _ in HORIZONS:
        for split in ("DEV", "HOLD"):
            base = lambda r, hn=hname, sp=split: r["h"] == hn and r["split"] == sp
            print("=" * 100)
            print(f"HORIZON {hname}   SPLIT {split}")
            print("  -- PUT-FADE x put-wall proximity --")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT", "PUT all")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT" and r["near"], "PUT  NEAR put-wall")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT" and not r["near"], "PUT  FAR from wall")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT" and r["vol"] == "EXPAND" and r["near"],
                   "PUTxEXPAND NEAR wall")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT" and r["vol"] == "EXPAND" and not r["near"],
                   "PUTxEXPAND FAR wall")
            print("  -- PUT-FADE x GEX regime (DEV-mostly) --")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT" and r["gex"] == "LONG_GAMMA",
                   "PUT  LONG_GAMMA")
            report(recs, lambda r, b=base: b(r) and r["side"] == "PUT" and r["gex"] == "SHORT_GAMMA",
                   "PUT  SHORT_GAMMA")
            print("  -- CALL symmetry: near call-wall -> DOWN? (your prediction) --")
            report(recs, lambda r, b=base: b(r) and r["side"] == "CALL", "CALL all")
            report(recs, lambda r, b=base: b(r) and r["side"] == "CALL" and r["near"], "CALL NEAR call-wall")
            report(recs, lambda r, b=base: b(r) and r["side"] == "CALL" and r["vol"] == "EXPAND" and r["near"],
                   "CALLxEXPAND NEAR wall")
            print()
    print("=" * 100)
    print("READ: dealer-cover mechanism => PUT fade STRONGER near put-wall & in LONG_GAMMA;")
    print("  CALL near call-wall hit<50% (price DOWN) would confirm the symmetric short-call cover.")
    print("=" * 100)


if __name__ == "__main__":
    main()
