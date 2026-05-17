"""Day-archetype generators for richer mock testing.

Each archetype is a pure function `(seed: int) -> list[DayFrame]` that
produces a full simulated SPX 0DTE session. Picks the archetype via the
FEF_MOCK_ARCHETYPE env var (one of: 5_15_REPLAY / STEADY_BULL /
STEADY_BEAR / REVERSAL / PURE_CHOP / FED_DAY / GAP_AND_FADE / QUAD_WITCHING).

Plus a stochastic mode: set FEF_MOCK_SEED=0 to get a fresh time-based seed
each run instead of deterministic seed=42.

Volume profile + aggression swings are layered on top of each archetype's
price/drift shape so the chart and aggression panel feel realistic.
"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class DayFrame:
    spx: float
    drift_cum_net_m: float
    drift_bias: str
    minutes_since_open: int
    vix: float
    volume: int
    aggression_call_pct: float
    aggression_put_pct: float
    label: str


def _volume_profile(minute: int) -> float:
    """U-shape volume multiplier: spike at open (0-30), spike at close
    (300-390), lull midday. Returns 0.4x to 3.0x."""
    if minute <= 30:
        return 2.0 + (30 - minute) / 30 * 1.0  # 3.0 at open, decaying
    if minute >= 330:
        return 1.5 + (minute - 330) / 60 * 1.0  # ramps from 1.5 to 2.5
    # Midday lull
    midday_dist = abs(minute - 180)
    return max(0.4, 1.0 - midday_dist / 150 * 0.4)


def _make_frame(
    spx: float, drift: float, bias: str, minute: int, vix: float, label: str,
    call_aggr: float, put_aggr: float, rng: random.Random,
) -> DayFrame:
    base_vol = int(1500 * _volume_profile(minute))
    volume = base_vol + rng.randint(-200, 400)
    return DayFrame(
        spx=round(spx, 2),
        drift_cum_net_m=round(drift, 2),
        drift_bias=bias,
        minutes_since_open=minute,
        vix=round(vix, 2),
        volume=volume,
        aggression_call_pct=round(call_aggr, 1),
        aggression_put_pct=round(put_aggr, 1),
        label=label,
    )


# ---------- archetype generators ----------


def _5_15_replay(rng: random.Random) -> list[DayFrame]:
    """Original 8-phase script: open drive -> trend -> absorption -> pin ->
    wall test -> high vol -> chop -> power hour."""
    frames: list[DayFrame] = []
    base_spx = 7400.0
    cum_drift = 0.0

    # OPEN_DRIVE
    for i in range(15):
        spx = base_spx + i * 1.5 + rng.uniform(-0.4, 0.4)
        cum_drift += 1.5 + rng.uniform(0, 1.0)
        frames.append(_make_frame(spx, cum_drift, "BULLISH", i * 2, 14.0,
                                  "OPEN_DRIVE", 45.0 + rng.uniform(-1, 1),
                                  38.0 + rng.uniform(-1, 1), rng))
    # TREND_DAY
    for i in range(25):
        spx = 7422 + i * 0.7 + math.sin(i * 0.4) * 2.0 + rng.uniform(-0.3, 0.3)
        cum_drift += 1.2 + rng.uniform(-0.2, 0.8)
        frames.append(_make_frame(spx, cum_drift, "BULLISH", 30 + i * 2, 14.0,
                                  "TREND_DAY", 44.0, 39.0, rng))
    # ABSORPTION
    for i in range(25):
        spx = 7444 - i * 0.05 + math.sin(i * 0.6) * 1.5 + rng.uniform(-0.2, 0.2)
        cum_drift += 2.0 + rng.uniform(0, 1.5)
        frames.append(_make_frame(spx, cum_drift, "BULLISH", 80 + i * 2, 15.0,
                                  "ABSORPTION", 46.0, 37.0, rng))
    # PIN_DAY
    for i in range(30):
        spx = 7440 + math.sin(i * 0.5) * 2.5 + rng.uniform(-0.3, 0.3)
        cum_drift += rng.uniform(-0.4, 0.5)
        frames.append(_make_frame(spx, cum_drift, "NEUTRAL", 130 + i * 2, 13.0,
                                  "PIN_DAY", 40.0, 40.0, rng))
    # WALL_TEST
    for i in range(20):
        spx = 7448 + math.sin(i * 0.9) * 1.8 + rng.uniform(-0.3, 0.3)
        cum_drift += rng.uniform(-0.3, 0.5)
        frames.append(_make_frame(spx, cum_drift, "NEUTRAL", 190 + i * 2, 14.5,
                                  "WALL_TEST", 41.0, 40.0, rng))
    # HIGH_VOL
    for i in range(20):
        spx = 7445 + math.sin(i * 0.5) * 12 + rng.uniform(-4, 4)
        cum_drift += rng.uniform(-3, 3)
        bias = "BULLISH" if math.sin(i * 0.5) > 0 else "BEARISH"
        frames.append(_make_frame(spx, cum_drift, bias, 230 + i * 2, 24.0 + rng.uniform(-1, 2),
                                  "HIGH_VOL", 43.0 + rng.uniform(-3, 3),
                                  43.0 + rng.uniform(-3, 3), rng))
    # CHOP
    for i in range(25):
        spx = 7440 + rng.uniform(-3, 3)
        cum_drift += rng.uniform(-1, 1)
        frames.append(_make_frame(spx, cum_drift, "NEUTRAL", 270 + i * 2, 15.0,
                                  "CHOP", 41.0, 41.0, rng))
    # POWER_HOUR
    for i in range(30):
        spx = 7440 - i * 1.3 + math.sin(i * 0.3) * 2.0 + rng.uniform(-0.4, 0.4)
        cum_drift -= 1.0 + rng.uniform(-0.3, 0.5)
        bias = "BEARISH" if i > 5 else "NEUTRAL"
        frames.append(_make_frame(spx, cum_drift, bias, 320 + i * 2, 15.5,
                                  "POWER_HOUR", 40.0, 44.0, rng))
    return frames


def _steady_bull(rng: random.Random) -> list[DayFrame]:
    """Relentless drift up all day with small pullbacks. Tests trend-day regime."""
    frames: list[DayFrame] = []
    spx = 7400.0
    cum_drift = 0.0
    for i in range(190):
        spx += 0.30 + rng.uniform(-0.4, 0.7) + math.sin(i * 0.15) * 0.3
        cum_drift += 1.2 + rng.uniform(-0.1, 0.6)
        frames.append(_make_frame(spx, cum_drift, "BULLISH", i * 2, 13.0 + math.sin(i * 0.02) * 0.5,
                                  "STEADY_BULL", 46.0, 37.0, rng))
    return frames


def _steady_bear(rng: random.Random) -> list[DayFrame]:
    """Mirror of steady_bull."""
    frames: list[DayFrame] = []
    spx = 7400.0
    cum_drift = 0.0
    for i in range(190):
        spx -= 0.30 + rng.uniform(-0.4, 0.7) - math.sin(i * 0.15) * 0.3
        cum_drift -= 1.2 + rng.uniform(-0.1, 0.6)
        frames.append(_make_frame(spx, cum_drift, "BEARISH", i * 2, 16.0 + math.sin(i * 0.02) * 0.5,
                                  "STEADY_BEAR", 37.0, 46.0, rng))
    return frames


def _reversal(rng: random.Random) -> list[DayFrame]:
    """Open strong one way, reverse hard midmorning, close opposite direction.
    Tests absorption + wall collapse + reversal regime transitions."""
    frames: list[DayFrame] = []
    spx = 7400.0
    cum_drift = 0.0
    # Up leg (0-100 min): drift +30M, SPX +25pts
    for i in range(50):
        spx += 0.5 + rng.uniform(-0.3, 0.5)
        cum_drift += 0.6 + rng.uniform(0, 0.5)
        frames.append(_make_frame(spx, cum_drift, "BULLISH", i * 2, 14.0,
                                  "UP_LEG", 45.0, 38.0, rng))
    # Reversal (100-160 min): absorption then bear breakout
    for i in range(30):
        spx -= 0.8 + rng.uniform(-0.3, 0.4)
        # Drift unwinds — flow flipped
        cum_drift -= 1.0 + rng.uniform(-0.2, 0.4)
        frames.append(_make_frame(spx, cum_drift, "BEARISH", 100 + i * 2, 18.0,
                                  "REVERSAL", 39.0, 47.0, rng))
    # Down leg (160-end): trend down
    for i in range(110):
        spx -= 0.25 + rng.uniform(-0.2, 0.3)
        cum_drift -= 0.8 + rng.uniform(0, 0.4)
        frames.append(_make_frame(spx, cum_drift, "BEARISH", 160 + i * 2,
                                  16.0 + math.sin(i * 0.05) * 0.5,
                                  "DOWN_LEG", 37.0, 46.0, rng))
    return frames


def _pure_chop(rng: random.Random) -> list[DayFrame]:
    """Random walk, no trend, no aligned signals. Tests CHOP regime."""
    frames: list[DayFrame] = []
    spx = 7430.0
    cum_drift = 0.0
    for i in range(190):
        spx += rng.uniform(-1.5, 1.5)
        spx = max(7415, min(7445, spx))  # bounded range
        cum_drift += rng.uniform(-0.8, 0.8)
        bias = "BULLISH" if cum_drift > 3 else "BEARISH" if cum_drift < -3 else "NEUTRAL"
        frames.append(_make_frame(spx, cum_drift, bias, i * 2, 14.0 + rng.uniform(-0.3, 0.3),
                                  "PURE_CHOP", 41.0 + rng.uniform(-2, 2),
                                  41.0 + rng.uniform(-2, 2), rng))
    return frames


def _fed_day(rng: random.Random) -> list[DayFrame]:
    """Tight range until 2pm ET (~270 min), then huge vol burst on announcement,
    then directional move. Tests transition from PIN/CHOP to HIGH_VOL/TREND."""
    frames: list[DayFrame] = []
    spx = 7420.0
    cum_drift = 0.0
    # Pre-announcement: tight pin
    for i in range(135):  # 0-270 min
        spx = 7420 + math.sin(i * 0.2) * 1.5 + rng.uniform(-0.3, 0.3)
        cum_drift += rng.uniform(-0.2, 0.2)
        frames.append(_make_frame(spx, cum_drift, "NEUTRAL", i * 2, 13.0,
                                  "FED_PIN", 40.0, 40.0, rng))
    # Vol explosion (270-310 min): huge swings, drift flips around
    for i in range(20):
        spx = 7420 + math.sin(i * 0.7) * 18 + rng.uniform(-5, 5)
        cum_drift += rng.uniform(-8, 8)
        bias = "BULLISH" if math.sin(i * 0.7) > 0 else "BEARISH"
        frames.append(_make_frame(spx, cum_drift, bias, 270 + i * 2, 28.0 + rng.uniform(-1, 3),
                                  "FED_SPIKE", 45.0 + rng.uniform(-5, 5),
                                  40.0 + rng.uniform(-5, 5), rng))
    # Post-vol direction (310-end): decisive trend
    final_direction = rng.choice([-1, 1])
    for i in range(35):
        spx += final_direction * (0.5 + rng.uniform(0, 0.3))
        cum_drift += final_direction * (1.0 + rng.uniform(0, 0.5))
        bias = "BULLISH" if final_direction > 0 else "BEARISH"
        frames.append(_make_frame(spx, cum_drift, bias, 310 + i * 2,
                                  20.0 - i * 0.1,
                                  "FED_DIRECTIONAL",
                                  46.0 if final_direction > 0 else 37.0,
                                  37.0 if final_direction > 0 else 46.0, rng))
    return frames


def _gap_and_fade(rng: random.Random) -> list[DayFrame]:
    """Open with gap (price already +20pts from "yesterday's close"), then
    fade slowly all session back toward flat. Tests absorption + power hour."""
    frames: list[DayFrame] = []
    spx = 7420.0  # gapped up from notional 7400 close
    cum_drift = 5.0  # some pre-open bullish flow
    for i in range(190):
        # Initial pop then slow grind down
        if i < 20:
            spx += rng.uniform(-0.3, 0.5)
            cum_drift += rng.uniform(-0.2, 0.5)
            label = "GAP_HOLD"
            bias = "BULLISH"
        else:
            spx -= 0.10 + rng.uniform(-0.3, 0.3)
            cum_drift -= 0.4 + rng.uniform(-0.2, 0.3)
            label = "FADE"
            bias = "BEARISH" if cum_drift < 0 else "NEUTRAL"
        frames.append(_make_frame(spx, cum_drift, bias, i * 2, 14.0 + math.sin(i * 0.03) * 0.5,
                                  label, 42.0 if bias != "BEARISH" else 38.0,
                                  40.0 if bias != "BEARISH" else 45.0, rng))
    return frames


ARCHETYPES = {
    "5_15_REPLAY": _5_15_replay,
    "STEADY_BULL": _steady_bull,
    "STEADY_BEAR": _steady_bear,
    "REVERSAL": _reversal,
    "PURE_CHOP": _pure_chop,
    "FED_DAY": _fed_day,
    "GAP_AND_FADE": _gap_and_fade,
}


def build_day_script() -> list[DayFrame]:
    """Build the day script based on env vars at module import time."""
    archetype_name = os.environ.get("FEF_MOCK_ARCHETYPE", "5_15_REPLAY").upper()
    if archetype_name not in ARCHETYPES:
        archetype_name = "5_15_REPLAY"

    seed_env = os.environ.get("FEF_MOCK_SEED", "42")
    if seed_env == "0":
        seed = int(time.time())
    else:
        try:
            seed = int(seed_env)
        except ValueError:
            seed = 42

    rng = random.Random(seed)
    return ARCHETYPES[archetype_name](rng)


def list_archetypes() -> list[str]:
    return list(ARCHETYPES.keys())
