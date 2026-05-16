"""Strike + target selection for actionable FEF signals.

v1: SPX-price-space targets (no option chain integration yet). v1.5 will
add option-price targets once IBKR market data is wired in Task #3.
"""

from __future__ import annotations

from dataclasses import dataclass

from fef_sidecar.db.models import FEFDirection
from fef_sidecar.quantdata.models import ExposureRow


@dataclass(frozen=True)
class StrikePlan:
    strike_rec: float
    t1: float
    t2: float
    t3: float
    stop: float


def _nearest_otm_5pt(spot: float, direction: FEFDirection, offset_pts: float = 35.0) -> float:
    """Pick the nearest 5-point SPX strike `offset_pts` OTM in the given direction."""
    if direction == FEFDirection.LONG_CALLS:
        target = spot + offset_pts
    else:
        target = spot - offset_pts
    return round(target / 5.0) * 5.0


def _next_wall_in_direction(
    walls: list[ExposureRow],
    spot: float,
    direction: FEFDirection,
) -> float | None:
    """Find the nearest wall in the trade direction (above for calls, below for puts).

    Used to anchor T2/T3 on actual market structure rather than fixed offsets.
    """
    candidates: list[float] = []
    for w in walls:
        if direction == FEFDirection.LONG_CALLS and w.strike > spot + 2:
            candidates.append(w.strike)
        elif direction == FEFDirection.LONG_PUTS and w.strike < spot - 2:
            candidates.append(w.strike)
    if not candidates:
        return None
    if direction == FEFDirection.LONG_CALLS:
        return min(candidates)
    return max(candidates)


def build_plan(
    spot: float,
    direction: FEFDirection,
    *,
    top_gex_walls: list[ExposureRow],
    top_dex_walls: list[ExposureRow],
    ntm_max_pain: float | None = None,
) -> StrikePlan:
    """Build a staged-exit plan around current spot + market structure.

    Targets are SPX index price levels. Stop is also an SPX level.
    The user maps these to option exits manually in v1 (clearer with the
    option-chain integration coming in Task #3).
    """
    strike = _nearest_otm_5pt(spot, direction)

    next_wall = _next_wall_in_direction(top_gex_walls + top_dex_walls, spot, direction)

    if direction == FEFDirection.LONG_CALLS:
        t1 = spot + 10
        t2 = next_wall if next_wall else (spot + 20)
        t3 = ntm_max_pain if (ntm_max_pain and ntm_max_pain > spot + 25) else (spot + 35)
        stop = spot - 10
    else:
        t1 = spot - 10
        t2 = next_wall if next_wall else (spot - 20)
        t3 = ntm_max_pain if (ntm_max_pain and ntm_max_pain < spot - 25) else (spot - 35)
        stop = spot + 10

    return StrikePlan(strike_rec=strike, t1=t1, t2=t2, t3=t3, stop=stop)
