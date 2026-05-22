"""Typed Pydantic models for QuantData responses.

Units convention:
    - Prices in dollars (float).
    - Exposure values in millions of dollars (float, signed).
    - OI in raw contract counts (int).
    - Premiums/flows in raw dollars (float, signed).
    - Timestamps in UTC milliseconds since epoch (int).
"""

from __future__ import annotations

from datetime import datetime
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
IVDriftBias = Literal["EXPANDING", "CONTRACTING", "NEUTRAL"]


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


class VolDriftEntry(BaseModel):
    """One per-minute IV drift entry — tracks IV evolution intraday."""

    timestamp_ms: int
    call_iv: float
    put_iv: float
    spx_price: float

    @property
    def iv_spread(self) -> float:
        return self.put_iv - self.call_iv


class VolDriftSummary(BaseModel):
    """Cumulative IV drift across the session — like DriftSummary but for IV."""

    entries: list[VolDriftEntry]
    cumulative_iv_change_call: float
    cumulative_iv_change_put: float
    bias: IVDriftBias

    def recent(self, n: int = 5) -> list[VolDriftEntry]:
        return self.entries[-n:] if self.entries else []


class IVSkewRow(BaseModel):
    """Per-strike IV snapshot row — like ExposureRow but values are IV percentages."""

    strike: float
    call_iv: float
    put_iv: float
    skew_pts: float = Field(description="put_iv - call_iv; positive = put skew (normal)")

    @classmethod
    def from_ivs(cls, strike: float, call_iv: float, put_iv: float) -> "IVSkewRow":
        return cls(strike=strike, call_iv=call_iv, put_iv=put_iv, skew_pts=put_iv - call_iv)


class IVSkewSnapshot(BaseModel):
    """IV skew snapshot across strikes — like StrikeExposureSnapshot but for IV."""

    spx_price: float
    rows: list[IVSkewRow]

    def top(self, n: int = 10) -> list[IVSkewRow]:
        """Top N rows by abs(skew_pts) — highest skew differential first."""
        return sorted(self.rows, key=lambda r: abs(r.skew_pts), reverse=True)[:n]


class DarkPoolLevel(BaseModel):
    """One price level of dark pool activity for SPY."""

    price: float
    notional_dollars: float
    trade_count: int


class DarkPoolSnapshot(BaseModel):
    """Dark pool levels snapshot — SPY price levels with summed notional."""

    rows: list[DarkPoolLevel]

    def top(self, n: int = 10) -> list[DarkPoolLevel]:
        """Top N levels by abs(notional_dollars)."""
        return sorted(self.rows, key=lambda r: abs(r.notional_dollars), reverse=True)[:n]


class QuantAlert(BaseModel):
    """One unusual-activity event (Whale/Lightning/Turbo/Bargain/Momentum/Ditto/Golden Sweep)."""

    timestamp_ms: int
    alert_type: str
    contract: str = Field(description="Compact contract description: strike + right + expiry")
    premium_dollars: float
    side: WallSide


class QuantAlertsSnapshot(BaseModel):
    """Recent unusual-activity alerts (last ~30 min)."""

    alerts: list[QuantAlert]

    def recent(self, cutoff_ms: int | None = None, n: int | None = None) -> list[QuantAlert]:
        """Return alerts within the last `cutoff_ms` milliseconds, or most recent n."""
        if cutoff_ms is not None:
            import time as _time
            threshold = int(_time.time() * 1000) - cutoff_ms
            return [a for a in self.alerts if a.timestamp_ms >= threshold]
        if n is not None:
            return self.alerts[-n:] if self.alerts else []
        return list(self.alerts)


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

    # New research-only metrics — added in batch 2 (2026-05-21). Engine does
    # NOT gate on these; they flow into persistent storage for backtest access.
    # vol_drift: IV time-series (tracks IV evolution intraday)
    vol_drift: VolDriftSummary | None = None
    # vol_skew: per-strike IV snapshot (IV percentages per strike)
    vol_skew: IVSkewSnapshot | None = None
    # dark_pool: per-price-level summed notional for SPY dark prints
    dark_pool: DarkPoolSnapshot | None = None
    # quant_alerts: recent unusual-activity events (Whale/Lightning/Turbo/etc.)
    quant_alerts: QuantAlertsSnapshot | None = None

    # Optional sim metadata — only set by the scripted-day mock. Lets the
    # regime detector use simulated time of day for testing transitions.
    simulated_minutes_since_open: int | None = None
    simulated_phase_label: str | None = None
    simulated_vix: float | None = None
