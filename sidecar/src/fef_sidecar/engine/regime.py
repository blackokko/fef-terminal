"""Market regime detector.

Looks at the last N iters' inputs + verdicts and classifies the current
session 'vibe' — PIN_DAY / TREND_DAY / CHOP / ABSORPTION_HEAVY /
WALL_TEST / OPEN_DRIVE / POWER_HOUR / HIGH_VOL.

Each regime carries a recommendation: which preset to apply, which
patterns to watch, a directional bias, and a sizing suggestion. The UI
shows this prominently so you know what kind of day you're trading.

This is a heuristic system, not magic — calibrate the thresholds based
on what you actually see in live sessions.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class RegimeAssessment:
    regime: str  # see REGIMES below
    label: str
    confidence: float  # 0.0–1.0
    rationale: str
    recommended_preset_hint: str  # name of preset family (user may or may not have one with this name)
    recommended_patterns: list[str] = field(default_factory=list)
    bias: str = "NEUTRAL"  # FOLLOW_DRIFT / REVERSAL / TOWARD_MP / NEUTRAL / SKIP
    sizing: str = "STANDARD"  # FULL / STANDARD / REDUCED / TOE_DIP / STAND_DOWN
    detected_at_iter: int | None = None


# Iter sample we feed into the detector (subset of FEFInputs + verdict)
@dataclass(frozen=True)
class RegimeSample:
    spx_price: float
    drift_cum_net_m: float
    drift_bias: str  # BULLISH / BEARISH / NEUTRAL
    wall_proximity_pts: float | None
    aligned: bool
    absorption: bool
    direction: str
    confidence: str
    fe_ratio: float | None
    minutes_since_open: int
    vix: float | None


REGIMES: dict[str, dict[str, Any]] = {
    "PIN_DAY": {
        "label": "Pin Day",
        "rationale_template": "SPX range {range:.1f}pts in last {n} iters, near walls {prox_pct:.0%} of the time",
        "recommended_preset_hint": "tight pin",
        "recommended_patterns": ["Pin regime entry", "Wall test bounce"],
        "bias": "NEUTRAL",
        "sizing": "STAND_DOWN",
    },
    "TREND_DAY": {
        "label": "Trend Day",
        "rationale_template": "SPX range {range:.1f}pts, drift {drift:+.1f}M, {aligned_count}/{n} iters aligned",
        "recommended_preset_hint": "trend day",
        "recommended_patterns": ["FEF: Aligned HIGH long calls", "FEF: Aligned HIGH long puts"],
        "bias": "FOLLOW_DRIFT",
        "sizing": "FULL",
    },
    "DRIFT_BULL": {
        "label": "Bull Drift",
        "rationale_template": "Drift +{drift:.1f}M over {n} iters, bullish bias, no clear archetype",
        "recommended_preset_hint": "default",
        "recommended_patterns": ["FEF: Aligned HIGH long calls", "Wall: Bullish wall stack"],
        "bias": "FOLLOW_DRIFT",
        "sizing": "STANDARD",
    },
    "DRIFT_BEAR": {
        "label": "Bear Drift",
        "rationale_template": "Drift {drift:.1f}M over {n} iters, bearish bias, no clear archetype",
        "recommended_preset_hint": "default",
        "recommended_patterns": ["FEF: Aligned HIGH long puts", "Wall: Bearish wall stack"],
        "bias": "FOLLOW_DRIFT",
        "sizing": "STANDARD",
    },
    "CHOP": {
        "label": "Chop",
        "rationale_template": "No alignment in last {n} iters, drift weak ({drift:+.1f}M)",
        "recommended_preset_hint": "wide",
        "recommended_patterns": [],
        "bias": "SKIP",
        "sizing": "STAND_DOWN",
    },
    "ABSORPTION_HEAVY": {
        "label": "Absorption Heavy",
        "rationale_template": "{absorption_count}/{n} iters absorbing — reversal setup brewing",
        "recommended_preset_hint": "wide",
        "recommended_patterns": ["Bullish absorption setup"],
        "bias": "REVERSAL",
        "sizing": "REDUCED",
    },
    "WALL_TEST": {
        "label": "Wall Test",
        "rationale_template": "Spot pinned near dominant wall {prox_pct:.0%} of recent iters",
        "recommended_preset_hint": "tight pin",
        "recommended_patterns": ["Wall test bounce"],
        "bias": "REVERSAL",
        "sizing": "TOE_DIP",
    },
    "OPEN_DRIVE": {
        "label": "Open Drive",
        "rationale_template": "First 30min of session, SPX range {range:.1f}pts already",
        "recommended_preset_hint": "trend day",
        "recommended_patterns": ["Aligned HIGH-conviction breakout"],
        "bias": "FOLLOW_DRIFT",
        "sizing": "TOE_DIP",
    },
    "POWER_HOUR": {
        "label": "Power Hour",
        "rationale_template": "Final 90min of session — MP gravity dominant",
        "recommended_preset_hint": "tight pin",
        "recommended_patterns": ["Mean revert toward NTM MP", "Wall test bounce"],
        "bias": "TOWARD_MP",
        "sizing": "REDUCED",
    },
    "HIGH_VOL": {
        "label": "High Volatility",
        "rationale_template": "VIX {vix:.1f} — size down, expect bigger moves",
        "recommended_preset_hint": "wide",
        "recommended_patterns": ["Aligned HIGH-conviction breakout", "Wall collapse special"],
        "bias": "FOLLOW_DRIFT",
        "sizing": "REDUCED",
    },
    "UNCLEAR": {
        "label": "Unclear",
        "rationale_template": "No regime score crossed threshold — waiting for clarity",
        "recommended_preset_hint": "",
        "recommended_patterns": [],
        "bias": "NEUTRAL",
        "sizing": "STANDARD",
    },
}


class RegimeDetector:
    """Sliding window of recent iters + heuristic scoring.

    Call `push(sample)` on each FEF iter. Then `assess()` returns the
    current best-guess regime.

    Default window: 60 samples. At the standard 2-min loop cadence that's
    2 hours of context — enough to classify pin/trend/wall-test/etc.
    Was 12 (= 24 min) which was too short for stable classification.
    """

    def __init__(self, window_size: int = 60) -> None:
        self._window: deque[RegimeSample] = deque(maxlen=window_size)
        self._window_size = window_size

    def push(self, sample: RegimeSample) -> None:
        self._window.append(sample)

    def reset(self) -> None:
        self._window.clear()

    def resize(self, new_window_size: int) -> None:
        """Change the window cap at runtime. Preserves existing samples
        up to the new cap (drops oldest if shrinking)."""
        new_window_size = max(3, min(500, int(new_window_size)))
        if new_window_size == self._window_size:
            return
        new_window: deque[RegimeSample] = deque(self._window, maxlen=new_window_size)
        self._window = new_window
        self._window_size = new_window_size

    def backfill(self, samples: list[RegimeSample]) -> None:
        """Prepend historical samples (oldest first) up to the window cap.
        Called on sidecar startup so the regime is already meaningful when
        you load the app mid-day instead of waiting through warmup iters.

        Samples already in the deque are kept — backfill only fills empty
        slots on the left.
        """
        if not samples:
            return
        existing = list(self._window)
        free = self._window_size - len(existing)
        if free <= 0:
            return
        # Keep the most-recent `free` historical samples
        tail = samples[-free:] if len(samples) > free else samples
        self._window.clear()
        for s in tail:
            self._window.append(s)
        for s in existing:
            self._window.append(s)

    @property
    def n(self) -> int:
        return len(self._window)

    def assess(self) -> RegimeAssessment:
        n = len(self._window)
        if n < 3:
            return RegimeAssessment(
                regime="UNCLEAR",
                label=REGIMES["UNCLEAR"]["label"],
                confidence=0.0,
                rationale=f"warming up ({n}/3 iters needed)",
                recommended_preset_hint="",
            )

        latest = self._window[-1]
        prices = [s.spx_price for s in self._window]
        spx_range = max(prices) - min(prices)
        drift = latest.drift_cum_net_m
        # v2 fix (bug #6): unify wall-proximity boundary with FEF engine.
        # FEF uses `<= self._wall_proximity_pts` (inclusive); regime used
        # `< 15` (hardcoded exclusive). Now `<= 15` inclusive, matching FEF.
        prox_count = sum(
            1 for s in self._window if s.wall_proximity_pts is not None and s.wall_proximity_pts <= 15
        )
        prox_pct = prox_count / n
        aligned_count = sum(1 for s in self._window if s.aligned)
        absorption_count = sum(1 for s in self._window if s.absorption)
        bullish_count = sum(1 for s in self._window if s.direction == "LONG_CALLS")
        bearish_count = sum(1 for s in self._window if s.direction == "LONG_PUTS")
        minutes = latest.minutes_since_open
        vix = latest.vix

        scores: dict[str, tuple[float, dict[str, Any]]] = {}

        # OPEN_DRIVE: first 30 min + meaningful range
        if 0 <= minutes <= 30 and spx_range >= 6:
            scores["OPEN_DRIVE"] = (0.85, {"range": spx_range, "n": n})

        # POWER_HOUR: after 3pm ET (330+ minutes since 9:30)
        if minutes >= 330:
            scores["POWER_HOUR"] = (0.75, {"n": n})

        # HIGH_VOL: VIX elevated
        if vix is not None and vix > 20:
            scores["HIGH_VOL"] = (min(1.0, (vix - 20) / 10 + 0.5), {"vix": vix, "n": n})

        # PIN_DAY: tight range + frequently near walls (loosened from <10 to <15)
        if spx_range < 12 and prox_pct >= 0.4:
            score = 0.7 + min(0.3, (0.6 - spx_range / 20))
            scores["PIN_DAY"] = (score, {"range": spx_range, "n": n, "prox_pct": prox_pct})

        # WALL_TEST: heavy proximity, moderate range
        if prox_pct >= 0.5 and spx_range >= 4:
            scores["WALL_TEST"] = (0.65 + prox_pct * 0.2, {"prox_pct": prox_pct, "n": n})

        # TREND_DAY: wide range + bias direction count (alignment is too strict)
        if spx_range >= 12 and (bullish_count + bearish_count) >= max(2, n // 4):
            direction_bias = max(bullish_count, bearish_count)
            score = min(1.0, 0.55 + spx_range / 100 + direction_bias / n * 0.4)
            scores["TREND_DAY"] = (score, {
                "range": spx_range, "drift": drift,
                "aligned_count": max(aligned_count, direction_bias), "n": n,
            })

        # ABSORPTION_HEAVY: lots of absorption flags
        if absorption_count >= max(2, n // 4):
            scores["ABSORPTION_HEAVY"] = (0.65 + absorption_count / n * 0.3, {"absorption_count": absorption_count, "n": n})

        # CHOP: no clear movement
        if aligned_count == 0 and abs(drift) < 5 and absorption_count == 0 and spx_range < 8:
            scores["CHOP"] = (0.6, {"drift": drift, "n": n})

        # DRIFT_BULL / DRIFT_BEAR: catch-all for moderate directional days
        # that don't qualify as full TREND_DAY but have clear bias.
        if drift > 15 and latest.drift_bias == "BULLISH":
            scores["DRIFT_BULL"] = (0.55 + min(0.25, abs(drift) / 200), {"drift": drift, "n": n})
        elif drift < -15 and latest.drift_bias == "BEARISH":
            scores["DRIFT_BEAR"] = (0.55 + min(0.25, abs(drift) / 200), {"drift": drift, "n": n})

        if not scores:
            return RegimeAssessment(
                regime="UNCLEAR",
                label=REGIMES["UNCLEAR"]["label"],
                confidence=0.0,
                rationale=f"no regime fired — range={spx_range:.1f} drift={drift:+.1f}M prox={prox_pct:.0%} aligned={aligned_count}/{n}",
                recommended_preset_hint="",
            )

        # Pick highest-scoring regime
        best_regime = max(scores.keys(), key=lambda r: scores[r][0])
        confidence, ctx = scores[best_regime]
        meta = REGIMES[best_regime]
        rationale = meta["rationale_template"].format(**ctx)

        return RegimeAssessment(
            regime=best_regime,
            label=meta["label"],
            confidence=round(confidence, 2),
            rationale=rationale,
            recommended_preset_hint=meta["recommended_preset_hint"],
            recommended_patterns=list(meta["recommended_patterns"]),
            bias=meta["bias"],
            sizing=meta["sizing"],
        )


def now_minutes_since_open_et() -> int:
    et = timezone(timedelta(hours=-4))
    now = datetime.now(et)
    return (now.hour - 9) * 60 + (now.minute - 30)
