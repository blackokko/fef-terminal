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


# SPX reference level for moneyness scaling. The point offsets below were
# calibrated in SPX POINTS at the ~7500 level (10 pts = 0.13%). Symbols
# trading at other price levels must get the SAME MONEYNESS, not the same
# points.
_SPX_REF_LEVEL = 7500.0


def _price_scale(spot: float) -> float:
    """Moneyness scale factor vs the SPX-calibrated point offsets.

    MONEYNESS FIX (2026-07-02): the scanner bought clearly-OTM strikes on
    ETF-level symbols (7/1: SPY 735P, QQQ 720P x2, SPY 745C; 7/2: QQQ 740C)
    while SPX got ~ATM (7490C @ spot ~7484). Root cause: this module applied
    the SPX-calibrated 0-10 POINT OTM offset AND 5-POINT strike rounding to
    every symbol — 10 pts is 0.13% on SPX ~7500 but ~1.4% on QQQ ~730, and
    5-pt rounding is 5 strike-widths on 1-wide ETF chains (720 instead of
    ~731). SPX-class symbols (spot >= 2000) keep scale 1.0 so existing SPX
    behavior is bit-identical; lower-priced symbols scale ALL point-based
    geometry (strike offset, stop distance, target offsets) by spot/7500 so
    every symbol trades the same effective moneyness SPX gets."""
    if spot >= 2000:
        return 1.0
    return max(0.01, spot / _SPX_REF_LEVEL)


def _strike_increment(spot: float) -> float:
    """Symbol-class strike increment: index-level chains (SPX/NDX/ES,
    spot >= 2000) quote 5-wide near ATM; ETF-level chains (SPY/QQQ/IWM/
    GLD/SMH/TLT) quote 1-wide."""
    return 5.0 if spot >= 2000 else 1.0


def _nearest_otm_strike(
    spot: float, direction: FEFDirection, offset_pts: float = 35.0,
    increment: float | None = None,
) -> float:
    """Nearest listed strike `offset_pts` OTM in the trade direction,
    rounded to the symbol-class strike increment. offset_pts must already
    be moneyness-scaled by the caller (build_plan does this)."""
    inc = increment if increment is not None else _strike_increment(spot)
    if direction == FEFDirection.LONG_CALLS:
        target = spot + offset_pts
    else:
        target = spot - offset_pts
    return round(target / inc) * inc


def _nearest_otm_5pt(spot: float, direction: FEFDirection, offset_pts: float = 35.0) -> float:
    """Back-compat alias — SPX 5-pt semantics preserved for spot >= 2000."""
    return _nearest_otm_strike(spot, direction, offset_pts)


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
    minutes_to_close: float | None = None,
) -> StrikePlan:
    """Build a staged-exit plan anchored on real market structure (walls + NTM MP).

    IMPROVEMENTS #6: T1 = nearest wall in direction (if within reasonable
    distance), T2 = next wall after that, T3 = NTM MP if reachable else
    third wall / fixed offset. Falls back to fixed offsets when wall map
    is sparse.
    """
    # Near-ATM strike selection: shrink the OTM offset toward ATM as 0DTE expiry
    # nears. A far-OTM strike can't go ITM late in the day -- it's a delta-0.1
    # lottery ticket that spikes then collapses (the give-back the operator saw).
    # ~10pt early, ATM in the final hour. (Was a flat 35pt OTM regardless of time.)
    if minutes_to_close is None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        _now = datetime.now(ZoneInfo("America/New_York"))
        _close = _now.replace(hour=16, minute=0, second=0, microsecond=0)
        minutes_to_close = max(0.0, (_close - _now).total_seconds() / 60.0)
    # Moneyness scaling (see _price_scale): all point-based geometry below is
    # SPX-calibrated; scale it to the symbol's own price level so ETF-class
    # symbols get the same effective moneyness (scale=1.0 for SPX — no change).
    scale = _price_scale(spot)
    otm_offset = max(0.0, min(10.0, minutes_to_close * 0.03)) * scale
    strike = _nearest_otm_strike(spot, direction, offset_pts=otm_offset)

    all_walls = top_gex_walls + top_dex_walls
    walls_dir = _walls_in_direction_sorted(all_walls, spot, direction,
                                           min_distance=2.0 * scale)

    is_long_calls = direction == FEFDirection.LONG_CALLS

    # T1 at 1.5R, T2 at 2.5R per Chat Claude's review. At 1:1 (T1=1R) the
    # math required 55%+ win rate to clear breakeven — current confluence
    # win rate is 41%. Moving T1 to 1.5R + 30% size lets each win contribute
    # more, T2 stays well-positioned for the MFE distribution (80% of winners
    # reach >=2R in today's data).
    stop_distance = 10.0 * scale
    stop = spot - stop_distance if is_long_calls else spot + stop_distance

    # T1: 1.5R from spot. Wall override only if 0.5R-2R band.
    t1_offset = 1.5 * stop_distance  # 15pts
    t1_fixed = spot + t1_offset if is_long_calls else spot - t1_offset
    if walls_dir and 0.5 * stop_distance <= abs(walls_dir[0] - spot) <= 2 * stop_distance:
        t1 = walls_dir[0]
    else:
        t1 = t1_fixed

    # T2: 2.5R from spot. Wall override only if 2R-3R band.
    t2_offset = 2.5 * stop_distance  # 25pts
    t2_fixed = spot + t2_offset if is_long_calls else spot - t2_offset
    t2_candidates = [w for w in walls_dir if abs(w - t1) > 3]
    if t2_candidates and 2 * stop_distance <= abs(t2_candidates[0] - spot) <= 3 * stop_distance:
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
