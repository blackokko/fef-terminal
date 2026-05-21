"""Statistical methodology helpers — DSR, PBO, K-fold + CPCV.

Built 2026-05-21 per Claude Chat's institutional-methodology gap analysis:
the methodology layer is where retail solo platforms differ most from
professional shops. These three tools address the multiple-testing /
overfitting / in-sample-fitting blind spots that classical Sharpe and
single-window backtests can't catch.

References:
- López de Prado, "The Deflated Sharpe Ratio" (2014)
- López de Prado, Advances in Financial Machine Learning (2018), ch. 7, 12, 14
- Bailey & López de Prado, "The Probability of Backtest Overfitting" (2014)

No external deps beyond scipy (already in sidecar venv).
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Callable, Sequence

EULER_MASCHERONI = 0.5772156649015329


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

    denom_inside = 1 - skew * sr_hat + ((excess_kurt - 1) / 4.0) * (sr_hat ** 2)
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
