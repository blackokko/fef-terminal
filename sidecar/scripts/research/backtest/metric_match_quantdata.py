# [REMOVED 2026-08-25] QuantData subscription CANCELLED. THIS SCRIPT NO LONGER
# RUNS: it imports fef_sidecar.quantdata, which is deleted, and it fetched from
# the QuantData REST API, which we no longer have credentials for. Kept as the
# provenance record of the study/backfill it performed (see RESEARCH_REGISTRY);
# delete it only as a deliberate archive decision, not as a cleanup.
"""Find the MINIMAL live-QuantData metric definition that matches the validated
ThetaData 0DTE aggressive signed-flow surge signal.

CONTEXT (see source_equivalence.py / directional_validate_quantdata.py):
  The live engine's drift_cum_net_m = QuantData net-drift with filter {ticker:SPX} and
  NO expirationDate -> ALL-EXPIRY net call-put premium (longer-dated hedging dominated).
  The VALIDATED edge used ThetaData option_flow_minute.net_signed_prem = SPXW 0DTE-only,
  +/-20 near-money strikes, AGGRESSOR-classified, net=(callBuy-callSell)-(putBuy-putSell).
  The all-expiry source correlated only ~0.40 (Spearman) / 65-75% sign with ThetaData.

THIS SCRIPT walks the fix list, fetching each candidate DIRECTLY from the QuantData API
per session (net-drift serves historical intraday via sessionDate), and measures how well
its surge series matches ThetaData. Same transform on both sides isolates the DATA effect.

CANDIDATES
  baseline : net-drift ALL-EXPIRY   net = Sigma(netCallPremium - netPutPremium)
  #1 0DTE  : net-drift expirationDate=0DTE,  net = Sigma(netCallPremium - netPutPremium)
             (netCall/netPut are ALREADY aggressor-signed per-ts -> #1 also covers #2's signing)
  #1b band : #1 + strikes=[+/-20 near-money @5pt] (the only working strike filter; min/maxStrike
             are accepted-but-ignored no-ops, verified)
  #2 flow  : net-flow 0DTE NET_PREMIUM, net = Sigma(callSum - putSum)  (GROSS sums, not signed)
  (aggressor trade-side-statistics is EOD-only -> no intraday series -> cannot build a surge series)

METRIC (isolates source definition): both series -> 1-min net -> 5-row rolling sum (the validated
ThetaData transform). Report Spearman corr (scale-invariant) + DIRECTION AGREEMENT at ThetaData
surge points (|theta 5-row roll| >= session p90). Target: Spearman >= 0.80 AND DA >= 85%.
A CADENCE section then shows, for the best candidate, engine-style (2-min, 3-iter delta) vs
ThetaData-style (1-min, 5-row) to see if cadence — not source — is the remaining drag.

Read-only on the DB; network-read on the QuantData API. No engine/live code is touched.
Run:  .venv/Scripts/python.exe -m scripts.research.backtest.metric_match_quantdata [START END]
"""
from __future__ import annotations

import datetime as dt
import sys
from zoneinfo import ZoneInfo

import httpx
import numpy as np

from fef_sidecar.quantdata.config import load_config
from .data import connect

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

ROLL = 5
SURGE_Q = 90
RTH_LO, RTH_HI = 9.5, 16.0
BAND_STRIKES = 20          # +/-20 strikes
STRIKE_STEP = 5            # SPXW 0DTE near-money spacing (pts)
TARGET_RHO, TARGET_DA = 0.80, 85.0


# --------------------------------------------------------------------------- API
def _api():
    cfg = load_config()
    return httpx.Client(
        base_url=cfg.base_url,
        headers={"Authorization": f"Bearer {cfg.auth_token}",
                 "Content-Type": "application/json", "accept": "application/json"},
        timeout=90.0,
    )


def _post(c, tool, body):
    r = c.post(f"/options/tool/{tool}", json=body)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("application/json"):
        return None
    return r.json().get("data")


def _et_min(ts_ms):
    """Floor a UTC-ms timestamp to its ET minute (returns minute-epoch ms)."""
    return (int(ts_ms) // 60000) * 60000


def _rth(ts_ms):
    t = dt.datetime.fromtimestamp(int(ts_ms) / 1000, ET)
    h = t.hour + t.minute / 60.0
    return RTH_LO <= h < RTH_HI


def per_minute_from_drift(data, call_k="netCallPremium", put_k="netPutPremium"):
    """data.<ts>.{call_k,put_k} -> dict ET-minute -> summed signed net (dollars), RTH only."""
    out: dict[int, float] = {}
    if not isinstance(data, dict):
        return out
    for ts, v in data.items():
        if not isinstance(v, dict) or not _rth(ts):
            continue
        net = float(v.get(call_k) or 0) - float(v.get(put_k) or 0)
        out[_et_min(ts)] = out.get(_et_min(ts), 0.0) + net
    return out


def spot_from_drift(data):
    """Median RTH stockPrice for building the near-money strike band."""
    sp = [float(v.get("stockPrice") or 0) for ts, v in data.items()
          if isinstance(v, dict) and _rth(ts) and v.get("stockPrice")]
    sp = [s for s in sp if s > 1000]
    return float(np.median(sp)) if sp else None


# ----------------------------------------------------------------------- series
def roll5(minute_net: dict[int, float]):
    """ordered (minutes, 5-row trailing rolling sum) in $M from a per-minute net dict."""
    mins = sorted(minute_net)
    net = np.array([minute_net[m] for m in mins], float) / 1e6
    out = np.full(len(net), np.nan)
    if len(net) >= ROLL:
        c = np.cumsum(net)
        out[ROLL - 1:] = c[ROLL - 1:] - np.concatenate(([0.0], c[:-ROLL]))
    return np.array(mins, np.int64), out


def engine_delta(minute_net: dict[int, float], step_min=2, win=3):
    """2-min-resampled cumulative, then `win`-iter delta (the live engine's surge)."""
    mins = sorted(minute_net)
    if not mins:
        return np.array([], np.int64), np.array([])
    # resample to step_min buckets
    b0 = mins[0]
    buck: dict[int, float] = {}
    for m in mins:
        bk = b0 + ((m - b0) // (step_min * 60000)) * (step_min * 60000)
        buck[bk] = buck.get(bk, 0.0) + minute_net[m]
    bmins = sorted(buck)
    net = np.array([buck[b] for b in bmins], float) / 1e6
    cum = np.cumsum(net)
    delta = np.full(len(cum), np.nan)
    if len(cum) > win:
        delta[win:] = cum[win:] - cum[:-win]
    return np.array(bmins, np.int64), delta


def theta_minute_net(con, date):
    rows = con.execute(
        "SELECT ts_ms, net_signed_prem FROM option_flow_minute WHERE session_date=? ORDER BY ts_ms",
        (date,)).fetchall()
    return {_et_min(r[0]): float(r[1]) for r in rows if _rth(r[0])}


# ------------------------------------------------------------------------ stats
def _rank(x):
    x = np.asarray(x, float)
    order = np.argsort(x, kind="mergesort")
    r = np.empty(len(x)); r[order] = np.arange(1, len(x) + 1)
    _, inv, cnt = np.unique(x, return_inverse=True, return_counts=True)
    cs = np.cumsum(cnt); st = cs - cnt
    return ((st + cs + 1) / 2.0)[inv]


def spearman(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 3 or np.std(a) == 0 or np.std(b) == 0:
        return float("nan")
    ra, rb = _rank(a), _rank(b)
    return float(np.corrcoef(ra, rb)[0, 1])


def da_at_surge(theta_mins, theta_roll, cand_mins, cand_roll, p90):
    """Sign agreement where |theta roll| >= p90, comparing aligned candidate roll."""
    cand = dict(zip(cand_mins.tolist(), cand_roll.tolist()))
    agree = tot = 0
    for m, tr in zip(theta_mins.tolist(), theta_roll.tolist()):
        if np.isnan(tr) or abs(tr) < p90:
            continue
        cr = cand.get(m)
        if cr is None or np.isnan(cr) or cr == 0 or tr == 0:
            continue
        tot += 1
        if np.sign(cr) == np.sign(tr):
            agree += 1
    return (100.0 * agree / tot if tot else float("nan")), tot


def align_corr(theta_mins, theta_roll, cand_mins, cand_roll):
    cand = dict(zip(cand_mins.tolist(), cand_roll.tolist()))
    ta, ca = [], []
    for m, tr in zip(theta_mins.tolist(), theta_roll.tolist()):
        cr = cand.get(m)
        if cr is None or np.isnan(tr) or np.isnan(cr):
            continue
        ta.append(tr); ca.append(cr)
    return spearman(ta, ca), len(ta)


# -------------------------------------------------------------------------- main
def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-05-18"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-26"
    con = connect()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_flow_minute WHERE session_date BETWEEN ? AND ? "
        "ORDER BY session_date", (start, end))]

    print("=" * 108)
    print("METRIC-MATCH: which live-QuantData definition reproduces the ThetaData 0DTE surge signal?")
    print(f"  sessions {start}..{end}  (n={len(dates)})   transform=1-min 5-row rolling sum (both sides)")
    print(f"  target: Spearman >= {TARGET_RHO}  AND  dir-agree@theta-p90 >= {TARGET_DA}%")
    print("=" * 108)

    cand_names = ["baseline_allexp", "c1_0dte", "c1b_0dte_band", "c2_netflow_0dte"]
    pool = {k: {"rho_pairs": ([], []), "da_a": 0, "da_t": 0} for k in cand_names}
    rows_out = {k: [] for k in cand_names}

    with _api() as c:
        for d in dates:
            tmn = theta_minute_net(con, d)
            if len(tmn) < 30:
                continue
            tmins, troll = roll5(tmn)
            valid = troll[~np.isnan(troll)]
            if len(valid) < 10:
                continue
            p90 = float(np.percentile(np.abs(valid), SURGE_Q))

            # fetch candidates
            d_all = _post(c, "net-drift", {"sessionDate": d, "filter": {"ticker": "SPX"}})
            d_0 = _post(c, "net-drift", {"sessionDate": d, "filter": {"ticker": "SPX", "expirationDate": d}})
            cands = {}
            cands["baseline_allexp"] = per_minute_from_drift(d_all) if d_all else {}
            cands["c1_0dte"] = per_minute_from_drift(d_0) if d_0 else {}
            # #1b near-money band via the working `strikes` list filter
            if d_0:
                spot = spot_from_drift(d_0)
                if spot:
                    base = round(spot / STRIKE_STEP) * STRIKE_STEP
                    strikes = [int(base + STRIKE_STEP * k)
                               for k in range(-BAND_STRIKES, BAND_STRIKES + 1)]
                    d_band = _post(c, "net-drift",
                                   {"sessionDate": d, "filter": {"ticker": "SPX",
                                    "expirationDate": d, "strikes": strikes}})
                    cands["c1b_0dte_band"] = per_minute_from_drift(d_band) if d_band else {}
                else:
                    cands["c1b_0dte_band"] = {}
            else:
                cands["c1b_0dte_band"] = {}
            d_nf = _post(c, "net-flow", {"sessionDate": d, "filter": {"ticker": "SPX",
                        "expirationDate": d}, "dataMode": "NET_PREMIUM"})
            cands["c2_netflow_0dte"] = per_minute_from_drift(d_nf, "callSum", "putSum") if d_nf else {}

            for k in cand_names:
                mn = cands[k]
                if len(mn) < 30:
                    rows_out[k].append((d, float("nan"), float("nan"), 0))
                    continue
                cmins, croll = roll5(mn)
                rho, npairs = align_corr(tmins, troll, cmins, croll)
                da, nda = da_at_surge(tmins, troll, cmins, croll, p90)
                rows_out[k].append((d, rho, da, nda))
                if not np.isnan(rho):
                    pa, pb = pool[k]["rho_pairs"]
                    # accumulate aligned pairs for a pooled spearman
                    cand_map = dict(zip(cmins.tolist(), croll.tolist()))
                    for m, tr in zip(tmins.tolist(), troll.tolist()):
                        cr = cand_map.get(m)
                        if cr is None or np.isnan(tr) or np.isnan(cr):
                            continue
                        pa.append(tr); pb.append(cr)
                if not np.isnan(da):
                    pool[k]["da_a"] += round(da / 100.0 * nda)
                    pool[k]["da_t"] += nda
            print(f"  fetched {d}  (theta p90={p90:.2f}M)", flush=True)

    # ---- per-candidate tables -------------------------------------------------
    for k in cand_names:
        print("\n" + "=" * 108)
        print(f"CANDIDATE: {k}")
        print(f"  {'date':11s} {'Spearman':>9s} {'DA@p90%':>8s} {'(nSurge)':>9s}")
        srhos, sdas = [], []
        for (d, rho, da, nda) in rows_out[k]:
            print(f"  {d:11s} {rho:+9.3f} {da:8.1f} {nda:9d}")
            if not np.isnan(rho):
                srhos.append(rho)
            if not np.isnan(da):
                sdas.append(da)
        pa, pb = pool[k]["rho_pairs"]
        pooled_rho = spearman(pa, pb) if len(pa) > 3 else float("nan")
        pooled_da = (100.0 * pool[k]["da_a"] / pool[k]["da_t"]) if pool[k]["da_t"] else float("nan")
        mean_rho = float(np.mean(srhos)) if srhos else float("nan")
        mean_da = float(np.mean(sdas)) if sdas else float("nan")
        print(f"  {'-'*40}")
        print(f"  session-mean Spearman={mean_rho:+.3f}   pooled Spearman={pooled_rho:+.3f}")
        print(f"  session-mean DA@p90={mean_da:.1f}%   pooled DA@p90={pooled_da:.1f}%  "
              f"(n_surge={pool[k]['da_t']})")
        hit = (not np.isnan(pooled_rho) and pooled_rho >= TARGET_RHO
               and not np.isnan(pooled_da) and pooled_da >= TARGET_DA)
        print(f"  >>> {'MEETS TARGET' if hit else 'below target'} "
              f"(rho>={TARGET_RHO}, DA>={TARGET_DA}%)")

    # ---- cadence sub-test on c1_0dte -----------------------------------------
    print("\n" + "=" * 108)
    print("CADENCE sub-test (candidate c1_0dte): engine-style 2-min/3-iter delta  vs  1-min/5-row roll")
    print("  (does cadence — not the data source — explain residual mismatch?)")
    eng_pa, eng_pb = [], []
    with _api() as c:
        for d in dates:
            tmn = theta_minute_net(con, d)
            if len(tmn) < 30:
                continue
            tmins, troll = roll5(tmn)
            d_0 = _post(c, "net-drift", {"sessionDate": d, "filter": {"ticker": "SPX", "expirationDate": d}})
            if not d_0:
                continue
            mn = per_minute_from_drift(d_0)
            if len(mn) < 30:
                continue
            emins, edelta = engine_delta(mn)   # 2-min/3-iter
            cand_map = dict(zip(emins.tolist(), edelta.tolist()))
            # align engine delta (2-min grid) to theta 1-min roll by nearest minute <=
            for m, tr in zip(tmins.tolist(), troll.tolist()):
                if np.isnan(tr):
                    continue
                # nearest engine bucket at/just-before m
                cr = cand_map.get(m)
                if cr is None:
                    le = [b for b in emins.tolist() if b <= m]
                    if not le:
                        continue
                    cr = cand_map.get(le[-1])
                if cr is None or np.isnan(cr):
                    continue
                eng_pa.append(tr); eng_pb.append(cr)
    eng_rho = spearman(eng_pa, eng_pb) if len(eng_pa) > 3 else float("nan")
    print(f"  pooled Spearman engine-style(2min/3iter) vs theta(1min/5row) = {eng_rho:+.3f}  "
          f"(n={len(eng_pa)})")
    print("  compare to c1_0dte 1-min/5-row pooled Spearman above (same data, theta transform).")
    print("=" * 108)


if __name__ == "__main__":
    main()
