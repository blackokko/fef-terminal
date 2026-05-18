"""Provider protocol — what any IBKR data source must expose."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from fef_sidecar.ibkr.models import AccountSummary, Bar, Contract, OptionQuote, Quote


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

    async def get_option_quote(self, contract: Contract) -> OptionQuote:
        """One-shot option price + greeks (delta required for target estimation)."""

    async def ping(self) -> bool:
        """Return True if the connection is alive (lightweight RPC roundtrip).

        Called by the health-check loop every ~30s. Returning False triggers
        a forced disconnect + reconnect attempt.
        """

    async def get_account_summary(self) -> AccountSummary | None:
        """Live account-level P/L + buying power. Polled every ~30s."""

    async def get_prior_close(self, contract: Contract) -> float | None:
        """Prior trading day's closing price for the underlying.

        Used by the Live Quote panel to display day-change %. Cached on
        AppState per symbol; fetched once at startup and on symbol switch.
        Return None if the data isn't available.
        """
