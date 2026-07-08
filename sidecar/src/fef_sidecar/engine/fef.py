"""FEF (Flow Effectiveness Filter) engine.

Stateful — maintains a rolling window of the last N iters so it can compute
3-iter deltas (ΔPrice, ΔDrift), FE ratio, alignment, wall collapse, and
absorption patterns per the model documented in prior sessions.

Single-threaded — call `push(snapshot)` on each iter (every 2 min in prod),
get a FEFVerdict back. Push is synchronous and pure given internal state.
"""

from __future__ import annotations

import math
from collections import deque
from time import time

import time as _time

from fef_sidecar.db.models import FEFConfidence, FEFDirection
from fef_sidecar.engine.strikes import build_plan
from fef_sidecar.engine.types import FEFInputs, FEFVerdict
from fef_sidecar.quantdata.max_pain import compute_ntm_max_pain
from fef_sidecar.quantdata.models import MarketSnapshot


def classify_confidence(
    delta_price: float,
    delta_drift_m: float,
    fe_ratio: float,
    *,
    threshold_scale: float = 1.0,
) -> FEFConfidence:
    """THE confidence formula — module-level so non-engine consumers (the
    auto-trader's wall-collapse re-grade) share the exact same math instead
    of copying it. HIGH ALSO requires FE in a sane range (1.0 .. 5.0) —
    outside that band the 2026-05-18 data showed HIGH underperforming LOW
    because magnitude alone was the trigger. FE > 5 means drift is being
    absorbed; FE < 1 means price moved without flow confirmation. Both are
    noise.
    """
    scale = threshold_scale
    # HIGH requires: meaningful magnitude AND FE in confirmation band
    if (
        abs(delta_price) >= 5 * scale
        and abs(delta_drift_m) >= 10 * scale
        and 1.0 <= fe_ratio <= 5.0
    ):
        return FEFConfidence.HIGH
    # MEDIUM: lower magnitude OR slightly out-of-band FE
    if (
        abs(delta_price) >= 2 * scale
        and abs(delta_drift_m) >= 3 * scale
        and fe_ratio <= 8.0
    ):
        return FEFConfidence.MEDIUM
    return FEFConfidence.LOW


def demote_confidence_on_bias(
    confidence: FEFConfidence,
    direction: FEFDirection | str,
    drift_bias: str | None,
) -> tuple[FEFConfidence, bool]:
    """Drift-bias disagreement demotion — the aligned branch's one-notch
    downgrade (HIGH -> MEDIUM, MEDIUM -> LOW, LOW stays LOW), extracted so
    the auto-trader's wall-collapse re-grade applies the identical rule.
    Returns (confidence, bias_disagrees)."""
    bias = (drift_bias or "NEUTRAL").upper()
    dir_value = getattr(direction, "value", direction)
    wants_bull = dir_value == FEFDirection.LONG_CALLS.value
    bias_disagrees = (
        (wants_bull and bias == "BEARISH")
        or (not wants_bull and bias == "BULLISH")
    )
    if not bias_disagrees:
        return confidence, False
    demoted = (
        FEFConfidence.MEDIUM if confidence == FEFConfidence.HIGH else
        FEFConfidence.LOW if confidence == FEFConfidence.MEDIUM else
        confidence
    )
    return demoted, True


def formula_confidence(
    delta_price: float,
    delta_drift_m: float,
    fe_ratio: float,
    *,
    direction: FEFDirection | str,
    drift_bias: str | None,
    threshold_scale: float = 1.0,
) -> FEFConfidence:
    """classify_confidence + bias demotion in one call — what an ALIGNED
    fire's badge would be. The auto-trader uses this to re-grade
    wall-collapse verdicts, whose hardcoded HIGH badge bypasses the formula
    (registered study: wc fires -1.56/ct OOS vs formula-HIGH +27.88/ct)."""
    conf = classify_confidence(
        delta_price, delta_drift_m, fe_ratio, threshold_scale=threshold_scale,
    )
    conf, _ = demote_confidence_on_bias(conf, direction, drift_bias)
    return conf


def snapshot_to_inputs(snap: MarketSnapshot) -> FEFInputs:
    """Convert a QuantData MarketSnapshot into FEF engine inputs."""
    ntm = (
        compute_ntm_max_pain(snap.oi.rows, snap.spx_price).max_pain
        if snap.oi is not None
        else snap.max_pain.max_pain
    )

    # Expiration-bucketed exposure
    today_top_gex = snap.gex_by_expiry.top(5) if snap.gex_by_expiry else []
    today_top_dex = snap.dex_by_expiry.top(5) if snap.dex_by_expiry else []
    zero_dte_gex = snap.gex_by_expiry.zero_dte() if snap.gex_by_expiry else None
    zero_dte_dex = snap.dex_by_expiry.zero_dte() if snap.dex_by_expiry else None

    # Order-flow summary — 30-min whale/golden-sweep counts
    now_ms = snap.fetched_at_ms
    cutoff_ms = now_ms - 30 * 60 * 1000
    of_orders = snap.order_flow.orders if snap.order_flow else []
    whale_30m = sum(1 for o in of_orders if o.is_whale and o.timestamp_ms >= cutoff_ms)
    gs_30m = sum(1 for o in of_orders if o.is_golden_sweep and o.timestamp_ms >= cutoff_ms)

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
        top_cex_walls=snap.cex.top(5) if snap.cex else [],
        top_vex_walls=snap.vex.top(5) if snap.vex else [],
        top_gex_by_expiry=today_top_gex,
        top_dex_by_expiry=today_top_dex,
        zero_dte_gex_net_m=zero_dte_gex.net_m if zero_dte_gex else None,
        zero_dte_dex_net_m=zero_dte_dex.net_m if zero_dte_dex else None,
        recent_orders=list(snap.order_flow.recent(20)) if snap.order_flow else [],
        recent_whale_count_30m=whale_30m,
        recent_golden_sweep_count_30m=gs_30m,
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
        wall_proximity_skip_enabled: bool = False,
        wall_collapse_pct: float = 0.20,
        wall_collapse_floor_m: float = 5.0,
        absorption_fe_ratio_min: float = 5.0,
        flow_surge_min_m: float = 25.0,
        flow_surge_pct: float = 90.0,
        flow_surge_window: int = 120,
        flow_surge_noise_floor_m: float = 1.0,
        flow_surge_min_samples: int = 8,
        drift_sanity_ceiling_m: float = 250.0,
        flow_surge_gate_enabled: bool = True,
        threshold_scale: float = 1.0,
    ) -> None:
        """
        Args:
            history_window: 3-iter rolling delta window.
            wall_proximity_pts: skip-trade radius around dominant wall.
            wall_collapse_pct: collapse trigger threshold (e.g., 0.20 = 20%).
            wall_collapse_floor_m: minimum prior |net_m| in $M required for a
                wall collapse to fire — filters noise on minor walls
                (IMPROVEMENTS #7).
            absorption_fe_ratio_min: minimum FE ratio for absorption flag.
            flow_surge_min_m: ABSOLUTE warm-up bootstrap floor for the flow-surge
                gate, in $M — used only until ``flow_surge_min_samples`` |ΔDrift|
                readings have accumulated this session. After that the gate is the
                ADAPTIVE rolling percentile below (the fixed absolute value was
                non-stationary: the flow series grew ~10x over 2022-2026, so an
                absolute threshold over-fired in recent years).
            flow_surge_pct: percentile of the trailing |ΔDrift| distribution that
                defines a "surge" (default 90 = top decile, matching the validated
                research). The causal analog of the research's per-session p90.
            flow_surge_window: how many recent |ΔDrift| readings to keep for the
                adaptive percentile (≈ a session at 2-min cadence).
            flow_surge_noise_floor_m: small ABSOLUTE minimum (in $M, scaled) the
                adaptive threshold can never drop below, so a dead-flat tape (where
                p90≈0) can't make every blip a "surge".
            flow_surge_min_samples: minimum trailing readings before the adaptive
                percentile is trusted; below this the warm-up bootstrap is used.
            drift_sanity_ceiling_m: reject any |ΔDrift| above this (in $M, scaled)
                as a corrupted-capture GLITCH — QuantData occasionally emits a
                garbage cumulative drift (e.g. -2839M vs a normal tens-of-M scale).
                Such a reading would otherwise fire a false surge in a random
                direction (as it enters AND leaves the 3-iter window) and poison
                the adaptive percentile buffer. Default 250M is ~15-20x the live
                p99 (~15M) — far above any real surge on either data scale.
            threshold_scale: scales the magnitude thresholds for ΔPrice/ΔDrift
                (IMPROVEMENTS #2). If FEF runs at non-default cadence, set this
                to (interval_sec / 120) so thresholds stay calibrated.
        """
        self._history: deque[FEFInputs] = deque(maxlen=history_window + 1)
        self._history_window = history_window
        self._wall_proximity_pts = wall_proximity_pts
        # WALL GATE OFF BY DEFAULT (2026-07-02 validation): the proximity
        # SKIP fails the harm-reduction bar — near-wall fires (<=2.5pts) are
        # historically the LEAST bad (-$7.06/ct vs -$19.35 at 5-10pts, which
        # a 5pt gate KEEPS), a 5pt gate removes 58.6% of all fires, the
        # toward-barrier class is flat-to-POSITIVE at scale (+$17.42/ct) and
        # would have skipped +$2,039 of the week's winners, and all real
        # near-wall harm sits inside the compressed subset the CHOP gate
        # already skips. The per-family nearest-wall RANKING fix + wall
        # context on every verdict stay (decision-support); the skip itself
        # is opt-in policy (FEF_WALL_PROXIMITY_SKIP_ENABLED / this flag).
        # Revisit only the toward-NON-barrier <=2.5pt cell after
        # paper-shadow evidence.
        self._wall_proximity_skip_enabled = wall_proximity_skip_enabled
        self._wall_collapse_pct = wall_collapse_pct
        self._wall_collapse_floor_m = wall_collapse_floor_m
        self._absorption_fe_min = absorption_fe_ratio_min
        self._flow_surge_min_m = flow_surge_min_m
        self._flow_surge_pct = flow_surge_pct
        self._flow_surge_noise_floor_m = flow_surge_noise_floor_m
        self._flow_surge_min_samples = max(1, flow_surge_min_samples)
        self._drift_sanity_ceiling_m = drift_sanity_ceiling_m
        # RETIRED 2026-06-30: the flow-surge directional edge is a spot-reconstruction
        # artifact (see project-flow-surge-edge). When False, the engine still computes
        # everything (drift, bias, regime, fe_ratio) for context/logging but does NOT
        # emit an actionable ALIGNED/wall-collapse directional verdict — the flow-surge
        # thesis no longer drives trades. Live wiring (state.py) sets this False.
        self._flow_surge_gate_enabled = flow_surge_gate_enabled
        # Trailing distribution of |ΔDrift| readings → adaptive surge gate.
        self._drift_mag_history: deque[float] = deque(maxlen=max(1, flow_surge_window))
        self._threshold_scale = max(0.01, threshold_scale)

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
        self._drift_mag_history.clear()

    # ------------------------------------------------------------------
    # Flow-surge gate (adaptive)
    # ------------------------------------------------------------------

    @staticmethod
    def _percentile(values: list[float], pct: float) -> float:
        """Linear-interpolated percentile (numpy 'linear' method), pure-python so
        the engine stays dependency-free in the hot path."""
        if not values:
            return 0.0
        s = sorted(values)
        if len(s) == 1:
            return s[0]
        k = (len(s) - 1) * (pct / 100.0)
        lo = math.floor(k)
        hi = math.ceil(k)
        if lo == hi:
            return s[int(k)]
        return s[lo] + (s[hi] - s[lo]) * (k - lo)

    def _surge_threshold(self) -> float:
        """The |ΔDrift| (in $M) a reading must exceed to count as a flow surge.

        FIX (2026-06-30): the old fixed-25M absolute floor was non-stationary — the
        signed-premium flow series grew ~10x over 2022-2026, so an absolute gate
        over-fired in rich-flow years and under-fired in lean ones. We now use a
        TRAILING ROLLING PERCENTILE of recent |ΔDrift| (the causal analog of the
        validated research's per-session p90), bootstrapped by the absolute floor
        until enough samples accumulate, and clamped to a small absolute noise floor
        so a dead-flat tape can't turn every blip into a 'surge'. The percentile is
        computed from already-measured ΔDrift magnitudes, which embed the live
        cadence — so it is NOT re-scaled by threshold_scale (only the absolute
        bootstrap/noise floors are).
        """
        scale = self._threshold_scale
        if len(self._drift_mag_history) >= self._flow_surge_min_samples:
            adaptive = self._percentile(list(self._drift_mag_history), self._flow_surge_pct)
            return max(adaptive, self._flow_surge_noise_floor_m * scale)
        return self._flow_surge_min_m * scale  # warm-up bootstrap

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

        # GLITCH GUARD (2026-06-30): reject a corrupted-capture ΔDrift before it can
        # fire a false surge OR poison the adaptive-percentile buffer. A garbage
        # cumulative drift (e.g. QuantData's -2839M on 2026-06-30) produces a huge
        # ΔDrift as it enters AND again as it leaves the 3-iter window — two
        # wrong-direction fires from one bad tick. Bail out without firing and
        # WITHOUT recording it into the surge distribution.
        drift_ceiling = self._drift_sanity_ceiling_m * self._threshold_scale
        if abs(delta_drift_m) > drift_ceiling:
            return FEFVerdict(
                direction=FEFDirection.WAIT,
                confidence=FEFConfidence.NONE,
                reasoning=(
                    f"DRIFT GLITCH REJECTED — ΔDrift {delta_drift_m:+.1f}M exceeds the "
                    f"sanity ceiling {drift_ceiling:.0f}M (corrupted capture). "
                    "Not firing; not recorded in the surge distribution."
                ),
                delta_price=delta_price,
                delta_drift_m=delta_drift_m,
            )

        # v2 fix (bug #1): the old `max(abs(delta_price), 0.1)` floor inflated
        # fe_ratio whenever price barely moved — e.g. dp=0.05, dDrift=1M gave
        # fe_ratio=10.0, triggering false-positive absorption flags structurally.
        # Use an additive noise floor scaled by typical SPX 2-iter range
        # (~1pt at 120s cadence × threshold_scale) so fe_ratio scales smoothly.
        noise_floor_pts = 1.0 * self._threshold_scale
        denom = abs(delta_price) + noise_floor_pts
        fe_ratio = abs(delta_drift_m) / denom

        same_sign = (
            (delta_price > 0 and delta_drift_m > 0)
            or (delta_price < 0 and delta_drift_m < 0)
        )
        # v2 fix (bug #5): scale the alignment thresholds by threshold_scale
        # so non-default cadences (e.g. 60s at scale=0.5) don't require the
        # same magnitude as 120s. The 1.0pt baseline applies at scale=1.0.
        align_thresh_pts = 1.0 * self._threshold_scale
        # FLOW-SURGE GATE (validated 2026-06-30, made ADAPTIVE 2026-06-30). The old
        # 1.0M floor fired on the majority of all readings and diluted the edge to
        # breakeven; the directional edge lives ONLY in the top-decile surge. The
        # first fix used a fixed 25M absolute floor, but that is non-stationary (the
        # flow series grew ~10x over 2022-2026). Now the gate is a TRAILING ROLLING
        # PERCENTILE of recent |ΔDrift| (see _surge_threshold): the causal analog of
        # the research's per-session p90. The 25M absolute value survives only as the
        # warm-up bootstrap + a small noise floor. Compute the threshold from PRIOR
        # readings, THEN record this reading's magnitude (keeps the gate causal).
        align_thresh_drift_m = self._surge_threshold()
        self._drift_mag_history.append(abs(delta_drift_m))
        aligned = (
            same_sign
            and abs(delta_price) >= align_thresh_pts
            and abs(delta_drift_m) >= align_thresh_drift_m
        )

        # Absorption: drift moved a lot but price didn't follow.
        # v2 fix: the extreme-FE clause uses a raw ratio (without the noise
        # floor) so it remains reachable after the v2 denominator change.
        # The primary clause uses the smoothed fe_ratio as before.
        fe_ratio_raw = (
            abs(delta_drift_m) / abs(delta_price) if abs(delta_price) > 0.01 else float("inf")
        )
        drift_floor_m = 3.0 * self._threshold_scale
        absorption = (
            (fe_ratio >= self._absorption_fe_min and not same_sign and abs(delta_drift_m) >= drift_floor_m)
            or (fe_ratio_raw >= 10.0 and abs(delta_drift_m) >= 5.0)
        )

        nearest_strike, nearest_dist, nearest_family, nearest_net_m = (
            self._nearest_dominant_wall_detail(current)
        )
        # Nearness is always computed (context on every verdict); the SKIP
        # itself is opt-in policy — OFF by default per the 2026-07-02
        # validation (chop gate owns the harm region; see __init__ note).
        wall_proximity_near = (
            nearest_dist is not None and nearest_dist <= self._wall_proximity_pts
        )
        wall_proximity_skip = wall_proximity_near and self._wall_proximity_skip_enabled
        wall_collapse, collapse_dir = self._detect_wall_collapse()

        # Wall collapse REQUIRES aligned confluence. Today's data shows
        # wall_collapse-alone at 15% win, wall_collapse + aligned at 42%.
        # Without aligned drift agreement, the collapse signal is noise.
        if (wall_collapse and collapse_dir is not None and aligned
                and self._flow_surge_gate_enabled):
            aligned_dir = (
                FEFDirection.LONG_CALLS if delta_price > 0 else FEFDirection.LONG_PUTS
            )
            if aligned_dir == collapse_dir:
                return self._build_actionable(
                    current=current,
                    direction=collapse_dir,
                    confidence=FEFConfidence.HIGH,
                    reasoning=(
                        f"WALL COLLAPSE + ALIGNED {collapse_dir.value} — top wall lost "
                        f">{int(self._wall_collapse_pct * 100)}% exposure in 1 iter AND "
                        f"ΔPrice {delta_price:+.2f}pts confirms direction."
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
                    nearest_wall_family=nearest_family,
                )

        if wall_proximity_skip:
            return FEFVerdict(
                direction=FEFDirection.WAIT,
                confidence=FEFConfidence.NONE,
                reasoning=(
                    f"WALL PROXIMITY SKIP — spot ${current.spx_price:.2f} within "
                    f"{self._wall_proximity_pts:.1f}pts of {nearest_family} "
                    f"{nearest_strike:.0f} ({nearest_net_m:+,.0f}M) "
                    f"{nearest_dist:.1f}pts away. Wait for clear break or rejection."
                ),
                delta_price=delta_price,
                delta_drift_m=delta_drift_m,
                fe_ratio=fe_ratio,
                aligned=aligned,
                absorption=absorption,
                wall_proximity_skip=True,
                nearest_wall_strike=nearest_strike,
                nearest_wall_distance=nearest_dist,
                nearest_wall_family=nearest_family,
                secondary_scores=self._secondary_scores(current),
            )

        if aligned and not self._flow_surge_gate_enabled:
            # Edge retired — log the would-be fire but do NOT act on it.
            would_be = "LONG_CALLS" if delta_price > 0 else "LONG_PUTS"
            return FEFVerdict(
                direction=FEFDirection.WAIT,
                confidence=FEFConfidence.NONE,
                reasoning=(
                    f"FLOW-SURGE GATE DISABLED — would be ALIGNED {would_be} "
                    f"(ΔPrice {delta_price:+.2f}pts, ΔDrift {delta_drift_m:+.2f}M) but the "
                    "flow-surge directional edge was RETIRED 2026-06-30 (spot-reconstruction "
                    "artifact). Not firing."
                ),
                delta_price=delta_price,
                delta_drift_m=delta_drift_m,
                fe_ratio=fe_ratio,
                aligned=True,
                absorption=False,
                nearest_wall_strike=nearest_strike,
                nearest_wall_distance=nearest_dist,
                nearest_wall_family=nearest_family,
                secondary_scores=self._secondary_scores(current),
            )

        if aligned:
            direction = (
                FEFDirection.LONG_CALLS if delta_price > 0 else FEFDirection.LONG_PUTS
            )
            confidence = self._classify_confidence(delta_price, delta_drift_m, fe_ratio)
            # Drift bias agreement filter — independent confluence signal.
            # If FEF says LONG_CALLS but the drift_bias field disagrees (BEARISH),
            # demote one notch (module-level helper so the auto-trader's
            # wall-collapse re-grade applies the identical rule). We KEEP the
            # signal (user wanted volume) but downgrade conviction so the
            # auto-trade confidence floor can gate it.
            confidence, bias_disagrees = demote_confidence_on_bias(
                confidence, direction, current.drift_bias,
            )
            reasoning = (
                f"ALIGNED {direction.value} — ΔPrice {delta_price:+.2f}pts, "
                f"ΔDrift {delta_drift_m:+.2f}M, FE {fe_ratio:.2f}, "
                f"bias={current.drift_bias}"
                + (" (bias-disagrees, demoted)" if bias_disagrees else "")
                + "."
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
                nearest_wall_family=nearest_family,
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
                nearest_wall_family=nearest_family,
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
            nearest_wall_family=nearest_family,
            secondary_scores=self._secondary_scores(current),
        )

    # ------------------------------------------------------------------
    # Sub-rules
    # ------------------------------------------------------------------

    def _nearest_dominant_wall_detail(
        self, inputs: FEFInputs
    ) -> tuple[float | None, float | None, str | None, float | None]:
        """Return (strike, distance, family, net_m) of the nearest significant wall.

        BUG FIX 2026-07-02 — the wall-proximity gate had NEVER functioned:
        the old code ranked GEX top-5 + DEX top-5 TOGETHER by abs(net_m),
        but the families live on incomparable scales (GEX ~1e4 M vs DEX
        ~1e7 M), so ranked[0] was ALWAYS a far-OTM DEX monster (e.g. strike
        8000, 558pts from spot) and wall_proximity_skip could never trigger.
        Live proof 2026-07-02 19:06Z: fired 7440P at spot 7441.7 with GEX
        walls 7430 (-10,016M) / 7440 (-9,800M) / 7435 (-7,653M) in its own
        inputs — 1.7pts from the 7440 wall — and the gate passed because
        'nearest dominant' was DEX 8000.

        Intent per the docstring ('most dominant wall NEAR SPOT'): dominance
        ranking only means anything WITHIN a family's own units — and the
        top_gex_walls / top_dex_walls inputs are already each family's top-5
        by magnitude. So proximity = the wall NEAREST to spot among the
        top-5 GEX walls, likewise among the top-5 DEX walls, and the closer
        of those two wins. (Equivalently: nearest-by-distance across both
        per-family top-5 lists, with the family tracked for logging.)
        """
        best_strike: float | None = None
        best_dist: float | None = None
        best_family: str | None = None
        best_net_m: float | None = None
        for family, walls in (("GEX", inputs.top_gex_walls), ("DEX", inputs.top_dex_walls)):
            for w in walls:
                dist = abs(inputs.spx_price - w.strike)
                if best_dist is None or dist < best_dist:
                    best_strike, best_dist = w.strike, dist
                    best_family, best_net_m = family, w.net_m
        return best_strike, best_dist, best_family, best_net_m

    def _nearest_dominant_wall(
        self, inputs: FEFInputs
    ) -> tuple[float | None, float | None]:
        """(strike, distance) of the nearest significant wall — thin wrapper
        kept for the original signature; see _nearest_dominant_wall_detail."""
        strike, dist, _family, _net_m = self._nearest_dominant_wall_detail(inputs)
        return strike, dist

    def _detect_wall_collapse(self) -> tuple[bool, FEFDirection | None]:
        """Detect whether the dominant wall lost >threshold exposure since last iter.

        Returns (collapsed, direction_to_fire). Direction reflects wall TYPE
        (call vs put via sign of net_m) AND position vs spot — only an
        out-of-the-money wall acting as resistance/support is actionable
        when it collapses. Confluence with drift bias is required so the
        trigger can't fire AGAINST trend (the 2026-05-18 7410C blowup).

        Mapping:
          Call wall above spot collapses → resistance gone → LONG_CALLS
            (only if drift_cum_net_m is non-negative)
          Put wall below spot collapses → support gone → LONG_PUTS
            (only if drift_cum_net_m is non-positive)
          ITM wall collapses (call below / put above spot) → not directional
        """
        # 2-iter persistence: require collapse signal to hold across two
        # comparisons (iter[-2]→iter[-1] AND iter[-3]→iter[-2]). On a 120s
        # cadence a 1-iter 20% wall drop is borderline indistinguishable
        # from QuantData snapshot noise. Standard flow-system noise filter.
        if len(self._history) < 3:
            return False, None

        prev = self._history[-2]
        prev2 = self._history[-3]
        cur = self._history[-1]

        def _check_collapse_walls(older_walls, newer_walls) -> bool:
            """True if newer iter shows >threshold loss vs older iter at the
            dominant wall of `older`."""
            if not older_walls or not newer_walls:
                return False
            older_top = max(older_walls, key=lambda w: abs(w.net_m))
            newer_same = next(
                (w for w in newer_walls if abs(w.strike - older_top.strike) < 1.0),
                None,
            )
            if newer_same is None:
                return True  # wall fell out of top 5 entirely
            older_abs = abs(older_top.net_m)
            newer_abs = abs(newer_same.net_m)
            if older_abs < self._wall_collapse_floor_m:
                return False
            loss_pct = (older_abs - newer_abs) / older_abs if older_abs > 0 else 0.0
            return loss_pct >= self._wall_collapse_pct

        # Check GEX wall collapse (2-iter persistence)
        gex_collapsed = (
            prev.top_gex_walls and cur.top_gex_walls and prev2.top_gex_walls
            and _check_collapse_walls(prev.top_gex_walls, cur.top_gex_walls)
            and _check_collapse_walls(prev2.top_gex_walls, prev.top_gex_walls)
        )

        # Check DEX wall collapse (2-iter persistence)
        dex_collapsed = (
            prev.top_dex_walls and cur.top_dex_walls and prev2.top_dex_walls
            and _check_collapse_walls(prev.top_dex_walls, cur.top_dex_walls)
            and _check_collapse_walls(prev2.top_dex_walls, prev.top_dex_walls)
        )

        if not gex_collapsed and not dex_collapsed:
            return False, None

        # Use the prior iter's top wall from whichever source collapsed as
        # the reference for direction.  If both collapsed, pick the one with
        # the larger dominant wall (more meaningful collapse).
        if gex_collapsed and dex_collapsed:
            gex_top = max(prev.top_gex_walls, key=lambda w: abs(w.net_m))
            dex_top = max(prev.top_dex_walls, key=lambda w: abs(w.net_m))
            prior_top = gex_top if abs(gex_top.net_m) >= abs(dex_top.net_m) else dex_top
        elif gex_collapsed:
            prior_top = max(prev.top_gex_walls, key=lambda w: abs(w.net_m))
        else:
            prior_top = max(prev.top_dex_walls, key=lambda w: abs(w.net_m))

        # Direction by wall TYPE + position
        is_call_wall = prior_top.net_m > 0
        is_above_spot = prior_top.strike > cur.spx_price

        if is_call_wall and is_above_spot:
            direction = FEFDirection.LONG_CALLS
        elif (not is_call_wall) and (not is_above_spot):
            direction = FEFDirection.LONG_PUTS
        else:
            # ITM wall collapse — call below or put above spot. Not directional.
            return False, None

        # Confluence with drift bias. If drift is significantly against the
        # wall-collapse direction, skip the trigger entirely — let other
        # branches (aligned / absorption / chop) handle the iter.
        drift = cur.drift_cum_net_m
        drift_threshold = self._wall_collapse_floor_m * 0.5
        if direction == FEFDirection.LONG_CALLS and drift < -drift_threshold:
            return False, None
        if direction == FEFDirection.LONG_PUTS and drift > drift_threshold:
            return False, None

        return True, direction

    def _classify_confidence(
        self,
        delta_price: float,
        delta_drift_m: float,
        fe_ratio: float,
    ) -> FEFConfidence:
        """Confidence scoring — delegates to the module-level
        classify_confidence so the formula has exactly one home (the
        auto-trader's wall-collapse re-grade imports the same function)."""
        return classify_confidence(
            delta_price, delta_drift_m, fe_ratio,
            threshold_scale=self._threshold_scale,
        )

    @staticmethod
    def _qty_for_confidence(confidence: FEFConfidence) -> int:
        """IMPROVEMENTS #5: suggest position size based on confidence.

        Defaults match user-trading-style memory (typical 10-20 contracts).
        HIGH = 15 (full size), MEDIUM = 10, LOW = 5 (scale-in toe-dip).
        """
        return {
            FEFConfidence.HIGH: 15,
            FEFConfidence.MEDIUM: 10,
            FEFConfidence.LOW: 5,
        }.get(confidence, 0)

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
        nearest_wall_family: str | None = None,
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
            suggested_qty=self._qty_for_confidence(confidence),
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
            nearest_wall_family=nearest_wall_family,
            secondary_scores=self._secondary_scores(current),
        )
