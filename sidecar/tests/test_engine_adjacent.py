"""Tests for engine-adjacent persistence/instrumentation:

  - audit_log.block_reason_code column population
  - live_order.parent_id linkage on watcher-fired SELLs

These tests cover the post-freeze annotation work (priorities 2 and 5
from the Chat round-2 audit). None of them exercise verdict generation —
they only verify that block reasons and parent_id are persisted alongside
the existing logic.
"""
from __future__ import annotations

import os

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from sqlmodel import select

from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import AuditLog, LiveOrder
from fef_sidecar.db.repository import SignalIterRepository
from fef_sidecar.engine.auto_trader import BlockReason


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


# ---------------------------------------------------------------------------
# Priority 2 — block_reason_code population
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_audit_log_populates_block_reason_code_column() -> None:
    """write_audit_log with block_reason_code kwarg sets the dedicated column."""
    await SignalIterRepository.write_audit_log(
        category="auto_trade_block",
        actor="system",
        summary="BLOCKED LONG_CALLS: verdict is WAIT",
        details={"reason": "verdict is WAIT"},
        block_reason_code=BlockReason.VERDICT_WAIT,
    )
    async with get_session() as session:
        rows = list((await session.exec(select(AuditLog))).all())
    assert len(rows) == 1
    row = rows[0]
    assert row.category == "auto_trade_block"
    assert row.block_reason_code == BlockReason.VERDICT_WAIT
    # Backwards-compat: details JSON also carries the code so old readers work.
    assert row.details.get("block_reason_code") == BlockReason.VERDICT_WAIT


@pytest.mark.asyncio
async def test_write_audit_log_default_block_reason_is_null() -> None:
    """Non-block audit rows (e.g. config) leave block_reason_code NULL."""
    await SignalIterRepository.write_audit_log(
        category="config", actor="user", summary="changed fe_min to 1.7",
    )
    async with get_session() as session:
        rows = list((await session.exec(select(AuditLog))).all())
    assert len(rows) == 1
    assert rows[0].block_reason_code is None


@pytest.mark.asyncio
async def test_write_audit_log_preserves_existing_details_code() -> None:
    """If callers already put block_reason_code in details (legacy), we
    keep the existing value rather than overwriting with the kwarg."""
    await SignalIterRepository.write_audit_log(
        category="auto_trade_block", actor="system",
        summary="legacy caller",
        details={"block_reason_code": BlockReason.COOL_DOWN},
        block_reason_code=BlockReason.COOL_DOWN,
    )
    async with get_session() as session:
        rows = list((await session.exec(select(AuditLog))).all())
    assert rows[0].block_reason_code == BlockReason.COOL_DOWN
    assert rows[0].details.get("block_reason_code") == BlockReason.COOL_DOWN


@pytest.mark.asyncio
async def test_block_reason_enum_has_expected_values() -> None:
    """BlockReason carries the codes the loop + watcher writers reference."""
    for code in (
        "DISABLED", "ENV_NOT_SET", "VERDICT_WAIT", "TRIGGER_NOT_ENABLED",
        "BELOW_FLOOR", "CALENDAR_PAUSE", "COOL_DOWN", "SYMBOL_CAP",
        "NO_OPTION_PRICING", "INVALID_PRICING", "NO_STRIKE", "PLACE_ERROR",
        "DUPLICATE_PARENT", "FIRED",
        "CONFIDENCE_FLOOR_SKIP", "CROSS_SYMBOL_CAP_SKIP",
        "OPTION_QUOTE_FAIL", "SAFE_SELL_NO_POSITION",
        "SAFE_SELL_SHORT_BLOCKED", "SAFE_SELL_CLAMPED", "SAFE_SELL_ERROR",
        "CREDIT_SPREAD_GATE_OFF", "ORDER_REFUSED",
    ):
        assert hasattr(BlockReason, code), f"BlockReason missing {code}"


# ---------------------------------------------------------------------------
# Priority 5 — parent_id linkage on watcher-fired SELLs
# ---------------------------------------------------------------------------


def _build_parent() -> LiveOrder:
    return LiveOrder(
        symbol="SPX", strike=7460.0, right="C", expiry="20260522",
        action="BUY", qty=15, order_type="MKT",
        bracket_role="parent", status="FILLED",
        filled_qty=15, avg_fill_price=4.20,
        exit_strategy="spx_points", dry_run=False, engine_version=2,
        spx_target_t1=7464.0, spx_target_t2=7467.0, spx_target_t3=7470.0,
        spx_target_stop=7456.0,
        spx_target_t1_qty=5, spx_target_t2_qty=5, spx_target_t3_qty=5,
    )


@pytest.mark.asyncio
async def test_write_watcher_sell_child_sets_parent_id() -> None:
    """A watcher-fired SELL produces a child LiveOrder with parent_id set."""
    parent = _build_parent()
    saved = await SignalIterRepository.write_live_order(parent)
    assert saved.id is not None

    child = await SignalIterRepository.write_watcher_sell_child(
        parent=saved, bracket_role="t1", qty=5,
        ib_order_id=12345, ib_perm_id=999,
        notes="watcher_close@SPX=7464.00",
    )
    assert child.id is not None
    assert child.parent_id == saved.id
    assert child.bracket_role == "t1"
    assert child.action == "SELL"
    assert child.order_type == "MKT"
    assert child.qty == 5
    assert child.ib_order_id == 12345
    assert child.symbol == saved.symbol
    assert child.strike == saved.strike
    assert child.right == saved.right
    assert child.expiry == saved.expiry
    assert child.dry_run == saved.dry_run
    assert child.engine_version == saved.engine_version
    assert child.notes == "watcher_close@SPX=7464.00"


@pytest.mark.asyncio
async def test_watcher_sell_child_preserves_ticket_link() -> None:
    """ticket_id (if any) propagates from parent to child for join paths."""
    parent = _build_parent()
    parent.ticket_id = 77
    saved = await SignalIterRepository.write_live_order(parent)
    child = await SignalIterRepository.write_watcher_sell_child(
        parent=saved, bracket_role="stop", qty=15,
        ib_order_id=22222,
    )
    assert child.ticket_id == 77


@pytest.mark.asyncio
async def test_watcher_sell_child_multiple_tiers_share_parent() -> None:
    """T1+T2+T3+STOP children from one bracket all point to the same parent."""
    parent = _build_parent()
    saved = await SignalIterRepository.write_live_order(parent)
    for role, qty, oid in (
        ("t1", 5, 100), ("t2", 5, 101), ("t3", 5, 102), ("stop", 15, 103),
    ):
        await SignalIterRepository.write_watcher_sell_child(
            parent=saved, bracket_role=role, qty=qty, ib_order_id=oid,
        )
    async with get_session() as session:
        children = list((await session.exec(
            select(LiveOrder).where(LiveOrder.parent_id == saved.id)
        )).all())
    assert len(children) == 4
    assert {c.bracket_role for c in children} == {"t1", "t2", "t3", "stop"}
    assert all(c.parent_id == saved.id for c in children)
    assert all(c.action == "SELL" for c in children)


@pytest.mark.asyncio
async def test_parent_id_column_exists_on_live_order() -> None:
    """Regression: ensure the migration list keeps parent_id alive on
    legacy DBs even though the model has carried the field since inception."""
    parent = _build_parent()
    saved = await SignalIterRepository.write_live_order(parent)
    async with get_session() as session:
        row = (await session.exec(
            select(LiveOrder).where(LiveOrder.id == saved.id)
        )).one()
    assert hasattr(row, "parent_id")
    # The parent row's own parent_id is naturally NULL (it has no parent).
    assert row.parent_id is None
