"""Standalone microstructure signal engine.

Based on established academic + practitioner-validated concepts:

  - Order Flow Imbalance (OFI): Cont, Kukanov & Stoikov (2014). Tracks the
    net change in best-bid + best-ask sizes between quote updates. Strong
    short-term price predictor.

  - Aggressive Volume Imbalance (AVI): per-print classification using
    Lee-Ready (1991) — trade at-or-above-ask = aggressive buy, at-or-
    below-bid = aggressive sell. Aggregated as a percentage over a
    rolling window.

  - Microprice: Stoikov (2018). Volume-weighted midpoint, better than
    naive (bid+ask)/2 at predicting where price actually settles next.
    The deviation of last-trade from microprice is a directional pressure
    signal.

The engine is intentionally independent of the FEF model — it runs on
its own sub-second cadence and broadcasts its own signals. Treat it as
a separate entry-timing system that you may or may not combine with FEF.

Mock mode: synthesizes plausible quote + trade events from the existing
mock tick stream so the UI populates without a live data feed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class QuoteUpdate:
    """Single best-bid / best-ask snapshot."""
    timestamp_ms: int
    bid: float
    ask: float
    bid_size: float
    ask_size: float


@dataclass(frozen=True)
class TradePrint:
    """Single trade execution print from the tape."""
    timestamp_ms: int
    price: float
    size: float
    aggressor: Literal["BUY", "SELL", "UNKNOWN"]


@dataclass
class MicrostructureState:
    """Rolling window of recent activity used to compute signals."""

    window_seconds: int = 60

    quotes: deque[QuoteUpdate] = field(default_factory=lambda: deque(maxlen=2000))
    trades: deque[TradePrint] = field(default_factory=lambda: deque(maxlen=4000))
    last_quote: QuoteUpdate | None = None


@dataclass(frozen=True)
class MicrostructureSignal:
    """Snapshot of the microstructure metrics + a derived directional signal."""

    timestamp_ms: int
    ofi_window: float                 # cumulative OFI over window (signed)
    avi_pct: float                    # aggressive-buy % of total aggressive volume
    aggressive_buy_volume: float
    aggressive_sell_volume: float
    microprice: float | None
    last_trade_price: float | None
    microprice_deviation_bp: float | None  # (last_trade - microprice) in basis points
    direction: Literal["BULLISH_PRESSURE", "BEARISH_PRESSURE", "BALANCED"]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    reasoning: str
    quote_count: int
    trade_count: int


def classify_trade(price: float, bid: float | None, ask: float | None) -> Literal["BUY", "SELL", "UNKNOWN"]:
    """Lee-Ready (1991) trade-side classification.

    Quote-based test first: at-or-above ask = buy, at-or-below bid = sell,
    inside = use tick rule fallback (here: 'UNKNOWN' since we don't track
    prev trade price)."""
    if bid is None or ask is None:
        return "UNKNOWN"
    if price >= ask:
        return "BUY"
    if price <= bid:
        return "SELL"
    return "UNKNOWN"  # could enhance with tick rule using previous trade price


def push_quote(state: MicrostructureState, q: QuoteUpdate) -> None:
    state.quotes.append(q)
    state.last_quote = q
    _evict_old(state, q.timestamp_ms)


def push_trade(state: MicrostructureState, t: TradePrint) -> None:
    state.trades.append(t)
    _evict_old(state, t.timestamp_ms)


def _evict_old(state: MicrostructureState, now_ms: int) -> None:
    cutoff = now_ms - state.window_seconds * 1000
    while state.quotes and state.quotes[0].timestamp_ms < cutoff:
        state.quotes.popleft()
    while state.trades and state.trades[0].timestamp_ms < cutoff:
        state.trades.popleft()


def compute_ofi(state: MicrostructureState) -> float:
    """OFI per Cont/Kukanov/Stoikov:

      for each pair of consecutive quotes:
        if bid increased OR bid_size increased at same bid: +bid_size_change
        if bid decreased OR bid_size decreased at same bid: -bid_size_change
        same logic flipped for ask
      sum over window = OFI
    """
    if len(state.quotes) < 2:
        return 0.0
    ofi = 0.0
    prev: QuoteUpdate | None = None
    for q in state.quotes:
        if prev is None:
            prev = q
            continue
        # Bid side contribution
        if q.bid > prev.bid:
            ofi += q.bid_size
        elif q.bid < prev.bid:
            ofi -= prev.bid_size
        else:
            ofi += (q.bid_size - prev.bid_size)
        # Ask side contribution (note: sign flipped — ask up = bearish)
        if q.ask > prev.ask:
            ofi += prev.ask_size
        elif q.ask < prev.ask:
            ofi -= q.ask_size
        else:
            ofi -= (q.ask_size - prev.ask_size)
        prev = q
    return ofi


def compute_avi(state: MicrostructureState) -> tuple[float, float, float]:
    """Aggressive volume imbalance from Lee-Ready-classified trades.

    Returns (buy_volume, sell_volume, buy_pct).
    """
    buy_vol = 0.0
    sell_vol = 0.0
    for t in state.trades:
        if t.aggressor == "BUY":
            buy_vol += t.size
        elif t.aggressor == "SELL":
            sell_vol += t.size
    total = buy_vol + sell_vol
    pct = (buy_vol / total * 100) if total > 0 else 50.0
    return buy_vol, sell_vol, pct


def compute_microprice(quote: QuoteUpdate | None) -> float | None:
    """Stoikov microprice: size-weighted midpoint.

      mp = (bid * ask_size + ask * bid_size) / (bid_size + ask_size)

    Note the cross-weighting — the more size sits at one side, the more
    the microprice pulls toward the OTHER side (because the heavy side
    is the resistance).
    """
    if quote is None:
        return None
    total = quote.bid_size + quote.ask_size
    if total <= 0:
        return None
    return (quote.bid * quote.ask_size + quote.ask * quote.bid_size) / total


def assess(state: MicrostructureState, now_ms: int) -> MicrostructureSignal:
    ofi = compute_ofi(state)
    buy_vol, sell_vol, avi_pct = compute_avi(state)
    mp = compute_microprice(state.last_quote)
    last_price = state.trades[-1].price if state.trades else None
    mp_dev_bp = None
    if mp is not None and last_price is not None and mp > 0:
        mp_dev_bp = round((last_price - mp) / mp * 10_000, 2)

    # Direction: combine OFI sign + AVI deviation from 50% + microprice deviation
    # Each contributes a vote; majority wins. Confidence = how unanimous.
    votes_bull = 0
    votes_bear = 0
    if ofi > 100:
        votes_bull += 1
    elif ofi < -100:
        votes_bear += 1
    if avi_pct > 55:
        votes_bull += 1
    elif avi_pct < 45:
        votes_bear += 1
    if mp_dev_bp is not None:
        if mp_dev_bp > 1.0:
            votes_bull += 1
        elif mp_dev_bp < -1.0:
            votes_bear += 1

    if votes_bull >= 2 and votes_bear == 0:
        direction = "BULLISH_PRESSURE"
    elif votes_bear >= 2 and votes_bull == 0:
        direction = "BEARISH_PRESSURE"
    else:
        direction = "BALANCED"

    confidence = (
        "HIGH" if (votes_bull >= 3 or votes_bear >= 3)
        else "MEDIUM" if (votes_bull >= 2 or votes_bear >= 2)
        else "LOW"
    )

    reasoning = (
        f"OFI {ofi:+.0f} · AVI {avi_pct:.0f}% buy "
        f"({buy_vol:.0f}B/{sell_vol:.0f}S) · "
        f"micro-dev {mp_dev_bp if mp_dev_bp is not None else '—'}bp"
    )

    return MicrostructureSignal(
        timestamp_ms=now_ms,
        ofi_window=round(ofi, 2),
        avi_pct=round(avi_pct, 1),
        aggressive_buy_volume=round(buy_vol, 0),
        aggressive_sell_volume=round(sell_vol, 0),
        microprice=round(mp, 4) if mp is not None else None,
        last_trade_price=last_price,
        microprice_deviation_bp=mp_dev_bp,
        direction=direction,
        confidence=confidence,
        reasoning=reasoning,
        quote_count=len(state.quotes),
        trade_count=len(state.trades),
    )
