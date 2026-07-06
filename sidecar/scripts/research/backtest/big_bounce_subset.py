"""BIG-BOUNCE SUBSET: which put-fades become BIG (>8pt) bounces you could trade as a LONG call?

The fade is the edge (long, dir=up) but its AVERAGE move is too small for a long 0DTE option (theta).
23% of fades make >+8pt. If a filter isolates a subset with much higher %big8, a long call on ONLY
those may clear theta. Condition each fade entry on:
  surge   - |roll|/session-p90 (surge intensity multiple)         q: <1.3 / 1.3-2 / 2-3 / >3
  vol     - trailing 15-min IBKR range vs session median          EXPAND / CONTRACT
  oversold- (trailing 30-min HIGH - spot0) on IBKR, quartiles     deeper = more capitulation
  tod     - OPEN(<11) / MID(11-14) / LATE(>=14) ET
  regime  - SHORT_GAMMA / LONG_GAMMA (dealer exposure)
Outcome = forward +30min move on IBKR (long). Report N, %big8, meanFav per bucket + best 2-way combos.
Read-only. Run:  .venv/Scripts/python.exe -m scripts.research.backtest.big_bounce_subset [START END]
"""
from __future__ import annotations

import sys

import numpy as np

from .data import connect
from .direction_confluence_realspot import trail_range
from .ibkr_ruler import ibkr_open, ibkr_series
from .mechanism_putfade import gex_at, gex_series
from .thetadata_real_spot_validate import MOVE_GLITCH_FRAC, ROLL, WIN_HI, WIN_LO, _et_h, _roll, real_at

sys.stdout.reconfigure(encoding="utf-8")
HORIZON = 30
TRAIL_MIN = 15
BIG8 = 8.0


def fade_entries(con, d):
    """put-dominant surges with intensity r=|roll|/p90. Returns [(ts, r)]."""
    rows = con.execute("SELECT ts_ms, net_signed_prem FROM option_flow_minute "
                       "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
    if len(rows) < 20:
        return []
    ts = np.array([r[0] for r in rows], np.int64)
    net = np.array([r[1] for r in rows], float)
    roll = _roll(net)
    absr = np.abs(roll)
    valid = absr[ROLL - 1:]
    valid = valid[~np.isnan(valid)]
    if len(valid) < 10:
        return []
    p90 = np.percentile(valid, 90)
    if p90 <= 0:
        return []
    out = []
    for i in range(ROLL - 1, len(ts)):
        h = _et_h(int(ts[i]))
        if h < WIN_LO or h > WIN_HI or np.isnan(roll[i]):
            continue
        if roll[i] < 0 and absr[i] >= p90:            # put-dominant surge
            out.append((int(ts[i]), float(absr[i] / p90)))
    return out


def trail_high(rt, rp, t0, t1):
    lo = int(np.searchsorted(rt, t0, side="right"))
    hi = int(np.searchsorted(rt, t1, side="right"))
    seg = rp[lo:hi]
    return float(seg.max()) if len(seg) else None


def bucket_report(recs, keyfn, title, order=None):
    groups = {}
    for r in recs:
        kb = keyfn(r)
        if kb is None:
            continue
        groups.setdefault(kb, []).append(r)
    keys = order if order else sorted(groups)
    print(f"  -- by {title} --")
    for kb in keys:
        rr = groups.get(kb, [])
        if len(rr) < 30:
            continue
        favs = np.array([r["fav"] for r in rr], float)
        big8 = 100.0 * np.mean(favs > BIG8)
        print(f"    {str(kb):16s} n={len(rr):5d}  %big8={big8:5.1f}  meanFav={favs.mean():+6.2f}  "
              f"hit={100.0*np.mean(favs>0):5.1f}%")


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 96)
    print("BIG-BOUNCE SUBSET of the put-fade (long) — which conditions make %big8 jump? (+30min, IBKR)")
    print(f"  {start}..{end} ({len(dates)} sess); baseline fade %big8~23%")
    print("=" * 96)

    recs = []
    for k, d in enumerate(dates):
        ents = fade_entries(con, d)
        if not ents:
            continue
        rs = ibkr_series(ic, d)
        if rs is None:
            continue
        rt, rp = rs
        g = gex_series(con, d)
        # session-median trailing range for EXPAND split
        trs = []
        for ts, _ in ents:
            tr = trail_range(rt, rp, ts - TRAIL_MIN * 60_000, ts)
            if tr is not None:
                trs.append(tr)
        tr_med = float(np.median(trs)) if trs else None
        for ts, r in ents:
            s0 = real_at(rt, rp, ts)
            sH = real_at(rt, rp, ts + HORIZON * 60_000)
            if s0 is None or sH is None:
                continue
            mv = sH - s0
            if abs(mv) > MOVE_GLITCH_FRAC * s0:
                continue
            tr = trail_range(rt, rp, ts - TRAIL_MIN * 60_000, ts)
            th = trail_high(rt, rp, ts - 30 * 60_000, ts)
            recs.append(dict(
                fav=float(mv), r=r,
                expand=(tr is not None and tr_med is not None and tr > tr_med),
                oversold=(th - s0 if th is not None else None),
                tod=("OPEN" if _et_h(ts) < 11 else ("MID" if _et_h(ts) < 14 else "LATE")),
                gex=gex_at(g, ts)))
        if (k + 1) % 200 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d})", flush=True)

    favs = np.array([r["fav"] for r in recs], float)
    print(f"\n  fade entries: {len(recs)}   baseline %big8={100.0*np.mean(favs>BIG8):.1f}  "
          f"meanFav={favs.mean():+.2f}\n")

    # oversold quartile thresholds (on positive depths)
    osv = np.array([r["oversold"] for r in recs if r["oversold"] is not None], float)
    oq = np.percentile(osv, [25, 50, 75]) if len(osv) else [0, 0, 0]

    def r_bucket(r):
        x = r["r"]
        return "r<1.3" if x < 1.3 else "r1.3-2" if x < 2 else "r2-3" if x < 3 else "r>3"

    def os_bucket(r):
        v = r["oversold"]
        if v is None:
            return None
        return ("os_q1" if v <= oq[0] else "os_q2" if v <= oq[1] else "os_q3" if v <= oq[2] else "os_q4(deep)")

    bucket_report(recs, r_bucket, "SURGE intensity", ["r<1.3", "r1.3-2", "r2-3", "r>3"])
    bucket_report(recs, lambda r: "EXPAND" if r["expand"] else "CONTRACT", "VOL state", ["EXPAND", "CONTRACT"])
    bucket_report(recs, os_bucket, "OVERSOLD depth", ["os_q1", "os_q2", "os_q3", "os_q4(deep)"])
    bucket_report(recs, lambda r: r["tod"], "TIME of day", ["OPEN", "MID", "LATE"])
    bucket_report(recs, lambda r: r["gex"], "GEX regime", ["LONG_GAMMA", "SHORT_GAMMA"])

    print("\n  -- best 2-way combos (n>=100) --")
    combos = {}
    for r in recs:
        osb = os_bucket(r)
        keys = [
            ("EXPAND+deepOS", r["expand"] and osb == "os_q4(deep)"),
            ("EXPAND+bigSurge", r["expand"] and r["r"] >= 2),
            ("deepOS+bigSurge", osb == "os_q4(deep)" and r["r"] >= 2),
            ("EXPAND+shortGamma", r["expand"] and r["gex"] == "SHORT_GAMMA"),
            ("deepOS+LATE", osb == "os_q4(deep)" and r["tod"] == "LATE"),
            ("EXPAND+deepOS+bigSurge", r["expand"] and osb == "os_q4(deep)" and r["r"] >= 2),
        ]
        for name, cond in keys:
            if cond:
                combos.setdefault(name, []).append(r["fav"])
    for name, fv in sorted(combos.items(), key=lambda x: -100.0 * np.mean(np.array(x[1]) > BIG8) if len(x[1]) >= 100 else 0):
        a = np.array(fv, float)
        if len(a) < 100:
            continue
        print(f"    {name:24s} n={len(a):5d}  %big8={100.0*np.mean(a>BIG8):5.1f}  meanFav={a.mean():+6.2f}  "
              f"hit={100.0*np.mean(a>0):5.1f}%")

    print("\n" + "=" * 96)
    print("READ: a subset with %big8 >> 23 (baseline) + hit>>50 is the LONG-CALL candidate to monetize.")
    print("=" * 96)


if __name__ == "__main__":
    main()
