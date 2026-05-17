"""Market data types shared across IBKR providers."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class ContractKind(str, Enum):
    INDEX = "INDEX"
    STOCK = "STOCK"
    OPTION = "OPTION"
    FUTURE = "FUTURE"  # ES, NQ, RTY (front-month continuous)


class Contract(BaseModel):
    """Lightweight contract reference. Maps to ib_async.Contract on the live side."""

    symbol: str
    kind: ContractKind = ContractKind.INDEX
    exchange: str = "CBOE"
    currency: str = "USD"
    # Option-only fields
    expiry: str | None = None  # YYYYMMDD
    strike: float | None = None
    right: str | None = None  # "C" or "P"

    @classmethod
    def spx_index(cls) -> Contract:
        return cls(symbol="SPX", kind=ContractKind.INDEX, exchange="CBOE")

    @classmethod
    def vix_index(cls) -> Contract:
        return cls(symbol="VIX", kind=ContractKind.INDEX, exchange="CBOE")

    @classmethod
    def es_future(cls) -> Contract:
        """ES front-month E-mini S&P 500 (continuous, IBKR resolves expiry)."""
        return cls(symbol="ES", kind=ContractKind.FUTURE, exchange="CME")

    @classmethod
    def qqq_stock(cls) -> Contract:
        """QQQ — Invesco Nasdaq-100 ETF. Heavy 0DTE option volume."""
        return cls(symbol="QQQ", kind=ContractKind.STOCK, exchange="SMART")

    @classmethod
    def iwm_stock(cls) -> Contract:
        """IWM — iShares Russell 2000 ETF. Small-cap exposure."""
        return cls(symbol="IWM", kind=ContractKind.STOCK, exchange="SMART")

    @classmethod
    def spy_stock(cls) -> Contract:
        """SPY — SPDR S&P 500 ETF. SPX proxy, better tick data, has shares."""
        return cls(symbol="SPY", kind=ContractKind.STOCK, exchange="SMART")

    @classmethod
    def for_symbol(cls, symbol: str) -> Contract:
        s = symbol.upper()
        if s == "SPX":
            return cls.spx_index()
        if s == "ES":
            return cls.es_future()
        if s == "VIX":
            return cls.vix_index()
        if s == "QQQ":
            return cls.qqq_stock()
        if s == "IWM":
            return cls.iwm_stock()
        if s == "SPY":
            return cls.spy_stock()
        raise ValueError(f"Unsupported symbol: {symbol}")


class Bar(BaseModel):
    """One OHLCV bar."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def epoch_ms(self) -> int:
        return int(self.timestamp.timestamp() * 1000)


class Quote(BaseModel):
    """Last-trade snapshot."""

    timestamp: datetime
    last: float
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None


class AccountSummary(BaseModel):
    """Live IBKR account snapshot — realized + unrealized P/L, BP, positions.

    Pulled via account_poll_loop every 30s. IBKR is the source of truth — no
    need to manually close tickets in our UI; P/L comes from the broker.
    """

    account: str
    timestamp_ms: int
    net_liquidation: float | None = None
    buying_power: float | None = None
    available_funds: float | None = None
    daily_pnl: float | None = None        # combined daily realized + unrealized
    realized_pnl: float | None = None     # closed-trade gains today
    unrealized_pnl: float | None = None   # mark-to-market on open positions
    gross_position_value: float | None = None
    open_positions_count: int = 0
    open_options_count: int = 0           # count of open option positions specifically
    currency: str = "USD"


class OptionQuote(BaseModel):
    """Option-specific quote with greeks (when available)."""

    timestamp: datetime
    contract: Contract
    last: float
    bid: float | None = None
    ask: float | None = None
    delta: float | None = None
    gamma: float | None = None
    iv: float | None = None
    underlying_price: float | None = None

    @property
    def mid(self) -> float:
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return round((self.bid + self.ask) / 2, 2)
        return self.last
