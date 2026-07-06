"""ORB DIRECTIONAL EDGE — does an Opening-Range breakout precede a tradeable SIGNED SPX move?

Signal is PRICE (the breakout), and the forward move is measured on the SAME independent IBKR
SPX 1-min ruler (ibkr_ruler) — this is a price->price test, NOT a flow signal on a reconstructed
spot, so there is no spot-reconstruction artifact here. For each session we build the 15-min and
30-min opening ranges (orb_core), fire on the break (dir=+1 up-break / -1 down-break), and score
the forward SIGNED move fav=dir*(spot_H - spot_0) at +30min / +60min / EOD.

VARIANTS (per OR window):
  FIRST    - the first break of the session (the tradeable one)
  ALL      - every fresh crossing (whipsaw-honest: chop days re-break both ways)
  FILTERED - first break that clears the OR band by > 2pt AND lands in the first ~90 min

Reports BY YEAR + ALL: N, hit%, meanFav(pt), t, %big8 (fav>+8pt = the monetizable-move rate),
and — for honesty — medFav and %adverse (fav<-8pt, i.e. the breakout reversed hard).

Read-only. Run: .venv/Scripts/python.exe -m scripts.research.backtest.orb_direction [START END]
"""
from __future__ import annotations

import sys

import numpy as np

from .data import connect
from .holdout import HOLDOUT_START
from .ibkr_ruler import ibkr_open, ibkr_series
from .orb_core import (FILT_BUFFER_PT, FILT_MAX_MIN, OR_WINDOWS, breaks, eod_close,
                       open_ms, or_band)
from .thetadata_real_spot_validate import MOVE_GLITCH_FRAC, real_at

sys.stdout.reconfigure(encoding="utf-8")

HORIZONS = [("+30min", 30), ("+60min", 60), ("EOD", None)]
BIG = 8.0
VARIANTS = ["FIRST", "ALL", "FILTERED"]
YEARS = ["2022", "2023", "2024", "2025", "2026", "ALL", "HOLD"]


def fwd(rt, rp, ts, hmin, eod):
    """Signed forward move on the ruler from ts to +hmin (or EOD). None if unmeasurable/glitch."""
    s0 = real_at(rt, rp, ts)
    if s0 is None:
        return None
    sH = eod if hmin is None else real_at(rt, rp, ts + hmin * 60_000)
    if sH is None:
        return None
    mv = sH - s0
    if abs(mv) > MOVE_GLITCH_FRAC * s0:
        return None
    return mv


def session_events(rt, rp, t_open, or_min):
    """(variant -> [(ts,dir)]) for one session & OR window."""
    band = or_band(rt, rp, t_open, or_min)
    if band is None:
        return None
    hi, lo, _rng, or_end = band
    raw = breaks(rt, rp, t_open, hi, lo, or_end)
    filt = breaks(rt, rp, t_open, hi, lo, or_end, buffer_pt=FILT_BUFFER_PT, max_min=FILT_MAX_MIN)
    return {"FIRST": raw[:1], "ALL": raw, "FILTERED": filt[:1]}


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 108)
    print("ORB DIRECTIONAL EDGE — Opening-Range breakout -> forward SIGNED SPX move (IBKR ruler, 2022-2026)")
    print(f"  {start}..{end} ({len(dates)} sess); OR={OR_WINDOWS}min; variants {VARIANTS}; big8=fav>+{BIG}pt")
    print(f"  FILTERED = first break clearing OR by >{FILT_BUFFER_PT}pt within first {FILT_MAX_MIN}min")
    print("=" * 108)

    # recs[(or_min, variant, horizon, yearkey)] -> list of fav
    recs = {}
    n_days = 0
    for k, d in enumerate(dates):
        rs = ibkr_series(ic, d)
        if rs is None:
            continue
        rt, rp = rs
        eod = eod_close(rt, rp)
        if eod is None:
            continue
        t_open = open_ms(d)
        year = d[:4]
        ykeys = [year, "ALL"] + (["HOLD"] if d >= HOLDOUT_START else [])
        n_days += 1
        for or_min in OR_WINDOWS:
            ev = session_events(rt, rp, t_open, or_min)
            if ev is None:
                continue
            for variant, events in ev.items():
                for ts, dirn in events:
                    for hname, hmin in HORIZONS:
                        mv = fwd(rt, rp, ts, hmin, eod)
                        if mv is None:
                            continue
                        fav = dirn * mv
                        for yk in ykeys:
                            recs.setdefault((or_min, variant, hname, yk), []).append(fav)
        if (k + 1) % 250 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d}) days_used={n_days}", flush=True)

    print(f"\n  ruler days used: {n_days}\n")

    for or_min in OR_WINDOWS:
        for variant in VARIANTS:
            print("=" * 108)
            print(f"OR={or_min}min   VARIANT {variant}")
            for hname, _ in HORIZONS:
                print(f"  horizon {hname}:")
                print(f"    {'year':5s} {'N':>6s} {'hit%':>6s} {'meanFav':>8s} {'t':>6s} "
                      f"{'medFav':>7s} {'%big8':>6s} {'%adv8':>6s}")
                for yk in YEARS:
                    a = np.array(recs.get((or_min, variant, hname, yk), []), float)
                    if len(a) < 10:
                        continue
                    hit = 100.0 * np.mean(a > 0)
                    m = float(a.mean())
                    sd = a.std(ddof=1)
                    t = m / (sd / np.sqrt(len(a))) if sd > 0 else float("nan")
                    print(f"    {yk:5s} {len(a):6d} {hit:5.1f}% {m:+8.3f} {t:+6.2f} "
                          f"{np.median(a):+7.2f} {100.0*np.mean(a > BIG):5.1f}% "
                          f"{100.0*np.mean(a < -BIG):5.1f}%")
            print()

    print("=" * 108)
    print("READ: hit>>50 + meanFav>0 + high %big8 (and %big8 >> %adv8) = the break precedes a")
    print("  tradeable directional move. hit~50 / meanFav~0 / %big8≈%adv8 = ORB whipsaws (chop eats it).")
    print("  FILTERED lifting FIRST = the >2pt/first-90min screen removes false pokes.")
    print("=" * 108)


if __name__ == "__main__":
    main()
