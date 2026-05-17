"""Multi-strategy framework — run N FEFEngine instances in parallel with
different configs. Each strategy's verdict is broadcast separately
(tournament already does this for thresholds; this generalizes to fully
distinct strategy preset configs).

For v1: strategies are configured via STRATEGY_CONFIGS dict (preset name
+ threshold overrides). Each gets its own FEFEngine but shares the same
input snapshot — so they all see the same market state, just interpret
it differently.

Sharpe-weighted capital allocation comes later. v1 just runs them all.
"""

from __future__ import annotations

from dataclasses import dataclass

from fef_sidecar.engine import FEFEngine
from fef_sidecar.engine.tournament import TournamentRunner, TournamentVariant


@dataclass(frozen=True)
class StrategyConfig:
    """One strategy — name + complete threshold set + suggested qty."""

    name: str
    description: str
    wall_proximity_pts: float = 5.0
    wall_collapse_pct: float = 0.20
    wall_collapse_floor_m: float = 5.0
    absorption_fe_ratio_min: float = 5.0
    threshold_scale: float = 1.0
    base_qty: int = 15


DEFAULT_STRATEGIES = [
    StrategyConfig(
        name="Core Bull-Drift",
        description="Looks for clean bull drift with absorption + tight walls",
        wall_proximity_pts=4.0, wall_collapse_pct=0.15, base_qty=15,
    ),
    StrategyConfig(
        name="Wall-Test Fade",
        description="Fades tests of dominant walls — looks for absorption + reversal",
        wall_proximity_pts=8.0, wall_collapse_pct=0.30, absorption_fe_ratio_min=8.0,
        base_qty=10,
    ),
    StrategyConfig(
        name="Trend-Day Aggressive",
        description="Loose thresholds for clear trend days, larger size",
        wall_proximity_pts=2.0, wall_collapse_pct=0.10, base_qty=20,
    ),
    StrategyConfig(
        name="Chop-Day Survivor",
        description="Very tight thresholds — fires rarely, small size",
        wall_proximity_pts=10.0, wall_collapse_pct=0.40, absorption_fe_ratio_min=10.0,
        base_qty=5,
    ),
]


def build_strategy_engines(configs: list[StrategyConfig] | None = None) -> list[tuple[str, FEFEngine, int]]:
    """Create one FEFEngine per strategy. Returns (name, engine, base_qty) tuples."""
    cfgs = configs or DEFAULT_STRATEGIES
    return [
        (
            c.name,
            FEFEngine(
                wall_proximity_pts=c.wall_proximity_pts,
                wall_collapse_pct=c.wall_collapse_pct,
                wall_collapse_floor_m=c.wall_collapse_floor_m,
                absorption_fe_ratio_min=c.absorption_fe_ratio_min,
                threshold_scale=c.threshold_scale,
            ),
            c.base_qty,
        )
        for c in cfgs
    ]


def build_multi_strategy_tournament(configs: list[StrategyConfig] | None = None) -> TournamentRunner:
    """Wrap the strategies in the existing TournamentRunner so they show
    up in the Tournament panel + on the WS as TournamentEvents."""
    cfgs = configs or DEFAULT_STRATEGIES
    variants = [
        TournamentVariant(
            name=c.name,
            wall_proximity_pts=c.wall_proximity_pts,
            wall_collapse_pct=c.wall_collapse_pct,
            wall_collapse_floor_m=c.wall_collapse_floor_m,
            absorption_fe_ratio_min=c.absorption_fe_ratio_min,
            threshold_scale=c.threshold_scale,
        )
        for c in cfgs
    ]
    return TournamentRunner(variants)
