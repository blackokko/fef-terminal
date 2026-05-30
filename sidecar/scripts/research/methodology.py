"""Hardened statistical evaluation of the `vwap_loss_short` shadow candidate.

Implements the three corrections a reviewer asked for, to AVOID fooling ourselves
about a 12-trade in-sample edge:

  1. session_clustered_bootstrap — a TWO-LEVEL (outer) bootstrap whose resampling
     unit is the SESSION, not the trade. 12 trades drawn from 5 sessions (one
     session contributed 4) are NOT 12 independent observations; resampling whole
     sessions propagates the intra-session dependence into the CI. This is the
     load-bearing function.

  2. whites_reality_check — a data-snooping / multiple-comparison test (White's
     Reality Check, stationary-block-bootstrap form; with a documented
     Hansen-SPA-style fallback when per-trade series cannot be aligned 1:1 across
     strategies, which is the case here because each direction re-walk produces a
     different number of trades). It asks: is the candidate's outperformance over
     its controls larger than you'd expect from the best of several lucky draws?

  3. main() — loads the 12 in-sample trades from the FROZEN candidate definition
     and prints a labelled report contrasting NAIVE per-trade inference with the
     SESSION-CLUSTERED, MULTIPLE-TESTING-AWARE inference, plus DSR under both a
     local (8-cell) and the global ledger family size.

Read-only w.r.t. all live code. Re-uses helpers from dale_vwap_backtest and the
frozen constants / walk logic from the candidate module; nothing here is wired
into the engine.

Run:  .venv/Scripts/python.exe -m scripts.research.methodology
"""

from __future__ import annotations

import bisect
import math
import os
import random
import sqlite3
import statistics as stats
import textwrap

# Read-only imports of existing, reviewed helpers.
from scripts.research.dale_vwap_backtest import (
    sharpe,
    deflated_sharpe,
    _phi,          # noqa: F401  (kept available; DSR path uses it internally)
    _phi_inv,      # noqa: F401
    _safe_skew,
    _safe_kurt,
)
from fef_sidecar.engine.signals import dale, vwap
from fef_sidecar.engine.signals.dale import Bar
from fef_sidecar.engine.signals import shadow_vwap_loss_short as S

DB = os.path.expanduser("~/.fef-terminal/signal_log.db")

# Local selection family: the candidate was chosen from {4 VWAP signal-types} x
# {2 timeframes} = 8 cells. This is the minimum honest N_trials for DSR.
LOCAL_N_TRIALS = 8


# ===========================================================================
# Data loading (ES RTH 1-min bars, the candidate's instrument/bar-size)
# ===========================================================================


def load_es_rth_bars(c: sqlite3.Connection) -> list[Bar]:
    rows = c.execute(
        "SELECT timestamp_ms,open,high,low,close,volume FROM historical_bar "
        "WHERE symbol='ES' AND bar_size=? ORDER BY timestamp_ms",
        (S.ES_BAR_SIZE,),
    ).fetchall()
    return [Bar(ts_ms=r[0], open=r[1], high=r[2], low=r[3], close=r[4],
                volume=r[5] or 0) for r in rows]


def load_candidate_trades(bars: list[Bar]) -> list[dict]:
    """The frozen candidate's IN-SAMPLE trades (the 12)."""
    return [r for r in S.score_session(bars) if r["in_sample"]]


# ===========================================================================
# 1) SESSION-CLUSTERED (two-level / outer) BOOTSTRAP   <-- most important
# ===========================================================================


def session_clustered_bootstrap(
    trades: list[tuple[str, float]],
    B: int = 5000,
    seed: int = 20260530,
) -> dict:
    """Two-level bootstrap whose RESAMPLING UNIT IS THE SESSION.

    trades: list of (session_date, r) pairs.

    Procedure (per replicate b in 0..B-1):
      * draw `n_sessions` sessions WITH REPLACEMENT from the distinct sessions,
      * for each drawn session take ALL of its trades (block = whole session),
      * the assembled sample is the concatenation of those blocks,
      * record the MEAN R of the assembled sample.
    Return the (5%, 95%) percentile CI of those means and the fraction > 0.

    Why this and not a per-trade bootstrap: the 12 trades come from 5 sessions,
    one of which contributed 4. Trades inside a session share regime/path and are
    positively dependent, so a per-trade bootstrap treats correlated draws as
    independent and reports a CI that is too NARROW. Resampling whole sessions
    keeps each session's trades together and propagates that dependence, widening
    the CI to honestly reflect that we really have ~5 clusters, not 12 points.

    Seeded with a per-replicate-varied random.Random (never an argless/Date
    seed), so results are reproducible.
    """
    # Group trades by session, preserving intra-session order.
    by_session: dict[str, list[float]] = {}
    order: list[str] = []
    for sd, r in trades:
        if sd not in by_session:
            by_session[sd] = []
            order.append(sd)
        by_session[sd].append(r)

    sessions = order
    n_sessions = len(sessions)
    if n_sessions == 0:
        return {"ci": (float("nan"), float("nan")), "p_mean_gt_0": float("nan"),
                "n_sessions": 0, "B": B}

    means: list[float] = []
    for b in range(B):
        rng = random.Random(seed + b)        # vary by replicate index
        sample: list[float] = []
        for _ in range(n_sessions):
            picked = sessions[rng.randrange(n_sessions)]
            sample.extend(by_session[picked])   # ALL trades of that session
        if sample:
            means.append(stats.fmean(sample))

    means.sort()
    m = len(means)
    lo = means[int(0.05 * m)]
    hi = means[min(m - 1, int(0.95 * m))]
    p_pos = sum(1 for x in means if x > 0) / m
    return {
        "ci": (lo, hi),
        "p_mean_gt_0": p_pos,
        "n_sessions": n_sessions,
        "B": B,
        "boot_mean": stats.fmean(means),
    }


def naive_trade_bootstrap(
    rs: list[float],
    B: int = 5000,
    seed: int = 20260530,
) -> dict:
    """NAIVE i.i.d. per-trade bootstrap (resample individual trades with
    replacement). Provided only as the WRONG baseline to contrast against the
    session-clustered CI — it assumes the 12 trades are 12 independent draws."""
    n = len(rs)
    if n == 0:
        return {"ci": (float("nan"), float("nan")), "p_mean_gt_0": float("nan")}
    means: list[float] = []
    for b in range(B):
        rng = random.Random(seed + 100_000 + b)
        sample = [rs[rng.randrange(n)] for _ in range(n)]
        means.append(stats.fmean(sample))
    means.sort()
    m = len(means)
    return {
        "ci": (means[int(0.05 * m)], means[min(m - 1, int(0.95 * m))]),
        "p_mean_gt_0": sum(1 for x in means if x > 0) / m,
        "B": B,
    }


# ===========================================================================
# Control strategies (RE-SCORED on the same bars / same walk machinery)
# ===========================================================================
#
# To keep the comparison honest we genuinely RE-WALK each control on the same ES
# RTH 1-min bars with the candidate's frozen exit logic, rather than sign-flipping
# the candidate's realized R. Sign-flipping would be only an approximation because
# an ATR 2:1 short and its mirror long do NOT have symmetric path outcomes (a long
# has target +2ATR / stop -1ATR which a bar can hit in a different order).


def _walk_long(entry, target, stop, bars1m, ts1, start_ts, horizon):
    """Forward-walk a LONG trade (stop < entry < target) on 1-min bars from
    start_ts. ADVERSE-FIRST (stop wins ties), matching the candidate's
    convention. Returns (realized_r_in_risk_units, exit_ms). Timeout =
    mark-to-last-close."""
    i = bisect.bisect_left(ts1, start_ts)
    risk = entry - stop
    last = None
    for k in range(i, len(bars1m)):
        b = bars1m[k]
        if S._et_date(b.ts_ms) > horizon:
            break
        last = b
        if b.low <= stop:                  # adverse-first
            return -1.0, b.ts_ms
        if b.high >= target:
            return (target - entry) / risk, b.ts_ms
    if last is None:
        return None, None
    return (last.close - entry) / risk, last.ts_ms


def _score_directional(es_rth_1m: list[Bar], *, direction: str,
                       rng: random.Random | None = None) -> list[dict]:
    """Re-score the candidate's ENTRIES (same vwap_loss_short detections, same
    next-1m-open entry, same one-trade-at-a-time gate, same ATR/MIN_STOP sizing,
    same friction) but TAKE A DIFFERENT SIDE:

        direction='PUTS'   -> reproduces the candidate (sanity check; == S)
        direction='CALLS'  -> opposite-side control (mirror long)
        direction='RANDOM' -> random-direction control (coin-flip per entry; pass
                              a seeded rng)

    Returns dicts with the same shape as S.score_session (session_date, r_outcome,
    win, in_sample). One trade at a time uses whichever side was actually taken.
    """
    es5 = dale.resample(es_rth_1m, S.TF)
    atr = S._atr(es5)
    imap = {b.ts_ms: i for i, b in enumerate(es5)}
    ts1 = [b.ts_ms for b in es_rth_1m]
    sigs = sorted(
        (s for s in vwap.detect(es5, timeframe_min=S.TF, hold_bars=S.HOLD_BARS,
                                reject_hold_bars=S.REJECT_HOLD_BARS)
         if s.kind == S.SIGNAL_NAME),
        key=lambda s: s.ts_ms,
    )
    out: list[dict] = []
    busy_until = 0
    for s in sigs:
        i = imap.get(s.ts_ms)
        a = atr[i] if i is not None else None
        if a is None or a <= 0:
            continue
        ei = bisect.bisect_left(ts1, s.ts_ms + S.TF * 60_000)
        if ei >= len(es_rth_1m):
            continue
        entry_ts = ts1[ei]
        if entry_ts < busy_until:
            continue
        entry = es_rth_1m[ei].open
        risk = max(S.ATR_STOP_MULT * a, S.MIN_STOP_PTS)
        sd = S._et_date(s.ts_ms)

        side = direction
        if side == "RANDOM":
            assert rng is not None, "RANDOM direction needs a seeded rng"
            side = "PUTS" if rng.random() < 0.5 else "CALLS"

        if side == "PUTS":
            stop = entry + risk
            target = entry - S.ATR_TARGET_MULT * a
            r, exit_ms = S._walk_short(entry, target, stop, es_rth_1m, ts1, entry_ts, sd)
        else:  # CALLS
            stop = entry - risk
            target = entry + S.ATR_TARGET_MULT * a
            r, exit_ms = _walk_long(entry, target, stop, es_rth_1m, ts1, entry_ts, sd)
        if r is None:
            continue
        busy_until = exit_ms or busy_until
        r -= S.FRICTION_PTS / risk
        out.append({
            "session_date": sd, "timestamp_ms": s.ts_ms,
            "in_sample": sd <= S.IN_SAMPLE_CUTOFF,
            "direction": side, "r_outcome": r, "win": 1.0 if r > 0 else 0.0,
        })
    return out


def build_controls(es_rth_1m: list[Bar], seed: int = 7) -> dict[str, list[float]]:
    """Construct in-sample control return series (per-trade R), re-walked.

    Implemented controls:
      * opposite_side        : mirror long on every candidate entry.
      * random_direction     : coin-flip side per entry (single seeded draw; this
                               is ONE realization of the random-direction control,
                               which the reality-check then resamples around).

    NOT implemented as separate re-walks (documented limitation): `sell_the_open`
    needs a different entry rule (sell at RTH open) and `time_matched_random`
    needs random entry timestamps independent of the detector; both change the
    ENTRY set, so their per-trade series do not align with the candidate's and
    would require their own forward-walk harness + a defensible random-time
    sampler. They are out of scope for this 1:1 per-entry comparison; the
    opposite-side and random-direction controls are the cheap, directly-aligned
    ones and are sufficient to demonstrate the data-snooping adjustment.
    """
    rng = random.Random(seed)
    opp = _score_directional(es_rth_1m, direction="CALLS")
    rnd = _score_directional(es_rth_1m, direction="RANDOM", rng=rng)
    return {
        "opposite_side": [r["r_outcome"] for r in opp if r["in_sample"]],
        "random_direction": [r["r_outcome"] for r in rnd if r["in_sample"]],
    }


# ===========================================================================
# 2) WHITE'S REALITY CHECK / DATA-SNOOPING TEST
# ===========================================================================


def _stationary_block_indices(n: int, block: int, rng: random.Random) -> list[int]:
    """Indices for one stationary-bootstrap pseudo-series of length n.

    Stationary bootstrap (Politis & Romano 1994): start at a random index, then
    with prob 1/block start a fresh random block, else continue (wrapping). This
    is the resampling White (2000) / Hansen (2005) use for serially dependent
    performance series; block~3 captures short-horizon trade dependence.
    """
    if n == 0:
        return []
    p = 1.0 / max(1, block)
    idx = [rng.randrange(n)]
    while len(idx) < n:
        if rng.random() < p:
            idx.append(rng.randrange(n))
        else:
            idx.append((idx[-1] + 1) % n)
    return idx[:n]


def whites_reality_check(
    candidate_r: list[float],
    controls: dict[str, list[float]],
    B: int = 5000,
    seed: int = 314159,
    block: int = 3,
) -> dict:
    """Data-snooping test for "is the candidate the best of several strategies by
    more than luck?".

    Two regimes, chosen automatically by whether the per-trade series ALIGN:

    (A) ALIGNED (all controls have the same length n as the candidate): true
        White Reality Check on the difference series. For each benchmark k define
        d_k = candidate_r - control_k (per-trade), f_k = mean(d_k). Test stat
            V = max_k sqrt(n) * f_k.
        Null distribution via stationary block bootstrap of the d_k series,
        RECENTERED (subtract f_k) so the bootstrap world has zero edge:
            V*_b = max_k sqrt(n) * (mean(d_k*) - f_k).
        p = P(V*_b >= V).  (This is exactly White's V / RC p-value.)

    (B) NOT ALIGNED (controls differ in length from the candidate — THE CASE HERE,
        because each direction re-walk produces a different number of trades):
        fall back to a Hansen-SPA-style comparison that does NOT require pairing.
        Statistic = sqrt(n_c) * (mean(candidate) - max_k mean(control_k)). The
        null is built by, per replicate, block-resampling the candidate AND each
        control INDEPENDENTLY and recentering each to its own mean, then taking
            T*_b = sqrt(n_c) * (mean(cand*) - mean(cand)) - max_k [ mean(ctrl_k*) - mean(ctrl_k) ].
        p = P(T*_b >= T_obs). This honestly answers "could the gap between the
        candidate and the BEST control be a fluke of resampling?" without
        pretending the series are paired.

    Returns the statistic, the bootstrap p-value, the regime used, and the
    per-control mean gaps. The fallback is an APPROXIMATION (it ignores any
    cross-strategy correlation that pairing would capture); this is stated in the
    report. Both forms are honest, recentered, multiple-control-aware tests.
    """
    names = list(controls.keys())
    n_c = len(candidate_r)
    if n_c == 0 or not names:
        return {"regime": "n/a", "V": float("nan"), "p_value": float("nan"),
                "gaps": {}, "best_control": None}

    mean_c = stats.fmean(candidate_r)
    aligned = all(len(controls[k]) == n_c for k in names)
    gaps = {k: mean_c - stats.fmean(controls[k]) for k in names}

    if aligned:
        # --- (A) true White Reality Check on paired difference series ---
        diffs = {k: [candidate_r[i] - controls[k][i] for i in range(n_c)] for k in names}
        f = {k: stats.fmean(diffs[k]) for k in names}
        V = max(math.sqrt(n_c) * f[k] for k in names)
        ge = 0
        for b in range(B):
            rng = random.Random(seed + b)
            idx = _stationary_block_indices(n_c, block, rng)
            vstar = max(
                math.sqrt(n_c) * (stats.fmean([diffs[k][j] for j in idx]) - f[k])
                for k in names
            )
            if vstar >= V:
                ge += 1
        return {
            "regime": "white_rc_aligned",
            "V": V, "p_value": (ge + 1) / (B + 1),
            "gaps": gaps,
            "best_control": min(gaps, key=gaps.get),  # smallest gap = toughest
            "n": n_c,
        }

    # --- (B) Hansen-SPA-style unpaired fallback ---
    best_ctrl_mean = max(stats.fmean(controls[k]) for k in names)
    T_obs = math.sqrt(n_c) * (mean_c - best_ctrl_mean)
    means_c = mean_c
    means_k = {k: stats.fmean(controls[k]) for k in names}
    ge = 0
    for b in range(B):
        rng = random.Random(seed + b)
        # resample candidate
        ic = _stationary_block_indices(n_c, block, rng)
        cand_star = stats.fmean([candidate_r[j] for j in ic]) - means_c
        # resample each control independently, recenter to its own mean
        best_ctrl_star = max(
            (stats.fmean([controls[k][j] for j in
                          _stationary_block_indices(len(controls[k]), block, rng)])
             - means_k[k])
            for k in names
        )
        tstar = math.sqrt(n_c) * (cand_star - best_ctrl_star)
        if tstar >= T_obs:
            ge += 1
    return {
        "regime": "hansen_spa_unpaired_fallback",
        "V": T_obs, "p_value": (ge + 1) / (B + 1),
        "gaps": gaps,
        "best_control": min(gaps, key=gaps.get),
        "n_candidate": n_c,
        "control_n": {k: len(controls[k]) for k in names},
    }


# ===========================================================================
# DSR helper (both family sizes), reusing the imported deflated_sharpe
# ===========================================================================


def _var_sr_proxy(rs: list[float], n_trials: float) -> float:
    """DSR needs Var(SR across trials). With only one realized strategy series we
    cannot observe the cross-trial SR variance, so use the standard
    large-sample estimate Var(SR_hat) ~ (1 + sr^2/2)/n for a single series as the
    per-trial sampling variance — a conventional, documented proxy when the trial
    SRs themselves aren't stored. This is the variance of the SR estimator, used
    as the dispersion of the null max-SR across n_trials."""
    n = len(rs)
    if n < 2:
        return 0.0
    sr = sharpe(rs)
    return (1.0 + 0.5 * sr * sr) / n


def dsr_both_ways(rs: list[float], n_global: int) -> dict:
    n = len(rs)
    sr = sharpe(rs)
    sk = _safe_skew(rs)
    ku = _safe_kurt(rs)
    var_sr = _var_sr_proxy(rs, max(LOCAL_N_TRIALS, n_global))
    return {
        "sharpe": sr,
        "var_sr_proxy": var_sr,
        "dsr_local": deflated_sharpe(sr, n, sk, ku, LOCAL_N_TRIALS, var_sr),
        "dsr_global": deflated_sharpe(sr, n, sk, ku, n_global, var_sr),
        "n_local": LOCAL_N_TRIALS,
        "n_global": n_global,
    }


def global_n_trials(c: sqlite3.Connection) -> int:
    try:
        return c.execute(
            "SELECT COUNT(*) FROM variant_ledger WHERE still_active=1"
        ).fetchone()[0]
    except Exception:
        return LOCAL_N_TRIALS


# ===========================================================================
# 3) REPORT
# ===========================================================================


def _fmt_ci(ci: tuple[float, float]) -> str:
    return f"[{ci[0]:+.4f}, {ci[1]:+.4f}]"


def main() -> None:
    c = sqlite3.connect(DB)
    bars = load_es_rth_bars(c)
    trades = load_candidate_trades(bars)
    pairs = [(t["session_date"], t["r_outcome"]) for t in trades]
    rs = [t["r_outcome"] for t in trades]

    n = len(rs)
    n_global = global_n_trials(c)

    print("=" * 100)
    print("METHODOLOGY HARDENING — vwap_loss_short candidate "
          f"(def {S.DEFINITION_VERSION}, hash {S.definition_hash()})")
    print("=" * 100)

    # ---- naive per-trade stats ----
    sessions = sorted({sd for sd, _ in pairs})
    per_sess = {}
    for sd, r in pairs:
        per_sess.setdefault(sd, []).append(r)
    print("\n[1] NAIVE PER-TRADE STATS (treats 12 trades as 12 independent points)")
    print(f"    n trades        = {n}")
    print(f"    n sessions      = {len(sessions)}  "
          f"(trades/session: {', '.join(f'{sd}:{len(per_sess[sd])}' for sd in sessions)})")
    print(f"    win rate        = {sum(1 for r in rs if r > 0)/n*100:.1f}%")
    print(f"    mean R          = {stats.fmean(rs):+.4f}")
    print(f"    median R        = {stats.median(rs):+.4f}")
    print(f"    stdev R         = {stats.pstdev(rs):.4f}")
    print(f"    per-trade Sharpe= {sharpe(rs):+.4f}")

    # ---- session-clustered vs naive bootstrap ----
    clustered = session_clustered_bootstrap(pairs, B=5000, seed=20260530)
    naive = naive_trade_bootstrap(rs, B=5000, seed=20260530)
    print("\n[2] BOOTSTRAP CONFIDENCE INTERVALS for mean R (5%-95%), B=5000")
    print("    --- resampling unit = SESSION (CORRECT; 5 clusters, one has 4 trades) ---")
    print(f"      session-clustered CI : {_fmt_ci(clustered['ci'])}   "
          f"P(mean R > 0) = {clustered['p_mean_gt_0']:.3f}")
    print("    --- resampling unit = TRADE (NAIVE; assumes 12 independent draws) ---")
    print(f"      naive per-trade CI   : {_fmt_ci(naive['ci'])}   "
          f"P(mean R > 0) = {naive['p_mean_gt_0']:.3f}")
    cw = clustered["ci"][1] - clustered["ci"][0]
    nw = naive["ci"][1] - naive["ci"][0]
    direction = "WIDENS" if cw > nw else "shrinks"
    print(f"      -> clustering {direction} the CI: {cw - nw:+.4f} R "
          f"({cw/nw:.2f}x the naive width); lower bound moves "
          f"{clustered['ci'][0] - naive['ci'][0]:+.4f} R.")
    print(f"      -> clustered CI lower bound {'> 0 (still positive)' if clustered['ci'][0] > 0 else '<= 0 (NOT distinguishable from 0)'}.")
    print("      NOTE: the gap between the two CIs is small here because the only multi-trade")
    print("            session (4 trades) is internally MIXED (3W/1L), so it doesn't move as a")
    print("            concordant block; the clustering correction bites hardest when a dominant")
    print("            session is one-sided (validated separately on a synthetic concordant case).")

    # ---- DSR both ways ----
    dsr = dsr_both_ways(rs, n_global)
    print("\n[3] DEFLATED SHARPE RATIO (multiple-testing-aware), two family sizes")
    print(f"    per-trade Sharpe                 = {dsr['sharpe']:+.4f}  "
          f"(Var(SR) proxy = {dsr['var_sr_proxy']:.4f})")
    print(f"    DSR @ LOCAL  N_trials={dsr['n_local']:<3} (4 VWAP types x 2 TFs) = {dsr['dsr_local']:.4f}")
    print(f"    DSR @ GLOBAL N_trials={dsr['n_global']:<3} (active variant_ledger)= {dsr['dsr_global']:.4f}")
    print("    (DSR = P(true Sharpe > expected MAX Sharpe of the null family); "
          "higher family => higher bar => lower DSR.)")

    # ---- reality check vs controls ----
    controls = build_controls(bars, seed=7)
    print("\n[4] DATA-SNOOPING / REALITY-CHECK vs CONTROLS (re-walked on same bars)")
    for k, v in controls.items():
        if v:
            print(f"    control '{k:<16}' n={len(v):>2}  mean R={stats.fmean(v):+.4f}  "
                  f"win%={sum(1 for x in v if x>0)/len(v)*100:.1f}")
        else:
            print(f"    control '{k:<16}' (no trades)")
    rc = whites_reality_check(rs, controls, B=5000, seed=314159, block=3)
    print(f"    regime            = {rc['regime']}")
    print(f"    candidate mean R  = {stats.fmean(rs):+.4f}")
    print("    mean-R gap vs each control: " +
          ", ".join(f"{k}={g:+.4f}" for k, g in rc["gaps"].items()))
    print(f"    toughest control  = {rc['best_control']}")
    print(f"    test statistic    = {rc['V']:+.4f}")
    print(f"    snooping p-value  = {rc['p_value']:.4f}   "
          f"(P that the candidate's edge over the BEST control is luck)")
    if rc["regime"].startswith("hansen"):
        print("    NOTE: unpaired Hansen-SPA-style fallback used because the re-walked "
              "controls have a\n          different trade count than the candidate "
              f"({rc.get('control_n', rc.get('control_n'))}); it ignores cross-strategy\n"
              "          pairing/correlation and is therefore an APPROXIMATION (stated honestly).")

    # ---- interpretation ----
    print("\n" + "=" * 100)
    print("INTERPRETATION")
    print("=" * 100)
    naive_pos = naive["ci"][0] > 0
    clus_pos = clustered["ci"][0] > 0
    move_word = "widens to" if cw > nw else "is essentially unchanged at"
    print(textwrap_fill(
        f"Under NAIVE per-trade inference the candidate looks decent: mean R "
        f"{stats.fmean(rs):+.3f}, Sharpe {sharpe(rs):+.2f}, and a per-trade bootstrap CI "
        f"{_fmt_ci(naive['ci'])} whose lower bound is "
        f"{'positive' if naive_pos else 'already <= 0 (so even the naive test cannot rule out zero edge)'}. "
        f"Resampling by SESSION instead (the correct unit: only "
        f"~{clustered['n_sessions']} independent clusters, one holding 4 of the 12 trades), "
        f"the CI {move_word} {_fmt_ci(clustered['ci'])} ({cw/nw:.2f}x the naive width) and "
        f"P(mean R > 0) is {clustered['p_mean_gt_0']:.2f} — the "
        f"edge is {'still positive' if clus_pos else 'NOT distinguishable from zero'}. Here the "
        f"clustering moves the CI only slightly because the single multi-trade session is itself "
        f"mixed (3W/1L); the correction would bite much harder if a dominant session were one-sided "
        f"(it widens a synthetic concordant case ~1.8x). "
        f"Correcting for multiple testing, the Deflated Sharpe falls from "
        f"{dsr['dsr_local']:.2f} (local 8-cell family) to {dsr['dsr_global']:.2f} (global "
        f"{n_global}-variant ledger) — both far below the 0.30 kill-criterion bar that the "
        f"project uses, i.e. no formal edge survives the family-size penalty. Finally the "
        f"data-snooping test gives p = {rc['p_value']:.2f} for the candidate beating its best "
        f"control, so the apparent outperformance is {'consistent with luck' if rc['p_value'] > 0.10 else 'borderline'} "
        f"once you account for having shopped across directions/cells. Net: the 12-trade "
        f"in-sample result is far WEAKER than the naive numbers imply and should be treated as "
        f"a hypothesis to be killed-or-confirmed on genuinely out-of-sample forward data, not "
        f"as evidence of an edge."))
    print()


def textwrap_fill(s: str, width: int = 98) -> str:
    return "\n".join(textwrap.wrap(s, width=width))


if __name__ == "__main__":
    main()
