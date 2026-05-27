"""Recompute legacy ``daily_review`` rows in place.

Background
----------
Per ``project_daily_review_accounting_bug.md`` (2026-05-21): the
``daily_review.wins`` / ``actual_total_R`` columns were unreliable on
partial-scaleout sessions before the 5/21 fix shipped. Old rows for
5/19, 5/20, 5/21 carry pre-fix values that don't reflect the corrected
realized-P&L accounting.

This script re-invokes the canonical ``compute_daily_review`` function
in ``fef_sidecar.engine.daily_review`` for each requested session and
upserts the resulting row. The engine function:

* Re-walks the SPX 1-min bars to score per-signal R-multiples (used
  for the theoretical ``wins`` / ``expectancy_r`` columns).
* Uses per-contract pair-off across ``ibkr_execution`` for the
  ``actual_total_R`` ground-truth dollar figure.
* Cascades to ``daily_session`` (re-writes gross_pnl / net_pnl /
  win_count / loss_count via ``_estimate_parent_r``).

Because the daily_session cascade uses the partial-flag estimator (not
the FIFO pair-off), running this script will SET ``daily_session``
back to estimator-derived counts. To restore the per-parent FIFO W/L,
run ``recompute_daily_session_wl.py`` *after* this script. The flow is:

.. code-block:: bash

    python scripts/maintenance/recompute_daily_review.py \\
        --dates 2026-05-19 2026-05-20 2026-05-21

    python scripts/maintenance/recompute_daily_session_wl.py \\
        --dates 2026-05-19 2026-05-20 2026-05-21

Usage
-----

.. code-block:: bash

    python scripts/maintenance/recompute_daily_review.py --dates 2026-05-19
    python scripts/maintenance/recompute_daily_review.py --all-pre-fix
    python scripts/maintenance/recompute_daily_review.py --dates 2026-05-19 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

PRE_FIX_CUTOFF = "2026-05-22"  # 5/22 and later were computed under the fixed logic


def _snapshot(db_path: Path, session_date: str) -> dict | None:
    """Pull current daily_review row values for a before/after diff."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT total_signals, actionable_signals, wins, stops, timeouts,
                      win_rate_pct, expectancy_r,
                      actual_trades_taken, actual_wins, actual_losses, actual_total_R,
                      t1_hits, t2_hits, t3_hits,
                      dedup_actionable, dedup_wins, dedup_stops, dedup_win_rate_pct,
                      dedup_expectancy_r,
                      sharpe_ratio, sortino_ratio, max_drawdown_R, profit_factor
                 FROM daily_review
                WHERE session_date = ?""",
            (session_date,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "total_signals": row[0],
            "actionable_signals": row[1],
            "wins": row[2],
            "stops": row[3],
            "timeouts": row[4],
            "win_rate_pct": row[5],
            "expectancy_r": row[6],
            "actual_trades_taken": row[7],
            "actual_wins": row[8],
            "actual_losses": row[9],
            "actual_total_R": row[10],
            "t1_hits": row[11],
            "t2_hits": row[12],
            "t3_hits": row[13],
            "dedup_actionable": row[14],
            "dedup_wins": row[15],
            "dedup_stops": row[16],
            "dedup_win_rate_pct": row[17],
            "dedup_expectancy_r": row[18],
            "sharpe_ratio": row[19],
            "sortino_ratio": row[20],
            "max_drawdown_R": row[21],
            "profit_factor": row[22],
        }
    finally:
        conn.close()


def _row_to_dict(review) -> dict:
    """Extract the same fields from a DailyReview ORM row for diffing."""
    return {
        "total_signals": review.total_signals,
        "actionable_signals": review.actionable_signals,
        "wins": review.wins,
        "stops": review.stops,
        "timeouts": review.timeouts,
        "win_rate_pct": review.win_rate_pct,
        "expectancy_r": review.expectancy_r,
        "actual_trades_taken": review.actual_trades_taken,
        "actual_wins": review.actual_wins,
        "actual_losses": review.actual_losses,
        "actual_total_R": review.actual_total_R,
        "t1_hits": review.t1_hits,
        "t2_hits": review.t2_hits,
        "t3_hits": review.t3_hits,
        "dedup_actionable": review.dedup_actionable,
        "dedup_wins": review.dedup_wins,
        "dedup_stops": review.dedup_stops,
        "dedup_win_rate_pct": review.dedup_win_rate_pct,
        "dedup_expectancy_r": review.dedup_expectancy_r,
        "sharpe_ratio": review.sharpe_ratio,
        "sortino_ratio": review.sortino_ratio,
        "max_drawdown_R": review.max_drawdown_R,
        "profit_factor": review.profit_factor,
    }


def _print_diff(session_date: str, before: dict | None, after: dict) -> None:
    print(f"\n--- {session_date} ---")
    if before is None:
        print("  (no existing daily_review row — will INSERT)")
        for k, v in after.items():
            print(f"    + {k:>22}: {v!r}")
        return
    for k in after.keys():
        old = before.get(k)
        new = after.get(k)
        mark = "  " if old == new else ">>"
        print(f"  {mark} {k:>22}: {old!r:>14}  ->  {new!r}")


async def _list_pre_fix_sessions(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT session_date FROM daily_review
                 WHERE session_date < ?
                 ORDER BY session_date""",
            (PRE_FIX_CUTOFF,),
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


async def _run(dates: list[str], db_path: Path, dry_run: bool) -> int:
    # Inject the DB path before importing engine modules so they pick it up.
    import os
    os.environ.setdefault(
        "FEF_DB_URL",
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
    )

    # Import after env var is set — module-level engine lazy-binds on first use.
    from fef_sidecar.engine.daily_review import compute_daily_review

    print(f"DB: {db_path}")
    for sess in dates:
        before = _snapshot(db_path, sess)
        if dry_run:
            # Dry-run path: just print the existing values (no recompute)
            print(f"\n--- {sess} (dry-run) ---")
            if before is None:
                print("  (no existing daily_review row)")
            else:
                for k, v in before.items():
                    print(f"    {k:>22}: {v!r}")
            continue
        review = await compute_daily_review(sess)
        after = _row_to_dict(review)
        _print_diff(sess, before, after)
    if dry_run:
        print("\nDRY RUN - no writes.")
    else:
        print("\nDaily-review recomputes complete.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Recompute legacy daily_review rows via "
                    "fef_sidecar.engine.daily_review.compute_daily_review.",
    )
    ap.add_argument(
        "--db",
        default=str(Path.home() / ".fef-terminal" / "signal_log.db"),
        help="Path to signal_log.db",
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--dates", nargs="+", metavar="YYYY-MM-DD",
                     help="Explicit list of session dates to recompute.")
    grp.add_argument("--all-pre-fix", action="store_true",
                     help="Recompute every session strictly before the "
                          "2026-05-21 accounting fix cutoff.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print existing values without recomputing.")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2

    if args.all_pre_fix:
        dates = asyncio.run(_list_pre_fix_sessions(db_path))
        print(f"Pre-fix sessions: {dates}")
    else:
        dates = list(args.dates)

    return asyncio.run(_run(dates, db_path, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
