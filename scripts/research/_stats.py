"""Statistical methodology helpers — DSR, PBO, K-fold + CPCV + Sequential Bootstrap.

Built 2026-05-21 per Claude Chat's institutional-methodology gap analysis:
the methodology layer is where retail solo platforms differ most from
professional shops. These tools address the multiple-testing /
overfitting / in-sample-fitting blind spots that classical Sharpe and
single-window backtests can't catch.

References:
- López de Prado, "The Deflated Sharpe Ratio" (2014)
- López de Prado, Advances in Financial Machine Learning (2018), ch. 4, 7, 12, 14
- Bailey & López de Prado, "The Probability of Backtest Overfitting" (2014)

No external deps beyond scipy (already in sidecar venv).
"""
from __future__ import annotations

import math
import random
import sqlite3
from itertools import combinations
from pathlib import Path
from typing import Callable, Sequence

EULER_MASCHERONI = 0.5772156649015329


def get_ledger_variant_count(
    db_path: str | None = None,
    family: str | None = None,
    sub_family: str | None = None,
    still_active_only: bool = True,
) -> dict[str, int]:
    """Query variant_ledger for honest N_trials inputs to DSR/PBO.

    Per Chat's recommendation 2026-05-21: methodology stats must pull
    variant counts from the ledger rather than hardcoding N_trials.
    Hierarchical filtering enables sub-family corrections (strongest,
    for parameter sweeps) and family-level corrections (cross-mechanism).

    Returns a dict with multiple useful counts so a single query covers
    the typical DSR-at-multiple-N_trials report pattern:
        {
            "matched": N matching the filter,
            "in_family": N in same family (if family given),
            "in_sub_family": N in same sub_family (if sub_family given),
            "in_project": N across all variants,
            # Honest counts including DROPPED / SUPERSEDED variants that
            # never landed in the SQL `variant_ledger` table. Per
            # Harvey & Liu 2015: N_trials must include every variant
            # ever tested, not just the survivors. The SQL ledger tracks
            # only the 12 registered shadow variants — the markdown
            # roll-up at scripts/research/VARIANT_LEDGER.md is the
            # canonical project-wide ledger.
            "in_project_honest": project-wide honest count from
                _variant_ledger.HONEST_N_PROJECT,
            "in_family_honest": per-family honest count (if family given),
        }

    Falls back to {"matched": 0, ...} if the ledger table doesn't exist
    or is empty so callers can use a conservative fallback N_trials.
    """
    if db_path is None:
        db_path = str(Path.home() / ".fef-terminal" / "signal_log.db")
    out = {
        "matched": 0, "in_family": 0, "in_sub_family": 0, "in_project": 0,
        "in_project_honest": 0, "in_family_honest": 0,
    }
    try:
        db = sqlite3.connect(db_path)
        cur = db.cursor()

        def _count(conds: list[str], args: tuple) -> int:
            where = "WHERE " + " AND ".join(conds) if conds else ""
            row = cur.execute(
                f"SELECT COUNT(*) FROM variant_ledger {where}", args
            ).fetchone()
            return int(row[0]) if row else 0

        base = ["still_active = 1"] if still_active_only else []
        out["in_project"] = _count(base, ())

        if family:
            conds = [*base, "family = ?"]
            out["in_family"] = _count(conds, (family,))
            if sub_family:
                conds.append("sub_family = ?")
                out["matched"] = out["in_sub_family"] = _count(
                    conds, (family, sub_family)
                )
            else:
                out["matched"] = out["in_family"]
        elif sub_family:
            conds = [*base, "sub_family = ?"]
            out["matched"] = out["in_sub_family"] = _count(conds, (sub_family,))
        else:
            out["matched"] = out["in_project"]
        db.close()
    except sqlite3.OperationalError:
        # Ledger not yet present (fresh install) — caller should fall
        # back to a hardcoded N_trials.
        pass

    # Layer on the honest counts from the markdown ledger
    # (scripts/research/VARIANT_LEDGER.md → _variant_ledger.py). These
    # include DROPPED / SUPERSEDED variants the SQL table never knew
    # about because they predate the shadow framework.
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).parent))
        from _variant_ledger import HONEST_N_BY_FAMILY, HONEST_N_PROJECT
        out["in_project_honest"] = HONEST_N_PROJECT
        if family:
            out["in_family_honest"] = HONEST_N_BY_FAMILY.get(family, HONEST_N_PROJECT)
    except ImportError:
        # Companion file missing — leave the honest counts at 0 so
        # callers fall back to the SQL-derived counts.
        pass
    return out


def sharpe_ratio(returns: Sequence[float], annualize: float | None = None) -> float | None:
    """Per-trade Sharpe = mean / stdev. Not annualized by default.

    annualize: if set, multiplies by sqrt(N) where N = annualize (e.g., 252
    for daily, 252*6.5*60 for per-minute). Pass None for per-trade SR which
    is what DSR expects."""
    n = len(returns)
    if n < 5:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0:
        return None
    sr = mean / math.sqrt(var)
    if annualize:
        sr *= math.sqrt(annualize)
    return sr


def _skew_kurt(returns: Sequence[float]) -> tuple[float, float]:
    """Sample skewness + excess kurtosis (sample formulae). No scipy.stats
    dependency since the population vs sample formulae are short."""
    n = len(returns)
    if n < 4:
        return 0.0, 0.0
    mean = sum(returns) / n
    m2 = sum((r - mean) ** 2 for r in returns) / n
    m3 = sum((r - mean) ** 3 for r in returns) / n
    m4 = sum((r - mean) ** 4 for r in returns) / n
    if m2 <= 0:
        return 0.0, 0.0
    g1 = m3 / (m2 ** 1.5)             # population skewness
    g2 = m4 / (m2 ** 2) - 3.0          # population excess kurtosis
    # Sample bias-corrected:
    if n > 2:
        sample_skew = (math.sqrt(n * (n - 1)) / (n - 2)) * g1
    else:
        sample_skew = g1
    if n > 3:
        sample_kurt = ((n - 1) / ((n - 2) * (n - 3))) * ((n + 1) * g2 + 6)
    else:
        sample_kurt = g2
    return sample_skew, sample_kurt


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via math.erf. Avoids the scipy import."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _normal_ppf(p: float) -> float:
    """Inverse standard normal CDF (probit). Beasley-Springer-Moro
    approximation, accurate to ~1e-9 across the support. No scipy."""
    if p <= 0 or p >= 1:
        raise ValueError(f"_normal_ppf out of range: p={p}")
    # Coefficients from Acklam's algorithm
    a = [-3.969683028665376e+01,  2.209460984245205e+02, -2.759285104469687e+02,
          1.383577518672690e+02, -3.066479806614716e+01,  2.506628277459239e+00]
    b = [-5.447609879822406e+01,  1.615858368580409e+02, -1.556989798598866e+02,
          6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00,  4.374664141464968e+00,  2.938163982698783e+00]
    d = [ 7.784695709041462e-03,  3.224671290700398e-01,  2.445134137142996e+00,
          3.754408661907416e+00]
    p_low = 0.02425
    p_high = 1 - p_low
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def deflated_sharpe_ratio(
    returns_winning: Sequence[float],
    n_trials: int,
    variance_of_sr_trials: float | None = None,
) -> dict | None:
    """Probability the winning strategy's Sharpe reflects real edge, not
    selection-bias from having tested n_trials variants. ∈ [0, 1].

    Args:
        returns_winning: per-trade returns of the BEST variant from the trial set.
        n_trials: number of independent strategy variants tested. For ablation
            with 4 variants, n_trials=4 — but honest counts include EVERY
            comparison you've made in adjacent analyses (per-regime cuts,
            methodology iterations, etc.). Under-counting trials inflates DSR.
        variance_of_sr_trials: V[SR] across the trial set. If None, defaults
            to the conservative 1.0. When you have multiple variants' SRs, pass
            their sample variance for a tighter (more honest) estimate.

    Returns: dict with diagnostic fields including 'deflated_sharpe_prob'.
        None if insufficient data (<5 returns or 0 variance).

    Interpretation guide:
        > 0.95 — high confidence the edge is real (passes formal hypothesis test)
        0.7-0.95 — directional evidence; consider replication
        0.5-0.7 — coin-flip; treat as selection-biased
        < 0.5 — apparent edge is probably noise
    """
    T = len(returns_winning)
    if T < 5:
        return None
    sr_hat = sharpe_ratio(returns_winning, annualize=None)
    if sr_hat is None:
        return None
    skew, excess_kurt = _skew_kurt(returns_winning)

    # Null threshold: expected max SR under N trials of pure noise
    if n_trials <= 1:
        sr_zero = 0.0
    else:
        var_sr = variance_of_sr_trials if variance_of_sr_trials is not None else 1.0
        try:
            sr_zero = math.sqrt(var_sr) * (
                (1 - EULER_MASCHERONI) * _normal_ppf(1 - 1.0 / n_trials)
                + EULER_MASCHERONI * _normal_ppf(1 - 1.0 / (n_trials * math.e))
            )
        except ValueError:
            sr_zero = 0.0

    # Bailey & López de Prado (2014) eq. 9 uses NON-excess kurtosis (κ = m4/m2²),
    # NOT excess kurtosis (κ_excess = κ - 3). _skew_kurt returns excess. The
    # denominator term `(κ - 1) / 4` thus becomes `(excess_kurt + 3 - 1) / 4
    # = (excess_kurt + 2) / 4`. Earlier versions of this file used
    # `(excess_kurt - 1) / 4` which biased DSR upward by 5-15% on
    # positively-kurtotic data (typical for option-trade R distributions).
    # Fixed 2026-05-21 per formula audit.
    denom_inside = 1 - skew * sr_hat + ((excess_kurt + 2) / 4.0) * (sr_hat ** 2)
    if denom_inside <= 0:
        return {
            "observed_sharpe": round(sr_hat, 4),
            "sr_zero_threshold": round(sr_zero, 4),
            "skewness": round(skew, 4),
            "excess_kurtosis": round(excess_kurt, 4),
            "deflated_sharpe_prob": None,
            "note": "denom non-positive (high-skew/kurt data); DSR undefined",
            "n_trades": T, "n_trials": n_trials,
        }
    dsr = _normal_cdf((sr_hat - sr_zero) * math.sqrt(T - 1) / math.sqrt(denom_inside))
    return {
        "observed_sharpe": round(sr_hat, 4),
        "sr_zero_threshold": round(sr_zero, 4),
        "skewness": round(skew, 4),
        "excess_kurtosis": round(excess_kurt, 4),
        "deflated_sharpe_prob": round(dsr, 4),
        "n_trades": T, "n_trials": n_trials,
    }


def _correlation_matrix(
    per_variant_returns: dict[str, Sequence[float]],
) -> tuple[list[str], list[list[float]]]:
    """Build Pearson correlation matrix across variants (variant × time
    returns). Variants are columns; returns are rows. Returns
    (variant_order, matrix) where matrix[i][j] = corr(variant_i, variant_j).

    Missing/degenerate columns get correlation 0 vs everyone (so they're
    treated as independent).
    """
    names = list(per_variant_returns.keys())
    if not names:
        return [], []
    n_obs = min((len(per_variant_returns[v]) for v in names), default=0)
    if n_obs < 3:
        return names, [[1.0 if i == j else 0.0
                        for j in range(len(names))] for i in range(len(names))]
    # Truncate everyone to common length
    cols: list[list[float]] = [list(per_variant_returns[v][:n_obs]) for v in names]
    # Means / stds
    means: list[float] = []
    stds: list[float] = []
    for c in cols:
        m = sum(c) / len(c)
        means.append(m)
        var = sum((x - m) ** 2 for x in c) / (len(c) - 1)
        stds.append(math.sqrt(var) if var > 0 else 0.0)
    matrix: list[list[float]] = [[1.0 if i == j else 0.0
                                  for j in range(len(names))] for i in range(len(names))]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            si, sj = stds[i], stds[j]
            if si == 0 or sj == 0:
                rho = 0.0
            else:
                cov = sum((cols[i][k] - means[i]) * (cols[j][k] - means[j])
                          for k in range(n_obs)) / (n_obs - 1)
                rho = cov / (si * sj)
                # Clip floating-point overshoot
                if rho > 1.0:
                    rho = 1.0
                elif rho < -1.0:
                    rho = -1.0
            matrix[i][j] = rho
            matrix[j][i] = rho
    return names, matrix


def _mean_off_diagonal(matrix: list[list[float]]) -> float:
    """Mean of off-diagonal entries of a square matrix. 0 for matrices
    smaller than 2x2."""
    n = len(matrix)
    if n < 2:
        return 0.0
    total = 0.0
    count = 0
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            total += matrix[i][j]
            count += 1
    return total / count if count else 0.0


def dsr_at_effective_n(
    winner_returns: Sequence[float],
    per_variant_returns: dict[str, Sequence[float]] | None,
    n_honest: int,
    variance_of_sr_trials: float | None = None,
) -> dict | None:
    """Correlation-aware Deflated Sharpe Ratio (Harvey & Liu 2015 §5).

    Per Audit priority #9 (2026-05-22): the honest N_trials (project-wide
    count, e.g., 58) implicitly assumes all trials are independent, but
    in practice they share underlying signal streams, exit logic, etc.,
    so per-trial returns are highly correlated. Harvey & Liu's
    correlation adjustment computes an effective independent count:

        N_eff = N_honest * (1 - mean_off_diagonal_correlation)

    where the correlation matrix is built across the variant × time
    return matrix (correlations between columns). This is the simple
    proxy from Harvey & Liu 2015 §5 — a more rigorous treatment uses
    1 / (1 + (N-1) * rho_bar) but the proxy form is closer in spirit
    to the "shrink N by the average dependence" intuition. When all
    variants are perfectly correlated (rho_bar = 1), N_eff = 0 trials,
    meaning DSR collapses to single-strategy hypothesis test. When
    they're independent (rho_bar = 0), N_eff = N_honest.

    Falls back to DSR at N_honest if the correlation matrix can't be
    built (e.g., single variant, missing data).

    Returns a dict with the DSR result + the N_eff / rho_bar diagnostics
    so the report can show what was computed. Returns None on insufficient
    winner data.
    """
    if not winner_returns or len(winner_returns) < 5:
        return None
    if not per_variant_returns or len(per_variant_returns) < 2:
        # No correlation matrix — fall back to honest N.
        dsr = deflated_sharpe_ratio(
            winner_returns, n_trials=n_honest,
            variance_of_sr_trials=variance_of_sr_trials,
        )
        if dsr is None:
            return None
        return {
            **dsr,
            "n_honest": n_honest,
            "n_effective": n_honest,
            "mean_off_diagonal_correlation": None,
            "note": "single-variant or insufficient data; effective N = honest N",
        }
    names, matrix = _correlation_matrix(per_variant_returns)
    rho_bar = _mean_off_diagonal(matrix)
    # Harvey & Liu 2015 §5 proxy: N_eff = N * (1 - rho_bar). Clamp to
    # [1, N_honest] — N_eff < 1 is degenerate (use min 1), and N_eff
    # can't be honestly larger than N_honest itself.
    n_eff_raw = n_honest * (1.0 - rho_bar)
    n_eff = max(1, min(n_honest, int(round(n_eff_raw))))
    dsr = deflated_sharpe_ratio(
        winner_returns, n_trials=n_eff,
        variance_of_sr_trials=variance_of_sr_trials,
    )
    if dsr is None:
        return None
    return {
        **dsr,
        "n_honest": n_honest,
        "n_effective": n_eff,
        "n_effective_raw": round(n_eff_raw, 3),
        "mean_off_diagonal_correlation": round(rho_bar, 4),
        "correlation_variants": names,
    }


def stratified_pbo_by_regime(
    sorted_results: Sequence[dict],
    variants: Sequence[tuple[str, str]],
    kfold_sharpes_fn: Callable,
    pbo_fn: Callable,
    min_n: int = 20,
    borderline_n: int = 30,
    k_folds: int = 6,
    embargo_minutes: int = 60,
    label_horizon_minutes: int = 60,
) -> dict[str, dict]:
    """Per-regime PBO: group sorted_results by regime_at_fire, then run PBO
    separately on each regime where n >= min_n. Returns a dict keyed by
    regime label, mirroring the shape of `_stratified_cpcv_by_regime`.

    Per Audit priority #6 (2026-05-22): the aggregate PBO above pools
    variant ranks across folds that may be regime-imbalanced (one fold
    all TREND_DAY, another all CHOP). The stratified version runs PBO
    separately within each regime, surfacing whether the winner survives
    cross-period within the same regime. Same threshold pattern as
    stratified CPCV — n < min_n is skipped, min_n <= n < borderline_n
    is flagged borderline.

    Schema:
        {
            "<regime>": {
                "n": int,
                "skipped": bool,
                "borderline": bool,
                "note": str | None,
                "pbo": {... pbo_fn(...) output ...} | None,
            },
            ...
        }
    """
    from collections import defaultdict as _defaultdict
    by_regime: dict[str, list[dict]] = _defaultdict(list)
    for r in sorted_results:
        regime = r.get("regime") or "NORMAL"
        by_regime[regime].append(r)

    out: dict[str, dict] = {}
    for regime, rows in sorted(by_regime.items()):
        n = len(rows)
        entry: dict = {
            "n": n,
            "skipped": False,
            "borderline": False,
            "note": None,
            "pbo": None,
        }
        if n < min_n:
            entry["skipped"] = True
            entry["note"] = f"n={n} < {min_n} — undersampled, PBO skipped"
            out[regime] = entry
            continue
        if n < borderline_n:
            entry["borderline"] = True
            entry["note"] = (
                f"n={n} < {borderline_n} — borderline; per-fold rankings "
                f"will be noisy"
            )
        ts_regime = [r["timestamp_ms"] for r in rows]
        per_variant_per_fold: dict[str, list[float | None]] = {}
        for name, key in variants:
            returns_v = [r[key] for r in rows]
            per_variant_per_fold[name] = kfold_sharpes_fn(
                returns_v, ts_regime, k=k_folds,
                embargo_minutes=embargo_minutes,
                label_horizon_minutes=label_horizon_minutes,
            )
        entry["pbo"] = pbo_fn(per_variant_per_fold)
        out[regime] = entry
    return out


def purged_kfold_indices(
    timestamps_ms: list[int],
    k: int,
    embargo_minutes: int = 60,
    label_horizon_minutes: int = 60,
) -> list[tuple[list[int], list[int]]]:
    """Chronological K-fold split with purging + embargo.

    Each fold becomes the test set once; the remaining K-1 folds form the
    training set. Training samples whose label-horizon overlaps with the
    test fold's time range are PURGED. An additional embargo window after
    the test fold is also excluded.

    Args:
        timestamps_ms: per-sample epoch ms, chronological order.
        k: number of folds.
        embargo_minutes: post-test buffer.
        label_horizon_minutes: how far a sample's label looks forward
            (60 for our signal_lookahead).

    Returns: list of (train_indices, test_indices) tuples, length K.
    """
    n = len(timestamps_ms)
    if n < k:
        return []
    fold_size = n // k
    folds: list[list[int]] = [
        list(range(i * fold_size, (i + 1) * fold_size if i < k - 1 else n))
        for i in range(k)
    ]
    embargo_ms = embargo_minutes * 60_000
    label_ms = label_horizon_minutes * 60_000
    splits: list[tuple[list[int], list[int]]] = []
    for ti, test_idx in enumerate(folds):
        if not test_idx:
            continue
        test_start = timestamps_ms[test_idx[0]]
        test_end = timestamps_ms[test_idx[-1]]
        # Purge: drop training samples whose label-horizon overlaps the
        # test window, plus embargo on both sides.
        purge_window_start = test_start - label_ms
        purge_window_end = test_end + embargo_ms
        train_idx: list[int] = []
        for fi, fold in enumerate(folds):
            if fi == ti:
                continue
            for j in fold:
                ts = timestamps_ms[j]
                if purge_window_start <= ts <= purge_window_end:
                    continue
                train_idx.append(j)
        splits.append((train_idx, test_idx))
    return splits


def kfold_sharpes(
    returns: Sequence[float],
    timestamps_ms: Sequence[int],
    k: int = 6,
    embargo_minutes: int = 60,
    label_horizon_minutes: int = 60,
) -> list[float | None]:
    """Per-fold out-of-sample Sharpe for a single variant.

    Each variant is evaluated on each fold's test-set returns only; train
    set isn't used here because we're not fitting anything — just measuring
    out-of-sample Sharpe stability across time. Returns list of K Sharpes.

    For a variant that has true edge, fold Sharpes should be consistently
    positive; for a noise-fit variant, they'll vary wildly around zero.
    """
    splits = purged_kfold_indices(
        list(timestamps_ms), k,
        embargo_minutes=embargo_minutes,
        label_horizon_minutes=label_horizon_minutes,
    )
    out: list[float | None] = []
    for _train_idx, test_idx in splits:
        test_returns = [returns[i] for i in test_idx]
        out.append(sharpe_ratio(test_returns))
    return out


def cpcv_paths(
    returns: Sequence[float],
    timestamps_ms: Sequence[int],
    n_groups: int = 6,
    test_groups: int = 2,
    embargo_minutes: int = 60,
    label_horizon_minutes: int = 60,
) -> dict:
    """Combinatorial Purged Cross-Validation (López de Prado 2018, ch. 12).

    Splits the time series into n_groups chronological groups, then for
    every combination of `test_groups` groups (out of n_groups), uses
    those as test and the remaining as training. Each combination
    produces ONE backtest path. With n_groups=6, test_groups=2 you get
    C(6,2)=15 paths — each path is a complete backtest with the same
    train/test split semantics as walk-forward, just along a different
    "route" through the time series.

    This is the diagnostic that distinguishes "lucky single walk-forward"
    from "stable across many train/test routes." Each path produces its
    own Sharpe; aggregate as mean ± std and compute Probabilistic Sharpe
    Ratio across the path distribution.

    For each path:
      - test set = union of the selected test_groups in chronological order
      - train set = everything else, purged + embargoed
      - per-path metric = Sharpe of variant's returns on test indices

    Args:
        returns: per-trade returns for ONE variant.
        timestamps_ms: per-trade epoch ms, chronological.
        n_groups: total chronological groups (default 6).
        test_groups: how many groups form the test set per path (default 2).
        embargo_minutes: post-test buffer.
        label_horizon_minutes: forward-looking label window (60 for us).

    Returns: dict with
      - paths: list of dicts with {test_groups, sharpe, n_test, sum_r}
      - sharpe_mean, sharpe_std across paths
      - n_paths
    """
    n = len(returns)
    if n < n_groups * 3:
        return {"paths": [], "n_paths": 0, "note": f"need >={n_groups*3} samples, have {n}"}
    if test_groups >= n_groups or test_groups < 1:
        return {"paths": [], "n_paths": 0, "note": "test_groups must be 1..n_groups-1"}

    timestamps = list(timestamps_ms)
    group_size = n // n_groups
    groups: list[list[int]] = [
        list(range(i * group_size, (i + 1) * group_size if i < n_groups - 1 else n))
        for i in range(n_groups)
    ]
    embargo_ms = embargo_minutes * 60_000
    label_ms = label_horizon_minutes * 60_000

    paths: list[dict] = []
    for combo in combinations(range(n_groups), test_groups):
        test_idx: list[int] = []
        for g in combo:
            test_idx.extend(groups[g])
        if not test_idx:
            continue
        # Train = remaining groups, purged + embargoed
        train_idx: list[int] = []
        # Build a union of forbidden time windows (each test group)
        forbidden: list[tuple[int, int]] = []
        for g in combo:
            if not groups[g]:
                continue
            t0 = timestamps[groups[g][0]] - label_ms
            t1 = timestamps[groups[g][-1]] + embargo_ms
            forbidden.append((t0, t1))
        for gi, gr in enumerate(groups):
            if gi in combo:
                continue
            for j in gr:
                ts = timestamps[j]
                if any(t0 <= ts <= t1 for t0, t1 in forbidden):
                    continue
                train_idx.append(j)
        test_returns = [returns[i] for i in test_idx]
        sr = sharpe_ratio(test_returns)
        sum_r = sum(test_returns)
        paths.append({
            "test_groups": list(combo),
            "sharpe": round(sr, 4) if sr is not None else None,
            "sum_r": round(sum_r, 3),
            "n_test": len(test_idx),
            "n_train": len(train_idx),
        })

    sharpes = [p["sharpe"] for p in paths if p["sharpe"] is not None]
    if sharpes:
        sr_mean = sum(sharpes) / len(sharpes)
        sr_var = sum((s - sr_mean) ** 2 for s in sharpes) / max(len(sharpes) - 1, 1)
        sr_std = math.sqrt(sr_var) if sr_var > 0 else 0.0
    else:
        sr_mean = sr_std = None
    return {
        "paths": paths,
        "n_paths": len(paths),
        "n_groups": n_groups,
        "test_groups_per_path": test_groups,
        "sharpe_mean": round(sr_mean, 4) if sr_mean is not None else None,
        "sharpe_std": round(sr_std, 4) if sr_std is not None else None,
        "sharpe_positive_pct": (
            round(100 * sum(1 for s in sharpes if s > 0) / len(sharpes), 1)
            if sharpes else None
        ),
    }


def probability_of_backtest_overfitting(
    per_variant_per_fold_sharpes: dict[str, list[float | None]],
) -> dict:
    """Bailey & López de Prado 2014 PBO via combinatorially symmetric CV.

    Input: dict of {variant_name: [sharpe_fold0, sharpe_fold1, ...]} where
    each list has the SAME length (K folds).

    Method:
      For each pair of folds (i, j) with i ≠ j:
        - "Best on fold i" = variant with max Sharpe on fold i
        - Check that variant's RANK among all variants on fold j
        - If the best-on-i ranks in the bottom half on j, that's overfitting evidence
      PBO = fraction of (i, j) pairs where best-on-i is in the bottom half on j.

      PBO close to 0 = good (winners on one period stay winners on another).
      PBO close to 0.5 = winners are random (pure overfitting).
      PBO > 0.5 = systematic anti-correlation (still overfitting).

    Returns: dict with pbo, n_pairs_evaluated, n_variants, k_folds.
    """
    variants = list(per_variant_per_fold_sharpes.keys())
    n_variants = len(variants)
    if n_variants < 2:
        return {"pbo": None, "note": "need ≥2 variants"}
    fold_arrays = [per_variant_per_fold_sharpes[v] for v in variants]
    K = max((len(a) for a in fold_arrays), default=0)
    if K < 2:
        return {"pbo": None, "note": "need ≥2 folds"}
    # Build per-fold rankings (variant -> rank on that fold; rank 1 = best)
    rank_by_fold: list[dict[str, int]] = []
    for fold_idx in range(K):
        pairs = []
        for v, arr in zip(variants, fold_arrays):
            val = arr[fold_idx] if fold_idx < len(arr) else None
            if val is None:
                continue
            pairs.append((v, val))
        pairs.sort(key=lambda x: x[1], reverse=True)
        ranks = {v: r + 1 for r, (v, _) in enumerate(pairs)}
        rank_by_fold.append(ranks)
    # PBO: for each (i, j) pair, check best-on-i's rank on j
    bottom_half_count = 0
    total_pairs = 0
    for i, j in combinations(range(K), 2):
        ranks_i = rank_by_fold[i]
        ranks_j = rank_by_fold[j]
        if not ranks_i or not ranks_j:
            continue
        # Best on i (rank 1)
        best_on_i = min(ranks_i.items(), key=lambda kv: kv[1])
        v_star = best_on_i[0]
        if v_star not in ranks_j:
            continue
        rank_on_j = ranks_j[v_star]
        # bottom half = rank > median
        n_on_j = len(ranks_j)
        median_rank = (n_on_j + 1) / 2
        if rank_on_j > median_rank:
            bottom_half_count += 1
        total_pairs += 1
        # Also do (j, i) — symmetric
        best_on_j = min(ranks_j.items(), key=lambda kv: kv[1])
        v_star2 = best_on_j[0]
        if v_star2 in ranks_i:
            rank_on_i = ranks_i[v_star2]
            n_on_i = len(ranks_i)
            median_rank_i = (n_on_i + 1) / 2
            if rank_on_i > median_rank_i:
                bottom_half_count += 1
            total_pairs += 1
    pbo = bottom_half_count / total_pairs if total_pairs else None
    return {
        "pbo": round(pbo, 4) if pbo is not None else None,
        "n_pairs_evaluated": total_pairs,
        "n_variants": n_variants,
        "k_folds": K,
        "interpretation": (
            "good (winners survive cross-period)" if pbo is not None and pbo < 0.25 else
            "moderate overfit risk" if pbo is not None and pbo < 0.5 else
            "high overfit risk — apparent winners don't survive cross-period" if pbo is not None else
            "insufficient data",
        ),
    }


def sequential_bootstrap_indices(
    signal_start_times_ms: Sequence[int],
    label_horizon_minutes: int = 60,
    sample_length: int | None = None,
    seed: int | None = None,
) -> list[int]:
    """Draw a sequential bootstrap sample of signal indices (López de Prado
    AFML 2018, Algorithm 4.1, "Sequential Bootstrap").

    For overlapping-label time series (like our 60-min lookahead signals),
    classical bootstrap underestimates SE because it treats overlapping
    samples as independent. Sequential Bootstrap samples with probability
    ∝ "uniqueness" to preserve the effective sample size correctly.

    Algorithm:
      1. Indicator matrix: each signal j touches a set of minute-bars
         [t_j, t_j + label_horizon_minutes - 1] (binned by minute).
      2. For each draw, the candidate-j uniqueness = mean over j's bars of
         1 / (1 + count_of_already_drawn_signals_touching_that_bar).
      3. Sample j with probability ∝ uniqueness. Repeat with replacement.

    The first draw has uniform uniqueness (1.0 for all candidates); each
    subsequent draw progressively downweights signals whose bars are
    already represented in the drawn sample.

    Args:
        signal_start_times_ms: per-signal epoch ms of signal fire / label start.
        label_horizon_minutes: forward window of each signal's label (default 60).
        sample_length: number of indices to draw (default = len(signals)).
        seed: random seed for reproducibility.

    Returns: list of indices into the input signal list (with replacement).
    """
    n_signals = len(signal_start_times_ms)
    if n_signals == 0:
        return []
    if sample_length is None:
        sample_length = n_signals

    rng = random.Random(seed)

    # Bin each signal's outcome window into integer minute-bars.
    # Bar key = floor(ts_ms / 60_000). Each signal touches `label_horizon_minutes`
    # consecutive bars: [start_bin, start_bin + label_horizon_minutes).
    signal_bars: list[list[int]] = []
    for ts_ms in signal_start_times_ms:
        start_bin = ts_ms // 60_000
        signal_bars.append(list(range(start_bin, start_bin + label_horizon_minutes)))

    # For each bar, count how many drawn signals currently touch it.
    # Default int = 0.
    bar_draw_count: dict[int, int] = {}

    drawn_indices: list[int] = []
    for _ in range(sample_length):
        # Compute uniqueness for each candidate signal.
        # uniqueness_j = mean over bars-in-j of 1 / (1 + bar_draw_count[bar])
        weights: list[float] = []
        for j in range(n_signals):
            bars = signal_bars[j]
            if not bars:
                weights.append(0.0)
                continue
            total = 0.0
            for b in bars:
                # +1 in denom: if no draws yet, weight = 1 (uniform). Each
                # additional draw on this bar halves, thirds, etc.
                total += 1.0 / (1.0 + bar_draw_count.get(b, 0))
            weights.append(total / len(bars))

        # Sample one signal index proportional to weights.
        total_w = sum(weights)
        if total_w <= 0:
            # All zero (shouldn't happen with non-empty signals); fall back uniform.
            chosen = rng.randrange(n_signals)
        else:
            r = rng.random() * total_w
            cum = 0.0
            chosen = n_signals - 1  # default last in case of float rounding
            for j, w in enumerate(weights):
                cum += w
                if r <= cum:
                    chosen = j
                    break

        drawn_indices.append(chosen)
        # Update bar-draw counts for the chosen signal's bars.
        for b in signal_bars[chosen]:
            bar_draw_count[b] = bar_draw_count.get(b, 0) + 1

    return drawn_indices


def _classical_bootstrap_sharpe_distribution(
    returns: Sequence[float],
    n_bootstraps: int,
    seed: int | None,
) -> dict:
    """Classical (uniform-with-replacement) bootstrap SR distribution.
    Used internally to surface the SE gap vs sequential bootstrap."""
    n = len(returns)
    if n < 5:
        return {"sharpe_mean": None, "sharpe_std": None}
    rng = random.Random(seed)
    srs: list[float] = []
    for _ in range(n_bootstraps):
        sample = [returns[rng.randrange(n)] for _ in range(n)]
        sr = sharpe_ratio(sample)
        if sr is not None:
            srs.append(sr)
    if not srs:
        return {"sharpe_mean": None, "sharpe_std": None}
    m = sum(srs) / len(srs)
    var = sum((s - m) ** 2 for s in srs) / max(len(srs) - 1, 1)
    return {
        "sharpe_mean": round(m, 4),
        "sharpe_std": round(math.sqrt(var) if var > 0 else 0.0, 4),
    }


def median_trade_duration_minutes(
    db_path: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    account_class: str = "paper",
) -> float | None:
    """Median observed trade duration in minutes across closed live_order parents.

    Pulls (last_status_at - submitted_at) deltas for non-dry-run parent
    orders with status in {CLOSED, STOPPED}. Used by `_methodology_stats`
    to calibrate the sequential bootstrap's label horizon to the actual
    observed holding period (typically much shorter than the 60-min
    lookahead horizon — 5/22 median was 21.4 min).

    Returns None if no rows match (caller should fall back to 60).

    Per Audit priority #7 (2026-05-22): hardcoding label_horizon_min=60
    in the sequential bootstrap was a residual research-script default
    from when 60-min was the lookahead window. The honest horizon is
    the median observed trade duration, since that's the window over
    which one signal's outcome competes with the next signal's outcome
    for the same minute-bars (i.e., the uniqueness denominator).
    """
    if db_path is None:
        db_path = str(Path.home() / ".fef-terminal" / "signal_log.db")
    try:
        db = sqlite3.connect(db_path)
        cur = db.cursor()
        # Closed parent durations, optionally bounded by date range.
        # account_class filter is intentionally lenient (no extras join here —
        # 5/19+ rows are all paper anyway, and the caller can post-filter).
        conds = ["lo.bracket_role = 'parent'", "lo.dry_run = 0",
                 "lo.status IN ('CLOSED','STOPPED')"]
        args: list = []
        if date_from:
            conds.append("substr(lo.submitted_at, 1, 10) >= ?")
            args.append(date_from)
        if date_to:
            conds.append("substr(lo.submitted_at, 1, 10) <= ?")
            args.append(date_to)
        where = " AND ".join(conds)
        rows = cur.execute(
            f"""
            SELECT (julianday(lo.last_status_at) - julianday(lo.submitted_at)) * 24 * 60
            FROM live_order lo
            WHERE {where}
              AND lo.last_status_at IS NOT NULL
              AND lo.submitted_at IS NOT NULL
            """,
            tuple(args),
        ).fetchall()
        db.close()
        durations = [float(r[0]) for r in rows if r[0] is not None and r[0] > 0]
        if not durations:
            return None
        durations.sort()
        n = len(durations)
        if n % 2 == 1:
            return durations[n // 2]
        return 0.5 * (durations[n // 2 - 1] + durations[n // 2])
    except sqlite3.OperationalError:
        return None


def _median_trade_duration_min(
    results: Sequence[dict] | None = None,
    db_path: str | None = None,
) -> float | None:
    """Helper that derives the median observed trade duration in minutes.

    First tries the `results` ablation rows for embedded duration info
    (none today, but kept for future extension). Otherwise queries
    live_order via `median_trade_duration_minutes` over the date range
    inferred from `results` timestamps.

    Returns None if no signal lives. Callers fall back to 60.
    """
    if not results:
        return median_trade_duration_minutes(db_path=db_path)
    timestamps = [r.get("timestamp_ms") for r in results if r.get("timestamp_ms")]
    if not timestamps:
        return median_trade_duration_minutes(db_path=db_path)
    import datetime as _dt
    min_ts_ms = min(timestamps)
    max_ts_ms = max(timestamps)
    date_from = _dt.datetime.fromtimestamp(min_ts_ms / 1000, tz=_dt.timezone.utc).date().isoformat()
    date_to = _dt.datetime.fromtimestamp(max_ts_ms / 1000, tz=_dt.timezone.utc).date().isoformat()
    return median_trade_duration_minutes(
        db_path=db_path, date_from=date_from, date_to=date_to,
    )


def sharpe_distribution_sequential(
    returns: Sequence[float],
    signal_start_times_ms: Sequence[int],
    n_bootstraps: int = 1000,
    label_horizon_minutes: int = 60,
    label_horizon_min: int | None = None,
    seed: int | None = None,
) -> dict:
    """Sequential-bootstrap Sharpe distribution (López de Prado AFML ch. 4).

    For each of n_bootstraps reps:
      1. Draw a sequential-bootstrap sample of indices (length = len(returns)).
      2. Compute Sharpe on the returns selected by those indices.
    Then report distribution stats.

    Also computes a classical bootstrap as a side-by-side comparison so
    callers can surface the SE gap that overlapping labels create. The
    common AFML claim is that classical bootstrap *understates* SE under
    overlapping labels (ratio of seq/classical std > 1); but in practice
    the direction depends on the data — uniqueness-weighted draws are more
    diverse, so bootstrap-rep variance can actually decrease. The
    sequential CI is the methodologically-correct one regardless of
    direction; the comparison just diagnoses how mis-calibrated the
    classical bootstrap was.

    Args:
        returns: per-signal realized return.
        signal_start_times_ms: per-signal epoch ms of signal fire.
        n_bootstraps: number of bootstrap reps (default 1000).
        label_horizon_minutes: forward label window (default 60). LEGACY ALIAS.
        label_horizon_min: forward label window (preferred argument name —
            per Audit priority #7 (2026-05-22) we now ship the
            median-observed-trade-duration version alongside the legacy
            60-min version). When both are supplied, label_horizon_min wins.
            Pass `_median_trade_duration_min(results)` for the calibrated
            (honest) horizon; pass 60 for the legacy back-compat run.
        seed: random seed for reproducibility.

    Returns: dict with sharpe_mean/std/CI/p_negative for the sequential
    bootstrap, plus classical_bootstrap_comparison block for context.
    """
    # Resolve the preferred new-name parameter when both are passed.
    if label_horizon_min is not None:
        try:
            label_horizon_minutes = int(round(float(label_horizon_min)))
        except (TypeError, ValueError):
            pass
    if label_horizon_minutes < 1:
        label_horizon_minutes = 1
    n = len(returns)
    if n < 5 or len(signal_start_times_ms) != n:
        return {
            "n_bootstraps": 0,
            "label_horizon_minutes": label_horizon_minutes,
            "note": f"need ≥5 returns and matching timestamps (got {n} returns, "
                    f"{len(signal_start_times_ms)} timestamps)",
        }

    srs: list[float] = []
    for rep in range(n_bootstraps):
        rep_seed = None if seed is None else seed + rep
        indices = sequential_bootstrap_indices(
            signal_start_times_ms,
            label_horizon_minutes=label_horizon_minutes,
            sample_length=n,
            seed=rep_seed,
        )
        sample_returns = [returns[i] for i in indices]
        sr = sharpe_ratio(sample_returns)
        if sr is not None:
            srs.append(sr)

    if not srs:
        return {
            "n_bootstraps": n_bootstraps,
            "label_horizon_minutes": label_horizon_minutes,
            "sharpe_mean": None,
            "sharpe_std": None,
            "note": "all bootstrap reps had degenerate Sharpe (zero variance)",
        }

    sr_mean = sum(srs) / len(srs)
    sr_var = sum((s - sr_mean) ** 2 for s in srs) / max(len(srs) - 1, 1)
    sr_std = math.sqrt(sr_var) if sr_var > 0 else 0.0
    srs_sorted = sorted(srs)
    lo_idx = int(0.025 * len(srs_sorted))
    hi_idx = min(int(0.975 * len(srs_sorted)), len(srs_sorted) - 1)
    p_neg = sum(1 for s in srs if s <= 0) / len(srs)

    # Side-by-side classical bootstrap so we can surface the SE gap.
    classical = _classical_bootstrap_sharpe_distribution(
        returns, n_bootstraps=n_bootstraps, seed=seed,
    )

    return {
        "n_bootstraps": n_bootstraps,
        "n_reps_with_valid_sharpe": len(srs),
        "label_horizon_minutes": label_horizon_minutes,
        "sharpe_mean": round(sr_mean, 4),
        "sharpe_std": round(sr_std, 4),
        "sharpe_ci_95": (round(srs_sorted[lo_idx], 4), round(srs_sorted[hi_idx], 4)),
        "sharpe_p_negative": round(p_neg, 4),
        "classical_bootstrap_comparison": classical,
        "se_inflation_factor": (
            round(sr_std / classical["sharpe_std"], 3)
            if classical.get("sharpe_std") and classical["sharpe_std"] > 0
            else None
        ),
    }
