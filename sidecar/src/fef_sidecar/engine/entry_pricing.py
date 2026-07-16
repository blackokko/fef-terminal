"""Marketable-limit entry pricing (2026-07-16 incident fix).

The auto-trader's entry orders were raw MARKET orders. At 14:54:22 CT a HIGH
fire placed BUY 3x SPX 7520C MKT (live_order 776 / ib_order_id 242115) and
IBKR's price-cap algorithm on market option orders RESTED the order for
4m20s while the ask ran away — final fill $8.10 vs ~$5.50-6.00 at signal
time (~$750 of hang-slippage at 3 lots). A marketable LIMIT at (ask +
small buffer) either fills immediately at a price we chose, or rests at OUR
price where the 45s entry timeout (entry_timeout.py) can cleanly cancel it.

Buffer policy (user-specified): max($0.10, 2% of ask), capped at $0.50.
The resulting limit is then rounded UP to the contract's minimum price
increment — IBKR rejects off-tick limit prices outright ("does not conform
to the minimum price variation"), and rounding DOWN could make the order
non-marketable. Rounding up can exceed the $0.50 buffer cap by at most one
tick; that is deliberate (price-grid compliance beats cap purity).

Quote freshness: the ask comes from the SAME fire-time quote gate 8 already
validated (option_pricing.ask + option_pricing.quote_ts_ms). If that quote
is stale (>10s old) we re-fetch ONCE; if no usable ask can be resolved we
do NOT fall back to MKT — the fire is blocked (BlockReason.NO_FRESH_QUOTE)
with the same counterfactual logging contract as the other gates.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Max age of the fire-time quote before a one-shot re-fetch (spec: 10s).
ENTRY_QUOTE_MAX_AGE_MS = 10_000
# Bound on the one-shot re-fetch round-trip so a wedged IBKR call can't
# stall the firing lock (get_option_quote itself waits up to 5s internally).
_REFETCH_TIMEOUT_SEC = float(os.environ.get("FEF_ENTRY_REFETCH_TIMEOUT_SEC", "6"))

_BUFFER_FLOOR = 0.10
_BUFFER_PCT = 0.02
_BUFFER_CAP = 0.50

# Re-fetch chase bound: when the fire-time quote went stale (>10s) BECAUSE
# the tape is running, the one-shot re-fetch can return an ask far above the
# economics the verdict was graded at (the incident's $8.10-vs-$5.50 class of
# fill, minus the hang). Refuse to chase a re-fetched ask more than this
# fraction above the fire-quote reference — the fire blocks NO_FRESH_QUOTE
# instead. Fresh-path asks are unaffected (they ARE the reference).
_MAX_CHASE_PCT = float(os.environ.get("FEF_ENTRY_MAX_CHASE_PCT", "0.15"))

# SPY/QQQ/IWM quote in pennies at ALL prices. Other penny-program equity/ETF
# names (GLD/SMH/TLT in the scanner universe) are $0.01 below $3.00 and
# $0.05 at/above — an off-tier limit is REJECTED by IBKR, which would make
# entries structurally impossible for those symbols.
_PENNY_ALL_PRICES = {"SPY", "QQQ", "IWM"}


def entry_buffer(ask: float) -> float:
    """max($0.10, 2% of ask), capped at $0.50. Pre-tick-rounding."""
    return round(min(max(_BUFFER_FLOOR, _BUFFER_PCT * ask), _BUFFER_CAP), 4)


def entry_tick(symbol: str, price: float) -> float:
    """Minimum price increment at `price` for the ENTRY order.

    SPX/SPXW (index options): $0.05 below $3.00 premium, $0.10 at/above.
    SPY/QQQ/IWM: $0.01 at all prices. Other penny-program names
    (GLD/SMH/TLT/...): $0.01 below $3.00, $0.05 at/above.
    """
    s = symbol.upper()
    if s == "SPX":
        return 0.05 if price < 3.0 else 0.10
    if s in _PENNY_ALL_PRICES:
        return 0.01
    return 0.01 if price < 3.0 else 0.05


def marketable_entry_limit(symbol: str, ask: float) -> float:
    """ask + buffer, rounded UP to the tick grid at the limit price."""
    raw = ask + entry_buffer(ask)
    tick = entry_tick(symbol, raw)
    # -1e-9 guards float dust (5.10/0.05 -> 101.99999...) so an already
    # on-tick price is NOT bumped a full extra tick.
    return round(math.ceil(raw / tick - 1e-9) * tick, 2)


@dataclass(frozen=True)
class EntryAskResolution:
    """Outcome of resolving a fresh, usable ask for the entry order."""
    ask: float | None
    limit_price: float | None
    source: str          # 'fire_quote' | 'refetch' | 'none'
    age_ms: int | None   # age of the quote the ask came from (None if unknown)
    refetched: bool
    error: str | None = None


def _valid_ask(v: object) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v) and v > 0


async def resolve_entry_ask(
    state,
    *,
    symbol: str,
    strike: float,
    right: str,
    expiry: str,
    opt: dict,
    now_ms: int | None = None,
) -> EntryAskResolution:
    """Resolve the ask to price the marketable-limit entry from.

    1. Use option_pricing.ask when the fire-time quote is <=10s old.
    2. Stale/missing -> re-fetch ONCE (bounded) from the live provider.
    3. Still unusable -> (None, ...) — caller must BLOCK the fire
       (never fall back to a market order).
    """
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    ask = (opt or {}).get("ask")
    q_ts = (opt or {}).get("quote_ts_ms")
    age: int | None = None
    if isinstance(q_ts, (int, float)) and q_ts > 0:
        age = now - int(q_ts)
    if _valid_ask(ask) and age is not None and 0 <= age <= ENTRY_QUOTE_MAX_AGE_MS:
        return EntryAskResolution(
            ask=float(ask), limit_price=marketable_entry_limit(symbol, float(ask)),
            source="fire_quote", age_ms=age, refetched=False,
        )

    # Stale, unstamped, or ask-less quote — re-fetch once.
    err: str | None = None
    try:
        from fef_sidecar.ibkr.models import Contract, ContractKind
        quote = await asyncio.wait_for(
            state.ibkr.get_option_quote(Contract(
                symbol=symbol, kind=ContractKind.OPTION,
                exchange="CBOE" if symbol == "SPX" else "SMART",
                expiry=expiry, strike=float(strike), right=right,
            )),
            timeout=_REFETCH_TIMEOUT_SEC,
        )
        fresh_ask = getattr(quote, "ask", None)
        if _valid_ask(fresh_ask):
            # CHASE BOUND: the verdict's economics (entry_limit, targets, R
            # geometry) were computed at the fire-time quote. A re-fetched
            # ask far above that reference means the tape ran away between
            # signal and placement — paying it is the incident fill without
            # the hang. Block instead of chasing.
            chase_ref = ask if _valid_ask(ask) else (opt or {}).get("entry_limit")
            if (_valid_ask(chase_ref)
                    and float(fresh_ask) > float(chase_ref) * (1.0 + _MAX_CHASE_PCT)):
                err = (
                    f"re-fetched ask {float(fresh_ask):.2f} exceeds chase bound "
                    f"{float(chase_ref):.2f} x {1.0 + _MAX_CHASE_PCT:.2f} — "
                    f"tape ran away from the signal; refusing to chase"
                )
            else:
                return EntryAskResolution(
                    ask=float(fresh_ask),
                    limit_price=marketable_entry_limit(symbol, float(fresh_ask)),
                    source="refetch", age_ms=0, refetched=True,
                )
        else:
            err = (
                f"re-fetched quote has no usable ask "
                f"(ask={fresh_ask!r} bid={getattr(quote, 'bid', None)!r} "
                f"last={getattr(quote, 'last', None)!r})"
            )
    except (TimeoutError, asyncio.TimeoutError):
        err = f"re-fetch timed out after {_REFETCH_TIMEOUT_SEC:.0f}s"
    except Exception as e:  # noqa: BLE001 — any provider failure blocks the fire
        err = f"re-fetch failed: {type(e).__name__}: {e}"

    stale_desc = (
        f"fire-time quote age {age}ms" if age is not None
        else "fire-time quote has no quote_ts_ms/ask"
    )
    return EntryAskResolution(
        ask=None, limit_price=None, source="none", age_ms=age, refetched=True,
        error=f"{stale_desc}; {err}",
    )
