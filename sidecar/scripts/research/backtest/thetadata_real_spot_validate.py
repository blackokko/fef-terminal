"""TASK B (DECISIVE): is the ThetaData flow-surge DIRECTIONAL edge REAL, or a
spot-RECONSTRUCTION ARTIFACT?

We measure the forward SPX move at the SAME ThetaData surge points TWO ways:
  (a) parity-RECONSTRUCTED spot  (data.Session.spot — exactly what the ORIGINAL
      ThetaData validation measured forward moves on), and
  (b) QuantData REAL stockPrice  (net-drift 0DTE API, per-ts stockPrice — ground truth).
over the 2025-01-02..2026-06-26 overlap (where BOTH ThetaData flow AND QuantData
stockPrice exist). For every horizon the two columns use the IDENTICAL surge-point set
(intersection: a point is scored only if both spots have a valid entry+forward reading),
so any divergence is purely the SPOT SOURCE — not the signal, timing, or sample.

SIGNAL  (unchanged from the validated def — confluence_test.py / flow_monetize_test.py):
  option_flow_minute.net_signed_prem ; 5-row trailing rolling sum ; surge = |roll| >=
  per-session p90 of |roll| ; decision window ET 09:45-15:00 ; dir = sign(roll).
  Every surge MINUTE in the window is scored (no every-3rd-minute sub-sampling — that is a
  monetization entry-spacing device, irrelevant to an exit-independent directional measure).

STATS  : direction hit-rate + mean favorable move, SESSION-CLUSTERED (the session is the
  unit of inference, N_eff = #sessions), reported BY YEAR (2025 / 2026) and by DEV/HOLDOUT
  split, with opposite-direction and random-direction controls at the same points.

VERDICT:
  REAL      -> real-spot hit ~= reconstructed-spot hit (~54-55%, +~1pt, significant t).
               => QuantData live failure is signal-FIDELITY -> live ThetaData worth building.
  ARTIFACT  -> real-spot collapses toward ~50% / ~0pt while reconstructed still shows +1pt.
               => the original edge was substantially a spot-reconstruction artifact -> stop.

Read-only DB + network-read API. No engine/live code touched.
Run:  .venv/Scripts/python.exe -m scripts.research.backtest.thetadata_real_spot_validate [START END]
"""
from __future__ import annotations

import datetime as dt
import sys

import numpy as np

from .data import ET, Session, connect
from .directional_validate_0dte_netdrift import _api, _post, _rth
from .holdout import HOLDOUT_START

sys.stdout.reconfigure(encoding="utf-8")

ROLL = 5
SURGE_Q = 90
WIN_LO, WIN_HI = 9.75, 15.0          # decision window, ET hours (09:45..15:00)
HORIZONS = [("+10min", 10), ("+30min", 30), ("EOD", None)]
TOL_MS = 120_000                     # nearest-observation match tolerance (both sources)
SANE_LO, SANE_HI = 3000.0, 9000.0    # absolute SPX sanity band (2025-26 ~5500-6900)
MOVE_GLITCH_FRAC = 0.05              # drop a reading whose |move|/spot0 > 5% (spot glitch)
SPX_BAND = 0.05                      # real-spot per-session median band (glitch filter)
RNG = np.random.default_rng(0)


def _et_h(ts_ms):
    x = dt.datetime.fromtimestamp(ts_ms / 1000, ET)
    return x.hour + x.minute / 60.0


def _roll(x, w=ROLL):
    out = np.full(len(x), np.nan)
    c = np.cumsum(x)
    out[w - 1:] = c[w - 1:] - np.concatenate(([0.0], c[:-w]))
    return out


def surges(con, d):
    """The VALIDATED ThetaData surge points for session d: list of (ts_ms, dir)."""
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
        if h < WIN_LO or h > WIN_HI:
            continue
        if absr[i] < p90:
            continue
        out.append((int(ts[i]), 1.0 if roll[i] > 0 else -1.0))
    return out


def real_series(data):
    """(ts_ms[], stockPrice[]) RTH, glitch-filtered (median band). None if too sparse."""
    pts = []
    for ts, v in data.items():
        if not isinstance(v, dict) or not _rth(ts):
            continue
        sp = float(v.get("stockPrice") or 0)
        if sp > 1000:
            pts.append((int(ts), sp))
    if len(pts) < 30:
        return None
    pts.sort()
    rt = np.array([p[0] for p in pts], np.int64)
    rp = np.array([p[1] for p in pts], float)
    med = np.median(rp)
    mask = np.abs(rp - med) <= SPX_BAND * med
    rt, rp = rt[mask], rp[mask]
    return (rt, rp) if len(rt) >= 30 else None


def _nearest(ts_arr, target, tol=TOL_MS):
    if len(ts_arr) == 0:
        return None
    j = int(np.searchsorted(ts_arr, target))
    best, bd = None, tol + 1
    for c in (j - 1, j):
        if 0 <= c < len(ts_arr):
            dd = abs(int(ts_arr[c]) - target)
            if dd <= tol and dd < bd:
                bd, best = dd, c
    return best


def recon_at(sess, mins, target):
    idx = _nearest(mins, target)
    if idx is None:
        return None
    s = sess.spot(int(mins[idx]))
    return s if (s is not None and SANE_LO <= s <= SANE_HI) else None


def real_at(rt, rp, target):
    idx = _nearest(rt, target)
    if idx is None:
        return None
    s = float(rp[idx])
    return s if SANE_LO <= s <= SANE_HI else None


def recon_eod(sess):
    for m in reversed(sess.minutes):
        s = sess.spot(m)
        if s is not None and SANE_LO <= s <= SANE_HI:
            return s
    return None


# -------------------------------------------------------------------------- stats
def clustered(vals):
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], float)
    n = len(a)
    if n < 2:
        return (float(a.mean()) if n else float("nan")), float("nan"), n
    se = a.std(ddof=1) / np.sqrt(n)
    return float(a.mean()), (float(a.mean() / se) if se > 0 else float("nan")), n


def row_stats(recs, h, src, pred):
    rr = [r for r in recs if r["h"] == h and r["src"] == src and pred(r)]
    if not rr:
        return None
    fmean, ft, fn = clustered([r["favmean"] for r in rr])
    ha = np.array([100.0 * r["hitfrac"] for r in rr], float)
    hmean = float(ha.mean())
    ht = ((hmean - 50.0) / (ha.std(ddof=1) / np.sqrt(len(ha)))) if len(ha) > 1 and ha.std(ddof=1) > 0 else float("nan")
    return dict(nsess=fn, nsurge=sum(r["n"] for r in rr), hit=hmean, fav=fmean,
                tfav=ft, thit=ht, opp=float(np.mean([r["oppmean"] for r in rr])),
                rnd=float(np.mean([r["rndmean"] for r in rr])))


GROUPS = [
    ("2025", lambda r: r["year"] == "2025"),
    ("2026", lambda r: r["year"] == "2026"),
    ("ALL", lambda r: True),
    ("DEV<8/13", lambda r: r["split"] == "DEV"),
    ("HOLDOUT", lambda r: r["split"] == "HOLD"),
]


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute "
        "WHERE session_date BETWEEN ? AND ? ORDER BY session_date", (start, end))]

    print("=" * 104)
    print("TASK B — ThetaData flow-surge DIRECTION: reconstructed spot  vs  REAL QuantData spot")
    print(f"  overlap {start}..{end}  ({len(dates)} candidate ThetaData sessions)")
    print(f"  signal: 5-row Σnet_signed_prem, |roll|>=p{SURGE_Q}, ET {WIN_LO}-{WIN_HI}, dir=sign(roll)")
    print(f"  identical surge points per horizon (intersection); session-clustered (N_eff=#sess)")
    print("=" * 104)

    recs = []
    n_sess_used = 0
    n_api_fail = 0
    cand_pts = 0
    meas_pts = {h[0]: 0 for h in HORIZONS}

    with _api() as c:
        for k, d in enumerate(dates):
            sg = surges(con, d)
            if not sg:
                continue
            data = _post(c, {"sessionDate": d, "filter": {"ticker": "SPX", "expirationDate": d}})
            if not data:
                n_api_fail += 1
                continue
            rs = real_series(data)
            if rs is None:
                continue
            rt, rp = rs
            sess = Session(con, d)
            if sess.empty():
                continue
            mins = np.array(sess.minutes, np.int64)
            year, split = d[:4], ("DEV" if d < HOLDOUT_START else "HOLD")
            r_eod = recon_eod(sess)
            q_eod = float(rp[-1])
            cand_pts += len(sg)
            used = False
            for hname, hmin in HORIZONS:
                fav_r, hit_r, opp_r, rnd_r = [], [], [], []
                fav_q, hit_q, opp_q, rnd_q = [], [], [], []
                for ts_i, dirn in sg:
                    s0r = recon_at(sess, mins, ts_i)
                    s0q = real_at(rt, rp, ts_i)
                    if hmin is None:
                        sHr, sHq = r_eod, q_eod
                    else:
                        tH = ts_i + hmin * 60_000
                        sHr = recon_at(sess, mins, tH)
                        sHq = real_at(rt, rp, tH)
                    if s0r is None or s0q is None or sHr is None or sHq is None:
                        continue
                    mvr, mvq = sHr - s0r, sHq - s0q
                    if abs(mvr) > MOVE_GLITCH_FRAC * s0r or abs(mvq) > MOVE_GLITCH_FRAC * s0q:
                        continue
                    coin = RNG.random() < 0.5
                    fr, fq = dirn * mvr, dirn * mvq
                    fav_r.append(fr); hit_r.append(1.0 if fr > 0 else 0.0)
                    opp_r.append(-fr); rnd_r.append(mvr if coin else -mvr)
                    fav_q.append(fq); hit_q.append(1.0 if fq > 0 else 0.0)
                    opp_q.append(-fq); rnd_q.append(mvq if coin else -mvq)
                if fav_r:
                    meas_pts[hname] += len(fav_r)
                    recs.append(dict(date=d, year=year, split=split, h=hname, src="recon",
                                     favmean=float(np.mean(fav_r)), hitfrac=float(np.mean(hit_r)),
                                     oppmean=float(np.mean(opp_r)), rndmean=float(np.mean(rnd_r)),
                                     n=len(fav_r)))
                    recs.append(dict(date=d, year=year, split=split, h=hname, src="real",
                                     favmean=float(np.mean(fav_q)), hitfrac=float(np.mean(hit_q)),
                                     oppmean=float(np.mean(opp_q)), rndmean=float(np.mean(rnd_q)),
                                     n=len(fav_q)))
                    used = True
            if used:
                n_sess_used += 1
            if (k + 1) % 25 == 0:
                print(f"  ...{k+1}/{len(dates)} fetched ({d})  sess_used={n_sess_used}", flush=True)

    cov = (100.0 * meas_pts["+10min"] / cand_pts) if cand_pts else float("nan")
    print(f"\n  usable sessions: {n_sess_used}   candidate surge pts: {cand_pts}   "
          f"scored@+10min: {meas_pts['+10min']} ({cov:.1f}% coverage)   api_fail={n_api_fail}")

    # ---- detailed per-horizon table -----------------------------------------
    for hname, _ in HORIZONS:
        print("\n" + "=" * 104)
        print(f"HORIZON {hname}")
        print(f"  {'group':9s} {'src':6s} {'#sess':>5s} {'#surge':>6s} {'hit%':>6s} "
              f"{'favPt':>7s} {'t_fav':>6s} {'t_hit':>6s} {'oppPt':>7s} {'rndPt':>7s}")
        for glabel, gpred in GROUPS:
            for src in ("recon", "real"):
                s = row_stats(recs, hname, src, gpred)
                if not s:
                    continue
                print(f"  {glabel:9s} {src:6s} {s['nsess']:5d} {s['nsurge']:6d} {s['hit']:5.1f}% "
                      f"{s['fav']:+7.3f} {s['tfav']:+6.2f} {s['thit']:+6.2f} "
                      f"{s['opp']:+7.3f} {s['rnd']:+7.3f}")

    # ---- compact two-column verdict table -----------------------------------
    print("\n" + "=" * 104)
    print("TWO-COLUMN VERDICT TABLE  (session-clustered; same points per horizon)")
    print("  recon = parity-reconstructed spot (original test)   real = QuantData stockPrice (truth)")
    for hname, _ in HORIZONS:
        if hname == "EOD":
            continue
        print(f"\n  HORIZON {hname}")
        print(f"    {'group':9s} | {'recon hit%':>10s} {'recon favPt':>11s} {'recon t':>8s} "
              f"| {'real hit%':>9s} {'real favPt':>10s} {'real t':>7s}")
        for glabel in ("2025", "2026", "ALL", "DEV<8/13", "HOLDOUT"):
            gpred = dict(GROUPS)[glabel]
            sr = row_stats(recs, hname, "recon", gpred)
            sq = row_stats(recs, hname, "real", gpred)
            if not sr or not sq:
                continue
            print(f"    {glabel:9s} | {sr['hit']:9.1f}% {sr['fav']:+11.3f} {sr['tfav']:+8.2f} "
                  f"| {sq['hit']:8.1f}% {sq['fav']:+10.3f} {sq['tfav']:+7.2f}")

    print("\n" + "=" * 104)
    print("Benchmark (ThetaData, 1093 sess, reconstructed spot): ~54-55% hit, +~1pt favorable,")
    print("large session-clustered t. VERDICT: real-spot ~= recon  -> edge REAL (fidelity problem);")
    print("real-spot collapses to ~50% / ~0pt while recon stays +1pt  -> spot-reconstruction ARTIFACT.")
    print("=" * 104)


if __name__ == "__main__":
    main()
