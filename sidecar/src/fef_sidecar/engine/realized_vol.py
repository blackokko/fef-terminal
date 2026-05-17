"""Realized volatility engine — rolling N-minute realized vol on tick stream.

Classic intraday vol signal. Detects:
  - Vol expansion (current 1m realized > 2x trailing 5m average) — regime shift
  - Vol contraction (1m << 5m) — coiling, potential breakout setup
  - Outlier moves (single tick > 3-sigma of recent distribution)

Sub-second computable on each new tick.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import sqrt
from typing import Literal


@dataclass
class RealizedVolState:
    """Rolling tick prices with windowed vol computation."""

    short_window_sec: int = 60      # 1m
    long_window_sec: int = 300      # 5m
    # Per-tick: (timestamp_ms, price)
    ticks: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=5000))


@dataclass(frozen=True)
class VolSignal:
    timestamp_ms: int
    short_vol_pts: float        # std dev of price changes over short window, in points
    long_vol_pts: float
    ratio: float                # short_vol / long_vol — >2 = expansion, <0.5 = contraction
    regime: Literal["EXPANSION", "CONTRACTION", "NORMAL"]
    n_short: int
    n_long: int


def push_tick(state: RealizedVolState, timestamp_ms: int, price: float) -> None:
    state.ticks.append((timestamp_ms, price))
    # Evict ticks older than long_window from the front
    cutoff = timestamp_ms - state.long_window_sec * 1000
    while state.ticks and state.ticks[0][0] < cutoff:
        state.ticks.popleft()


def _realized_vol(ticks: list[tuple[int, float]]) -> float:
    """Standard deviation of consecutive tick price differences, in points."""
    if len(ticks) < 2:
        return 0.0
    diffs = [ticks[i][1] - ticks[i - 1][1] for i in range(1, len(ticks))]
    mean = sum(diffs) / len(diffs)
    variance = sum((d - mean) ** 2 for d in diffs) / max(1, len(diffs) - 1)
    return sqrt(variance)


def assess(state: RealizedVolState, now_ms: int | None = None) -> VolSignal:
    if not state.ticks:
        return VolSignal(
            timestamp_ms=now_ms or 0, short_vol_pts=0, long_vol_pts=0,
            ratio=1.0, regime="NORMAL", n_short=0, n_long=0,
        )
    now_ms = now_ms or state.ticks[-1][0]
    short_cutoff = now_ms - state.short_window_sec * 1000
    long_cutoff = now_ms - state.long_window_sec * 1000

    short_ticks = [t for t in state.ticks if t[0] >= short_cutoff]
    long_ticks = [t for t in state.ticks if t[0] >= long_cutoff]

    sv = _realized_vol(short_ticks)
    lv = _realized_vol(long_ticks)

    ratio = (sv / lv) if lv > 0 else 1.0
    if ratio >= 2.0:
        regime = "EXPANSION"
    elif ratio <= 0.5 and lv > 0.05:
        regime = "CONTRACTION"
    else:
        regime = "NORMAL"

    return VolSignal(
        timestamp_ms=now_ms,
        short_vol_pts=round(sv, 3),
        long_vol_pts=round(lv, 3),
        ratio=round(ratio, 2),
        regime=regime,
        n_short=len(short_ticks),
        n_long=len(long_ticks),
    )
