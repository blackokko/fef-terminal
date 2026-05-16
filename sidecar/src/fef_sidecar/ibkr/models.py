"""Market data types shared across IBKR providers."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class ContractKind(str, Enum):
    INDEX = "INDEX"
    STOCK = "STOCK"
    OPTION = "OPTION"


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
