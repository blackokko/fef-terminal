"""Regression tests for the PhaseDetector (ride / fade / wait). Synthetic dense tapes.

Shipped defaults (2026-07-01 backtest) DISABLE ride (ride_min=7 > the 6-pillar
max) and require fade_min=5, so the ride/fade *mechanism* tests pin explicit
looser thresholds; the default-config test asserts ride stays off.
"""
from fef_sidecar.engine.phase_detector import PhaseDetector, PhaseSample


def _feed(det, path):
    """path: list of (t_sec, spx, drift, call, put, iv, maxpain). Returns last verdict."""
    v = None
    for t, spx, drift, cp, pp, iv, mp in path:
        v = det.update(PhaseSample(ts_ms=t * 1000, spx=spx, drift_bias=drift,
                                   call_pct=cp, put_pct=pp, atm_iv=iv, max_pain=mp))
    return v


def test_flat_tape_waits():
    det = PhaseDetector()
    path = [(t, 7500.0, "NEUTRAL", 50, 50, 0.20, 7500) for t in range(0, 200, 10)]
    assert _feed(det, path).state == "WAIT"


def test_active_downthrust_rides_puts():
    # RIDE mechanism (enabled via ride_min=3): -2pt/10s = -12pt/min thrust.
    det = PhaseDetector(ride_min=3, vel_ignite=12)
    path = [(t, 7500.0 - 0.2 * t, "BEARISH", 40, 60, 0.20 + 0.0004 * t, 7500)
            for t in range(0, 160, 10)]
    v = _feed(det, path)
    assert v.state == "RIDE" and v.direction == "LONG_PUTS", v


def test_default_config_disables_ride():
    # Shipped default ride_min=7 > 6-pillar max: even a hard -24pt/min down-thrust
    # never produces RIDE (backtest: chasing the max-velocity bar is a net loser).
    det = PhaseDetector()
    path = [(t, 7500.0 - 0.4 * t, "BEARISH", 30, 70, 0.20 + 0.0008 * t, 7500)
            for t in range(0, 200, 10)]
    v = _feed(det, path)
    assert v.state != "RIDE", v


def test_stall_then_bounce_fades_calls():
    # FADE mechanism + ride->fade hand-off, pinned to the pre-backtest scale.
    det = PhaseDetector(fade_min=4, min_move=12, bounce_pt=5)
    path = []
    # 1) thrust down 7500 -> 7460 over 100s
    for t in range(0, 110, 10):
        path.append((t, 7500.0 - 0.4 * t, "BEARISH", 40, 60, 0.20 + 0.0006 * t, 7475))
    # 2) stall at the low ~60s (IV peaks), drift flips to BULLISH, puts still slightly ahead
    for t in range(110, 170, 10):
        path.append((t, 7460.0, "BULLISH", 45, 48, 0.266, 7472))
    # 3) bounce +7pt over ~40s, calls take over, IV rolls back
    for i, t in enumerate(range(170, 220, 10)):
        path.append((t, 7460.0 + 1.5 * (i + 1), "BULLISH", 52, 40, 0.256 - 0.002 * i, 7472))
    v = _feed(det, path)
    assert v.state == "FADE" and v.direction == "LONG_CALLS", v
    # the stalled low itself must NOT have been a RIDE (no active thrust there),
    # even with ride enabled — the thrust gate is what hands off to FADE.
    det2 = PhaseDetector(fade_min=4, min_move=12, bounce_pt=5, ride_min=3, vel_ignite=12)
    mid = _feed(det2, path[:16])   # up to the stall, before the bounce
    assert mid.state != "RIDE" or mid.direction != "LONG_PUTS", mid


def test_thrust_gate_blocks_ride_when_stalled():
    """At a stalled low with bearish flow, RIDE-PUTS must NOT fire (the old bug)."""
    det = PhaseDetector(ride_min=3, vel_ignite=12)  # ride enabled — test the gate, not the min
    path = [(t, 7460.0, "BEARISH", 45, 50, 0.25, 7475) for t in range(0, 200, 10)]  # flat, no velocity
    v = _feed(det, path)
    assert not (v.state == "RIDE" and v.direction == "LONG_PUTS"), v
