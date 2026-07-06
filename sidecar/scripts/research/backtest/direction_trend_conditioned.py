"""TASK: is the PUT-FADE (aggressive 0DTE put buying -> SPX rises) a GENUINE capitulation
DIP-BUY, or just bull-market drift?

direction_confluence_realspot.py found (371 sess, real spot, both yrs+holdout): fading aggressive
put flow -> price rises ~59% overall, ~65-67% in expanding vol (+4.9pt/30min, t=-10). But the whole
2025-26 sample is a bull/chop epoch, so "fade puts -> up" might just be "buy any dip in an uptrend."

DISCRIMINATOR: condition each surge point on the LOCAL TREND at entry = sign(spot_now - spot_30min_ago)
on REAL spot. If the put-fade still wins when local trend is DOWN (price bounces up off a falling tape),
it's a real capitulation/mean-reversion edge. If it only wins when trend is already UP, it's drift.
Also reports AMBIENT forward move (all points) by trend = the baseline drift to beat.

hit% = price went the PUT's way (DOWN); LOW hit% = the fade wins. fav<0 = fade profitable.
Session-clustered (N_eff=#sess), by year + holdout. Read-only DB + network-read API.
Run: .venv/Scripts/python.exe -m scripts.research.backtest.direction_trend_conditioned [START END]
"""
from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

from .data import connect
from .direction_confluence_realspot import surges, trail_range
from .directional_validate_0dte_netdrift import _api, _post
from .holdout import HOLDOUT_START
from .thetadata_real_spot_validate import (
    MOVE_GLITCH_FRAC, _et_h, real_at, real_series,
)

sys.stdout.reconfigure(encoding="utf-8")

HORIZONS = [("+10min", 10), ("+30min", 30)]
TRAIL_MIN = 15
TREND_MIN = 30


def clustered_by_session(rr, key, center=0.0):
    per = defaultdict(list)
    for r in rr:
        per[r["date"]].append(r[key])
    sm = np.array([np.mean(v) for v in per.values()], float)
    n = len(sm)
    if n == 0:
        return None
    m = float(sm.mean())
    if n < 2 or sm.std(ddof=1) == 0:
        return m, float("nan"), n
    return m, float((m - center) / (sm.std(ddof=1) / np.sqrt(n))), n


def line(recs, filt, label):
    rr = [r for r in recs if filt(r)]
    if not rr:
        print(f"    {label:20s}   (no points)")
        return
    hit = clustered_by_session([{**r, "h100": 100.0 * r["hit"]} for r in rr], "h100", 50.0)
    fav = clustered_by_session(rr, "fav", 0.0)
    amb = clustered_by_session(rr, "mv", 0.0)
    print(f"    {label:20s} {hit[2]:4d}s {len(rr):5d}p  hit={hit[0]:5.1f}% (t{hit[1]:+5.2f})  "
          f"fav={fav[0]:+6.3f}pt (t{fav[1]:+5.2f})  ambientMove={amb[0]:+6.3f}pt")


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute "
        "WHERE session_date BETWEEN ? AND ? ORDER BY session_date", (start, end))]

    print("=" * 100)
    print("PUT-FADE x LOCAL TREND on REAL spot — capitulation dip-buy, or just bull drift?")
    print(f"  {start}..{end} ({len(dates)} sess); trend = sign(spot - spot[-{TREND_MIN}min]); REAL spot")
    print("  hit% = price went PUT's way (down); LOW hit = fade wins; fav<0 = fade profitable")
    print("=" * 100)

    recs = []
    n_sess, n_fail = 0, 0
    with _api() as cli:
        for k, d in enumerate(dates):
            sg = surges(con, d)
            if not sg:
                continue
            data = _post(cli, {"sessionDate": d, "filter": {"ticker": "SPX", "expirationDate": d}})
            if not data:
                n_fail += 1
                continue
            rs = real_series(data)
            if rs is None:
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
            if (k + 1) % 25 == 0:
                print(f"  ...{k+1}/{len(dates)} ({d}) sess_used={n_sess}", flush=True)

    print(f"\n  usable sessions: {n_sess}  api_fail={n_fail}  surge-points: {len(recs)//2}\n")

    GROUPS = [("ALL", lambda r: True), ("2025", lambda r: r["year"] == "2025"),
              ("2026", lambda r: r["year"] == "2026"), ("HOLDOUT", lambda r: r["split"] == "HOLD")]
    for hname, _ in HORIZONS:
        print("=" * 100)
        print(f"HORIZON {hname}")
        for glabel, gp in GROUPS:
            b = lambda r, gp=gp, hn=hname: gp(r) and r["h"] == hn
            print(f"  --- {glabel} ---")
            for tr in ("TUP", "TDN"):
                line(recs, lambda r, b=b, t=tr: b(r) and r["trend"] == t, f"AMBIENT/{tr} (all)")
            for tr in ("TUP", "TDN"):
                line(recs, lambda r, b=b, t=tr: b(r) and r["side"] == "PUT" and r["trend"] == t, f"PUT/{tr}")
            for tr in ("TUP", "TDN"):
                line(recs, lambda r, b=b, t=tr: b(r) and r["side"] == "PUT" and r["vol"] == "EXPAND"
                     and r["trend"] == t, f"PUTxEXPAND/{tr}")
            for tr in ("TUP", "TDN"):
                line(recs, lambda r, b=b, t=tr: b(r) and r["side"] == "CALL" and r["trend"] == t, f"CALL/{tr}")
        print()
    print("=" * 100)
    print("VERDICT: PUTxEXPAND fade wins (low hit / fav<0) on BOTH TUP and TDN -> genuine capitulation")
    print("  edge. Wins only on TUP (and AMBIENT/TUP already strongly +) -> just bull dip-buy drift.")
    print("=" * 100)


if __name__ == "__main__":
    main()
