"""Per-symbol option expiry resolver.

Different underlyings list options on different schedules:
  - SPX: daily 0DTE (M-F)
  - SPY / QQQ: 0DTE on M/W/F; Tu/Th have only weeklies
  - IWM: varies — typically 0DTE on M/W/F
  - GLD / SMH: no 0DTE — weeklies (Fri) or monthlies
  - TLT: weeklies (Fri)

The multi-symbol scanner previously hard-coded today's date as expiry for
every symbol, which silently failed at the option-quote fetch for symbols
without 0DTE listings. The auto-trader then blocked at Gate 8
(NO_OPTION_PRICING) without firing.

This resolver asks IBKR via `reqContractDetailsAsync` for available
expiries for a symbol, picks the nearest one ≥ today, and caches per
session_date so we hit IBKR at most once per symbol per day.

Cache is keyed by (symbol, session_date) so a sidecar that runs across
midnight CT automatically re-resolves the next day's expiry.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _today_et_ymd() -> str:
    et = timezone(timedelta(hours=-4))
    return datetime.now(et).strftime("%Y%m%d")


def _today_et_session() -> str:
    et = timezone(timedelta(hours=-4))
    return datetime.now(et).strftime("%Y-%m-%d")


async def resolve_nearest_expiry(state, symbol: str) -> str:
    """Return the nearest available option expiry for `symbol` in
    YYYYMMDD format. Falls back to today's date if IBKR query fails.

    Cache: state.expiry_cache_by_key[(symbol, session_date)] = expiry
    """
    sym = symbol.upper()
    session = _today_et_session()
    cache_key = (sym, session)

    cache = getattr(state, "expiry_cache_by_key", None)
    if cache is None:
        cache = {}
        state.expiry_cache_by_key = cache  # type: ignore[attr-defined]

    if cache_key in cache:
        return cache[cache_key]

    today_ymd = _today_et_ymd()

    # SPX is the safe default — daily 0DTE always exists during RTH.
    # Skip the IBKR query for SPX to avoid unnecessary overhead.
    if sym == "SPX":
        cache[cache_key] = today_ymd
        return today_ymd

    try:
        from fef_sidecar.ibkr.live import LiveIBKRProvider
        if not isinstance(state.ibkr, LiveIBKRProvider):
            cache[cache_key] = today_ymd
            return today_ymd

        ib = getattr(state.ibkr, "_ib", None)
        if ib is None or not ib.isConnected():
            cache[cache_key] = today_ymd
            return today_ymd

        # Build a stub Option contract — IBKR returns ALL listed expiries
        # when strike/right are omitted on reqContractDetails.
        from ib_async import Option
        stub = Option(sym, "", 0, "", "SMART")
        details = await asyncio.wait_for(
            ib.reqContractDetailsAsync(stub),
            timeout=5.0,
        )
        # Collect unique expiries
        expiries = sorted({
            d.contract.lastTradeDateOrContractMonth
            for d in details
            if d.contract and d.contract.lastTradeDateOrContractMonth
        })
        if not expiries:
            logger.warning("expiry resolver: no expiries returned for %s; falling back to today", sym)
            cache[cache_key] = today_ymd
            return today_ymd

        # Find nearest expiry ≥ today
        nearest = next((e for e in expiries if e >= today_ymd), expiries[-1])
        cache[cache_key] = nearest
        logger.info(
            "expiry resolver: %s nearest expiry = %s (today=%s, %d expiries seen)",
            sym, nearest, today_ymd, len(expiries),
        )
        return nearest
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("expiry resolver: IBKR timeout for %s; falling back to today", sym)
        cache[cache_key] = today_ymd
        return today_ymd
    except Exception:
        logger.exception("expiry resolver: unexpected error for %s; falling back to today", sym)
        cache[cache_key] = today_ymd
        return today_ymd
