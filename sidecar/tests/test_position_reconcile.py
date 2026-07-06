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
