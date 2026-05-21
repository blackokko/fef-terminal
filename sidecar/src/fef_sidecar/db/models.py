"""SQLModel tables for the FEF signal log, trade tickets, and daily sessions.

JSON-shaped fields (walls, drift recent, reasoning context) are stored as TEXT
via sa_column=Column(JSON) — SQLite handles this as TEXT under the hood and
round-trips cleanly through json.dumps/loads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import JSON, Column, UniqueConstraint
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


class SignalLookaheadOutcome(SQLModel, table=True):
    """Per-signal forward observation — what SPX did in the N minutes
    AFTER each SignalIter (fired or WAIT). Closes the "missed signal"
    blind spot: outcomes for WAIT verdicts let us tell whether a WAIT
    correctly skipped chop vs left money on the table.

    Populated by a background worker that walks SignalIter rows whose
    `timestamp + lookahead_minutes` is now in the past, joins forward
    SPX 1-min bars from HistoricalBar, and records MFE/MAE.

    `would_have_hit_t1` and `would_have_hit_stop` apply the SignalIter's
    own t1/stop SPX levels (from extras.option_pricing) for WAIT
    verdicts — they answer "if this WAIT had instead fired with the
    same target/stop the engine COULD have computed, would it have
    won?"
    """

    __tablename__ = "signal_lookahead_outcome"

    id: int | None = Field(default=None, primary_key=True)
    signal_iter_id: int = Field(foreign_key="signal_iter.id", index=True)
    session_date: str = Field(index=True)
    lookahead_minutes: int = Field(index=True, description="5, 15, 30, or 60")
    spx_at_signal: float
    spx_mfe_pts: float = Field(description="max favorable excursion in SPX points (signed by direction)")
    spx_mae_pts: float = Field(description="max adverse excursion in SPX points (signed by direction)")
    spx_close_at_window_end: float
    would_have_hit_t1: bool = False
    would_have_hit_stop: bool = False
    direction_at_signal: str  # "LONG_CALLS" / "LONG_PUTS" / "WAIT"
    confidence_at_signal: str
    computed_at: datetime = Field(default_factory=_utcnow)


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
    spx_target_t2_at_spx: float | None = None  # spot when T2 fired (audit truth)
    spx_target_t3_at_spx: float | None = None  # spot when T3 fired (audit truth)
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

    # Risk-adjusted performance metrics — computed from per-signal R outcomes
    # in daily_review.py. NULL when there's not enough variance (n<2 or
    # all-equal returns) for the statistic to be defined.
    sharpe_ratio: float | None = None   # mean(R) / std(R), annualization left to caller
    sortino_ratio: float | None = None  # mean(R) / std(R | R<0) — downside only
    max_drawdown_R: float | None = None # largest peak-to-trough on cumulative R
    profit_factor: float | None = None  # sum(R | R>0) / abs(sum(R | R<0))
    ulcer_index: float | None = None    # RMS of pct drawdown — penalises long drawdowns more


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


class TournamentRun(SQLModel, table=True):
    """One row per Preset Tournament invocation. Persists the leaderboard
    so you can compare day-over-day winners and track preset performance
    over time instead of every Run being ephemeral.
    """

    __tablename__ = "tournament_run"

    id: int | None = Field(default=None, primary_key=True)
    run_at: datetime = Field(default_factory=_utcnow, index=True)
    session_date_from: str = Field(index=True)
    session_date_to: str = Field(index=True)
    apply_be_ladder: bool = True
    commission_per_contract: float = 0.65
    preset_count: int = 0
    notes: str = Field(default="")
    # Regime distribution during the backtest window — pulled from
    # SignalIter.extras.regime_at_fire across the date range. Format:
    # {"TREND_DAY": 0.42, "CHOP": 0.31, "PIN_DAY": 0.27} (fractions sum to 1).
    # Lets us filter "show me tournaments run on PIN_DAY windows" so preset
    # comparisons get statistically valid faster.
    regime_distribution: dict[str, float] = Field(default_factory=dict, sa_column=Column(JSON))
    dominant_regime: str = Field(default="UNKNOWN", index=True)


class TournamentRunResult(SQLModel, table=True):
    """One row per preset per TournamentRun — the leaderboard rows."""

    __tablename__ = "tournament_run_result"

    id: int | None = Field(default=None, primary_key=True)
    tournament_run_id: int = Field(foreign_key="tournament_run.id", index=True)
    preset_id: int = Field(index=True)
    preset_name: str = ""
    rank: int = 0  # 1-indexed; 0 means not ranked (errored or 0 trades)
    trade_count: int = 0
    iters_scanned: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    expectancy_r: float = 0.0
    sharpe_ratio: float | None = None
    max_drawdown_r: float = 0.0
    profit_factor: float | None = None
    cumulative_r: float = 0.0
    skipped: dict[str, int] = Field(default_factory=dict, sa_column=Column(JSON))
    error: str | None = None


class IBKRExecution(SQLModel, table=True):
    """Persisted IBKR fill log. reqExecutionsAsync only returns ~7 days from
    IBKR; we capture into DB on each /diag/ibkr_executions call so longer
    historical audits remain possible.

    Idempotent: exec_id is unique across all IBKR fills, so upsert-by-exec_id
    means the same fill captured twice produces zero duplicates.
    """

    __tablename__ = "ibkr_execution"

    id: int | None = Field(default=None, primary_key=True)
    exec_id: str = Field(index=True, unique=True)  # IBKR's unique execution ID
    timestamp: datetime = Field(index=True)
    session_date: str = Field(index=True)  # YYYY-MM-DD ET, derived at capture

    symbol: str = Field(index=True)
    strike: float
    right: str  # 'C' or 'P'
    expiry: str  # YYYYMMDD

    side: str  # 'BOT' or 'SLD'
    shares: float
    price: float
    cum_qty: float = 0.0
    avg_price: float = 0.0

    ib_order_id: int | None = Field(default=None, index=True)
    perm_id: int | None = Field(default=None, index=True)
    account: str | None = None

    # Broker-reported commission for this fill (USD). Comes from ib_async's
    # CommissionReport event paired with each Fill. NULL when not yet
    # captured (older rows pre-2026-05-21) or when IBKR hasn't sent the
    # commissionReport yet at write time. $0.00 is a valid value — paper
    # accounts (DUM prefix) typically report $0 commission since they're
    # testing harnesses.
    commission: float | None = None
    realized_pnl: float | None = None  # CommissionReport.realizedPNL when present

    captured_at: datetime = Field(default_factory=_utcnow)


class ShadowExitOutcome(SQLModel, table=True):
    """Per-live-order counterfactual outcome for a named exit-logic
    variant. Multiple rows per parent — one per variant being tested.

    Built 2026-05-21 post the ablation P1 finding that B+C+D was the
    dominant post-freeze exit candidate (CI fragile but headline
    +$6.3k on 5/19-5/21 paper). To validate live we need bar-walk-
    measured outcomes on REAL entries, not just signal counterfactuals.
    Populated by scripts/research/shadow_exit_replay.py post-session —
    no live-engine code is touched.

    Generalized in v1 (rather than hardcoding B+C+D + Baseline) so any
    new exit variant (e.g., tighter trail pullback, alt partial shares,
    timeout-driven exits) can be added by registering it in the variant
    registry without a schema change. variant_params stores the config
    snapshot for full reproducibility.

    Methodology: bar-walk over forward SPX 1-min bars from entry_spx +
    entry_ts. Stop-priority on ambiguous bars (matches the rest of the
    research stack). 1R = 10pt SPX (FEF's standard 0DTE targeting).

    Compare across variants: SELECT variant_label, SUM(realized_r) FROM
    shadow_exit_outcome WHERE session_date BETWEEN ... GROUP BY variant_label.
    """

    __tablename__ = "shadow_exit_outcome"

    id: int | None = Field(default=None, primary_key=True)
    parent_order_id: int = Field(foreign_key="live_order.id", index=True)
    variant_label: str = Field(index=True)
    session_date: str = Field(index=True)
    computed_at: datetime = Field(default_factory=_utcnow)

    entry_spx: float
    direction: str  # 'LONG_CALLS' or 'LONG_PUTS'
    qty: int

    # Variant config snapshot — full reproducibility without joining a
    # separate registry table.
    variant_params: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    # Counterfactual outcome
    realized_r: float = 0.0
    exit_reason: str = ""  # 'stop' / 'targets' / 'trail' / 'timeout' / 'no_data'
    peak_r: float = 0.0
    exit_ms: int | None = None

    # Actual outcome from live_order / IBKR (filled in by a separate
    # backfill pass that pair-offs ibkr_execution rows). NULL when not
    # yet computed or position still open.
    actual_realized_pnl: float | None = None
    actual_realized_r: float | None = None

    __table_args__ = (
        UniqueConstraint("parent_order_id", "variant_label",
                         name="ux_shadow_exit_parent_variant"),
    )


class VariantLedger(SQLModel, table=True):
    """First-class registry of every shadow exit variant tested in any
    analysis (ablation, shadow_replay, sweep, scratchpad).

    Per Claude Chat's recommendation 2026-05-21 (multiple-testing
    correction needs honest N_trials): without this ledger you have
    variants in shadow + variants in old ablation files + variants in
    scratchpads, and DSR/PBO use whatever count is convenient — which
    systematically underestimates the correction. Methodology stats
    must query active variant counts per family from this ledger
    rather than hardcoding N_trials.

    Idempotent upsert by variant_id at every shadow replay run. When
    a variant is decisively retired, set still_active=False (never
    delete — keeps audit history).

    Family taxonomy (per Chat's spec):
      - exit_logic
          - trail_tightness (parameter sweep on trail pullback)
          - be_ladder_combo (BE ladder + various exits)
          - timeout (timeout-window variants)
      - entry_filter (entry-side gates)
      - regime_gate (regime-conditional routing)
    DSR/PBO get applied at sub-family level for parameter sweeps,
    family level for cross-mechanism comparisons, full-project level
    when selecting the final shipping candidate.
    """

    __tablename__ = "variant_ledger"

    id: int | None = Field(default=None, primary_key=True)
    variant_id: str = Field(unique=True, index=True)
    family: str = Field(index=True)
    sub_family: str | None = Field(default=None, index=True)
    description: str = ""
    hypothesis: str = ""
    registered_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: datetime = Field(default_factory=_utcnow)
    tested_in: str = ""   # 'shadow_replay' / 'ablation_backtest' / 'per_regime_sweep'
    sample_window: str = ""
    outcome_summary: str = "pending"  # 'wins' / 'loses' / 'inconclusive' / 'placeholder' / 'pending'
    still_active: bool = True

    variant_params: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


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
    """One trained ML model run — features, CV metrics, persisted estimator path."""

    __tablename__ = "ml_model_run"

    id: int | None = Field(default=None, primary_key=True)
    trained_at: datetime = Field(default_factory=_utcnow, index=True)
    algorithm: str = Field(default="RandomForestRegressor")
    n_training_rows: int
    n_features: int

    # Legacy classifier metrics (kept nullable for backwards-compat with
    # older runs predating the regression switch). New regression runs leave
    # these NULL.
    train_accuracy: float | None = None
    test_accuracy: float | None = None
    test_auc: float | None = None

    # Regression / walk-forward CV metrics (RandomForestRegressor on R-multiple).
    label_kind: str = Field(default="r_multiple")  # "r_multiple" | "binary_win"
    cv_mae: float | None = None      # mean abs error in R units, averaged across TS folds
    cv_r2: float | None = None       # coefficient of determination, averaged across folds
    cv_n_folds: int | None = None    # how many TimeSeriesSplit folds actually ran

    # Path (under FEF_MODEL_DIR or ~/.fef-terminal/models) to a joblib pickle
    # of the fitted estimator. NULL for legacy runs that didn't persist.
    model_path: str | None = None

    # Top features by importance (sorted desc): [{name, importance}, ...]
    feature_importances: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    notes: str = Field(default="")
