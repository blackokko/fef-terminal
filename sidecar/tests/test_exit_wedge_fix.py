"""Regression tests for the 2026-07-23 EXIT-WEDGE incident (A) and the
same-day spot-source-flip incident (B).

Incident A (13:37-13:52Z): parent 796's T1 fire wedged inside an UNBOUNDED
`qualifyContractsAsync` (hung TWS contract-details farm) — twice — and each
wedge froze the ENTIRE poll loop for the full 30s FIRE DEADLINE because the
fire was awaited INLINE. A /config/symbol respawn cancelling the loop
mid-deadline would additionally have orphaned the shielded fire with NO
deadline and the firing latch held (silent per-parent skip).

Incident B: `entry_spx_price=state.last_spx_price` stamped the ACTIVE-CHART
(ES) tick on SPX parents (~+35pt vs cash). For puts, _maybe_reanchor_plan
then rewrote a CORRECT scanner plan onto the polluted anchor (parent 799:
stop 7439.04 -> 7474.5, instant T1/T2 + BE-ladder burst); for calls it
silently inflated the BE-ladder risk ~4x and disabled every step (parent 796
rode to the full -10pt stop, -$1,770). The tick fast-path separately
consumed UNLABELED bar TickEvents (ES closes) as SPX ticks (STOP/TICK at
"SPX 7464.25" while cash sat ~7429).

Fixes under test:
  (A1) bounded fire-path qualify (_qualified_ib_contract, _QUALIFY_TIMEOUT_SEC);
  (A2) inline-budget fire handoff (_fire_with_inline_budget): a wedged fire
       detaches WITH its own deadline/rollback; the loop continues; single
       flight per parent;
  (A3) watchdog wedged-fire escalation (loop_watchdog._watchdog_check_once
       cancels + audits fires older than the deferral bound);
  (B1) symbol-correct entry ref (auto_trader._entry_spx_ref);
  (B2) same-scale entry-pollution repair in _maybe_reanchor_plan (0.5%
       deviation -> repair entry to the symbol-correct spot, plan preserved);
  (B3) tick fast-path drops events without an explicit symbol=="SPX" label;
       bar TickEvents are now labeled.
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
from fef_sidecar.engine import spx_target_watcher as watcher
from fef_sidecar.engine.auto_trader import _entry_spx_ref
from fef_sidecar.engine.loop_watchdog import _watchdog_check_once
from fef_sidecar.engine.spx_target_watcher import (
    _eval_spx_points_parent,
    _fire_with_inline_budget,
    _maybe_reanchor_plan,
    _qualified_ib_contract,
)
from fef_sidecar.events import TickEvent
from fef_sidecar.ibkr.live import LiveIBKRProvider

EXPIRY = "20260723"


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


class HangingQualifyIB:
    """qualifyContractsAsync pends forever (the hung CDS-farm signature);
    positions()/placeOrder behave normally so a fire that gets PAST qualify
    would place."""

    def __init__(self, symbol="SPX", strike=7445.0, right="C", expiry=EXPIRY,
                 long_qty=3) -> None:
        self._symbol, self._strike = symbol, strike
        self._right, self._expiry = right, expiry
        self.long_qty = long_qty
        self.placed: list[tuple[str, int]] = []
        self.qualify_calls = 0
        self._hang = asyncio.Event()

    async def qualifyContractsAsync(self, *contracts):
        self.qualify_calls += 1
        await self._hang.wait()  # never set — pends until cancelled
        return list(contracts)

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

    def placeOrder(self, contract, order):
        self.placed.append((order.action, int(order.totalQuantity)))
        return SimpleNamespace(
            order=SimpleNamespace(orderId=1000 + len(self.placed), permId=0),
            orderStatus=SimpleNamespace(
                avgFillPrice=1.23, filled=int(order.totalQuantity),
            ),
        )


def _live_provider(fake_ib) -> LiveIBKRProvider:
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
        symbol="SPX", strike=7445.0, right="C", expiry=EXPIRY,
        action="BUY", qty=3, order_type="MKT", limit_price=16.3,
        bracket_role="parent", status="FILLED", filled_qty=3,
        avg_fill_price=16.3, exit_strategy="spx_points",
        spx_target_t1_qty=1, spx_target_t2_qty=1, spx_target_t3_qty=1,
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


# The incident-day 796 geometry: scanner plan from cash 7436.75, entry ref
# polluted with the ES print 7473.0.
def _796_call_plan(entry=7473.0) -> dict:
    return dict(
        entry_spx_price=entry,
        spx_target_t1=7451.75, spx_target_t2=7461.75, spx_target_t3=7650.0,
        spx_target_stop=7426.75, dynamic_stop_level=7426.75,
        be_ladder_max_step_R=0.0,
    )


# ---------------------------------------------------------------------------
# (A1) bounded fire-path qualify
# ---------------------------------------------------------------------------


async def test_qualified_ib_contract_times_out_fast(monkeypatch) -> None:
    """A hung contract-details request fails the lookup in ~the configured
    bound instead of pending forever (the incident ate 30s twice)."""
    monkeypatch.setattr(watcher, "_QUALIFY_TIMEOUT_SEC", 0.2)
    fake = HangingQualifyIB()
    state = _state(_live_provider(fake))
    p = SimpleNamespace(symbol="SPX", strike=7445.0, right="C", expiry=EXPIRY)
    t0 = _time.monotonic()
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await _qualified_ib_contract(state, p)
    assert _time.monotonic() - t0 < 2.0
    # Nothing cached — the next attempt retries the qualify.
    assert state._qualified_ib_contracts == {}
    await _drain()


async def test_wedged_qualify_lands_on_refusal_retry_not_30s_deadline(
    monkeypatch,
) -> None:
    """Stop crossed + hung qualify: with the bound TIGHTER than the inline
    budget the attempt fails inline as outcome 'error' -> optimistic latch
    reverted, parent stays managed, watcher_refusal_retry row written — and
    the whole evaluation returns in well under the old 30s stall."""
    monkeypatch.setattr(watcher, "_QUALIFY_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(watcher, "_FIRE_INLINE_BUDGET_SEC", 5.0)
    fake = HangingQualifyIB()
    state = _state(_live_provider(fake))
    p = await _insert_parent(**_796_call_plan(entry=7436.75))

    t0 = _time.monotonic()
    await _eval_spx_points_parent(state, p, 7426.0)  # call stop crossed
    elapsed = _time.monotonic() - t0

    assert elapsed < 3.0                      # not the 30s inline stall
    assert fake.placed == []                  # never reached placeOrder
    assert p.status == "FILLED"               # latch reverted
    row = await _db_parent(p.id)
    assert row.status == "FILLED"
    retries = await SignalIterRepository.query_audit_log(
        category="watcher_refusal_retry")
    assert any("STOP" in r.summary for r in retries)
    assert state.firing_parent_ids == set()   # latch released
    await _drain()


# ---------------------------------------------------------------------------
# (A2) inline-budget handoff — one wedged parent can't stall the loop
# ---------------------------------------------------------------------------


async def test_fire_handoff_returns_promptly_and_deadline_still_fires(
    monkeypatch,
) -> None:
    """A fire that outlives the inline budget is handed off detached; the
    caller returns ~immediately. The wrapper's own FIRE DEADLINE then cancels
    the wedged sell and the rollback keeps the parent managed + releases the
    firing latch (the incident's silent-leak class)."""
    monkeypatch.setattr(watcher, "_QUALIFY_TIMEOUT_SEC", 30.0)  # qualify hangs on
    monkeypatch.setattr(watcher, "_FIRE_INLINE_BUDGET_SEC", 0.2)
    monkeypatch.setenv("FEF_FIRE_DEADLINE_SEC", "0.6")
    fake = HangingQualifyIB()
    state = _state(_live_provider(fake))
    p = await _insert_parent(**_796_call_plan(entry=7436.75))

    t0 = _time.monotonic()
    await _eval_spx_points_parent(state, p, 7426.0)  # stop crossed -> wedge
    elapsed = _time.monotonic() - t0
    assert elapsed < 1.0                       # loop moved on at the budget
    assert p.id in state.firing_parent_ids     # fire still in flight, latched
    assert watcher._fire_handoff_inflight(state, p.id)

    # The detached wrapper's deadline (0.6s) cancels the wedged sell.
    await asyncio.sleep(1.2)
    assert state.firing_parent_ids == set()    # latch released by rollback
    assert not watcher._fire_handoff_inflight(state, p.id)
    assert p.status == "FILLED"                # parent stays managed
    row = await _db_parent(p.id)
    assert row.status == "FILLED"
    deadline_rows = await SignalIterRepository.query_audit_log(
        category="spx_watcher")
    assert any("FIRE DEADLINE" in r.summary for r in deadline_rows)
    assert any("FIRE HANDOFF" in r.summary for r in deadline_rows)
    await _drain()


async def test_fire_handoff_single_flight_per_parent(monkeypatch) -> None:
    """While a handed-off fire is in flight, further fire attempts for the
    SAME parent are refused without creating another coroutine."""
    monkeypatch.setattr(watcher, "_FIRE_INLINE_BUDGET_SEC", 0.1)
    state = _state(_live_provider(HangingQualifyIB()))
    p = SimpleNamespace(id=1, symbol="SPX", strike=7445.0, right="C")
    gate = asyncio.Event()
    calls = {"n": 0}

    async def _factory():
        calls["n"] += 1
        await gate.wait()
        return True

    r1 = await _fire_with_inline_budget(state, p, _factory, "T1")
    assert r1 is None and calls["n"] == 1          # handed off
    r2 = await _fire_with_inline_budget(state, p, _factory, "T1")
    assert r2 is None and calls["n"] == 1          # refused — single flight

    gate.set()
    await asyncio.sleep(0.05)
    assert not watcher._fire_handoff_inflight(state, p.id)  # cleaned up

    r3 = await _fire_with_inline_budget(state, p, _factory, "T1")
    assert r3 is True and calls["n"] == 2          # healthy fire runs inline
    await _drain()


async def test_healthy_fire_still_completes_inline(monkeypatch) -> None:
    """Sub-budget fires keep the exact pre-fix inline semantics (result
    returned synchronously to the caller)."""
    state = _state(_live_provider(HangingQualifyIB()))
    p = SimpleNamespace(id=2, symbol="SPX", strike=7445.0, right="C")

    async def _factory():
        return "placed"

    assert await _fire_with_inline_budget(state, p, _factory, "T1") == "placed"
    assert not watcher._fire_handoff_inflight(state, p.id)
    await _drain()


# ---------------------------------------------------------------------------
# (A3) watchdog wedged-fire escalation
# ---------------------------------------------------------------------------


async def test_watchdog_cancels_and_audits_overage_fire() -> None:
    """A fire older than the deferral bound is cancelled + loudly audited
    instead of silently deferring the whole watchdog pass forever."""
    hang = asyncio.Event()

    async def _wedged():
        await hang.wait()

    task = asyncio.create_task(_wedged())
    await asyncio.sleep(0)
    state = SimpleNamespace(
        firing_parent_ids={796},
        _watcher_fire_started_mono={task: _time.monotonic() - 100.0},
        watchdog_loops={}, loop_heartbeats={},
    )
    actions = await _watchdog_check_once(state)
    assert "cancelled_wedged_fire" in actions
    await asyncio.sleep(0.05)
    assert task.cancelled()
    rows = await SignalIterRepository.query_audit_log(category="loop_watchdog")
    assert any("EXIT FIRE WEDGED" in r.summary for r in rows)
    # Idempotent: a second pass never re-audits the same (now done) task.
    actions2 = await _watchdog_check_once(state)
    assert "cancelled_wedged_fire" not in actions2
    await _drain()


async def test_watchdog_still_defers_for_young_fires() -> None:
    """The pre-existing deferral behavior is preserved for healthy in-flight
    fires (younger than the bound)."""
    hang = asyncio.Event()

    async def _live_fire():
        await hang.wait()

    task = asyncio.create_task(_live_fire())
    await asyncio.sleep(0)
    state = SimpleNamespace(
        firing_parent_ids={1},
        _watcher_fire_started_mono={task: _time.monotonic() - 1.0},
        watchdog_loops={}, loop_heartbeats={},
    )
    assert await _watchdog_check_once(state) == []
    assert not task.cancelled()
    hang.set()
    await _drain()


# ---------------------------------------------------------------------------
# (B1) symbol-correct entry ref
# ---------------------------------------------------------------------------


def test_entry_spx_ref_prefers_gate8_underlying() -> None:
    state = SimpleNamespace(active_symbol="ES", last_spx_price=7473.0,
                            last_scanner_verdict_by_symbol={})
    assert _entry_spx_ref(state, "SPX",
                          {"underlying_price_at_quote": 7436.75}) == 7436.75


def test_entry_spx_ref_falls_back_to_fresh_scanner_spot() -> None:
    state = SimpleNamespace(
        active_symbol="ES", last_spx_price=7473.0,
        last_scanner_verdict_by_symbol={
            "SPX": {"spx_price": 7436.75,
                    "timestamp_ms": int(_time.time() * 1000) - 5_000},
        },
    )
    assert _entry_spx_ref(state, "SPX", {}) == 7436.75


def test_entry_spx_ref_rejects_stale_scanner_spot_and_cross_symbol_tick() -> None:
    """A stale verdict + an ES active chart must yield None, never the
    active-chart tick (the incident stamped 7473.0 on an SPX parent)."""
    state = SimpleNamespace(
        active_symbol="ES", last_spx_price=7473.0,
        last_scanner_verdict_by_symbol={
            "SPX": {"spx_price": 7436.75,
                    "timestamp_ms": int(_time.time() * 1000) - 600_000},
        },
    )
    assert _entry_spx_ref(state, "SPX", {}) is None


def test_entry_spx_ref_active_tick_ok_when_symbol_matches() -> None:
    state = SimpleNamespace(active_symbol="SPX", last_spx_price=7436.5,
                            last_scanner_verdict_by_symbol={})
    assert _entry_spx_ref(state, "SPX", {}) == 7436.5


# ---------------------------------------------------------------------------
# (B2) same-scale entry-pollution repair in _maybe_reanchor_plan
# ---------------------------------------------------------------------------


async def test_reanchor_repairs_polluted_put_entry_and_preserves_plan() -> None:
    """Parent 799 replay: ES-priced entry 7464.50 on a put whose CORRECT
    scanner plan (stop 7439.04) then looked 'wrong side'. The fix repairs the
    entry to the symbol-correct spot and PRESERVES the plan levels instead of
    rewriting them onto the polluted anchor."""
    state = _state(_live_provider(HangingQualifyIB()), active_symbol="ES")
    p = await _insert_parent(
        strike=7420.0, right="P",
        entry_spx_price=7464.5,
        spx_target_t1=7414.04, spx_target_t2=7400.0, spx_target_t3=7000.0,
        spx_target_stop=7439.04, dynamic_stop_level=7439.04,
        be_ladder_max_step_R=0.0,
    )
    ok = await _maybe_reanchor_plan(state, p, 7429.04, "scanner_snapshot")
    assert ok
    assert p.entry_spx_price == 7429.04           # repaired to cash
    assert p.spx_target_stop == 7439.04           # plan PRESERVED
    assert p.spx_target_t1 == 7414.04
    assert p.spx_target_t2 == 7400.0
    assert p.spx_target_t3 == 7000.0
    row = await _db_parent(p.id)
    assert row.entry_spx_price == 7429.04
    assert row.spx_target_stop == 7439.04
    rows = await SignalIterRepository.query_audit_log(category="plan_reanchored")
    assert any("repaired" in r.summary for r in rows)
    await _drain()


async def test_reanchor_repairs_polluted_call_entry_reviving_be_ladder() -> None:
    """Parent 796 replay: ES-priced entry 7473.0 inflated the call ladder
    risk to 46.25pt (vs 10.25 real) — no BE step ever armed and the parent
    rode to the full stop (-$1,770 live). With the repair, risk is computed
    from the cash entry and the 0.5R step ratchets the stop as designed."""
    state = _state(_live_provider(HangingQualifyIB()), active_symbol="ES")
    p = await _insert_parent(**_796_call_plan(entry=7473.0))

    ok = await _maybe_reanchor_plan(state, p, 7436.75, "scanner_snapshot")
    assert ok
    assert p.entry_spx_price == 7436.75
    assert p.spx_target_stop == 7426.75           # plan preserved

    # Next cycle at cash 7442.25 (13:40:45 live value): mfe = 5.5pt on a
    # 10pt risk -> the 0.5R BE step must ratchet the stop to entry.
    await _eval_spx_points_parent(state, p, 7442.25)
    assert p.be_ladder_max_step_R == 0.5
    assert p.dynamic_stop_level == pytest.approx(7436.75)
    await _drain()


async def test_reanchor_leaves_clean_entry_untouched() -> None:
    """A sane entry ref (within 0.5% of the symbol spot) is not repaired and
    the parent is not re-anchored — no false positives on ordinary drift."""
    state = _state(_live_provider(HangingQualifyIB()))
    p = await _insert_parent(**_796_call_plan(entry=7436.75))
    ok = await _maybe_reanchor_plan(state, p, 7437.5, "scanner_snapshot")
    assert ok
    assert p.entry_spx_price == 7436.75
    rows = await SignalIterRepository.query_audit_log(category="plan_reanchored")
    assert rows == []
    await _drain()


async def test_reanchor_pollution_repair_needs_trusted_spot() -> None:
    """With only a stale bar spot available, the polluted parent is SKIPPED
    this cycle (never repaired against an untrusted anchor)."""
    state = _state(_live_provider(HangingQualifyIB()), active_symbol="ES")
    p = await _insert_parent(
        strike=7420.0, right="P",
        entry_spx_price=7464.5,
        spx_target_t1=7414.04, spx_target_t2=7400.0, spx_target_t3=7000.0,
        spx_target_stop=7439.04, dynamic_stop_level=7439.04,
    )
    ok = await _maybe_reanchor_plan(state, p, 7429.04, "1min_bar")
    assert ok is False                    # evaluation skipped this cycle
    assert p.entry_spx_price == 7464.5    # nothing mutated
    assert p.spx_target_stop == 7439.04
    await _drain()


# ---------------------------------------------------------------------------
# (B3) tick fast-path symbol trust
# ---------------------------------------------------------------------------


def test_tick_event_symbol_defaults_to_none() -> None:
    """The event contract: no publisher gets an implicit SPX label."""
    ev = TickEvent(spx_price=7464.25)
    assert ev.symbol is None


async def test_tick_watcher_ignores_unlabeled_and_wrong_symbol_events() -> None:
    """Incident B replay: an UNLABELED (bar-stream) event carrying an
    ES-priced 7464.25 must not fire an SPX parent's stop; an explicit
    symbol='SPX' event at the same price must."""

    class _Bcast:
        def __init__(self):
            self.queue: asyncio.Queue = asyncio.Queue()

        def subscribe(self):
            outer = self

            class _Sub:
                async def __aenter__(self):
                    return outer.queue

                async def __aexit__(self, *a):
                    return False

            return _Sub()

    fake = HangingQualifyIB(strike=7420.0, right="P")
    prov = _live_provider(fake)
    state = _state(prov, active_symbol="ES")
    state.broadcaster = _Bcast()
    state.last_live_tick_spx = 0.0
    state.last_spx_price = 0.0

    # Put parent whose stop 7454.50 sits BELOW the ES-priced event (right=P
    # fires when spot >= stop). Entry ref chosen sane relative to the event
    # spot (dev < 0.25%, stop offset +8pt) so plan sanity passes and the
    # ONLY thing separating fire from no-fire is the event's symbol label.
    p = await _insert_parent(
        strike=7420.0, right="P",
        entry_spx_price=7446.5,
        spx_target_t1=7414.5, spx_target_t2=7400.0, spx_target_t3=7000.0,
        spx_target_stop=7454.5, dynamic_stop_level=7454.5,
    )
    assert p.id is not None

    task = asyncio.create_task(watcher.spx_stop_tick_watcher_loop(state))
    try:
        # Unlabeled event (the old code defaulted it to "SPX") and an
        # explicitly ES-labeled event: both must be ignored.
        await state.broadcaster.queue.put(TickEvent(spx_price=7464.25))
        await state.broadcaster.queue.put(
            TickEvent(spx_price=7464.25, symbol="ES"))
        await asyncio.sleep(0.2)
        assert state.firing_parent_ids == set()
        assert fake.qualify_calls == 0            # no fire attempt at all

        # A properly-labeled SPX event at the same price DOES evaluate the
        # stop (the fire reaches the qualify — attempt made).
        await state.broadcaster.queue.put(
            TickEvent(spx_price=7464.25, symbol="SPX"))
        await asyncio.sleep(0.3)
        assert fake.qualify_calls >= 1
        # Unwedge the qualify so the in-flight fire completes (and its DB
        # writes land) BEFORE the fixture disposes the DB.
        fake._hang.set()
        await asyncio.sleep(0.3)
        assert not watcher._fire_handoff_inflight(state, p.id)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await _drain()
