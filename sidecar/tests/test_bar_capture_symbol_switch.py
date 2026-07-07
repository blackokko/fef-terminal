"""Bar-capture regression for the 2026-07-07 incident: the ES->SPX chart
switch produced ZERO SPX bars all session (subscription keyed to the
spawn-time symbol; wedged stream had no watchdog) and nothing surfaced it.

Covers: (a) bar_stream_loop follows a mid-session active-symbol switch by
itself — new-symbol bars get persisted with no external respawn; (b) the
capture-health report audits per-symbol row counts and flags a dead active
symbol."""

from __future__ import annotations

import asyncio
import os
import time as _time
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.loop import _bar_capture_health_report, bar_stream_loop


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


class FakeBarProvider:
    """stream_bars is an async generator per the provider protocol; yields a
    completed bar for whatever CONTRACT it was subscribed with, forever."""

    def __init__(self) -> None:
        self.streamed_symbols: list[str] = []
        self._ts = int(_time.time() * 1000)

    async def connect(self) -> None:
        return None

    async def stream_bars(self, contract, *, bar_size="1 min", duration="1 D"):
        self.streamed_symbols.append(contract.symbol)
        base = 7500.0 if contract.symbol == "SPX" else 5000.0
        while True:
            self._ts += 60_000  # unique, increasing bar timestamps
            yield SimpleNamespace(
                epoch_ms=self._ts, open=base, high=base + 1.0,
                low=base - 1.0, close=base, volume=10.0,
            )
            await asyncio.sleep(0.01)


class FakeBroadcaster:
    async def publish(self, event) -> None:
        return None


async def _bar_count(symbol: str) -> int:
    bars = await SignalIterRepository.query_recent_bars(symbol, "1 min", limit=400)
    return len(bars)


async def _has_bars(symbol: str) -> bool:
    return await _bar_count(symbol) > 0


async def _wait_until(cond, timeout: float = 5.0) -> bool:
    """cond: zero-arg callable returning a fresh awaitable per poll."""
    end = _time.monotonic() + timeout
    while _time.monotonic() < end:
        if await cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_bar_capture_follows_mid_session_symbol_switch() -> None:
    prov = FakeBarProvider()
    state = SimpleNamespace(
        active_symbol="ES", ibkr=prov, prior_close_by_symbol={},
        last_spx_price=0.0, broadcaster=FakeBroadcaster(),
    )
    task = asyncio.create_task(bar_stream_loop(state))
    try:
        assert await _wait_until(lambda: _has_bars("ES")), "no ES bars persisted"

        state.active_symbol = "SPX"  # the mid-session chart switch
        assert await _wait_until(lambda: _has_bars("SPX")), (
            "no SPX bars after ES->SPX switch — capture did not follow the symbol"
        )
        # The loop resubscribed itself with the new symbol's contract.
        assert prov.streamed_symbols[0] == "ES"
        assert "SPX" in prov.streamed_symbols
        # Watchdog heartbeat is stamping.
        assert state.loop_heartbeats.get("bar-stream", 0) > 0
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_capture_health_report_flags_dead_active_symbol() -> None:
    now_ms = int(_time.time() * 1000)
    await SignalIterRepository.write_bar(
        symbol="ES", bar_size="1 min", timestamp_ms=now_ms - 60_000,
        open_=5000.0, high=5001.0, low=4999.0, close=5000.0, volume=10.0,
    )
    state = SimpleNamespace(active_symbol="SPX")

    summary = await _bar_capture_health_report(state, tag="test")
    assert "WARNING" in summary                       # active symbol wrote 0 rows
    assert "ES (1 min)=1" in summary

    rows = await SignalIterRepository.query_audit_log(category="capture_health")
    assert len(rows) == 1
    assert rows[0].details["active_rows_last_hour"] == 0

    # Active symbol writing again -> no warning.
    await SignalIterRepository.write_bar(
        symbol="SPX", bar_size="1 min", timestamp_ms=now_ms - 30_000,
        open_=7500.0, high=7501.0, low=7499.0, close=7500.0, volume=10.0,
    )
    summary = await _bar_capture_health_report(state, tag="test2")
    assert "WARNING" not in summary
    assert "SPX (1 min)=1" in summary
