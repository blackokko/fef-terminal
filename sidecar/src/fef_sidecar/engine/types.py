"""Typed inputs/outputs for the FEF engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fef_sidecar.db.models import FEFConfidence, FEFDirection
from fef_sidecar.quantdata.models import (
    ExpirationExposureRow,
    ExposureRow,
    OrderFlowRow,
)


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
