"""COMPLETE VIABILITY SCAN — does ANY historical microstructure feature predict DIRECTION inside the
big-move (loaded) windows? Pulls together everything with history: charm/vanna/gex/flip/IV
(option_dealer_exposure), OI walls (option_open_interest), aggressor flow (option_flow_minute), price
(IBKR), + the put-fade + 8/21 EMA state. 2022-2025-08 (charm/vanna coverage).

Logic: magnitude is predictable (big_move_characteristics) -> only trade LOADED windows. Direction is
the hard part -> test every feature there. Sample every 5min; forward +30min SIGNED move on IBKR.
Per feature report hit%, meanFav, %big8(fav>+8) for ALL vs LOADED_VOL vs OPEN, by year. A feature with
hit>>50 + fav>0 + high %big8 in LOADED = a viable directional edge to monetize.

Feature dirs: CHARM sign(cex) / VANNA sign(vex) / FLIP sign(spot-gamma_flip) / DRIFT sign(5-roll flow) /
EMA sign(ema8-ema21 on 5min) / OIWALL toward nearest big OI wall / FADE(+1 at put surge, event-based).
Read-only. Run:  .venv/Scripts/python.exe -m scripts.research.backtest.viability_scan [START END]
"""
from __future__ import annotations

import datetime as dt
import sys
from zoneinfo import ZoneInfo

import numpy as np

from .data import connect
from .ibkr_ruler import ibkr_open, ibkr_series
from .thetadata_real_spot_validate import MOVE_GLITCH_FRAC, ROLL, _roll, real_at

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")
HORIZON = 30
SAMPLE_MIN = 5
WIN_LO, WIN_HI = 9.5833, 15.0
TRAIL_MIN = 30
BIG8 = 8.0
FEATS = ["CHARM", "VANNA", "FLIP", "DRIFT", "EMA", "OIWALL", "FADE"]


def eth(t):
    x = dt.datetime.fromtimestamp(int(t) / 1000, ET)
    return x.hour + x.minute / 60.0


def ema(x, span):
    a = 2.0 / (span + 1)
    o = np.empty(len(x)); o[0] = x[0]
    for i in range(1, len(x)):
        o[i] = a * x[i] + (1 - a) * o[i - 1]
    return o


def dealer(con, d):
    r = con.execute("SELECT ts_ms, spot, gamma_flip, cex, vex FROM option_dealer_exposure "
                    "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
    if not r:
        return None
    return (np.array([x[0] for x in r], np.int64),
            np.array([(x[2] if x[2] else 0) for x in r], float),   # gamma_flip
            np.array([(x[3] if x[3] else 0) for x in r], float),   # cex
            np.array([(x[4] if x[4] else 0) for x in r], float))   # vex


def walls(con, d):
    r = con.execute("SELECT strike, right, open_interest FROM option_open_interest WHERE session_date=?", (d,)).fetchall()
    pk = [(s, oi) for s, rt, oi in r if rt == "P" and oi]
    ck = [(s, oi) for s, rt, oi in r if rt == "C" and oi]
    if not pk or not ck:
        return None
    return max(pk, key=lambda x: x[1])[0], max(ck, key=lambda x: x[1])[0]


def near(arr, t):
    j = int(np.searchsorted(arr, t)) - 1
    return j if 0 <= j < len(arr) else None


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2025-08-12"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 100)
    print("VIABILITY SCAN — microstructure features as DIRECTION predictors in big-move windows")
    print(f"  {start}..{end} ({len(dates)} sess); +{HORIZON}min signed move; LOADED=wild-vol, OPEN=<10:30")
    print("=" * 100)

    # global trailing-range median for the LOADED (wild) split -> collect then threshold
    recs = []   # dict(feat, dir, fav, year, loaded, openw)
    trailvals = []
    for k, d in enumerate(dates):
        rs = ibkr_series(ic, d)
        if rs is None:
            continue
        rt, rp = rs
        de = dealer(con, d)
        w = walls(con, d)
        frows = con.execute("SELECT ts_ms, net_signed_prem FROM option_flow_minute "
                            "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
        if len(frows) >= 20:
            fts = np.array([x[0] for x in frows], np.int64)
            fnet = np.array([x[1] for x in frows], float)
            froll = _roll(fnet)
            fabs = np.abs(froll[~np.isnan(froll)])
            fp90 = np.percentile(fabs, 90) if len(fabs) else np.inf
        else:
            fts = None
        # 5-min EMA state
        step = 5 * 60_000
        buck = {}
        for t, p in zip(rt, rp):
            buck[(int(t) // step) * step] = (int(t), float(p))
        bk = sorted(buck)
        bts = np.array([buck[b][0] for b in bk], np.int64)
        bc = np.array([buck[b][1] for b in bk], float)
        ediff = (ema(bc, 8) - ema(bc, 21)) if len(bc) > 23 else None
        year = d[:4]
        last = None
        for i in range(len(rt)):
            t = int(rt[i]); h = eth(t)
            if h < WIN_LO or h > WIN_HI:
                continue
            if last is not None and t - last < SAMPLE_MIN * 60_000:
                continue
            last = t
            s0 = rp[i]
            sH = real_at(rt, rp, t + HORIZON * 60_000)
            if sH is None or abs(sH - s0) > MOVE_GLITCH_FRAC * s0:
                continue
            mv = sH - s0
            lo2 = int(np.searchsorted(rt, t - TRAIL_MIN * 60_000, side="right"))
            seg = rp[lo2:i + 1]
            trail = float(seg.max() - seg.min()) if len(seg) >= 2 else None
            if trail is not None:
                trailvals.append(trail)
            openw = h < 10.5
            dirs = {}
            if de is not None:
                j = near(de[0], t)
                if j is not None:
                    dirs["CHARM"] = np.sign(de[2][j])
                    dirs["VANNA"] = np.sign(de[3][j])
                    if de[1][j]:
                        dirs["FLIP"] = np.sign(s0 - de[1][j])
            if fts is not None:
                jf = near(fts, t)
                if jf is not None and jf < len(froll) and not np.isnan(froll[jf]):
                    dirs["DRIFT"] = np.sign(froll[jf])
                    if abs(froll[jf]) >= fp90 and froll[jf] < 0:
                        dirs["FADE"] = 1.0     # put surge -> fade up
            if ediff is not None:
                je = near(bts, t)
                if je is not None and je < len(ediff):
                    dirs["EMA"] = np.sign(ediff[je])
            if w is not None:
                pw, cw = w
                nearest_wall = pw if abs(s0 - pw) < abs(s0 - cw) else cw
                dirs["OIWALL"] = np.sign(nearest_wall - s0)   # magnet toward the wall
            for feat, dr in dirs.items():
                if dr == 0:
                    continue
                recs.append((feat, float(dr * mv), year, trail, openw))
        if (k + 1) % 200 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d}) recs={len(recs)}", flush=True)

    tmed = float(np.median(trailvals)) if trailvals else 0.0
    print(f"\n  samples->feature-rows: {len(recs)}   wild-vol threshold (median trail): {tmed:.2f}pt\n")

    def stat(rows):
        a = np.array([r[1] for r in rows], float)
        if len(a) < 30:
            return None
        return (len(a), 100.0 * np.mean(a > 0), float(a.mean()),
                float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))) if a.std(ddof=1) > 0 else float("nan"),
                100.0 * np.mean(a > BIG8))

    for feat in FEATS:
        fr = [r for r in recs if r[0] == feat]
        if not fr:
            continue
        print("=" * 100)
        print(f"FEATURE {feat}")
        print(f"  {'window':10s} {'#':>7s} {'hit%':>7s} {'meanFav':>8s} {'t':>6s} {'%big8':>7s}")
        for wl, rows in (("ALL", fr),
                         ("LOADED_vol", [r for r in fr if r[3] is not None and r[3] > tmed]),
                         ("OPEN", [r for r in fr if r[4]])):
            s = stat(rows)
            if s:
                print(f"  {wl:10s} {s[0]:7d} {s[1]:6.1f}% {s[2]:+8.3f} {s[3]:+6.2f} {s[4]:6.1f}%")
        # by-year in LOADED_vol (where the edge should be if any)
        for yr in ("2022", "2023", "2024", "2025"):
            s = stat([r for r in fr if r[2] == yr and r[3] is not None and r[3] > tmed])
            if s:
                print(f"    LOADED {yr} {s[0]:7d} {s[1]:6.1f}% {s[2]:+8.3f} {s[3]:+6.2f} {s[4]:6.1f}%")
    print("\n" + "=" * 100)
    print("READ: any feature with hit>>50 + fav>0 + high %big8 in LOADED (esp. every year) = viable direction.")
    print("  All ~50/flat = direction genuinely unpredictable from microstructure even in big-move windows.")
    print("=" * 100)


if __name__ == "__main__":
    main()
