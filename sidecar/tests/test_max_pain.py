"""Validate NTM max pain calculation against the 2026-05-15 session.

Reference: on 5/15/2026 the raw chain-wide max pain returned $7,000 — useless
for intraday signaling. The NTM-filtered calculation should return $7,400,
matching where price actually pinned all day.

[REMOVED 2026-08-25] QuantData subscription CANCELLED. The 5/15 fixture
(SPX_PRICE + _OI_ROWS) used to be imported from the deleted
`fef_sidecar.quantdata.mock`. It is FROZEN CAPTURED DATA, not vendor code, and
this is a real regression pin ([[ntm-max-pain]]), so the rows are inlined below
verbatim rather than the test being dropped with the client.
"""

from __future__ import annotations

import pytest

from fef_sidecar.engine.max_pain import compute_ntm_max_pain
from fef_sidecar.engine.types import OIRow

# Frozen 2026-05-15 SPX 0DTE open interest, as captured. Do not edit — the
# assertions below (max pain 7400, 14 strikes in the +/-50 window) pin it.
SPX_PRICE = 7409.18

_OI_ROWS = [
    OIRow(strike=7400.0, call_oi=30921, put_oi=9240),
    OIRow(strike=7450.0, call_oi=8333, put_oi=6618),
    OIRow(strike=7425.0, call_oi=2314, put_oi=3783),
    OIRow(strike=7375.0, call_oi=3905, put_oi=1312),
    OIRow(strike=7460.0, call_oi=1206, put_oi=3626),
    OIRow(strike=7360.0, call_oi=3077, put_oi=1564),
    OIRow(strike=7440.0, call_oi=1593, put_oi=3024),
    OIRow(strike=7420.0, call_oi=1695, put_oi=1986),
    OIRow(strike=7455.0, call_oi=646, put_oi=2833),
    OIRow(strike=7390.0, call_oi=908, put_oi=2318),
    OIRow(strike=7410.0, call_oi=812, put_oi=2269),
    OIRow(strike=7395.0, call_oi=622, put_oi=2185),
    OIRow(strike=7445.0, call_oi=582, put_oi=1929),
    OIRow(strike=7415.0, call_oi=275, put_oi=1985),
    OIRow(strike=7405.0, call_oi=920, put_oi=1196),
]


def test_ntm_max_pain_pins_at_7400_on_2026_05_15() -> None:
    """Per [[ntm-max-pain]] memory: real 5/15 pin was $7,400 while raw chain MP was $7,000."""
    result = compute_ntm_max_pain(list(_OI_ROWS), spx_price=SPX_PRICE, window=50.0)
    assert result.max_pain == pytest.approx(7400.0)
    assert result.spx_price == pytest.approx(SPX_PRICE)
    # Window [SPX-50, SPX+50] = [7359.18, 7459.18] — 7460 is just outside.
    assert result.strikes_considered == 14
    assert result.distance_pts > 0  # spot was above NTM max pain


def test_ntm_window_excludes_far_otm() -> None:
    rows = [
        OIRow(strike=7400.0, call_oi=30000, put_oi=10000),
        OIRow(strike=6000.0, call_oi=1, put_oi=999_999),  # macro hedge — should be ignored
    ]
    result = compute_ntm_max_pain(rows, spx_price=7410.0, window=50.0)
    assert result.max_pain == pytest.approx(7400.0)
    assert result.strikes_considered == 1  # the far-OTM put was excluded


def test_ntm_distance_signs() -> None:
    rows = [
        OIRow(strike=7400.0, call_oi=1000, put_oi=1000),
        OIRow(strike=7420.0, call_oi=1000, put_oi=1000),
    ]
    result_above = compute_ntm_max_pain(rows, spx_price=7430.0, window=50.0)
    assert result_above.distance_pts > 0
    assert result_above.distance_pct > 0

    result_below = compute_ntm_max_pain(rows, spx_price=7390.0, window=50.0)
    assert result_below.distance_pts < 0


def test_empty_chain_returns_spot() -> None:
    result = compute_ntm_max_pain([], spx_price=7410.0)
    assert result.max_pain == 7410.0
    assert result.distance_pts == 0.0
    assert result.strikes_considered == 0
