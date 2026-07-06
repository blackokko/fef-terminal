"""Phase 0 reservation (locked 2026-06-29). The holdout is the most recent ~20% of
0DTE sessions, touched EXACTLY ONCE at the end (Phase 5). Development code must use
dev_range()/assert_dev() and never read holdout dates.

  DEV     : 2022-01-03 .. 2025-08-12  (874 sessions; 2022 bear, 2023-24 bull, 2025-H1 mixed)
  HOLDOUT : 2025-08-13 .. 2026-06-26  (219 sessions; recent mixed)

NOTE: the holdout lacks a 2022-style high-vol bear regime. A dev-set edge that depends on
bear conditions cannot be fully confirmed on the holdout.
"""
from __future__ import annotations

DEV_START = "2022-01-01"
DEV_END = "2025-08-12"
HOLDOUT_START = "2025-08-13"
HOLDOUT_END = "2026-06-26"


def dev_range():
    return (DEV_START, DEV_END)


def holdout_range():
    return (HOLDOUT_START, HOLDOUT_END)


def assert_dev(end_date):
    """Guard: raise if a development query would touch the reserved holdout."""
    if str(end_date) >= HOLDOUT_START:
        raise AssertionError(
            f"end_date {end_date} touches the reserved holdout (>= {HOLDOUT_START}). "
            f"Holdout is Phase 5 only — touched once, at the end.")
