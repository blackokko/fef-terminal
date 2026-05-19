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

    # Provider mode at write time — 'live', 'mock', or 'mixed' (one live + one
    # mock provider). Lets analytics/attribution filter out test/mock rows.
    # NULL on rows created before this column existed.
    provider_mode: str | None = Field(default=None, index=True)


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


class HFTSample(SQLModel, table=True):
    """One HFT signal sample — realized vol + quote stuffing + L2 book.

    Written by hft_loop at FEF_HFT_PERSIST_SEC cadence (default 1s,
    sampled from the 250ms broadcast). Used by ml_patterns + analytical
    queries to correlate HFT state with trade outcomes.
    """

    __tablename__ = "hft_sample"

    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    timestamp_ms: int = Field(index=True)

    short_vol_pts: float = 0.0
    long_vol_pts: float = 0.0
    vol_ratio: float = 1.0
    vol_regime: str = "NORMAL"

    cancel_to_trade_ratio: float = 0.0
    quote_intensity_per_sec: float = 0.0
    stuffing_severity: str = "NORMAL"

    book_imbalance: float = 0.0
    book_imbalance_5lvl: float = 0.0
    book_direction: str = "BALANCED"
    book_confidence: str = "LOW"
    weighted_mid_price: float | None = None
    book_spread: float | None = None

    composite_direction: str = "NEUTRAL"

    vol_source: str = "synth"
    stuffing_source: str = "synth"
    depth_source: str = "synth"


class VixSample(SQLModel, table=True):
    """One VIX index reading. Written by vix_poll_loop (default 30s)."""

    __tablename__ = "vix_sample"

    id: int | None = Field(default=None, primary_key=True)
    timestamp_ms: int = Field(index=True)
    value: float


# ---------------------------------------------------------------------------
# Audit log — every config change, override, kill switch
# ---------------------------------------------------------------------------


class AuditLog(SQLModel, table=True):
    """Append-only log of decisions, config changes, manual overrides.
    Source of truth for 'what did I change Tuesday and why'."""

    __tablename__ = "audit_log"

    id: int | None = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=_utcnow, index=True)
    category: str = Field(index=True, description="config / override / kill_switch / symbol_switch / order")
    actor: str = Field(default="user", description="user / system / sidecar / auto-walk-forward")
    summary: str
    details: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


# ---------------------------------------------------------------------------
# Calendar events — FOMC / CPI / NFP / OPEX
# ---------------------------------------------------------------------------


class CalendarEvent(SQLModel, table=True):
    """Scheduled macro / market events. Used by the engine to auto-pause or
    de-risk around them. Seeded with recurring patterns at startup +
    user-editable via /calendar endpoints."""

    __tablename__ = "calendar_event"

    id: int | None = Field(default=None, primary_key=True)
    event_date: str = Field(index=True, description="YYYY-MM-DD")
    event_time_et: str | None = Field(default=None, description="HH:MM ET, null if all-day")
    category: str = Field(index=True, description="FOMC / CPI / NFP / OPEX / EARNINGS / OTHER")
    title: str
    impact: str = Field(default="HIGH", description="HIGH / MEDIUM / LOW — drives auto-pause behavior")
    auto_pause_minutes_before: int = Field(default=15, description="Minutes prior to event to pause new signals")
    auto_pause_minutes_after: int = Field(default=30)
    notes: str = Field(default="")


# ---------------------------------------------------------------------------
# Live execution — orders + positions
# ---------------------------------------------------------------------------


class LiveOrder(SQLModel, table=True):
    """Order placed via the live trading scaffold. Each row tracks one
    order's lifecycle (submitted → filled → closed)."""

    __tablename__ = "live_order"

    id: int | None = Field(default=None, primary_key=True)
    submitted_at: datetime = Field(default_factory=_utcnow, index=True)
    ticket_id: int | None = Field(default=None, foreign_key="trade_ticket.id", index=True)

    ib_order_id: int | None = Field(default=None, index=True, description="IBKR-assigned order ID")
    ib_perm_id: int | None = Field(default=None, description="IBKR permanent order ID")

    symbol: str
    strike: float
    right: str = Field(description="C or P")
    expiry: str = Field(description="YYYYMMDD")
    action: str = Field(description="BUY / SELL")
    qty: int
    order_type: str = Field(default="LMT")
    limit_price: float | None = None

    # Bracket children — set when parent order has T1/T2/T3/stop attached
    parent_id: int | None = Field(default=None, foreign_key="live_order.id")
    bracket_role: str | None = Field(default=None, description="parent / t1 / t2 / t3 / stop")

    status: str = Field(default="SUBMITTED", description="SUBMITTED / FILLED / PARTIAL / CANCELLED / REJECTED / CLOSED")
    filled_qty: int = 0
    avg_fill_price: float | None = None
    commission: float = 0.0

    last_status_at: datetime = Field(default_factory=_utcnow)
    notes: str = Field(default="")

    # If DRY_RUN, we wrote the row but never submitted to IBKR
    dry_run: bool = Field(default=True)

    # Exit strategy this order was placed with: 'option_price' / 'spx_points' /
    # 'fixed_dollar' / 'spread_premium'. Watchers use this to decide how to
    # fire exits.
    exit_strategy: str | None = Field(default=None)
    # SPX target levels stored on the PARENT order when exit_strategy='spx_points'.
    # NULL for option_price brackets.
    spx_target_t1: float | None = None
    spx_target_t2: float | None = None
    spx_target_t3: float | None = None
    spx_target_stop: float | None = None
    spx_target_t1_qty: int | None = None
    spx_target_t2_qty: int | None = None
    spx_target_t3_qty: int | None = None
    # SPX target watcher state — bumped as each tier fires
    spx_target_t1_fired: bool = Field(default=False)
    spx_target_t2_fired: bool = Field(default=False)
    spx_target_t3_fired: bool = Field(default=False)
    spx_target_t1_at_spx: float | None = None  # spot when T1 fired (for retrace)
    # Spread metadata when exit_strategy='spread_premium'
    spread_kind: str | None = None       # 'PCS' or 'CCS'
    spread_short_strike: float | None = None
    spread_long_strike: float | None = None
    spread_initial_credit: float | None = None
    # Auto-trade dollar-loss cap. When set, spx_target_watcher market-exits
    # remaining qty if the option premium drops enough that the open position
    # is down by this many dollars. None disables the check.
    dollar_loss_cap: float | None = None
    # BE ladder state. dynamic_stop_level starts at spx_target_stop and
    # ratchets to lock profit as MFE crosses each BE_LADDER step.
    dynamic_stop_level: float | None = None
    be_ladder_max_step_R: float = 0.0
    entry_spx_price: float | None = None


class DailyReview(SQLModel, table=True):
    """One row per session — the model's daily report card.

    Computed by engine/daily_review.py either nightly (via the
    auto-walk-forward scheduler) or on-demand via POST /admin/daily_review.
    Lets the user track day-over-day expectancy + win-rate as the engine
    is iterated.
    """

    __tablename__ = "daily_review"

    id: int | None = Field(default=None, primary_key=True)
    session_date: str = Field(unique=True, index=True)  # YYYY-MM-DD
    computed_at: datetime = Field(default_factory=_utcnow)

    total_signals: int = 0
    actionable_signals: int = 0
    wins: int = 0
    stops: int = 0
    timeouts: int = 0
    win_rate_pct: float = 0.0
    expectancy_r: float = 0.0  # R-multiple per signal (negative = losing model)

    # Tier-hit breakdown
    t1_hits: int = 0
    t2_hits: int = 0
    t3_hits: int = 0

    # By-trigger breakdown — JSON: {"confluence": {n,wins,exp}, "aligned_only": {...}, ...}
    by_trigger: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    by_confidence: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    by_hour_ct: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    by_direction: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    by_regime: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    by_wall_distance: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    by_fe_ratio: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    by_block_reason: dict[str, int] = Field(default_factory=dict, sa_column=Column(JSON))

    # Actual TRADES taken (from live_order rows) — distinct from signal-based
    # theoretical outcomes above. real win rate / R can differ wildly when the
    # user only takes a subset of signals.
    actual_trades_taken: int = 0
    actual_wins: int = 0
    actual_losses: int = 0
    actual_total_R: float = 0.0

    # Deduplicated per-setup stats — groups consecutive same-contract fires
    # within DEDUP_WINDOW_MIN minutes into one "setup", scores only the
    # FIRST signal of each group. Closer to real-money P/L expectancy
    # since stacking on the same contract produces correlated outcomes
    # but only one underlying market move to capture.
    dedup_actionable: int = 0
    dedup_wins: int = 0
    dedup_stops: int = 0
    dedup_timeouts: int = 0
    dedup_win_rate_pct: float = 0.0
    dedup_expectancy_r: float = 0.0


class EngineChange(SQLModel, table=True):
    """One row per engine semantic change. Lets the TuningRecommender
    correlate pre/post-change DailyReview rows to measure the impact of
    each change. Backfilled from git history for older changes; auto-
    logged going forward via /admin/engine_change."""

    __tablename__ = "engine_change"

    id: int | None = Field(default=None, primary_key=True)
    applied_at: datetime = Field(default_factory=_utcnow, index=True)
    name: str  # e.g. "wall_collapse_requires_aligned"
    description: str = ""
    category: str = "gate"  # gate / threshold / exit / safety / scoring / data
    expected_impact: str = ""  # human-written hypothesis
    measured_impact_r: float | None = None  # filled in post-hoc
    rollback_condition: str = ""
    git_commit_sha: str | None = None


class ShadowComparison(SQLModel, table=True):
    """One row per iter when shadow mode is active. Records what the
    production engine produced vs what the shadow engine would have
    produced. Used to A/B test engine changes without risking real
    money.
    """

    __tablename__ = "shadow_comparison"

    id: int | None = Field(default=None, primary_key=True)
    iter_timestamp: datetime = Field(default_factory=_utcnow, index=True)
    session_date: str = Field(index=True)
    iter_num: int

    production_direction: FEFDirection
    production_confidence: FEFConfidence
    production_reasoning: str = ""

    shadow_label: str = "default"  # name of the shadow config
    shadow_direction: FEFDirection
    shadow_confidence: FEFConfidence
    shadow_reasoning: str = ""

    # Did they agree? Easy filter for analysis.
    direction_agree: bool = False
    confidence_agree: bool = False


class TuningRecommendation(SQLModel, table=True):
    """Auto-generated recommendation from the rule engine. Reviewed by
    user, then either applied (logged as EngineChange) or dismissed."""

    __tablename__ = "tuning_recommendation"

    id: int | None = Field(default=None, primary_key=True)
    generated_at: datetime = Field(default_factory=_utcnow, index=True)
    based_on_sessions: int = 0  # how many sessions of DailyReview backed it
    based_on_window_start: str = ""  # YYYY-MM-DD
    based_on_window_end: str = ""

    kind: str  # "PROPOSE_GATE" / "TIGHTEN_GATE" / "ROLLBACK_CANDIDATE" / "FREEZE" / "ELIGIBLE_FOR_CHANGE" / "STATE_OF_MODEL"
    priority: str = "MEDIUM"  # HIGH / MEDIUM / LOW
    title: str
    body: str  # markdown-style explanation
    backing_data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    # Lifecycle
    status: str = Field(default="OPEN", index=True)  # OPEN / APPLIED / DISMISSED / SUPERSEDED
    applied_engine_change_id: int | None = None
    decided_at: datetime | None = None
    decided_by: str = ""  # "user" / "system"


class LivePosition(SQLModel, table=True):
    """Snapshot of an open position with live greeks. Refreshed by the
    position monitor loop."""

    __tablename__ = "live_position"

    id: int | None = Field(default=None, primary_key=True)
    snapshot_ts: datetime = Field(default_factory=_utcnow, index=True)

    symbol: str
    strike: float
    right: str
    expiry: str
    qty: int
    avg_cost: float
    market_price: float
    unrealized_pnl: float = 0.0

    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    iv: float | None = None


# ---------------------------------------------------------------------------
# Auto walk-forward results — daily optimal vs current threshold drift
# ---------------------------------------------------------------------------


class WalkForwardRun(SQLModel, table=True):
    """One nightly auto-walk-forward run with optimal thresholds + drift
    from currently-configured engine values."""

    __tablename__ = "walk_forward_run"

    id: int | None = Field(default=None, primary_key=True)
    run_at: datetime = Field(default_factory=_utcnow, index=True)
    window_days: int = 7
    train_test_split: float = 0.6

    # Current config snapshot
    current_wall_proximity_pts: float
    current_wall_collapse_pct: float
    current_wall_collapse_floor_m: float
    current_absorption_fe_ratio_min: float

    # Optimal from sweep
    optimal_wall_proximity_pts: float | None = None
    optimal_wall_collapse_pct: float | None = None
    optimal_wall_collapse_floor_m: float | None = None
    optimal_absorption_fe_ratio_min: float | None = None

    optimal_sharpe: float | None = None
    optimal_total_pnl: float | None = None
    optimal_win_rate: float | None = None

    # Drift magnitude: how far off is current from optimal?
    drift_score: float = Field(default=0.0, description="0=identical, higher=more drift")
    notes: str = Field(default="")


# ---------------------------------------------------------------------------
# ML pattern discovery — feature importance + win-rate model
# ---------------------------------------------------------------------------


class MLModelRun(SQLModel, table=True):
    """One trained ML model run — features, predicted win rate, importance."""

    __tablename__ = "ml_model_run"

    id: int | None = Field(default=None, primary_key=True)
    trained_at: datetime = Field(default_factory=_utcnow, index=True)
    algorithm: str = Field(default="RandomForestClassifier")
    n_training_rows: int
    n_features: int

    train_accuracy: float
    test_accuracy: float
    test_auc: float | None = None

    # Top features by importance (sorted desc): [{name, importance}, ...]
    feature_importances: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    notes: str = Field(default="")
