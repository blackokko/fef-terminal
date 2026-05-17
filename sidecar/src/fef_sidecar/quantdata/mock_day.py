"""Scripted full-day mock — walks through every regime state in sequence.

Enable with FEF_MOCK_SCRIPTED_DAY=true. The MockQuantDataClient then
returns data appropriate to the 'current phase' based on its fetch counter,
plus a simulated minutes-since-open so the regime detector sees the day
advance through OPEN_DRIVE / TREND / ABSORPTION / PIN / WALL_TEST / HIGH_VOL /
POWER_HOUR.

Combine with FEF_LOOP_INTERVAL_SEC=3 to compress the entire day into
~10 minutes for fast testing.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class DayFrame:
    """One simulated iter's worth of data."""

    spx: float
    drift_cum_net_m: float
    drift_bias: str
    minutes_since_open: int
    vix: float
    label: str  # which regime phase this frame is from


def _build_day_script() -> list[DayFrame]:
    """Generate ~165 frames covering a synthetic SPX 0DTE trading day.

    Each frame is one FEF iter. With FEF_LOOP_INTERVAL_SEC=120 this is a
    full 5.5hr day; with =3 it replays in ~8min for testing.
    """
    frames: list[DayFrame] = []
    base_spx = 7400.0
    cum_drift = 0.0
    rng = random.Random(42)  # deterministic — same arc every restart

    # ----- OPEN_DRIVE (0-30 min): big initial directional move up -----
    for i in range(15):
        spx = base_spx + i * 1.5 + rng.uniform(-0.4, 0.4)
        cum_drift += 1.5 + rng.uniform(0, 1.0)
        frames.append(DayFrame(
            spx=round(spx, 2),
            drift_cum_net_m=round(cum_drift, 2),
            drift_bias="BULLISH",
            minutes_since_open=i * 2,
            vix=14.0,
            label="OPEN_DRIVE",
        ))

    # ----- TREND_DAY (30-90 min): aligned bull continuation -----
    for i in range(25):
        spx = 7422 + i * 0.7 + math.sin(i * 0.4) * 2.0 + rng.uniform(-0.3, 0.3)
        cum_drift += 1.2 + rng.uniform(-0.2, 0.8)
        frames.append(DayFrame(
            spx=round(spx, 2),
            drift_cum_net_m=round(cum_drift, 2),
            drift_bias="BULLISH",
            minutes_since_open=30 + i * 2,
            vix=14.0,
            label="TREND_DAY",
        ))

    # ----- ABSORPTION_HEAVY (90-140 min): drift up, price refuses -----
    for i in range(25):
        spx = 7444 - i * 0.05 + math.sin(i * 0.6) * 1.5 + rng.uniform(-0.2, 0.2)
        cum_drift += 2.0 + rng.uniform(0, 1.5)  # drift keeps building
        frames.append(DayFrame(
            spx=round(spx, 2),
            drift_cum_net_m=round(cum_drift, 2),
            drift_bias="BULLISH",
            minutes_since_open=80 + i * 2,
            vix=15.0,
            label="ABSORPTION",
        ))

    # ----- PIN_DAY (140-200 min): tight range near walls, flat drift -----
    for i in range(30):
        spx = 7440 + math.sin(i * 0.5) * 2.5 + rng.uniform(-0.3, 0.3)
        cum_drift += rng.uniform(-0.4, 0.5)  # near-zero net drift
        frames.append(DayFrame(
            spx=round(spx, 2),
            drift_cum_net_m=round(cum_drift, 2),
            drift_bias="NEUTRAL",
            minutes_since_open=130 + i * 2,
            vix=13.0,
            label="PIN_DAY",
        ))

    # ----- WALL_TEST (200-240 min): oscillates around dominant wall -----
    for i in range(20):
        spx = 7448 + math.sin(i * 0.9) * 1.8 + rng.uniform(-0.3, 0.3)
        cum_drift += rng.uniform(-0.3, 0.5)
        frames.append(DayFrame(
            spx=round(spx, 2),
            drift_cum_net_m=round(cum_drift, 2),
            drift_bias="NEUTRAL",
            minutes_since_open=190 + i * 2,
            vix=14.5,
            label="WALL_TEST",
        ))

    # ----- HIGH_VOL (240-280 min): VIX spike + big SPX swings -----
    for i in range(20):
        spx = 7445 + math.sin(i * 0.5) * 12 + rng.uniform(-4, 4)
        cum_drift += rng.uniform(-3, 3)
        bias = "BULLISH" if math.sin(i * 0.5) > 0 else "BEARISH"
        frames.append(DayFrame(
            spx=round(spx, 2),
            drift_cum_net_m=round(cum_drift, 2),
            drift_bias=bias,
            minutes_since_open=230 + i * 2,
            vix=24.0 + rng.uniform(-1, 2),
            label="HIGH_VOL",
        ))

    # ----- CHOP (280-330 min): random small moves, no alignment -----
    for i in range(25):
        spx = 7440 + rng.uniform(-3, 3)
        cum_drift += rng.uniform(-1, 1)
        frames.append(DayFrame(
            spx=round(spx, 2),
            drift_cum_net_m=round(cum_drift, 2),
            drift_bias="NEUTRAL",
            minutes_since_open=270 + i * 2,
            vix=15.0,
            label="CHOP",
        ))

    # ----- POWER_HOUR (330-390 min): drift toward MP ($7,400) -----
    for i in range(30):
        spx = 7440 - i * 1.3 + math.sin(i * 0.3) * 2.0 + rng.uniform(-0.4, 0.4)
        cum_drift -= 1.0 + rng.uniform(-0.3, 0.5)
        frames.append(DayFrame(
            spx=round(spx, 2),
            drift_cum_net_m=round(cum_drift, 2),
            drift_bias="BEARISH" if i > 5 else "NEUTRAL",
            minutes_since_open=320 + i * 2,
            vix=15.5,
            label="POWER_HOUR",
        ))

    return frames


DAY_SCRIPT: list[DayFrame] = _build_day_script()


def total_frames() -> int:
    return len(DAY_SCRIPT)


def frame_at(index: int) -> DayFrame:
    """Clamp at the last frame instead of looping — looping caused the chart
    to receive bars with regressing timestamps (lightweight-charts errors)."""
    if index >= len(DAY_SCRIPT):
        return DAY_SCRIPT[-1]
    return DAY_SCRIPT[index]


def replay_complete(index: int) -> bool:
    return index >= len(DAY_SCRIPT)
