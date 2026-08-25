# [REMOVED 2026-08-25] QuantData subscription CANCELLED. THIS SCRIPT NO LONGER
# RUNS: it imports fef_sidecar.quantdata, which is deleted, and it fetched from
# the QuantData REST API, which we no longer have credentials for. Kept as the
# provenance record of the study/backfill it performed (see RESEARCH_REGISTRY);
# delete it only as a deliberate archive decision, not as a cleanup.
"""DECISIVE directional re-validation of the flow-surge edge on the CORRECTED 0DTE signal.

WHY THIS SUPERSEDES directional_validate_quantdata.py
  That script used signal_iter.drift_cum_net_m = QuantData net-drift with NO expirationDate
  = ALL-EXPIRY, which source_equivalence/metric_match showed is ANTI-correlated (Spearman
  -0.32, 25% sign-agreement) with the validated ThetaData edge. Its "+0.67pt/+10min,
  reversal by +30min, underpowered" result was measured on the BACKWARDS signal -> void for
  the corrected engine.

THIS script re-runs the exit-independent directional test on the CORRECTED signal:
  net-drift with filter {ticker:SPX, expirationDate:<session 0DTE>}, drift = per-ts
  (netCallPremium - netPutPremium) [already aggressor-signed]. The response carries stockPrice
  per ts, so forward SPX returns are self-contained (no DB join). metric_match showed this
  signal is +0.40 Spearman / 73% sign-agreement with ThetaData — what the corrected engine fires.

COVERAGE: net-drift 0DTE serves 2025-01-02 .. present (2024 and earlier return empty). So this
  is a ~370-session, 2-regime (2025 full year + 2026 H1) test, reported BY YEAR — far stronger
  than the 7 live sessions. (ThetaData option_flow_minute supplies the trading-day calendar.)

TEST (same as directional_validate_quantdata.py, on the new signal)
  resample the ~1015-pt intraday series to the engine's 2-min cadence; cum drift = cumsum;
  ΔDrift = cum[i]-cum[i-3] (engine 3-iter). surge = |ΔDrift| >= per-session p90. dir=sign(ΔDrift).
  forward SPX move at +10min/+30min/EOD from net-drift stockPrice: (a) hit rate, (b) favorable pts.
  SESSION-CLUSTERED significance (N_eff=#sessions, session as the unit), BY YEAR. Controls:
  opposite-direction and random-direction at the same surge points.
  ALSO runs the all-expiry baseline (the backwards signal) through identical machinery -> floor.

BENCHMARK (ThetaData, 1093 sessions): ~54-55% hit, +~1pt favorable EVERY year, large t.

Read-only DB (calendar only) + network-read API. No engine/live code touched.
Run:  .venv/Scripts/python.exe -m scripts.research.backtest.directional_validate_0dte_netdrift [START END]
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from zoneinfo import ZoneInfo

import httpx
import numpy as np

from fef_sidecar.quantdata.config import load_config
from .data import connect

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")

HISTORY_WINDOW = 3
STEP_MIN = 2
SURGE_Q = 90
RTH_LO, RTH_HI = 9.5, 16.0
HORIZONS = [("+10min", 10), ("+30min", 30), ("EOD", None)]
TOL_MS = {10: 150_000, 30: 300_000}
MIN_BUCKETS = 60            # drop very short/incomplete sessions
GLITCH_CUM_M = 3000.0       # implausible cumulative drift -> corrupt session
RNG = np.random.default_rng(0)


def _api():
    cfg = load_config()
    return httpx.Client(
        base_url=cfg.base_url,
        headers={"Authorization": f"Bearer {cfg.auth_token}",
                 "Content-Type": "application/json", "accept": "application/json"},
        timeout=90.0)


def _post(c, body, retries=3):
    for a in range(retries):
        try:
            r = c.post("/options/tool/net-drift", json=body)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                return r.json().get("data")
            if r.status_code == 429:
                time.sleep(0.5 * (2 ** a)); continue
            return None
        except httpx.HTTPError:
            time.sleep(0.4 * (2 ** a))
    return None


def _rth(ts_ms):
    t = dt.datetime.fromtimestamp(int(ts_ms) / 1000, ET)
    return RTH_LO <= t.hour + t.minute / 60.0 < RTH_HI


def build_series(data):
    """Resample net-drift data to STEP_MIN buckets. Returns (bt, bnet, bspx) or None.
    bnet in $M per bucket; bspx = last sane stockPrice in bucket."""
    if not isinstance(data, dict):
        return None
    pts = []
    for ts, v in data.items():
        if not isinstance(v, dict) or not _rth(ts):
            continue
        net = float(v.get("netCallPremium") or 0) - float(v.get("netPutPremium") or 0)
        sp = float(v.get("stockPrice") or 0)
        pts.append((int(ts), net, sp))
    if len(pts) < MIN_BUCKETS:
        return None
    pts.sort()
    open_ms = pts[0][0]
    buck: dict[int, list] = {}
    for ts, net, sp in pts:
        bk = open_ms + ((ts - open_ms) // (STEP_MIN * 60000)) * (STEP_MIN * 60000)
        if bk not in buck:
            buck[bk] = [0.0, np.nan]
        buck[bk][0] += net
        if sp > 1000:                      # sane spx -> update last
            buck[bk][1] = sp
    bt = np.array(sorted(buck), np.int64)
    if len(bt) < MIN_BUCKETS:
        return None
    bnet = np.array([buck[b][0] for b in bt], float) / 1e6
    bspx = np.array([buck[b][1] for b in bt], float)
    # session glitch guards
    if abs(np.nansum(bnet)) > GLITCH_CUM_M:
        return None
    finite = bspx[np.isfinite(bspx)]
    if len(finite) < MIN_BUCKETS or finite.min() < 1000 or finite.max() / finite.min() > 1.3:
        return None
    return bt, bnet, bspx


def fwd(bt, bspx, i, hmin):
    if not np.isfinite(bspx[i]):
        return None, None
    if hmin is None:
        ok = np.where(np.isfinite(bspx))[0]
        return bspx[i], bspx[ok[-1]]
    target = bt[i] + hmin * 60000
    cand = np.where(np.isfinite(bspx) & (np.abs(bt - target) <= TOL_MS[hmin]))[0]
    if len(cand) == 0:
        return bspx[i], None
    j = cand[np.argmin(np.abs(bt[cand] - target))]
    return bspx[i], bspx[j]


def measure(series, hmin):
    """Returns per-session (mean_fav, hit_frac, mean_opp, mean_rnd, n_meas) or None."""
    bt, bnet, bspx = series
    cum = np.cumsum(bnet)
    delta = np.full(len(cum), np.nan)
    if len(cum) <= HISTORY_WINDOW:
        return None
    delta[HISTORY_WINDOW:] = cum[HISTORY_WINDOW:] - cum[:-HISTORY_WINDOW]
    valid = delta[~np.isnan(delta)]
    if len(valid) < 10:
        return None
    p90 = np.percentile(np.abs(valid), SURGE_Q)
    favs, hits, opps, rnds = [], [], [], []
    for i in range(len(delta)):
        if np.isnan(delta[i]) or abs(delta[i]) < p90:
            continue
        s0, sH = fwd(bt, bspx, i, hmin)
        if s0 is None or sH is None:
            continue
        mv = sH - s0
        dirn = 1.0 if delta[i] > 0 else -1.0
        fav = dirn * mv
        favs.append(fav); hits.append(1.0 if fav > 0 else 0.0)
        opps.append(-fav); rnds.append(mv if RNG.random() < 0.5 else -mv)
    if not favs:
        return None
    return (float(np.mean(favs)), float(np.mean(hits)),
            float(np.mean(opps)), float(np.mean(rnds)), len(favs))


def clustered(vals):
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], float)
    n = len(a)
    if n < 2:
        return (float(a.mean()) if n else float("nan")), float("nan"), float("nan"), n
    se = a.std(ddof=1) / np.sqrt(n)
    return float(a.mean()), float(se), (float(a.mean() / se) if se > 0 else float("nan")), n


def report(label, per_session):
    """per_session: list of (year, mean_fav, hit_frac, mean_opp, mean_rnd, n_meas)."""
    years = sorted({r[0] for r in per_session})
    print(f"\n  {label}")
    print(f"    {'group':9s} {'#sess':>5s} {'#surge':>6s} {'hit%':>6s} "
          f"{'favPt':>7s} {'t_fav':>6s} {'t_hit':>6s} {'oppPt':>7s} {'rndPt':>7s}")
    for grp in years + ["ALL"]:
        rows = per_session if grp == "ALL" else [r for r in per_session if r[0] == grp]
        if not rows:
            continue
        favs = [r[1] for r in rows]
        hits = [100.0 * r[2] for r in rows]
        opps = [r[3] for r in rows]
        rnds = [r[4] for r in rows]
        nsurge = sum(r[5] for r in rows)
        fmean, fse, ft, fn = clustered(favs)
        ha = np.array(hits, float)
        hmean = ha.mean()
        ht = ((hmean - 50.0) / (ha.std(ddof=1) / np.sqrt(len(ha)))) if len(ha) > 1 and ha.std(ddof=1) > 0 else float("nan")
        print(f"    {str(grp):9s} {fn:5d} {nsurge:6d} {hmean:5.1f}% "
              f"{fmean:+7.3f} {ft:+6.2f} {ht:+6.2f} {np.mean(opps):+7.3f} {np.mean(rnds):+7.3f}")


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 96)
    print("DECISIVE DIRECTIONAL RE-VALIDATION — corrected 0DTE net-drift signal")
    print(f"  calendar {start}..{end}  ({len(dates)} candidate sessions)")
    print(f"  surge=|3-iter ΔDrift|>=p{SURGE_Q} on {STEP_MIN}-min cadence; dir=sign(ΔDrift); "
          f"session-clustered, by year.")
    print("=" * 96)

    # collect per-session results for both signals at all horizons
    res = {sig: {h[0]: [] for h in HORIZONS} for sig in ("0DTE", "ALLEXP")}
    n_used = {"0DTE": 0, "ALLEXP": 0}
    n_glitch = 0
    with _api() as c:
        for k, d in enumerate(dates):
            year = d[:4]
            d0 = _post(c, {"sessionDate": d, "filter": {"ticker": "SPX", "expirationDate": d}})
            da = _post(c, {"sessionDate": d, "filter": {"ticker": "SPX"}})
            for sig, data in (("0DTE", d0), ("ALLEXP", da)):
                series = build_series(data) if data else None
                if series is None:
                    if sig == "0DTE":
                        n_glitch += 1
                    continue
                used = False
                for hname, hmin in HORIZONS:
                    m = measure(series, hmin)
                    if m is None:
                        continue
                    res[sig][hname].append((year, *m))
                    used = True
                if used:
                    n_used[sig] += 1
            if (k + 1) % 25 == 0:
                print(f"  ...{k+1}/{len(dates)} sessions fetched ({d})", flush=True)

    print(f"\n  usable sessions: 0DTE={n_used['0DTE']}  ALLEXP={n_used['ALLEXP']}  "
          f"(0DTE dropped/empty/glitch={n_glitch})")

    for sig in ("0DTE", "ALLEXP"):
        print("\n" + "=" * 96)
        print(f"SIGNAL = {sig}" + ("   (CORRECTED — what the fixed engine fires)" if sig == "0DTE"
                                   else "   (all-expiry BASELINE — the backwards signal)"))
        for hname, _ in HORIZONS:
            report(f"HORIZON {hname}", res[sig][hname])

    print("\n" + "=" * 96)
    print("Benchmark (ThetaData, 1093 sess): ~54-55% hit, +~1pt favorable every year, large t.")
    print("t_fav / t_hit are SESSION-CLUSTERED (N_eff=#sessions). |t|>~2 => significant.")
    print("=" * 96)


if __name__ == "__main__":
    main()
