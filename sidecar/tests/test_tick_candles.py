"""Tick-candle aggregation tests — N-tick grouping, OHLCV math, monotonic
chart times, ring buffer, and the service's status/degradation contract."""

from __future__ import annotations

import time

from fef_sidecar.engine.tick_candles import (
    TickCandleAggregator,
    TickCandleService,
)

_T0 = 1_750_000_000_000


def test_aggregator_groups_exactly_n_ticks() -> None:
    agg = TickCandleAggregator(n_ticks=10)
    for i in range(25):
        agg.add_trade(_T0 + i * 10, 100.0 + i * 0.25, 1.0)
    out = agg.candles()
    # 2 completed candles + 1 forming (5 ticks in)
    assert len(out) == 3
    assert [c["forming"] for c in out] == [False, False, True]
    assert [c["tick_count"] for c in out] == [10, 10, 5]


def test_aggregator_ohlcv_math() -> None:
    agg = TickCandleAggregator(n_ticks=4)
    trades = [(100.0, 2), (101.5, 1), (99.5, 3), (100.5, 4)]
    for i, (p, sz) in enumerate(trades):
        agg.add_trade(_T0 + i * 1000, p, sz)
    out = agg.candles()
    assert len(out) == 1
    c = out[0]
    assert c["open"] == 100.0 and c["close"] == 100.5
    assert c["high"] == 101.5 and c["low"] == 99.5
    assert c["volume"] == 10
    assert c["timestamp_ms"] == _T0  # first trade's time


def test_aggregator_times_strictly_increase_within_bursts() -> None:
    # 30 trades in the SAME millisecond -> 3 candles of 10; chart times must
    # still strictly increase at second resolution.
    agg = TickCandleAggregator(n_ticks=10)
    for _ in range(30):
        agg.add_trade(_T0, 100.0, 1.0)
    out = agg.candles()
    secs = [c["timestamp_ms"] // 1000 for c in out]
    assert len(out) == 3
    assert secs == sorted(set(secs)), f"non-monotonic candle times: {secs}"


def test_aggregator_ring_buffer_caps_history() -> None:
    agg = TickCandleAggregator(n_ticks=1, max_candles=5)
    for i in range(12):
        agg.add_trade(_T0 + i * 1000, 100.0 + i, 1.0)
    out = agg.candles()
    assert len(out) == 5
    assert out[-1]["open"] == 111.0  # newest kept


def test_aggregator_ignores_bad_prices() -> None:
    agg = TickCandleAggregator(n_ticks=2)
    agg.add_trade(_T0, 0.0, 1.0)
    agg.add_trade(_T0, -5.0, 1.0)
    assert agg.candles() == []


def test_service_feeds_all_tick_sizes_and_reports_live() -> None:
    svc = TickCandleService(tick_sizes=(3, 5))
    now = int(time.time() * 1000)
    for i in range(15):
        svc.feed("ES", now - (15 - i) * 100, 7400.0 + i * 0.25, 2.0, source="live")
    assert len(svc.get_candles("ES", 3)) == 5
    assert len(svc.get_candles("ES", 5)) == 3
    st = svc.status("ES")
    assert st["streaming"] is True
    assert st["source"] == "live"
    assert st["trades_seen"] == 15
    assert st["detail"] == "ok"


def test_service_degrades_gracefully_without_feed() -> None:
    svc = TickCandleService()
    assert svc.get_candles("ES", 1000) == []
    st = svc.status("ES")
    assert st["streaming"] is False
    assert st["source"] == "none"
    assert "no tick feed" in st["detail"]


def test_service_flags_wrong_symbol_stream() -> None:
    svc = TickCandleService(tick_sizes=(2,))
    now = int(time.time() * 1000)
    svc.feed("NQ", now, 20_000.0, 1.0, source="live")
    st = svc.status("ES")
    assert st["streaming"] is False
    assert "NQ" in st["detail"]


def test_service_marks_stale_feed() -> None:
    svc = TickCandleService(tick_sizes=(2,))
    old = int(time.time() * 1000) - 10 * 60_000
    svc.feed("ES", old, 7400.0, 1.0, source="live")
    st = svc.status("ES")
    assert st["streaming"] is False
    assert "stale" in st["detail"]
