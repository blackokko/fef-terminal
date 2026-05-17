"""Mock IBKR provider that replays a scripted sequence of bars.

Lets the frontend chart and FEF engine run end-to-end without a live TWS
connection. Default sequence is a synthetic SPX 5/15 morning replay.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from fef_sidecar.ibkr.models import Bar, Contract, ContractKind, OptionQuote, Quote


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

    async def get_option_quote(self, contract: Contract) -> OptionQuote:
        """Synthetic option quote: intrinsic + flat extrinsic, ATM-ish delta."""
        if contract.kind != ContractKind.OPTION:
            raise ValueError(f"Expected OPTION contract, got {contract.kind}")
        if contract.strike is None or contract.right is None:
            raise ValueError("Option contract requires strike + right")

        if not self._connected:
            await self.connect()

        from datetime import datetime as _dt

        spx = self._bars[-1].close
        strike = contract.strike
        is_call = contract.right == "C"

        intrinsic = max(0.0, (spx - strike) if is_call else (strike - spx))
        # Simple extrinsic model: $8 ATM, decays linearly with moneyness up to $50 OTM
        moneyness = abs(spx - strike)
        extrinsic = max(0.05, 8.0 * max(0.0, 1.0 - moneyness / 50.0))
        price = round(intrinsic + extrinsic, 2)

        # Delta approximation: 0.5 ATM, scales with how OTM/ITM
        if is_call:
            raw_delta = 0.5 + (spx - strike) / 200.0
        else:
            raw_delta = -0.5 + (spx - strike) / 200.0
        delta = round(max(-0.95, min(0.95, raw_delta)), 3)

        return OptionQuote(
            timestamp=_dt.now(),
            contract=contract,
            last=price,
            bid=round(price - 0.25, 2),
            ask=round(price + 0.25, 2),
            delta=delta,
            underlying_price=spx,
        )

    async def ping(self) -> bool:
        return self._connected

    async def stream_last_price(
        self,
        contract: Contract,
    ) -> AsyncIterator[Quote]:
        """Emit synthetic ticks every FEF_TICK_INTERVAL_SEC (default 2s) with
        small random walk around the last bar's close."""
        import os
        import random
        from datetime import datetime

        if not self._connected:
            await self.connect()
        interval = float(os.environ.get("FEF_TICK_INTERVAL_SEC", "2"))
        base_price = self._bars[-1].close
        price = base_price
        while True:
            # Random walk: small step each tick, mean-reverting to base
            step = random.uniform(-0.5, 0.5)
            drift = (base_price - price) * 0.1  # gentle pull back to base
            price = round(price + step + drift, 2)
            yield Quote(
                timestamp=datetime.now(),
                last=price,
                bid=price - 0.25,
                ask=price + 0.25,
                bid_size=10,
                ask_size=10,
            )
            await asyncio.sleep(interval)
