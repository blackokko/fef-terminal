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
    _tick_check_parent_stop,
    _watcher_safe_sell,
    drain_detached_fires,
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
        self.connected = True

    def isConnected(self) -> bool:
        return self.connected

    async def reqPositionsAsync(self):
        # Fresh broker read (re-review N4) — same truth as the cache here.
        return self.positions()

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


# ---------------------------------------------------------------------------
# Re-review N1: fire deadline + age-bounded watchdog deferral
# ---------------------------------------------------------------------------


async def test_fire_deadline_cancels_wedged_sell_parent_stays_managed(monkeypatch) -> None:
    """A sell wedged inside IBKR qualify (hung TWS socket — the original
    incident trigger) must not block the poll loop forever: the fire deadline
    cancels the inner task, its finally-gated rollback keeps the parent
    managed, and the eval call returns."""
    monkeypatch.setenv("FEF_FIRE_DEADLINE_SEC", "0.2")
    fake = BlockingQualifyIB("SPX", 7510.0, "P", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    p = await _insert_parent()

    # Never released — the deadline is the only way out.
    await asyncio.wait_for(_eval_spx_points_parent(state, p, 7541.0), timeout=2.0)
    await asyncio.sleep(0.2)           # cancelled fire finishes its rollback

    assert fake.placed == []
    assert p.status == "FILLED"        # latch rolled back, parent managed
    row = await _db_parent(p.id)
    assert row.status == "FILLED"
    assert p.id not in state.firing_parent_ids
    rows = await SignalIterRepository.query_audit_log(category="spx_watcher")
    assert any("FIRE DEADLINE" in r.summary for r in rows)
    retries = await SignalIterRepository.query_audit_log(category="watcher_refusal_retry")
    assert any("STOP" in r.summary for r in retries)
    await _drain()


async def test_watchdog_deferral_is_age_bounded() -> None:
    """A wedged fire must not freeze the watchdog: deferral applies only
    while the youngest tracked fire is younger than the deadline+slack —
    older (or untracked) fires let a deliberately-dead OTHER loop respawn."""
    from fef_sidecar.engine.loop_watchdog import (
        _watchdog_check_once,
        heartbeat,
        spawn_supervised,
    )

    state = SimpleNamespace()

    async def dies():
        heartbeat(state, "victim")
        raise RuntimeError("dead loop")

    spawn_supervised(state, "victim", dies, cadence_sec=0.01, stale_after_sec=3600)
    await asyncio.sleep(0.05)          # victim is dead and needs a respawn

    # Young in-flight fire -> deferred.
    state.firing_parent_ids = {1}
    state._watcher_fire_started_mono = {object(): _time.monotonic()}
    assert await _watchdog_check_once(state) == []

    # Fire older than deadline+slack (the wedge) -> checks proceed.
    state._watcher_fire_started_mono = {object(): _time.monotonic() - 3600.0}
    actions = await _watchdog_check_once(state)
    assert "respawned:victim" in actions

    # firing id leaked with NO tracked fire -> also proceeds (next pass).
    state._watcher_fire_started_mono = {}
    await asyncio.sleep(0.05)          # respawned victim died again
    actions = await _watchdog_check_once(state)
    assert "respawned:victim" in actions
    await _drain()


# ---------------------------------------------------------------------------
# Re-review N2: tick-fire inner cancellation still rolls back
# ---------------------------------------------------------------------------


async def test_tick_fire_inner_cancellation_rolls_back(monkeypatch) -> None:
    """CancelledError inside _fire_tick_stop (deadline cancel at qualify)
    bypasses except-Exception — the finally must still roll the STOPPED
    latch back via the fail-safe rollback_kind default."""
    monkeypatch.setenv("FEF_FIRE_DEADLINE_SEC", "0.2")
    fake = BlockingQualifyIB("SPX", 7510.0, "P", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    p = await _insert_parent()

    resolved = await asyncio.wait_for(
        _tick_check_parent_stop(state, p, 7541.0, "SPX"), timeout=2.0,
    )
    assert not resolved                # timeout -> parent stays cached
    await asyncio.sleep(0.2)           # cancelled fire finishes its rollback

    assert fake.placed == []
    assert p.status == "FILLED"
    row = await _db_parent(p.id)
    assert row.status == "FILLED"      # NOT stranded at STOPPED
    assert p.id not in state.firing_parent_ids
    retries = await SignalIterRepository.query_audit_log(category="watcher_refusal_retry")
    assert any("STOP/TICK" in r.summary for r in retries)
    await _drain()


# ---------------------------------------------------------------------------
# Re-review N3: CancelledError in the clamped audit after placeOrder
# ---------------------------------------------------------------------------


class VaryingLongIB(FakeIB):
    """positions() returns scripted long qtys per call (last value repeats)."""

    def __init__(self, *a, longs=(), **k) -> None:
        super().__init__(*a, long_qty=longs[-1] if longs else 0, **k)
        self._longs = list(longs)
        self._call = 0

    def positions(self):
        idx = min(self._call, len(self._longs) - 1)
        self._call += 1
        self.long_qty = self._longs[idx]
        return super().positions()


async def test_cancelled_clamped_audit_after_place_stays_placed_cap_kept(monkeypatch) -> None:
    """A CancelledError delivered during safe_market_sell's clamped-audit
    write (after placeOrder) must not escape: result stays placed=True, the
    caller books placed_qty, and the cap keeps the placed slice reserved —
    no rollback, no re-sell."""
    orig = SignalIterRepository.write_audit_log

    async def _cancel_on_clamped(category, summary, actor="user", details=None,
                                 block_reason_code=None):
        if category == "safe_sell_clamped":
            raise asyncio.CancelledError()
        return await orig(category, summary, actor=actor, details=details,
                          block_reason_code=block_reason_code)

    monkeypatch.setattr(SignalIterRepository, "write_audit_log", _cancel_on_clamped)

    # Outer guard sees long=3 (requests 3); safe_market_sell re-reads long=2
    # -> inner clamp 3->2 -> placeOrder -> clamped audit raises CancelledError.
    fake = VaryingLongIB("SPX", 7510.0, "P", EXPIRY, longs=(3, 2))
    state = _state(_live_provider(fake))
    p = await _insert_parent()

    outcome: dict = {}
    placed = await _watcher_safe_sell(state, p, 3, "STOP", "SPX 7541.00",
                                      outcome=outcome)
    assert placed is True
    assert outcome["kind"] == "placed"
    assert fake.placed == [("SELL", 2)]   # exactly one transmission
    # Cap accounting: 3 reserved, 2 placed -> only the unplaced 1 released.
    assert state._watcher_lifetime_sold[p.id] == 2
    retries = await SignalIterRepository.query_audit_log(category="watcher_refusal_retry")
    assert retries == []                  # nothing to retry — it placed
    await _drain()


# ---------------------------------------------------------------------------
# Re-review N4: park classification needs a FRESH broker read
# ---------------------------------------------------------------------------


async def test_park_with_disconnected_broker_is_stuck_not_closed_external() -> None:
    """During a TWS restart the positions cache is empty but nothing is
    KNOWN: parking must classify STUCK (intervention), never CLOSED_EXTERNAL
    (which claims the user closed it)."""
    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=0)  # cache empty
    fake.connected = False                                  # ...because TWS is down
    state = _state(_live_provider(fake))
    p = await _insert_parent()
    now_ms = int(_time.time() * 1000)
    state._watcher_refusal_streak = {p.id: (19, now_ms - 10_000, now_ms)}

    await _eval_spx_points_parent(state, p, 7541.0)   # 20th refusal -> park

    assert p.status == "STUCK"                        # NOT CLOSED_EXTERNAL
    row = await _db_parent(p.id)
    assert row.status == "STUCK"
    stuck = await SignalIterRepository.query_audit_log(category="watcher_stuck")
    assert len(stuck) == 1
    assert "MANUAL INTERVENTION REQUIRED" in stuck[0].summary
    await _drain()


# ---------------------------------------------------------------------------
# Re-review N6: teardown drains detached fires
# ---------------------------------------------------------------------------


async def test_teardown_drain_waits_for_detached_fires() -> None:
    state = SimpleNamespace()
    finished = {"v": False}

    async def fire():
        await asyncio.sleep(0.05)
        finished["v"] = True

    t = asyncio.ensure_future(fire())
    state._watcher_detached_fires = {t}
    left = await drain_detached_fires(state, timeout=2.0)
    assert left == 0 and finished["v"] is True        # fire completed first

    # A wedged fire must not hang teardown past the drain timeout.
    ev = asyncio.Event()

    async def wedged():
        await ev.wait()

    t2 = asyncio.ensure_future(wedged())
    state._watcher_detached_fires = {t2}
    left = await drain_detached_fires(state, timeout=0.05)
    assert left == 1
    t2.cancel()
    # Nothing registered -> immediate no-op.
    state._watcher_detached_fires = set()
    assert await drain_detached_fires(state, timeout=0.05) == 0
    await _drain()
