"""FEF engine tests — encode prior session behavior as regression cases.

Scenarios borrowed from the 5/15 session log:
- Warm-up (first 3 iters): always WAIT
- Pin regime (price flat, drift flat): WAIT
- Wall proximity skip (within 5pts of dominant wall): WAIT
- Absorption (drift up but price flat/down): WAIT with absorption=True
- Aligned breakout (price + drift same direction with magnitude): LONG_CALLS / LONG_PUTS
"""

from __future__ import annotations

from fef_sidecar.db.models import FEFConfidence, FEFDirection
from fef_sidecar.engine import FEFEngine, FEFInputs
from fef_sidecar.quantdata.models import ExposureRow


def _iter(
    *,
    ts: int,
    spx: float,
    drift_m: float,
    bias: str = "BULLISH",
    gex_walls: list[tuple[float, float]] | None = None,
    dex_walls: list[tuple[float, float]] | None = None,
    ntm_mp: float = 7400.0,
) -> FEFInputs:
    """Helper: build a FEFInputs with concise wall syntax (strike, net_m)."""
    def _rows(pairs: list[tuple[float, float]] | None) -> list[ExposureRow]:
        if not pairs:
            return []
        return [
            ExposureRow(
                strike=s,
                call_m=max(0.0, n),
                put_m=min(0.0, n),
                net_m=n,
            )
            for s, n in pairs
        ]

    return FEFInputs(
        timestamp_ms=ts,
        spx_price=spx,
        drift_cum_net_m=drift_m,
        drift_bias=bias,
        top_gex_walls=_rows(gex_walls),
        top_dex_walls=_rows(dex_walls),
        ntm_max_pain=ntm_mp,
        raw_max_pain=7000.0,
        aggression_call_pct=40.0,
        aggression_put_pct=38.0,
    )


def test_warmup_returns_wait_until_window_filled() -> None:
    engine = FEFEngine(history_window=3)

    v1 = engine.push(_iter(ts=1, spx=7410.0, drift_m=0.0))
    v2 = engine.push(_iter(ts=2, spx=7411.0, drift_m=1.0))
    v3 = engine.push(_iter(ts=3, spx=7412.0, drift_m=2.0))

    for v in (v1, v2, v3):
        assert v.direction == FEFDirection.WAIT
        assert "warming up" in v.reasoning


def test_pin_regime_returns_wait_with_no_alignment() -> None:
    engine = FEFEngine(history_window=3)

    for i, spx in enumerate([7410.0, 7411.0, 7409.5, 7410.5]):
        engine.push(_iter(ts=i, spx=spx, drift_m=0.0))

    last = engine.push(_iter(ts=5, spx=7410.0, drift_m=0.5))
    assert last.direction == FEFDirection.WAIT
    assert last.aligned is False
    assert "chop" in last.reasoning or "no edge" in last.reasoning


def test_wall_proximity_skip() -> None:
    engine = FEFEngine(history_window=3, wall_proximity_pts=5.0)

    walls = [(7400.0, 50000.0)]  # mega call wall at $7,400
    for i, spx in enumerate([7395.0, 7398.0, 7401.0, 7402.0]):
        engine.push(_iter(ts=i, spx=spx, drift_m=i * 3.0, gex_walls=walls))

    # Spot $7,402 is within 5pts of $7,400 — should SKIP regardless
    last = engine.push(_iter(ts=5, spx=7402.0, drift_m=15.0, gex_walls=walls))
    assert last.direction == FEFDirection.WAIT
    assert last.wall_proximity_skip is True
    assert last.nearest_wall_strike == 7400.0


def test_absorption_pattern_flags_but_does_not_fire() -> None:
    engine = FEFEngine(history_window=3)

    # Drift accumulates bullishly while price drifts down — absorption setup
    walls = [(7440.0, 5000.0)]  # call wall well above, no proximity skip
    engine.push(_iter(ts=1, spx=7420.0, drift_m=0.0, gex_walls=walls))
    engine.push(_iter(ts=2, spx=7418.0, drift_m=10.0, gex_walls=walls))
    engine.push(_iter(ts=3, spx=7416.0, drift_m=22.0, gex_walls=walls))
    last = engine.push(_iter(ts=4, spx=7414.0, drift_m=35.0, gex_walls=walls))

    assert last.absorption is True
    assert last.direction == FEFDirection.WAIT
    assert last.fe_ratio is not None and last.fe_ratio >= 5.0


def test_aligned_long_calls_fires_high_confidence() -> None:
    engine = FEFEngine(history_window=3)

    walls = [(7460.0, 5000.0)]  # call wall well above
    engine.push(_iter(ts=1, spx=7420.0, drift_m=0.0, gex_walls=walls))
    engine.push(_iter(ts=2, spx=7423.0, drift_m=4.0, gex_walls=walls))
    engine.push(_iter(ts=3, spx=7425.0, drift_m=8.0, gex_walls=walls))
    last = engine.push(_iter(ts=4, spx=7430.0, drift_m=18.0, gex_walls=walls))

    assert last.direction == FEFDirection.LONG_CALLS
    assert last.confidence == FEFConfidence.HIGH
    assert last.aligned is True
    assert last.strike_rec is not None and last.strike_rec >= 7430
    assert last.t1 is not None and last.t1 > last.stop  # type: ignore[operator]


def test_aligned_long_puts_fires() -> None:
    engine = FEFEngine(history_window=3)

    walls = [(7380.0, -5000.0)]  # put wall well below, no proximity skip
    engine.push(_iter(ts=1, spx=7420.0, drift_m=0.0, gex_walls=walls, bias="BEARISH"))
    engine.push(_iter(ts=2, spx=7417.0, drift_m=-5.0, gex_walls=walls, bias="BEARISH"))
    engine.push(_iter(ts=3, spx=7414.0, drift_m=-10.0, gex_walls=walls, bias="BEARISH"))
    last = engine.push(_iter(ts=4, spx=7410.0, drift_m=-20.0, gex_walls=walls, bias="BEARISH"))

    assert last.direction == FEFDirection.LONG_PUTS
    assert last.aligned is True
    assert last.strike_rec is not None and last.strike_rec <= 7410


def test_wall_collapse_special_trigger() -> None:
    """Wall collapse with 2-iter persistence: requires the >20% loss to hold
    across two consecutive iter comparisons (noise filter)."""
    engine = FEFEngine(history_window=3, wall_collapse_pct=0.20)

    big = [(7440.0, 50000.0)]      # massive call wall
    mid = [(7440.0, 30000.0)]      # 40% loss vs big (collapse step 1)
    collapsed = [(7440.0, 15000.0)]  # 50% loss vs mid (collapse step 2)

    engine.push(_iter(ts=1, spx=7420.0, drift_m=0.0, gex_walls=big))
    engine.push(_iter(ts=2, spx=7422.0, drift_m=2.0, gex_walls=big))
    engine.push(_iter(ts=3, spx=7424.0, drift_m=4.0, gex_walls=mid))
    last = engine.push(_iter(ts=4, spx=7426.0, drift_m=6.0, gex_walls=collapsed))

    assert last.wall_collapse_trigger is True
    assert last.direction == FEFDirection.LONG_CALLS
    assert last.confidence == FEFConfidence.HIGH


def test_wall_collapse_skips_when_drift_contradicts() -> None:
    """Regression: 2026-05-18 7410C blowup.

    A put wall BELOW spot collapsed (net_m < 0, strike < spx) while drift
    was strongly bearish. Old logic mapped this to LONG_CALLS based on
    strike position alone, firing a directional bull trade into a
    confirmed downtrend. New logic requires (wall TYPE × position)
    confluence + drift agreement; this scenario must NOT fire.
    """
    engine = FEFEngine(history_window=3, wall_collapse_pct=0.20)

    put_wall_big = [(7360.0, -50000.0)]
    put_wall_collapsed = [(7360.0, -20000.0)]

    engine.push(_iter(ts=1, spx=7395.0, drift_m=0.0,
                      gex_walls=put_wall_big, bias="BEARISH"))
    engine.push(_iter(ts=2, spx=7390.0, drift_m=-3.0,
                      gex_walls=put_wall_big, bias="BEARISH"))
    engine.push(_iter(ts=3, spx=7385.0, drift_m=-6.0,
                      gex_walls=put_wall_big, bias="BEARISH"))
    last = engine.push(_iter(ts=4, spx=7372.0, drift_m=-10.0,
                             gex_walls=put_wall_collapsed, bias="BEARISH"))

    # Put wall below spot collapsing during bearish drift = floor breaking.
    # Either fires LONG_PUTS via wall_collapse (correct direction), or
    # falls through to aligned/chop. MUST NOT fire LONG_CALLS.
    assert last.direction != FEFDirection.LONG_CALLS


def test_wall_collapse_put_below_fires_long_puts_when_drift_agrees() -> None:
    """Put wall below collapses + bearish drift → LONG_PUTS (floor gone).
    Updated for 2-iter persistence: collapse must hold across two iter
    comparisons, so the wall drops in two steps."""
    engine = FEFEngine(history_window=3, wall_collapse_pct=0.20)

    put_wall_big = [(7380.0, -50000.0)]
    put_wall_mid = [(7380.0, -30000.0)]       # 40% loss vs big
    put_wall_collapsed = [(7380.0, -10000.0)]  # 67% loss vs mid

    engine.push(_iter(ts=1, spx=7420.0, drift_m=0.0,
                      gex_walls=put_wall_big, bias="BEARISH"))
    engine.push(_iter(ts=2, spx=7415.0, drift_m=-3.0,
                      gex_walls=put_wall_big, bias="BEARISH"))
    engine.push(_iter(ts=3, spx=7410.0, drift_m=-6.0,
                      gex_walls=put_wall_mid, bias="BEARISH"))
    last = engine.push(_iter(ts=4, spx=7405.0, drift_m=-10.0,
                             gex_walls=put_wall_collapsed, bias="BEARISH"))

    assert last.wall_collapse_trigger is True
    assert last.direction == FEFDirection.LONG_PUTS


def test_wall_collapse_itm_wall_does_not_fire() -> None:
    """ITM wall (call below spot, OR put above) collapse is not directional."""
    engine = FEFEngine(history_window=3, wall_collapse_pct=0.20)

    # Call wall BELOW spot (deep ITM calls being closed for profit)
    itm_call_big = [(7300.0, 50000.0)]
    itm_call_collapsed = [(7300.0, 10000.0)]

    engine.push(_iter(ts=1, spx=7400.0, drift_m=0.0, gex_walls=itm_call_big))
    engine.push(_iter(ts=2, spx=7402.0, drift_m=1.0, gex_walls=itm_call_big))
    engine.push(_iter(ts=3, spx=7404.0, drift_m=2.0, gex_walls=itm_call_big))
    last = engine.push(_iter(ts=4, spx=7406.0, drift_m=3.0,
                             gex_walls=itm_call_collapsed))

    assert last.wall_collapse_trigger is False


def test_secondary_scores_populated_on_wait() -> None:
    engine = FEFEngine(history_window=3)

    for i in range(4):
        engine.push(_iter(ts=i, spx=7410.0 + i * 0.1, drift_m=0.0))

    last = engine.push(_iter(ts=5, spx=7410.5, drift_m=0.0))
    assert last.direction == FEFDirection.WAIT
    assert "mp_gravity" in last.secondary_scores
    assert "drift" in last.secondary_scores
    assert "aggression" in last.secondary_scores
