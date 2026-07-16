"""Re-adoption gate for orphaned positions (position_reconcile).

Safety net for orphaning bugs (the 2026-07-01 7520C): a naked IBKR position
whose parent was wrongly marked CLOSED/STOPPED gets RE-ADOPTED into management —
but only when it's genuinely our own resolved bracket, never an untracked manual
trade, and only when we have a spot to anchor the emergency stop.
"""
from fef_sidecar.engine.position_reconcile import _resurrectable_for_orphan

KEY = ("SPX", 7520.0, "C", "20260701")
PARENT = object()  # stand-in for the resolved LiveOrder to reopen


def test_readopt_when_orphaned_with_resolved_parent_and_spot():
    # The 7520C case: no open parent, a CLOSED parent exists, spot known → reopen.
    assert _resurrectable_for_orphan(KEY, parent_keys={}, resurrectable={KEY: PARENT}, spot=7518.0) is PARENT


def test_no_readopt_when_position_already_managed():
    # An OPEN parent already manages it → never re-adopt (would double-manage).
    assert _resurrectable_for_orphan(KEY, parent_keys={KEY: object()}, resurrectable={KEY: PARENT}, spot=7518.0) is None


def test_no_readopt_for_untracked_manual_trade():
    # No resolved parent for this contract → it's not our bracket; warn, don't manage.
    assert _resurrectable_for_orphan(KEY, parent_keys={}, resurrectable={}, spot=7518.0) is None


def test_no_readopt_without_spot():
    # No spot → can't anchor an emergency stop → leave it a warned orphan.
    assert _resurrectable_for_orphan(KEY, parent_keys={}, resurrectable={KEY: PARENT}, spot=None) is None
    assert _resurrectable_for_orphan(KEY, parent_keys={}, resurrectable={KEY: PARENT}, spot=0) is None


# ---------------------------------------------------------------------------
# Entry-timeout CANCELLED resurrection (2026-07-16) — the 'cancelled but
# filled' race backstop must NEVER adopt the user's manual lots.
# ---------------------------------------------------------------------------

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fef_sidecar.engine.position_reconcile import _cancelled_entry_race_candidate


def _cancelled_parent(qty=3, notes="entry_timeout: unfilled after 45s — cancelled",
                      age_sec=60):
    return SimpleNamespace(
        status="CANCELLED", qty=qty, notes=notes,
        last_status_at=datetime.now(UTC) - timedelta(seconds=age_sec),
    )


def test_cancelled_race_candidate_recent_tagged():
    assert _cancelled_entry_race_candidate(_cancelled_parent()) is True


def test_cancelled_race_candidate_rejects_untagged():
    # A CANCELLED parent NOT cancelled by the entry-timeout path is never
    # resurrectable — only the race the watchdog created gets the backstop.
    assert _cancelled_entry_race_candidate(
        _cancelled_parent(notes="user cancelled in TWS")) is False


def test_cancelled_race_candidate_rejects_stale():
    # Beyond the window an orphan on that strike is presumed the user's own
    # manual trade (shared strikes are their documented habit).
    assert _cancelled_entry_race_candidate(
        _cancelled_parent(age_sec=3600)) is False


def test_cancelled_readopt_qty_bound_blocks_manual_lots():
    # Race fill can never exceed the bot's own cancelled order size: a
    # 100-lot orphan against a 3-lot CANCELLED parent is the USER'S manual
    # position — leave it a warned orphan, never adopt it into a sell ladder.
    p = _cancelled_parent(qty=3)
    resurrectable = {KEY: p}
    assert _resurrectable_for_orphan(
        KEY, parent_keys={}, resurrectable=resurrectable, spot=7518.0,
        orphan_qty=100) is None
    # The genuine race shapes (partial or full fill of OUR order) re-adopt.
    assert _resurrectable_for_orphan(
        KEY, parent_keys={}, resurrectable=resurrectable, spot=7518.0,
        orphan_qty=3) is p
    assert _resurrectable_for_orphan(
        KEY, parent_keys={}, resurrectable=resurrectable, spot=7518.0,
        orphan_qty=1) is p


def test_qty_bound_only_applies_to_cancelled():
    # CLOSED/STOPPED resurrection keeps its historical semantics (the bot
    # genuinely held the contract; remnant rebase handles size).
    p = SimpleNamespace(status="CLOSED", qty=3)
    assert _resurrectable_for_orphan(
        KEY, parent_keys={}, resurrectable={KEY: p}, spot=7518.0,
        orphan_qty=100) is p
