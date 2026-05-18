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
import time
from collections.abc import AsyncIterator
from datetime import datetime

from fef_sidecar.ibkr.models import AccountSummary, Bar, Contract, ContractKind, OptionQuote, Quote

logger = logging.getLogger(__name__)


def _to_ib_contract(c: Contract):
    """Translate our Contract to ib_async's Contract."""
    from ib_async import ContFuture, Index, Option, Stock

    if c.kind == ContractKind.INDEX:
        return Index(c.symbol, c.exchange, c.currency)
    if c.kind == ContractKind.STOCK:
        return Stock(c.symbol, c.exchange, c.currency)
    if c.kind == ContractKind.FUTURE:
        # Continuous front-month future (IBKR resolves the expiry)
        return ContFuture(c.symbol, c.exchange, c.currency)
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
        self._connect_lock = asyncio.Lock()  # serialize connect() across loops
        # Second connection dedicated to one-shot historical fetches so they
        # don't conflict with the streaming bar_stream_loop's keepUpToDate
        # subscription on the same contract.
        self._ib_hist = None
        self._hist_connect_lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    async def connect(self) -> None:
        # Serialize so the 11 background loops don't all race connectAsync()
        # at startup (which TWS handles poorly and ends up rejecting all of them).
        async with self._connect_lock:
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

    async def _ensure_hist_connected(self):
        """Get/create a dedicated IB connection for one-shot historical fetches.

        Uses client_id + 100 so it doesn't collide with the streaming one.
        Created lazily on first call. Single shared instance for all
        historical requests."""
        async with self._hist_connect_lock:
            if self._ib_hist is not None and self._ib_hist.isConnected():
                return self._ib_hist
            from ib_async import IB
            self._ib_hist = IB()
            await self._ib_hist.connectAsync(self.host, self.port, clientId=self.client_id + 100)
            logger.info("IBKR historical conn ready (client_id=%d)", self.client_id + 100)
            return self._ib_hist

    async def fetch_historical_bars(self, contract: Contract, *, bar_size: str, duration: str,
                                    use_rth: bool = False, what_to_show: str = "TRADES",
                                    timeout_seconds: float = 20.0) -> list[Bar]:
        """One-shot historical bar fetch using the dedicated historical
        connection.

        Hard timeout via asyncio.wait_for so a too-large duration+bar_size
        combo (e.g. ES 4h × 1Y exceeds IBKR's per-request cap) can't wedge
        the connection. On timeout, the historical conn is discarded so the
        next call gets a fresh one — prevents the chart from going dark
        permanently after one bad request."""
        ib = await self._ensure_hist_connected()
        ib_contract = _to_ib_contract(contract)
        try:
            await asyncio.wait_for(
                ib.qualifyContractsAsync(ib_contract), timeout=timeout_seconds,
            )
            bars = await asyncio.wait_for(
                ib.reqHistoricalDataAsync(
                    ib_contract, endDateTime="", durationStr=duration,
                    barSizeSetting=bar_size, whatToShow=what_to_show,
                    useRTH=use_rth, formatDate=2,
                ),
                timeout=timeout_seconds,
            )
            return [_bar_from_ib(b) for b in bars]
        except asyncio.TimeoutError:
            logger.warning(
                "Historical fetch timed out (%s × %s on %s) — resetting hist connection",
                bar_size, duration, contract.symbol,
            )
            # Discard the wedged connection so next call gets a fresh one
            try:
                if self._ib_hist is not None and self._ib_hist.isConnected():
                    self._ib_hist.disconnect()
            except Exception:
                pass
            self._ib_hist = None
            raise TimeoutError(
                f"IBKR historical fetch timed out after {timeout_seconds}s for "
                f"{contract.symbol} {bar_size} × {duration}. "
                f"Try a shorter lookback."
            )

    async def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()
        self._ib = None
        if self._ib_hist is not None and self._ib_hist.isConnected():
            self._ib_hist.disconnect()
        self._ib_hist = None

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

        # TRADES works for everything we care about including indices (SPX
        # returns OHLC with volume=0). MIDPOINT returns 0 bars on SPX,
        # counterintuitively. useRTH=False so futures get overnight bars.
        bars = await self._ib.reqHistoricalDataAsync(
            ib_contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=False,
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

    async def get_prior_close(self, contract: Contract) -> float | None:
        """Fetch yesterday's daily close via reqHistoricalData('2 D', '1 day').

        Returns the close of the most-recent COMPLETED daily bar — i.e. the
        bar whose date is strictly before today. Today's partial bar (if any)
        is filtered out so the % change reflects 'change since prior close',
        not 'change since today's open'.
        """
        from datetime import date as _date
        await self._ensure_connected()
        assert self._ib is not None

        ib_contract = _to_ib_contract(contract)
        await self._ib.qualifyContractsAsync(ib_contract)

        try:
            bars = await asyncio.wait_for(
                self._ib.reqHistoricalDataAsync(
                    ib_contract,
                    endDateTime="",
                    durationStr="3 D",
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=2,
                    keepUpToDate=False,
                ),
                timeout=10.0,
            )
        except (TimeoutError, asyncio.TimeoutError):
            return None

        if not bars:
            return None

        today = _date.today()
        for b in reversed(bars):
            bar_date = b.date if isinstance(b.date, _date) else None
            if bar_date is not None and bar_date < today and float(b.close) > 0:
                return float(b.close)
            if bar_date is None and float(b.close) > 0:
                return float(b.close)
        return float(bars[-1].close) if float(bars[-1].close) > 0 else None

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
        """Fetch option price + greeks via reqMktData (genericTickList='106' for IV).

        Captures IBKR error events so 'no market data subscription' shows up
        as an exception rather than silent NaN — paper accounts without an
        SPX options data subscription get error 354 'Requested market data
        is not subscribed' that we previously swallowed.
        """
        if contract.kind != ContractKind.OPTION:
            raise ValueError(f"Expected OPTION contract, got {contract.kind}")
        await self._ensure_connected()
        assert self._ib is not None

        ib_contract = _to_ib_contract(contract)
        await self._ib.qualifyContractsAsync(ib_contract)

        # Capture per-contract IBKR errors during this reqMktData lifecycle
        captured_errors: list[str] = []

        def _on_error(reqId, errorCode, errorString, *_a):  # type: ignore
            if errorCode in (354, 10089, 10090, 10091, 10092, 10167, 10168, 200):
                captured_errors.append(f"IBKR error {errorCode}: {errorString}")
            elif errorCode < 2000:
                captured_errors.append(f"IBKR error {errorCode}: {errorString}")
        try:
            self._ib.errorEvent += _on_error  # type: ignore[operator]
        except Exception:
            pass

        # No generic tick list for OPT contracts — IBKR rejects "13" (stocks-only)
        # with error 321. modelGreeks (delta/gamma/vega/theta/IV) stream
        # automatically via tickOptionComputation events on the Ticker.
        ticker = self._ib.reqMktData(ib_contract, "", False, False)
        # Wait up to 5s for any of: last/bid/ask/modelGreeks
        for _ in range(50):
            await asyncio.sleep(0.1)
            has_price = (ticker.last and ticker.last > 0) or \
                        (ticker.bid and ticker.bid > 0) or \
                        (ticker.ask and ticker.ask > 0)
            if has_price or ticker.modelGreeks is not None:
                break
        try:
            self._ib.errorEvent -= _on_error  # type: ignore[operator]
        except Exception:
            pass

        # If we got NO data AND captured any IBKR errors, raise — otherwise
        # the silent NaN return makes the caller think the contract was bad.
        if (
            (not ticker.last or ticker.last <= 0 or (isinstance(ticker.last, float) and ticker.last != ticker.last))
            and not (ticker.bid and ticker.bid > 0)
            and not (ticker.ask and ticker.ask > 0)
            and captured_errors
        ):
            try:
                self._ib.cancelMktData(ib_contract)
            except Exception:
                pass
            raise RuntimeError("; ".join(captured_errors))

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

    async def get_account_summary(self) -> AccountSummary | None:
        """Pulls accountSummary + positions via ib_async.

        accountSummary returns ~30 tags per account; we pick the ones that
        matter for a live trading scorecard. P/L tags are auto-updated by
        TWS as positions move + fills happen.
        """
        await self._ensure_connected()
        assert self._ib is not None

        # accountSummaryAsync returns list[AccountValue] for all accounts.
        # Most retail users have one account; we aggregate if multiple.
        try:
            values = await self._ib.accountSummaryAsync()
        except Exception as e:
            logger.warning("accountSummary failed: %s", e)
            return None

        if not values:
            return None

        # Pick the first account's tags (or 'All' if it's the only one)
        account_id = values[0].account if values else "DEFAULT"
        by_tag: dict[str, float] = {}
        for v in values:
            if v.account != account_id and account_id != "All":
                continue
            try:
                by_tag[v.tag] = float(v.value)
            except (ValueError, TypeError):
                pass  # non-numeric tag (currency string, etc.)

        positions = self._ib.positions()
        open_positions = [p for p in positions if abs(p.position) > 0]
        open_options = [
            p for p in open_positions if getattr(p.contract, "secType", "") == "OPT"
        ]

        return AccountSummary(
            account=account_id,
            timestamp_ms=int(time.time() * 1000),
            net_liquidation=by_tag.get("NetLiquidation"),
            buying_power=by_tag.get("BuyingPower"),
            available_funds=by_tag.get("AvailableFunds"),
            daily_pnl=by_tag.get("DailyPnL") or (
                (by_tag.get("RealizedPnL", 0) or 0) + (by_tag.get("UnrealizedPnL", 0) or 0)
            ),
            realized_pnl=by_tag.get("RealizedPnL"),
            unrealized_pnl=by_tag.get("UnrealizedPnL"),
            gross_position_value=by_tag.get("GrossPositionValue"),
            open_positions_count=len(open_positions),
            open_options_count=len(open_options),
            currency="USD",
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

    async def subscribe_tick_by_tick(
        self,
        contract: Contract,
        on_quote,
        on_trade,
    ):
        """Subscribe to tick-by-tick quote + trade events from IBKR.

        Returns an async cancellation handle. Use:
            handle = await provider.subscribe_tick_by_tick(c, on_quote, on_trade)
            try: ...
            finally: await handle()

        on_quote: callable(timestamp_ms: int, bid: float, ask: float,
                           bid_size: float, ask_size: float)
        on_trade: callable(timestamp_ms: int, price: float, size: float,
                           aggressor: 'BUY' | 'SELL' | 'UNKNOWN')

        Uses ib_async's reqTickByTickData with tickType='BidAsk' for quote
        updates and 'AllLast' for every trade print. Trade aggressor is
        classified via Lee-Ready (1991) based on the latest known BBO.

        Note: SPX is an INDEX — it has no trade tape. For microstructure on
        an SPX-related instrument, use SPY (the ETF proxy).
        """
        import time as _time

        await self._ensure_connected()
        assert self._ib is not None

        ib_contract = _to_ib_contract(contract)
        await self._ib.qualifyContractsAsync(ib_contract)

        latest_bid: float | None = None
        latest_ask: float | None = None

        bidask_ticker = self._ib.reqTickByTickData(ib_contract, "BidAsk", 0, False)
        alllast_ticker = self._ib.reqTickByTickData(ib_contract, "AllLast", 0, False)

        def _on_bidask(t) -> None:
            nonlocal latest_bid, latest_ask
            tbb = t.tickByTicks[-1] if t.tickByTicks else None
            if tbb is None:
                return
            bid = float(getattr(tbb, "bidPrice", 0) or 0)
            ask = float(getattr(tbb, "askPrice", 0) or 0)
            bid_size = float(getattr(tbb, "bidSize", 0) or 0)
            ask_size = float(getattr(tbb, "askSize", 0) or 0)
            if bid > 0:
                latest_bid = bid
            if ask > 0:
                latest_ask = ask
            on_quote(int(_time.time() * 1000), bid, ask, bid_size, ask_size)

        def _on_alllast(t) -> None:
            tbb = t.tickByTicks[-1] if t.tickByTicks else None
            if tbb is None:
                return
            price = float(getattr(tbb, "price", 0) or 0)
            size = float(getattr(tbb, "size", 0) or 0)
            if price <= 0 or size <= 0:
                return
            # Lee-Ready classification
            if latest_ask is not None and price >= latest_ask:
                agg = "BUY"
            elif latest_bid is not None and price <= latest_bid:
                agg = "SELL"
            else:
                agg = "UNKNOWN"
            on_trade(int(_time.time() * 1000), price, size, agg)

        bidask_ticker.updateEvent += _on_bidask
        alllast_ticker.updateEvent += _on_alllast

        async def _cancel() -> None:
            bidask_ticker.updateEvent -= _on_bidask
            alllast_ticker.updateEvent -= _on_alllast
            try:
                self._ib.cancelTickByTickData(ib_contract, "BidAsk")
                self._ib.cancelTickByTickData(ib_contract, "AllLast")
            except Exception as e:
                logger.warning("Tick-by-tick cancellation error: %s", e)

        return _cancel

    async def subscribe_depth(self, contract: Contract, on_book):
        """Subscribe to L2 depth-of-book updates via reqMktDepth.

        Requires an IBKR depth-data subscription for the exchange (extra
        per-month cost). Without it, ib_async returns an empty book.

        on_book: callable(timestamp_ms: int, bids: list[BookLevel], asks: list[BookLevel])
        Returns an async cancel handle.
        """
        import time as _time

        from fef_sidecar.engine.depth import BookLevel

        await self._ensure_connected()
        assert self._ib is not None

        ib_contract = _to_ib_contract(contract)
        await self._ib.qualifyContractsAsync(ib_contract)

        # numRows=10, useSmartDepth=True for SMART-routed depth where available
        ticker = self._ib.reqMktDepth(ib_contract, numRows=10, isSmartDepth=True)

        def _on_update(t) -> None:
            bids = [BookLevel(price=float(d.price), size=float(d.size))
                    for d in (t.domBids or [])]
            asks = [BookLevel(price=float(d.price), size=float(d.size))
                    for d in (t.domAsks or [])]
            on_book(int(_time.time() * 1000), bids, asks)

        ticker.updateEvent += _on_update

        async def _cancel() -> None:
            ticker.updateEvent -= _on_update
            try:
                self._ib.cancelMktDepth(ib_contract, isSmartDepth=True)
            except Exception as e:
                logger.warning("Depth cancellation failed: %s", e)

        return _cancel

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
