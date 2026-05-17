"""Live IBKR provider using ib_async.

Requires TWS or IB Gateway running locally with API enabled:
    - Paper trading default port: 7497
    - Live trading default port:  7496
    - In TWS: Edit > Global Configuration > API > Settings
        - Enable ActiveX and Socket Clients
        - Trusted IPs: 127.0.0.1
        - Read-Only API: ON for v1 (no order routing yet — see Task #12)

Reconnect behavior: if the TWS connection drops mid-stream, this provider
attempts to reconnect with exponential backoff up to `max_reconnect_attempts`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime

from fef_sidecar.ibkr.models import Bar, Contract, ContractKind, OptionQuote, Quote

logger = logging.getLogger(__name__)


def _to_ib_contract(c: Contract):
    """Translate our Contract to ib_async's Contract."""
    from ib_async import Index, Option, Stock

    if c.kind == ContractKind.INDEX:
        return Index(c.symbol, c.exchange, c.currency)
    if c.kind == ContractKind.STOCK:
        return Stock(c.symbol, c.exchange, c.currency)
    if c.kind == ContractKind.OPTION:
        if not (c.expiry and c.strike is not None and c.right):
            raise ValueError("Option contract requires expiry, strike, right")
        return Option(c.symbol, c.expiry, c.strike, c.right, c.exchange, currency=c.currency)
    raise ValueError(f"Unsupported contract kind: {c.kind}")


def _bar_from_ib(b) -> Bar:
    """Convert an ib_async BarData into our Bar model."""
    ts = b.date
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace(" ", "T"))
    return Bar(
        timestamp=ts,
        open=float(b.open),
        high=float(b.high),
        low=float(b.low),
        close=float(b.close),
        volume=float(b.volume or 0),
    )


class LiveIBKRProvider:
    """ib_async-backed provider."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 1,
        *,
        max_reconnect_attempts: int = 5,
        reconnect_base_delay: float = 2.0,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self._max_reconnect = max_reconnect_attempts
        self._reconnect_base = reconnect_base_delay
        self._ib = None  # lazy-imported

    @property
    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    async def connect(self) -> None:
        if self.is_connected:
            return
        from ib_async import IB

        if self._ib is None:
            self._ib = IB()
        await self._ib.connectAsync(self.host, self.port, clientId=self.client_id)
        logger.info(
            "IBKR connected: host=%s port=%d client_id=%d",
            self.host,
            self.port,
            self.client_id,
        )

    async def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()
        self._ib = None

    async def _ensure_connected(self) -> None:
        for attempt in range(self._max_reconnect):
            try:
                await self.connect()
                return
            except Exception as e:
                delay = self._reconnect_base * (2**attempt)
                logger.warning(
                    "IBKR connect failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    self._max_reconnect,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
        raise ConnectionError(
            f"Could not connect to IBKR at {self.host}:{self.port} "
            f"after {self._max_reconnect} attempts"
        )

    async def stream_bars(
        self,
        contract: Contract,
        *,
        bar_size: str = "2 mins",
        duration: str = "1 D",
    ) -> AsyncIterator[Bar]:
        await self._ensure_connected()
        assert self._ib is not None

        ib_contract = _to_ib_contract(contract)
        await self._ib.qualifyContractsAsync(ib_contract)

        bars = await self._ib.reqHistoricalDataAsync(
            ib_contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES" if contract.kind == ContractKind.STOCK else "MIDPOINT",
            useRTH=True,
            formatDate=2,
            keepUpToDate=True,
        )

        queue: asyncio.Queue[Bar] = asyncio.Queue()

        for b in bars:
            await queue.put(_bar_from_ib(b))

        def _on_update(bar_list, has_new_bar: bool) -> None:
            if not has_new_bar or not bar_list:
                return
            queue.put_nowait(_bar_from_ib(bar_list[-1]))

        bars.updateEvent += _on_update

        try:
            while True:
                yield await queue.get()
        finally:
            bars.updateEvent -= _on_update
            self._ib.cancelHistoricalData(bars)

    async def get_last_quote(self, contract: Contract) -> Quote:
        await self._ensure_connected()
        assert self._ib is not None

        ib_contract = _to_ib_contract(contract)
        await self._ib.qualifyContractsAsync(ib_contract)

        ticker = self._ib.reqMktData(ib_contract, snapshot=True)
        # Give it a moment to populate
        await asyncio.sleep(1.5)
        self._ib.cancelMktData(ib_contract)

        return Quote(
            timestamp=datetime.now(),
            last=float(ticker.last) if ticker.last else float(ticker.close or 0),
            bid=float(ticker.bid) if ticker.bid else None,
            ask=float(ticker.ask) if ticker.ask else None,
            bid_size=float(ticker.bidSize) if ticker.bidSize else None,
            ask_size=float(ticker.askSize) if ticker.askSize else None,
        )

    async def get_option_quote(self, contract: Contract) -> OptionQuote:
        """Fetch option price + greeks via reqMktData (genericTickList='106' for IV)."""
        if contract.kind != ContractKind.OPTION:
            raise ValueError(f"Expected OPTION contract, got {contract.kind}")
        await self._ensure_connected()
        assert self._ib is not None

        ib_contract = _to_ib_contract(contract)
        await self._ib.qualifyContractsAsync(ib_contract)

        # genericTickList='13' = model option computation (delta/gamma/vega/theta/IV)
        ticker = self._ib.reqMktData(ib_contract, "13", False, False)
        # Wait briefly for the greeks to populate (TWS streams them)
        for _ in range(20):
            await asyncio.sleep(0.1)
            if ticker.modelGreeks is not None or (ticker.last and ticker.last > 0):
                break

        last = float(ticker.last) if ticker.last and ticker.last > 0 else float(
            ticker.close or 0
        )
        bid = float(ticker.bid) if ticker.bid and ticker.bid > 0 else None
        ask = float(ticker.ask) if ticker.ask and ticker.ask > 0 else None
        delta = (
            float(ticker.modelGreeks.delta)
            if ticker.modelGreeks and ticker.modelGreeks.delta is not None
            else None
        )
        gamma = (
            float(ticker.modelGreeks.gamma)
            if ticker.modelGreeks and ticker.modelGreeks.gamma is not None
            else None
        )
        iv = (
            float(ticker.modelGreeks.impliedVol)
            if ticker.modelGreeks and ticker.modelGreeks.impliedVol is not None
            else None
        )
        underlying = (
            float(ticker.modelGreeks.undPrice)
            if ticker.modelGreeks and ticker.modelGreeks.undPrice is not None
            else None
        )

        self._ib.cancelMktData(ib_contract)

        return OptionQuote(
            timestamp=datetime.now(),
            contract=contract,
            last=last,
            bid=bid,
            ask=ask,
            delta=delta,
            gamma=gamma,
            iv=iv,
            underlying_price=underlying,
        )

    async def ping(self) -> bool:
        """Lightweight RPC roundtrip — calls reqCurrentTimeAsync.

        Returns False if not connected OR if the call times out / errors.
        """
        if self._ib is None or not self._ib.isConnected():
            return False
        try:
            await asyncio.wait_for(self._ib.reqCurrentTimeAsync(), timeout=5.0)
            return True
        except Exception as e:
            logger.warning("IBKR ping failed: %s", e)
            return False

    async def stream_last_price(self, contract: Contract):  # AsyncIterator[Quote]
        """Subscribe to reqMktData and yield Quotes on each tick update."""
        await self._ensure_connected()
        assert self._ib is not None

        ib_contract = _to_ib_contract(contract)
        await self._ib.qualifyContractsAsync(ib_contract)

        ticker = self._ib.reqMktData(ib_contract, "", False, False)
        queue: asyncio.Queue[Quote] = asyncio.Queue()

        def _on_update(t) -> None:
            last = t.last if t.last and t.last > 0 else (t.close or 0)
            if not last:
                return
            queue.put_nowait(
                Quote(
                    timestamp=datetime.now(),
                    last=float(last),
                    bid=float(t.bid) if t.bid and t.bid > 0 else None,
                    ask=float(t.ask) if t.ask and t.ask > 0 else None,
                    bid_size=float(t.bidSize) if t.bidSize and t.bidSize > 0 else None,
                    ask_size=float(t.askSize) if t.askSize and t.askSize > 0 else None,
                )
            )

        ticker.updateEvent += _on_update
        try:
            while True:
                yield await queue.get()
        finally:
            ticker.updateEvent -= _on_update
            self._ib.cancelMktData(ib_contract)
