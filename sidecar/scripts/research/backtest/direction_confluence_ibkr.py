"""COMPREHENSIVE (2022-2026, INDEPENDENT IBKR ruler) re-run of direction x confluence.
Same buckets as direction_confluence_realspot.py (CALL/PUT, vol EXPAND/CONTRACT, ToD, side x vol)
but the forward-move ruler is IBKR SPX 1-min across the full history, reported BY YEAR.
hit>50% + fav>0 + |t|>~2 = directional edge; for PUT a LOW hit = the fade.
Run:  .venv/Scripts/python.exe -m scripts.research.backtest.direction_confluence_ibkr [START END]
"""
from __future__ import annotations

import sys

import numpy as np

from .data import connect
from .direction_confluence_realspot import RNG, report, surges, tod_bucket, trail_range
from .holdout import HOLDOUT_START
from .ibkr_ruler import ibkr_open, ibkr_series
from .thetadata_real_spot_validate import MOVE_GLITCH_FRAC, _et_h, real_at

sys.stdout.reconfigure(encoding="utf-8")
HORIZONS = [("+10min", 10), ("+30min", 30)]
TRAIL_MIN = 15
GROUPS = [("ALL", lambda r: True), ("2022", lambda r: r["year"] == "2022"),
          ("2023", lambda r: r["year"] == "2023"), ("2024", lambda r: r["year"] == "2024"),
          ("2025", lambda r: r["year"] == "2025"), ("2026", lambda r: r["year"] == "2026"),
          ("DEV", lambda r: r["split"] == "DEV"), ("HOLDOUT", lambda r: r["split"] == "HOLD")]


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 104)
    print("DIRECTION x CONFLUENCE — INDEPENDENT IBKR SPX ruler, 2022-2026 BY YEAR")
    print(f"  {start}..{end} ({len(dates)} sess); signal=net_signed_prem p90 surge")
    print("=" * 104)

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
        pts = [(ts_i, dirn, trail_range(rt, rp, ts_i - TRAIL_MIN * 60_000, ts_i)) for ts_i, dirn in sg]
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
                recs.append(dict(
                    date=d, year=year, split=split, h=hname,
                    side=("CALL" if dirn > 0 else "PUT"),
                    vol=("EXPAND" if (tr is not None and tr > tr_med) else "CONTRACT"),
                    tod=tod_bucket(_et_h(ts_i)),
                    fav=float(dirn * mv), hit=1.0 if (dirn * mv) > 0 else 0.0,
                    rnd=float(mv if (RNG.random() < 0.5) else -mv)))
                used = True
        if used:
            n_sess += 1
        if (k + 1) % 100 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d}) sess={n_sess} no_ibkr={n_noib}", flush=True)

    print(f"\n  usable sessions: {n_sess}  missing IBKR ruler: {n_noib}  surge-points: {len(recs)//2}\n")

    for hname, _ in HORIZONS:
        print("=" * 104)
        print(f"HORIZON {hname}")
        for glabel, gp in GROUPS:
            base = lambda r, gp=gp, hn=hname: gp(r) and r["h"] == hn
            print(f"  --- {glabel} ---")
            report(recs, lambda r, b=base: b(r), "overall")
            for side in ("CALL", "PUT"):
                report(recs, lambda r, b=base, s=side: b(r) and r["side"] == s, f"side={side}")
            for vol in ("EXPAND", "CONTRACT"):
                report(recs, lambda r, b=base, v=vol: b(r) and r["vol"] == v, f"vol={vol}")
            for side in ("CALL", "PUT"):
                for vol in ("EXPAND", "CONTRACT"):
                    report(recs, lambda r, b=base, s=side, v=vol: b(r) and r["side"] == s and r["vol"] == v,
                           f"{side}x{vol}")
        print()
    print("=" * 104)


if __name__ == "__main__":
    main()
