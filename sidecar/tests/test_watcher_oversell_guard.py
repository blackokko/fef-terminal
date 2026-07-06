"""Regression tests for the 2026-07-06 watcher OVERSELL incident.

Live failures reproduced here:
  - SPX 7520C: the stop fired twice (SLD 3 + SLD 3 → real short -3). The
    fire-and-forget per-tick checks ran CONCURRENTLY for one parent, and the
    reservation guard's stale-position window (pending decayed ~2s after
    placement, ib.positions() still reporting the pre-fill long) let the
    re-check's sell through.
  - QQQ 724C: T1+T2 sold 1+1, runner-protect sold the last runner, then T3
    sold 1 MORE off a stale snapshot → 4 sold on 3 bought (short -1).

Fixes under test:
  (a) per-position in-flight latch (_try_spawn_exit_check) — a still-running
      check means SKIP this tick, never a concurrent re-evaluation;
  (b) optimistic fired-flag/status latch — flags set BEFORE the awaited sell,
      reverted only on a definitive refusal (preserves the 7520C phantom-bank
      guarantee);
  (c) lifetime sell cap — the watcher's OWN cumulative sold count per parent
      hard-refuses any sell past the parent's filled qty, independent of
      broker position snapshots.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.db.models import LiveOrder
from fef_sidecar.engine.spx_target_watcher import (
    _check_fixed_dollar,
    _lifetime_cap_release,
    _lifetime_cap_reserve,
    _try_spawn_exit_check,
    _watcher_safe_sell,
)
from fef_sidecar.ibkr.live import LiveIBKRProvider


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


class FakeIB:
    """Stale-position IBKR stub: positions() reports whatever long qty the
    test sets (simulating fill-propagation lag); placeOrder records sells and
    returns an immediately-filled trade."""

    def __init__(self, symbol: str, strike: float, right: str, expiry: str,
                 long_qty: int) -> None:
        self._symbol, self._strike = symbol, strike
        self._right, self._expiry = right, expiry
        self.long_qty = long_qty
        self.placed: list[tuple[str, int]] = []

    def positions(self):
        if self.long_qty <= 0:
            return []
        return [SimpleNamespace(
            contract=SimpleNamespace(
                secType="OPT", symbol=self._symbol, strike=self._strike,
                right=self._right, lastTradeDateOrContractMonth=self._expiry,
            ),
            position=self.long_qty,
        )]

    async def qualifyContractsAsync(self, *contracts):
        return list(contracts)

    def placeOrder(self, contract, order):
        self.placed.append((order.action, int(order.totalQuantity)))
        return SimpleNamespace(
            order=SimpleNamespace(orderId=1000 + len(self.placed), permId=0),
            orderStatus=SimpleNamespace(
                avgFillPrice=1.23, filled=int(order.totalQuantity),
            ),
        )


class FillTrackingIB(FakeIB):
    """Positions shrink immediately as sells place (no propagation lag)."""

    def placeOrder(self, contract, order):
        trade = super().placeOrder(contract, order)
        self.long_qty -= int(order.totalQuantity)
        return trade


def _live_provider(fake_ib: FakeIB, quote_mid: dict) -> LiveIBKRProvider:
    prov = LiveIBKRProvider()  # never connected — _ib is stubbed
    prov._ib = fake_ib

    async def _quote(contract):
        return SimpleNamespace(mid=quote_mid["v"], bid=quote_mid["v"] - 0.05)

    prov.get_option_quote = _quote  # type: ignore[method-assign]
    return prov


def _state(prov) -> SimpleNamespace:
    return SimpleNamespace(
        ibkr=prov, firing_parent_ids=set(), firing_contracts={},
        runner_protect_enabled=True, exit_dollar_semantics="whole_unrealized",
    )


async def _insert_parent(**kw) -> LiveOrder:
    d = dict(
        symbol="SPX", strike=7520.0, right="C", expiry="20260706",
        action="BUY", qty=3, order_type="LMT", limit_price=10.0,
        bracket_role="parent", status="FILLED", filled_qty=3,
        avg_fill_price=10.0, exit_strategy="fixed_dollar",
        spx_target_t1_qty=1, spx_target_t2_qty=1, spx_target_t3_qty=1,
        dollar_target_t1=300.0, dollar_target_t2=500.0,
        dollar_target_t3=1000.0, dollar_target_stop=-500.0,
        dry_run=False,
    )
    d.update(kw)
    return await SignalIterRepository.write_live_order(LiveOrder(**d))


def _stale_copy(p: LiveOrder) -> LiveOrder:
    """Detached snapshot of the parent as another (earlier-spawned) check
    would have seen it — same id, stale flags/status."""
    return LiveOrder(**p.model_dump())


async def _drain() -> None:
    """Let spawned audit tasks finish, then cancel lingering 2s
    reservation-decay sleepers so the test loop closes clean."""
    await asyncio.sleep(0.05)
    for t in asyncio.all_tasks():
        if t is not asyncio.current_task() and not t.done():
            t.cancel()
    await asyncio.sleep(0)


async def _db_parent(parent_id: int) -> LiveOrder:
    rows = await SignalIterRepository.query_live_orders()
    return next(r for r in rows if r.id == parent_id)


# ---------------------------------------------------------------------------
# (c) lifetime cap — the SPX 7520C double-stop
# ---------------------------------------------------------------------------


async def test_double_stop_second_attempt_refused_by_lifetime_cap() -> None:
    """Attempt 1 sells all 3; a stale re-check inside the reservation guard's
    stale-position window (pending decayed, positions() still long=3) must be
    hard-refused by the lifetime cap — exactly one sell ever placed."""
    fake = FakeIB("SPX", 7520.0, "C", "20260706", long_qty=3)
    quote = {"v": 4.0}  # (4-10)*100*3 = -1800 <= -500 → stop all 3
    state = _state(_live_provider(fake, quote))
    p1 = await _insert_parent()
    p_stale = _stale_copy(p1)

    await _check_fixed_dollar(state, p1)
    assert fake.placed == [("SELL", 3)]
    assert state._watcher_lifetime_sold[p1.id] == 3
    assert p1.status == "STOPPED"

    # The stale window: reservation expired (2s decay), positions() STILL
    # reports long=3 (fill not propagated), re-check holds a stale snapshot.
    state.firing_contracts.clear()
    await _check_fixed_dollar(state, p_stale)

    assert fake.placed == [("SELL", 3)]  # no second sell — no short
    rows = await SignalIterRepository.query_audit_log(category="spx_watcher")
    assert any("REFUSED by lifetime sell cap" in r.summary for r in rows)
    await _drain()


# ---------------------------------------------------------------------------
# (a) in-flight latch — concurrent checks skip, never queue
# ---------------------------------------------------------------------------


async def test_inflight_latch_skips_concurrent_check_then_releases() -> None:
    state = SimpleNamespace()
    started: list[int] = []
    release = asyncio.Event()

    def make():
        async def _slow():
            started.append(1)
            await release.wait()
        return _slow()

    assert _try_spawn_exit_check(state, "fixed_dollar", 42, make) is True
    # While the first check is in flight, subsequent ticks SKIP (no queue).
    assert _try_spawn_exit_check(state, "fixed_dollar", 42, make) is False
    await asyncio.sleep(0.01)
    assert _try_spawn_exit_check(state, "fixed_dollar", 42, make) is False
    assert len(started) == 1  # exactly one check ever ran

    # Different parent / different check kind are independent latches.
    assert _try_spawn_exit_check(state, "fixed_dollar", 43, make) is True
    assert _try_spawn_exit_check(state, "dollar_cap", 42, make) is True

    release.set()
    await asyncio.sleep(0.01)
    # Latch released after completion → next tick checks again.
    assert _try_spawn_exit_check(state, "fixed_dollar", 42, make) is True
    release.set()
    await asyncio.sleep(0.01)
    await _drain()


# ---------------------------------------------------------------------------
# (b)+(c) — the QQQ 724C runner-protect-then-T3 fourth sell
# ---------------------------------------------------------------------------


async def test_runner_protect_then_stale_t3_refused_by_lifetime_cap() -> None:
    """T1+T2 banked (cum 2), runner-protect sells the last runner (cum 3);
    a stale re-check that still sees T3 unfired must have its T3 sell refused
    by the cap AND its optimistic T3 latch reverted (no phantom bank)."""
    fake = FakeIB("QQQ", 724.0, "C", "20260706", long_qty=1)  # stale: runner still visible
    quote = {"v": 1.90}  # below entry → runner-protect territory
    state = _state(_live_provider(fake, quote))
    p1 = await _insert_parent(
        symbol="QQQ", strike=724.0, limit_price=2.0, avg_fill_price=2.0,
        dollar_target_t1=100.0, dollar_target_t2=200.0,
        dollar_target_t3=300.0, dollar_target_stop=-300.0,
        spx_target_t1_fired=True, spx_target_t2_fired=True, status="PARTIAL",
    )
    state._watcher_lifetime_sold = {p1.id: 2}  # T1+T2 were watcher sells
    p_stale = _stale_copy(p1)

    # Runner-protect: T1 banked, mid <= entry → sell the last runner.
    await _check_fixed_dollar(state, p1)
    assert fake.placed == [("SELL", 1)]
    assert state._watcher_lifetime_sold[p1.id] == 3
    assert p1.status == "CLOSED"

    # Stale re-check 8s later: option ripped, T3 unfired in its snapshot,
    # positions() still shows 1 long, reservation decayed.
    state.firing_contracts.clear()
    quote["v"] = 5.00  # (5-2)*100*3 = +900 >= 300 → T3 would fire
    await _check_fixed_dollar(state, p_stale)

    assert fake.placed == [("SELL", 1)]  # the fourth sell never happens
    assert p_stale.spx_target_t3_fired is False  # optimistic latch reverted
    row = await _db_parent(p1.id)
    assert row.spx_target_t3_fired is False
    assert row.status == "CLOSED"
    rows = await SignalIterRepository.query_audit_log(category="spx_watcher")
    assert any("T3 REFUSED by lifetime sell cap" in r.summary for r in rows)
    await _drain()


# ---------------------------------------------------------------------------
# Normal staged exit unaffected
# ---------------------------------------------------------------------------


async def test_normal_three_tier_flow_unaffected() -> None:
    fake = FillTrackingIB("SPX", 7520.0, "C", "20260706", long_qty=3)
    quote = {"v": 12.5}  # +750 on 3ct → T1 (300) + T2 (500)
    state = _state(_live_provider(fake, quote))
    p = await _insert_parent()

    await _check_fixed_dollar(state, p)
    assert fake.placed == [("SELL", 1), ("SELL", 1)]
    assert p.spx_target_t1_fired and p.spx_target_t2_fired
    assert not p.spx_target_t3_fired

    state.firing_contracts.clear()  # reservation decay between checks
    quote["v"] = 14.0  # +1200 → T3
    await _check_fixed_dollar(state, p)
    assert fake.placed == [("SELL", 1), ("SELL", 1), ("SELL", 1)]
    assert p.spx_target_t3_fired
    assert state._watcher_lifetime_sold[p.id] == 3

    row = await _db_parent(p.id)
    assert row.status == "CLOSED"
    assert row.spx_target_t1_fired and row.spx_target_t2_fired and row.spx_target_t3_fired
    rows = await SignalIterRepository.query_audit_log(category="spx_watcher")
    assert not any("lifetime sell cap" in r.summary for r in rows)
    await _drain()


# ---------------------------------------------------------------------------
# spx_points shares the same guarantees via _watcher_safe_sell
# ---------------------------------------------------------------------------


async def test_watcher_safe_sell_lifetime_cap_clamps_then_refuses() -> None:
    """spx_points T1/T2/T3/STOP/RUNNER_PROTECT all exit through
    _watcher_safe_sell — cap clamps a stop to the true remainder and refuses
    anything past it, regardless of the stale broker snapshot."""
    fake = FakeIB("SPX", 7520.0, "C", "20260706", long_qty=3)  # stale forever
    quote = {"v": 8.0}
    state = _state(_live_provider(fake, quote))
    p = await _insert_parent(exit_strategy="spx_points")
    state._watcher_lifetime_sold = {p.id: 2}  # watcher already sold 2

    placed = await _watcher_safe_sell(state, p, 3, "STOP", "SPX 7500.00")
    assert placed is True
    assert fake.placed == [("SELL", 1)]  # clamped to the 1 truly remaining
    rows = await SignalIterRepository.query_audit_log(category="spx_watcher")
    assert any("CLAMPED by lifetime sell cap" in r.summary for r in rows)

    state.firing_contracts.clear()
    placed = await _watcher_safe_sell(state, p, 1, "STOP", "SPX 7500.00")
    assert placed is False
    assert fake.placed == [("SELL", 1)]
    rows = await SignalIterRepository.query_audit_log(category="spx_watcher")
    assert any("STOP REFUSED by lifetime sell cap" in r.summary for r in rows)
    await _drain()


def test_lifetime_cap_reserve_release_edges() -> None:
    state = SimpleNamespace()
    p = SimpleNamespace(id=7, qty=3, filled_qty=0)  # fill unknown → cap = qty
    assert _lifetime_cap_reserve(state, p, 2) == 2
    assert _lifetime_cap_reserve(state, p, 2) == 1  # clamped to cap
    assert _lifetime_cap_reserve(state, p, 1) == 0  # refused
    _lifetime_cap_release(state, p, 1)
    assert _lifetime_cap_reserve(state, p, 5) == 1  # released slice reusable
    # Partial entry fill: cap = filled_qty (2), not ordered qty (3).
    p2 = SimpleNamespace(id=8, qty=3, filled_qty=2)
    assert _lifetime_cap_reserve(state, p2, 3) == 2
    # No id → untrackable; fall through to the other guards unchanged.
    p3 = SimpleNamespace(id=None, qty=3, filled_qty=3)
    assert _lifetime_cap_reserve(state, p3, 5) == 5
