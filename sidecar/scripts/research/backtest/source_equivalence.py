"""Data-equivalence test: ThetaData flow-surge signal  vs  live-engine QuantData drift.

WHY THIS EXISTS
  The directional flow-surge edge ([[project-flow-surge-edge]]) was validated on ONE
  data source — ThetaData `option_flow_minute.net_signed_prem` (per-minute signed
  premium; signal = 5-row rolling sum, surge = |roll| >= per-session p90). But the LIVE
  engine (src/fef_sidecar/engine/fef.py) fires on a DIFFERENT source — QuantData
  `drift_cum_net_m` (cumulative session net premium, $M), via the 3-iter delta
  `delta_drift_m = current.drift_cum_net_m - oldest.drift_cum_net_m` over a deque of the
  last (history_window+1)=4 iters, gated when |delta_drift_m| >= FEF_FLOW_SURGE_MIN_M (25M).
  If the two sources don't track each other (or disagree on SIGN), the edge validated on
  Theta DOES NOT transfer to the live engine — and we'd be trading a fiction.

WHAT IT DOES (read-only on the DB)
  Both tables live in the SAME sqlite file (~/.fef-terminal/signal_log.db):
    * option_flow_minute  — ThetaData, research/backtest source (2022-01..2026-06, RTH per-min).
    * signal_iter         — QuantData drift the live engine actually consumed (recent paper sessions).
  We auto-discover overlapping session dates, time-align by ET minute, and compute:
    1. CUMULATIVE correlation : Theta cumsum(net_signed_prem)  vs  Quanta drift_cum_net_m.
    2. SURGE correlation      : Theta 5-row rolling sum         vs  Quanta engine-style 3-iter delta.
    3. DIRECTION AGREEMENT    : do they agree on SIGN at surge points?
         - at QuantData gate points (|delta_drift_m| >= 25M)  -> the live-relevant test
         - at ThetaData surge points (|roll5| >= session p90) -> the validation-relevant test
  Units differ ($ vs $M) and aggregation differs, so we compare SHAPE/CORREL/SIGN, never
  absolute magnitude. Pearson AND Spearman (rank) are both reported; Spearman is robust to
  the scale mismatch and to the occasional corrupted drift value.

INTERPRETATION GUARDRAILS
  * High positive correlation + high sign-agreement  -> sources equivalent, edge transfers.
  * Low / negative correlation, or sign-agreement near 50%  -> NOT equivalent; the live
    engine is firing on a series that does not match the one the edge was proven on.

DB opened READ-ONLY via scripts.research.backtest.data.connect().
Run:  .venv/Scripts/python.exe -m scripts.research.backtest.source_equivalence
"""
from __future__ import annotations

import datetime as dt
import sys
from zoneinfo import ZoneInfo

import numpy as np

from .data import connect

sys.stdout.reconfigure(encoding="utf-8")
ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# ---- knobs (mirror the live engine + the validated signal) -------------------
ROLL = 5                       # ThetaData rolling window (minute-rows) — the validated signal
HISTORY_WINDOW = 3             # engine deque(maxlen=HISTORY_WINDOW+1); delta = drift[i]-drift[i-3]
FLOW_SURGE_MIN_M = 25.0        # live gate FEF_FLOW_SURGE_MIN_M
SURGE_Q = 90                   # ThetaData per-session surge percentile (validated signal)
RTH_LO, RTH_HI = 9.5, 16.0     # regular trading hours, ET (decimal hours)
ALIGN_TOL_MS = 150_000         # match a Quanta iter to a Theta minute within +/- 2.5 min
GLITCH_M = 300.0               # |drift_cum_net_m| above this is a corrupt capture -> dropped
PROVIDER_OK = ("live",)        # only compare genuinely-live QuantData iters (drop mock/mixed)


# --------------------------------------------------------------------------- utils
def _roll_sum(x, w=ROLL):
    """Trailing w-row sum; NaN for the first w-1 rows (window not yet full)."""
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        c = np.cumsum(x)
        out[w - 1:] = c[w - 1:] - np.concatenate(([0.0], c[:-w]))
    return out


def _pearson(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 3 or np.std(a) == 0 or np.std(b) == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def _rankdata(x):
    """Average-rank (handles ties) — for Spearman without a scipy dependency."""
    x = np.asarray(x, float)
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), float)
    ranks[order] = np.arange(1, len(x) + 1)
    # average ties
    _, inv, counts = np.unique(x, return_inverse=True, return_counts=True)
    csum = np.cumsum(counts)
    start = csum - counts
    avg = (start + csum + 1) / 2.0
    return avg[inv]


def _spearman(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 3:
        return float("nan")
    return _pearson(_rankdata(a), _rankdata(b))


def _sign_agree(theta, quanta):
    """% of points where sign(theta) == sign(quanta), ignoring exact-zero entries."""
    t, q = np.asarray(theta, float), np.asarray(quanta, float)
    m = (t != 0) & (q != 0)
    if m.sum() == 0:
        return float("nan"), 0
    return 100.0 * (np.sign(t[m]) == np.sign(q[m])).mean(), int(m.sum())


# --------------------------------------------------------------------------- load
def overlap_dates(con):
    """Session dates present in BOTH option_flow_minute (Theta) and live RTH signal_iter."""
    theta = {r[0] for r in con.execute("SELECT DISTINCT session_date FROM option_flow_minute")}
    quanta = {r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM signal_iter "
        "WHERE drift_cum_net_m IS NOT NULL")}
    return sorted(theta & quanta)


def load_theta(con, d):
    """ThetaData per-minute series for session d, indexed by ET-minute (UTC-ms floored).

    Returns dict: minute_ms -> (cum_m, roll5_m), plus arrays for p90.
    cum_m / roll5_m are in $M (net_signed_prem is in raw $)."""
    rows = con.execute(
        "SELECT ts_ms, net_signed_prem FROM option_flow_minute "
        "WHERE session_date=? ORDER BY ts_ms", (d,)).fetchall()
    if len(rows) < ROLL + 2:
        return None
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    net = np.array([r[1] for r in rows], float) / 1e6        # -> $M
    cum = np.cumsum(net)
    roll = _roll_sum(net)
    minute = (ts // 60000) * 60000                            # floor to minute (UTC ms)
    idx = {int(minute[i]): (float(cum[i]), float(roll[i])) for i in range(len(rows))}
    valid_roll = roll[~np.isnan(roll)]
    p90 = float(np.percentile(np.abs(valid_roll), SURGE_Q)) if len(valid_roll) else float("nan")
    return {"idx": idx, "minutes": minute.astype(np.int64), "p90": p90}


def load_quanta(con, d):
    """Live QuantData drift the engine consumed for session d.

    Returns ordered lists: minute_ms (UTC, floored), drift_m, delta_m (engine 3-iter).
    Filtered to RTH + live provider; corrupt |drift|>GLITCH_M dropped (reported)."""
    rows = con.execute(
        "SELECT timestamp, drift_cum_net_m, provider_mode FROM signal_iter "
        "WHERE session_date=? ORDER BY timestamp", (d,)).fetchall()
    minute_ms, drift_m, n_glitch, n_offhours, n_mock = [], [], 0, 0, 0
    for ts, drift, prov in rows:
        if prov not in PROVIDER_OK:
            n_mock += 1
            continue
        t = dt.datetime.fromisoformat(ts).replace(tzinfo=UTC).astimezone(ET)
        h = t.hour + t.minute / 60.0
        if not (RTH_LO <= h < RTH_HI):
            n_offhours += 1
            continue
        if drift is None or abs(drift) > GLITCH_M:
            n_glitch += 1
            continue
        utc_ms = int(dt.datetime.fromisoformat(ts).replace(tzinfo=UTC).timestamp() * 1000)
        minute_ms.append((utc_ms // 60000) * 60000)
        drift_m.append(float(drift))
    drift_arr = np.array(drift_m, float)
    # engine-faithful 3-iter delta: delta[i] = drift[i] - drift[i-HISTORY_WINDOW]
    delta = np.full(len(drift_arr), np.nan)
    if len(drift_arr) > HISTORY_WINDOW:
        delta[HISTORY_WINDOW:] = drift_arr[HISTORY_WINDOW:] - drift_arr[:-HISTORY_WINDOW]
    return {"minute_ms": np.array(minute_ms, np.int64), "drift_m": drift_arr,
            "delta_m": delta, "n_glitch": n_glitch, "n_offhours": n_offhours,
            "n_mock": n_mock}


def align(theta, quanta):
    """For each live Quanta iter, find the Theta minute at/just-before it (within tol).

    Returns paired arrays (one row per matched iter):
      tc/qc  = theta cumulative $M / quanta drift_cum_net_m $M
      tr/qd  = theta 5-row rolling $M / quanta 3-iter delta $M
      qdelta_gate = |quanta delta| >= FLOW_SURGE_MIN_M  (live gate mask)
      tr_p90      = |theta roll5|  >= session p90        (validated surge mask)"""
    tmins = theta["minutes"]
    tc, qc, tr, qd, gate, p90mask = [], [], [], [], [], []
    for i, qm in enumerate(quanta["minute_ms"]):
        hit = theta["idx"].get(int(qm))
        if hit is None:
            # nearest theta minute at/just-before qm, within tolerance
            le = tmins[tmins <= qm]
            if len(le) == 0 or (qm - le[-1]) > ALIGN_TOL_MS:
                continue
            hit = theta["idx"][int(le[-1])]
        cum_m, roll_m = hit
        if np.isnan(roll_m):
            continue
        tc.append(cum_m)
        qc.append(quanta["drift_m"][i])
        tr.append(roll_m)
        qd.append(quanta["delta_m"][i])           # may be NaN for first 3 iters
        gate.append(abs(quanta["delta_m"][i]) >= FLOW_SURGE_MIN_M
                    if not np.isnan(quanta["delta_m"][i]) else False)
        p90mask.append(abs(roll_m) >= theta["p90"])
    return (np.array(tc), np.array(qc), np.array(tr), np.array(qd),
            np.array(gate, bool), np.array(p90mask, bool))


# --------------------------------------------------------------------------- main
def main():
    con = connect()
    dates = overlap_dates(con)
    print("=" * 100)
    print("SOURCE-EQUIVALENCE TEST  —  ThetaData net_signed_prem surge  vs  live QuantData drift")
    print("=" * 100)
    print(f"  overlap sessions (in BOTH option_flow_minute AND live signal_iter): {len(dates)}")
    print(f"  date range: {dates[0]} .. {dates[-1]}" if dates else "  NONE")
    print(f"  ROLL={ROLL} (theta)  HISTORY_WINDOW={HISTORY_WINDOW} (engine delta)  "
          f"gate>={FLOW_SURGE_MIN_M}M  theta surge=p{SURGE_Q}")
    print(f"  RTH {RTH_LO}-{RTH_HI} ET, provider in {PROVIDER_OK}, |drift|<= {GLITCH_M}M\n")

    # pooled accumulators
    P = dict(tc=[], qc=[], tr=[], qd=[], gate_pool=[], p90_pool=[])
    hdr = (f"{'date':11s} {'nAlign':>6s} {'CUMc r':>7s} {'CUMc ρ':>7s} "
           f"{'SRGc r':>7s} {'SRGc ρ':>7s} {'DA-all%':>8s} {'DA-gate%':>9s}(n) "
           f"{'DA-p90%':>8s}(n)  drops(g/oh/mk)")
    print(hdr)
    print("-" * len(hdr))

    for d in dates:
        theta = load_theta(con, d)
        if theta is None:
            print(f"{d:11s}  (theta too short)")
            continue
        q = load_quanta(con, d)
        if len(q["drift_m"]) < HISTORY_WINDOW + 2:
            print(f"{d:11s}  (live quanta too short: {len(q['drift_m'])} iters)  "
                  f"drops g/oh/mk={q['n_glitch']}/{q['n_offhours']}/{q['n_mock']}")
            continue
        tc, qc, tr, qd, gate, p90 = align(theta, q)
        if len(tc) < 5:
            print(f"{d:11s}  (insufficient aligned points: {len(tc)})")
            continue

        cum_r, cum_rho = _pearson(tc, qc), _spearman(tc, qc)
        # surge correlation only where quanta delta is defined (not NaN)
        sm = ~np.isnan(qd)
        srg_r = _pearson(tr[sm], qd[sm]) if sm.sum() >= 3 else float("nan")
        srg_rho = _spearman(tr[sm], qd[sm]) if sm.sum() >= 3 else float("nan")
        da_all, n_all = _sign_agree(tr[sm], qd[sm])
        gmask = gate & sm
        da_gate, n_gate = _sign_agree(tr[gmask], qd[gmask])
        pmask = p90 & sm
        da_p90, n_p90 = _sign_agree(tr[pmask], qd[pmask])

        # pool: cumulative uses all aligned pts; surge uses the delta-defined subset (sm),
        # so the pooled gate/p90 masks must be sliced by sm to stay row-aligned with tr/qd.
        P["tc"] += list(tc); P["qc"] += list(qc)
        P["tr"] += list(tr[sm]); P["qd"] += list(qd[sm])
        P["gate_pool"] += list(gate[sm])
        P["p90_pool"] += list(p90[sm])

        def f(x):
            return f"{x:+.3f}" if not np.isnan(x) else "   nan"
        def g(x):
            return f"{x:5.1f}" if not np.isnan(x) else "  nan"
        print(f"{d:11s} {len(tc):6d} {f(cum_r):>7s} {f(cum_rho):>7s} "
              f"{f(srg_r):>7s} {f(srg_rho):>7s} {g(da_all):>8s} {g(da_gate):>7s}({n_gate:2d}) "
              f"{g(da_p90):>6s}({n_p90:2d})  {q['n_glitch']}/{q['n_offhours']}/{q['n_mock']}")

    # ---- pooled ------------------------------------------------------------
    print("-" * len(hdr))
    tc, qc = np.array(P["tc"]), np.array(P["qc"])
    tr, qd = np.array(P["tr"]), np.array(P["qd"])
    gate = np.array(P["gate_pool"], bool)
    p90 = np.array(P["p90_pool"], bool)
    if len(tc) >= 5:
        cum_r, cum_rho = _pearson(tc, qc), _spearman(tc, qc)
        srg_r, srg_rho = _pearson(tr, qd), _spearman(tr, qd)
        da_all, n_all = _sign_agree(tr, qd)
        da_gate, n_gate = _sign_agree(tr[gate], qd[gate])
        da_p90, n_p90 = _sign_agree(tr[p90], qd[p90])
        print(f"{'POOLED':11s} {len(tc):6d} {cum_r:+.3f}  {cum_rho:+.3f}  "
              f"{srg_r:+.3f}  {srg_rho:+.3f}  {da_all:5.1f}%  "
              f"{da_gate:5.1f}%({n_gate:3d}) {da_p90:5.1f}%({n_p90:3d})")
        print("\n" + "=" * 100)
        print("LEGEND  CUMc=cumulative corr (Theta cumsum vs Quanta drift_cum_net_m); "
              "SRGc=surge corr (Theta 5-row roll vs Quanta 3-iter delta);")
        print("        DA-all=sign agreement over all aligned surge pts; "
              "DA-gate=at live gate pts (|delta|>=25M); DA-p90=at Theta p90 surge pts.")
        print("        r=Pearson, ρ=Spearman (rank, robust to the $-vs-$M scale mismatch).")
        print("        drops g/oh/mk = signal_iter rows dropped: glitch / off-hours / mock-provider.")
        print("=" * 100)

        # ---- gate-reachability diagnostic ----------------------------------
        # The edge's live gate is |delta_drift_m| >= FLOW_SURGE_MIN_M. If the live
        # QuantData delta almost never reaches it, the gate is calibrated to the
        # WRONG source's scale and effectively never fires (see report).
        aq = np.abs(qd)
        gate_rate = 100.0 * (aq >= FLOW_SURGE_MIN_M).mean()
        at = np.abs(tr)
        print("\nGATE-REACHABILITY (scale check):")
        print(f"  live QuantData |delta_drift_m|: p50={np.percentile(aq,50):.2f}M "
              f"p90={np.percentile(aq,90):.2f}M p99={np.percentile(aq,99):.2f}M  "
              f"-> reaches {FLOW_SURGE_MIN_M:.0f}M gate {gate_rate:.2f}% of the time")
        print(f"  ThetaData    |5-row roll|     : p50={np.percentile(at,50):.2f}M "
              f"p90={np.percentile(at,90):.2f}M p99={np.percentile(at,99):.2f}M  "
              f"-> reaches {FLOW_SURGE_MIN_M:.0f}M  {100.0*(at>=FLOW_SURGE_MIN_M).mean():.2f}% of the time")
        print(f"  median scale ratio Theta/Quanta = "
              f"{np.percentile(at,50)/max(np.percentile(aq,50),1e-9):.1f}x")

        # ---- verdict heuristics --------------------------------------------
        print("\nVERDICT HEURISTICS (a chance-level sign-agreement is 50%):")
        verdict_ok = (cum_rho >= 0.7 and srg_rho >= 0.5 and da_gate >= 70.0)
        print(f"  cumulative Spearman ρ = {cum_rho:+.3f}   (want >= +0.70 for 'tracks')")
        print(f"  surge      Spearman ρ = {srg_rho:+.3f}   (want >= +0.50)")
        print(f"  sign-agree at live gate = {da_gate:.1f}%  (want >= 70%; 50% = coin flip)")
        print(f"\n  >>> {'EQUIVALENT — edge plausibly transfers' if verdict_ok else 'NOT EQUIVALENT — edge does NOT transfer as-is'} <<<")
    else:
        print("INSUFFICIENT ALIGNED DATA for a pooled verdict.")


if __name__ == "__main__":
    main()
