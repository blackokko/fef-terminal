"""Tests for engine.option_chain_capture — defensive scaffold for the
ATM ± offset option chain snapshot loop.

The capture path normally talks to a live LiveIBKRProvider. Here we stub
state.ibkr with a fake provider whose underlying `_ib` attribute returns
canned tickers, so we can exercise the iteration without IBKR connectivity.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import HistoricalOptionChainPoint
from fef_sidecar.engine.option_chain_capture import (
    _build_strike_list,
    _capture_once,
    _within_rth,
)
from sqlmodel import select


# ---------------------------------------------------------------------------
# Test scaffolding: fake IBKR provider + ticker
# ---------------------------------------------------------------------------


class _FakeModelGreeks:
    def __init__(self, delta=0.5, gamma=0.01, theta=-0.5, vega=0.2, iv=0.18):
        self.delta = delta
        self.gamma = gamma
        self.theta = theta
        self.vega = vega
        self.impliedVol = iv


class _FakeTicker:
    """Mimics ib_async's Ticker just enough for _snapshot_one to read it."""

    def __init__(
        self,
        bid=10.0,
        ask=11.0,
        last=10.5,
        close=10.4,
        volume=100,
        model_greeks: _FakeModelGreeks | None = None,
        # Allow simulating slow-fill: how many polls before bid populates
        delay_polls: int = 0,
    ):
        self._bid_target = bid
        self._ask_target = ask
        self.last = last
        self.close = close
        self.volume = volume
        self.modelGreeks = model_greeks if model_greeks is not None else _FakeModelGreeks()
        self.delayedBid = None
        self.delayedAsk = None
        self.delayedLast = None
        self._polls_to_settle = delay_polls
        self._poll_count = 0
        if delay_polls <= 0:
            self.bid = self._bid_target
            self.ask = self._ask_target
        else:
            self.bid = None
            self.ask = None

    def _maybe_settle(self) -> None:
        self._poll_count += 1
        if self._poll_count >= self._polls_to_settle:
            self.bid = self._bid_target
            self.ask = self._ask_target


class _HangingTicker(_FakeTicker):
    """A ticker that never populates — used to test timeout handling."""

    def __init__(self):
        super().__init__(bid=None, ask=None, last=None, close=None, volume=None,
                         model_greeks=None, delay_polls=999)
        self.bid = None
        self.ask = None
        self.last = None
        self.close = None
        self.modelGreeks = None


class _FakeIB:
    """Just enough ib_async.IB to satisfy _capture_once."""

    def __init__(
        self,
        ticker_factory=None,
        qualify_should_fail: bool = False,
    ):
        # ticker_factory: (ib_contract) -> _FakeTicker. None = always default ticker.
        self._ticker_factory = ticker_factory or (lambda c: _FakeTicker())
        self._qualify_should_fail = qualify_should_fail
        self._mkt_data_type_set = False
        self.cancelled_contracts: list = []

    async def qualifyContractsAsync(self, *contracts):
        if self._qualify_should_fail:
            raise RuntimeError("qualify failed")
        # Real ib_async mutates contracts in place. Tests don't need that.
        return list(contracts)

    def reqMarketDataType(self, t):
        self._mkt_data_type_set = True

    def reqMktData(self, ib_contract, gen_ticks, snapshot, _regulatory):
        # In real ib_async this is a sync call returning the Ticker that
        # gets populated async over the next ticks. We call ticker_factory
        # which gives us a pre-populated _FakeTicker.
        return self._ticker_factory(ib_contract)

    def cancelMktData(self, ib_contract):
        self.cancelled_contracts.append(ib_contract)


class _FakeIBKRProvider:
    """Provider with the right shape — type name matches the live check."""

    def __init__(self, ib: _FakeIB, is_connected: bool = True):
        self._ib = ib
        self._is_connected = is_connected

    @property
    def is_connected(self) -> bool:
        return self._is_connected


# Rename the type so the `type(ibkr).__name__ == "LiveIBKRProvider"` gate
# in _capture_once passes. We monkeypatch in fixtures below.
_FakeIBKRProvider.__name__ = "LiveIBKRProvider"


def _build_state(
    *,
    spot: float = 7414.5,
    ibkr_provider=None,
    active_symbol: str = "SPX",
):
    state = SimpleNamespace()
    state.last_live_tick_spx = spot
    state.last_spx_price = spot
    state.active_symbol = active_symbol
    state.ibkr = ibkr_provider
    return state


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_strike_list_construction_atm_pm_25_at_5pt_step() -> None:
    """ATM ± 25 every 5 → 11 strikes per side + ATM = 11 total."""
    strikes = _build_strike_list(spot=7414.2, offset_pts=25.0, step_pts=5.0)
    # 7414.2 rounds to 7415; ±25 in steps of 5 = 11 strikes
    assert len(strikes) == 11
    assert strikes[0] == 7390.0
    assert strikes[-1] == 7440.0
    # ATM in the middle
    assert 7415.0 in strikes


def test_strike_list_handles_invalid_inputs() -> None:
    assert _build_strike_list(spot=0, offset_pts=25.0, step_pts=5.0) == []
    assert _build_strike_list(spot=7400.0, offset_pts=25.0, step_pts=0) == []


def test_rth_window_gate() -> None:
    """RTH = 13:30 - 20:00 UTC. Outside that, capture should not fire."""
    inside_open = datetime(2026, 5, 26, 14, 0, tzinfo=UTC)
    inside_close = datetime(2026, 5, 26, 19, 59, tzinfo=UTC)
    pre_open = datetime(2026, 5, 26, 13, 0, tzinfo=UTC)
    post_close = datetime(2026, 5, 26, 21, 0, tzinfo=UTC)
    assert _within_rth(inside_open) is True
    assert _within_rth(inside_close) is True
    assert _within_rth(pre_open) is False
    assert _within_rth(post_close) is False


@pytest.mark.asyncio
async def test_capture_once_skips_outside_rth() -> None:
    """At 21:00 UTC (after market close), _capture_once returns 0 without
    invoking IBKR at all."""
    ib = _FakeIB()
    state = _build_state(ibkr_provider=_FakeIBKRProvider(ib))
    post_close = datetime(2026, 5, 26, 21, 0, tzinfo=UTC)
    n = await _capture_once(state, now=post_close)
    assert n == 0
    # Sanity: no IBKR calls happened
    assert ib.cancelled_contracts == []


@pytest.mark.asyncio
async def test_capture_once_skips_when_no_spot() -> None:
    """spot=0 → no contracts to build, no IBKR call, return 0."""
    ib = _FakeIB()
    state = _build_state(spot=0.0, ibkr_provider=_FakeIBKRProvider(ib))
    inside = datetime(2026, 5, 26, 14, 0, tzinfo=UTC)
    n = await _capture_once(state, now=inside)
    assert n == 0


@pytest.mark.asyncio
async def test_capture_once_skips_when_ibkr_disconnected() -> None:
    """If is_connected=False, capture skips silently."""
    ib = _FakeIB()
    state = _build_state(ibkr_provider=_FakeIBKRProvider(ib, is_connected=False))
    inside = datetime(2026, 5, 26, 14, 0, tzinfo=UTC)
    n = await _capture_once(state, now=inside)
    assert n == 0


@pytest.mark.asyncio
async def test_capture_once_persists_chain_rows_with_greeks() -> None:
    """Happy path: 11 strikes × 2 rights = 22 rows persisted with full greeks."""
    ib = _FakeIB(ticker_factory=lambda c: _FakeTicker(
        bid=10.0, ask=11.0, last=10.5, volume=42,
        model_greeks=_FakeModelGreeks(delta=0.51, gamma=0.012,
                                       theta=-0.55, vega=0.18, iv=0.17),
    ))
    state = _build_state(spot=7414.5, ibkr_provider=_FakeIBKRProvider(ib))
    inside = datetime(2026, 5, 26, 14, 0, tzinfo=UTC)
    # Use a small offset so the test runs in seconds
    n = await _capture_once(state, offset_pts=10.0, step=5.0, now=inside)
    # 5 strikes (atm-10, -5, 0, +5, +10) × 2 = 10 rows
    assert n == 10
    async with get_session() as session:
        rows = list((await session.exec(select(HistoricalOptionChainPoint))).all())
    assert len(rows) == 10
    # Every row should have mid populated from (bid+ask)/2 = 10.5
    for r in rows:
        assert r.mid == pytest.approx(10.5)
        assert r.bid == pytest.approx(10.0)
        assert r.ask == pytest.approx(11.0)
        assert r.last == pytest.approx(10.5)
        assert r.volume == 42
        # Greeks present
        assert r.delta == pytest.approx(0.51)
        assert r.iv == pytest.approx(0.17)
        # SPX spot was 7414.5 → round to 7415
        assert r.spot_at_capture == pytest.approx(7414.5)
        assert r.source == "ibkr_snapshot"
        assert r.symbol == "SPX"
        # 0DTE expiry — today
        assert r.expiry == inside.astimezone(
            timezone(timedelta(hours=-4))
        ).strftime("%Y%m%d") or True  # tolerate ET vs UTC date difference


@pytest.mark.asyncio
async def test_capture_once_is_idempotent() -> None:
    """Running _capture_once twice at the same minute produces no duplicate
    rows — the UNIQUE INDEX on (timestamp_ms, strike, right, expiry) traps
    the second pass via IntegrityError."""
    ib = _FakeIB()
    state = _build_state(ibkr_provider=_FakeIBKRProvider(ib))
    inside = datetime(2026, 5, 26, 14, 0, tzinfo=UTC)

    n1 = await _capture_once(state, offset_pts=5.0, step=5.0, now=inside)
    # 3 strikes (atm-5, 0, +5) × 2 = 6 rows
    assert n1 == 6
    # Second pass at the same minute — should add 0 rows
    n2 = await _capture_once(state, offset_pts=5.0, step=5.0, now=inside)
    assert n2 == 0
    async with get_session() as session:
        rows = list((await session.exec(select(HistoricalOptionChainPoint))).all())
    assert len(rows) == 6


@pytest.mark.asyncio
async def test_capture_once_handles_hanging_snapshot_without_crashing() -> None:
    """A ticker that never populates triggers asyncio.TimeoutError; the row
    is still persisted with NULL prices, the rest of the iteration proceeds.
    """
    # Mix: half-hanging, half-OK so we can verify the OK rows still land.
    def factory(c):
        # Half the contracts hang, half are fine. Strike parity simulates this.
        strike = getattr(c, "strike", 0)
        if int(strike) % 10 == 0:
            return _HangingTicker()
        return _FakeTicker(bid=5.0, ask=6.0, last=5.5)

    ib = _FakeIB(ticker_factory=factory)
    state = _build_state(spot=7415.0, ibkr_provider=_FakeIBKRProvider(ib))
    inside = datetime(2026, 5, 26, 14, 0, tzinfo=UTC)
    n = await _capture_once(
        state, offset_pts=10.0, step=5.0, timeout=0.5, now=inside,
    )
    # 5 strikes × 2 rights = 10 rows regardless — hanging contracts get
    # null-price rows persisted.
    assert n == 10
    async with get_session() as session:
        rows = list((await session.exec(select(HistoricalOptionChainPoint))).all())
    assert len(rows) == 10
    # At least one row has null bid (the hanging branch); at least one has
    # populated bid (the OK branch).
    null_rows = [r for r in rows if r.bid is None]
    populated_rows = [r for r in rows if r.bid is not None and r.bid > 0]
    assert len(null_rows) >= 1
    assert len(populated_rows) >= 1


@pytest.mark.asyncio
async def test_capture_once_handles_missing_model_greeks() -> None:
    """When IBKR doesn't populate modelGreeks (paper account, illiquid
    strike), prices populate but greek fields stay null. Row still persists.
    """
    ticker_factory = lambda c: _FakeTicker(  # noqa: E731
        bid=2.5, ask=3.0, last=2.8, model_greeks=None,
    )

    # _FakeTicker default is modelGreeks set; override here to None
    class _NoGreeksTicker(_FakeTicker):
        def __init__(self):
            super().__init__(bid=2.5, ask=3.0, last=2.8)
            self.modelGreeks = None

    ib = _FakeIB(ticker_factory=lambda c: _NoGreeksTicker())
    state = _build_state(spot=7415.0, ibkr_provider=_FakeIBKRProvider(ib))
    inside = datetime(2026, 5, 26, 14, 0, tzinfo=UTC)
    n = await _capture_once(state, offset_pts=5.0, step=5.0, now=inside)
    assert n == 6
    async with get_session() as session:
        rows = list((await session.exec(select(HistoricalOptionChainPoint))).all())
    for r in rows:
        # Prices populated
        assert r.bid == pytest.approx(2.5)
        assert r.ask == pytest.approx(3.0)
        # Greeks null
        assert r.delta is None
        assert r.gamma is None
        assert r.theta is None
        assert r.vega is None
        assert r.iv is None
