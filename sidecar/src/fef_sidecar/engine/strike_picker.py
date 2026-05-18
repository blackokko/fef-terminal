"""Pick the short strike for a credit spread based on delta + premium.

Target: |delta| <= 0.15 AND mid premium >= $0.70. Walks strikes outward
from spot at 5-point increments and queries each option's quote via
IBKRProvider.get_option_quote. Returns the FIRST strike satisfying both
constraints, or None if no strike in the scan window qualifies (per
user direction: 'show no spread' when criteria can't be met).

Async with conservative bounds to avoid hammering IBKR — at most
``max_scan_strikes`` quote calls per scan; results cached for
``cache_ttl_sec`` so repeated recommender ticks don't re-query.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from fef_sidecar.ibkr.models import Contract, ContractKind, OptionQuote
from fef_sidecar.ibkr.provider import IBKRProvider


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrikePick:
    short_strike: float
    long_strike: float
    short_delta: float
    short_mid: float
    short_bid: float | None
    short_ask: float | None
    width: float
    estimated_credit: float  # in dollars per spread (not per-contract)


@dataclass
class _CacheEntry:
    pick: StrikePick | None
    fetched_at_ms: int


_pick_cache: dict[str, _CacheEntry] = {}


def _round_to_5(price: float) -> float:
    return round(price / 5) * 5


async def _quote_with_timeout(
    ibkr: IBKRProvider, contract: Contract, timeout_sec: float = 4.0
) -> OptionQuote | None:
    try:
        return await asyncio.wait_for(
            ibkr.get_option_quote(contract), timeout=timeout_sec
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.debug("Option quote timed out: %s", contract)
        return None
    except Exception:
        logger.exception("Option quote failed: %s", contract)
        return None


async def find_pcs_strike(
    ibkr: IBKRProvider,
    *,
    symbol: str,
    expiry: str,
    spot: float,
    width: float = 5.0,
    delta_max: float = 0.15,
    premium_min: float = 0.70,
    starting_offset: float = 5.0,
    max_scan_strikes: int = 10,
    cache_ttl_sec: int = 60,
) -> StrikePick | None:
    """Scan PUT strikes BELOW spot for the first one with |delta| <=
    delta_max AND mid >= premium_min. Used for PCS short leg.

    starting_offset = how many points below spot to begin scanning.
    Each iteration walks an additional `width` points OTM.
    """
    cache_key = f"PCS|{symbol}|{expiry}|{_round_to_5(spot)}"
    now_ms = int(time.time() * 1000)
    entry = _pick_cache.get(cache_key)
    if entry and now_ms - entry.fetched_at_ms < cache_ttl_sec * 1000:
        return entry.pick

    starting = _round_to_5(spot - starting_offset)
    for i in range(max_scan_strikes):
        short_strike = starting - i * width
        if short_strike <= 0:
            break
        contract = Contract(
            symbol=symbol,
            kind=ContractKind.OPTION,
            exchange="CBOE" if symbol == "SPX" else "SMART",
            expiry=expiry,
            strike=short_strike,
            right="P",
        )
        quote = await _quote_with_timeout(ibkr, contract)
        if quote is None or quote.delta is None:
            continue
        d = abs(quote.delta)
        mid = quote.mid
        if d <= delta_max and mid >= premium_min:
            long_strike = short_strike - width
            pick = StrikePick(
                short_strike=short_strike,
                long_strike=long_strike,
                short_delta=quote.delta,
                short_mid=mid,
                short_bid=quote.bid,
                short_ask=quote.ask,
                width=width,
                estimated_credit=mid,
            )
            _pick_cache[cache_key] = _CacheEntry(pick, now_ms)
            return pick
        if d < delta_max * 0.5:
            break

    _pick_cache[cache_key] = _CacheEntry(None, now_ms)
    return None


async def find_ccs_strike(
    ibkr: IBKRProvider,
    *,
    symbol: str,
    expiry: str,
    spot: float,
    width: float = 5.0,
    delta_max: float = 0.15,
    premium_min: float = 0.70,
    starting_offset: float = 5.0,
    max_scan_strikes: int = 10,
    cache_ttl_sec: int = 60,
) -> StrikePick | None:
    """Mirror of find_pcs_strike for CCS short leg — scans CALL strikes
    ABOVE spot."""
    cache_key = f"CCS|{symbol}|{expiry}|{_round_to_5(spot)}"
    now_ms = int(time.time() * 1000)
    entry = _pick_cache.get(cache_key)
    if entry and now_ms - entry.fetched_at_ms < cache_ttl_sec * 1000:
        return entry.pick

    starting = _round_to_5(spot + starting_offset)
    for i in range(max_scan_strikes):
        short_strike = starting + i * width
        contract = Contract(
            symbol=symbol,
            kind=ContractKind.OPTION,
            exchange="CBOE" if symbol == "SPX" else "SMART",
            expiry=expiry,
            strike=short_strike,
            right="C",
        )
        quote = await _quote_with_timeout(ibkr, contract)
        if quote is None or quote.delta is None:
            continue
        d = abs(quote.delta)
        mid = quote.mid
        if d <= delta_max and mid >= premium_min:
            long_strike = short_strike + width
            pick = StrikePick(
                short_strike=short_strike,
                long_strike=long_strike,
                short_delta=quote.delta,
                short_mid=mid,
                short_bid=quote.bid,
                short_ask=quote.ask,
                width=width,
                estimated_credit=mid,
            )
            _pick_cache[cache_key] = _CacheEntry(pick, now_ms)
            return pick
        if d < delta_max * 0.5:
            break

    _pick_cache[cache_key] = _CacheEntry(None, now_ms)
    return None


def clear_strike_cache() -> None:
    """Wipe the strike-pick cache. Called when symbol changes."""
    _pick_cache.clear()
