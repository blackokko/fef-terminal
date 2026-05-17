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


def _walls_in_direction_sorted(
    walls: list[ExposureRow],
    spot: float,
    direction: FEFDirection,
    min_distance: float = 2.0,
) -> list[float]:
    """All wall strikes in the trade direction, sorted nearest-first."""
    if direction == FEFDirection.LONG_CALLS:
        candidates = [w.strike for w in walls if w.strike > spot + min_distance]
        return sorted(candidates)
    else:
        candidates = [w.strike for w in walls if w.strike < spot - min_distance]
        return sorted(candidates, reverse=True)


def build_plan(
    spot: float,
    direction: FEFDirection,
    *,
    top_gex_walls: list[ExposureRow],
    top_dex_walls: list[ExposureRow],
    ntm_max_pain: float | None = None,
) -> StrikePlan:
    """Build a staged-exit plan anchored on real market structure (walls + NTM MP).

    IMPROVEMENTS #6: T1 = nearest wall in direction (if within reasonable
    distance), T2 = next wall after that, T3 = NTM MP if reachable else
    third wall / fixed offset. Falls back to fixed offsets when wall map
    is sparse.
    """
    strike = _nearest_otm_5pt(spot, direction)

    all_walls = top_gex_walls + top_dex_walls
    walls_dir = _walls_in_direction_sorted(all_walls, spot, direction)

    is_long_calls = direction == FEFDirection.LONG_CALLS

    # T1: nearest wall, but not more than ~25pts away (else use +10 fallback)
    if walls_dir and abs(walls_dir[0] - spot) <= 25:
        t1 = walls_dir[0]
    else:
        t1 = spot + 10 if is_long_calls else spot - 10

    # T2: next wall after T1 (skip duplicates near T1)
    t2_candidates = [w for w in walls_dir if abs(w - t1) > 3]
    if t2_candidates and abs(t2_candidates[0] - spot) <= 40:
        t2 = t2_candidates[0]
    else:
        t2 = spot + 20 if is_long_calls else spot - 20

    # T3: NTM max pain if reachable in direction, else third wall, else fixed
    if is_long_calls:
        if ntm_max_pain and ntm_max_pain > t2 + 3:
            t3 = ntm_max_pain
        elif len(walls_dir) > 2 and walls_dir[2] > t2:
            t3 = walls_dir[2]
        else:
            t3 = spot + 35
    else:
        if ntm_max_pain and ntm_max_pain < t2 - 3:
            t3 = ntm_max_pain
        elif len(walls_dir) > 2 and walls_dir[2] < t2:
            t3 = walls_dir[2]
        else:
            t3 = spot - 35

    stop = spot - 10 if is_long_calls else spot + 10

    return StrikePlan(strike_rec=strike, t1=t1, t2=t2, t3=t3, stop=stop)
