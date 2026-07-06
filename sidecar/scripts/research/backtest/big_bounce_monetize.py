"""FINAL long-only $ test: does the BIG-BOUNCE SUBSET of the fade clear theta as a LONG CALL?

The average fade loses as a long call (theta). The big-bounce subset (EXPAND vol + big surge r>=2,
optionally deep-oversold) has ~4x the mean move (+3.5pt) and %big8 up to 36%. This monetizes a LONG
CALL on ONLY that subset (strike sweep, hold-to-time / no-stop ladder, REAL ThetaData NBBO, friction),
$/ct BY YEAR. If even this best subset loses, the long path is exhausted; if it pays, it's tradeable.
Read-only. Run:  .venv/Scripts/python.exe -m scripts.research.backtest.big_bounce_monetize [START END]
"""
from __future__ import annotations

import sys

import numpy as np

from .big_bounce_subset import fade_entries, trail_high
from .data import Session, connect
from .direction_confluence_realspot import trail_range
from .holdout import HOLDOUT_START
from .ibkr_ruler import ibkr_open, ibkr_series
from .put_fade_monetize import EXITS as ALL_EXITS
from .put_fade_monetize import QTY, STRIKES, cl
from .thetadata_real_spot_validate import real_at

sys.stdout.reconfigure(encoding="utf-8")

EXITS = {k: ALL_EXITS[k] for k in ("hold20", "hold30", "ladder_noStop")}
TRAIL_MIN = 15
R_MIN = 2.0            # "big surge" = |roll| >= 2x session p90


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 104)
    print("BIG-BOUNCE SUBSET MONETIZATION — long CALL on EXPAND + big-surge(r>=2) fades ($/ct BY YEAR)")
    print(f"  {start}..{end} ({len(dates)} sess); + deepOS split; strikes {STRIKES}; exits {list(EXITS)}")
    print("=" * 104)

    recs = []
    n_ent = 0
    for k, d in enumerate(dates):
        ents = fade_entries(con, d)
        if not ents:
            continue
        sess = Session(con, d)
        if sess.empty() or not sess.strikes:
            continue
        rs = ibkr_series(ic, d)
        if rs is None:
            continue
        rt, rp = rs
        year, split = d[:4], ("DEV" if d < HOLDOUT_START else "HOLD")
        trs = [trail_range(rt, rp, ts - TRAIL_MIN * 60_000, ts) for ts, _ in ents]
        tr_med = float(np.median([t for t in trs if t is not None])) if any(t is not None for t in trs) else None
        for (ts, r), tr in zip(ents, trs):
            expand = (tr is not None and tr_med is not None and tr > tr_med)
            if not (expand and r >= R_MIN):
                continue                      # BIG-BOUNCE SUBSET filter
            ets = sess.minute_at_or_before(ts)
            if ets is None:
                continue
            spot0 = sess.spot(ets)
            if spot0 is None:
                continue
            th = trail_high(rt, rp, ts - 30 * 60_000, ts)
            s0 = real_at(rt, rp, ts)
            deep = (th is not None and s0 is not None and (th - s0) >= (tr or 0))  # oversold >= a bar's range
            for N in STRIKES:
                strike = min(sess.strikes, key=lambda kk: abs(kk - (spot0 + N)))
                for xname, fn in EXITS.items():
                    t = fn(sess, ets, strike)
                    if t is None:
                        continue
                    recs.append(dict(year=year, split=split, N=N, exit=xname, deep=deep,
                                     pc=t["pnl_per_contract"]))
            n_ent += 1
        if (k + 1) % 150 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d}) subset_entries={n_ent}", flush=True)

    print(f"\n  subset entries: {n_ent}\n")
    YEARS = ["2022", "2023", "2024", "2025", "2026", "ALL", "HOLD"]

    def sel(xname, N, yr, deep=None):
        out = []
        for r in recs:
            if r["exit"] != xname or r["N"] != N:
                continue
            if deep is not None and r["deep"] != deep:
                continue
            if yr == "HOLD" and r["split"] != "HOLD":
                continue
            if yr not in ("ALL", "HOLD") and r["year"] != yr:
                continue
            out.append(r["pc"])
        return out

    for xname in EXITS:
        print("=" * 104)
        print(f"EXIT {xname}   (per-contract $, net friction; +ve = the subset PAYS as a long call)")
        print(f"  {'strike':7s} " + " ".join(f"{y:>13s}" for y in YEARS) + "   win%(ALL)")
        for N in STRIKES:
            cells = []
            for yr in YEARS:
                m, t, n = cl(sel(xname, N, yr))
                cells.append(f"{m:+6.1f}(t{t:+4.1f})" if n >= 2 else f"{'·':>13s}")
            pcs = sel(xname, N, 'ALL')
            w = 100.0 * np.mean([p > 0 for p in pcs]) if pcs else float('nan')
            print(f"  +{N:<5d} " + " ".join(f"{c:>13s}" for c in cells) + f"   {w:4.0f}%")
        # deep-oversold subset (ALL years) at each strike, hold30-style contrast
        print("  deepOS-only (ALL): " + "  ".join(
            f"+{N}:{cl(sel(xname, N, 'ALL', deep=True))[0]:+.0f}(n{len(sel(xname, N, 'ALL', deep=True))})"
            for N in STRIKES))
        print()
    print("=" * 104)
    print("READ: +ve $/ct with |t|>~2 at some strike = the selective fade is a REAL long-call edge.")
    print("  vs full-fade long call which lost -$20..-$68/ct. deepOS line = the tightest subset.")
    print("=" * 104)


if __name__ == "__main__":
    main()
