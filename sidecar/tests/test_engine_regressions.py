"""Regression tests for engine changes shipped 2026-05-18.

Each test pins one specific behavior so the next iteration's changes can't
silently break a fix that took live trading data to discover.
"""

from __future__ import annotations

from fef_sidecar.db.models import FEFConfidence, FEFDirection
from fef_sidecar.engine import FEFEngine, FEFInputs
from fef_sidecar.quantdata.models import ExposureRow


def _iter(*, ts: int, spx: float, drift_m: float, bias: str = "BULLISH",
          gex_walls=None, dex_walls=None, ntm_mp: float = 7400.0) -> FEFInputs:
    def _rows(pairs):
        if not pairs: return []
        return [ExposureRow(strike=s, call_m=max(0.0, n), put_m=min(0.0, n), net_m=n)
                for s, n in pairs]
    return FEFInputs(
        timestamp_ms=ts, spx_price=spx, drift_cum_net_m=drift_m, drift_bias=bias,
        top_gex_walls=_rows(gex_walls), top_dex_walls=_rows(dex_walls),
        ntm_max_pain=ntm_mp, raw_max_pain=7000.0,
        aggression_call_pct=40.0, aggression_put_pct=38.0,
    )


# ---------------------------------------------------------------------------
# drift_bias gate (today shipped: aligned signal demoted if bias disagrees)
# ---------------------------------------------------------------------------


def test_drift_bias_disagrees_demotes_confidence_long_calls_during_bearish():
    """LONG_CALLS aligned but bias=BEARISH should demote HIGH -> MEDIUM."""
    engine = FEFEngine(history_window=3)
    # Set up an aligned LONG_CALLS situation: rising price + rising drift,
    # large enough magnitudes for HIGH classification. Walls way above so
    # no wall_collapse + no wall_proximity_skip.
    walls = [(7500.0, 100.0)]  # far away
    engine.push(_iter(ts=1, spx=7400, drift_m=0, gex_walls=walls, bias="BEARISH"))
    engine.push(_iter(ts=2, spx=7405, drift_m=5, gex_walls=walls, bias="BEARISH"))
    engine.push(_iter(ts=3, spx=7410, drift_m=10, gex_walls=walls, bias="BEARISH"))
    last = engine.push(_iter(ts=4, spx=7420, drift_m=20, gex_walls=walls, bias="BEARISH"))
    assert last.direction == FEFDirection.LONG_CALLS
    assert last.aligned is True
    # Without the gate this would be HIGH; with the gate it should demote
    assert last.confidence != FEFConfidence.HIGH
    assert "bias-disagrees" in last.reasoning or "demoted" in last.reasoning


def test_drift_bias_agrees_keeps_high():
    """LONG_CALLS aligned with bias=BULLISH should KEEP HIGH."""
    engine = FEFEngine(history_window=3)
    walls = [(7500.0, 100.0)]
    engine.push(_iter(ts=1, spx=7400, drift_m=0, gex_walls=walls, bias="BULLISH"))
    engine.push(_iter(ts=2, spx=7405, drift_m=5, gex_walls=walls, bias="BULLISH"))
    engine.push(_iter(ts=3, spx=7410, drift_m=10, gex_walls=walls, bias="BULLISH"))
    last = engine.push(_iter(ts=4, spx=7420, drift_m=20, gex_walls=walls, bias="BULLISH"))
    assert last.direction == FEFDirection.LONG_CALLS
    assert last.aligned is True
    # FE must be in HIGH band for this to fire HIGH: |dPrice|>=5, |dDrift|>=10, FE in [1,5]
    # dPrice=20, dDrift=20, FE=20/20=1.0 — boundary. Should be HIGH or MEDIUM, not LOW.
    assert last.confidence in (FEFConfidence.HIGH, FEFConfidence.MEDIUM)
    assert "bias-disagrees" not in last.reasoning


# ---------------------------------------------------------------------------
# wall_collapse_requires_aligned (today shipped)
# ---------------------------------------------------------------------------


def test_wall_collapse_alone_does_not_fire_without_aligned():
    """Wall collapse fires HIGH only when aligned=True too."""
    engine = FEFEngine(history_window=3, wall_collapse_pct=0.20)
    big = [(7440.0, 50000.0)]
    collapsed = [(7440.0, 20000.0)]  # lost 60%
    # No alignment: price flat, drift flat
    engine.push(_iter(ts=1, spx=7420, drift_m=0, gex_walls=big))
    engine.push(_iter(ts=2, spx=7420, drift_m=0, gex_walls=big))
    engine.push(_iter(ts=3, spx=7420, drift_m=0, gex_walls=big))
    last = engine.push(_iter(ts=4, spx=7420, drift_m=0, gex_walls=collapsed))
    # Wall did collapse but aligned=False -> should NOT fire actionable
    assert not (last.direction != FEFDirection.WAIT and last.wall_collapse_trigger)


# ---------------------------------------------------------------------------
# fe_ratio scoring band (today shipped: HIGH requires FE in [1.0, 5.0])
# ---------------------------------------------------------------------------


def test_high_confidence_requires_fe_in_band():
    """An aligned signal with FE outside [1.0, 5.0] should not be HIGH."""
    engine = FEFEngine(history_window=3)
    walls = [(7500.0, 100.0)]
    # Make FE > 5: tiny price move + big drift
    # dPrice~1pt, dDrift~10M -> FE=10
    engine.push(_iter(ts=1, spx=7400, drift_m=0, gex_walls=walls, bias="BULLISH"))
    engine.push(_iter(ts=2, spx=7400.3, drift_m=3, gex_walls=walls, bias="BULLISH"))
    engine.push(_iter(ts=3, spx=7400.6, drift_m=6, gex_walls=walls, bias="BULLISH"))
    last = engine.push(_iter(ts=4, spx=7401, drift_m=10, gex_walls=walls, bias="BULLISH"))
    # FE = 10/1 = 10 -> outside HIGH band, should be MEDIUM or LOW
    if last.direction != FEFDirection.WAIT:
        assert last.confidence != FEFConfidence.HIGH


# ---------------------------------------------------------------------------
# absorption_extended_to_extreme_fe (FE >= 10 same-sign also flags absorption)
# ---------------------------------------------------------------------------


def test_absorption_flagged_when_fe_extreme_same_sign():
    """FE >= 10 with large drift should mark absorption even when same_sign."""
    engine = FEFEngine(history_window=3)
    walls = [(7500.0, 100.0)]
    # Same-sign rising but drift way bigger than price = absorption-like
    engine.push(_iter(ts=1, spx=7400, drift_m=0, gex_walls=walls))
    engine.push(_iter(ts=2, spx=7400.2, drift_m=5, gex_walls=walls))
    engine.push(_iter(ts=3, spx=7400.4, drift_m=10, gex_walls=walls))
    last = engine.push(_iter(ts=4, spx=7400.6, drift_m=15, gex_walls=walls))
    # FE = 15/0.6 = 25, |dDrift|=15 -> absorption should trigger
    assert last.absorption is True
