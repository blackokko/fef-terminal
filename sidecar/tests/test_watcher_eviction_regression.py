"""Regression tests for the 2026-07-07 watcher EVICTION incident.

Live failures reproduced here:
  - Parent 751 (SPX 7510P): scanner plan levels were built from the scanner's
    per-symbol QD spot (~7520.66) while entry_spx_price came from the
    active-chart tick (7530.28) — R collapsed to |stop-entry| = 0.38pt, the
    BE ladder insta-stepped to 1.5R within 200ms, and the ratcheted stop
    crossed ~40s later.
  - Parent 752 (SPY 749P): the tick-path stop check compared the SPY plan
    stop 751.16 against the ACTIVE-CHART SPX tick ("NEAR_STOP ... spot=7533.00
    stop=751.16 dist=-6781.84pt") — instant stop trigger.
  - Both stop sells were REFUSED by the anti-short guard (entry fill not yet
    in ib.positions()), and the optimistic latch had already persisted
    status=STOPPED — the refusal path returned without reverting, so both
    parents fell out of the watcher's SUBMITTED/FILLED/PARTIAL scan filter
    with contracts still open. No alert.

Fixes under test:
  (1) refusal rollback + retry (_rollback_refused_terminal_latch +
      category="watcher_refusal_retry");
  (2) entry-fill grace (_entry_fill_grace_pending, fail-open after
      FEF_ENTRY_FILL_GRACE_SEC);
  (3) arm-time plan sanity / re-anchor (_maybe_reanchor_plan,
      category="plan_reanchored");
  (4) per-symbol spot resolution + units guard (_watcher_symbol_spot,
      _units_guard_ok, and the tick path's symbol filter).
"""

from __future__ import annotations

import asyncio
import os
import time as _time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.db.models import LiveOrder
from fef_sidecar.engine.spx_target_watcher import (
    _eval_spx_points_parent,
    _tick_check_parent_stop,
    _watcher_symbol_spot,
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
    """Fill-lag IBKR stub: positions() reports whatever long qty the test
    sets (0 = the just-filled entry has not propagated yet); placeOrder
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
        dry_run=False,
    )
    d.update(kw)
    return await SignalIterRepository.write_live_order(LiveOrder(**d))


async def _db_parent(parent_id: int) -> LiveOrder:
    rows = await SignalIterRepository.query_live_orders()
    return next(r for r in rows if r.id == parent_id)


async def _drain() -> None:
    """Let spawned audit tasks finish, then cancel lingering 2s
    reservation-decay sleepers so the test loop closes clean."""
    await asyncio.sleep(0.05)
    for t in asyncio.all_tasks():
        if t is not asyncio.current_task() and not t.done():
            t.cancel()
    await asyncio.sleep(0)


# A plan that PASSES arm-time sanity for a put anchored at 7530.28
# (stop +10, T1 -15, T2 -25, T3 -50).
def _sane_put_plan() -> dict:
    return dict(
        entry_spx_price=7530.28,
        spx_target_t1=7515.28, spx_target_t2=7505.28, spx_target_t3=7480.28,
        spx_target_stop=7540.28, dynamic_stop_level=7540.28,
    )


# ---------------------------------------------------------------------------
# (1) refusal rollback + retry — the eviction hole
# ---------------------------------------------------------------------------


async def test_stop_refusal_rolls_back_and_later_tier_executes() -> None:
    """Today's sequence with the fill KNOWN but positions() lagging: stop
    crossed -> sell refused by the anti-short guard -> the optimistic STOPPED
    latch must revert (parent stays managed) and a later legitimate tier must
    still execute."""
    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=0)  # positions lag
    state = _state(_live_provider(fake))
    p = await _insert_parent(**_sane_put_plan())

    await _eval_spx_points_parent(state, p, 7541.0)  # put stop crossed

    assert fake.placed == []                       # refused — nothing shorted
    assert p.status == "FILLED"                    # latch reverted in memory
    row = await _db_parent(p.id)
    assert row.status == "FILLED"                  # ...and persisted — the
    # parent still matches the watcher scan filter (the eviction hole)
    assert row.status in ("SUBMITTED", "FILLED", "PARTIAL")
    retries = await SignalIterRepository.query_audit_log(category="watcher_refusal_retry")
    assert any("STOP" in r.summary and "reverted" in r.summary for r in retries)
    skips = await SignalIterRepository.query_audit_log(category="spx_watcher")
    assert any("SKIPPED: 0 available" in r.summary for r in skips)

    # Fill propagates; price recovers to T1 — the tier must still execute.
    # (runner_protect off here: its same-pass retrace-at-the-fire-level
    # behavior is pre-existing and not under test.)
    fake.long_qty = 3
    state.runner_protect_enabled = False
    state._watcher_refusal_backoff_ms = {}         # collapse the 2s retry clock
    await _eval_spx_points_parent(state, p, 7515.0)
    assert fake.placed == [("SELL", 1)]
    assert p.spx_target_t1_fired is True
    row = await _db_parent(p.id)
    assert row.status == "PARTIAL" and row.spx_target_t1_fired
    await _drain()


async def test_stop_retry_sells_once_positions_catch_up() -> None:
    """After a rollback, the same stop retries and resolves normally once
    ib.positions() reflects the entry fill."""
    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=0)
    state = _state(_live_provider(fake))
    p = await _insert_parent(**_sane_put_plan())

    await _eval_spx_points_parent(state, p, 7541.0)
    assert fake.placed == [] and p.status == "FILLED"

    fake.long_qty = 3
    state._watcher_refusal_backoff_ms = {}
    await _eval_spx_points_parent(state, p, 7541.0)
    assert fake.placed == [("SELL", 3)]
    assert p.status == "STOPPED"
    row = await _db_parent(p.id)
    assert row.status == "STOPPED"
    await _drain()


async def test_refusal_backoff_throttles_retry_spam() -> None:
    """Between refused attempts the parent is left alone for the backoff
    window — no second SKIPPED/refusal row pair per 0.1s tick."""
    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=0)
    state = _state(_live_provider(fake))
    p = await _insert_parent(**_sane_put_plan())

    await _eval_spx_points_parent(state, p, 7541.0)
    retries = await SignalIterRepository.query_audit_log(category="watcher_refusal_retry")
    assert len(retries) == 1
    await _eval_spx_points_parent(state, p, 7541.0)  # within backoff — skipped
    retries = await SignalIterRepository.query_audit_log(category="watcher_refusal_retry")
    assert len(retries) == 1
    assert fake.placed == []
    await _drain()


# ---------------------------------------------------------------------------
# (2) entry-fill grace
# ---------------------------------------------------------------------------


async def test_grace_skips_evaluation_until_fill_then_stop_fires() -> None:
    """Pre-fill (filled_qty=0, no avg fill, no broker position) the stop/tier
    triggers must not be consumed at all; once the fill syncs the stop fires
    normally."""
    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=0)
    state = _state(_live_provider(fake))
    p = await _insert_parent(
        status="SUBMITTED", filled_qty=0, avg_fill_price=None,
        **_sane_put_plan(),
    )

    await _eval_spx_points_parent(state, p, 7541.0)  # stop crossed pre-fill
    assert fake.placed == []
    assert p.status == "SUBMITTED"                   # never even latched
    row = await _db_parent(p.id)
    assert row.status == "SUBMITTED"
    retries = await SignalIterRepository.query_audit_log(category="watcher_refusal_retry")
    assert retries == []

    # Fill syncs in (what _sync_parent_fill_from_trades does live).
    p.filled_qty = 3
    p.avg_fill_price = 10.0
    p.status = "FILLED"
    await SignalIterRepository.update_live_order(
        p.id, filled_qty=3, avg_fill_price=10.0, status="FILLED",
    )
    fake.long_qty = 3
    await _eval_spx_points_parent(state, p, 7541.0)
    assert fake.placed == [("SELL", 3)]
    assert p.status == "STOPPED"
    await _drain()


async def test_grace_confirms_via_broker_position_without_fill_sync() -> None:
    """A visible broker position confirms the fill even when filled_qty has
    not synced yet — evaluation proceeds and the sell places."""
    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=3)  # position visible
    state = _state(_live_provider(fake))
    p = await _insert_parent(
        status="SUBMITTED", filled_qty=0, avg_fill_price=None,
        **_sane_put_plan(),
    )
    await _eval_spx_points_parent(state, p, 7541.0)
    assert fake.placed == [("SELL", 3)]
    assert p.status == "STOPPED"
    await _drain()


async def test_grace_fails_open_after_timeout_with_audit() -> None:
    """Grace expired without a confirmed fill: evaluation proceeds anyway
    (fail-open, audited once); the refused sell still rolls back so the
    parent stays managed."""
    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=0)
    state = _state(_live_provider(fake))
    p = await _insert_parent(
        status="SUBMITTED", filled_qty=0, avg_fill_price=None,
        submitted_at=datetime.now(UTC) - timedelta(seconds=600),
        **_sane_put_plan(),
    )
    await _eval_spx_points_parent(state, p, 7541.0)
    assert fake.placed == []
    assert p.status == "SUBMITTED"                   # latched then reverted
    rows = await SignalIterRepository.query_audit_log(category="spx_watcher")
    assert any("ENTRY_FILL_GRACE expired" in r.summary for r in rows)
    retries = await SignalIterRepository.query_audit_log(category="watcher_refusal_retry")
    assert len(retries) == 1
    await _drain()


# ---------------------------------------------------------------------------
# (3) arm-time plan sanity / re-anchor — parent 751's exact numbers
# ---------------------------------------------------------------------------


async def test_reanchor_replays_todays_exact_plan() -> None:
    """Parent 751 replay: plan ref 7520.66 / entry 7530.28 (put). The 0.38pt
    R must be detected and all levels re-anchored as offsets from
    entry_spx_price; T2 (7500.0, 30.28pt offset vs 25 expected = 21% dev)
    passes the sanity ratio and is preserved as a wall-anchored level."""
    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    p = await _insert_parent(
        entry_spx_price=7530.28,
        spx_target_t1=7505.66, spx_target_t2=7500.0, spx_target_t3=7450.0,
        spx_target_stop=7530.66, dynamic_stop_level=7530.66,
    )

    await _eval_spx_points_parent(state, p, 7530.0)

    assert p.spx_target_stop == pytest.approx(7540.28)   # entry + 10
    assert p.spx_target_t1 == pytest.approx(7515.28)     # entry - 15 (24.6pt off -> replaced)
    assert p.spx_target_t2 == pytest.approx(7500.0)      # preserved (passes 40% ratio)
    assert p.spx_target_t3 == pytest.approx(7480.28)     # entry - 50 (80pt off -> replaced)
    assert p.dynamic_stop_level == pytest.approx(7540.28)
    assert p.be_ladder_max_step_R == 0.0

    # No insta-BE-step, no stop trigger, no sell at spot 7530.
    assert fake.placed == []
    assert p.status == "FILLED"
    spx_rows = await SignalIterRepository.query_audit_log(category="spx_watcher")
    assert not any("BE LADDER" in r.summary for r in spx_rows)

    row = await _db_parent(p.id)
    assert row.spx_target_stop == pytest.approx(7540.28)
    assert row.spx_target_t1 == pytest.approx(7515.28)
    assert row.spx_target_t2 == pytest.approx(7500.0)
    assert row.spx_target_t3 == pytest.approx(7480.28)
    assert row.dynamic_stop_level == pytest.approx(7540.28)

    audits = await SignalIterRepository.query_audit_log(category="plan_reanchored")
    assert len(audits) == 1
    d = audits[0].details
    assert d["before"]["spx_target_stop"] == pytest.approx(7530.66)
    assert d["after"]["spx_target_stop"] == pytest.approx(7540.28)

    # Memoized: a second evaluation must not re-anchor or re-audit.
    await _eval_spx_points_parent(state, p, 7530.0)
    audits = await SignalIterRepository.query_audit_log(category="plan_reanchored")
    assert len(audits) == 1
    await _drain()


async def test_sane_plan_left_untouched() -> None:
    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake))
    p = await _insert_parent(**_sane_put_plan())
    await _eval_spx_points_parent(state, p, 7530.0)
    assert p.spx_target_stop == pytest.approx(7540.28)
    assert p.spx_target_t1 == pytest.approx(7515.28)
    audits = await SignalIterRepository.query_audit_log(category="plan_reanchored")
    assert audits == []
    await _drain()


# ---------------------------------------------------------------------------
# (4) per-symbol spot + units guard — parent 752 (SPY vs the SPX tick)
# ---------------------------------------------------------------------------


async def test_spy_parent_uses_own_spot_not_spx_active_tick() -> None:
    """SPY parent with SPX as the active chart: spot resolution must return
    the SPY scanner snapshot (not the 7533 SPX tick), the polluted SPX-scale
    entry_spx_price must be repaired, and no cross-symbol stop may fire.
    SPY's own spot crossing the stop still fires."""
    fake = FakeIB("SPY", 749.0, "P", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake), active_symbol="SPX")
    state.last_scanner_verdict_by_symbol = {
        "SPY": {"timestamp_ms": int(_time.time() * 1000), "spx_price": 750.20},
    }
    p = await _insert_parent(
        symbol="SPY", strike=749.0,
        entry_spx_price=7530.28,        # active-chart pollution (the incident)
        spx_target_t1=748.70, spx_target_t2=747.70, spx_target_t3=745.20,
        spx_target_stop=751.16, dynamic_stop_level=751.16,
    )

    sym_spot = await _watcher_symbol_spot(state, p, 7533.0, {})
    assert sym_spot == pytest.approx(750.20)         # scanner snapshot, not SPX

    await _eval_spx_points_parent(state, p, sym_spot)
    assert fake.placed == []                          # no cross-symbol trigger
    assert p.status == "FILLED"
    assert p.entry_spx_price == pytest.approx(750.20)  # polluted entry repaired
    assert p.spx_target_stop == pytest.approx(751.16)  # SPY-scale plan preserved
    row = await _db_parent(p.id)
    assert row.entry_spx_price == pytest.approx(750.20)

    # SPY's own spot crossing the (put) stop fires normally.
    await _eval_spx_points_parent(state, p, 751.20)
    assert fake.placed == [("SELL", 3)]
    assert p.status == "STOPPED"
    await _drain()


async def test_units_guard_refuses_cross_unit_comparison() -> None:
    """An SPX-scale spot against a SPY-scale stop (ratio ~10) must never be
    evaluated — audited once per 5 min, never a trigger."""
    fake = FakeIB("SPY", 749.0, "P", EXPIRY, long_qty=3)
    # active_symbol == parent symbol, so the (mislabeled SPX-scale) poll spot
    # is accepted as SPY's own tick — the units guard is the last line.
    state = _state(_live_provider(fake), active_symbol="SPY")
    p = await _insert_parent(
        symbol="SPY", strike=749.0, entry_spx_price=750.30,
        spx_target_t1=748.70, spx_target_t2=747.70, spx_target_t3=745.20,
        spx_target_stop=751.16, dynamic_stop_level=751.16,
    )
    await _eval_spx_points_parent(state, p, 7533.0)
    assert fake.placed == []
    assert p.status == "FILLED"
    rows = await SignalIterRepository.query_audit_log(category="watcher_units_mismatch")
    assert len(rows) == 1
    await _eval_spx_points_parent(state, p, 7533.0)   # throttled — no new row
    rows = await SignalIterRepository.query_audit_log(category="watcher_units_mismatch")
    assert len(rows) == 1
    await _drain()


async def test_no_symbol_spot_skips_with_single_audit() -> None:
    """No active-tick match, no scanner snapshot, no recent bar -> evaluation
    is skipped and category='watcher_no_spot' fires once per 5 min."""
    fake = FakeIB("SPY", 749.0, "P", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake), active_symbol="SPX")
    p = await _insert_parent(
        symbol="SPY", strike=749.0, entry_spx_price=750.30,
        spx_target_stop=751.16, dynamic_stop_level=751.16,
    )
    assert await _watcher_symbol_spot(state, p, 7533.0, {}) is None
    assert await _watcher_symbol_spot(state, p, 7533.0, {}) is None
    rows = await SignalIterRepository.query_audit_log(category="watcher_no_spot")
    assert len(rows) == 1
    await _drain()


# ---------------------------------------------------------------------------
# Tick fast path — same guarantees
# ---------------------------------------------------------------------------


async def test_tick_path_skips_cross_symbol_parent() -> None:
    """Incident #2 shape: an SPX tick must never evaluate a SPY parent."""
    fake = FakeIB("SPY", 749.0, "P", EXPIRY, long_qty=3)
    state = _state(_live_provider(fake), active_symbol="SPX")
    p = await _insert_parent(
        symbol="SPY", strike=749.0, entry_spx_price=750.30,
        spx_target_t1=748.70, spx_target_t2=747.70, spx_target_t3=745.20,
        spx_target_stop=751.16, dynamic_stop_level=751.16,
    )
    resolved = await _tick_check_parent_stop(state, p, 7533.0, "SPX")
    assert resolved is False
    assert fake.placed == []
    assert p.status == "FILLED"
    await _drain()


async def test_tick_path_refusal_rolls_back_then_retries() -> None:
    """Tick-path stop with fill-lagged positions: 'STOP/TICK SKIPPED' must
    revert the STOPPED latch (parent stays cached + managed); once positions
    catch up the same path fires and resolves."""
    fake = FakeIB("SPX", 7510.0, "P", EXPIRY, long_qty=0)
    state = _state(_live_provider(fake))
    p = await _insert_parent(**_sane_put_plan())

    resolved = await _tick_check_parent_stop(state, p, 7541.0, "SPX")
    assert resolved is False                          # keep managing
    assert fake.placed == []
    assert p.status == "FILLED"
    row = await _db_parent(p.id)
    assert row.status == "FILLED"
    retries = await SignalIterRepository.query_audit_log(category="watcher_refusal_retry")
    assert any("STOP/TICK" in r.summary for r in retries)

    fake.long_qty = 3
    state._watcher_refusal_backoff_ms = {}
    resolved = await _tick_check_parent_stop(state, p, 7541.0, "SPX")
    assert resolved is True
    assert fake.placed == [("SELL", 3)]
    assert p.status == "STOPPED"
    await _drain()
