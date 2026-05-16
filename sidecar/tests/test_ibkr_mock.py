"""Tests for the mock IBKR provider + provider protocol compliance.

Live provider tests require a running TWS connection and are not run in CI.
"""

from __future__ import annotations

import pytest

from fef_sidecar.ibkr import (
    Bar,
    Contract,
    IBKRProvider,
    LiveIBKRProvider,
    MockIBKRProvider,
)


def test_mock_satisfies_protocol() -> None:
    assert isinstance(MockIBKRProvider(), IBKRProvider)


def test_live_satisfies_protocol() -> None:
    """Live provider should also conform — we just don't actually connect."""
    assert isinstance(LiveIBKRProvider(), IBKRProvider)


@pytest.mark.asyncio
async def test_mock_stream_yields_default_bars() -> None:
    provider = MockIBKRProvider()
    await provider.connect()
    assert provider.is_connected

    bars: list[Bar] = []
    async for bar in provider.stream_bars(Contract.spx_index()):
        bars.append(bar)

    assert len(bars) == 15
    assert bars[0].open == 7420.0
    assert bars[-1].close == 7439.0
    # Verify monotonic timestamps
    for prev, cur in zip(bars[:-1], bars[1:], strict=True):
        assert cur.timestamp > prev.timestamp


@pytest.mark.asyncio
async def test_mock_last_quote_matches_final_bar() -> None:
    provider = MockIBKRProvider()
    quote = await provider.get_last_quote(Contract.spx_index())
    assert quote.last == 7439.0
    assert quote.bid is not None and quote.ask is not None
    assert quote.bid < quote.last < quote.ask


@pytest.mark.asyncio
async def test_mock_custom_bars_replayed_in_order() -> None:
    from datetime import datetime

    custom = [
        Bar(timestamp=datetime(2026, 5, 16, 9, 30), open=100, high=101, low=99, close=100.5),
        Bar(timestamp=datetime(2026, 5, 16, 9, 32), open=100.5, high=102, low=100, close=101.8),
    ]
    provider = MockIBKRProvider(bars=custom)
    bars: list[Bar] = []
    async for bar in provider.stream_bars(Contract.spx_index()):
        bars.append(bar)
    assert bars == custom


def test_spx_index_contract_defaults() -> None:
    c = Contract.spx_index()
    assert c.symbol == "SPX"
    assert c.exchange == "CBOE"
    assert c.kind == "INDEX"
