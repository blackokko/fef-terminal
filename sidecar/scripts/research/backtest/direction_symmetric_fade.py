"""SYMMETRIC contrarian test: is the CALL-fade (buy PUT on aggressive CALL surge) an edge too,
mirroring the PUT-fade (buy CALL on put surge)? Answers 'why only calls / are puts also the way'.

Both sides get the SAME winning conditioners the put-fade used: EXPAND vol + big surge (|roll|/p90>=2)
+ price EXTENSION (puts: oversold = trailing-high - spot ; calls: overbought = spot - trailing-low).
  PUT surge  -> dir=+1 (fade UP,   buy call)
  CALL surge -> dir=-1 (fade DOWN, buy put)
Forward +30min on IBKR ruler; fav=dir*move; report N, hit%, meanFav, %big8(fav>+8) per SIDE per YEAR.
If CALL-fade ~ PUT-fade -> symmetric two-sided long system. If CALL-fade null -> only puts predict.
Read-only. Run:  .venv/Scripts/python.exe -m scripts.research.backtest.direction_symmetric_fade [START END]
"""
from __future__ import annotations

import sys

import numpy as np

from .big_bounce_subset import trail_high
from .data import connect
from .direction_confluence_realspot import trail_range
from .ibkr_ruler import ibkr_open, ibkr_series
from .thetadata_real_spot_validate import MOVE_GLITCH_FRAC, ROLL, WIN_HI, WIN_LO, _et_h, _roll, real_at

sys.stdout.reconfigure(encoding="utf-8")
HORIZON = 30
TRAIL_MIN = 15
BIG8 = 8.0
R_MIN = 2.0


def surges_both(con, d):
    """[(ts, side, r)]  side='PUT'(roll<0) / 'CALL'(roll>0), r=|roll|/session-p90."""
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
        if h < WIN_LO or h > WIN_HI or np.isnan(roll[i]) or absr[i] < p90:
            continue
        out.append((int(ts[i]), "PUT" if roll[i] < 0 else "CALL", float(absr[i] / p90)))
    return out


def trail_low(rt, rp, t0, t1):
    lo = int(np.searchsorted(rt, t0, side="right"))
    hi = int(np.searchsorted(rt, t1, side="right"))
    seg = rp[lo:hi]
    return float(seg.min()) if len(seg) else None


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 96)
    print("SYMMETRIC FADE — PUT-fade(buy call) vs CALL-fade(buy put), same conditioners, IBKR +30min")
    print(f"  {start}..{end} ({len(dates)} sess); subset = EXPAND + surge r>={R_MIN} + extension; big8=fav>+{BIG8}pt")
    print("=" * 96)

    recs = {}   # (side, year, subset) -> list of fav
    def add(side, year, subset, fav):
        recs.setdefault((side, year, subset), []).append(fav)

    for k, d in enumerate(dates):
        sg = surges_both(con, d)
        if not sg:
            continue
        rs = ibkr_series(ic, d)
        if rs is None:
            continue
        rt, rp = rs
        trs = [trail_range(rt, rp, ts - TRAIL_MIN * 60_000, ts) for ts, _, _ in sg]
        tr_med = float(np.median([t for t in trs if t is not None])) if any(t is not None for t in trs) else None
        year = d[:4]
        for (ts, side, r), tr in zip(sg, trs):
            s0 = real_at(rt, rp, ts)
            sH = real_at(rt, rp, ts + HORIZON * 60_000)
            if s0 is None or sH is None:
                continue
            mv = sH - s0
            if abs(mv) > MOVE_GLITCH_FRAC * s0:
                continue
            dirn = 1.0 if side == "PUT" else -1.0
            fav = dirn * mv
            expand = (tr is not None and tr_med is not None and tr > tr_med)
            if side == "PUT":
                th = trail_high(rt, rp, ts - 30 * 60_000, ts)
                ext = (th is not None and (th - s0) >= (tr or 0))
            else:
                tl = trail_low(rt, rp, ts - 30 * 60_000, ts)
                ext = (tl is not None and (s0 - tl) >= (tr or 0))
            subset = expand and (r >= R_MIN) and ext
            for tag in ("ALL", "SUBSET") if subset else ("ALL",):
                add(side, year, tag, fav)
                add(side, "ALL", tag, fav)

    YEARS = ["2022", "2023", "2024", "2025", "2026", "ALL"]
    for subset in ("ALL", "SUBSET"):
        for side in ("PUT", "CALL"):
            label = f"{side}-fade ({'buy call' if side == 'PUT' else 'buy put'})"
            print("\n" + "=" * 96)
            print(f"{label}   [{subset}]   (fade dir; hit>50 + fav>0 + high %big8 = edge)")
            print(f"  {'year':5s} {'#ent':>7s} {'hit%':>7s} {'meanFav':>8s} {'t':>6s} {'%big8':>7s}")
            for yr in YEARS:
                a = np.array(recs.get((side, yr, subset), []), float)
                if len(a) < 20:
                    continue
                hit = 100.0 * np.mean(a > 0)
                m = float(a.mean())
                t = m / (a.std(ddof=1) / np.sqrt(len(a))) if a.std(ddof=1) > 0 else float("nan")
                print(f"  {yr:5s} {len(a):7d} {hit:6.1f}% {m:+8.3f} {t:+6.2f} {100.0*np.mean(a>BIG8):6.1f}%")
    print("\n" + "=" * 96)
    print("READ: if CALL-fade [SUBSET] shows hit>>50 + fav>0 + %big8 like PUT-fade -> symmetric two-sided")
    print("  long edge (buy puts on call-euphoria too). If CALL-fade ~50/flat -> only put-flow is contrarian.")
    print("=" * 96)


if __name__ == "__main__":
    main()
