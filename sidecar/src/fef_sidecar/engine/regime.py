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
        "recommended_patterns": ["Aligned HIGH-conviction breakout"],
        "bias": "FOLLOW_DRIFT",
        "sizing": "FULL",
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
    """

    def __init__(self, window_size: int = 12) -> None:
        self._window: deque[RegimeSample] = deque(maxlen=window_size)
        self._window_size = window_size

    def push(self, sample: RegimeSample) -> None:
        self._window.append(sample)

    def reset(self) -> None:
        self._window.clear()

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
        prox_count = sum(
            1 for s in self._window if s.wall_proximity_pts is not None and s.wall_proximity_pts < 10
        )
        prox_pct = prox_count / n
        aligned_count = sum(1 for s in self._window if s.aligned)
        absorption_count = sum(1 for s in self._window if s.absorption)
        minutes = latest.minutes_since_open
        vix = latest.vix

        scores: dict[str, tuple[float, dict[str, Any]]] = {}

        # OPEN_DRIVE: first 30 min + meaningful range
        if 0 <= minutes <= 30 and spx_range >= 8:
            scores["OPEN_DRIVE"] = (0.85, {"range": spx_range, "n": n})

        # POWER_HOUR: after 3pm ET (210+ minutes since 9:30)
        if minutes >= 330:  # 9:30 + 5.5hr = 15:00 ET = 330 min
            scores["POWER_HOUR"] = (0.7, {"n": n})

        # HIGH_VOL: VIX elevated
        if vix is not None and vix > 20:
            scores["HIGH_VOL"] = (min(1.0, (vix - 20) / 10 + 0.5), {"vix": vix, "n": n})

        # PIN_DAY: tight range + frequently near walls
        if spx_range < 10 and prox_pct >= 0.5 and aligned_count <= 1:
            score = 0.7 + min(0.3, (0.5 - spx_range / 20))
            scores["PIN_DAY"] = (score, {"range": spx_range, "n": n, "prox_pct": prox_pct})

        # WALL_TEST: heavy proximity (but not pin-tight)
        if prox_pct >= 0.6 and spx_range >= 5:
            scores["WALL_TEST"] = (0.7, {"prox_pct": prox_pct, "n": n})

        # TREND_DAY: wide range + aligned iters + big drift
        if spx_range >= 15 and aligned_count >= max(2, n // 3) and abs(drift) >= 10:
            score = min(1.0, 0.6 + spx_range / 100 + aligned_count / n * 0.3)
            scores["TREND_DAY"] = (score, {"range": spx_range, "drift": drift, "aligned_count": aligned_count, "n": n})

        # ABSORPTION_HEAVY: lots of absorption flags
        if absorption_count >= max(2, n // 3):
            scores["ABSORPTION_HEAVY"] = (0.65 + absorption_count / n * 0.3, {"absorption_count": absorption_count, "n": n})

        # CHOP: no alignment, weak drift
        if aligned_count == 0 and abs(drift) < 5 and absorption_count == 0:
            scores["CHOP"] = (0.6, {"drift": drift, "n": n})

        if not scores:
            return RegimeAssessment(
                regime="UNCLEAR",
                label=REGIMES["UNCLEAR"]["label"],
                confidence=0.0,
                rationale=REGIMES["UNCLEAR"]["rationale_template"],
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
