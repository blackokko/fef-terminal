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


def sharpe_distribution_sequential(
    returns: Sequence[float],
    signal_start_times_ms: Sequence[int],
    n_bootstraps: int = 1000,
    label_horizon_minutes: int = 60,
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
        label_horizon_minutes: forward label window (default 60).
        seed: random seed for reproducibility.

    Returns: dict with sharpe_mean/std/CI/p_negative for the sequential
    bootstrap, plus classical_bootstrap_comparison block for context.
    """
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
