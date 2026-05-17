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


class StrategyPreset(SQLModel, table=True):
    """IMPROVEMENTS #C — named threshold sets the user can swap into the engine.

    Examples: 'tight pin day' (high wall_proximity), 'trend day' (lower
    thresholds, smaller wall_collapse_pct), 'wild Fed day' (everything wider).
    """

    __tablename__ = "strategy_preset"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    description: str = Field(default="")

    fef_loop_interval_sec: int = 120
    wall_proximity_pts: float = 5.0
    wall_collapse_pct: float = 0.20
    wall_collapse_floor_m: float = 5.0
    absorption_fe_ratio_min: float = 5.0

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ChartAnnotation(SQLModel, table=True):
    """IMPROVEMENTS #Q — user-marked horizontal levels persisted to the chart."""

    __tablename__ = "chart_annotation"

    id: int | None = Field(default=None, primary_key=True)
    price: float
    label: str = Field(default="")
    color: str = Field(default="#d29922")  # amber default
    line_style: str = Field(default="dashed")  # solid | dashed | dotted
    created_at: datetime = Field(default_factory=_utcnow)


class Pattern(SQLModel, table=True):
    """IMPROVEMENTS #R — user-defined pattern that fires when conditions match."""

    __tablename__ = "pattern"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    description: str = Field(default="")
    conditions: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    enabled: bool = Field(default=True, index=True)
    audio_alert: bool = Field(default=True)
    toast_alert: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_utcnow)
    last_matched_at: datetime | None = None
    match_count: int = Field(default=0)


class PatternMatch(SQLModel, table=True):
    """One row per pattern firing — link to the iter that caused it."""

    __tablename__ = "pattern_match"

    id: int | None = Field(default=None, primary_key=True)
    pattern_id: int = Field(foreign_key="pattern.id", index=True)
    signal_iter_id: int = Field(foreign_key="signal_iter.id", index=True)
    matched_at: datetime = Field(default_factory=_utcnow)
    fields_snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class DailySession(SQLModel, table=True):
    """Per-session aggregates, updated incrementally during the day."""

    __tablename__ = "daily_session"

    id: int | None = Field(default=None, primary_key=True)
    session_date: str = Field(unique=True, index=True, description="YYYY-MM-DD")
    updated_at: datetime = Field(default_factory=_utcnow)

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


# ---------------------------------------------------------------------------
# Historical persistence — bars, microstructure, flow snapshots, VIX
#
# These tables exist so that ALL data flowing through the sidecar is
# durable across restarts and replayable for backtesting. None of these
# were saved in v1 — the bar/tick/microstructure/snapshot WS broadcasts
# evaporated after delivery. With these tables in place, every iter,
# every bar, every microstructure sample, and every QuantData snapshot
# is recoverable for historical chart review + offline strategy testing.
#
# Storage rough order-of-magnitude per symbol (active trading hours only):
#   - HistoricalBar:        ~390 rows/day/timeframe ⟶ ~14 MB/yr per timeframe
#   - FlowSnapshot:         ~1,600 rows/day        ⟶ ~280 MB/yr (walls JSON heavy)
#   - MicrostructureSample: ~23,400 rows/day (1s)  ⟶ ~1.7 GB/yr
#   - VixSample:            ~780 rows/day (30s)    ⟶ ~22 MB/yr
# ---------------------------------------------------------------------------


class HistoricalBar(SQLModel, table=True):
    """One OHLCV bar persisted for chart history + backtest replay.

    Written by bar_stream_loop every time a new bar closes. UNIQUE
    (symbol, bar_size, timestamp_ms) to prevent duplicates on reconnect.
    """

    __tablename__ = "historical_bar"

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(index=True, description="SPX / ES / QQQ / etc.")
    bar_size: str = Field(default="1 min", description="ib_async bar_size string")
    timestamp_ms: int = Field(index=True, description="Bar start, UTC ms")
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class FlowSnapshot(SQLModel, table=True):
    """One QuantData market snapshot — walls, drift, max pain, aggression, IVR.

    Written by quantdata_poll_loop on every successful fetch (default 15s
    cadence). Lets the user replay any past day's flow evolution without
    re-hitting the QuantData REST API."""

    __tablename__ = "flow_snapshot"

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    session_date: str = Field(index=True, description="YYYY-MM-DD ET")
    timestamp_ms: int = Field(index=True)

    spx_price: float

    top_gex_walls: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    top_dex_walls: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))

    drift_cum_net_m: float = 0.0
    drift_bias: str = "NEUTRAL"
    ntm_max_pain: float = 0.0
    raw_max_pain: float = 0.0
    aggression_call_pct: float | None = None
    aggression_put_pct: float | None = None
    iv_rank_call_pct: float | None = None
    iv_rank_put_pct: float | None = None


class MicrostructureSample(SQLModel, table=True):
    """One microstructure signal sample — OFI / AVI / microprice / direction.

    Written by microstructure_loop at FEF_MICRO_PERSIST_SEC cadence (default
    1s, sampled from the 500ms broadcast). Source of truth for
    backtesting microstructure-driven entries."""

    __tablename__ = "microstructure_sample"

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(index=True, description="The PROXY symbol used (ES/NQ/RTY) — not the active symbol")
    timestamp_ms: int = Field(index=True)

    ofi_window: float
    avi_pct: float
    aggressive_buy_volume: float
    aggressive_sell_volume: float
    microprice: float | None = None
    last_trade_price: float | None = None
    microprice_deviation_bp: float | None = None
    direction: str = "BALANCED"
    confidence: str = "LOW"


class VixSample(SQLModel, table=True):
    """One VIX index reading. Written by vix_poll_loop (default 30s)."""

    __tablename__ = "vix_sample"

    id: int | None = Field(default=None, primary_key=True)
    timestamp_ms: int = Field(index=True)
    value: float
