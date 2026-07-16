"""Entry-timeout watchdog (2026-07-16 incident fix, part 2).

Contract under test (see engine/entry_timeout.py outcome matrix):
  - Unfilled entry at timeout -> cancel; on confirmed zero-fill the parent
    (+ child rows) goes CANCELLED, the per-symbol session-cap slot is
    RELEASED (cool-down deliberately kept), the signal row is
    counterfactual-tagged and an 'entry_timeout' audit row is written.
  - Partial fill -> remainder cancelled, filled portion kept as PARTIAL
    with filled_qty/avg_fill_price synced (the watcher scans PARTIAL and
    its lifetime sell cap keys off filled_qty); budget NOT released.
  - Fill completing DURING the async cancel ('too late to cancel') -> kept,
    nothing cancelled in the DB, budget kept.
  - Cancel unconfirmed inside the wait -> FAIL SAFE: row left SUBMITTED for
    fill-sync / ENTRY_FILL_GRACE / reconcile; budget kept.
  - DB row diverged (fill-sync raced us to FILLED) -> never clobbered.
  - Legacy option_price bracket with a partial -> left working (attached-
    children cancel semantics), audited.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from sqlmodel import select

from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import AuditLog, LiveOrder, SignalIter
from fef_sidecar.engine.entry_timeout import entry_timeout_monitor, entry_timeout_sec


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


# ---------------------------------------------------------------------------
# Fakes — ib_async Trade / IB shapes (duck-typed; monitor only reads
# orderStatus.{status,filled,avgFillPrice} and calls ib.cancelOrder).
# ---------------------------------------------------------------------------

def _fake_trade(order_id: int = 101, filled: int = 0,
                status: str = "Submitted", avg: float = 0.0):
    return SimpleNamespace(
        order=SimpleNamespace(orderId=order_id, action="BUY"),
        orderStatus=SimpleNamespace(status=status, filled=filled, avgFillPrice=avg),
    )


class _FakeIB:
    def __init__(self, trade, on_cancel=None):
        self._trade = trade
        self.cancel_calls: list = []
        self._on_cancel = on_cancel

    def trades(self):
        return [self._trade]

    def cancelOrder(self, order):
        self.cancel_calls.append(order)
        if self._on_cancel is not None:
            self._on_cancel(self._trade)


def _ibkr(fake_ib):
    return SimpleNamespace(_ib=fake_ib)


def _budget_state(count: int = 1):
    return SimpleNamespace(
        auto_trade_count_by_symbol={"SPX": count},
        auto_trade_count_session=count,
        auto_trade_last_at_ms_by_symbol={"SPX": 111},
    )


async def _mk_parent(qty: int = 3, status: str = "SUBMITTED",
                     filled_qty: int = 0, exit_strategy: str = "spx_points",
                     ib_order_id: int = 101) -> int:
    async with get_session() as session:
        row = LiveOrder(
            symbol="SPX", strike=7520.0, right="C", expiry="20260716",
            action="BUY", qty=qty, order_type="LMT", limit_price=5.70,
            bracket_role="parent", status=status, filled_qty=filled_qty,
            dry_run=False, exit_strategy=exit_strategy, ib_order_id=ib_order_id,
        )
        session.add(row)
        await session.flush()
        return row.id


async def _row(order_id: int) -> LiveOrder:
    async with get_session() as session:
        return (await session.exec(
            select(LiveOrder).where(LiveOrder.id == order_id)
        )).first()


async def _audit_rows(outcome: str) -> list[AuditLog]:
    async with get_session() as session:
        rows = list((await session.exec(
            select(AuditLog).where(AuditLog.category == "entry_timeout")
        )).all())
    return [r for r in rows if r.details.get("outcome") == outcome]


def _run_args(state, ib, parent_id, **over):
    d = dict(
        state=state, ibkr=_ibkr(ib), parent_id=parent_id, ib_order_id=101,
        symbol="SPX", qty=3, exit_strategy="spx_points", source="auto_main",
        limit_price=5.70, timeout_sec=0.01, cancel_wait_sec=1.0,
    )
    d.update(over)
    return d


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

async def test_zero_fill_timeout_cancels_and_stands_down() -> None:
    parent_id = await _mk_parent()
    child_id = await _mk_parent(qty=1, exit_strategy="spx_points")  # stand-in child row

    def on_cancel(trade):
        trade.orderStatus.status = "Cancelled"

    trade = _fake_trade(filled=0)
    ib = _FakeIB(trade, on_cancel=on_cancel)
    state = _budget_state(count=1)

    # Signal row for the counterfactual tag
    async with get_session() as session:
        sig = SignalIter(session_date="2026-07-16", iter_num=1, spx_price=7515.0)
        session.add(sig)
        await session.flush()
        sig_id = sig.id

    res = await entry_timeout_monitor(**_run_args(
        state, ib, parent_id, parent_trade=trade,
        signal_iter_id=sig_id, child_ids=(child_id,),
    ))
    assert res["outcome"] == "cancelled_stand_down"
    assert len(ib.cancel_calls) == 1

    row = await _row(parent_id)
    assert row.status == "CANCELLED"
    assert "entry_timeout" in row.notes
    child = await _row(child_id)
    assert child.status == "CANCELLED"

    # Budget released; cool-down NOT rolled back.
    assert state.auto_trade_count_by_symbol["SPX"] == 0
    assert state.auto_trade_count_session == 0
    assert state.auto_trade_last_at_ms_by_symbol["SPX"] == 111

    # Counterfactual + audit trail
    assert len(await _audit_rows("cancelled_stand_down")) == 1
    async with get_session() as session:
        sig_row = (await session.exec(
            select(SignalIter).where(SignalIter.id == sig_id)
        )).first()
    assert (sig_row.extras or {}).get("skipped_by") == "entry_timeout"
    assert "ENTRY TIMEOUT" in (sig_row.reasoning or "")


async def test_partial_fill_keeps_filled_portion() -> None:
    parent_id = await _mk_parent(qty=3)

    def on_cancel(trade):
        trade.orderStatus.status = "Cancelled"  # remainder cancelled, 1 lot filled

    trade = _fake_trade(filled=1, avg=5.65)
    ib = _FakeIB(trade, on_cancel=on_cancel)
    state = _budget_state(count=1)

    res = await entry_timeout_monitor(**_run_args(state, ib, parent_id, parent_trade=trade))
    assert res["outcome"] == "partial_kept"
    assert len(ib.cancel_calls) == 1

    row = await _row(parent_id)
    # Exactly what _sync_parent_fill_from_trades would write — the watcher
    # scans ("SUBMITTED","FILLED","PARTIAL") and the lifetime sell cap is
    # `filled_qty or qty` (spx_target_watcher), so 1ct is managed, never 3.
    assert row.status == "PARTIAL"
    assert row.filled_qty == 1
    assert row.avg_fill_price == pytest.approx(5.65)

    # Budget kept — a position exists.
    assert state.auto_trade_count_session == 1
    assert len(await _audit_rows("partial_kept")) == 1


async def test_full_fill_before_timeout_no_cancel() -> None:
    parent_id = await _mk_parent()
    trade = _fake_trade(filled=3, status="Filled", avg=5.62)
    ib = _FakeIB(trade)
    state = _budget_state()

    res = await entry_timeout_monitor(**_run_args(state, ib, parent_id, parent_trade=trade))
    assert res["outcome"] == "filled_in_time"
    assert ib.cancel_calls == []
    assert (await _row(parent_id)).status == "SUBMITTED"  # fill-sync's job, not ours
    assert state.auto_trade_count_session == 1


async def test_fill_lands_during_cancel_window_is_kept() -> None:
    """IBKR cancel is async — the fill can complete after cancelOrder().
    The monitor must re-read AFTER the wait and keep the position."""
    parent_id = await _mk_parent()

    def on_cancel(trade):
        # 'Too late to cancel': the order finishes filling instead.
        trade.orderStatus.filled = 3
        trade.orderStatus.status = "Filled"
        trade.orderStatus.avgFillPrice = 5.7

    trade = _fake_trade(filled=2, avg=5.68)  # partial at timeout evaluation
    ib = _FakeIB(trade, on_cancel=on_cancel)
    state = _budget_state()

    res = await entry_timeout_monitor(**_run_args(state, ib, parent_id, parent_trade=trade))
    assert res["outcome"] == "filled_during_cancel"
    row = await _row(parent_id)
    assert row.status == "SUBMITTED"  # untouched; watcher fill-sync stamps FILLED
    assert state.auto_trade_count_session == 1  # budget kept
    assert len(await _audit_rows("filled_during_cancel")) == 1


async def test_cancel_unconfirmed_fails_safe() -> None:
    parent_id = await _mk_parent()
    trade = _fake_trade(filled=0)          # status never leaves 'Submitted'
    ib = _FakeIB(trade)                    # cancelOrder does nothing
    state = _budget_state()

    res = await entry_timeout_monitor(**_run_args(
        state, ib, parent_id, parent_trade=trade, cancel_wait_sec=0.3,
    ))
    assert res["outcome"] == "cancel_unconfirmed"
    assert (await _row(parent_id)).status == "SUBMITTED"  # fail-safe: untouched
    assert state.auto_trade_count_session == 1            # budget kept
    assert len(await _audit_rows("cancel_unconfirmed")) == 1


async def test_db_row_diverged_never_clobbered() -> None:
    """Broker says cancelled/0-filled but fill-sync already advanced the DB
    row — the monitor must not overwrite FILLED with CANCELLED."""
    parent_id = await _mk_parent(status="FILLED", filled_qty=3)

    def on_cancel(trade):
        trade.orderStatus.status = "Cancelled"

    trade = _fake_trade(filled=0)
    ib = _FakeIB(trade, on_cancel=on_cancel)
    state = _budget_state()

    res = await entry_timeout_monitor(**_run_args(state, ib, parent_id, parent_trade=trade))
    assert res["outcome"] == "db_row_diverged"
    assert (await _row(parent_id)).status == "FILLED"
    assert state.auto_trade_count_session == 1  # budget kept


async def test_legacy_option_price_partial_left_working() -> None:
    parent_id = await _mk_parent(exit_strategy="option_price")
    trade = _fake_trade(filled=1, avg=5.6)
    ib = _FakeIB(trade)
    state = _budget_state()

    res = await entry_timeout_monitor(**_run_args(
        state, ib, parent_id, parent_trade=trade, exit_strategy="option_price",
    ))
    assert res["outcome"] == "legacy_partial_left_working"
    assert ib.cancel_calls == []  # never cancel a partially-filled legacy bracket
    assert (await _row(parent_id)).status == "SUBMITTED"


async def test_cancel_request_failure_fails_safe() -> None:
    parent_id = await _mk_parent()
    trade = _fake_trade(filled=0)

    class _RaisingIB(_FakeIB):
        def cancelOrder(self, order):
            raise ConnectionError("TWS gone")

    ib = _RaisingIB(trade)
    state = _budget_state()
    res = await entry_timeout_monitor(**_run_args(state, ib, parent_id, parent_trade=trade))
    assert res["outcome"] == "cancel_request_failed"
    assert (await _row(parent_id)).status == "SUBMITTED"
    assert state.auto_trade_count_session == 1


async def test_already_cancelled_by_kill_switch_no_double_cancel() -> None:
    """Kill-switch (or TWS-side) cancel got there first: the monitor must
    NOT send a second cancelOrder, but still runs the same zero-fill
    stand-down accounting (CANCELLED mark + budget release)."""
    parent_id = await _mk_parent()
    trade = _fake_trade(filled=0, status="Cancelled")  # already terminal
    ib = _FakeIB(trade)
    state = _budget_state(count=1)

    res = await entry_timeout_monitor(**_run_args(state, ib, parent_id, parent_trade=trade))
    assert res["outcome"] == "cancelled_stand_down"
    assert ib.cancel_calls == []  # no redundant cancel of a terminal order
    assert (await _row(parent_id)).status == "CANCELLED"
    assert state.auto_trade_count_session == 0


async def test_timeout_env_knob_and_disable() -> None:
    prev = os.environ.pop("FEF_ENTRY_TIMEOUT_SEC", None)
    try:
        assert entry_timeout_sec() == 45.0
        os.environ["FEF_ENTRY_TIMEOUT_SEC"] = "30"
        assert entry_timeout_sec() == 30.0
        os.environ["FEF_ENTRY_TIMEOUT_SEC"] = "0"
        res = await entry_timeout_monitor(
            state=None, ibkr=None, parent_id=1, ib_order_id=1, symbol="SPX",
            qty=3, exit_strategy="spx_points", source="auto_main",
            timeout_sec=None,
        )
        assert res["outcome"] == "disabled"
    finally:
        if prev is None:
            os.environ.pop("FEF_ENTRY_TIMEOUT_SEC", None)
        else:
            os.environ["FEF_ENTRY_TIMEOUT_SEC"] = prev
