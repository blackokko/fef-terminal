"""SQLModel tables for the FEF signal log, trade tickets, and daily sessions.

JSON-shaped fields (walls, drift recent, reasoning context) are stored as TEXT
via sa_column=Column(JSON) — SQLite handles this as TEXT under the hood and
round-trips cleanly through json.dumps/loads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class FEFDirection(str, Enum):
    LONG_CALLS = "LONG_CALLS"
    LONG_PUTS = "LONG_PUTS"
    WAIT = "WAIT"


class FEFConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


class TicketStatus(str, Enum):
    DRAFT = "DRAFT"
    COPIED = "COPIED"  # user copied the order text to TWS
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"  # some scale-out tiers hit
    CLOSED = "CLOSED"
    STOPPED = "STOPPED"
    SKIPPED = "SKIPPED"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SignalIter(SQLModel, table=True):
    """One row per FEF engine iteration (every 2 min during market hours)."""

    __tablename__ = "signal_iter"

    id: int | None = Field(default=None, primary_key=True)

    session_date: str = Field(index=True, description="YYYY-MM-DD, ET")
    iter_num: int = Field(description="Per-session counter, starts at 1")
    timestamp: datetime = Field(default_factory=_utcnow, index=True)

    spx_price: float

    # FEF verdict
    direction: FEFDirection = Field(default=FEFDirection.WAIT, index=True)
    confidence: FEFConfidence = Field(default=FEFConfidence.NONE)
    reasoning: str = Field(default="")

    # Flow snapshot at this iter
    drift_cum_net_m: float = 0.0
    drift_bias: str = "NEUTRAL"
    ntm_max_pain: float = 0.0
    raw_max_pain: float = 0.0

    # Top walls captured as JSON arrays of {strike, net_m, side}
    top_gex_walls: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    top_dex_walls: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))

    # Optional metrics (nullable)
    aggression_call_pct: float | None = None
    aggression_put_pct: float | None = None
    iv_rank_call_pct: float | None = None
    iv_rank_put_pct: float | None = None

    # Full reasoning context — anything the engine wants to recall later
    extras: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class TradeTicket(SQLModel, table=True):
    """Order ticket generated from an actionable FEF signal.

    v1: tickets are read-only — user copies to TWS manually.
    v2: status will progress through FILLED/PARTIAL/CLOSED via IBKR integration.
    """

    __tablename__ = "trade_ticket"

    id: int | None = Field(default=None, primary_key=True)
    signal_iter_id: int = Field(foreign_key="signal_iter.id", index=True)
    created_at: datetime = Field(default_factory=_utcnow, index=True)

    direction: FEFDirection
    contract_strike: float
    contract_expiry: str = Field(description="YYYY-MM-DD (0DTE)")
    qty: int = Field(default=15, description="Default 15 per [[user-trading-style]]")

    entry_limit: float
    t1: float
    t2: float
    t3: float
    stop: float

    status: TicketStatus = Field(default=TicketStatus.DRAFT, index=True)

    # Outcomes (filled in as the trade progresses)
    filled_at: datetime | None = None
    fill_price: float | None = None
    exit_t1_price: float | None = None
    exit_t2_price: float | None = None
    exit_t3_price: float | None = None
    exit_stop_price: float | None = None
    closed_at: datetime | None = None
    pnl_dollars: float | None = None

    notes: str = Field(default="")


class DailySession(SQLModel, table=True):
    """Per-session aggregates, updated incrementally during the day."""

    __tablename__ = "daily_session"

    id: int | None = Field(default=None, primary_key=True)
    session_date: str = Field(unique=True, index=True, description="YYYY-MM-DD")

    total_iters: int = 0
    actionable_signals: int = 0
    trades_taken: int = 0

    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0

    notes: str = Field(default="")

    updated_at: datetime = Field(default_factory=_utcnow)
