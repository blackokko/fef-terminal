"""Direct re-validation of the flow-surge DIRECTIONAL edge on the live QuantData drift.

DIFFERENT QUESTION than source_equivalence.py. That script asked "does QuantData drift
AGREE with ThetaData flow?" (answer: no). THIS script asks the fairer, original question
the edge was validated with: on QuantData's OWN terms, do QuantData's own drift surges
PREDICT the forward SPX move?  (exit-independent directional test — the gold standard.)

METHOD (mirrors the ThetaData validation, but on QuantData's own scale)
  source   : signal_iter (drift_cum_net_m + spx_price per ~2-min engine iter) — the engine's
             actual input. flow_snapshot covers the SAME 11 calendar dates at higher cadence
             but has corrupted spx_price + same drift glitches, so it adds no usable SESSIONS;
             reported as a cross-check only. We use the SESSION as the unit of inference.
  filters  : provider_mode='live'; RTH 09:30-16:00 ET; drop corrupted drift sessions
             (2026-05-29, 2026-06-30 per the equivalence audit) and the 3-iter stub 2026-05-23;
             drop per-row glitch spx (outside median +/- SPX_BAND) for the move measurement only.
  surge    : delta_drift_m = drift_cum_net_m[i] - drift_cum_net_m[i-3]  (engine's 3-iter delta).
             A surge = |delta_drift_m| >= that SESSION's p90 of |delta_drift_m| (QuantData's own
             adaptive scale — NOT ThetaData's fixed 25M, which never fires here).
  predict  : dir = sign(delta_drift_m).  favorable_move = dir * (spx[t+H] - spx[t]), in SPX pts.
             hit = favorable_move > 0.  Horizons H = +10min, +30min, session-end (EOD).
  controls : opposite-direction and random-direction at the SAME surge points (vs chance).

SIGNIFICANCE (session-clustered — critical)
  Surge points within a session are NOT independent. We report per-session mean favorable move
  + hit rate, then a SESSION-LEVEL t-stat: mean(session_means) / (sd(session_means)/sqrt(#sessions)),
  with N_eff = #sessions (NOT #surge points). A t computed over pooled points would be inflated.

BENCHMARK (ThetaData validation): ~54-55% direction hit, +~1pt favorable move every year, large t
over 1093 sessions. This live sample is ~2 weeks of 2026 (single calm-bull regime, a handful of
sessions), so an "every-year" replication is impossible — frame the verdict by power, not just sign.

DB opened READ-ONLY via scripts.research.backtest.data.connect().
Run:  .venv/Scripts/python.exe -m scripts.research.backtest.directional_validate_quantdata
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

HISTORY_WINDOW = 3              # engine delta = drift[i] - drift[i-3]
SURGE_Q = 90                   # per-session surge percentile of |delta_drift_m|
RTH_LO, RTH_HI = 9.5, 16.0     # ET decimal hours
GLITCH_DRIFT_M = 300.0         # |drift| above this = corrupt capture
SPX_BAND = 0.05                # per-row spx kept only within +/- 5% of session median
DROP_SESSIONS = {"2026-05-23", "2026-05-29", "2026-06-30"}  # stub + corrupted-drift days
HORIZONS = [("+10min", 10), ("+30min", 30), ("EOD", None)]
TOL_MS = {10: 150_000, 30: 450_000}   # forward-row match tolerance per horizon
RNG = np.random.default_rng(0)


def _et_h(utc_ms):
    t = dt.datetime.fromtimestamp(utc_ms / 1000, ET)
    return t.hour + t.minute / 60.0


def load_session(con, d):
    """Live RTH rows for session d: returns ordered et_ms, drift_m arrays (drift-clean),
    plus a parallel 'spx_ok' clean-spx lookup. delta computed engine-faithfully."""
    rows = con.execute(
        "SELECT timestamp, drift_cum_net_m, spx_price, provider_mode "
        "FROM signal_iter WHERE session_date=? ORDER BY timestamp", (d,)).fetchall()
    et_ms, drift, spx = [], [], []
    n_mock = n_off = n_gd = 0
    for ts, dr, sp, prov in rows:
        if prov != "live":
            n_mock += 1
            continue
        utc_ms = int(dt.datetime.fromisoformat(ts).replace(tzinfo=UTC).timestamp() * 1000)
        if not (RTH_LO <= _et_h(utc_ms) < RTH_HI):
            n_off += 1
            continue
        if dr is None or abs(dr) > GLITCH_DRIFT_M:
            n_gd += 1
            continue
        et_ms.append(utc_ms)
        drift.append(float(dr))
        spx.append(float(sp) if sp is not None else np.nan)
    et_ms = np.array(et_ms, np.int64)
    drift = np.array(drift, float)
    spx = np.array(spx, float)
    if len(drift) <= HISTORY_WINDOW + 1:
        return None
    # clean spx (robust band around session median) — for the move measurement only
    med = np.nanmedian(spx)
    spx_ok = spx.copy()
    bad = ~np.isfinite(spx_ok) | (np.abs(spx_ok - med) > SPX_BAND * med)
    spx_ok[bad] = np.nan
    n_spx_bad = int(bad.sum())
    # engine-faithful 3-iter delta
    delta = np.full(len(drift), np.nan)
    delta[HISTORY_WINDOW:] = drift[HISTORY_WINDOW:] - drift[:-HISTORY_WINDOW]
    return dict(d=d, et_ms=et_ms, delta=delta, spx_ok=spx_ok,
                n_mock=n_mock, n_off=n_off, n_gd=n_gd, n_spx_bad=n_spx_bad)


def fwd_spx(et_ms, spx_ok, i, h_min):
    """SPX h_min minutes after surge i, from the nearest clean-spx row within tolerance.
    h_min=None -> session EOD (last clean-spx row)."""
    if not np.isfinite(spx_ok[i]):
        return None, None
    spx0 = spx_ok[i]
    if h_min is None:
        ok = np.where(np.isfinite(spx_ok))[0]
        j = ok[-1]
        return spx0, spx_ok[j]
    target = et_ms[i] + h_min * 60_000
    tol = TOL_MS[h_min]
    cand = np.where(np.isfinite(spx_ok) & (np.abs(et_ms - target) <= tol))[0]
    if len(cand) == 0:
        return spx0, None
    j = cand[np.argmin(np.abs(et_ms[cand] - target))]
    return spx0, spx_ok[j]


def session_clustered(session_vals):
    """session_vals = list of per-session mean favorable moves. Returns (mean, se, t, n)."""
    a = np.array([v for v in session_vals if v is not None and np.isfinite(v)], float)
    n = len(a)
    if n < 2:
        return (float(a.mean()) if n else float("nan")), float("nan"), float("nan"), n
    se = a.std(ddof=1) / np.sqrt(n)
    return float(a.mean()), float(se), (float(a.mean() / se) if se > 0 else float("nan")), n


def main():
    con = connect()
    all_dates = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM signal_iter ORDER BY session_date")]
    dates = [d for d in all_dates if d not in DROP_SESSIONS]

    print("=" * 100)
    print("DIRECTIONAL VALIDATION of the flow-surge edge — on LIVE QuantData drift (own scale)")
    print("=" * 100)
    print(f"  source: signal_iter (drift_cum_net_m + spx_price). live+RTH only.")
    print(f"  dropped sessions (corrupt drift / stub): {sorted(DROP_SESSIONS)}")
    print(f"  surge = |3-iter delta_drift_m| >= per-session p{SURGE_Q}; dir = sign(delta).")
    print(f"  horizons: {[h[0] for h in HORIZONS]}; significance = SESSION-clustered (N_eff=#sessions).\n")

    sessions = []
    for d in dates:
        s = load_session(con, d)
        if s is None:
            continue
        valid = s["delta"][~np.isnan(s["delta"])]
        # require a REAL session — a thin/half-data day (e.g. 2026-05-28, ~17 iters)
        # produces a degenerate p90 and a 1-2 surge session-mean that is pure noise as
        # a clustered unit. A normal RTH session has ~150-200 live iters.
        if len(valid) < 30:
            print(f"  skip {d}: thin session, only {len(valid)} delta points (need >=30)")
            continue
        s["p90"] = float(np.percentile(np.abs(valid), SURGE_Q))
        s["surge_idx"] = [i for i in range(len(s["delta"]))
                          if not np.isnan(s["delta"][i]) and abs(s["delta"][i]) >= s["p90"]]
        sessions.append(s)

    print(f"  usable sessions: {len(sessions)}  ({sessions[0]['d']}..{sessions[-1]['d']})\n")

    for hname, hmin in HORIZONS:
        print("=" * 100)
        print(f"HORIZON {hname}")
        print(f"{'date':11s} {'#surge':>6s} {'#meas':>5s} {'hit%':>6s} "
              f"{'favMove':>8s} {'oppMove':>8s} {'rndMove':>8s}   p90Δ($M)  drops(spx/mock/off/gd)")
        print("-" * 100)
        sess_fav, sess_hit, sess_opp, sess_rnd = [], [], [], []
        pool_fav, pool_hit, pool_opp, pool_rnd = [], [], [], []
        tot_surge = 0
        for s in sessions:
            favs, hits, opps, rnds = [], [], [], []
            for i in s["surge_idx"]:
                spx0, spxH = fwd_spx(s["et_ms"], s["spx_ok"], i, hmin)
                if spx0 is None or spxH is None:
                    continue
                mv = spxH - spx0
                dirn = 1.0 if s["delta"][i] > 0 else -1.0
                fav = dirn * mv
                favs.append(fav); hits.append(1.0 if fav > 0 else 0.0)
                opps.append(-fav)
                rnds.append(mv if RNG.random() < 0.5 else -mv)
            tot_surge += len(s["surge_idx"])
            if favs:
                sm = float(np.mean(favs)); sh = 100.0 * float(np.mean(hits))
                sess_fav.append(sm); sess_hit.append(sh)
                sess_opp.append(float(np.mean(opps))); sess_rnd.append(float(np.mean(rnds)))
                pool_fav += favs; pool_hit += hits; pool_opp += opps; pool_rnd += rnds
                print(f"{s['d']:11s} {len(s['surge_idx']):6d} {len(favs):5d} {sh:5.1f}% "
                      f"{sm:+8.3f} {np.mean(opps):+8.3f} {np.mean(rnds):+8.3f}   "
                      f"{s['p90']:7.2f}   {s['n_spx_bad']}/{s['n_mock']}/{s['n_off']}/{s['n_gd']}")
            else:
                print(f"{s['d']:11s} {len(s['surge_idx']):6d} {0:5d}  (no measurable fwd move)")
        # pooled + session-clustered
        print("-" * 100)
        fmean, fse, ft, fn = session_clustered(sess_fav)
        hmean, hse, ht_unused, hn = session_clustered(sess_hit)
        # hit-rate t vs 50% (session-level)
        ha = np.array(sess_hit, float)
        ht = ((ha.mean() - 50.0) / (ha.std(ddof=1) / np.sqrt(len(ha)))) if len(ha) > 1 and ha.std(ddof=1) > 0 else float("nan")
        omean = float(np.mean(sess_opp)) if sess_opp else float("nan")
        rmean = float(np.mean(sess_rnd)) if sess_rnd else float("nan")
        pool_hit_pct = 100.0 * float(np.mean(pool_hit)) if pool_hit else float("nan")
        print(f"  POOLED points: n={len(pool_fav)} surge-measures   pooled hit={pool_hit_pct:.1f}%   "
              f"pooled mean fav={np.mean(pool_fav):+.3f}pt" if pool_fav else "  (no data)")
        print(f"  SESSION-CLUSTERED (N_eff={fn} sessions):")
        print(f"     favorable move = {fmean:+.3f} pt/surge   SE={fse:.3f}   t={ft:+.2f}  (df={fn-1})")
        print(f"     hit rate       = {hmean:.1f}%   t_vs50%={ht:+.2f}")
        print(f"     control opp    = {omean:+.3f} pt    control random = {rmean:+.3f} pt")
        print()

    # ---- flow_snapshot cross-check note -----------------------------------
    print("=" * 100)
    print("NOTE: flow_snapshot (15-50s cadence) spans the SAME 11 dates — no extra SESSIONS, and")
    print("carries corrupted spx_price (mins 67/275/415) + the same 5/29 & 6/30 drift glitches, so")
    print("it cannot raise N_eff (sessions), which is what bounds significance. Not used as primary.")
    print("=" * 100)


if __name__ == "__main__":
    main()
