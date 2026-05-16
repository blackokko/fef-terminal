"""Validate NTM max pain calculation against the 2026-05-15 session.

Reference: on 5/15/2026 the raw chain-wide max pain (qd_get_max_pain) returned
$7,000 — useless for intraday signaling. The NTM-filtered calculation should
return $7,400, matching where price actually pinned all day.
"""

from __future__ import annotations

import pytest

from fef_sidecar.quantdata.max_pain import compute_ntm_max_pain
from fef_sidecar.quantdata.mock import SPX_PRICE, _OI_ROWS


def test_ntm_max_pain_pins_at_7400_on_2026_05_15() -> None:
    """Per [[ntm-max-pain]] memory: real 5/15 pin was $7,400 while raw chain MP was $7,000."""
    result = compute_ntm_max_pain(list(_OI_ROWS), spx_price=SPX_PRICE, window=50.0)
    assert result.max_pain == pytest.approx(7400.0)
    assert result.spx_price == pytest.approx(SPX_PRICE)
    # Window [SPX-50, SPX+50] = [7359.18, 7459.18] — 7460 is just outside.
    assert result.strikes_considered == 14
    assert result.distance_pts > 0  # spot was above NTM max pain


def test_ntm_window_excludes_far_otm() -> None:
    from fef_sidecar.quantdata.models import OIRow

    rows = [
        OIRow(strike=7400.0, call_oi=30000, put_oi=10000),
        OIRow(strike=6000.0, call_oi=1, put_oi=999_999),  # macro hedge — should be ignored
    ]
    result = compute_ntm_max_pain(rows, spx_price=7410.0, window=50.0)
    assert result.max_pain == pytest.approx(7400.0)
    assert result.strikes_considered == 1  # the far-OTM put was excluded


def test_ntm_distance_signs() -> None:
    from fef_sidecar.quantdata.models import OIRow

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
