"""Quote stuffing / cancel-to-trade ratio detector.

Quote stuffing = bursts of quote updates that get cancelled before any trade
happens. Often an HFT signal that they're scoping out the book to detect
hidden liquidity or trying to slow competitors.

Cancel-to-trade ratio = (quote updates - new trades) / new trades. Healthy
markets sit around 5-15x. Stuffing periods spike to 50x+.

Sub-second computable on the existing quote + trade streams (already feeding
the microstructure engine — we just need to also count cancellations).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class StuffingState:
    window_seconds: int = 30
    quote_timestamps: deque[int] = field(default_factory=lambda: deque(maxlen=10000))
    trade_timestamps: deque[int] = field(default_factory=lambda: deque(maxlen=2000))


@dataclass(frozen=True)
class StuffingSignal:
    timestamp_ms: int
    quote_count: int
    trade_count: int
    cancel_to_trade_ratio: float
    quote_intensity_per_sec: float
    severity: Literal["NORMAL", "ELEVATED", "STUFFING"]
    reasoning: str


def push_quote(state: StuffingState, ts_ms: int) -> None:
    state.quote_timestamps.append(ts_ms)
    _evict(state, ts_ms)


def push_trade(state: StuffingState, ts_ms: int) -> None:
    state.trade_timestamps.append(ts_ms)
    _evict(state, ts_ms)


def _evict(state: StuffingState, now_ms: int) -> None:
    cutoff = now_ms - state.window_seconds * 1000
    while state.quote_timestamps and state.quote_timestamps[0] < cutoff:
        state.quote_timestamps.popleft()
    while state.trade_timestamps and state.trade_timestamps[0] < cutoff:
        state.trade_timestamps.popleft()


def assess(state: StuffingState, now_ms: int) -> StuffingSignal:
    qc = len(state.quote_timestamps)
    tc = len(state.trade_timestamps)
    ratio = (qc / tc) if tc > 0 else float(qc)
    intensity = qc / max(1, state.window_seconds)

    if ratio >= 50 or intensity >= 100:
        severity = "STUFFING"
        reason = f"ratio {ratio:.0f}x, intensity {intensity:.0f}/s — likely stuffing"
    elif ratio >= 20 or intensity >= 50:
        severity = "ELEVATED"
        reason = f"ratio {ratio:.0f}x, intensity {intensity:.0f}/s — elevated quote churn"
    else:
        severity = "NORMAL"
        reason = f"ratio {ratio:.1f}x, intensity {intensity:.1f}/s"

    return StuffingSignal(
        timestamp_ms=now_ms,
        quote_count=qc,
        trade_count=tc,
        cancel_to_trade_ratio=round(ratio, 1),
        quote_intensity_per_sec=round(intensity, 1),
        severity=severity,
        reasoning=reason,
    )
