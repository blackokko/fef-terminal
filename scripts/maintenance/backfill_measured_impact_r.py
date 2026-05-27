"""Backfill `engine_change.measured_impact_r` retroactively.

Audit finding (Chat round-2): the engine_change table has 22 rows. All
applied_at populated. measured_impact_r populated for 0/22. The
post-hoc-measurement framework exists but the discipline doesn't.

What this script does
---------------------
For each engine_change row:
  1. Define a pre-change window of N sessions BEFORE applied_at.
  2. Define a post-change window of N sessions AFTER applied_at.
  3. Compute mean R-multiple per actionable signal in each window using
     the canonical daily_review.expectancy_r (already aggregated per
     session) — that's the same metric daily_review computes for
     comparison across days.
  4. measured_impact_r = post_mean - pre_mean.

Guardrails
----------
- The data window starts 2026-05-18 (5/16, 5/17 daily_review rows are
  empty placeholders). Changes applied before 2026-05-18 cannot have a
  pre window — mark NULL with reason="pre_data_window".
- Need at least 2 sessions in BOTH windows to compute a meaningful
  delta. With <2 in either side, mark NULL with reason="insufficient_data".
- Default window size is 5 sessions; can be overridden via CLI.

Rollback condition defaulting
-----------------------------
For variant-style changes ("v2_engine_formula_fixes", drift_bias v2,
fe_ratio v2, and similar), populate a sensible default rollback
condition if the column is empty:
    "measured_impact_r < -0.1R over 10 sessions"
Categories like 'data', 'safety' generally don't get a default added —
they're plumbing, not strategy parameters.

Usage
-----
    python scripts/maintenance/backfill_measured_impact_r.py [--window 5] [--dry-run]

Default DB: ~/.fef-terminal/signal_log.db (override with FEF_DB_PATH or
--db-path).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# Data window where daily_review has non-placeholder rows. 5/16 and 5/17
# exist but are zero across all metrics, so they don't count as "real
# pre data".
DATA_WINDOW_START = date(2026, 5, 18)

# Default rollback for variant changes when none is set. Conservative
# threshold + a 10-session evaluation period — matches the freeze-counter
# pattern the user is already using.
DEFAULT_VARIANT_ROLLBACK = "measured_impact_r < -0.1R over 10 sessions"

# Categories where a default rollback condition makes sense. 'data' and
# 'safety' are excluded — they're plumbing, not strategy tunables.
VARIANT_CATEGORY_WHITELIST = {"scoring", "gate", "exit", "threshold"}

# Variant-marker substrings in `name` that should always get a default
# rollback even if category is something else.
VARIANT_NAME_MARKERS = ("v2", "v3", "v4", "_split", "_drift_bias", "_fe_ratio")


@dataclass
class BackfillResult:
    """One row's backfill outcome for the report."""
    id: int
    name: str
    applied_at_date: str
    pre_window: str           # human description of window
    post_window: str
    measured_impact_r: float | None
    rollback_condition: str | None
    skip_reason: str          # "" if backfilled, else why


def _default_db_path() -> Path:
    import os
    p = os.environ.get("FEF_DB_PATH")
    if p:
        return Path(p)
    return Path.home() / ".fef-terminal" / "signal_log.db"


def _load_daily_review_index(conn: sqlite3.Connection) -> dict[date, float]:
    """Return {session_date: expectancy_r} for sessions in the data window.

    Sessions outside DATA_WINDOW_START (zero-actionable placeholders) are
    excluded so they don't dilute the mean.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT session_date, expectancy_r, actionable_signals "
        "FROM daily_review WHERE session_date >= ? "
        "ORDER BY session_date",
        (DATA_WINDOW_START.isoformat(),),
    )
    out: dict[date, float] = {}
    for sd_str, exp_r, n_action in cur.fetchall():
        # Skip true zero rows (no signals at all — placeholder), but keep
        # rows that legitimately had signals but 0 expectancy.
        if n_action and n_action > 0:
            out[date.fromisoformat(sd_str)] = float(exp_r or 0.0)
    return out


def _window_sessions(
    sorted_sessions: list[date],
    pivot: date,
    direction: str,
    window: int,
) -> list[date]:
    """Pick up to `window` session dates that are strictly before
    (direction='pre') or on/after (direction='post') `pivot`.

    'post' includes the pivot date itself — the change is applied at some
    point during that session, but the FULL session record reflects post-
    change behavior for any signal generated after applied_at. This is a
    rough approximation; for changes applied mid-session the pivot day
    measures a mix. With only 5 days of data we accept the noise rather
    than overengineering.
    """
    if direction == "pre":
        eligible = [d for d in sorted_sessions if d < pivot]
        return eligible[-window:] if len(eligible) > window else eligible
    elif direction == "post":
        eligible = [d for d in sorted_sessions if d >= pivot]
        return eligible[:window] if len(eligible) > window else eligible
    raise ValueError(direction)


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _format_window(dates: list[date]) -> str:
    if not dates:
        return "(empty)"
    if len(dates) == 1:
        return dates[0].isoformat()
    return f"{dates[0].isoformat()}..{dates[-1].isoformat()} (n={len(dates)})"


def _should_default_rollback(name: str, category: str, current: str) -> bool:
    """Decide whether to populate a default rollback condition."""
    if current and current.strip():
        return False  # already set
    cat = (category or "").lower()
    nm = (name or "").lower()
    if cat in VARIANT_CATEGORY_WHITELIST:
        return True
    if any(marker in nm for marker in VARIANT_NAME_MARKERS):
        return True
    return False


def backfill(
    db_path: Path,
    window: int,
    dry_run: bool,
) -> list[BackfillResult]:
    """Run the backfill and return per-row results."""
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    conn = sqlite3.connect(db_path)
    try:
        review_idx = _load_daily_review_index(conn)
        sorted_sessions = sorted(review_idx.keys())

        cur = conn.cursor()
        cur.execute(
            "SELECT id, applied_at, name, category, rollback_condition "
            "FROM engine_change ORDER BY applied_at ASC"
        )
        rows = cur.fetchall()

        results: list[BackfillResult] = []
        updates: list[tuple] = []  # (impact_r, rollback, id)

        for eid, applied_at_str, name, category, current_rollback in rows:
            # Parse applied_at -> date
            applied_dt = datetime.fromisoformat(applied_at_str.split(".")[0]
                                                if "." in applied_at_str
                                                else applied_at_str)
            applied_date = applied_dt.date()

            # Pre-data-window guard
            if applied_date < DATA_WINDOW_START:
                results.append(BackfillResult(
                    id=eid, name=name,
                    applied_at_date=applied_date.isoformat(),
                    pre_window="(pre data window)",
                    post_window="(pre data window)",
                    measured_impact_r=None,
                    rollback_condition=None,
                    skip_reason="pre_data_window",
                ))
                continue

            pre_dates = _window_sessions(sorted_sessions, applied_date,
                                          "pre", window)
            post_dates = _window_sessions(sorted_sessions, applied_date,
                                           "post", window)

            # Need at least 2 sessions in BOTH windows. With <2 the
            # delta is dominated by single-day noise — meaningless.
            if len(pre_dates) < 2 or len(post_dates) < 2:
                skip = (
                    "insufficient_pre" if len(pre_dates) < 2
                    else "insufficient_post"
                )
                results.append(BackfillResult(
                    id=eid, name=name,
                    applied_at_date=applied_date.isoformat(),
                    pre_window=_format_window(pre_dates),
                    post_window=_format_window(post_dates),
                    measured_impact_r=None,
                    rollback_condition=None,
                    skip_reason=skip,
                ))
                # Even when we can't compute impact, we may still want
                # to seed a rollback condition for variant changes.
                new_rb: str | None = None
                if _should_default_rollback(name, category, current_rollback):
                    new_rb = DEFAULT_VARIANT_ROLLBACK
                    updates.append((None, new_rb, eid))
                    results[-1].rollback_condition = new_rb
                continue

            pre_mean = _mean([review_idx[d] for d in pre_dates])
            post_mean = _mean([review_idx[d] for d in post_dates])
            impact = round(post_mean - pre_mean, 4)

            # Optionally seed rollback
            new_rb_opt = None
            if _should_default_rollback(name, category, current_rollback):
                new_rb_opt = DEFAULT_VARIANT_ROLLBACK

            updates.append((impact, new_rb_opt, eid))
            results.append(BackfillResult(
                id=eid, name=name,
                applied_at_date=applied_date.isoformat(),
                pre_window=_format_window(pre_dates),
                post_window=_format_window(post_dates),
                measured_impact_r=impact,
                rollback_condition=new_rb_opt,
                skip_reason="",
            ))

        # Apply updates
        if updates and not dry_run:
            for impact, new_rb, eid in updates:
                if new_rb is not None:
                    cur.execute(
                        "UPDATE engine_change SET measured_impact_r = ?, "
                        "rollback_condition = ? WHERE id = ?",
                        (impact, new_rb, eid),
                    )
                else:
                    cur.execute(
                        "UPDATE engine_change SET measured_impact_r = ? "
                        "WHERE id = ?",
                        (impact, eid),
                    )
            conn.commit()

        return results
    finally:
        conn.close()


def _print_report(results: list[BackfillResult]) -> None:
    """Pretty-print the per-row results table + summary counts."""
    # Aggregated counts
    backfilled = sum(1 for r in results if r.measured_impact_r is not None)
    skipped_pre = sum(1 for r in results if r.skip_reason == "pre_data_window")
    skipped_pre_window = sum(
        1 for r in results if r.skip_reason == "insufficient_pre"
    )
    skipped_post_window = sum(
        1 for r in results if r.skip_reason == "insufficient_post"
    )
    rollbacks_added = sum(1 for r in results if r.rollback_condition)

    # Header
    print("=" * 120)
    print(f"{'ID':>3} {'Applied':<12} {'Name':<42} {'Pre window':<32} "
          f"{'Post window':<32} {'Impact R':>10} {'Rollback':<8}")
    print("-" * 120)
    for r in results:
        impact_str = (f"{r.measured_impact_r:+.4f}"
                      if r.measured_impact_r is not None
                      else f"NULL({r.skip_reason})")
        rb_str = "set" if r.rollback_condition else ""
        print(f"{r.id:>3} {r.applied_at_date:<12} {r.name[:42]:<42} "
              f"{r.pre_window[:32]:<32} {r.post_window[:32]:<32} "
              f"{impact_str:>10} {rb_str:<8}")
    print("=" * 120)

    print()
    print("Summary counts:")
    print(f"  backfilled measured_impact_r:    {backfilled}")
    print(f"  skipped pre_data_window:          {skipped_pre}")
    print(f"  skipped insufficient_pre:         {skipped_pre_window}")
    print(f"  skipped insufficient_post:        {skipped_post_window}")
    print(f"  rollback_condition seeded:        {rollbacks_added}")
    print(f"  total rows scanned:               {len(results)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", type=int, default=5,
                    help="Sessions in each pre/post window (default 5)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute + report but don't UPDATE the DB")
    ap.add_argument("--db-path", type=Path, default=None,
                    help="Path to signal_log.db (default ~/.fef-terminal/...)")
    args = ap.parse_args()

    db_path = args.db_path or _default_db_path()
    print(f"DB: {db_path}")
    print(f"Window: {args.window} sessions  Dry-run: {args.dry_run}")
    print(f"Data window starts: {DATA_WINDOW_START.isoformat()}")
    print()

    results = backfill(db_path, args.window, args.dry_run)
    _print_report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
