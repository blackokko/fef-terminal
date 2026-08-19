"""Band-touch tolerance must scale with the channel, not with the price level.

WHY THIS EXISTS. `touches_lower_band` / `touches_upper_band` used a hardcoded
`tolerance_pts = 0.5`. That is only meaningful because the credit-spread
detector has so far run on SPX alone. Measured on real 65-bar / 30-min channels
(the exact shape `_get_spread_bars_30m` builds — "30 mins", "5 D"):

    SPX  median residual sigma 24.39 pts  ->  0.50 pts = 0.0205 sigma
    QQQ  median residual sigma  2.74 pts  ->  0.50 pts = 0.1826 sigma

i.e. the SAME constant was 8.9x looser on QQQ. `multi_symbol_scanner` already
scans six symbols, so the first time this detector was pointed at one of them
the band test would have silently become a much more permissive rule.

The contract these tests pin:
  * a touch is decided in units of the channel's OWN sigma, so two instruments
    at wildly different price levels with geometrically identical setups agree
  * SPX behaviour is UNCHANGED (0.02 sigma was chosen to reproduce the measured
    0.0205) — this is a portability fix, not a threshold change
  * an explicit `tolerance_pts` still overrides, for callers/tests that need it
  * a degenerate (zero-sigma) channel fails CLOSED rather than fabricating width
"""

from __future__ import annotations

from dataclasses import dataclass

from fef_sidecar.engine.technical import (
    ChannelPoint,
    DEFAULT_BAND_TOLERANCE_SIGMA,
    touches_lower_band,
    touches_upper_band,
)


@dataclass
class _Bar:
    """Only .high/.low are read by the functions under test."""
    high: float
    low: float


def _point(center: float, sigma: float) -> ChannelPoint:
    return ChannelPoint(
        center=center,
        upper_1=center + sigma, upper_2=center + 2 * sigma, upper_3=center + 3 * sigma,
        lower_1=center - sigma, lower_2=center - 2 * sigma, lower_3=center - 3 * sigma,
    )


# --- the portability property, which is the whole point --------------------

def test_same_geometry_same_verdict_across_price_scales():
    """SPX-like (7750 / sigma 24.4) and QQQ-like (600 / sigma 2.74) channels with
    a low the SAME fraction of sigma away from STD1 must agree. Under the old
    absolute constant they did not."""
    spx = _point(7750.0, 24.39)
    qqq = _point(600.0, 2.738)

    for frac, expected in ((0.01, "STD1"), (0.5, None)):
        # low sits `frac` sigma ABOVE lower_1 -> inside the band by that much
        spx_bar = _Bar(high=0.0, low=spx.lower_1 + frac * 24.39)
        qqq_bar = _Bar(high=0.0, low=qqq.lower_1 + frac * 2.738)
        assert touches_lower_band(spx_bar, spx) == expected
        assert touches_lower_band(qqq_bar, qqq) == expected, (
            f"QQQ disagreed with SPX at {frac} sigma — tolerance is not scaling"
        )


def test_old_absolute_constant_would_have_disagreed():
    """Demonstrates the bug this fix removes, by forcing the old absolute 0.5
    via the override and showing it splits two GEOMETRICALLY IDENTICAL setups.

    Both bars sit 0.1 sigma above their own lower_1 — the same setup on any
    chart. Under 0.5 absolute points that is 2.44 pts on SPX (no touch) but
    0.27 pts on QQQ (a touch). Same geometry, opposite verdict. Under the
    sigma-relative default both correctly say no touch."""
    spx, qqq = _point(7750.0, 24.39), _point(600.0, 2.738)
    spx_bar = _Bar(0.0, spx.lower_1 + 0.1 * 24.39)
    qqq_bar = _Bar(0.0, qqq.lower_1 + 0.1 * 2.738)

    # the old behaviour: disagrees
    assert touches_lower_band(spx_bar, spx, tolerance_pts=0.5) is None
    assert touches_lower_band(qqq_bar, qqq, tolerance_pts=0.5) == "STD1"

    # the fix: agrees
    assert touches_lower_band(spx_bar, spx) is None
    assert touches_lower_band(qqq_bar, qqq) is None


# --- SPX behaviour must not move -------------------------------------------

def test_spx_default_reproduces_the_old_constant():
    """0.02 sigma on SPX's median 24.39 sigma is 0.488 pts, vs the old 0.5 —
    within a hundredth of a sigma, so SPX results are unchanged."""
    assert DEFAULT_BAND_TOLERANCE_SIGMA == 0.02
    assert abs(DEFAULT_BAND_TOLERANCE_SIGMA * 24.39 - 0.5) < 0.02


# --- deepest band wins, unchanged ------------------------------------------

def test_deepest_band_wins():
    p = _point(100.0, 10.0)
    assert touches_lower_band(_Bar(0.0, p.lower_3 - 1), p) == "STD3"
    assert touches_lower_band(_Bar(0.0, p.lower_2 - 1), p) == "STD2"
    assert touches_lower_band(_Bar(0.0, p.lower_1 - 1), p) == "STD1"
    assert touches_lower_band(_Bar(0.0, p.center), p) is None


def test_upper_band_mirrors():
    p = _point(100.0, 10.0)
    assert touches_upper_band(_Bar(p.upper_3 + 1, 0.0), p) == "STD3"
    assert touches_upper_band(_Bar(p.upper_1 + 0.1, 0.0), p) == "STD1"
    assert touches_upper_band(_Bar(p.center, 0.0), p) is None


# --- escape hatch + degenerate channel -------------------------------------

def test_absolute_override_still_works():
    p = _point(600.0, 2.738)
    bar = _Bar(0.0, p.lower_1 + 0.5)
    assert touches_lower_band(bar, p) is None                      # sigma-relative
    assert touches_lower_band(bar, p, tolerance_pts=1.0) == "STD1"  # forced absolute


def test_zero_sigma_channel_fails_closed():
    """A flat series gives sigma 0. Fabricating a width there would invent
    touches; an exact comparison is the honest answer and errs toward fewer
    signals."""
    flat = _point(100.0, 0.0)
    assert touches_lower_band(_Bar(0.0, 100.0), flat) == "STD3"   # exactly on it
    assert touches_lower_band(_Bar(0.0, 100.0001), flat) is None  # a hair above
