"""Short-gamma directional continuation signal (Chat spec 2026-05-29).

Hypothesis: in SHORT_GAMMA regimes dealer hedging AMPLIFIES moves (sell into
declines, buy into rallies), so a directional commitment in the direction of an
established move should have positive expectancy. Distinct from generic momentum
(falsified): here the regime + directional commitment IS the signal.

Fires when ALL of:
  1. regime == SHORT_GAMMA at signal time (caller supplies regime_lookup)
  2. established move: |close_now - close_{lookback_min ago}| >= move_pts
  3. confirmation: current bar closes in the move's direction
  4. direction = the move's direction (up -> CALLS, down -> PUTS) [structural]
Entry: same-bar close of the confirmation bar. Cooldown prevents stacking.

Pure detector — operates on SPX 1-min bars + a regime_lookup(ts_ms, spot) ->
regime string (or None). require_regime lets the harness reuse this for the
controls (buy-the-move = None, long-gamma comparison = "LONG_GAMMA").
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fef_sidecar.engine.signals.dale import Bar, CALLS, PUTS


@dataclass
class SGSignal:
    ts_ms: int
    direction: str        # CALLS / PUTS
    price: float          # confirmation-bar close (entry)
    move_pts: float       # signed established move
    regime: str
    detail: dict = field(default_factory=dict)


def detect(
    bars_1m: list[Bar],
    regime_lookup,
    *,
    move_pts: float = 5.0,
    lookback_min: int = 15,
    cooldown_min: int = 15,
    require_regime: str | None = "SHORT_GAMMA",
    random_direction=None,
) -> list[SGSignal]:
    """bars_1m: contiguous 1-min SPX bars for ONE session (sorted).
    regime_lookup(ts_ms, spot) -> regime str or None.
    random_direction: optional callable()->bool for the random-direction control
    (True=CALLS); when set, direction is randomized instead of structural."""
    sigs: list[SGSignal] = []
    last_fire = -10**18
    for i in range(lookback_min, len(bars_1m)):
        b = bars_1m[i]
        if b.ts_ms - last_fire < cooldown_min * 60_000:
            continue
        move = b.close - bars_1m[i - lookback_min].close
        if abs(move) < move_pts:
            continue
        up = move > 0
        # confirmation: bar closes in the move's direction
        if up and b.close <= b.open:
            continue
        if (not up) and b.close >= b.open:
            continue
        reg = regime_lookup(b.ts_ms, b.close)
        if reg is None:
            continue
        if require_regime is not None and reg != require_regime:
            continue
        if random_direction is not None:
            direction = CALLS if random_direction() else PUTS
        else:
            direction = CALLS if up else PUTS
        sigs.append(SGSignal(ts_ms=b.ts_ms, direction=direction, price=b.close,
                             move_pts=move, regime=reg,
                             detail={"up_move": up}))
        last_fire = b.ts_ms
    return sigs
