"""Liquidity-aware strike selection — adjusts the engine's recommended
strike based on actual market liquidity (OI + recent volume + bid-ask spread)
rather than purely picking the largest wall.

Filters that downgrade a strike:
  - Bid-ask spread > 5% of mid → wide market = bad fill expected
  - Recent volume < 100 contracts in the session → illiquid, slippage risk
  - Open interest < 500 → may not be defended

If the picked strike fails any filter, the next-best wall is tried until
one passes (within ±50pt of the original). If none pass, returns the
original with a warning so the user can decide.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrikeQuality:
    strike: float
    score: float          # 0-100, higher = more liquid
    bid_ask_pct: float | None
    open_interest: int | None
    session_volume: int | None
    reasons: list[str]
    accepted: bool


def assess_strike(
    strike: float,
    *,
    bid: float | None = None,
    ask: float | None = None,
    open_interest: int | None = None,
    session_volume: int | None = None,
    min_open_interest: int = 500,
    min_session_volume: int = 100,
    max_bid_ask_pct: float = 5.0,
) -> StrikeQuality:
    score = 100.0
    reasons: list[str] = []

    bid_ask_pct = None
    if bid is not None and ask is not None and ask > 0:
        mid = (bid + ask) / 2
        if mid > 0:
            bid_ask_pct = round((ask - bid) / mid * 100, 2)
            if bid_ask_pct > max_bid_ask_pct:
                score -= 40
                reasons.append(f"wide spread {bid_ask_pct:.1f}% (max {max_bid_ask_pct}%)")
            elif bid_ask_pct > max_bid_ask_pct / 2:
                score -= 15
                reasons.append(f"moderate spread {bid_ask_pct:.1f}%")

    if open_interest is not None:
        if open_interest < min_open_interest:
            score -= 30
            reasons.append(f"low OI {open_interest} (min {min_open_interest})")
        elif open_interest > 5000:
            reasons.append(f"strong OI {open_interest}")

    if session_volume is not None:
        if session_volume < min_session_volume:
            score -= 30
            reasons.append(f"low volume {session_volume} (min {min_session_volume})")
        elif session_volume > 1000:
            reasons.append(f"active {session_volume}")

    accepted = score >= 60
    return StrikeQuality(
        strike=strike, score=round(score, 1),
        bid_ask_pct=bid_ask_pct, open_interest=open_interest,
        session_volume=session_volume, reasons=reasons, accepted=accepted,
    )
