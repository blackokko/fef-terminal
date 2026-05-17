"""Typed inputs/outputs for the FEF engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fef_sidecar.db.models import FEFConfidence, FEFDirection
from fef_sidecar.quantdata.models import ExposureRow


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
            "secondary_scores": self.secondary_scores,
            "strike_rec": self.strike_rec,
            "suggested_qty": self.suggested_qty,
            "t1": self.t1,
            "t2": self.t2,
            "t3": self.t3,
            "stop": self.stop,
        }
