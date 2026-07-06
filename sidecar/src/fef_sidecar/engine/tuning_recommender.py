"""TuningRecommendation rule engine.

Pure functions over (DailyReview list, EngineChange list, now) → list of
TuningRecommendation rows. Same logic powers the auto-nightly run AND the
manual /tuning/recompute date-range button — both call evaluate().

Design philosophy: each rule is a small function returning 0..N
recommendations. Add new rules by appending to RULES. Pure, testable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from fef_sidecar.db.models import DailyReview, EngineChange, TuningRecommendation


def _state_of_model(reviews: list[DailyReview]) -> TuningRecommendation:
    """Always emitted. Summarizes overall health for the TopBar pill."""
    if not reviews:
        return TuningRecommendation(
            kind="STATE_OF_MODEL", priority="HIGH",
            title="State of model: NO DATA",
            body="Insufficient data — need at least 1 DailyReview row.",
            backing_data={"state": "NO_DATA"},
            based_on_sessions=0,
        )
    last5 = reviews[-5:]
    exp_avg = sum(r.expectancy_r for r in last5) / len(last5)
    positive_count = sum(1 for r in last5 if r.expectancy_r > 0)
    if exp_avg < -0.4 and len(last5) >= 3:
        state = "RED"
    elif exp_avg < 0:
        state = "AMBER"
    elif positive_count >= len(last5) // 2 + 1:
        state = "GREEN"
    else:
        state = "AMBER"
    return TuningRecommendation(
        kind="STATE_OF_MODEL", priority="HIGH",
        title=f"State of model: {state}",
        body=(f"Last {len(last5)} sessions avg expectancy {exp_avg:+.3f}R. "
              f"{positive_count}/{len(last5)} positive."),
        backing_data={"state": state, "expectancy_avg_r": round(exp_avg, 3),
                      "positive_count": positive_count,
                      "sessions_considered": len(last5)},
        based_on_sessions=len(last5),
        based_on_window_start=last5[0].session_date if last5 else "",
        based_on_window_end=last5[-1].session_date if last5 else "",
    )


def _check_freeze(reviews: list[DailyReview]) -> list[TuningRecommendation]:
    """If expectancy < -0.5R for 3 consecutive sessions, recommend freeze."""
    if len(reviews) < 3:
        return []
    last3 = reviews[-3:]
    if all(r.expectancy_r < -0.5 for r in last3):
        return [TuningRecommendation(
            kind="FREEZE", priority="HIGH",
            title="FREEZE recommended — 3 sessions of expectancy < −0.5R",
            body=("Auto-trade has produced sustained negative expectancy. "
                  "Disable auto-trade, restrict to confluence-only review, "
                  "investigate before resuming."),
            backing_data={"sessions": [r.session_date for r in last3],
                          "expectancy_r": [r.expectancy_r for r in last3]},
            based_on_sessions=3,
            based_on_window_start=last3[0].session_date,
            based_on_window_end=last3[-1].session_date,
        )]
    return []


def _check_eligible_for_change(reviews: list[DailyReview],
                               changes: list[EngineChange],
                               now: datetime) -> list[TuningRecommendation]:
    """Eligible to ship a new gate if: confluence subset has held >= 35% win
    for 5+ sessions AND >= 5 sessions since last engine change."""
    if len(reviews) < 5:
        return []
    last5 = reviews[-5:]
    conf_win_rates = []
    for r in last5:
        conf = (r.by_trigger or {}).get("confluence") or {}
        if conf.get("n", 0) >= 3:
            conf_win_rates.append(conf.get("win_pct", 0))
    if len(conf_win_rates) < 3:
        return []  # need confluence data on most sessions
    conf_stable = all(w >= 35 for w in conf_win_rates)

    # Days since last change
    if changes:
        last_change_at = max(c.applied_at for c in changes)
        # naive comparison — both are datetime; if tz mismatch use a fallback
        try:
            days_since = (now - last_change_at).days
        except TypeError:
            days_since = 999
    else:
        days_since = 999

    if conf_stable and days_since >= 5:
        return [TuningRecommendation(
            kind="ELIGIBLE_FOR_CHANGE", priority="MEDIUM",
            title="Eligible to ship a new gate or threshold",
            body=(f"Confluence subset stable at {min(conf_win_rates):.0f}–"
                  f"{max(conf_win_rates):.0f}% win across last "
                  f"{len(conf_win_rates)} sessions. {days_since} days since "
                  f"last engine change. Safe to A/B-test a new filter."),
            backing_data={"confluence_win_pcts": conf_win_rates,
                          "days_since_last_change": days_since},
            based_on_sessions=len(last5),
            based_on_window_start=last5[0].session_date,
            based_on_window_end=last5[-1].session_date,
        )]
    return []


# Recent-window split: only the last RECENT_WINDOW sessions feed a gate
# recommendation, so a burst of old-May sessions doesn't out-vote the 2
# current ones by sheer count.
RECENT_WINDOW = 5
# A bucket must persist across at least this many sessions of the recent
# window to be considered stable enough to gate on (not a one-day spike).
MIN_SESSIONS_PRESENT = 3


def _pooled_win_pct(summaries: list[dict]) -> tuple[float, float, int]:
    """Sample-weighted pooled win% for one bucket across sessions.

    Returns (pooled_win_pct, total_n, sessions_present). Pools as
    Σwins / Σn — a 30-sample session counts 10× a 3-sample one — instead of
    the old UNWEIGHTED mean of per-session win% that let a 3-sample bucket
    swing the estimate as hard as a 30-sample one.

    Prefers REALIZED-fill outcomes (``realized_win_pct`` / ``realized_n``)
    over the hypothetical signal win% whenever a session recorded them.
    """
    total_wins = 0.0
    total_n = 0.0
    present = 0
    for summary in summaries:
        realized_n = summary.get("realized_n")
        if realized_n:
            n = realized_n
            win_pct = summary.get("realized_win_pct", 0)
        else:
            n = summary.get("n", 0)
            win_pct = summary.get("win_pct", 0)
        if not n or n < 3:
            continue  # ignore tiny buckets
        total_wins += (win_pct / 100.0) * n
        total_n += n
        present += 1
    pooled = (total_wins / total_n * 100.0) if total_n > 0 else 0.0
    return pooled, total_n, present


def _check_feature_separation(reviews: list[DailyReview]) -> list[TuningRecommendation]:
    """For each by_X dimension, check if any bucket has consistently higher
    win rate than the others across the recent window. If so, recommend
    gating. Uses sample-weighted pooling (Σwins/Σn) over the last
    RECENT_WINDOW sessions, preferring realized-fill outcomes when present."""
    if len(reviews) < 5:
        return []
    recent = reviews[-min(len(reviews), RECENT_WINDOW):]
    recs: list[TuningRecommendation] = []

    DIMENSIONS = [
        ("by_regime", "regime"),
        ("by_wall_distance", "wall_distance"),
        ("by_fe_ratio", "fe_ratio"),
        ("by_direction", "direction"),
        ("by_hour_ct", "hour_ct"),
    ]
    for col, label in DIMENSIONS:
        # Collect each bucket's per-session summary dicts across the recent
        # window, then pool with sample weighting.
        bucket_summaries: dict[str, list[dict]] = {}
        for r in recent:
            data = getattr(r, col, None) or {}
            for bucket, summary in data.items():
                if not isinstance(summary, dict):
                    continue
                bucket_summaries.setdefault(bucket, []).append(summary)
        if not bucket_summaries:
            continue
        avg_by_bucket: dict[str, float] = {}
        n_by_bucket: dict[str, float] = {}
        for bucket, summaries in bucket_summaries.items():
            pooled, total_n, present = _pooled_win_pct(summaries)
            if present >= MIN_SESSIONS_PRESENT:
                avg_by_bucket[bucket] = round(pooled, 1)
                n_by_bucket[bucket] = total_n
        if len(avg_by_bucket) < 2:
            continue
        best = max(avg_by_bucket.items(), key=lambda kv: kv[1])
        worst = min(avg_by_bucket.items(), key=lambda kv: kv[1])
        gap = best[1] - worst[1]
        if gap >= 25:  # 25+ percentage point gap between best and worst bucket
            recs.append(TuningRecommendation(
                kind="PROPOSE_GATE", priority="HIGH" if gap >= 35 else "MEDIUM",
                title=f"Gate candidate: {label}={best[0]} (best bucket)",
                body=(f"Across last {len(recent)} sessions (sample-weighted), "
                      f"'{label}={best[0]}' has {best[1]:.0f}% win rate vs "
                      f"'{worst[0]}' at {worst[1]:.0f}%. Gap: {gap:.0f}pp. "
                      f"Consider restricting auto-trade to {label}=='{best[0]}'."),
                backing_data={"dimension": label,
                              "best_bucket": best[0], "best_avg_winpct": best[1],
                              "worst_bucket": worst[0], "worst_avg_winpct": worst[1],
                              "gap_pp": gap,
                              "per_bucket_avg": avg_by_bucket,
                              "per_bucket_pooled_n": n_by_bucket,
                              "pooling": "sample_weighted",
                              "window_sessions": len(recent)},
                based_on_sessions=len(recent),
                based_on_window_start=recent[0].session_date,
                based_on_window_end=recent[-1].session_date,
            ))
    return recs


def _check_post_change_impact(reviews: list[DailyReview],
                              changes: list[EngineChange]) -> list[TuningRecommendation]:
    """For each engine change in the last 14 days, compute pre vs post
    expectancy. Flag rollback candidates."""
    recs = []
    for c in changes:
        try:
            change_date = c.applied_at.strftime("%Y-%m-%d")
        except Exception:
            continue
        pre = [r for r in reviews if r.session_date < change_date]
        post = [r for r in reviews if r.session_date > change_date]
        if len(pre) < 3 or len(post) < 3:
            continue
        pre_avg = sum(r.expectancy_r for r in pre[-5:]) / len(pre[-5:])
        post_avg = sum(r.expectancy_r for r in post[:5]) / len(post[:5])
        delta = post_avg - pre_avg
        if delta < -0.2:
            recs.append(TuningRecommendation(
                kind="ROLLBACK_CANDIDATE", priority="HIGH",
                title=f"Rollback candidate: {c.name}",
                body=(f"Pre-change avg expectancy {pre_avg:+.3f}R, post {post_avg:+.3f}R "
                      f"(Δ {delta:+.3f}R). Consider reverting if persists."),
                backing_data={"change_name": c.name, "applied_at": str(c.applied_at),
                              "pre_avg_r": pre_avg, "post_avg_r": post_avg,
                              "delta_r": delta},
                based_on_sessions=len(pre) + len(post),
            ))
    return recs


RULES: list[Callable] = [
    lambda reviews, changes, now: [_state_of_model(reviews)],
    lambda reviews, changes, now: _check_freeze(reviews),
    lambda reviews, changes, now: _check_eligible_for_change(reviews, changes, now),
    lambda reviews, changes, now: _check_feature_separation(reviews),
    lambda reviews, changes, now: _check_post_change_impact(reviews, changes),
]


def evaluate(reviews: list[DailyReview],
             changes: list[EngineChange],
             now: datetime | None = None) -> list[TuningRecommendation]:
    """Run all rules against the data window. Returns list of recommendations.
    Caller is responsible for persisting them.

    reviews must be sorted ascending by session_date.
    """
    if now is None:
        from datetime import timezone
        now = datetime.now(timezone.utc)
    out: list[TuningRecommendation] = []
    for rule in RULES:
        try:
            out.extend(rule(reviews, changes, now))
        except Exception:
            import logging
            logging.getLogger(__name__).exception("rule failed")
    return out
