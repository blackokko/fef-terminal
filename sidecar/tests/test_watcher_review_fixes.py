"""Regression tests for the 2026-07-07 adversarial-review fixes (SHIP-WITH-FIXES).

Seams under test (reviewer-confirmed untested before this file):
  (a) CancelledError delivered mid-sell at the latch->sell critical section
      -> the shielded fire completes detached, rollback runs, parent NOT
      evicted (BLOCKER-1);
  (b) exception between the lifetime-cap reserve and its release -> the
      reservation is released, the next retry is NOT refused_cap (BLOCKER-2);
  (c) safe_market_sell's clamped-audit write raises AFTER placeOrder -> the
      result is still placed=True so no caller rolls back and re-sells a
      transmitted order (MAJOR-3);
  (d) two overlapping poll-loop instances evaluating the same parent -> a
      tier sells exactly once (per-parent in-flight guard, MAJOR-6).
Plus the bounded-retry park (MAJOR-5) and the stop-path cap divergence
escalation (BLOCKER-2).
"""

from __future__ import annotations

import asyncio
import os
import time as _time
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.db.models import LiveOrder
from fef_sidecar.engine.spx_target_watcher import (
    _eval_spx_points_parent,
    _watcher_safe_sell,
)
from fef_sidecar.ibkr.live import LiveIBKRProvider

EXPIRY = "20260707"


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


class FakeIB:
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


class BlockingQualifyIB(FakeIB):
    """qualifyContractsAsync blocks until the test releases it — pins the
    fire inside the latch->sell critical section."""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.release = asyncio.Event()

    async def qualifyContractsAsync(self, *contracts):
        await self.release.wait()
        return list(contracts)


class SlowQualifyIB(FakeIB):
    async def qualifyContractsAsync(self, *contracts):
        await asyncio.sleep(0.05)
        return list(contracts)


class FlakyPositionsIB(FakeIB):
    """positions() raises on selected call numbers — simulates a TWS burp
    between the cap reserve and placement."""

    def __init__(self, *a, fail_at_calls=(), **k) -> None:
        super().__init__(*a, **k)
        self.fail_at_calls = set(fail_at_calls)
        self.position_calls = 0

    def positions(self):
        self.position_calls += 1
        if self.position_calls in self.fail_at_calls:
            raise RuntimeError("TWS burp")
        return super().positions()


def _live_provider(fake_ib: FakeIB) -> LiveIBKRProvider:
    prov = LiveIBKRProvider()  # never connected — _ib is stubbed
    prov._ib = fake_ib

    async def _quote(contract):
        return SimpleNamespace(mid=1.25, bid=1.20)

    prov.get_option_quote = _quote  # type: ignore[method-assign]
    return prov


def _state(prov, active_symbol: str = "SPX") -> SimpleNamespace:
    return SimpleNamespace(
        ibkr=prov, firing_parent_ids=set(), firing_contracts={},
        runner_protect_enabled=True, last_live_tick_ms=0,
        active_symbol=active_symbol,
    )


async def _insert_parent(**kw) -> LiveOrder:
    d = dict(
        symbol="SPX", strike=7510.0, right="P", expiry=EXPIRY,
        action="BUY", qty=3, order_type="MKT", limit_price=10.0,
        bracket_role="parent", status="FILLED", filled_qty=3,
        avg_fill_price=10.0, exit_strategy="spx_points",
        spx_target_t1_qty=1, spx_target_t2_qty=1, spx_target_t3_qty=1,
        entry_spx_price=7530.28,
        spx_target_t1=7515.28, spx_target_t2=7505.28, spx_target_t3=7480.28,
        spx_target_stop=7540.28, dynamic_stop_level=7540.28,
        dry_run=False,
    )
    d.update(kw)
    return await SignalIterRepository.write_live_order(LiveOrder(**d))


async def _db_parent(parent_id: int) -> LiveOrder:
    rows = await SignalIterRepository.query_live_orders()
    return next(r for r in rows if r.id == parent_id)


async def _drain() -> None:
    await asyncio.sleep(0.05)
    for t in asyncio.all_tasks():
        if t is not asyncio.current_task() and not t.done():
            t.cancel()
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# (a) CancelledError mid-sell — shielded fire, rollback still runs
# ---------------------------------------------------------------------------


async def test_cancel_mid_sell_does_not_evict_parent() -> None:
    """The watcher task is cancelled while the stop sell is blocked inside
    IBKR qualify (exactly the watchdog stale-respawn timing). The shielded
    fire must finish detached: the refused sell rolls the STOPPED latch back
    and the parent stays managed."""
    fake = BlockingQualifyIB("SPX", 7510.0, "P", EXPIRY, long_qty=0)
    state = _state(_live_provider(fake))
    p = await _insert_parent()

    task = asyncio.create_task(_eval_spx_points_parent(state, p, 7541.0))
    await asyncio.sleep(0.05)          # STOPPED latched; sell pinned at qualify
    assert p.status == "STOPPED"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    fake.release.set()                 # let the detached fire finish
    await asyncio.sleep(0.1)

    assert fake.placed == []           # refused — nothing sold
    assert p.status == "FILLED"        # rollback ran despite the cancel
    row = await _db_parent(p.id)
    assert row.status == "FILLED"      # persisted — parent still in the scan
    assert p.id not in state.firing_parent_ids
    retries = await SignalIterRepository.query_audit_log(category="watcher_refusal_retry")
    assert any("STOP" in r.summary for r in retries)
    await _drain()


# ---------------------------------------------------------------------------
# (b) cap reservation released on exception between reserve and placement
# ---------------------------------------------------------------------------


async def test_cap_reservation_released_on_exception_no_refused_cap_on_retry() -> None:
    """positions() raises inside safe_market_sell (after the cap reserved the
    full qty). The reservation must be released so the retry is NOT refused
    by the lifetime cap — the pre-fix leak turned every retry into
    refused_cap and re-opened the eviction."""
    fake = FlakyPositionsIB("SPX", 7510.0, "P", EXPIRY, long_qty=3,
                            fail_at_calls={2})
    state = _state(_live_provider(fake))
    p = await _insert_parent()

    outcome: dict = {}
    placed = await _watcher_safe_sell(state, p, 3, "STOP", "SPX 7541.00",
                                      outcome=outcome)
    assert placed is False
    assert outcome["kind"] == "error"
    # The leak: this used to stay 3 — every later sell became refused_cap.
    assert getattr(state, "_watcher_lifetime_sold", {}).get(p.id, 0) == 0

    state.firing_contracts.clear()     # pending reservation decay
    placed = await _watcher_safe_sell(state, p, 3, "STOP", "SPX 7541.00")
    assert placed is True
    assert fake.placed == [("SELL", 3)]
    rows = await SignalIterRepository.query_audit_log(category="spx_watcher")
    assert not any("lifetime sell cap" in r.summary for r in rows)
    await _drain()


# ---------------------------------------------------------------------------
# (c) safe_market_sell: clamped-audit failure after placeOrder stays placed
# ---------------------------------------------------------------------------


async def test_safe_sell_clamped_audit_failure_still_placed(monkeypatch) -> None:
    from fef_sidecar.ibkr.safe_sell import safe_market_sell

    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=2)  # clamps 3 -> 2
    state = SimpleNamespace(firing_contracts={})
    contract = SimpleNamespace(
        symbol="SPX", strike=7510.0, right="P",
        lastTradeDateOrContractMonth=EXPIRY,
    )

    async def _boom(*a, **k):
        raise RuntimeError("audit db down")

    monkeypatch.setattr(SignalIterRepository, "write_audit_log", _boom)
    result = await safe_market_sell(
        fake, contract, 3, purpose="watcher_close", state=state, parent_id=1,
    )
    assert result.placed is True          # the order WAS transmitted
    assert result.sell_qty == 2
    assert result.reason == "clamped_to_long"
    assert fake.placed == [("SELL", 2)]   # exactly one transmission — a
    # placed=False here would have made the caller roll back and re-sell


# ---------------------------------------------------------------------------
# (d) two overlapping poll-loop instances — tier sells exactly once
# ---------------------------------------------------------------------------


async def test_overlapping_instances_sell_tier_once() -> None:
    """Two watcher instances (the /config/symbol overlap) hold their own ORM
    copies of one parent and evaluate the same T1 touch concurrently — the
    per-parent in-flight guard must allow exactly one sell."""
    fake = SlowQualifyIB("SPX", 7510.0, "P", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    state.runner_protect_enabled = False  # isolate the tier fire
    p1 = await _insert_parent()
    p2 = LiveOrder(**p1.model_dump())     # second instance's own stale copy

    await asyncio.gather(
        _eval_spx_points_parent(state, p1, 7515.0),
        _eval_spx_points_parent(state, p2, 7515.0),
    )
    assert fake.placed == [("SELL", 1)]   # one T1 sell, not two
    row = await _db_parent(p1.id)
    assert row.spx_target_t1_fired is True
    assert row.status == "PARTIAL"
    await _drain()


# ---------------------------------------------------------------------------
# MAJOR-5: bounded refusal retries -> park
# ---------------------------------------------------------------------------


async def test_refusal_streak_parks_flat_parent_as_closed_external() -> None:
    """A parent whose position is gone at the broker (manual flatten) must
    stop flapping after the retry budget: broker net-flat -> CLOSED_EXTERNAL
    with one loud watcher_stuck row, and it leaves the watcher scan filter."""
    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=0)  # flattened
    state = _state(_live_provider(fake))
    p = await _insert_parent()
    now_ms = int(_time.time() * 1000)
    state._watcher_refusal_streak = {p.id: (19, now_ms - 10_000, now_ms)}

    await _eval_spx_points_parent(state, p, 7541.0)  # 20th refusal -> park

    assert fake.placed == []
    assert p.status == "CLOSED_EXTERNAL"
    row = await _db_parent(p.id)
    assert row.status == "CLOSED_EXTERNAL"           # out of the scan filter
    assert row.status not in ("SUBMITTED", "FILLED", "PARTIAL")
    stuck = await SignalIterRepository.query_audit_log(category="watcher_stuck")
    assert len(stuck) == 1
    assert "closed outside the watcher" in stuck[0].summary
    await _drain()


async def test_refusal_streak_parks_still_long_parent_as_stuck() -> None:
    """Same budget exhaustion but the broker STILL shows the position (a cap
    divergence that never resolves): park as STUCK + manual-intervention
    audit — never a silent terminal latch, never an infinite flap."""
    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    p = await _insert_parent()
    state._watcher_lifetime_sold = {p.id: 3}          # leaked/foreign counter
    now_ms = int(_time.time() * 1000)
    state._watcher_refusal_streak = {p.id: (19, now_ms - 10_000, now_ms)}

    await _eval_spx_points_parent(state, p, 7541.0)   # divergence -> park

    assert fake.placed == []
    assert p.status == "STUCK"
    row = await _db_parent(p.id)
    assert row.status == "STUCK"
    stuck = await SignalIterRepository.query_audit_log(category="watcher_stuck")
    assert len(stuck) == 1
    assert "MANUAL INTERVENTION REQUIRED" in stuck[0].summary
    await _drain()


# ---------------------------------------------------------------------------
# BLOCKER-2: stop-path cap divergence escalates instead of terminal-latching
# ---------------------------------------------------------------------------


async def test_stop_cap_divergence_keeps_parent_managed_and_escalates() -> None:
    """Lifetime cap says fully sold but the broker still shows the whole
    position (leaked reservation scenario): the stop must NOT terminal-latch
    — status rolls back, watcher_cap_divergence escalates loudly."""
    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    p = await _insert_parent()
    state._watcher_lifetime_sold = {p.id: 3}          # cap: everything "sold"

    await _eval_spx_points_parent(state, p, 7541.0)   # stop crossed

    assert fake.placed == []                          # cap held — no oversell
    assert p.status == "FILLED"                       # NOT evicted
    row = await _db_parent(p.id)
    assert row.status == "FILLED"
    divergence = await SignalIterRepository.query_audit_log(
        category="watcher_cap_divergence",
    )
    assert len(divergence) == 1
    assert "VERIFY POSITION" in divergence[0].summary
    await _drain()
