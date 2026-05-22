"""Typed Pydantic models for QuantData responses.

Units convention:
    - Prices in dollars (float).
    - Exposure values in millions of dollars (float, signed).
    - OI in raw contract counts (int).
    - Premiums/flows in raw dollars (float, signed).
    - Timestamps in UTC milliseconds since epoch (int).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class GreekType(str, Enum):
    GAMMA = "GAMMA"
    DELTA = "DELTA"
    CHARM = "CHARM"
    VANNA = "VANNA"


WallSide = Literal["CALL", "PUT"]
DriftBias = Literal["BULLISH", "BEARISH", "NEUTRAL"]


class ExposureRow(BaseModel):
    """One row in the GEX/DEX/CEX/VEX wall table."""

    strike: float
    call_m: float = Field(description="Call exposure in millions of dollars")
    put_m: float = Field(description="Put exposure in millions of dollars")
    net_m: float = Field(description="Net (call + put) exposure in millions; sign matters")

    @property
    def side(self) -> WallSide:
        return "CALL" if self.net_m > 0 else "PUT"


class StrikeExposureSnapshot(BaseModel):
    """GEX/DEX/CEX/VEX snapshot at a moment in time."""

    greek: GreekType
    spx_price: float
    rows: list[ExposureRow]

    def top(self, n: int = 10) -> list[ExposureRow]:
        return sorted(self.rows, key=lambda r: abs(r.net_m), reverse=True)[:n]


# ---------------------------------------------------------------------------
# Expiration-bucketed exposure (exposure_by_expiration)
# ---------------------------------------------------------------------------


class ExpirationExposureRow(BaseModel):
    """One row in the expiration-bucketed GEX/DEX table.

    Mirrors ExposureRow but keyed by expiration date instead of strike.
    Used to isolate 0DTE exposure from multi-expiry chains.
    """

    expiration: str  # YYYY-MM-DD or YYYYMMDD (normalised by parser)
    call_m: float = Field(description="Call exposure in millions of dollars")
    put_m: float = Field(description="Put exposure in millions of dollars")
    net_m: float = Field(description="Net (call + put) exposure in millions; sign matters")

    @property
    def side(self) -> WallSide:
        return "CALL" if self.net_m > 0 else "PUT"


class ExpirationExposureSnapshot(BaseModel):
    """GEX/DEX snapshot bucketed by expiration rather than strike.

    Lets the engine (and research tooling) isolate how much of the
    aggregate gamma/delta exposure is sitting in the 0DTE expiry vs
    future expirations — critical context for 0DTE scalp sessions.
    """

    greek: GreekType
    spx_price: float
    rows: list[ExpirationExposureRow]

    def top(self, n: int = 10) -> list[ExpirationExposureRow]:
        """Return the top-N rows by absolute net_m."""
        return sorted(self.rows, key=lambda r: abs(r.net_m), reverse=True)[:n]

    def zero_dte(self, today: str | None = None) -> ExpirationExposureRow | None:
        """Return the row for today's 0DTE expiration, or None if absent.

        `today` should be a YYYY-MM-DD string (ET date). Defaults to the
        current ET date. Matches against both YYYY-MM-DD and YYYYMMDD
        formats that QuantData may return.
        """
        if today is None:
            from datetime import timedelta
            et = timezone(timedelta(hours=-4))
            today = datetime.now(et).strftime("%Y-%m-%d")
        today_compact = today.replace("-", "")  # YYYYMMDD
        for row in self.rows:
            exp = row.expiration
            if exp == today or exp == today_compact:
                return row
        return None


# ---------------------------------------------------------------------------
# Consolidated order flow (order_flow)
# ---------------------------------------------------------------------------


class OrderFlowRow(BaseModel):
    """One order from the consolidated order-flow stream.

    Classifications are as reported by QuantData. `side_aggression` is one
    of AA/A/M/B/BB (from most aggressive buyer to most aggressive seller).
    Classification flags (whale/sweep/block) are additive — a golden sweep
    will have both is_sweep=True and is_golden_sweep=True.
    """

    timestamp_ms: int
    strike: float
    right: str                      # "C" or "P"
    expiration: str                 # YYYY-MM-DD or YYYYMMDD
    premium_dollars: float
    side_aggression: str            # AA / A / M / B / BB
    is_whale: bool = False
    is_sweep: bool = False
    is_golden_sweep: bool = False   # >= $1M sweep
    is_block: bool = False
    other_flags: list[str] = Field(default_factory=list)  # Lightning/Turbo/Bargain/Momentum/Ditto …


class OrderFlowSnapshot(BaseModel):
    """Consolidated order-flow snapshot (most recent first).

    Research-only — not persisted to DB as a JSON blob. Used to compute
    summary counts (whale_count_30m, golden_sweep_count_30m) that ARE
    persisted to SignalIter and FlowSnapshot.
    """

    orders: list[OrderFlowRow] = Field(default_factory=list)

    def recent(self, n: int = 50) -> list[OrderFlowRow]:
        """Return the N most-recent orders (list is already most-recent-first)."""
        return self.orders[:n]

    def whales(self) -> list[OrderFlowRow]:
        return [o for o in self.orders if o.is_whale]

    def golden_sweeps(self) -> list[OrderFlowRow]:
        return [o for o in self.orders if o.is_golden_sweep]


# ---------------------------------------------------------------------------
# Per-contract intraday price path (contract_price_time)
# ---------------------------------------------------------------------------


class ContractPricePoint(BaseModel):
    """One (timestamp, price, volume) sample in a contract's intraday path."""

    timestamp_ms: int
    price: float
    volume: int = 0


class ContractPriceTimeSnapshot(BaseModel):
    """Intraday price path for a specific option contract.

    On-demand only — not part of fetch_market_snapshot. Used for post-trade
    review / debugging of specific contracts. Not persisted to DB.
    """

    strike: float
    right: str      # "C" or "P"
    expiration: str
    points: list[ContractPricePoint] = Field(default_factory=list)


class DriftEntry(BaseModel):
    """One per-minute net drift entry."""

    timestamp_ms: int
    call_dollars: float
    put_dollars: float
    spx_price: float

    @property
    def net_dollars(self) -> float:
        return self.call_dollars - self.put_dollars

    @property
    def time_et(self) -> datetime:
        from datetime import timezone, timedelta

        return datetime.fromtimestamp(self.timestamp_ms / 1000, tz=timezone(timedelta(hours=-4)))


class DriftSummary(BaseModel):
    """Cumulative drift across the session + recent slice."""

    entries: list[DriftEntry]
    cumulative_call_m: float
    cumulative_put_m: float
    cumulative_net_m: float
    bias: DriftBias

    def recent(self, n: int = 5) -> list[DriftEntry]:
        return self.entries[-n:] if self.entries else []


class MaxPainSnapshot(BaseModel):
    """Raw chain-wide max pain (use NTMMaxPainResult for signaling — see [[ntm-max-pain]] memory)."""

    max_pain: float
    spx_price: float

    @property
    def distance_pts(self) -> float:
        return self.spx_price - self.max_pain

    @property
    def distance_pct(self) -> float:
        return (self.distance_pts / self.spx_price * 100) if self.spx_price else 0.0

    @property
    def direction(self) -> Literal["above", "below", "at"]:
        if self.distance_pts > 0:
            return "above"
        if self.distance_pts < 0:
            return "below"
        return "at"


class IVRankByContract(BaseModel):
    last_iv: float
    window_min_iv: float
    window_max_iv: float

    @property
    def ivr_pct(self) -> float:
        rng = self.window_max_iv - self.window_min_iv
        if rng <= 0:
            return 0.0
        return (self.last_iv - self.window_min_iv) / rng * 100


class IVRankSnapshot(BaseModel):
    session_date: str
    call: IVRankByContract | None = None
    put: IVRankByContract | None = None


class ContractAggression(BaseModel):
    """One side's aggression breakdown (AA/A/M/B/BB are dollar premium totals)."""

    side: WallSide
    aa: float
    a: float
    mid: float
    b: float
    bb: float

    @property
    def total(self) -> float:
        return self.aa + self.a + self.mid + self.b + self.bb

    @property
    def aggression_pct(self) -> float:
        t = self.total
        return ((self.aa + self.a) / t * 100) if t > 0 else 0.0


class AggressionStats(BaseModel):
    call: ContractAggression
    put: ContractAggression


class OIRow(BaseModel):
    strike: float
    call_oi: int
    put_oi: int

    @property
    def total_oi(self) -> int:
        return self.call_oi + self.put_oi

    @property
    def pc_ratio(self) -> float:
        return (self.put_oi / self.call_oi) if self.call_oi else 0.0


class OpenInterestSnapshot(BaseModel):
    spx_price: float
    rows: list[OIRow]

    def near(self, strike: float, window: float = 50.0) -> list[OIRow]:
        return [r for r in self.rows if abs(r.strike - strike) <= window]


class ContractStatsBySide(BaseModel):
    premium_dollars: float = 0.0
    trade_count: int = 0
    volume: int = 0


class ContractStats(BaseModel):
    call: ContractStatsBySide
    put: ContractStatsBySide


class NetFlowEntry(BaseModel):
    timestamp_ms: int
    call_dollars: float
    put_dollars: float

    @property
    def net_dollars(self) -> float:
        return self.call_dollars - self.put_dollars


class NetFlowSnapshot(BaseModel):
    entries: list[NetFlowEntry]


class MarketSnapshot(BaseModel):
    """Composite snapshot — one call gathers everything the FEF engine needs.

    Built by client.fetch_market_snapshot() via concurrent fetches.
    """

    fetched_at_ms: int
    spx_price: float
    gex: StrikeExposureSnapshot
    dex: StrikeExposureSnapshot
    drift: DriftSummary
    max_pain: MaxPainSnapshot
    aggression: AggressionStats | None = None
    iv_rank: IVRankSnapshot | None = None
    oi: OpenInterestSnapshot | None = None

    # Higher-order Greek exposures — pulled via same endpoint as GEX/DEX with
    # different greekModeType. Used as research-only features during freeze;
    # engine doesn't gate on them yet. cex captures the EOD pin/unpin force
    # that dominates 0DTE; vex captures dealer hedging when IV moves.
    cex: StrikeExposureSnapshot | None = None
    vex: StrikeExposureSnapshot | None = None

    # Expiration-bucketed GEX/DEX (GAMMA + DELTA only — CHARM/VANNA skipped to
    # limit API cost; strike-level CEX/VEX are already captured above).
    # Primarily used to isolate 0DTE net exposure via .zero_dte(). Research-only.
    gex_by_expiry: ExpirationExposureSnapshot | None = None
    dex_by_expiry: ExpirationExposureSnapshot | None = None

    # Consolidated order-flow stream. Populated by get_order_flow() in the poll
    # loop. Not persisted as a blob — only summary counts flow to DB columns.
    order_flow: OrderFlowSnapshot | None = None

    # Optional sim metadata — only set by the scripted-day mock. Lets the
    # regime detector use simulated time of day for testing transitions.
    simulated_minutes_since_open: int | None = None
    simulated_phase_label: str | None = None
    simulated_vix: float | None = None
