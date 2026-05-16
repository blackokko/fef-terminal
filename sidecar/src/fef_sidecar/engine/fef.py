"""FEF (Flow Effectiveness Filter) engine.

Stateful — maintains a rolling window of the last N iters so it can compute
3-iter deltas (ΔPrice, ΔDrift), FE ratio, alignment, wall collapse, and
absorption patterns per the model documented in prior sessions.

Single-threaded — call `push(snapshot)` on each iter (every 2 min in prod),
get a FEFVerdict back. Push is synchronous and pure given internal state.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from time import time

from fef_sidecar.db.models import FEFConfidence, FEFDirection
from fef_sidecar.engine.strikes import build_plan
from fef_sidecar.engine.types import FEFInputs, FEFVerdict
from fef_sidecar.quantdata.max_pain import compute_ntm_max_pain
from fef_sidecar.quantdata.models import ExposureRow, MarketSnapshot


def snapshot_to_inputs(snap: MarketSnapshot) -> FEFInputs:
    """Convert a QuantData MarketSnapshot into FEF engine inputs."""
    ntm = (
        compute_ntm_max_pain(snap.oi.rows, snap.spx_price).max_pain
        if snap.oi is not None
        else snap.max_pain.max_pain
    )
    return FEFInputs(
        timestamp_ms=snap.fetched_at_ms,
        spx_price=snap.spx_price,
        drift_cum_net_m=snap.drift.cumulative_net_m,
        drift_bias=snap.drift.bias,
        top_gex_walls=snap.gex.top(5),
        top_dex_walls=snap.dex.top(5),
        ntm_max_pain=ntm,
        raw_max_pain=snap.max_pain.max_pain,
        aggression_call_pct=snap.aggression.call.aggression_pct if snap.aggression else None,
        aggression_put_pct=snap.aggression.put.aggression_pct if snap.aggression else None,
        iv_rank_call_pct=snap.iv_rank.call.ivr_pct if snap.iv_rank and snap.iv_rank.call else None,
        iv_rank_put_pct=snap.iv_rank.put.ivr_pct if snap.iv_rank and snap.iv_rank.put else None,
    )


class FEFEngine:
    """Rolling-window FEF model.

    Args:
        history_window: How many prior iters to retain for delta calc.
            Default 3 → engine computes ΔPrice/ΔDrift over the 3-iter span.
        wall_proximity_pts: Skip-trade radius around the dominant nearby wall.
        wall_collapse_pct: Threshold for wall-collapse special trigger.
    """

    def __init__(
        self,
        *,
        history_window: int = 3,
        wall_proximity_pts: float = 5.0,
        wall_collapse_pct: float = 0.20,
        absorption_fe_ratio_min: float = 5.0,
    ) -> None:
        self._history: deque[FEFInputs] = deque(maxlen=history_window + 1)
        self._history_window = history_window
        self._wall_proximity_pts = wall_proximity_pts
        self._wall_collapse_pct = wall_collapse_pct
        self._absorption_fe_min = absorption_fe_ratio_min

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push(self, inputs: FEFInputs) -> FEFVerdict:
        """Add an iter and return the FEF verdict."""
        self._history.append(inputs)

        # Need full window + current to compute deltas
        if len(self._history) <= self._history_window:
            return FEFVerdict(
                direction=FEFDirection.WAIT,
                confidence=FEFConfidence.NONE,
                reasoning=(
                    f"warming up — need {self._history_window + 1} iters, "
                    f"have {len(self._history)}"
                ),
            )

        return self._evaluate()

    def reset(self) -> None:
        """Clear history (e.g. on new session)."""
        self._history.clear()

    @property
    def history_size(self) -> int:
        return len(self._history)

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def _evaluate(self) -> FEFVerdict:
        current = self._history[-1]
        oldest = self._history[0]  # exactly history_window iters ago

        delta_price = current.spx_price - oldest.spx_price
        delta_drift_m = current.drift_cum_net_m - oldest.drift_cum_net_m

        denom = max(abs(delta_price), 0.1)
        fe_ratio = abs(delta_drift_m) / denom

        same_sign = (
            (delta_price > 0 and delta_drift_m > 0)
            or (delta_price < 0 and delta_drift_m < 0)
        )
        aligned = same_sign and abs(delta_price) >= 1.0 and abs(delta_drift_m) >= 1.0

        absorption = (
            fe_ratio >= self._absorption_fe_min
            and not same_sign
            and abs(delta_drift_m) >= 3.0
        )

        # Risk filters
        nearest_strike, nearest_dist = self._nearest_dominant_wall(current)
        wall_proximity_skip = (
            nearest_dist is not None and nearest_dist <= self._wall_proximity_pts
        )
        wall_collapse, collapse_dir = self._detect_wall_collapse()

        # Direction decision
        if wall_collapse and collapse_dir is not None:
            return self._build_actionable(
                current=current,
                direction=collapse_dir,
                confidence=FEFConfidence.HIGH,
                reasoning=(
                    f"WALL COLLAPSE — top wall lost >{int(self._wall_collapse_pct * 100)}% "
                    f"exposure in 1 iter. Fade in direction of collapse."
                ),
                delta_price=delta_price,
                delta_drift_m=delta_drift_m,
                fe_ratio=fe_ratio,
                aligned=aligned,
                absorption=absorption,
                wall_proximity_skip=False,
                wall_collapse_trigger=True,
                nearest_wall_strike=nearest_strike,
                nearest_wall_distance=nearest_dist,
            )

        if wall_proximity_skip:
            return FEFVerdict(
                direction=FEFDirection.WAIT,
                confidence=FEFConfidence.NONE,
                reasoning=(
                    f"WALL PROXIMITY SKIP — spot ${current.spx_price:.2f} within "
                    f"{nearest_dist:.1f}pts of dominant wall at ${nearest_strike:.0f}. "
                    "Wait for clear break or rejection."
                ),
                delta_price=delta_price,
                delta_drift_m=delta_drift_m,
                fe_ratio=fe_ratio,
                aligned=aligned,
                absorption=absorption,
                wall_proximity_skip=True,
                nearest_wall_strike=nearest_strike,
                nearest_wall_distance=nearest_dist,
                secondary_scores=self._secondary_scores(current),
            )

        if aligned:
            direction = (
                FEFDirection.LONG_CALLS if delta_price > 0 else FEFDirection.LONG_PUTS
            )
            confidence = self._classify_confidence(delta_price, delta_drift_m, fe_ratio)
            reasoning = (
                f"ALIGNED {direction.value} — ΔPrice {delta_price:+.2f}pts, "
                f"ΔDrift {delta_drift_m:+.2f}M, FE {fe_ratio:.2f}, "
                f"bias={current.drift_bias}."
            )
            return self._build_actionable(
                current=current,
                direction=direction,
                confidence=confidence,
                reasoning=reasoning,
                delta_price=delta_price,
                delta_drift_m=delta_drift_m,
                fe_ratio=fe_ratio,
                aligned=True,
                absorption=False,
                wall_proximity_skip=False,
                wall_collapse_trigger=False,
                nearest_wall_strike=nearest_strike,
                nearest_wall_distance=nearest_dist,
            )

        # Absorption flagged but we don't fire yet — wait for confirmation
        if absorption:
            absorbed_direction = (
                FEFDirection.LONG_CALLS if delta_drift_m > 0 else FEFDirection.LONG_PUTS
            )
            return FEFVerdict(
                direction=FEFDirection.WAIT,
                confidence=FEFConfidence.LOW,
                reasoning=(
                    f"ABSORPTION — drift {delta_drift_m:+.2f}M but price only "
                    f"{delta_price:+.2f}pts (FE {fe_ratio:.2f}). "
                    f"Watch for {absorbed_direction.value} reversal confirmation."
                ),
                delta_price=delta_price,
                delta_drift_m=delta_drift_m,
                fe_ratio=fe_ratio,
                aligned=False,
                absorption=True,
                nearest_wall_strike=nearest_strike,
                nearest_wall_distance=nearest_dist,
                secondary_scores=self._secondary_scores(current),
            )

        return FEFVerdict(
            direction=FEFDirection.WAIT,
            confidence=FEFConfidence.NONE,
            reasoning=(
                f"chop — ΔPrice {delta_price:+.2f}pts, ΔDrift {delta_drift_m:+.2f}M, "
                f"FE {fe_ratio:.2f}, no edge."
            ),
            delta_price=delta_price,
            delta_drift_m=delta_drift_m,
            fe_ratio=fe_ratio,
            aligned=False,
            absorption=False,
            nearest_wall_strike=nearest_strike,
            nearest_wall_distance=nearest_dist,
            secondary_scores=self._secondary_scores(current),
        )

    # ------------------------------------------------------------------
    # Sub-rules
    # ------------------------------------------------------------------

    def _nearest_dominant_wall(
        self, inputs: FEFInputs
    ) -> tuple[float | None, float | None]:
        """Return (strike, distance) for the most dominant wall near spot.

        Dominance = abs(net_m). Considers GEX top-5 + DEX top-5.
        """
        candidates: Iterable[ExposureRow] = list(inputs.top_gex_walls) + list(inputs.top_dex_walls)
        ranked = sorted(candidates, key=lambda w: abs(w.net_m), reverse=True)
        if not ranked:
            return None, None
        top = ranked[0]
        return top.strike, abs(inputs.spx_price - top.strike)

    def _detect_wall_collapse(self) -> tuple[bool, FEFDirection | None]:
        """Detect whether the dominant wall lost >threshold exposure since last iter.

        Returns (collapsed, direction_to_fire). Direction is away from the
        collapsed wall: if a call wall above us collapsed, expect upward break.
        """
        if len(self._history) < 2:
            return False, None

        prev = self._history[-2]
        cur = self._history[-1]

        if not prev.top_gex_walls or not cur.top_gex_walls:
            return False, None

        # Track the prior iter's #1 wall and see if its exposure dropped
        prior_top = max(prev.top_gex_walls, key=lambda w: abs(w.net_m))
        current_same = next(
            (w for w in cur.top_gex_walls if abs(w.strike - prior_top.strike) < 1.0),
            None,
        )
        if current_same is None:
            # Wall fell out of top 5 entirely
            direction = (
                FEFDirection.LONG_CALLS if prior_top.strike > cur.spx_price else FEFDirection.LONG_PUTS
            )
            return True, direction

        prior_abs = abs(prior_top.net_m)
        cur_abs = abs(current_same.net_m)
        if prior_abs <= 0:
            return False, None
        loss_pct = (prior_abs - cur_abs) / prior_abs
        if loss_pct < self._wall_collapse_pct:
            return False, None

        # Wall collapsed — fire in direction away from it
        direction = (
            FEFDirection.LONG_CALLS if prior_top.strike > cur.spx_price else FEFDirection.LONG_PUTS
        )
        return True, direction

    def _classify_confidence(
        self,
        delta_price: float,
        delta_drift_m: float,
        fe_ratio: float,
    ) -> FEFConfidence:
        if abs(delta_price) >= 5 and abs(delta_drift_m) >= 10 and fe_ratio >= 1.0:
            return FEFConfidence.HIGH
        if abs(delta_price) >= 2 and abs(delta_drift_m) >= 3:
            return FEFConfidence.MEDIUM
        return FEFConfidence.LOW

    def _secondary_scores(self, inputs: FEFInputs) -> dict[str, float]:
        """6-factor secondary scorer (informational, not gating)."""
        mp_dist = abs(inputs.spx_price - inputs.ntm_max_pain)
        mp_gravity = max(0.0, 1.0 - mp_dist / 50.0)  # 1.0 at MP, 0.0 at 50pts away

        drift_score = 0.5
        if inputs.drift_bias == "BULLISH":
            drift_score = 0.8
        elif inputs.drift_bias == "BEARISH":
            drift_score = 0.2

        if inputs.aggression_call_pct is not None and inputs.aggression_put_pct is not None:
            agg = (inputs.aggression_call_pct - inputs.aggression_put_pct) / 100.0
            aggression = max(-1.0, min(1.0, agg))
        else:
            aggression = 0.0

        # Wall structure: top GEX wall's signed net (positive = call wall = floor)
        gex_score = 0.0
        if inputs.top_gex_walls:
            top = max(inputs.top_gex_walls, key=lambda w: abs(w.net_m))
            gex_score = 1.0 if top.net_m > 0 else -1.0

        # DEX floor: top DEX call wall acts as floor; top put wall as ceiling
        dex_floor = 0.0
        if inputs.top_dex_walls:
            top = max(inputs.top_dex_walls, key=lambda w: abs(w.net_m))
            dex_floor = 1.0 if top.net_m > 0 else -1.0

        # Price action: short-term trend over the buffer
        price_action = 0.0
        if len(self._history) >= 2:
            recent = list(self._history)
            net_move = recent[-1].spx_price - recent[0].spx_price
            price_action = max(-1.0, min(1.0, net_move / 20.0))

        return {
            "mp_gravity": round(mp_gravity, 3),
            "drift": round(drift_score, 3),
            "aggression": round(aggression, 3),
            "gex_walls": round(gex_score, 3),
            "dex_floor": round(dex_floor, 3),
            "price_action": round(price_action, 3),
        }

    def _build_actionable(
        self,
        *,
        current: FEFInputs,
        direction: FEFDirection,
        confidence: FEFConfidence,
        reasoning: str,
        delta_price: float,
        delta_drift_m: float,
        fe_ratio: float,
        aligned: bool,
        absorption: bool,
        wall_proximity_skip: bool,
        wall_collapse_trigger: bool,
        nearest_wall_strike: float | None,
        nearest_wall_distance: float | None,
    ) -> FEFVerdict:
        plan = build_plan(
            spot=current.spx_price,
            direction=direction,
            top_gex_walls=current.top_gex_walls,
            top_dex_walls=current.top_dex_walls,
            ntm_max_pain=current.ntm_max_pain,
        )
        return FEFVerdict(
            direction=direction,
            confidence=confidence,
            reasoning=reasoning,
            strike_rec=plan.strike_rec,
            t1=plan.t1,
            t2=plan.t2,
            t3=plan.t3,
            stop=plan.stop,
            delta_price=delta_price,
            delta_drift_m=delta_drift_m,
            fe_ratio=fe_ratio,
            aligned=aligned,
            absorption=absorption,
            wall_proximity_skip=wall_proximity_skip,
            wall_collapse_trigger=wall_collapse_trigger,
            nearest_wall_strike=nearest_wall_strike,
            nearest_wall_distance=nearest_wall_distance,
            secondary_scores=self._secondary_scores(current),
        )
