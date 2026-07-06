"""N-tick candle aggregation from a trade-print stream.

SPX is an index (no trade tape), so tick candles run on ES futures — the
same tick-by-tick AllLast stream the microstructure loop ALREADY holds
(reqTickByTickData BidAsk+AllLast on the micro proxy, ES when the active
symbol is SPX/SPY). This module adds ZERO IBKR subscriptions: the
microstructure loop's _on_trade callback feeds TickCandleService, which
fans one trade stream into any number of N-tick aggregators.

Why piggyback instead of a dedicated reqTickByTickData('AllLast') call:
IBKR caps simultaneous tick-by-tick subscriptions (default 3 per user —
same formula as market depth; error 10190 when exceeded). The app already
uses 2 (BidAsk + AllLast on the proxy). Piggybacking keeps us at 2 and
guarantees the candles are built from the identical trade prints the
microstructure metrics see.

Degradation: if no trade stream is live for the requested symbol (mock
mode, proxy switched to NQ/RTY, TWS down), get_candles returns [] and
status() explains why — the chart shows "no tick feed".
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TICK_SIZES = (1000, 2000)
MAX_CANDLES = 500

# A feed is considered live if a trade arrived within this window.
STALE_AFTER_MS = 120_000


@dataclass
class _Candle:
    first_ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    tick_count: int

    def to_dict(self, *, forming: bool = False) -> dict[str, Any]:
        return {
            "timestamp_ms": self.first_ts_ms,
            "open": self.open, "high": self.high,
            "low": self.low, "close": self.close,
            "volume": self.volume,
            "tick_count": self.tick_count,
            "forming": forming,
        }


class TickCandleAggregator:
    """Aggregates individual trade prints into N-tick OHLCV candles.

    Candle time = timestamp of its FIRST trade. Keeps the last
    `max_candles` completed candles plus the currently forming one."""

    def __init__(self, n_ticks: int, max_candles: int = MAX_CANDLES) -> None:
        if n_ticks <= 0:
            raise ValueError("n_ticks must be positive")
        self.n_ticks = n_ticks
        self._completed: deque[_Candle] = deque(maxlen=max_candles)
        self._forming: _Candle | None = None

    def add_trade(self, ts_ms: int, price: float, size: float) -> None:
        if price <= 0:
            return
        c = self._forming
        if c is None:
            self._forming = _Candle(
                first_ts_ms=ts_ms, open=price, high=price, low=price,
                close=price, volume=max(size, 0.0), tick_count=1,
            )
        else:
            c.high = max(c.high, price)
            c.low = min(c.low, price)
            c.close = price
            c.volume += max(size, 0.0)
            c.tick_count += 1
        if self._forming is not None and self._forming.tick_count >= self.n_ticks:
            self._completed.append(self._forming)
            self._forming = None

    def candles(self, *, include_forming: bool = True) -> list[dict[str, Any]]:
        """Completed candles (+ the forming one) with STRICTLY increasing
        second-resolution times — lightweight-charts requires ascending
        unique times, and ES bursts can complete multiple 1000-tick candles
        within one wall-clock second."""
        out = [c.to_dict() for c in self._completed]
        if include_forming and self._forming is not None:
            out.append(self._forming.to_dict(forming=True))
        # Enforce strictly-increasing chart times (second resolution)
        prev_sec = None
        for d in out:
            sec = d["timestamp_ms"] // 1000
            if prev_sec is not None and sec <= prev_sec:
                sec = prev_sec + 1
            d["timestamp_ms"] = sec * 1000
            prev_sec = sec
        return out


@dataclass
class TickCandleService:
    """One trade stream in, N aggregators out. Keyed by (symbol, n_ticks).

    Fed from the microstructure loop's trade callback. `source` records
    whether prints are real IBKR ticks or mock-synthesized so the endpoint
    can badge the chart honestly."""

    tick_sizes: tuple[int, ...] = DEFAULT_TICK_SIZES
    max_candles: int = MAX_CANDLES
    _aggs: dict[tuple[str, int], TickCandleAggregator] = field(default_factory=dict)
    _trades_seen: dict[str, int] = field(default_factory=dict)
    _last_trade_ms: dict[str, int] = field(default_factory=dict)
    _source: dict[str, str] = field(default_factory=dict)  # symbol -> live | mock

    def feed(self, symbol: str, ts_ms: int, price: float, size: float,
             *, source: str = "live") -> None:
        sym = symbol.upper()
        for n in self.tick_sizes:
            key = (sym, n)
            agg = self._aggs.get(key)
            if agg is None:
                agg = TickCandleAggregator(n, self.max_candles)
                self._aggs[key] = agg
            agg.add_trade(ts_ms, price, size)
        self._trades_seen[sym] = self._trades_seen.get(sym, 0) + 1
        self._last_trade_ms[sym] = ts_ms
        self._source[sym] = source

    def get_candles(self, symbol: str, n_ticks: int) -> list[dict[str, Any]]:
        agg = self._aggs.get((symbol.upper(), int(n_ticks)))
        return agg.candles() if agg is not None else []

    def status(self, symbol: str) -> dict[str, Any]:
        sym = symbol.upper()
        now_ms = int(time.time() * 1000)
        last = self._last_trade_ms.get(sym, 0)
        streaming = last > 0 and (now_ms - last) < STALE_AFTER_MS
        fed_symbols = sorted({s for s, _ in self._aggs})
        if streaming:
            detail = "ok"
        elif last > 0:
            detail = f"stale — last {sym} trade {(now_ms - last) // 1000}s ago"
        elif fed_symbols:
            detail = f"no {sym} tick feed (streaming: {', '.join(fed_symbols)})"
        else:
            detail = "no tick feed"
        return {
            "symbol": sym,
            "streaming": streaming,
            "source": self._source.get(sym, "none"),
            "trades_seen": self._trades_seen.get(sym, 0),
            "last_trade_ms": last or None,
            "detail": detail,
        }
