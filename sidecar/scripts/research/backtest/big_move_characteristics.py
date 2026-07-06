"""WHAT PRECEDES a big (>5pt / >8pt) 30-min move? (magnitude, not direction) — the tradeable question.

You trade 5-8pt moves; direction you read off the chart. So the useful edge is knowing WHEN a big move
is likely. This samples every 5 min across 2022-2026 (IBKR SPX ruler) and measures the forward 30-min
|move| (UNSIGNED), then buckets it by observable conditions to find the big-move ENVIRONMENTS:
  TOD        - hour of day (ET)
  TRAIL_RNG  - trailing 30-min realized range (vol state), quartiles
  REGIME     - dealer gamma: SHORT_GAMMA (amplifies) vs LONG_GAMMA (dampens)   [2022..2025-08]
  FLIP_DIST  - |spot - gamma_flip|: near the flip = unstable                    [2022..2025-08]
  IV_ATM     - ATM implied vol level, quartiles                                 [2022..2025-08]
  FLOW_SURGE - is there a 0DTE aggressor flow surge (|5-roll net| >= p90)?
Report per bucket: N, median |move|, P(|move|>5pt), P(|move|>8pt). High P(>5/8) = trade-here windows.
Read-only. Run:  .venv/Scripts/python.exe -m scripts.research.backtest.big_move_characteristics [START END]
"""
from __future__ import annotations

import datetime as dt
import sys

import numpy as np

from .data import connect
from .ibkr_ruler import ibkr_open, ibkr_series
from .thetadata_real_spot_validate import MOVE_GLITCH_FRAC, _roll, real_at

sys.stdout.reconfigure(encoding="utf-8")
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HORIZON = 30
SAMPLE_MIN = 5
WIN_LO, WIN_HI = 9.5833, 15.5      # 09:35 .. 15:30 ET (fuller RTH for magnitude)
TRAIL_MIN = 30


def dealer(con, d):
    rows = con.execute("SELECT ts_ms, spot, gamma_flip, iv_atm, regime FROM option_dealer_exposure "
                       "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
    if not rows:
        return None
    return (np.array([r[0] for r in rows], np.int64),
            [(r[1], r[2], r[3], r[4]) for r in rows])


def d_at(de, t):
    if de is None:
        return None
    ts, vals = de
    j = int(np.searchsorted(ts, t)) - 1
    return vals[j] if 0 <= j < len(vals) else None


def trail_range(rt, rp, t0, t1):
    lo = int(np.searchsorted(rt, t0, side="right"))
    hi = int(np.searchsorted(rt, t1, side="right"))
    seg = rp[lo:hi]
    return float(seg.max() - seg.min()) if len(seg) >= 2 else None


def eth(t):
    x = dt.datetime.fromtimestamp(int(t) / 1000, ET)
    return x.hour + x.minute / 60.0


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2022-01-03"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    ic = ibkr_open()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 96)
    print("BIG-MOVE CHARACTERISTICS — what precedes a >5pt / >8pt 30-min move? (SPX, 2022-2026)")
    print(f"  {start}..{end} ({len(dates)} sess); sampled every {SAMPLE_MIN}min in ET 09:35-15:30")
    print("=" * 96)

    recs = []
    for k, d in enumerate(dates):
        rs = ibkr_series(ic, d)
        if rs is None:
            continue
        rt, rp = rs
        de = dealer(con, d)
        frows = con.execute("SELECT ts_ms, net_signed_prem FROM option_flow_minute "
                            "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
        if len(frows) >= 20:
            fts = np.array([r[0] for r in frows], np.int64)
            froll = _roll(np.array([r[1] for r in frows], float))
            fabs = np.abs(froll[~np.isnan(froll)])
            fp90 = np.percentile(fabs, 90) if len(fabs) else np.inf
        else:
            fts = None
        last = None
        for i in range(len(rt)):
            t = int(rt[i])
            h = eth(t)
            if h < WIN_LO or h > WIN_HI:
                continue
            if last is not None and t - last < SAMPLE_MIN * 60_000:
                continue
            last = t
            s0 = rp[i]
            sH = real_at(rt, rp, t + HORIZON * 60_000)
            if sH is None or abs(sH - s0) > MOVE_GLITCH_FRAC * s0:
                continue
            amove = abs(sH - s0)
            dv = d_at(de, t)
            surge = None
            if fts is not None:
                j = int(np.searchsorted(fts, t)) - 1
                if 0 <= j < len(froll) and not np.isnan(froll[j]):
                    surge = abs(froll[j]) >= fp90
            recs.append(dict(
                amove=amove, hour=int(h),
                trail=trail_range(rt, rp, t - TRAIL_MIN * 60_000, t),
                regime=(dv[3] if dv else None),
                flip=(abs(s0 - dv[1]) if (dv and dv[1]) else None),
                iv=(dv[2] if dv else None),
                surge=surge))
        if (k + 1) % 200 == 0:
            print(f"  ...{k+1}/{len(dates)} ({d})", flush=True)

    A = np.array([r["amove"] for r in recs], float)
    print(f"\n  samples: {len(recs)}   baseline: median|move|={np.median(A):.2f}pt  "
          f"P(>5)={100*np.mean(A>5):.1f}%  P(>8)={100*np.mean(A>8):.1f}%\n")

    def rep(title, keyfn, order=None):
        groups = {}
        for r in recs:
            kb = keyfn(r)
            if kb is None:
                continue
            groups.setdefault(kb, []).append(r["amove"])
        print(f"  -- by {title} --")
        keys = order if order else sorted(groups)
        for kb in keys:
            a = np.array(groups.get(kb, []), float)
            if len(a) < 100:
                continue
            print(f"    {str(kb):16s} n={len(a):6d}  med|mv|={np.median(a):5.2f}  "
                  f"P(>5)={100*np.mean(a>5):5.1f}%  P(>8)={100*np.mean(a>8):5.1f}%")

    def q(vals):
        v = np.array([x for x in vals if x is not None], float)
        return np.percentile(v, [25, 50, 75]) if len(v) else [0, 0, 0]

    tq = q([r["trail"] for r in recs])
    iq = q([r["iv"] for r in recs])
    fq = q([r["flip"] for r in recs])

    rep("HOUR (ET)", lambda r: r["hour"])
    rep("TRAIL-RANGE (vol) quartile", lambda r: None if r["trail"] is None else
        ("Q1 calm" if r["trail"] <= tq[0] else "Q2" if r["trail"] <= tq[1] else "Q3" if r["trail"] <= tq[2] else "Q4 wild"),
        ["Q1 calm", "Q2", "Q3", "Q4 wild"])
    rep("GAMMA regime", lambda r: r["regime"], ["LONG_GAMMA", "SHORT_GAMMA"])
    rep("|FLIP-DIST| quartile", lambda r: None if r["flip"] is None else
        ("near flip" if r["flip"] <= fq[0] else "q2" if r["flip"] <= fq[1] else "q3" if r["flip"] <= fq[2] else "far"),
        ["near flip", "q2", "q3", "far"])
    rep("IV-ATM quartile", lambda r: None if r["iv"] is None else
        ("Q1 low" if r["iv"] <= iq[0] else "Q2" if r["iv"] <= iq[1] else "Q3" if r["iv"] <= iq[2] else "Q4 high"),
        ["Q1 low", "Q2", "Q3", "Q4 high"])
    rep("FLOW surge", lambda r: None if r["surge"] is None else ("surge" if r["surge"] else "no surge"),
        ["surge", "no surge"])

    print("\n  -- best 2-way (n>=200) --")
    combos = {}
    for r in recs:
        if r["trail"] is None:
            continue
        wild = r["trail"] > tq[2]
        openclose = r["hour"] < 10 or r["hour"] >= 15
        sg = r["regime"] == "SHORT_GAMMA"
        for name, cond in [("wild+open/close", wild and openclose),
                           ("wild+shortGamma", wild and sg),
                           ("surge+wild", r["surge"] and wild),
                           ("shortGamma+open/close", sg and openclose)]:
            if cond:
                combos.setdefault(name, []).append(r["amove"])
    for name, fv in sorted(combos.items(), key=lambda x: -np.mean(np.array(x[1]) > 8) if len(x[1]) >= 200 else 0):
        a = np.array(fv, float)
        if len(a) < 200:
            continue
        print(f"    {name:22s} n={len(a):6d}  med|mv|={np.median(a):5.2f}  "
              f"P(>5)={100*np.mean(a>5):5.1f}%  P(>8)={100*np.mean(a>8):5.1f}%")
    print("\n" + "=" * 96)
    print("READ: buckets with high P(>5)/P(>8) = the environments to trade your setups; low = sit out.")
    print("=" * 96)


if __name__ == "__main__":
    main()
