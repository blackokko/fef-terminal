"""Pattern proposal — turn ML training data into candidate rules the user
can adopt into the existing Pattern table.

Algorithm (intentionally simple so output is human-interpretable):

For each numeric feature, scan candidate thresholds at the 20/30/40/50/60/
70/80 percentiles. For each (feature, threshold, direction) combination,
compute the historical expected-R, win-rate, and sample size of trades
that *would have matched* this single-condition rule.

A candidate is "promising" if:
  - n_match ≥ MIN_MATCHES (default 12)
  - expected_R ≥ MIN_EXPECTED_R (default +0.10R)
  - expected_R is at least MIN_R_LIFT above the global baseline so we
    surface conditions that are materially better than "fire on
    everything"

Returns the top K candidates ranked by `expected_R * sqrt(n_match)` —
that gives some weight to sample size without being dominated by it.

This produces *single-condition* candidates by design. Multi-condition
patterns would need either tree-based extraction (decision-path mining
from the RF) or exhaustive 2-feature gridding — both are next-iteration
work. Single conditions are easier to validate manually and easier to
write into the existing Pattern table schema.

These are PROPOSALS, not engine rules. They never affect verdict
generation. The user reviews and explicitly accepts them into the
`pattern` table where the existing pattern-matcher will then evaluate
them live.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass

from fef_sidecar.engine.ml_patterns import _load_training_data

logger = logging.getLogger(__name__)


# Features eligible for threshold scanning. Excludes binary/one-hot
# columns (direction one-hots, confidence ordinal) because a "threshold"
# on those is just an equality check.
SCANNABLE_FEATURES: tuple[str, ...] = (
    "drift_cum_net_m",
    "mp_distance",
    "aggression_call_pct",
    "aggression_put_pct",
    "iv_rank_call_pct",
    "iv_rank_put_pct",
    "hour_of_day",
    "top_gex_distance",
    "top_dex_distance",
    "fe_ratio",
    "delta_price",
    "delta_drift_m",
)


# Percentile thresholds to test for each feature.
CANDIDATE_PERCENTILES: tuple[int, ...] = (20, 30, 40, 50, 60, 70, 80)


MIN_MATCHES = 12
MIN_EXPECTED_R = 0.10
MIN_R_LIFT_OVER_BASELINE = 0.10


@dataclass(frozen=True)
class PatternProposal:
    """A single-condition rule with historical performance attached."""

    feature: str
    operator: str               # ">=" or "<="
    threshold: float
    n_match: int
    n_match_wins: int
    expected_R: float           # mean R-multiple of matched historical rows
    win_rate_pct: float
    baseline_expected_R: float  # for context — overall mean R across all rows
    r_lift_vs_baseline: float
    score: float                # ranking score: expected_R * sqrt(n_match)
    notes: str = ""


def _percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[int(k)])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def _evaluate_rule(
    X: list[list[float]],
    y: list[float],
    feat_idx: int,
    threshold: float,
    operator: str,
) -> tuple[int, float, float] | None:
    """Return (n_match, mean_R, win_rate) for rows matching the rule,
    or None if no matches."""
    matched_y = [
        yv for row, yv in zip(X, y)
        if (row[feat_idx] >= threshold if operator == ">=" else row[feat_idx] <= threshold)
    ]
    if not matched_y:
        return None
    n = len(matched_y)
    mean_R = sum(matched_y) / n
    wins = sum(1 for v in matched_y if v > 0)
    return n, float(mean_R), float(wins / n * 100.0)


async def propose_patterns(*, top_k: int = 10) -> list[dict]:
    """Scan training data and surface the top single-condition rules.

    Returns a list of dicts (JSON-serializable) sorted by `score` desc.
    Empty list if there isn't enough data or no rule clears the bar.
    """
    X_list, y_list, feature_names, _ts = await _load_training_data()
    if len(X_list) < MIN_MATCHES * 2:
        return []

    baseline_R = sum(y_list) / len(y_list)

    proposals: list[PatternProposal] = []

    for feat_name in SCANNABLE_FEATURES:
        if feat_name not in feature_names:
            continue
        feat_idx = feature_names.index(feat_name)
        values = [row[feat_idx] for row in X_list]
        if max(values) == min(values):
            continue  # constant feature, no threshold split helps

        for pct in CANDIDATE_PERCENTILES:
            threshold = _percentile(values, pct)
            for op in (">=", "<="):
                ev = _evaluate_rule(X_list, y_list, feat_idx, threshold, op)
                if ev is None:
                    continue
                n_match, mean_R, win_rate = ev
                if n_match < MIN_MATCHES:
                    continue
                if mean_R < MIN_EXPECTED_R:
                    continue
                lift = mean_R - baseline_R
                if lift < MIN_R_LIFT_OVER_BASELINE:
                    continue
                score = float(mean_R * math.sqrt(n_match))
                wins = int(round(n_match * win_rate / 100.0))
                proposals.append(PatternProposal(
                    feature=feat_name,
                    operator=op,
                    threshold=round(threshold, 4),
                    n_match=n_match,
                    n_match_wins=wins,
                    expected_R=round(mean_R, 4),
                    win_rate_pct=round(win_rate, 2),
                    baseline_expected_R=round(baseline_R, 4),
                    r_lift_vs_baseline=round(lift, 4),
                    score=round(score, 4),
                    notes=f"pct={pct}",
                ))

    # Dedupe near-duplicates: if two proposals share (feature, operator)
    # and have thresholds within 5% of each other's spread, keep only the
    # higher-scored one. Keeps the list focused on materially distinct
    # rules.
    proposals.sort(key=lambda p: -p.score)
    seen: list[PatternProposal] = []
    for p in proposals:
        is_dup = False
        for s in seen:
            if s.feature != p.feature or s.operator != p.operator:
                continue
            # tolerance = 5% of |threshold| or 0.05 absolute, whichever is bigger
            tol = max(0.05, abs(p.threshold) * 0.05)
            if abs(s.threshold - p.threshold) <= tol:
                is_dup = True
                break
        if not is_dup:
            seen.append(p)
        if len(seen) >= top_k:
            break

    return [asdict(p) for p in seen]
