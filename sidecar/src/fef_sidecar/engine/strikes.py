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

    # Stop sets the R unit. Today's data showed 10pt risk + 10pt T1 = 1:1
    # which can't be profitable at 22% win rate. New design: T1 = 1R, T2 = 2R
    # so wins can clear the loser-magnitude in fewer hits. With 50/30/20 split
    # the weighted reward is (0.5 * 1R) + (0.3 * 2R) + (0.2 * T3R).
    stop_distance = 10.0
    stop = spot - stop_distance if is_long_calls else spot + stop_distance

    # T1: 1R from spot. If a wall is closer (and ahead), prefer it but never
    # use a wall that yields <0.5R since that's a setup we can't win at.
    one_r_offset = stop_distance  # 10pts
    t1_fixed = spot + one_r_offset if is_long_calls else spot - one_r_offset
    if walls_dir and abs(walls_dir[0] - spot) >= 0.5 * stop_distance and abs(walls_dir[0] - spot) <= 25:
        t1 = walls_dir[0]
    else:
        t1 = t1_fixed

    # T2: 2R from spot. Wall override only if 2R <= wall <= 2.5R.
    two_r_offset = 2 * stop_distance  # 20pts
    t2_fixed = spot + two_r_offset if is_long_calls else spot - two_r_offset
    t2_candidates = [w for w in walls_dir if abs(w - t1) > 3]
    if t2_candidates and 0.8 * two_r_offset <= abs(t2_candidates[0] - spot) <= 1.3 * two_r_offset:
        t2 = t2_candidates[0]
    else:
        t2 = t2_fixed

    # T3: max-pain pull or third wall, else fixed 5R for the runner
    if is_long_calls:
        if ntm_max_pain and ntm_max_pain > t2 + 3:
            t3 = ntm_max_pain
        elif len(walls_dir) > 2 and walls_dir[2] > t2:
            t3 = walls_dir[2]
        else:
            t3 = spot + 5 * stop_distance
    else:
        if ntm_max_pain and ntm_max_pain < t2 - 3:
            t3 = ntm_max_pain
        elif len(walls_dir) > 2 and walls_dir[2] < t2:
            t3 = walls_dir[2]
        else:
            t3 = spot - 5 * stop_distance

    return StrikePlan(strike_rec=strike, t1=t1, t2=t2, t3=t3, stop=stop)
