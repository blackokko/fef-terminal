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
import math
import os
import time
from collections.abc import AsyncIterator
from datetime import datetime

from fef_sidecar.ibkr.quote_stream import (
    QuoteStreamBase,
    quote_stream_qualify_timeout_sec,
)


def _is_nan(x) -> bool:
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return True

from fef_sidecar.ibkr.models import AccountSummary, Bar, Contract, ContractKind, OptionQuote, Quote

logger = logging.getLogger(__name__)


def _detect_account_class(account_id: str) -> str:
    """Classify an IBKR account ID as paper / live / unknown.

    IBKR convention:
      - Paper: leading 'D' (DU... or DUM...) — these are demo / paper accounts.
      - Live: 'U' + digits, or pure-digit account IDs.

    Conservative: anything not clearly paper is treated as 'live' so a
    misclassification fails toward more caution downstream (e.g. research
    scripts default to filtering out 'live' data when not asked).
    Returns 'unknown' only when the account_id is empty.
    """
    if not account_id:
        return "unknown"
    s = account_id.strip().upper()
    if s.startswith("D"):
        return "paper"
    return "live"


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
        # SPX options MUST pin tradingClass="SPXW" (the daily class this bot
        # always trades). Without it, on every monthly-expiration eve the
        # AM-settled SPX monthly shares the same lastTradeDate and the request
        # goes AMBIGUOUS -> qualify fails -> all quotes come back empty
        # (2026-07-16 incident: gate-8 blocked the first theta-era HIGH fire).
        trading_class = "SPXW" if c.symbol == "SPX" else ""
        return Option(c.symbol, c.expiry, c.strike, c.right, c.exchange,
                      currency=c.currency, tradingClass=trading_class)
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


class LiveOptionQuoteStream(QuoteStreamBase):
    """Streaming reqMktData handle for ONE option contract (2026-07-21
    fire-time starvation fix — see ibkr/quote_stream.py for the receipts).

    ib_async pushes ticks into the Ticker in-place; this handle stamps the
    LOCAL monotonic time of every update that carries a usable ask, so
    `fresh_ask()` is a zero-round-trip memory read with an honest tick age.
    Deliberately NOT seeded from the Ticker's residual fields at attach time
    — a previously-cancelled subscription on the same contract leaves values
    of unknown age on the shared Ticker object, and faking those as 'fresh
    now' could let the chase re-post off an old price. Only ticks that
    arrive on THIS subscription count."""

    def __init__(self, ib, ib_contract, ticker) -> None:
        super().__init__()
        self._ib = ib
        self._ib_contract = ib_contract
        self._ticker = ticker
        ticker.updateEvent += self._on_update

    def _on_update(self, ticker, *_args) -> None:
        # Live fields first; delayed fields (market data type 3) as fallback
        # — same acceptance policy as get_option_quote.
        ask = ticker.ask
        if not (isinstance(ask, (int, float)) and ask == ask and ask > 0):
            ask = getattr(ticker, "delayedAsk", None)
        bid = ticker.bid
        if not (isinstance(bid, (int, float)) and bid == bid and bid > 0):
            bid = getattr(ticker, "delayedBid", None)
        self._stamp(bid, ask)

    def _do_close(self) -> None:
        try:
            self._ticker.updateEvent -= self._on_update
        except Exception:
            logger.debug("quote stream: updateEvent detach failed", exc_info=True)
        # cancelMktData is keyed by contract; if another subscription was
        # opened meanwhile this cancels one line — callers keep at most ONE
        # subscription per contract open at a time (see live_trading /
        # spy_mirror wiring) precisely so this stays unambiguous.
        self._ib.cancelMktData(self._ib_contract)


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
        # Populated in connect(). Both surface via .account_id / .account_class
        # properties for callers (state, loop, audit log).
        self._account_id: str = ""
        self._account_class: str = "unknown"

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def account_class(self) -> str:
        return self._account_class

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
            # Detect paper-vs-live account class from the managed account ID.
            # IBKR convention:
            #   - Paper accounts: 'DU' / 'DUM' prefix (e.g. DUM098107, DU1234567)
            #   - Live accounts:  digits only or 'U' + digits (e.g. U1234567)
            # Used downstream to tag signal_iter / live_order so paper and
            # live data never get silently mixed in research output.
            try:
                managed = self._ib.managedAccounts()
                if managed:
                    self._account_id = str(managed[0])
                    self._account_class = _detect_account_class(self._account_id)
                else:
                    self._account_id = ""
                    self._account_class = "unknown"
            except Exception:
                self._account_id = ""
                self._account_class = "unknown"
            logger.info(
                "IBKR connected: host=%s port=%d client_id=%d account=%s class=%s",
                self.host,
                self.port,
                self.client_id,
                self._account_id or "?",
                self._account_class,
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

        # Initial load: enqueue COMPLETED bars only. bars[-1] is the
        # in-progress bar (keepUpToDate) — persisting it now would freeze its
        # partial OHLC in historical_bar (write_bar no-ops on duplicate
        # timestamps, so the row would never get the final values).
        for b in bars[:-1]:
            await queue.put(_bar_from_ib(b))

        def _on_update(bar_list, has_new_bar: bool) -> None:
            # has_new_bar fires when a NEW bar is appended; at that moment
            # bar_list[-1] is the newborn bar (open == first tick) and
            # bar_list[-2] is the just-COMPLETED bar in its final state.
            # Emit the completed one so downstream persistence stores real
            # OHLC. (Previously this emitted bar_list[-1] — every live-
            # captured row was a first-tick snapshot of the bar.)
            if not has_new_bar or len(bar_list) < 2:
                return
            queue.put_nowait(_bar_from_ib(bar_list[-2]))

        bars.updateEvent += _on_update

        try:
            while True:
                yield await queue.get()
        finally:
            bars.updateEvent -= _on_update
            self._ib.cancelHistoricalData(bars)

    async def get_prior_close(self, contract: Contract) -> float | None:
        """Return the prior session's reference close.

        For FUTURES: pulls `ticker.close` from a live reqMktData snapshot.
        That field is the prior daily SETTLEMENT (CME official settle for
        ES/NQ/RTY) — exactly what IBKR's quote panel uses to compute the
        day-change %. The historical daily bar close drifts from this by a
        few ticks because the bar boundary is the 5pm CT new-session start,
        not the 3pm CT settlement.

        For CASH (stocks/indices): also prefer `ticker.close` (yesterday's
        official close), with a reqHistoricalData('2 D', '1 day') fallback
        if the market-data tick hasn't arrived yet.
        """
        from fef_sidecar.ibkr.models import ContractKind
        await self._ensure_connected()
        assert self._ib is not None

        ib_contract = _to_ib_contract(contract)
        await self._ib.qualifyContractsAsync(ib_contract)

        ticker = self._ib.reqMktData(ib_contract, "", False, False)
        try:
            for _ in range(20):  # up to ~2s for the CLOSE tick to arrive
                await asyncio.sleep(0.1)
                close = getattr(ticker, "close", None)
                if close and float(close) > 0 and not _is_nan(close):
                    return float(close)
        finally:
            try:
                self._ib.cancelMktData(ib_contract)
            except Exception:
                pass

        # Fallback: historical daily bar (used to be the primary path).
        is_future = contract.kind == ContractKind.FUTURE
        use_rth = not is_future
        try:
            bars = await asyncio.wait_for(
                self._ib.reqHistoricalDataAsync(
                    ib_contract,
                    endDateTime="",
                    durationStr="3 D",
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=use_rth,
                    formatDate=2,
                    keepUpToDate=False,
                ),
                timeout=10.0,
            )
        except (TimeoutError, asyncio.TimeoutError):
            return None

        if not bars:
            return None
        for b in reversed(bars):
            if float(b.close) > 0:
                return float(b.close)
        return None

    async def get_last_quote(self, contract: Contract) -> Quote:
        await self._ensure_connected()
        assert self._ib is not None

        # Enable delayed market data — required for accounts without
        # live VIX/SPX subscriptions (paper accounts are the main case).
        # Without this, reqMktData on an unsubscribed index returns NaN
        # for last/close and vix_poll_loop silently skips state.last_vix
        # updates (the symptom: TopBar "VIX —" forever after restart).
        # Idempotent: same flag the option-quote path uses.
        if not getattr(self, "_market_data_type_set", False):
            try:
                self._ib.reqMarketDataType(3)  # 3 = delayed (free)
                self._market_data_type_set = True  # type: ignore[attr-defined]
            except Exception:
                pass

        ib_contract = _to_ib_contract(contract)
        await self._ib.qualifyContractsAsync(ib_contract)

        ticker = self._ib.reqMktData(ib_contract, snapshot=True)
        # Give it a moment to populate
        await asyncio.sleep(1.5)
        self._ib.cancelMktData(ib_contract)

        # Multi-level fallback on `last`. After-hours or for indices like
        # VIX where ticker.last can be NaN, fall back to close, then to
        # bid/ask mid. NaN check via the `x == x` idiom (NaN != NaN).
        def _good(v) -> bool:
            try:
                return v is not None and v == v and float(v) > 0
            except (TypeError, ValueError):
                return False
        last_val: float | None = None
        if _good(ticker.last):
            last_val = float(ticker.last)
        elif _good(ticker.close):
            last_val = float(ticker.close)
        elif _good(ticker.bid) and _good(ticker.ask):
            last_val = (float(ticker.bid) + float(ticker.ask)) / 2

        return Quote(
            timestamp=datetime.now(),
            last=float(last_val) if last_val is not None else 0.0,
            bid=float(ticker.bid) if _good(ticker.bid) else None,
            ask=float(ticker.ask) if _good(ticker.ask) else None,
            bid_size=float(ticker.bidSize) if _good(ticker.bidSize) else None,
            ask_size=float(ticker.askSize) if _good(ticker.askSize) else None,
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

        # Enable delayed market data globally — required for paper accounts
        # without OPRA/Cboe live subscription. Returns 15-min delayed
        # bid/ask/last (and live greeks computed from IV). One-shot per
        # connection; subsequent calls are no-ops.
        if not getattr(self, "_market_data_type_set", False):
            try:
                self._ib.reqMarketDataType(3)  # 3 = delayed
                self._market_data_type_set = True  # type: ignore[attr-defined]
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

        # Also accept delayed-data fields when live ticks aren't subscribed.
        # tickType 68 = delayedLast, 66 = delayedBid, 67 = delayedAsk.
        def _pick(*candidates: Any) -> float | None:
            for c in candidates:
                try:
                    f = float(c) if c is not None else None
                except (TypeError, ValueError):
                    continue
                if f is not None and f == f and f > 0:
                    return f
            return None
        last = _pick(ticker.last, ticker.close, getattr(ticker, "delayedLast", None))
        bid = _pick(ticker.bid, getattr(ticker, "delayedBid", None))
        ask = _pick(ticker.ask, getattr(ticker, "delayedAsk", None))
        # Final fallback: model theoretical price (when chain data is missing
        # entirely — paper account, illiquid strike, etc.)
        if last is None and bid is None and ask is None:
            opt_price = (
                ticker.modelGreeks.optPrice
                if ticker.modelGreeks and ticker.modelGreeks.optPrice is not None
                else None
            )
            last = _pick(opt_price)
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
        vega = (
            float(ticker.modelGreeks.vega)
            if ticker.modelGreeks and ticker.modelGreeks.vega is not None
            else None
        )
        theta = (
            float(ticker.modelGreeks.theta)
            if ticker.modelGreeks and ticker.modelGreeks.theta is not None
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
            vega=vega,
            theta=theta,
            iv=iv,
            underlying_price=underlying,
        )

    async def open_option_quote_stream(
        self,
        contract: Contract,
        *,
        ib_contract=None,
        qualify_timeout_sec: float | None = None,
    ) -> LiveOptionQuoteStream:
        """Open a STREAMING (snapshot=False) reqMktData subscription for a
        fire-time option quote and return a memory-read handle (2026-07-21
        starvation fix — see ibkr/quote_stream.py).

        `ib_contract`: an ALREADY-QUALIFIED ib_async contract. When given
        (the entry-chase path — place_bracket qualified it moments earlier),
        this method does ZERO awaited round-trips: reqMktData is a
        synchronous client-side send and the ticks flow push-style. When
        None (the SPY-mirror path), the qualify round-trip is bounded by
        `qualify_timeout_sec` (default FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC).

        Raises immediately when not connected — deliberately NO reconnect
        backoff here (fire-time; the caller is fail-soft and falls back to
        the legacy snapshot re-fetch path). The caller MUST close() the
        returned handle in a finally on every resolution path.
        """
        ib = self._ib
        if ib is None or not ib.isConnected():
            raise ConnectionError(
                "IBKR not connected — cannot open a fire-time quote stream"
            )
        if ib_contract is None:
            t = (quote_stream_qualify_timeout_sec()
                 if qualify_timeout_sec is None else max(0.1, float(qualify_timeout_sec)))
            ib_contract = _to_ib_contract(contract)
            await asyncio.wait_for(ib.qualifyContractsAsync(ib_contract), timeout=t)
        # Same delayed-data enablement as the snapshot path (idempotent).
        if not getattr(self, "_market_data_type_set", False):
            try:
                ib.reqMarketDataType(3)  # 3 = delayed (free)
                self._market_data_type_set = True  # type: ignore[attr-defined]
            except Exception:
                pass
        ticker = ib.reqMktData(ib_contract, "", False, False)  # streaming
        return LiveOptionQuoteStream(ib, ib_contract, ticker)

    async def get_account_summary(self) -> AccountSummary | None:
        """Pulls accountSummary + reqPnL streaming + positions via ib_async.

        accountSummary returns balance/buying-power tags but NOT live P&L —
        IBKR exposes DailyPnL/RealizedPnL/UnrealizedPnL only via reqPnL(),
        which is a streaming subscription. We subscribe once per account
        and read the cached PnL object on each poll.
        """
        await self._ensure_connected()
        assert self._ib is not None

        try:
            values = await self._ib.accountSummaryAsync()
        except Exception as e:
            logger.warning("accountSummary failed: %s", e)
            return None

        if not values:
            return None

        account_id = values[0].account if values else "DEFAULT"
        by_tag: dict[str, float] = {}
        for v in values:
            if v.account != account_id and account_id != "All":
                continue
            try:
                by_tag[v.tag] = float(v.value)
            except (ValueError, TypeError):
                pass

        # Subscribe to reqPnL streaming once per account, then read cached
        # P&L on every poll. Without this, RealizedPnL/UnrealizedPnL are
        # ALWAYS None — accountSummaryAsync doesn't include them.
        if not hasattr(self, "_pnl_subscriptions"):
            self._pnl_subscriptions: dict[str, Any] = {}  # type: ignore[attr-defined]
        if account_id not in self._pnl_subscriptions and account_id != "All":
            try:
                pnl_obj = self._ib.reqPnL(account_id)
                self._pnl_subscriptions[account_id] = pnl_obj
                # Brief wait for TWS to send the first PnL snapshot
                for _ in range(20):
                    await asyncio.sleep(0.1)
                    if pnl_obj.dailyPnL == pnl_obj.dailyPnL:  # not NaN
                        break
            except Exception as e:
                logger.warning("reqPnL subscribe failed for %s: %s", account_id, e)
        pnl = self._pnl_subscriptions.get(account_id)

        def _finite(v: Any) -> float | None:
            try:
                f = float(v)
                return f if f == f else None  # filter NaN
            except (ValueError, TypeError):
                return None

        daily = _finite(pnl.dailyPnL) if pnl else None
        realized = _finite(pnl.realizedPnL) if pnl else None
        unrealized = _finite(pnl.unrealizedPnL) if pnl else None
        if daily is None:
            r = realized or 0.0
            u = unrealized or 0.0
            daily = r + u if (realized is not None or unrealized is not None) else None

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
            daily_pnl=daily,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
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

        Note: SPX is an INDEX — it has no trade tape. The microstructure
        loop maps SPX (and SPY) to ES futures via its MICRO_PROXY table
        (futures lead cash; consolidated CME book) — see loop.py. This
        docstring previously said "use SPY"; stale since the proxy map.
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
        # BOUNDED (2026-07-23 exit-wedge incident): this qualify runs on every
        # (re)subscribe — a hung contract-details farm used to pend it forever,
        # freezing state.last_live_tick_spx at the LAST symbol's final print
        # (an ES 7473.75 served as the "SPX" active tick for 60+s). A timeout
        # raises into the stream loop's except path, which retries/reconnects.
        await asyncio.wait_for(
            self._ib.qualifyContractsAsync(ib_contract),
            timeout=float(os.environ.get("FEF_STREAM_QUALIFY_TIMEOUT_SEC", "10.0")),
        )

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
