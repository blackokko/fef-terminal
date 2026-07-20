"""Regression tests for the 2026-07-20 watcher UNDERSELL / orphan incident.

Live failure reproduced here:
  - SPY 741P (parent 790): a BUY 10 entry filled in pieces (9 then 1). The
    watcher's fill-sync caught the parent mid-fill at 9 and — because the old
    idempotency guard treated PARTIAL as a *terminal* sync state — froze
    filled_qty=9 and never re-read orderStatus.filled. The lifetime sell cap is
    `filled_qty or qty`, so at exit the stop sold only 9. IBKR had filled all
    10, leaving 1 unmanaged (naked) contract until position_reconcile re-adopted
    it ~6 min later.

Root-cause fix under test (_sync_parent_fill_from_trades):
  filled_qty is kept reconciled to the AUTHORITATIVE orderStatus.filled until
  the order is genuinely done (fully filled OR broker-terminal), so the cap
  reflects the TRUE held quantity before any exit treats the parent as closed.

Invariants asserted:
  * NO UNDERSELL / orphan: a 10-filled/9-recorded parent syncs up to 10 and the
    exit sells all 10.
  * NO OVERSELL: the cap still refuses selling past filled_qty AND safe-sell
    still clamps to the broker-held long (the 2026-07-06 guard is untouched).
  * A TRUE partial (9 filled, remainder cancelled) sells exactly 9 — no phantom
    10th, no leftover.
  * filled_qty is monotonic; a lifecycle status the exit path advanced
    (STOPPED/CLOSED/FILLED) is never downgraded; the normal full-fill path is
    unaffected.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.db.models import LiveOrder
from fef_sidecar.engine.position_reconcile import _reset_watcher_runtime_state
from fef_sidecar.engine.spx_target_watcher import (
    _lifetime_cap_reserve,
    _lifetime_cap_state,
    _sync_parent_fill_from_trades,
    _watcher_safe_sell,
)
from fef_sidecar.ibkr.live import LiveIBKRProvider


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


class MutableFillIB:
    """IBKR stub whose single BUY trade's orderStatus can be advanced between
    sync calls (simulates a market entry that fills in pieces). positions()
    reports the true broker-held long, which shrinks as SELLs are placed."""

    def __init__(self, symbol: str, strike: float, right: str, expiry: str,
                 ib_order_id: int, *, order_qty: int, filled: int,
                 avg: float = 1.20, status: str = "Submitted") -> None:
        self._c = SimpleNamespace(
            secType="OPT", symbol=symbol, strike=strike, right=right,
            lastTradeDateOrContractMonth=expiry,
        )
        self._order = SimpleNamespace(
            orderId=ib_order_id, action="BUY", totalQuantity=order_qty, permId=0,
        )
        self.order_status = SimpleNamespace(
            status=status, avgFillPrice=avg, filled=filled,
        )
        self._trade = SimpleNamespace(
            order=self._order, orderStatus=self.order_status, contract=self._c,
        )
        self.held = filled          # broker long as positions() would report
        self.placed: list[tuple[str, int]] = []

    def advance_fill(self, filled: int, *, status: str = "Submitted",
                     avg: float | None = None, held: int | None = None) -> None:
        # `held` (broker position) defaults to tracking a real fill; pin it
        # explicitly to inject a BOGUS reported orderStatus.filled without
        # moving the true position (used to prove a latched sync ignores it).
        self.order_status.filled = filled
        self.order_status.status = status
        if avg is not None:
            self.order_status.avgFillPrice = avg
        self.held = filled if held is None else held

    def trades(self):
        return [self._trade]

    def positions(self):
        if self.held <= 0:
            return []
        return [SimpleNamespace(contract=self._c, position=self.held)]

    async def qualifyContractsAsync(self, *contracts):
        return list(contracts)

    def placeOrder(self, contract, order):
        self.placed.append((order.action, int(order.totalQuantity)))
        if order.action == "SELL":
            self.held -= int(order.totalQuantity)
        return SimpleNamespace(
            order=SimpleNamespace(orderId=9000 + len(self.placed), permId=0),
            orderStatus=SimpleNamespace(
                avgFillPrice=1.5, filled=int(order.totalQuantity),
            ),
        )


def _live_provider(fake_ib: MutableFillIB) -> LiveIBKRProvider:
    prov = LiveIBKRProvider()  # never connected — _ib is stubbed
    prov._ib = fake_ib

    async def _quote(contract):
        return SimpleNamespace(mid=1.5, bid=1.45, ask=1.55)

    prov.get_option_quote = _quote  # type: ignore[method-assign]
    return prov


def _state_min(prov) -> SimpleNamespace:
    return SimpleNamespace(ibkr=prov)


def _state_full(prov) -> SimpleNamespace:
    return SimpleNamespace(
        ibkr=prov, firing_parent_ids=set(), firing_contracts={},
        runner_protect_enabled=True, exit_dollar_semantics="whole_unrealized",
    )


async def _insert_parent(**kw) -> LiveOrder:
    d = dict(
        symbol="SPY", strike=741.0, right="P", expiry="20260720",
        action="BUY", qty=10, order_type="MKT", limit_price=1.20,
        bracket_role="parent", status="SUBMITTED", filled_qty=0,
        avg_fill_price=None, exit_strategy="spx_points", ib_order_id=790,
        dry_run=False,
    )
    d.update(kw)
    return await SignalIterRepository.write_live_order(LiveOrder(**d))


async def _drain() -> None:
    await asyncio.sleep(0.05)
    for t in asyncio.all_tasks():
        if t is not asyncio.current_task() and not t.done():
            t.cancel()
    await asyncio.sleep(0)


async def _db_parent(parent_id: int) -> LiveOrder:
    rows = await SignalIterRepository.query_live_orders()
    return next(r for r in rows if r.id == parent_id)


# ---------------------------------------------------------------------------
# Root cause: a mid-fill (9/10) snapshot must NOT freeze filled_qty at 9.
# ---------------------------------------------------------------------------


async def test_partial_snapshot_then_completed_fill_syncs_up_to_full() -> None:
    fake = MutableFillIB("SPY", 741.0, "P", "20260720", 790,
                         order_qty=10, filled=9, avg=1.20, status="Submitted")
    state = _state_min(_live_provider(fake))
    p = await _insert_parent()

    # Sync #1 catches the entry PARTIALLY filled (9 of 10) — the precondition
    # of the historic bug. It must record 9 but NOT latch as done.
    await _sync_parent_fill_from_trades(state, p)
    assert p.filled_qty == 9
    assert p.status == "PARTIAL"
    assert _lifetime_cap_state(state, p) == (0, 9)  # OLD code froze here forever
    assert p.id not in state._watcher_fill_synced

    # The 10th lot lands; the order completes. The fix keeps re-reading
    # orderStatus.filled, so filled_qty advances to the AUTHORITATIVE 10.
    fake.advance_fill(10, status="Filled", avg=1.21)
    await _sync_parent_fill_from_trades(state, p)
    assert p.filled_qty == 10
    assert p.status == "FILLED"
    assert _lifetime_cap_state(state, p) == (0, 10)
    assert p.id in state._watcher_fill_synced  # now latched — order is done

    # DB reflects the authoritative fill too.
    row = await _db_parent(p.id)
    assert row.filled_qty == 10
    assert row.status == "FILLED"


# ---------------------------------------------------------------------------
# End-to-end 10-filled / 9-recorded: no undersell/orphan AND no oversell.
# ---------------------------------------------------------------------------


async def test_ten_filled_nine_recorded_no_undersell_no_oversell() -> None:
    fake = MutableFillIB("SPY", 741.0, "P", "20260720", 790,
                         order_qty=10, filled=9, avg=1.20, status="Submitted")
    state = _state_full(_live_provider(fake))
    p = await _insert_parent()

    # Mid-fill snapshot, then completion — cap tracks the true held qty.
    await _sync_parent_fill_from_trades(state, p)
    assert _lifetime_cap_reserve(state, p, 10) == 9  # would have orphaned 1 (old)
    _lifetime_cap_state(state, p)  # (9, 9)
    # (release the probe reservation so the real exit below starts clean)
    state._watcher_lifetime_sold[p.id] = 0

    fake.advance_fill(10, status="Filled", avg=1.21)
    await _sync_parent_fill_from_trades(state, p)

    # NO UNDERSELL: the exit sells all 10 that IBKR actually holds → no orphan.
    placed = await _watcher_safe_sell(state, p, 10, "STOP", "SPY 741.00")
    assert placed is True
    assert fake.placed == [("SELL", 10)]
    assert fake.held == 0  # nothing left naked
    assert state._watcher_lifetime_sold[p.id] == 10

    # NO OVERSELL: a stale re-check cannot sell an 11th — cap is exhausted and
    # safe-sell would clamp to held=0 regardless.
    state.firing_contracts.clear()
    placed2 = await _watcher_safe_sell(state, p, 1, "STOP", "SPY 741.00")
    assert placed2 is False
    assert fake.placed == [("SELL", 10)]
    await _drain()


# ---------------------------------------------------------------------------
# A TRUE partial (remainder cancelled) sells exactly 9 — no phantom 10th.
# ---------------------------------------------------------------------------


async def test_true_partial_cancelled_remainder_sells_exactly_nine() -> None:
    # Entry filled 9, the remaining 1 was cancelled (entry-timeout / IBKR).
    fake = MutableFillIB("SPY", 741.0, "P", "20260720", 790,
                         order_qty=10, filled=9, avg=1.20, status="Cancelled")
    state = _state_full(_live_provider(fake))
    p = await _insert_parent()

    await _sync_parent_fill_from_trades(state, p)
    assert p.filled_qty == 9
    assert p.status == "PARTIAL"
    # Broker order is terminal (Cancelled) → latched; no more re-reads.
    assert p.id in state._watcher_fill_synced

    # Even a bogus later "10" on the stub must NOT resurrect a phantom lot,
    # because a cancelled order can never fill more (true broker-held stays 9).
    fake.advance_fill(10, status="Cancelled", held=9)
    await _sync_parent_fill_from_trades(state, p)
    assert p.filled_qty == 9
    assert _lifetime_cap_state(state, p) == (0, 9)

    # The exit sells exactly the 9 truly held — no oversell, no phantom leftover.
    placed = await _watcher_safe_sell(state, p, 9, "STOP", "SPY 741.00")
    assert placed is True
    assert fake.placed == [("SELL", 9)]
    assert fake.held == 0
    assert state._watcher_lifetime_sold[p.id] == 9
    await _drain()


# ---------------------------------------------------------------------------
# Normal full fill and defensive invariants.
# ---------------------------------------------------------------------------


async def test_normal_full_fill_syncs_once_then_latches() -> None:
    fake = MutableFillIB("SPY", 741.0, "P", "20260720", 790,
                         order_qty=10, filled=10, avg=1.30, status="Filled")
    state = _state_min(_live_provider(fake))
    p = await _insert_parent()

    await _sync_parent_fill_from_trades(state, p)
    assert p.filled_qty == 10
    assert p.status == "FILLED"
    assert p.id in state._watcher_fill_synced
    assert _lifetime_cap_state(state, p) == (0, 10)

    # A second pass is a pure no-op (latched).
    fake.advance_fill(10, status="Filled")
    await _sync_parent_fill_from_trades(state, p)
    assert p.filled_qty == 10


async def test_filled_qty_is_monotonic_never_decreases() -> None:
    fake = MutableFillIB("SPY", 741.0, "P", "20260720", 790,
                         order_qty=10, filled=9, avg=1.20, status="Submitted")
    state = _state_min(_live_provider(fake))
    p = await _insert_parent()

    await _sync_parent_fill_from_trades(state, p)
    assert p.filled_qty == 9

    # Transient low/zero read (object mid-mutation) must not lower the count.
    fake.advance_fill(0, status="Submitted")
    await _sync_parent_fill_from_trades(state, p)
    assert p.filled_qty == 9

    fake.advance_fill(10, status="Filled")
    await _sync_parent_fill_from_trades(state, p)
    assert p.filled_qty == 10


async def test_lifecycle_terminal_status_not_resynced() -> None:
    # A parent the exit path already advanced to STOPPED must never be touched
    # by the fill-sync (and is latched off immediately).
    fake = MutableFillIB("SPY", 741.0, "P", "20260720", 790,
                         order_qty=10, filled=10, avg=1.20, status="Filled")
    state = _state_min(_live_provider(fake))
    p = await _insert_parent(status="STOPPED", filled_qty=9, avg_fill_price=1.20)

    await _sync_parent_fill_from_trades(state, p)
    assert p.status == "STOPPED"     # not downgraded / not re-stamped
    assert p.filled_qty == 9         # left exactly as the exit path set it
    assert p.id in state._watcher_fill_synced


async def test_reset_watcher_runtime_state_clears_fill_sync_latch() -> None:
    # Re-adoption must let a parent re-sync its (broker-rebased) fill.
    state = SimpleNamespace(_watcher_fill_synced={7, 8}, _watcher_lifetime_sold={7: 3})
    _reset_watcher_runtime_state(state, 7)
    assert 7 not in state._watcher_fill_synced
    assert 8 in state._watcher_fill_synced
    assert 7 not in state._watcher_lifetime_sold
