"""Provider protocol — what any IBKR data source must expose."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from fef_sidecar.ibkr.models import Bar, Contract, Quote


@runtime_checkable
class IBKRProvider(Protocol):
    """Async source of OHLCV bars + last-trade quotes."""

    async def connect(self) -> None:
        """Establish connection. Idempotent."""

    async def disconnect(self) -> None:
        """Tear down. Safe to call multiple times."""

    @property
    def is_connected(self) -> bool: ...

    async def stream_bars(
        self,
        contract: Contract,
        *,
        bar_size: str = "2 mins",
        duration: str = "1 D",
    ) -> AsyncIterator[Bar]:
        """Yield OHLCV bars as they complete.

        Backfills `duration` of history first, then yields live bar updates.
        The generator never returns under normal operation — consumer should
        wrap in a task and cancel on shutdown.
        """
        # Implementations are async generators; protocol stub raises so
        # accidental direct calls surface clearly.
        raise NotImplementedError
        yield  # pragma: no cover  # makes the function a generator

    async def get_last_quote(self, contract: Contract) -> Quote:
        """One-shot last-trade snapshot."""

    async def stream_last_price(
        self,
        contract: Contract,
    ) -> AsyncIterator[Quote]:
        """Yield Quote objects as tick prices update (sub-second cadence when live).

        Used by the tick_stream_loop to feed the top-bar SPX banner without
        waiting for bar closes or the FEF iter cadence.
        """
        raise NotImplementedError
        yield  # pragma: no cover
