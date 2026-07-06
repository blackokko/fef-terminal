"""Strike-selection moneyness consistency across symbols.

Root cause (2026-07-02): build_plan applied the SPX-calibrated 0-10 POINT
OTM offset and 5-POINT strike rounding to EVERY symbol. On SPX ~7500 that
is ~0.13% and one strike-width — effectively ATM. On QQQ/SPY ~730-745 the
same 10 points is ~1.4% OTM and the 5-pt rounding jumps 5 strike-widths on
1-wide ETF chains: that is exactly how the 7/1-7/2 scanner fires bought
SPY 735P / QQQ 720P x2 / SPY 745C / QQQ 740C while SPX got ~ATM 7490C.

The fix scales all point-based geometry by spot/7500 for spot < 2000 (and
rounds to 1-wide increments there); SPX-class behavior is bit-identical.
"""

from __future__ import annotations

import pytest

from fef_sidecar.db.models import FEFDirection
from fef_sidecar.engine.strikes import (
    _nearest_otm_strike,
    _price_scale,
    _strike_increment,
    build_plan,
)

CALLS = FEFDirection.LONG_CALLS
PUTS = FEFDirection.LONG_PUTS


def _plan(spot: float, direction, *, minutes: float):
    return build_plan(
        spot, direction,
        top_gex_walls=[], top_dex_walls=[],
        ntm_max_pain=None, minutes_to_close=minutes,
    )


def _old_rule(spot: float, direction, minutes: float) -> float:
    """The pre-fix rule, for documentation/reconciliation in tests:
    flat SPX-point offset + 5-pt rounding regardless of symbol level."""
    offset = max(0.0, min(10.0, minutes * 0.03))
    target = spot + offset if direction == CALLS else spot - offset
    return round(target / 5.0) * 5.0


# ---------------------------------------------------------------------------
# Scale / increment primitives
# ---------------------------------------------------------------------------

def test_price_scale_and_increment_classes() -> None:
    assert _price_scale(7484.0) == 1.0            # SPX-class: unchanged
    assert _strike_increment(7484.0) == 5.0
    assert _price_scale(733.0) == pytest.approx(733.0 / 7500.0)
    assert _strike_increment(733.0) == 1.0        # ETF-class: 1-wide strikes


@pytest.mark.parametrize(
    "spot,direction,expected",
    [
        (7484.0, CALLS, 7485.0),   # SPX late-day -> nearest 5-wide strike
        (7484.0, PUTS, 7485.0),
        (734.0, CALLS, 734.0),     # QQQ late-day -> nearest 1-wide strike (ATM)
        (734.0, PUTS, 734.0),
        (620.0, CALLS, 620.0),     # SPY-level -> nearest strike
        (620.0, PUTS, 620.0),
    ],
)
def test_nearest_atm_late_day_across_symbols(spot, direction, expected) -> None:
    plan = _plan(spot, direction, minutes=10)  # offset ~0.3pt SPX-scale -> ATM
    assert plan.strike_rec == expected


# ---------------------------------------------------------------------------
# SPX behavior is bit-identical to the old rule (regression pin)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("minutes", [390, 200, 60, 5])
@pytest.mark.parametrize("direction", [CALLS, PUTS])
def test_spx_class_unchanged(minutes, direction) -> None:
    spot = 7484.0
    plan = _plan(spot, direction, minutes=minutes)
    assert plan.strike_rec == _old_rule(spot, direction, minutes)
    # geometry unchanged too (scale = 1.0)
    if direction == CALLS:
        assert plan.stop == spot - 10.0 and plan.t1 == spot + 15.0
    else:
        assert plan.stop == spot + 10.0 and plan.t1 == spot - 15.0


def test_spx_7490c_case_reproduced() -> None:
    # The live SPX fire (~ATM 7490C with spot ~7484, mid-session offset ~6pt)
    # behaves identically before and after the fix.
    plan = _plan(7484.0, CALLS, minutes=200)  # offset = 6.0
    assert plan.strike_rec == 7490.0
    assert plan.strike_rec == _old_rule(7484.0, CALLS, 200)


# ---------------------------------------------------------------------------
# The 7/1-7/2 non-SPX fires: old rule reproduces the bad strikes; the fix
# picks nearest-ATM. Spots are back-derived from the live fills (the old
# rule maps them exactly onto the strikes that were actually bought).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "spot,direction,old_strike,fixed_strike",
    [
        (731.8, PUTS, 720.0, 731.0),   # QQQ 720P — ~1.6% OTM -> ~ATM
        (742.8, PUTS, 735.0, 742.0),   # SPY 735P — ~1.0% OTM -> ~ATM
        (737.2, CALLS, 745.0, 738.0),  # SPY 745C — ~1.1% OTM -> ~ATM
        (731.9, CALLS, 740.0, 733.0),  # QQQ 740C — ~1.1% OTM -> ~ATM
    ],
)
def test_etf_fires_become_nearest_atm(spot, direction, old_strike, fixed_strike) -> None:
    minutes = 390.0  # morning fire -> max offset (worst case for the old rule)
    assert _old_rule(spot, direction, minutes) == old_strike  # reproduces live fill
    plan = _plan(spot, direction, minutes=minutes)
    assert plan.strike_rec == fixed_strike
    # moneyness parity with SPX: strike within ~0.2% of spot (SPX gets 0.13%)
    assert abs(plan.strike_rec - spot) / spot <= 0.002


def test_etf_exit_geometry_scales_with_price_level() -> None:
    """Stop/targets were also SPX-point-calibrated: a 10-pt stop on QQQ 733
    is a 1.4% move (never hit / absurd R). Scaled geometry keeps the same
    RELATIVE distances SPX gets."""
    spot = 733.0
    plan = _plan(spot, CALLS, minutes=390)
    scale = spot / 7500.0
    assert plan.stop == pytest.approx(spot - 10.0 * scale)
    assert plan.t1 == pytest.approx(spot + 15.0 * scale)
    assert plan.t2 == pytest.approx(spot + 25.0 * scale)
    assert plan.t3 == pytest.approx(spot + 50.0 * scale)
    # relative stop distance matches SPX's 10/7500
    assert abs(plan.stop - spot) / spot == pytest.approx(10.0 / 7500.0, rel=1e-6)


def test_nearest_otm_strike_increment_rounding() -> None:
    assert _nearest_otm_strike(7492.0, CALLS, 0.0) == 7490.0
    assert _nearest_otm_strike(7493.0, CALLS, 0.0) == 7495.0
    assert _nearest_otm_strike(733.4, CALLS, 0.0) == 733.0
    assert _nearest_otm_strike(733.6, PUTS, 0.0) == 734.0
