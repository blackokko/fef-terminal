"""Runner-protect fix (2026-07-08) — replay of the 7/8 live defect.

Live incident: 3-for-3 T1 fills triggered RUNNER_PROTECT in the same second
at the same price ("sell all at T1"). Root cause: the trigger anchored at
the T1 FILL-MOMENT spot (spx_target_t1_at_spx), which _fire_tier had just
set to the current spot in the SAME cycle -> spot <= spot fired instantly.

Fixed (backtested) semantics under test, live path AND lowvix shrink shadow:
  - anchor  = the T1 PLAN level (spx_target_t1), never the fill spot;
  - trigger = genuine retrace THROUGH the anchor with a buffer
              (calls: spot <= t1 - buf; puts: spot >= t1 + buf;
              buf = FEF_RP_BUFFER_PTS (default 0.5) x _price_scale);
  - arming  = never in the watcher cycle that filled T1 (RP evaluated
              before the tier fires, the backtest walker's ordering).
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
    _eval_spx_points_parent,
    _rp_buffer_pts,
    _shadow_step,
)
from fef_sidecar.ibkr.live import LiveIBKRProvider

EXPIRY = "20260708"


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.fixture(autouse=True)
def _default_buffer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FEF_RP_BUFFER_PTS", raising=False)


class FakeIB:
    """IBKR stub: positions() reports the configured long qty; placeOrder
    records sells and returns an immediately-filled trade."""

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
        self.long_qty -= int(order.totalQuantity)
        return SimpleNamespace(
            order=SimpleNamespace(orderId=1000 + len(self.placed), permId=0),
            orderStatus=SimpleNamespace(
                avgFillPrice=1.23, filled=int(order.totalQuantity),
            ),
        )


def _live_provider(fake_ib: FakeIB) -> LiveIBKRProvider:
    prov = LiveIBKRProvider()  # never connected — _ib is stubbed
    prov._ib = fake_ib

    async def _quote(contract):
        return SimpleNamespace(mid=1.25, bid=1.20)

    prov.get_option_quote = _quote  # type: ignore[method-assign]
    return prov


def _state(prov) -> SimpleNamespace:
    return SimpleNamespace(
        ibkr=prov, firing_parent_ids=set(), firing_contracts={},
        runner_protect_enabled=True, last_live_tick_ms=0,
        active_symbol="SPX",
    )


async def _insert_call_parent(**kw) -> LiveOrder:
    """SPX 7450C, qty 3 (1/1/1), build_plan default geometry anchored at
    7440 (t1 7455 / t2 7465 / t3 7490 / stop 7430) — passes arm-time plan
    sanity + entry-fill grace."""
    d = dict(
        symbol="SPX", strike=7450.0, right="C", expiry=EXPIRY,
        action="BUY", qty=3, order_type="MKT", limit_price=10.0,
        bracket_role="parent", status="FILLED", filled_qty=3,
        avg_fill_price=10.0, exit_strategy="spx_points",
        entry_spx_price=7440.0,
        spx_target_t1=7455.0, spx_target_t2=7465.0, spx_target_t3=7490.0,
        spx_target_stop=7430.0, dynamic_stop_level=7430.0,
        spx_target_t1_qty=1, spx_target_t2_qty=1, spx_target_t3_qty=1,
        dry_run=False,
    )
    d.update(kw)
    return await SignalIterRepository.write_live_order(LiveOrder(**d))


async def _db_parent(parent_id: int) -> LiveOrder:
    rows = await SignalIterRepository.query_live_orders()
    return next(r for r in rows if r.id == parent_id)


async def _drain(state: SimpleNamespace | None = None) -> None:
    """Let spawned audit tasks finish, then cancel lingering reservation-
    decay sleepers so the test loop closes clean. Cancelling the 2s decay
    task would LEAK its pending-sell reservation and clamp the next sell,
    so simulate the decay by clearing the map (the anti-short guard is the
    stub's own positions(), which tracks fills exactly)."""
    await asyncio.sleep(0.05)
    for t in asyncio.all_tasks():
        if t is not asyncio.current_task() and not t.done():
            t.cancel()
    await asyncio.sleep(0)
    if state is not None:
        state.firing_contracts.clear()


def test_rp_buffer_scaling(monkeypatch: pytest.MonkeyPatch) -> None:
    """SPX-class refs get the raw 0.5pt buffer; ETF-class scales by
    spot/7500; FEF_RP_BUFFER_PTS overrides the base."""
    assert _rp_buffer_pts(7455.0) == pytest.approx(0.5)
    assert _rp_buffer_pts(750.0) == pytest.approx(0.5 * 750.0 / 7500.0)
    monkeypatch.setenv("FEF_RP_BUFFER_PTS", "2.0")
    assert _rp_buffer_pts(7455.0) == pytest.approx(2.0)
    monkeypatch.setenv("FEF_RP_BUFFER_PTS", "garbage")
    assert _rp_buffer_pts(7455.0) == pytest.approx(0.5)  # bad env -> default


async def test_t1_fill_cycle_sells_t1_only_no_rp(monkeypatch) -> None:
    """THE 7/8 replay: spot touches T1 -> exactly the T1 slice sells; the
    same cycle must NOT fire RUNNER_PROTECT (the defect sold all 3)."""
    fake = FakeIB("SPX", 7450.0, "C", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    p = await _insert_call_parent()

    await _eval_spx_points_parent(state, await _db_parent(p.id), 7455.0)
    await _drain(state)

    assert fake.placed == [("SELL", 1)]  # T1 only — never "sell all at T1"
    row = await _db_parent(p.id)
    assert row.spx_target_t1_fired is True
    assert row.spx_target_t2_fired is False
    assert row.spx_target_t3_fired is False


async def test_spot_holding_at_fill_print_never_fires_rp(monkeypatch) -> None:
    """After T1 fills, cycles with spot pinned at the fill print (== T1
    level) must leave the runners alone — the old anchor (t1_at_spx ==
    fill print) fired here."""
    fake = FakeIB("SPX", 7450.0, "C", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    p = await _insert_call_parent()

    await _eval_spx_points_parent(state, await _db_parent(p.id), 7455.0)  # T1
    await _drain(state)
    for _ in range(3):  # spot stays at the fill print across cycles
        await _eval_spx_points_parent(state, await _db_parent(p.id), 7455.0)
        await _drain(state)
    # Inside the buffer band (> 7454.5) is still NOT a genuine retrace.
    await _eval_spx_points_parent(state, await _db_parent(p.id), 7454.6)
    await _drain(state)

    assert fake.placed == [("SELL", 1)]  # still only the T1 slice
    row = await _db_parent(p.id)
    assert row.spx_target_t2_fired is False and row.spx_target_t3_fired is False


async def test_retrace_through_buffer_fires_rp_once(monkeypatch) -> None:
    fake = FakeIB("SPX", 7450.0, "C", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    p = await _insert_call_parent()

    await _eval_spx_points_parent(state, await _db_parent(p.id), 7455.0)  # T1
    await _drain(state)
    # Genuine retrace THROUGH t1 - buffer (7455 - 0.5 = 7454.5).
    await _eval_spx_points_parent(state, await _db_parent(p.id), 7454.5)
    await _drain(state)

    assert fake.placed == [("SELL", 1), ("SELL", 2)]  # T1 slice, then runners
    row = await _db_parent(p.id)
    assert row.spx_target_t2_fired is True and row.spx_target_t3_fired is True

    # Idempotent: nothing left for further cycles to sell.
    await _eval_spx_points_parent(state, await _db_parent(p.id), 7454.5)
    await _drain(state)
    assert fake.placed == [("SELL", 1), ("SELL", 2)]


async def test_rp_anchor_is_plan_level_not_fill_spot(monkeypatch) -> None:
    """Gap fill: T1 fires on a print well PAST the plan level (7458). Under
    the old fill-spot anchor, 7457 would have fired RP (<= 7458); under the
    plan anchor it must not — only crossing 7454.5 does."""
    fake = FakeIB("SPX", 7450.0, "C", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    p = await _insert_call_parent()

    await _eval_spx_points_parent(state, await _db_parent(p.id), 7458.0)  # gap T1
    await _drain(state)
    assert fake.placed == [("SELL", 1)]
    await _eval_spx_points_parent(state, await _db_parent(p.id), 7457.0)
    await _drain(state)
    assert fake.placed == [("SELL", 1)]  # old anchor would have dumped here
    await _eval_spx_points_parent(state, await _db_parent(p.id), 7454.5)
    await _drain(state)
    assert fake.placed == [("SELL", 1), ("SELL", 2)]


async def test_put_side_mirror(monkeypatch) -> None:
    """Puts: T1 at entry-15; RP requires spot >= t1 + buffer."""
    fake = FakeIB("SPX", 7520.0, "P", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    p = await _insert_call_parent(
        strike=7520.0, right="P", entry_spx_price=7530.0,
        spx_target_t1=7515.0, spx_target_t2=7505.0, spx_target_t3=7480.0,
        spx_target_stop=7540.0, dynamic_stop_level=7540.0,
    )

    await _eval_spx_points_parent(state, await _db_parent(p.id), 7515.0)  # T1
    await _drain(state)
    assert fake.placed == [("SELL", 1)]
    await _eval_spx_points_parent(state, await _db_parent(p.id), 7515.0)  # hold
    await _eval_spx_points_parent(state, await _db_parent(p.id), 7515.4)  # in band
    await _drain(state)
    assert fake.placed == [("SELL", 1)]
    await _eval_spx_points_parent(state, await _db_parent(p.id), 7515.5)  # through
    await _drain(state)
    assert fake.placed == [("SELL", 1), ("SELL", 2)]


async def test_env_buffer_override(monkeypatch) -> None:
    monkeypatch.setenv("FEF_RP_BUFFER_PTS", "2.0")
    fake = FakeIB("SPX", 7450.0, "C", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    p = await _insert_call_parent()

    await _eval_spx_points_parent(state, await _db_parent(p.id), 7455.0)  # T1
    await _drain(state)
    await _eval_spx_points_parent(state, await _db_parent(p.id), 7453.5)  # > 7453
    await _drain(state)
    assert fake.placed == [("SELL", 1)]
    await _eval_spx_points_parent(state, await _db_parent(p.id), 7453.0)  # through
    await _drain(state)
    assert fake.placed == [("SELL", 1), ("SELL", 2)]


async def test_rp_disabled_leaves_runners(monkeypatch) -> None:
    fake = FakeIB("SPX", 7450.0, "C", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    state.runner_protect_enabled = False
    p = await _insert_call_parent()

    await _eval_spx_points_parent(state, await _db_parent(p.id), 7455.0)  # T1
    await _drain(state)
    await _eval_spx_points_parent(state, await _db_parent(p.id), 7454.0)
    await _drain(state)
    assert fake.placed == [("SELL", 1)]  # retrace ignored with RP off


# ---------------------------------------------------------------------------
# F2 — legacy runner_protector must never manage watcher parents
# ---------------------------------------------------------------------------


async def test_legacy_rp_scan_excludes_watcher_parents() -> None:
    """Review F2: the watcher's t1/t2/t3 market-sell CHILDREN are written
    status=SUBMITTED/filled_qty=0, so a t1-banked spx_points parent with
    open runners masquerades as a bracket with a filled T1 + resting
    runners — the legacy bid-retrace loop would place a SECOND uncapped
    market sell on top of the watcher's own RP. Structural exclusion: the
    legacy scan only ever sees option_price (or legacy NULL-strategy)
    bracket parents."""
    from fef_sidecar.engine.runner_protector import _legacy_rp_parents

    # The exact 7/8-fix steady state: T1 banked, runners open, PARTIAL.
    watcher_p = await _insert_call_parent(
        status="PARTIAL", spx_target_t1_fired=True, spx_target_t1_at_spx=7455.0,
    )
    await SignalIterRepository.write_watcher_sell_child(
        parent=await _db_parent(watcher_p.id), bracket_role="t1", qty=1,
        ib_order_id=5001, notes="watcher_close@SPX 7455.00",
    )
    fd_p = await _insert_call_parent(exit_strategy="fixed_dollar", strike=7460.0)
    legacy_p = await _insert_call_parent(exit_strategy="option_price", strike=7470.0)
    null_p = await _insert_call_parent(exit_strategy=None, strike=7480.0)
    closed_p = await _insert_call_parent(
        exit_strategy="option_price", strike=7500.0, status="CLOSED",
    )
    dry_p = await _insert_call_parent(
        exit_strategy="option_price", strike=7510.0, dry_run=True,
    )

    rows = await SignalIterRepository.query_live_orders(limit=200)
    managed_ids = {o.id for o in _legacy_rp_parents(rows)}

    assert watcher_p.id not in managed_ids   # spx_points -> invisible
    assert fd_p.id not in managed_ids        # fixed_dollar -> invisible
    assert legacy_p.id in managed_ids        # option_price bracket -> managed
    assert null_p.id in managed_ids          # legacy NULL strategy -> managed
    assert closed_p.id not in managed_ids    # resolved -> out
    assert dry_p.id not in managed_ids       # dry-run -> out


# ---------------------------------------------------------------------------
# Shadow alignment — the lowvix shrink walker runs the identical semantics
# ---------------------------------------------------------------------------


def _rp_shadow() -> tuple[SimpleNamespace, dict]:
    """Minimal lowvix-style shadow (rp_protect on): entry 7440, m=0.6 plan
    t1/t2/t3/stop = 7449/7455/7470/7434 — the numbers test_lowvix_shadow
    hand-builds."""
    async def get_option_quote(contract):
        return SimpleNamespace(bid=6.0, ask=6.2, mid=6.1)

    state = SimpleNamespace(ibkr=SimpleNamespace(get_option_quote=get_option_quote))
    sh = {
        "variant": "lowvix_shrink_shadow", "parent_id": 999,
        "symbol": "SPX", "strike": 7450.0, "right": "C", "expiry": EXPIRY,
        "qty": 3, "is_long": True,
        "entry_spx": 7440.0, "entry_premium": 5.0,
        "tier_qtys": [1, 1, 1], "tier_fired": [False, False, False],
        "t1": 7449.0, "t2": 7455.0, "t3": 7470.0, "stop": 7434.0,
        "dyn_stop": 7434.0, "be_step": 0.0, "be_lock_R": 0.0,
        "peak_mfe_R": 0.0, "fills": [], "premium_tasks": [],
        "done": False, "exit_reason": None,
        "settled_mtm_at_real_close": False,
        "rp_protect": True,
    }
    return state, sh


async def test_shadow_rp_holds_at_fill_print_then_fires_through_buffer() -> None:
    """Shadow mirror of the live fix: samples at/above t1 - buffer leave the
    runners open; a sample THROUGH 7448.5 (= 7449 - 0.5) exits them."""
    state, sh = _rp_shadow()

    _shadow_step(state, sh, 7449.0)          # T1 fill sample
    assert sh["tier_fired"] == [True, False, False]
    assert not sh["done"]

    _shadow_step(state, sh, 7449.0)          # holds at the fill print
    _shadow_step(state, sh, 7448.6)          # inside the buffer band
    assert sh["tier_fired"] == [True, False, False]
    assert not sh["done"]

    _shadow_step(state, sh, 7448.5)          # retrace THROUGH t1 - buffer
    assert sh["done"] and sh["exit_reason"] == "runner_protect"
    assert sh["fills"][-1]["tier"] == "RUNNER_PROTECT"
    assert sh["fills"][-1]["qty"] == 2
    for t in sh["premium_tasks"]:
        t.cancel()


async def test_shadow_without_rp_flag_never_runner_protects() -> None:
    """be_ladder shadow contract: no rp flag -> no rp (unchanged)."""
    state, sh = _rp_shadow()
    sh.pop("rp_protect")
    _shadow_step(state, sh, 7449.0)          # T1
    _shadow_step(state, sh, 7448.0)          # deep retrace, above trail 7446
    assert not sh["done"]
    assert sh["tier_fired"] == [True, False, False]
    for t in sh["premium_tasks"]:
        t.cancel()
