"""L2 depth-of-book — book imbalance signal.

The classic L2 microstructure signal:
  imbalance = (sum bid sizes in top N levels - sum ask sizes) / (bid + ask)
  → -1 (heavy sell-side) to +1 (heavy buy-side)

Strong imbalance often precedes price movement in the direction of pressure.

Mock mode synthesizes a plausible book around the current price.
Live mode requires an IBKR depth-data subscription (extra cost) and uses
reqMktDepth.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass
class DepthState:
    """Top-N bids + asks at any moment. Levels are sorted: bids descending
    (best at index 0), asks ascending (best at index 0)."""

    max_depth: int = 10
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    last_update_ms: int = 0
    # Rolling history of imbalance values for trend detection
    imbalance_history: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=300))


@dataclass(frozen=True)
class DepthSignal:
    timestamp_ms: int
    bid_size_total: float
    ask_size_total: float
    imbalance: float                    # -1 to +1, signed pressure
    imbalance_5lvl: float               # only top 5
    weighted_mid_price: float | None
    spread: float | None
    direction: Literal["BID_HEAVY", "ASK_HEAVY", "BALANCED"]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]


def update_book(state: DepthState, bids: list[BookLevel], asks: list[BookLevel], now_ms: int) -> None:
    state.bids = sorted(bids, key=lambda l: -l.price)[:state.max_depth]
    state.asks = sorted(asks, key=lambda l: l.price)[:state.max_depth]
    state.last_update_ms = now_ms

    sig = assess(state, now_ms)
    state.imbalance_history.append((now_ms, sig.imbalance))


def assess(state: DepthState, now_ms: int) -> DepthSignal:
    if not state.bids or not state.asks:
        return DepthSignal(
            timestamp_ms=now_ms, bid_size_total=0, ask_size_total=0,
            imbalance=0, imbalance_5lvl=0, weighted_mid_price=None,
            spread=None, direction="BALANCED", confidence="LOW",
        )

    bid_total = sum(l.size for l in state.bids)
    ask_total = sum(l.size for l in state.asks)
    bid_5 = sum(l.size for l in state.bids[:5])
    ask_5 = sum(l.size for l in state.asks[:5])

    imbalance = (bid_total - ask_total) / max(bid_total + ask_total, 1)
    imbalance_5 = (bid_5 - ask_5) / max(bid_5 + ask_5, 1)

    best_bid = state.bids[0].price
    best_ask = state.asks[0].price
    spread = round(best_ask - best_bid, 2)
    weighted_mid = (best_bid * ask_total + best_ask * bid_total) / max(bid_total + ask_total, 1)

    if imbalance_5 > 0.3:
        direction = "BID_HEAVY"
    elif imbalance_5 < -0.3:
        direction = "ASK_HEAVY"
    else:
        direction = "BALANCED"

    confidence = (
        "HIGH" if abs(imbalance_5) > 0.5 and abs(imbalance) > 0.3
        else "MEDIUM" if abs(imbalance_5) > 0.3
        else "LOW"
    )

    return DepthSignal(
        timestamp_ms=now_ms,
        bid_size_total=round(bid_total, 0),
        ask_size_total=round(ask_total, 0),
        imbalance=round(imbalance, 3),
        imbalance_5lvl=round(imbalance_5, 3),
        weighted_mid_price=round(weighted_mid, 2),
        spread=spread,
        direction=direction, confidence=confidence,
    )
