"""LONG-ONLY DIRECTION HUNT for BIG moves (2022-2026, independent IBKR ruler).

The put-fade is directionally right but its ~+5pt move is too small to beat 0DTE theta as a long
option. So this hunts UNTAPPED signals for LARGE directional moves, judged by MAGNITUDE ($-relevant),
not hit-rate. Four signals, shared forward-move on the IBKR SPX ruler:

  CHARM      - dir = sign(cex) (dealer charm exposure) -> into-close delta-decay drift
  VANNA      - dir = sign(vex) (dealer vanna exposure)
  SGAMMA_FOL - flow surge FOLLOWED (dir=sign(roll)) when regime=SHORT_GAMMA AND price momentum agrees
               (dealers amplify -> continuation leg)
  DRIFT_TRND - cum-drift slope AND price slope agree over 30min -> dir = that sign (trend confluence)
  FADE       - the validated put-fade (dir=+1 long call), for magnitude reference + surge-size split

KEY METRIC per signal/year: hit%, mean favorable pt, and %fav>+8pt / %fav>+15pt (the fraction of
entries that make a MONETIZABLE long-option move). big8 is the number that matters for long $.
CHARM/VANNA/SGAMMA use option_dealer_exposure (2022..2025-08). Read-only. Run:
  .venv/Scripts/python.exe -m scripts.research.backtest.direction_hunt_ibkr [START END]
"""
from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

from .data import connect
from .direction_confluence_realspot import surges
from .ibkr_ruler import ibkr_open, ibkr_series
from .thetadata_real_spot_validate import MOVE_GLITCH_FRAC, WIN_HI, WIN_LO, _et_h, _roll, real_at

sys.stdout.reconfigure(encoding="utf-8")
HORIZON = 30
SAMPLE_MS = 10 * 60_000        # sample charm/vanna/drift every ~10 min
TREND_MS = 30 * 60_000
BIG8, BIG15 = 8.0, 15.0


def dealer_series(con, d):
    rows = con.execute("SELECT ts_ms, cex, vex, regime FROM option_dealer_exposure "
                       "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
    if not rows:
        return None
    return (np.array([r[0] for r in rows], np.int64),
            np.array([r[1] if r[1] is not None else 0.0 for r in rows], float),
            np.array([r[2] if r[2] is not None else 0.0 for r in rows], float),
            [r[3] for r in rows])


def nearest_idx(ts_arr, ts, tol=SAMPLE_MS):
    if len(ts_arr) == 0:
        return None
    j = int(np.searchsorted(ts_arr, ts))
    best, bd = None, tol + 1
    for c in (j - 1, j):
        if 0 <= c < len(ts_arr) and abs(int(ts_arr[c]) - ts) < bd:
            bd, best = abs(int(ts_arr[c]) - ts), c
    return best


def sampled(ts_arr):
    """Indices sampled ~every SAMPLE_MS within the decision window."""
    out, last = [], None
    for i, t in enumerate(ts_arr):
        h = _et_h(int(t))
        if h < WIN_LO or h > WIN_HI:
            continue
        if last is None or int(t) - last >= SAMPLE_MS:
            out.append(i); last = int(t)
    return out


def entries_for(sig, con, d, rt, rp, de, flow):
    """Return list of (ts_ms, dir) for the given signal on session d."""
    out = []
    if sig in ("CHARM", "VANNA"):
        if de is None:
            return out
        ts_de, cex, vex, _ = de
        arr = cex if sig == "CHARM" else vex
        for i in sampled(ts_de):
            v = arr[i]
            if v != 0.0:
                out.append((int(ts_de[i]), 1.0 if v > 0 else -1.0))
    elif sig == "SGAMMA_FOL":
        if de is None or flow is None:
            return out
        ts_de, _, _, regime = de
        ts_f, net = flow
        roll = _roll(net)
        absr = np.abs(roll)
        valid = absr[~np.isnan(absr)]
        if len(valid) < 10:
            return out
        p90 = np.percentile(valid, 90)
        for i in range(len(ts_f)):
            if np.isnan(roll[i]) or absr[i] < p90:
                continue
            h = _et_h(int(ts_f[i]))
            if h < WIN_LO or h > WIN_HI:
                continue
            j = nearest_idx(ts_de, int(ts_f[i]), tol=TREND_MS)
            if j is None or regime[j] != "SHORT_GAMMA":
                continue
            # price momentum must agree with flow dir
            s0 = real_at(rt, rp, int(ts_f[i]))
            sB = real_at(rt, rp, int(ts_f[i]) - TREND_MS)
            if s0 is None or sB is None:
                continue
            fdir = 1.0 if roll[i] > 0 else -1.0
            if np.sign(s0 - sB) == fdir:
                out.append((int(ts_f[i]), fdir))
    elif sig == "DRIFT_TRND":
        if flow is None:
            return out
        ts_f, net = flow
        drift = np.cumsum(net)
        K = 30  # ~30 one-min bars
        for i in range(K, len(ts_f)):
            h = _et_h(int(ts_f[i]))
            if h < WIN_LO or h > WIN_HI:
                continue
            if i % 10 != 0:
                continue
            dslope = drift[i] - drift[i - K]
            s0 = real_at(rt, rp, int(ts_f[i]))
            sB = real_at(rt, rp, int(ts_f[i]) - TREND_MS)
            if s0 is None or sB is None or dslope == 0:
                continue
            if np.sign(dslope) == np.sign(s0 - sB) and (s0 - sB) != 0:
                out.append((int(ts_f[i]), float(np.sign(dslope))))
    elif sig == "FADE":
        for ts, dirn in surges(con, d):
            if dirn < 0:                    # aggressive put surge -> fade -> long (dir=+1 up)
                out.append((int(ts), 1.0))
    return out


SIGNALS = ["CHARM", "VANNA", "SGAMMA_FOL", "DRIFT_TRND", "FADE"]


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 100)
    print("LONG-ONLY DIRECTION HUNT for BIG moves — IBKR ruler, 2022-2026, +30min")
    print(f"  {start}..{end} ({len(dates)} sess); big8=%fav>+{BIG8}pt (monetizable long-option move)")
    print("=" * 100)

    recs = defaultdict(list)   # (sig, year) -> list of dict(fav, hit, big8, big15)
    counts = defaultdict(int)
    for k, d in enumerate(dates):
        rs = ibkr_series(ic, d)
        if rs is None:
            continue
        rt, rp = rs
        de = dealer_series(con, d)
        frows = con.execute("SELECT ts_ms, net_signed_prem FROM option_flow_minute "
                            "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
        flow = (np.array([r[0] for r in frows], np.int64),
                np.array([r[1] for r in frows], float)) if len(frows) >= 20 else None
        year = d[:4]
        for sig in SIGNALS:
            for ts, dirn in entries_for(sig, con, d, rt, rp, de, flow):
                s0 = real_at(rt, rp, ts)
                sH = real_at(rt, rp, ts + HORIZON * 60_000)
                if s0 is None or sH is None:
                    continue
                mv = sH - s0
                if abs(mv) > MOVE_GLITCH_FRAC * s0:
                    continue
                fav = dirn * mv
                recs[(sig, year)].append(fav)
                recs[(sig, "ALL")].append(fav)
        counts[d[:4]] += 1
        if (k + 1) % 150 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d})", flush=True)

    YEARS = ["2022", "2023", "2024", "2025", "2026", "ALL"]
    for sig in SIGNALS:
        print("\n" + "=" * 100)
        print(f"SIGNAL {sig}")
        print(f"  {'year':5s} {'#ent':>7s} {'hit%':>7s} {'meanFav':>8s} {'t_fav':>6s} "
              f"{'%big8':>7s} {'%big15':>7s}")
        for yr in YEARS:
            a = np.array(recs[(sig, yr)], float)
            if len(a) < 20:
                continue
            hit = 100.0 * np.mean(a > 0)
            mfav = float(a.mean())
            t = mfav / (a.std(ddof=1) / np.sqrt(len(a))) if a.std(ddof=1) > 0 else float("nan")
            b8 = 100.0 * np.mean(a > BIG8)
            b15 = 100.0 * np.mean(a > BIG15)
            print(f"  {yr:5s} {len(a):7d} {hit:6.1f}% {mfav:+8.3f} {t:+6.2f} {b8:6.1f}% {b15:6.1f}%")

    print("\n" + "=" * 100)
    print("READ: a LONG edge needs high %big8 (moves large enough to beat theta) with hit>50% + fav>0.")
    print("  Mean-fav ~0 but symmetric = no edge. High %big8 with hit>>50% = a monetizable long signal.")
    print("=" * 100)


if __name__ == "__main__":
    main()
