"""Mock IBKR provider that replays a scripted sequence of bars.

Lets the frontend chart and FEF engine run end-to-end without a live TWS
connection. Default sequence is a synthetic SPX 5/15 morning replay.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from fef_sidecar.ibkr.models import Bar, Contract, Quote


def _default_spx_5_15_bars() -> list[Bar]:
    """Synthetic 2-min bars replicating the 5/15 morning pin → absorption → breakout arc."""
    start = datetime(2026, 5, 15, 9, 30)
    pattern = [
        # (open, high, low, close)
        (7420, 7422, 7418, 7420),
        (7420, 7421, 7416, 7418),
        (7418, 7419, 7414, 7416),
        (7416, 7418, 7410, 7412),  # absorption begins
        (7412, 7414, 7406, 7408),
        (7408, 7410, 7404, 7409),
        (7409, 7413, 7407, 7412),  # bottoming
        (7412, 7416, 7411, 7415),
        (7415, 7420, 7414, 7419),
        (7419, 7424, 7418, 7423),  # breakout
        (7423, 7428, 7421, 7427),
        (7427, 7432, 7425, 7430),
        (7430, 7435, 7428, 7434),
        (7434, 7438, 7432, 7436),
        (7436, 7440, 7434, 7439),
    ]
    bars: list[Bar] = []
    for i, (o, h, low, c) in enumerate(pattern):
        bars.append(
            Bar(
                timestamp=start + timedelta(minutes=2 * i),
                open=float(o),
                high=float(h),
                low=float(low),
                close=float(c),
                volume=1500 + i * 100,
            )
        )
    return bars


class MockIBKRProvider:
    """In-memory bar replayer matching IBKRProvider's interface."""

    def __init__(
        self,
        bars: list[Bar] | None = None,
        *,
        playback_interval: float = 0.0,
    ) -> None:
        """
        Args:
            bars: Scripted bar sequence. Defaults to the 5/15 SPX morning arc.
            playback_interval: Seconds between yielded bars during streaming.
                Set to 0 for tests, >0 for realistic replay in dev.
        """
        self._bars = bars if bars is not None else _default_spx_5_15_bars()
        self._playback_interval = playback_interval
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def stream_bars(
        self,
        contract: Contract,
        *,
        bar_size: str = "2 mins",
        duration: str = "1 D",
    ) -> AsyncIterator[Bar]:
        if not self._connected:
            await self.connect()
        for bar in self._bars:
            yield bar
            if self._playback_interval > 0:
                await asyncio.sleep(self._playback_interval)

    async def get_last_quote(self, contract: Contract) -> Quote:
        if not self._connected:
            await self.connect()
        last_bar = self._bars[-1]
        return Quote(
            timestamp=last_bar.timestamp,
            last=last_bar.close,
            bid=last_bar.close - 0.25,
            ask=last_bar.close + 0.25,
            bid_size=10,
            ask_size=10,
        )
