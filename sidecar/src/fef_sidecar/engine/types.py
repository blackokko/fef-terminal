"""Typed inputs/outputs for the FEF engine.

[REMOVED 2026-08-25] QuantData subscription CANCELLED. The exposure /
order-flow / OI value models below used to live in
`fef_sidecar.quantdata.models` and were populated by the QuantData REST
client. They are NOT QuantData-specific — they are plain value objects, and
they still have live producers that have nothing to do with that vendor:

  * `exposure_rows_from_dicts` / `expiration_exposure_rows_from_dicts`
    rebuild them from PERSISTED DB JSON (counterfactual replay, optimization,
    auto_walk_forward).
  * `scripts/research/replay_engine_backfill.py` builds `ExposureRow` /
    `OIRow` from ThetaData option chains.

So they moved here rather than being deleted with the vendor package. There
is no longer any code path that constructs them from a live vendor feed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field as _PField

from fef_sidecar.db.models import FEFConfidence, FEFDirection

WallSide = Literal["CALL", "PUT"]
DriftBias = Literal["BULLISH", "BEARISH", "NEUTRAL"]


class ExposureRow(BaseModel):
    """One row in the GEX/DEX/CEX/VEX wall table."""

    strike: float
    call_m: float = _PField(description="Call exposure in millions of dollars")
    put_m: float = _PField(description="Put exposure in millions of dollars")
    net_m: float = _PField(description="Net (call + put) exposure in millions; sign matters")

    @property
    def side(self) -> WallSide:
        return "CALL" if self.net_m > 0 else "PUT"


class ExpirationExposureRow(BaseModel):
    """One row in the expiration-bucketed GEX/DEX table.

    Mirrors ExposureRow but keyed by expiration date instead of strike.
    Used to isolate 0DTE exposure from multi-expiry chains.
    """

    expiration: str  # YYYY-MM-DD or YYYYMMDD
    call_m: float = _PField(description="Call exposure in millions of dollars")
    put_m: float = _PField(description="Put exposure in millions of dollars")
    net_m: float = _PField(description="Net (call + put) exposure in millions; sign matters")

    @property
    def side(self) -> WallSide:
        return "CALL" if self.net_m > 0 else "PUT"


class OrderFlowRow(BaseModel):
    """One order from the consolidated order-flow stream.

    `side_aggression` is one of AA/A/M/B/BB (from most aggressive buyer to
    most aggressive seller). Classification flags (whale/sweep/block) are
    additive — a golden sweep has both is_sweep=True and is_golden_sweep=True.
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
    other_flags: list[str] = _PField(default_factory=list)


class OIRow(BaseModel):
    """One strike's open interest. Consumed by `engine.max_pain`."""

    strike: float
    call_oi: int
    put_oi: int

    @property
    def total_oi(self) -> int:
        return self.call_oi + self.put_oi

    @property
    def pc_ratio(self) -> float:
        return (self.put_oi / self.call_oi) if self.call_oi else 0.0


def exposure_rows_from_dicts(dicts: list[dict] | None) -> list[ExposureRow]:
    """Convert persisted JSON wall dicts back to ExposureRow models.

    Used by every site that rebuilds FEFInputs from DB rows:
    counterfactual, optimization, auto_walk_forward, api shadow-replay.

    Idempotent — if an element is already an ExposureRow it passes through.
    Non-dict, non-ExposureRow elements are silently skipped.
    """
    out: list[ExposureRow] = []
    for d in (dicts or []):
        if isinstance(d, ExposureRow):
            out.append(d)
            continue
        if not isinstance(d, dict):
            continue
        try:
            out.append(ExposureRow(
                strike=float(d.get("strike", 0)),
                call_m=float(d.get("call_m", 0) or 0),
                put_m=float(d.get("put_m", 0) or 0),
                net_m=float(d.get("net_m", 0) or 0),
            ))
        except Exception:
            pass
    return out


def expiration_exposure_rows_from_dicts(
    dicts: list[dict] | None,
) -> list[ExpirationExposureRow]:
    """Convert persisted JSON expiry dicts back to ExpirationExposureRow models.

    Same pattern as exposure_rows_from_dicts but for expiration-bucketed rows.
    """
    out: list[ExpirationExposureRow] = []
    for d in (dicts or []):
        if isinstance(d, ExpirationExposureRow):
            out.append(d)
            continue
        if not isinstance(d, dict):
            continue
        try:
            out.append(ExpirationExposureRow(
                expiration=str(d.get("expiration", "")),
                call_m=float(d.get("call_m", 0) or 0),
                put_m=float(d.get("put_m", 0) or 0),
                net_m=float(d.get("net_m", 0) or 0),
            ))
        except Exception:
            pass
    return out


@dataclass(frozen=True)
class FEFInputs:
    """Single iter's inputs — derived from a MarketSnapshot."""

    timestamp_ms: int
    spx_price: float
    drift_cum_net_m: float
    drift_bias: str
    top_gex_walls: list[ExposureRow]
    top_dex_walls: list[ExposureRow]
    ntm_max_pain: float
    raw_max_pain: float
    aggression_call_pct: float | None = None
    aggression_put_pct: float | None = None
    iv_rank_call_pct: float | None = None
    iv_rank_put_pct: float | None = None
    # Higher-order Greek walls (CHARM = delta decay drives EOD pin/unpin,
    # VANNA = delta sensitivity to IV moves). Populated when MarketSnapshot
    # has cex/vex; engine doesn't gate on them yet — research-only fields
    # recorded to signal_iter.extras for post-freeze pattern discovery.
    top_cex_walls: list[ExposureRow] = field(default_factory=list)
    top_vex_walls: list[ExposureRow] = field(default_factory=list)
    # Expiration-bucketed GEX/DEX (GAMMA + DELTA only). Top-N rows by
    # abs(net_m); 0DTE net is extracted separately. Research-only.
    top_gex_by_expiry: list[ExpirationExposureRow] = field(default_factory=list)
    top_dex_by_expiry: list[ExpirationExposureRow] = field(default_factory=list)
    zero_dte_gex_net_m: float | None = None
    zero_dte_dex_net_m: float | None = None
    # Order-flow summary (last 20 orders + 30m whale/golden-sweep counts).
    # Engine MUST NOT gate on these — research-only during freeze.
    recent_orders: list[OrderFlowRow] = field(default_factory=list)
    recent_whale_count_30m: int = 0
    recent_golden_sweep_count_30m: int = 0


@dataclass
class FEFVerdict:
    """One iter's FEF output. Always populated, even on WAIT."""

    direction: FEFDirection
    confidence: FEFConfidence
    reasoning: str

    # Trade plan (only set when direction != WAIT)
    strike_rec: float | None = None
    suggested_qty: int | None = None  # sized by confidence
    t1: float | None = None
    t2: float | None = None
    t3: float | None = None
    stop: float | None = None

    # Derived metrics (always populated when enough history exists)
    delta_price: float | None = None
    delta_drift_m: float | None = None
    fe_ratio: float | None = None
    aligned: bool = False
    absorption: bool = False

    # Risk filters (transparent rationale)
    wall_proximity_skip: bool = False
    wall_collapse_trigger: bool = False
    nearest_wall_strike: float | None = None
    nearest_wall_distance: float | None = None
    # Which family the nearest wall came from ('GEX' / 'DEX'). Added with the
    # 2026-07-02 per-family proximity fix (GEX and DEX magnitudes are on
    # incomparable scales, so 'dominant' is ranked within-family only).
    nearest_wall_family: str | None = None

    # Secondary 6-factor scorer (informational)
    secondary_scores: dict[str, float] = field(default_factory=dict)

    def to_log_extras(self) -> dict[str, Any]:
        """Pack the non-primary fields into the SignalIter.extras JSON column."""
        return {
            "delta_price": self.delta_price,
            "delta_drift_m": self.delta_drift_m,
            "fe_ratio": self.fe_ratio,
            "aligned": self.aligned,
            "absorption": self.absorption,
            "wall_proximity_skip": self.wall_proximity_skip,
            "wall_collapse_trigger": self.wall_collapse_trigger,
            "nearest_wall_strike": self.nearest_wall_strike,
            "nearest_wall_distance": self.nearest_wall_distance,
            "nearest_wall_family": self.nearest_wall_family,
            "secondary_scores": self.secondary_scores,
            "strike_rec": self.strike_rec,
            "suggested_qty": self.suggested_qty,
            "t1": self.t1,
            "t2": self.t2,
            "t3": self.t3,
            "stop": self.stop,
        }
