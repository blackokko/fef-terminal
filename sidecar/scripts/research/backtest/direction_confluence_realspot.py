"""TASK: does one-sided 0DTE aggression predict DIRECTION on REAL spot, CONDITIONAL on
a confluence factor (vol-expansion/contraction, time-of-day)?

WHY: the bare directional edge is falsified on real SPX (48%/-0.2pt over 370 sess). BUT every
confluence finding we have (keep ToD, keep IV-regime, GEX...) was computed on the parity-
RECONSTRUCTED spot = the artifact ruler. So we never learned whether call/put aggression predicts
direction in a SUB-REGIME on real price. This redoes the directional test on REAL spot
(QuantData stockPrice) and BUCKETS it by confluence, hunting for a regime where it survives.

SIGNAL (validated def): option_flow_minute.net_signed_prem, 5-row trailing sum; surge =
  |roll| >= per-session p90; ET 09:45-15:00; dir = sign(roll)  (>0 = call-dominant aggression).
OUTCOME: forward SIGNED SPX move on REAL spot, fav = dir*move, hit = fav>0.  +10/+30 min.
BUCKETS (each surge point):
  SIDE : CALL (dir>0)  /  PUT (dir<0)            -- does call-buying->up, put-buying->down hold?
  VOL  : EXPAND / CONTRACT  -- trailing 15-min realized range on real spot vs per-session median
  TOD  : OPEN(<11) / MID(11-14) / LATE(>=14) ET
Session-clustered (N_eff = #sessions) within each bucket, by year + DEV/HOLDOUT, w/ random control.
A real conditional edge = a bucket with hit clearly >50% + fav>0 + t>~2 in BOTH years & holdout.

Read-only DB + network-read API. No engine/live code touched.
Run: .venv/Scripts/python.exe -m scripts.research.backtest.direction_confluence_realspot [START END]
"""
from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

from .data import Session, connect
from .directional_validate_0dte_netdrift import _api, _post
from .holdout import HOLDOUT_START
from .thetadata_real_spot_validate import (
    MOVE_GLITCH_FRAC, ROLL, SANE_HI, SANE_LO, SURGE_Q, WIN_HI, WIN_LO,
    _et_h, _roll, real_at, real_series,
)

sys.stdout.reconfigure(encoding="utf-8")

HORIZONS = [("+10min", 10), ("+30min", 30)]
TRAIL_MIN = 15
RNG = np.random.default_rng(0)


def surges(con, d):
    rows = con.execute(
        "SELECT ts_ms, net_signed_prem FROM option_flow_minute "
        "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
    if len(rows) < 20:
        return []
    ts = np.array([r[0] for r in rows], np.int64)
    net = np.array([r[1] for r in rows], float)
    roll = _roll(net)
    absr = np.abs(roll)
    if np.isnan(absr[ROLL - 1:]).all() or len(absr) - (ROLL - 1) < 10:
        return []
    p90 = np.percentile(absr[ROLL - 1:], SURGE_Q)
    out = []
    for i in range(ROLL - 1, len(ts)):
        h = _et_h(int(ts[i]))
        if h < WIN_LO or h > WIN_HI or absr[i] < p90:
            continue
        out.append((int(ts[i]), 1.0 if roll[i] > 0 else -1.0))
    return out


def trail_range(rt, rp, t0, t1):
    lo = int(np.searchsorted(rt, t0, side="right"))
    hi = int(np.searchsorted(rt, t1, side="right"))
    seg = rp[lo:hi]
    seg = seg[(seg >= SANE_LO) & (seg <= SANE_HI)]
    return float(seg.max() - seg.min()) if len(seg) >= 2 else None


def tod_bucket(h):
    return "OPEN" if h < 11 else ("MID" if h < 14 else "LATE")


def clustered_by_session(rr, key):
    """rr: list of point-records. Cluster by date -> per-session mean of key -> stats over sessions."""
    per = defaultdict(list)
    for r in rr:
        per[r["date"]].append(r[key])
    sess_means = np.array([np.mean(v) for v in per.values()], float)
    n = len(sess_means)
    if n == 0:
        return None
    m = float(sess_means.mean())
    if n < 2 or sess_means.std(ddof=1) == 0:
        return m, float("nan"), n
    se = sess_means.std(ddof=1) / np.sqrt(n)
    return m, float((m - (50.0 if key == "hit" else 0.0)) / se), n


def report(recs, filt, label):
    rr = [r for r in recs if filt(r)]
    if not rr:
        return
    fav = clustered_by_session(rr, "fav")
    hitr = clustered_by_session([{**r, "hit": 100.0 * r["hit"]} for r in rr], "hit")
    rnd = float(np.mean([r["rnd"] for r in rr]))
    nsurge = len(rr)
    print(f"    {label:22s} {fav[2]:5d}s {nsurge:6d}p  hit={hitr[0]:5.1f}% (t{hitr[1]:+5.2f})  "
          f"fav={fav[0]:+6.3f}pt (t{fav[1]:+5.2f})  rnd={rnd:+6.3f}")


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute "
        "WHERE session_date BETWEEN ? AND ? ORDER BY session_date", (start, end))]

    print("=" * 104)
    print("DIRECTION x CONFLUENCE on REAL spot — does call/put aggression call direction in a sub-regime?")
    print(f"  {start}..{end} ({len(dates)} sessions); signal=net_signed_prem p{SURGE_Q} surge; REAL spot")
    print("=" * 104)

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
            # first pass: trailing range at each surge -> per-session median for EXPAND/CONTRACT
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
                    fav = dirn * mv
                    coin = RNG.random() < 0.5
                    recs.append(dict(
                        date=d, year=year, split=split, h=hname,
                        side=("CALL" if dirn > 0 else "PUT"),
                        vol=("EXPAND" if (tr is not None and tr > tr_med) else "CONTRACT"),
                        tod=tod_bucket(_et_h(ts_i)),
                        fav=float(fav), hit=1.0 if fav > 0 else 0.0,
                        rnd=float(mv if coin else -mv)))
                    used = True
            if used:
                n_sess += 1
            if (k + 1) % 25 == 0:
                print(f"  ...{k+1}/{len(dates)} ({d}) sess_used={n_sess}", flush=True)

    print(f"\n  usable sessions: {n_sess}   api_fail={n_fail}   surge-points: {len(recs)//2}\n")

    GROUPS = [("ALL", lambda r: True), ("2025", lambda r: r["year"] == "2025"),
              ("2026", lambda r: r["year"] == "2026"), ("HOLDOUT", lambda r: r["split"] == "HOLD")]
    for hname, _ in HORIZONS:
        print("=" * 104)
        print(f"HORIZON {hname}   (hit>50% + fav>0 + |t|>~2 = directional edge; rnd≈control)")
        for glabel, gp in GROUPS:
            base = lambda r, gp=gp, hn=hname: gp(r) and r["h"] == hn
            print(f"  --- {glabel} ---")
            report(recs, lambda r, b=base: b(r), "overall")
            for side in ("CALL", "PUT"):
                report(recs, lambda r, b=base, s=side: b(r) and r["side"] == s, f"side={side}")
            for vol in ("EXPAND", "CONTRACT"):
                report(recs, lambda r, b=base, v=vol: b(r) and r["vol"] == v, f"vol={vol}")
            for tod in ("OPEN", "MID", "LATE"):
                report(recs, lambda r, b=base, t=tod: b(r) and r["tod"] == t, f"tod={tod}")
            # the most-promising cross: side x vol
            for side in ("CALL", "PUT"):
                for vol in ("EXPAND", "CONTRACT"):
                    report(recs, lambda r, b=base, s=side, v=vol: b(r) and r["side"] == s and r["vol"] == v,
                           f"{side}x{vol}")
        print()
    print("=" * 104)


if __name__ == "__main__":
    main()
