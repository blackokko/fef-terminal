"""2026-07-02 exit-layer additions + the 2026-07-06 shadow rework.

  1. Per-symbol dollar-exit scaling — fixed_dollar $ targets are scaled by
     the strike fix's notional ratio (_price_scale) for ETF-class symbols;
     SPX-class behavior is bit-identical (regression-pinned). The entry
     audit line shows the scaled ladder; the watcher consumes the parent
     row's targets unchanged (spec-driven).
  2. BE-ladder exit shadow — the watcher simulates the SPX-point BE-ladder
     exit on fixed_dollar parents (same polled spot), records shadow fills
     at quote prices, and writes a shadow_exit_outcome row + audit line
     when the real position closes. Zero real orders.
  3. 2026-07-06 shadow plumbing (7/6 EOD sec. 2): entry_spx sourced from
     the symbol's own CASH spot (never the active-chart/ES-fed tick, with a
     0.5% cross-check on parent-recorded values), premium gaps rescued from
     the nearest captured quote within ±2 min else the row is explicitly
     unusable (status column, never $0), the close-time recompute UPSERTS
     (replaces) the (parent, variant) row, and coverage extends to non-SPX
     watcher parents via _price_scale geometry.
  4. 2026-07-06 shadow flip: the variant is conditional on the parent's
     REAL exit_strategy — fixed_dollar parents shadow the BE-ladder (as
     before); spx_points parents shadow the FIXED-DOLLAR ladder
     (300/500/1000/-400, cumulative-realized, ETF-scaled,
     variant_label='fixed_dollar_shadow').
"""

from __future__ import annotations

import asyncio
import os
import time as _time
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from sqlmodel import select

from fef_sidecar.config_store import UIConfig
from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import LiveOrder, ShadowExitOutcome
from fef_sidecar.engine.spx_target_watcher import (
    _FD_SHADOW_VARIANT_LABEL,
    _fixed_dollar_actions,
    _shadow_be_ladder_cycle,
    _shadow_fd_step,
    _shadow_init,
    _shadow_split_qty,
)
from fef_sidecar.state import AppState


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.fixture(autouse=True)
def _deterministic_config(monkeypatch: pytest.MonkeyPatch):
    """Never read the live ~/.fef-terminal/config.json from tests. Patches
    the config_store source so auto_trader's in-function import sees it.
    Floor pinned LOW: this suite's LOW-confidence verdicts test the exit
    additions, not the 2026-07-08 always-on confidence floor
    (test_confidence_floor.py owns that)."""
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(
        cs, "load_config",
        lambda: UIConfig(auto_trade_confidence_floor="LOW"),
    )
    monkeypatch.setattr(cs, "config_file_status", lambda: "ok")


@pytest.fixture(autouse=True)
def _restore_mock_coherent_env():
    """AppState.build() setdefaults FEF_MOCK_COHERENT=true process-wide;
    this file sorts before test_ibkr_mock.py whose default-bars stream test
    hangs in coherent mode — leave the env exactly as found."""
    before = os.environ.get("FEF_MOCK_COHERENT")
    yield
    if before is None:
        os.environ.pop("FEF_MOCK_COHERENT", None)
    elif os.environ.get("FEF_MOCK_COHERENT") != before:
        os.environ["FEF_MOCK_COHERENT"] = before


# ---------------------------------------------------------------------------
# 1. Per-symbol dollar-exit scaling
# ---------------------------------------------------------------------------


def _armed_fixed_dollar_state(monkeypatch: pytest.MonkeyPatch) -> AppState:
    monkeypatch.setenv("FEF_LIVE_TRADING", "true")
    s = AppState.build()
    s.active_symbol = "SPX"
    s.auto_trade_enabled = True
    s.auto_trade_trigger_types = ["confluence", "aligned_only"]
    s.auto_trade_qty_override = 3
    s.auto_trade_cool_down_sec = 300
    s.exit_strategy_long = "fixed_dollar"
    s.exit_dollar_t1 = 300.0
    s.exit_dollar_t2 = 500.0
    s.exit_dollar_t3 = 1000.0
    s.exit_dollar_stop = -400.0
    return s


def _fd_verdict(*, strike: float, underlying: float) -> dict:
    return {
        "direction": "LONG_CALLS",
        "confidence": "LOW",
        "reasoning": "ALIGNED LONG_CALLS — test",
        "strike_rec": strike,
        "t1": underlying + 15, "t2": underlying + 25,
        "t3": underlying + 50, "stop": underlying - 10,
        "aligned": True,
        "wall_collapse_trigger": False,
        "option_pricing": {
            "contract_strike": strike, "right": "C", "entry_limit": 2.0,
            "delta": 0.4, "underlying_price_at_quote": underlying,
            "t1": 3.0, "t2": 4.0, "t3": 6.0, "stop": 1.0,
        },
        "suggested_qty": 3,
    }


async def _fd_parent() -> LiveOrder:
    async with get_session() as session:
        rows = list((await session.exec(
            select(LiveOrder).where(LiveOrder.bracket_role == "parent")
        )).all())
    assert len(rows) == 1
    return rows[0]


async def test_etf_symbol_gets_notional_scaled_dollar_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPY @ 743: scale = 743/7500 -> 300/500/1000/-400 becomes 30/50/99/-40
    (whole dollars). Live exhibit: today's SPY 743C couldn't reach the raw
    SPX-notional targets."""
    from fef_sidecar.engine.auto_trader import maybe_auto_trade

    state = _armed_fixed_dollar_state(monkeypatch)
    d = await maybe_auto_trade(
        state, _fd_verdict(strike=743.0, underlying=743.0),
        symbol="SPY", source="auto_scanner",
    )
    assert d.fired
    p = await _fd_parent()
    assert p.exit_strategy == "fixed_dollar"
    assert p.dollar_target_t1 == pytest.approx(30.0)
    assert p.dollar_target_t2 == pytest.approx(50.0)
    assert p.dollar_target_t3 == pytest.approx(99.0)
    assert p.dollar_target_stop == pytest.approx(-40.0)
    # The entry audit line prints the SCALED ladder (visible per trade).
    orders = await SignalIterRepository.query_audit_log(category="order")
    assert len(orders) == 1
    assert "$ T1/T2/T3/STOP=30.0/50.0/99.0/-40.0" in orders[0].summary


async def test_spx_dollar_targets_bit_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPX-class (spot >= 2000) pins scale to 1.0 — regression pin that the
    scaling change cannot move SPX behavior by a cent."""
    from fef_sidecar.engine.auto_trader import maybe_auto_trade

    state = _armed_fixed_dollar_state(monkeypatch)
    d = await maybe_auto_trade(
        state, _fd_verdict(strike=7450.0, underlying=7448.0),
        symbol="SPX", source="auto_scanner",
    )
    assert d.fired
    p = await _fd_parent()
    assert p.dollar_target_t1 == pytest.approx(300.0)
    assert p.dollar_target_t2 == pytest.approx(500.0)
    assert p.dollar_target_t3 == pytest.approx(1000.0)
    assert p.dollar_target_stop == pytest.approx(-400.0)
    orders = await SignalIterRepository.query_audit_log(category="order")
    assert "$ T1/T2/T3/STOP=300.0/500.0/1000.0/-400.0" in orders[0].summary


async def test_etf_scaling_toggle_and_min_magnitude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fef_sidecar.engine.auto_trader import maybe_auto_trade
    import fef_sidecar.config_store as cs

    # Toggle OFF: SPY keeps the raw SPX-notional targets. (Floor stays LOW —
    # these LOW-confidence fires test the $ scaling, not the floor.)
    state = _armed_fixed_dollar_state(monkeypatch)
    monkeypatch.setattr(
        cs, "load_config", lambda: UIConfig(
            etf_dollar_exit_scaling=False, auto_trade_confidence_floor="LOW",
        )
    )
    d = await maybe_auto_trade(
        state, _fd_verdict(strike=743.0, underlying=743.0),
        symbol="SPY", source="auto_scanner",
    )
    assert d.fired
    p = await _fd_parent()
    assert p.dollar_target_t1 == pytest.approx(300.0)
    assert p.dollar_target_stop == pytest.approx(-400.0)

    # $10 minimum magnitude: a $50 T1 would scale to ~$5 -> floored to $10.
    monkeypatch.setattr(
        cs, "load_config",
        lambda: UIConfig(auto_trade_confidence_floor="LOW"),
    )
    state2 = _armed_fixed_dollar_state(monkeypatch)
    state2.exit_dollar_t1 = 50.0
    d2 = await maybe_auto_trade(
        state2, _fd_verdict(strike=620.0, underlying=620.0),
        symbol="QQQ", source="auto_scanner",
    )
    assert d2.fired
    async with get_session() as session:
        rows = list((await session.exec(
            select(LiveOrder).where(LiveOrder.bracket_role == "parent")
        )).all())
    q_parent = [r for r in rows if r.symbol == "QQQ"][0]
    assert q_parent.dollar_target_t1 == pytest.approx(10.0)


def test_scaled_targets_reachable_by_watcher_math() -> None:
    """Spec-driven watcher consumes the scaled ladder unchanged: a +$0.35
    premium move on a 3-lot SPY position now clears T1 ($30 incr) and T2
    ($20 incr) under cumulative semantics — with the raw $300/$500 ladder
    the same move fired nothing."""
    stop_qty, fires = _fixed_dollar_actions(
        entry_px=2.0, mid=2.35, original_qty=3,
        tier_qtys=[1, 1, 1], tier_fired=[False, False, False],
        d_targets=[30.0, 50.0, 99.0], d_stop=-40.0,
        semantics="cumulative_realized",
    )
    assert stop_qty == 0
    assert fires == [("T1", 1), ("T2", 1)]
    # Raw SPX-notional ladder: unreachable at the same premium move.
    _, fires_raw = _fixed_dollar_actions(
        entry_px=2.0, mid=2.35, original_qty=3,
        tier_qtys=[1, 1, 1], tier_fired=[False, False, False],
        d_targets=[300.0, 500.0, 1000.0], d_stop=-400.0,
        semantics="cumulative_realized",
    )
    assert fires_raw == []


# ---------------------------------------------------------------------------
# 2. BE-ladder exit shadow
# ---------------------------------------------------------------------------


def _shadow_state(quote_bids: list[float]) -> SimpleNamespace:
    """Fake state whose option quotes pop from quote_bids in call order."""
    bids = list(quote_bids)

    async def get_option_quote(contract):
        bid = bids.pop(0) if bids else None
        return SimpleNamespace(
            bid=bid, ask=(bid + 0.2) if bid else None,
            mid=(bid + 0.1) if bid else None,
        )

    return SimpleNamespace(
        ibkr=SimpleNamespace(get_option_quote=get_option_quote),
        shadow_be_ladder={},
        _watcher_banked_cum={},
    )


def _fake_parent(**kw) -> SimpleNamespace:
    """Watcher parent fixture. Default exit_strategy is fixed_dollar — under
    the 2026-07-06 flip that is the strategy whose shadow is the BE-ladder
    (spx_points parents shadow the fixed-$ ladder instead). The spx_target_*
    levels stay set so the BE-ladder shadow exercises the parent-levels
    path; real fixed_dollar parents may or may not carry them."""
    d = dict(
        id=1, symbol="SPX", strike=7450.0, right="C", expiry="20260702",
        qty=3, exit_strategy="fixed_dollar", status="FILLED",
        entry_spx_price=7440.0,
        spx_target_t1=7455.0, spx_target_t2=7465.0, spx_target_t3=7490.0,
        spx_target_stop=7430.0,
        avg_fill_price=5.0, limit_price=5.0,
    )
    d.update(kw)
    return SimpleNamespace(**d)


async def _settle_tasks() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


async def _outcome_rows() -> list[ShadowExitOutcome]:
    async with get_session() as session:
        return list((await session.exec(select(ShadowExitOutcome))).all())


def test_shadow_split_qty() -> None:
    assert _shadow_split_qty(3) == [1, 1, 1]     # user's live size
    assert _shadow_split_qty(10) == [5, 3, 2]    # canonical 50/30/20
    assert _shadow_split_qty(2) == [1, 1, 0]
    assert _shadow_split_qty(1) == [1, 0, 0]


async def test_shadow_full_target_run_dollar_math() -> None:
    """Hand-computed fixture: entry prem 5.0, tier fills at quote bids
    6/8/12 on a 1/1/1 split -> shadow $ = 100+300+700 = +$1,100; real
    banked $777 -> audit line 'would have made +$1,100 vs real +$777'."""
    state = _shadow_state([6.0, 8.0, 12.0])
    state._watcher_banked_cum = {1: 777.0}
    p = _fake_parent()

    await _shadow_be_ladder_cycle(state, [p], 7440.0)   # init
    assert 1 in state.shadow_be_ladder
    sh = state.shadow_be_ladder[1]
    assert sh["tier_qtys"] == [1, 1, 1]
    assert sh["levels_source"] == "parent_spx_targets"

    await _shadow_be_ladder_cycle(state, [p], 7456.0)   # T1 touch
    await _settle_tasks()
    await _shadow_be_ladder_cycle(state, [p], 7466.0)   # T2 touch
    await _settle_tasks()
    await _shadow_be_ladder_cycle(state, [p], 7491.0)   # T3 touch -> done
    await _settle_tasks()
    assert sh["done"] and sh["exit_reason"] == "targets"

    # Real position closes -> parent leaves the active set -> finalize.
    await _shadow_be_ladder_cycle(state, [], 7491.0)
    assert state.shadow_be_ladder == {}

    rows = await _outcome_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r.parent_order_id == 1
    assert r.variant_label == "be_ladder_shadow"
    assert r.direction == "LONG_CALLS" and r.qty == 3
    assert r.exit_reason == "targets"
    # SPX-R: fills at 7456/7466/7491, risk 10 -> (1.6+2.6+5.1)/3 = 3.1
    assert r.realized_r == pytest.approx(3.1, abs=1e-6)
    assert r.peak_r == pytest.approx(5.1, abs=1e-6)
    assert r.variant_params["shadow_dollars"] == pytest.approx(1100.0)
    assert r.variant_params["real_dollars"] == pytest.approx(777.0)
    assert r.actual_realized_pnl == pytest.approx(777.0)
    fills = r.variant_params["fills"]
    assert [(f["tier"], f["qty"], f["premium"]) for f in fills] == [
        ("T1", 1, 6.0), ("T2", 1, 8.0), ("T3", 1, 12.0),
    ]

    audits = await SignalIterRepository.query_audit_log(category="shadow_exit")
    assert len(audits) == 1
    assert "SHADOW BE-ladder would have made +$1,100 vs real +$777" in audits[0].summary
    # ZERO real ordering from the shadow path.
    assert await SignalIterRepository.query_audit_log(category="order") == []
    assert await SignalIterRepository.query_audit_log(category="spx_watcher") == []


async def test_shadow_be_ratchet_trails_stop_hand_math() -> None:
    """MFE 1.6R ratchets straight to the 1.5R rung (stop -> entry+1.0R =
    7450); the retrace to 7449 stops the runners there. Hand math:
    shadow $ = (6-5)*100 - (5-4)*100*2 = -$100; realized_R =
    1/3*1.6 + 2/3*0.9 = 1.1333."""
    state = _shadow_state([6.0, 4.0])
    p = _fake_parent(id=2)

    await _shadow_be_ladder_cycle(state, [p], 7440.0)   # init
    sh = state.shadow_be_ladder[2]
    await _shadow_be_ladder_cycle(state, [p], 7456.0)   # T1 + ratchet to lock 1.0R
    await _settle_tasks()
    assert sh["be_step"] == pytest.approx(1.5)
    assert sh["be_lock_R"] == pytest.approx(1.0)
    assert sh["dyn_stop"] == pytest.approx(7450.0)

    await _shadow_be_ladder_cycle(state, [p], 7449.0)   # trail stop hit
    await _settle_tasks()
    assert sh["done"] and sh["exit_reason"] == "trail"

    await _shadow_be_ladder_cycle(state, [], 7449.0)    # real close -> finalize
    rows = await _outcome_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r.exit_reason == "trail"
    assert r.variant_params["shadow_dollars"] == pytest.approx(-100.0)
    # (row stores realized_r rounded to 4 decimals)
    assert r.realized_r == pytest.approx(1.0 / 3 * 1.6 + 2.0 / 3 * 0.9, abs=1e-4)
    assert r.actual_realized_pnl is None                # no real banked $ known
    audits = await SignalIterRepository.query_audit_log(category="shadow_exit")
    assert "would have made -$100 vs real n/a" in audits[0].summary


async def test_shadow_mtm_settle_on_real_close_and_default_levels() -> None:
    """fixed_dollar parents carry no SPX targets -> levels come from
    build_plan's default geometry (entry 7440 -> 7455/7465/7490/7430).
    Real position closes while the shadow is still open -> remainder is
    settled mark-to-market and labeled honestly."""
    state = _shadow_state([5.5])
    p = _fake_parent(
        id=3, exit_strategy="fixed_dollar",
        spx_target_t1=None, spx_target_t2=None, spx_target_t3=None,
        spx_target_stop=None,
    )

    await _shadow_be_ladder_cycle(state, [p], 7440.0)   # init via build_plan
    sh = state.shadow_be_ladder[3]
    assert sh["levels_source"] == "build_plan_default"
    assert (sh["t1"], sh["t2"], sh["t3"], sh["stop"]) == (7455.0, 7465.0, 7490.0, 7430.0)

    await _shadow_be_ladder_cycle(state, [p], 7444.0)   # 0.4R — nothing fires
    assert sh["fills"] == [] and not sh["done"]

    await _shadow_be_ladder_cycle(state, [], 7444.0)    # real close -> MTM settle
    await _settle_tasks()
    rows = await _outcome_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r.exit_reason == "real_closed_mtm"
    assert r.variant_params["settled_mtm_at_real_close"] is True
    # 3 contracts marked at bid 5.5 vs entry 5.0 -> +$150; R = 0.4
    assert r.variant_params["shadow_dollars"] == pytest.approx(150.0)
    assert r.realized_r == pytest.approx(0.4, abs=1e-6)
    # Zero orders, zero LiveOrder rows — the shadow never touches execution.
    async with get_session() as session:
        assert list((await session.exec(select(LiveOrder))).all()) == []
    assert await SignalIterRepository.query_audit_log(category="order") == []


async def test_shadow_covers_non_spx_parents_with_scaled_geometry() -> None:
    """2026-07-06 coverage fix: SPY/QQQ watcher parents get shadows too. A
    SPY fixed_dollar parent (no SPX targets) draws build_plan default
    geometry at ITS OWN price level — _price_scale(743.2) scales the
    SPX-calibrated 10-pt stop to ~0.99 SPY pts. (The fake state has no
    active_symbol, so the polled spot is treated as the symbol's own —
    production resolves per-symbol cash spots via _shadow_cash_spot.)"""
    state = _shadow_state([6.0, 5.8])
    p = _fake_parent(
        id=4, symbol="SPY", strike=744.0, entry_spx_price=743.2,
        spx_target_t1=None, spx_target_t2=None, spx_target_t3=None,
        spx_target_stop=None,
    )

    await _shadow_be_ladder_cycle(state, [p], 743.2)    # init
    assert 4 in state.shadow_be_ladder
    sh = state.shadow_be_ladder[4]
    assert sh["variant"] == "be_ladder_shadow"          # fixed_dollar parent
    assert sh["levels_source"] == "build_plan_default"
    scale = 743.2 / 7500.0
    sd = 10.0 * scale                                    # scaled stop distance
    assert sh["stop"] == pytest.approx(743.2 - sd, abs=1e-6)
    assert sh["t1"] == pytest.approx(743.2 + 1.5 * sd, abs=1e-6)
    assert sh["t2"] == pytest.approx(743.2 + 2.5 * sd, abs=1e-6)
    assert sh["t3"] == pytest.approx(743.2 + 5.0 * sd, abs=1e-6)

    await _shadow_be_ladder_cycle(state, [p], 743.2 + 1.6 * sd)  # T1 touch
    await _settle_tasks()
    assert sh["tier_fired"] == [True, False, False]

    await _shadow_be_ladder_cycle(state, [], 743.2 + 1.6 * sd)   # real close
    await _settle_tasks()
    rows = await _outcome_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r.variant_label == "be_ladder_shadow"
    assert r.entry_spx == pytest.approx(743.2)
    assert r.status == "ok"
    # T1 slice at bid 6.0 (+$100), MTM remainder (2ct) at bid 5.8 (+$160).
    assert r.variant_params["shadow_dollars"] == pytest.approx(260.0)


# ---------------------------------------------------------------------------
# 3. Shadow plumbing fixes (7/6 EOD sec. 2)
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(_time.time() * 1000)


async def test_shadow_entry_spx_sources_cash_not_es_fed_tick() -> None:
    """(a) The 7/6 failure: an ES active chart fed 7561 into the watcher's
    polled spot while SPX cash sat at 7511 — the shadow must take the cash
    reference (scanner snapshot) and record provenance, not the ES tick."""
    state = _shadow_state([])
    state.active_symbol = "ES"                          # ES morning
    state.last_scanner_verdict_by_symbol = {
        "SPX": {"spx_price": 7511.0, "timestamp_ms": _now_ms()},
    }
    p = _fake_parent(
        id=10, entry_spx_price=None,                    # fixed_dollar parents carry none
        spx_target_t1=None, spx_target_t2=None, spx_target_t3=None,
        spx_target_stop=None,
    )

    await _shadow_be_ladder_cycle(state, [p], 7561.0)   # poll spot = ES-fed
    sh = state.shadow_be_ladder[10]
    assert sh["entry_spx"] == pytest.approx(7511.0)     # cash, NOT 7561
    assert sh["entry_spx_source"] == "cash_spot(scanner_snapshot)"
    assert sh["spot_source"] == "scanner_snapshot"


async def test_shadow_entry_spx_overrides_polluted_parent_value() -> None:
    """(a) A parent-recorded entry_spx_price carrying the ES basis (7561 vs
    cash 7511 = +0.67% > the 0.5% tolerance) is overridden by the cash
    reference; both values persist for audit."""
    state = _shadow_state([])
    state.active_symbol = "ES"
    state.last_scanner_verdict_by_symbol = {
        "SPX": {"spx_price": 7511.0, "timestamp_ms": _now_ms()},
    }
    p = _fake_parent(id=11, entry_spx_price=7561.0)

    await _shadow_be_ladder_cycle(state, [p], 7561.0)
    sh = state.shadow_be_ladder[11]
    assert sh["entry_spx"] == pytest.approx(7511.0)
    assert sh["entry_spx_source"] == "cash_spot_override(scanner_snapshot)"
    assert sh["parent_entry_spx_price"] == pytest.approx(7561.0)


async def test_shadow_entry_spx_keeps_consistent_parent_value() -> None:
    """(a) A parent entry_spx_price that AGREES with cash within 0.5% is
    used as-is (it is the truest entry mark)."""
    state = _shadow_state([])
    state.active_symbol = "SPX"
    p = _fake_parent(id=12, entry_spx_price=7440.5)

    await _shadow_be_ladder_cycle(state, [p], 7441.0)   # active tick = cash
    sh = state.shadow_be_ladder[12]
    assert sh["entry_spx"] == pytest.approx(7440.5)
    assert sh["entry_spx_source"] == "parent_entry_spx_price"


async def test_shadow_defers_init_without_cash_reference() -> None:
    """(a) No trustworthy cash source (off-symbol chart, no scanner cache,
    no recent bars): the shadow DEFERS init rather than trusting the polled
    tick — the 7/6 rows were exactly unvalidated entries."""
    state = _shadow_state([])
    state.active_symbol = "ES"
    state.last_scanner_verdict_by_symbol = {}
    p = _fake_parent(id=13)

    await _shadow_be_ladder_cycle(state, [p], 7561.0)
    assert state.shadow_be_ladder == {}                 # deferred, not polluted


async def _seed_chain_capture(
    *, ts_ms: int, bid: float, symbol: str = "SPX", strike: float = 7450.0,
    right: str = "C", expiry: str = "20260702",
) -> None:
    from fef_sidecar.db.models import HistoricalOptionChainPoint
    async with get_session() as session:
        session.add(HistoricalOptionChainPoint(
            timestamp_ms=ts_ms, session_date="2026-07-06",
            symbol=symbol, strike=strike, right=right, expiry=expiry,
            spot_at_capture=7450.0, bid=bid, ask=bid + 0.2, mid=bid + 0.1,
        ))


async def test_premium_gap_rescued_from_captured_quote_within_2min() -> None:
    """(b) Fills whose live quote capture failed are rescued from the nearest
    historical_option_chain_point within ±2 min — the row stays usable with
    real dollars instead of a silent $0."""
    state = _shadow_state([])                            # every live quote fails
    p = _fake_parent(id=20)

    await _shadow_be_ladder_cycle(state, [p], 7440.0)    # init
    await _shadow_be_ladder_cycle(state, [p], 7456.0)    # T1 (premium None)
    await _settle_tasks()
    # Captured chain quote 30s after the fill — inside the ±2 min window.
    await _seed_chain_capture(ts_ms=_now_ms() + 30_000, bid=6.2)

    await _shadow_be_ladder_cycle(state, [], 7456.0)     # close -> finalize
    await _settle_tasks()
    rows = await _outcome_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r.status == "ok"
    fills = r.variant_params["fills"]
    assert all(f["premium"] == pytest.approx(6.2) for f in fills)
    assert all(str(f["premium_src"]).startswith("capture_nearest(bid") for f in fills)
    # T1 1ct + MTM 2ct, all at 6.2 vs entry 5.0 -> +$360.
    assert r.variant_params["shadow_dollars"] == pytest.approx(360.0)
    assert r.variant_params["premium_gaps"] == 0


async def test_premium_gap_marks_row_unusable_never_zero() -> None:
    """(b) No live quote AND no captured quote in ±2 min: the row is marked
    unusable via the status column with shadow_dollars=None — it must NEVER
    surface as a $0 outcome (the 7/6 rows polluted the A/B aggregate)."""
    state = _shadow_state([])
    p = _fake_parent(id=21)

    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    await _shadow_be_ladder_cycle(state, [p], 7456.0)    # T1, no premium anywhere
    await _settle_tasks()
    await _shadow_be_ladder_cycle(state, [], 7456.0)
    await _settle_tasks()

    rows = await _outcome_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r.status == "unusable_premium_gap"
    assert r.variant_params["status"] == "unusable_premium_gap"
    assert r.variant_params["shadow_dollars"] is None    # NOT 0.0
    assert r.variant_params["premium_gaps"] == 2         # T1 fill + MTM fill
    # SPX-R math is premium-independent and stays available.
    assert r.realized_r == pytest.approx(1.6, abs=1e-4)
    audits = await SignalIterRepository.query_audit_log(category="shadow_exit")
    assert "would have made n/a" in audits[0].summary
    assert "[unusable_premium_gap]" in audits[0].summary


async def test_finalize_upserts_replacing_stale_entry_time_row() -> None:
    """(c) Parent 747's row froze at its entry-time compute because the
    close-time recompute collided with ux_shadow_exit_parent_variant. The
    finalize now UPSERTS: exactly one row per (parent, variant), carrying
    the RECOMPUTED values."""
    # Stale entry-time row (what the 7/6 bug left behind).
    async with get_session() as session:
        session.add(ShadowExitOutcome(
            parent_order_id=747, variant_label="be_ladder_shadow",
            session_date="2026-07-06", entry_spx=7561.0,   # ES-polluted
            direction="LONG_CALLS", qty=3,
            variant_params={"stale": True},
            realized_r=0.0, exit_reason="real_closed_mtm", peak_r=0.0,
        ))

    state = _shadow_state([6.0, 8.0, 12.0])
    p = _fake_parent(id=747)
    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    await _shadow_be_ladder_cycle(state, [p], 7456.0)
    await _settle_tasks()
    await _shadow_be_ladder_cycle(state, [p], 7466.0)
    await _settle_tasks()
    await _shadow_be_ladder_cycle(state, [p], 7491.0)
    await _settle_tasks()
    await _shadow_be_ladder_cycle(state, [], 7491.0)     # close -> recompute

    rows = [r for r in await _outcome_rows() if r.parent_order_id == 747]
    assert len(rows) == 1                                # replaced, not duplicated
    r = rows[0]
    assert r.entry_spx == pytest.approx(7440.0)          # recomputed, not 7561
    assert r.exit_reason == "targets"
    assert r.realized_r == pytest.approx(3.1, abs=1e-6)
    assert "stale" not in r.variant_params
    assert r.variant_params["shadow_dollars"] == pytest.approx(1100.0)


# ---------------------------------------------------------------------------
# 4. Shadow flip — variant conditional on the parent's REAL exit_strategy
# ---------------------------------------------------------------------------


def _fd_state(mids: list[float]) -> SimpleNamespace:
    """Fake state whose option-quote MIDs pop from `mids` in call order."""
    seq = list(mids)

    async def get_option_quote(contract):
        m = seq.pop(0) if seq else None
        return SimpleNamespace(
            bid=(m - 0.1) if m else None, ask=(m + 0.1) if m else None, mid=m,
        )

    return SimpleNamespace(
        ibkr=SimpleNamespace(get_option_quote=get_option_quote),
        shadow_be_ladder={},
        _watcher_banked_cum={},
        runner_protect_enabled=True,
    )


def _spx_points_parent(**kw) -> SimpleNamespace:
    return _fake_parent(exit_strategy="spx_points", **kw)


async def test_variant_selection_is_conditional_on_real_exit_strategy() -> None:
    """fixed_dollar parent -> be_ladder_shadow (unchanged); spx_points parent
    -> fixed_dollar_shadow with the 300/500/1000/-400 cumulative ladder."""
    state = _fd_state([])
    fd_parent = _fake_parent(id=30)                      # fixed_dollar
    sp_parent = _spx_points_parent(id=31)
    await _shadow_be_ladder_cycle(state, [fd_parent, sp_parent], 7440.0)

    assert state.shadow_be_ladder[30]["variant"] == "be_ladder_shadow"
    sh = state.shadow_be_ladder[31]
    assert sh["variant"] == _FD_SHADOW_VARIANT_LABEL == "fixed_dollar_shadow"
    assert sh["d_targets"] == [300.0, 500.0, 1000.0]     # SPX-class: unscaled
    assert sh["d_stop"] == -400.0
    assert sh["fd_semantics"] == "cumulative_realized"


async def test_fd_shadow_etf_scaled_ladder_for_spy_parent() -> None:
    """Non-SPX spx_points parent: the shadow ladder is ETF-scaled exactly
    like the real auto_trader path (SPY @743 -> 30/50/99/-40)."""
    state = _fd_state([])
    state.active_symbol = "SPY"
    p = _spx_points_parent(
        id=32, symbol="SPY", strike=744.0, entry_spx_price=743.0,
    )
    await _shadow_be_ladder_cycle(state, [p], 743.0)
    sh = state.shadow_be_ladder[32]
    assert sh["variant"] == "fixed_dollar_shadow"
    assert sh["d_targets"] == [30.0, 50.0, 99.0]
    assert sh["d_stop"] == -40.0


async def test_fd_shadow_cumulative_target_run_hand_math() -> None:
    """Hand-computed cumulative run (entry prem 5.0, 1/1/1 split): mid 6.0
    fires nothing (gain $100 < $300 incr); mid 8.5 fires T1+T2 ($350 clears
    300 and 200); mid 10.5 fires T3 ($550 >= 500 incr). Shadow $ =
    350+350+550 = +$1,250; R = 1250/400 = 3.125."""
    state = _fd_state([])
    p = _spx_points_parent(id=33)
    await _shadow_be_ladder_cycle(state, [p], 7440.0)    # init
    sh = state.shadow_be_ladder[33]
    assert sh["tier_qtys"] == [1, 1, 1]

    _shadow_fd_step(state, sh, 6.0)
    assert sh["tier_fired"] == [False, False, False]
    _shadow_fd_step(state, sh, 8.5)
    assert sh["tier_fired"] == [True, True, False]
    _shadow_fd_step(state, sh, 10.5)
    assert sh["done"] and sh["exit_reason"] == "targets"
    assert [(f["tier"], f["premium"]) for f in sh["fills"]] == [
        ("T1", 8.5), ("T2", 8.5), ("T3", 10.5),
    ]

    await _shadow_be_ladder_cycle(state, [], 7460.0)     # real close -> finalize
    rows = await _outcome_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r.variant_label == "fixed_dollar_shadow"
    assert r.status == "ok"
    assert r.exit_reason == "targets"
    assert r.variant_params["shadow_dollars"] == pytest.approx(1250.0)
    assert r.realized_r == pytest.approx(1250.0 / 400.0, abs=1e-4)
    assert r.variant_params["d_targets"] == [300.0, 500.0, 1000.0]
    assert r.variant_params["semantics"] == "cumulative_realized"
    audits = await SignalIterRepository.query_audit_log(category="shadow_exit")
    assert "SHADOW fixed-$ ladder would have made +$1,250" in audits[0].summary
    # ZERO real ordering from the shadow path.
    assert await SignalIterRepository.query_audit_log(category="order") == []
    assert await SignalIterRepository.query_audit_log(category="spx_watcher") == []


async def test_fd_shadow_dollar_stop_hand_math() -> None:
    """Remaining 3ct at mid 3.5 = -$450 open <= -$400 stop -> sell all at
    the trigger mid; R = -450/400 = -1.125."""
    state = _fd_state([])
    p = _spx_points_parent(id=34)
    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    sh = state.shadow_be_ladder[34]

    _shadow_fd_step(state, sh, 3.5)
    assert sh["done"] and sh["exit_reason"] == "dollar_stop"
    assert [(f["tier"], f["qty"], f["premium"]) for f in sh["fills"]] == [
        ("STOP", 3, 3.5),
    ]
    await _shadow_be_ladder_cycle(state, [], 7420.0)
    r = (await _outcome_rows())[0]
    assert r.variant_params["shadow_dollars"] == pytest.approx(-450.0)
    assert r.realized_r == pytest.approx(-1.125, abs=1e-4)


async def test_fd_shadow_runner_protect_mirrors_live_semantics() -> None:
    """After T1/T2 bank, a retrace to entry (mid <= entry premium) exits the
    runner at breakeven-ish — the same runner-protect the real fixed_dollar
    checker applies. $ = 350 + 350 - 10 = +$690."""
    state = _fd_state([])
    p = _spx_points_parent(id=35)
    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    sh = state.shadow_be_ladder[35]

    _shadow_fd_step(state, sh, 8.5)                      # T1 + T2
    _shadow_fd_step(state, sh, 4.9)                      # retrace below entry
    assert sh["done"] and sh["exit_reason"] == "runner_protect"
    assert sh["fills"][-1]["tier"] == "RUNNER_PROTECT"
    await _shadow_be_ladder_cycle(state, [], 7442.0)
    r = (await _outcome_rows())[0]
    assert r.variant_params["shadow_dollars"] == pytest.approx(690.0)


async def test_fd_shadow_cycle_samples_quotes_and_finalizes() -> None:
    """End-to-end through the cycle's throttled quote sampler: quote mids
    drive the ladder without any direct _shadow_fd_step calls, and the real
    close settles the remainder MTM at the last sampled mid."""
    state = _fd_state([8.5])
    p = _spx_points_parent(id=36)
    await _shadow_be_ladder_cycle(state, [p], 7440.0)    # init
    sh = state.shadow_be_ladder[36]

    await _shadow_be_ladder_cycle(state, [p], 7450.0)    # spawns quote sample
    await _settle_tasks()
    assert sh["tier_fired"] == [True, True, False]       # T1+T2 at mid 8.5
    assert sh["last_mid"] == pytest.approx(8.5)

    await _shadow_be_ladder_cycle(state, [], 7450.0)     # close with runner open
    await _settle_tasks()
    r = (await _outcome_rows())[0]
    assert r.variant_label == "fixed_dollar_shadow"
    assert r.exit_reason == "real_closed_mtm"
    assert r.variant_params["settled_mtm_at_real_close"] is True
    # T1+T2 at 8.5 (+$700) plus 1ct MTM at the last sampled mid 8.5 (+$350).
    assert r.variant_params["shadow_dollars"] == pytest.approx(1050.0)
    assert r.status == "ok"
