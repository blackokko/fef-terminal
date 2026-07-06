"""Tournament mode — run N FEF variants in parallel and broadcast their
verdicts for side-by-side comparison.

Lets the user see at a glance which strategy preset is firing right now
and which are sitting on WAIT. They can spot when 4 of 5 strategies all
agree (high consensus) vs everyone splitting (low conviction).

Each variant uses a copy of the base FEFInputs but with different
threshold params. We don't run actual trades for variants — just compute
verdicts. The primary engine remains the source of truth for tickets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fef_sidecar.engine.fef import FEFEngine
from fef_sidecar.engine.types import FEFInputs, FEFVerdict


@dataclass
class VariantConfig:
    name: str
    wall_proximity_pts: float
    wall_collapse_pct: float
    wall_collapse_floor_m: float
    absorption_fe_ratio_min: float
    threshold_scale: float = 1.0


@dataclass
class VariantVerdict:
    name: str
    direction: str
    confidence: str
    reasoning: str
    aligned: bool
    absorption: bool


class TournamentRunner:
    """Holds N parallel FEF engines, each with different thresholds."""

    def __init__(self, variants: list[VariantConfig]) -> None:
        self._engines: list[tuple[str, FEFEngine]] = []
        for v in variants:
            engine = FEFEngine(
                wall_proximity_pts=v.wall_proximity_pts,
                # Variants explicitly parametrize the proximity knob -> the
                # gate is opt-in here (live default is OFF, 2026-07-02).
                wall_proximity_skip_enabled=True,
                wall_collapse_pct=v.wall_collapse_pct,
                wall_collapse_floor_m=v.wall_collapse_floor_m,
                absorption_fe_ratio_min=v.absorption_fe_ratio_min,
                threshold_scale=v.threshold_scale,
            )
            self._engines.append((v.name, engine))

    def push(self, inputs: FEFInputs) -> list[VariantVerdict]:
        """Push inputs to all engines, return per-variant verdicts."""
        results: list[VariantVerdict] = []
        for name, engine in self._engines:
            verdict: FEFVerdict = engine.push(inputs)
            results.append(VariantVerdict(
                name=name,
                direction=verdict.direction.value,
                confidence=verdict.confidence.value,
                reasoning=verdict.reasoning[:120],
                aligned=verdict.aligned,
                absorption=verdict.absorption,
            ))
        return results

    @property
    def variant_count(self) -> int:
        return len(self._engines)


DEFAULT_VARIANTS = [
    VariantConfig("Default", wall_proximity_pts=5.0, wall_collapse_pct=0.20,
                  wall_collapse_floor_m=5.0, absorption_fe_ratio_min=5.0),
    VariantConfig("Tight Pin", wall_proximity_pts=3.0, wall_collapse_pct=0.30,
                  wall_collapse_floor_m=10.0, absorption_fe_ratio_min=8.0),
    VariantConfig("Trend Aggressive", wall_proximity_pts=2.0, wall_collapse_pct=0.15,
                  wall_collapse_floor_m=3.0, absorption_fe_ratio_min=4.0),
    VariantConfig("Volatile", wall_proximity_pts=8.0, wall_collapse_pct=0.25,
                  wall_collapse_floor_m=10.0, absorption_fe_ratio_min=6.0),
    VariantConfig("Chop", wall_proximity_pts=15.0, wall_collapse_pct=0.40,
                  wall_collapse_floor_m=20.0, absorption_fe_ratio_min=10.0),
]


def consensus(verdicts: list[VariantVerdict]) -> dict[str, Any]:
    """Compute consensus stats — direction distribution, HIGH count, etc."""
    n = len(verdicts)
    if n == 0:
        return {"n": 0}
    long_calls = sum(1 for v in verdicts if v.direction == "LONG_CALLS")
    long_puts = sum(1 for v in verdicts if v.direction == "LONG_PUTS")
    wait = sum(1 for v in verdicts if v.direction == "WAIT")
    high = sum(1 for v in verdicts if v.confidence == "HIGH")
    aligned = sum(1 for v in verdicts if v.aligned)
    return {
        "n": n,
        "long_calls": long_calls,
        "long_puts": long_puts,
        "wait": wait,
        "high_confidence": high,
        "aligned": aligned,
        "consensus_direction": (
            "LONG_CALLS" if long_calls > n / 2
            else "LONG_PUTS" if long_puts > n / 2
            else "MIXED" if wait < n / 2
            else "WAIT"
        ),
        "consensus_strength": round(max(long_calls, long_puts, wait) / n * 100, 0),
    }
