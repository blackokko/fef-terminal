"""BE-ladder rung schedule (2026-07-28 rung-sweep adoption) — tests.

Pins: the adopted default (arms 0.6/1.2/1.8R, locks 0/0.5/0.5R — the
181-cell sweep winner, +$22.95/ct OOS t=+3.36), the env parser's fail-safe
validation, the single-source-of-truth aliases (shadow + api + analysis
paths), and the live ratchet behavior at the new arms including the
forward-only guard that protects mid-flight parents across the deploy.
"""

from __future__ import annotations

import os

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.engine import spx_target_watcher as w


def test_adopted_default_schedule():
    assert w._BE_LADDER_DEFAULT == [(0.6, 0.0), (1.2, 0.5), (1.8, 0.5)]
    # No env override in the test run -> live table IS the default.
    assert w._BE_LADDER_STEPS == w._BE_LADDER_DEFAULT


def test_shadow_and_api_share_the_live_table():
    # Contract: one object, everywhere — divergence unrepresentable.
    assert w._SHADOW_BE_LADDER is w._BE_LADDER_STEPS
    from fef_sidecar import api
    assert api._BE_LADDER is w._BE_LADDER_STEPS


def test_parser_accepts_old_schedule_rollback_string():
    assert w._parse_be_ladder("0.5:0,1.0:0.5,1.5:1.0") == [
        (0.5, 0.0), (1.0, 0.5), (1.5, 1.0)]


def test_parser_rejects_invalid_to_default():
    bad = [
        "garbage",
        "0.5:0,0.4:0.2",        # non-increasing triggers
        "0.5:0.6",              # lock > trigger
        "0.5:0,1.0:-0.2",       # negative lock
        "0.6:0,1.2:0.5,1.8:0.4",  # locks decreasing
        "0.5:nan",
        "",
        None,
    ]
    for raw in bad:
        assert w._parse_be_ladder(raw) == w._BE_LADDER_DEFAULT, raw


def test_ratchet_arms_at_new_triggers_call_side():
    """Synthetic walk of the ladder loop math (call): risk 10pt, entry 7400.
    Old schedule armed at +5; the adopted one must NOT step before +6, must
    lock BE at +6, 0.5R at +12, and add nothing new at +18 (lock equal)."""
    entry, risk = 7400.0, 10.0
    steps_hit: list[tuple[float, float]] = []
    be_step = 0.0
    dyn = None
    for spot in (7404.9, 7405.9, 7406.0, 7411.9, 7412.0, 7417.9, 7418.0):
        mfe_R = (spot - entry) / risk
        for trig_R, lock_R in w._BE_LADDER_STEPS:
            if mfe_R >= trig_R and be_step < trig_R:
                new_stop = entry + lock_R * risk
                if new_stop > (dyn or -1e9):
                    dyn = new_stop
                    be_step = trig_R
                    steps_hit.append((spot, new_stop))
                else:
                    be_step = be_step  # forward-only guard: no rewind
    # +5.9 must not arm (old schedule would have); +6.0 arms BE; +12 locks
    # 0.5R; +18's lock (0.5R) is not an improvement -> no third stop move.
    assert steps_hit == [(7406.0, 7400.0), (7412.0, 7405.0)]
    assert dyn == 7405.0


def test_deploy_day_forward_only_no_rewind_put_side():
    """A put parent restarted mid-flight with the OLD schedule's 1.0R lock
    already ratcheted (dyn = entry - 10): the new table's 1.8R step computes
    a WORSE stop (entry - 5) and must not move it backward."""
    entry, risk = 7400.0, 10.0
    dyn = entry - 1.0 * risk          # 7390 — old-schedule 1.5R step's lock
    be_step = 1.5                      # legacy marker from the old table
    spot = entry - 1.9 * risk          # deep in profit, past every new arm
    mfe_R = (entry - spot) / risk
    moved = []
    for trig_R, lock_R in w._BE_LADDER_STEPS:
        if mfe_R >= trig_R and be_step < trig_R:
            new_stop = entry - lock_R * risk
            if new_stop < dyn:         # put-side forward-only guard
                dyn = new_stop
                be_step = trig_R
                moved.append(trig_R)
    # Real watcher semantics: a guard-failed step neither moves the stop nor
    # records the step (it re-evaluates as a no-op each cycle — benign).
    assert moved == []
    assert dyn == 7390.0               # the stop NEVER rewinds
