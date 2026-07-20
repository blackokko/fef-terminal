"""Bounded-chase entry placement (2026-07-20 fill-rate fix).

ROOT CAUSE (diagnosed live 2026-07-20): the marketable-limit entry
(entry_pricing.py) posts a STATIC LMT at (fire-time ask + buffer:
max($0.10, 2%) capped $0.50). On the fast MOMENTUM entries this strategy
generates (buying puts into a fast SPX drop) the option ask climbs faster
than that static limit, so the order never fills and the 45s entry timeout
(entry_timeout.py) cancels it. Live fill rate 2026-07-20: SPX HIGH fires
1-of-5 (20%); penny-wide SPY filled fine.

THE FIX chases the moving ask WITHOUT reintroducing the naked-MKT hang a raw
MarketOrder caused on 2026-07-16 (IBKR's price-cap algorithm RESTED a MKT
option order for 4m20s on a running ask):

  1. Post the marketable LMT (exactly as today).
  2. If unfilled after CHASE_INTERVAL_SEC, CANCEL and RE-POST at the CURRENT
     (re-fetched) ask + the same buffer — chasing the climbing ask. Repeat up
     to MAX_REPOSTS times. Every re-post is itself a price-BOUNDED marketable
     limit, so no single order can hang at an unbounded price.
  3. If STILL unfilled after MAX_REPOSTS, send ONE MarketOrder as the FINAL
     fallback — the only naked-MKT path, taken only in the rare
     no-fill-after-chase case. MKT_FALLBACK can be disabled for chase-only.

LIVE-PATH INVARIANTS (this runs on the real trading path — full self-review
in scratchpad/bounded_chase_report.md):

  * NEVER OVERSELL. A new order (chase re-post OR the MKT fallback) is placed
    ONLY after the previous order is CONFIRMED terminal with a KNOWN filled
    count, and only when that count is ZERO. ANY fill — partial or full, even
    one that lands in the async cancel window — STOPS the chase and returns
    that order for the existing 45s entry-timeout / watcher fill-sync to
    manage. A cancel we cannot confirm ALSO stops the chase (a second live BUY
    beside an un-cancelled first = oversell). So fills only ever accumulate on
    the ONE order this helper returns, which is exactly what the watcher's
    _sync_parent_fill_from_trades attributes against (single ib_order_id).

  * 45s CEILING PRESERVED. The chase is a fast inner loop bounded by
    HARD_BUDGET_SEC (default 20s, well under 45s). place_bracket arms the
    existing entry_timeout on the FINAL order for the REMAINING budget
    (entry_timeout_sec - chase_elapsed), so the whole entry — chase + rest,
    including a rested MKT fallback — is bounded by the SAME 45s. The 7/16
    hang therefore cannot recur unbounded: the timeout cancels a rested MKT.

  * NO-FRESH-QUOTE PRESERVED. Each re-post prices off a FRESH re-fetched ask.
    If the re-fetch yields no usable ask we do NOT chase off a stale quote and
    do NOT blindly MKT — the chase stops and the last good, price-bounded
    limit rests under the 45s ceiling (the post-fire analog of the pre-fire
    NO_FRESH_QUOTE block).

  * TICK GRID PRESERVED. Re-post prices come from the refetch callback, which
    returns marketable_entry_limit() output — already rounded to the
    contract's minimum price increment.

Scope: the watcher-managed parent-only entry (spx_points / fixed_dollar),
which is what auto-trade AND the SPY mirror place live — the same scope
entry_timeout owns. The legacy option_price bracket (parent + attached
children in one shot) is NOT chased: cancel/replacing a parent with attached
children has ambiguous exchange-side child semantics (entry_timeout avoids it
for the identical reason).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# IBKR order statuses that mean the order is no longer working (mirror of
# entry_timeout._TERMINAL_IB_STATUSES).
_TERMINAL_IB_STATUSES = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


# ---------------------------------------------------------------------------
# Tunable knobs — env-overridable so the operator can retune (or kill) the
# chase without a code change. Read per-call so a /config env flip takes hold
# on the next fire.
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("true", "1", "yes", "on")


def chase_enabled() -> bool:
    """Master switch. False => single static marketable-limit post + 45s
    timeout, i.e. behaviour identical to before the chase (instant kill)."""
    return _env_bool("FEF_ENTRY_CHASE_ENABLED", True)


def chase_interval_sec() -> float:
    """Poll window before a re-post: unfilled this long => cancel + chase."""
    return max(0.05, _env_float("FEF_ENTRY_CHASE_INTERVAL_SEC", 0.7))


def chase_max_reposts() -> int:
    """Max cancel/re-post cycles before the MKT fallback (0 => no chase)."""
    return max(0, _env_int("FEF_ENTRY_CHASE_MAX_REPOSTS", 3))


def chase_mkt_fallback() -> bool:
    """Send ONE MarketOrder after the re-posts are exhausted. False =>
    chase-only: leave the last resting limit for the 45s timeout to cancel."""
    return _env_bool("FEF_ENTRY_CHASE_MKT_FALLBACK", True)


def chase_cancel_wait_sec() -> float:
    """Bounded wait for an IBKR cancel to CONFIRM terminal before we re-post
    or MKT — the oversell guard depends on this confirmation."""
    return max(0.05, _env_float("FEF_ENTRY_CHASE_CANCEL_WAIT_SEC", 1.0))


def chase_poll_sec() -> float:
    """Inner poll granularity for detecting a fill within an interval/wait."""
    return max(0.01, _env_float("FEF_ENTRY_CHASE_POLL_SEC", 0.1))


def chase_refetch_timeout_sec() -> float:
    """Bound on the per-re-post ask re-fetch round-trip (used by the caller's
    refetch callback) so a wedged quote call can't stall the chase."""
    return max(0.1, _env_float("FEF_ENTRY_CHASE_REFETCH_TIMEOUT_SEC", 1.5))


def chase_hard_budget_sec() -> float:
    """Absolute ceiling on total chase wall-time, well under the 45s entry
    timeout so the remaining budget handed to entry_timeout stays healthy."""
    return max(1.0, _env_float("FEF_ENTRY_CHASE_HARD_BUDGET_SEC", 20.0))


def chase_monitor_floor_sec() -> float:
    """Minimum entry-timeout budget the CALLER must reserve/hand to the MKT
    fallback so a fired market order is ALWAYS armed with a cancellable
    watchdog — the 2026-07-16 price-cap hang can only recur on an UNMONITORED
    MKT, so the caller keeps the chase deadline this far below the timeout and
    floors the post-chase budget to it whenever the fallback fires."""
    return max(1.0, _env_float("FEF_ENTRY_CHASE_MONITOR_FLOOR_SEC", 10.0))


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class ChaseResult:
    """Outcome of a bounded-chase placement. `trade` is the FINAL order the
    caller must sync the DB ib_order_id to and arm the entry-timeout on."""
    trade: object                 # final working/filled ib_async Trade
    limit_price: float | None     # final limit ($); None iff the MKT fired
    used_market: bool             # the MKT fallback fired
    outcome: str                  # summary tag (see the loop below)
    reposts: int                  # cancel/re-post cycles actually performed
    steps: list = field(default_factory=list)  # compact per-step audit trail


def _filled(trade) -> int:
    try:
        return int(float(getattr(trade.orderStatus, "filled", 0) or 0))
    except Exception:
        return 0


def _status(trade) -> str:
    try:
        return str(getattr(trade.orderStatus, "status", "") or "")
    except Exception:
        return ""


def _order_id(trade):
    try:
        return getattr(getattr(trade, "order", None), "orderId", None)
    except Exception:
        return None


async def _safe_hook(hook: Callable[[object], Awaitable[None]] | None, trade) -> None:
    if hook is None:
        return
    try:
        await hook(trade)
    except Exception:
        logger.exception("entry chase: on_order_placed hook raised")


async def _safe_refetch(refetch_limit: Callable[[], Awaitable[float | None]]) -> float | None:
    try:
        v = await refetch_limit()
    except Exception:
        logger.exception("entry chase: refetch_limit raised")
        return None
    try:
        if v is not None and math.isfinite(float(v)) and float(v) > 0:
            return float(v)
    except (TypeError, ValueError):
        return None
    return None


async def _poll_until_fill_or_interval(
    trade, qty: int, window_sec: float, poll_sec: float, deadline: float | None,
) -> str:
    """Poll `trade` for up to `window_sec`. Returns 'filled' (>=qty),
    'terminal' (order died with <qty filled), 'deadline' (hard budget hit), or
    'timeout' (window elapsed, still working)."""
    end = time.monotonic() + max(0.0, window_sec)
    while True:
        if _filled(trade) >= qty:
            return "filled"
        if _status(trade) in _TERMINAL_IB_STATUSES:
            return "terminal"
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            return "deadline"
        if now >= end:
            return "timeout"
        await asyncio.sleep(min(poll_sec, max(0.01, end - now)))


async def _cancel_and_confirm(
    ib, trade, qty: int, cancel_wait_sec: float, poll_sec: float, deadline: float | None,
) -> tuple[bool, int]:
    """Request a cancel (unless already terminal) and wait up to
    `cancel_wait_sec` for a terminal status. Returns (confirmed_terminal,
    filled). `confirmed_terminal` True means it is SAFE (no oversell) to place
    the next order for the qty NOT filled here."""
    if _status(trade) not in _TERMINAL_IB_STATUSES:
        try:
            ib.cancelOrder(trade.order)
        except Exception:
            logger.exception("entry chase: cancelOrder raised for order %s", _order_id(trade))
            # Cannot cancel -> treat as unconfirmed; caller will NOT re-post/MKT.
            return (False, _filled(trade))
    end = time.monotonic() + max(0.0, cancel_wait_sec)
    while _status(trade) not in _TERMINAL_IB_STATUSES and _filled(trade) < qty:
        now = time.monotonic()
        if now >= end or (deadline is not None and now >= deadline):
            break
        await asyncio.sleep(min(poll_sec, max(0.01, end - now)))
    f = _filled(trade)
    confirmed = _status(trade) in _TERMINAL_IB_STATUSES or f >= qty
    return (confirmed, f)


async def place_entry_with_chase(
    *,
    ib,
    ib_contract,
    symbol: str,
    qty: int,
    initial_limit: float,
    make_limit_order: Callable[[int, float], object],
    make_market_order: Callable[[int], object],
    refetch_limit: Callable[[], Awaitable[float | None]],
    on_order_placed: Callable[[object], Awaitable[None]] | None = None,
    interval_sec: float | None = None,
    max_reposts: int | None = None,
    mkt_fallback: bool | None = None,
    cancel_wait_sec: float | None = None,
    poll_sec: float | None = None,
    deadline_monotonic: float | None = None,
) -> ChaseResult:
    """Place the marketable-limit parent BUY and bounded-chase the ask.

    `refetch_limit()` returns a FRESH marketable limit ($, tick-rounded) or
    None (no usable/fresh ask). `on_order_placed(trade)` (optional) is awaited
    after every (re)placement so the caller can keep the DB ib_order_id on the
    currently-working order. See the module docstring for the invariants.
    """
    interval = chase_interval_sec() if interval_sec is None else max(0.01, float(interval_sec))
    n_reposts = chase_max_reposts() if max_reposts is None else max(0, int(max_reposts))
    do_mkt = chase_mkt_fallback() if mkt_fallback is None else bool(mkt_fallback)
    c_wait = chase_cancel_wait_sec() if cancel_wait_sec is None else max(0.01, float(cancel_wait_sec))
    poll = chase_poll_sec() if poll_sec is None else max(0.01, float(poll_sec))

    steps: list = []
    current_limit = float(initial_limit)

    # Initial marketable-limit post (identical to the pre-chase behaviour).
    trade = ib.placeOrder(ib_contract, make_limit_order(qty, current_limit))
    await _safe_hook(on_order_placed, trade)
    steps.append({"action": "post", "limit": current_limit, "order_id": _order_id(trade)})

    reposts = 0
    for _ in range(n_reposts):
        outcome = await _poll_until_fill_or_interval(trade, qty, interval, poll, deadline_monotonic)
        f = _filled(trade)
        if f >= qty:
            return ChaseResult(trade, current_limit, False, "filled", reposts, steps)
        if f > 0:
            # Partial while working -> stop chasing; the 45s timeout owns the
            # remainder (never cancel/re-post a partially-filled order).
            steps.append({"action": "partial_stop", "filled": f})
            return ChaseResult(trade, current_limit, False, "partial_stop", reposts, steps)
        if outcome == "terminal":
            # Terminal with zero fill => an EXTERNAL cancel (kill-switch / TWS).
            # Do NOT re-post a fresh BUY against a deliberate cancel.
            steps.append({"action": "external_cancel"})
            return ChaseResult(trade, current_limit, False, "external_cancel", reposts, steps)
        if outcome == "deadline":
            steps.append({"action": "deadline"})
            return ChaseResult(trade, current_limit, False, "deadline", reposts, steps)

        # Zero-fill after the interval -> price a FRESH re-post (no stale chase).
        new_limit = await _safe_refetch(refetch_limit)
        if new_limit is None:
            # NO FRESH QUOTE: don't chase stale, don't blindly MKT. Leave the
            # resting price-bounded limit for the 45s ceiling.
            steps.append({"action": "no_fresh_quote"})
            return ChaseResult(trade, current_limit, False, "no_fresh_quote", reposts, steps)
        if new_limit <= current_limit:
            # Ask hasn't climbed above our resting marketable limit — a re-post
            # would not improve fill odds. Keep the working order; keep polling.
            steps.append({"action": "no_climb", "refetched": new_limit})
            continue

        cancelled, f_after = await _cancel_and_confirm(
            ib, trade, qty, c_wait, poll, deadline_monotonic)
        if f_after >= qty:
            steps.append({"action": "filled_during_cancel", "filled": f_after})
            return ChaseResult(trade, current_limit, False, "filled_during_cancel", reposts, steps)
        if f_after > 0:
            # Partial landed in the cancel window -> stop; keep for the timeout.
            steps.append({"action": "partial_during_cancel", "filled": f_after})
            return ChaseResult(trade, current_limit, False, "partial_during_cancel", reposts, steps)
        if not cancelled:
            # Cancel unconfirmed -> a second live BUY would be OVERSELL. Stop;
            # leave the (maybe still-working) order for the 45s timeout.
            steps.append({"action": "cancel_unconfirmed"})
            return ChaseResult(trade, current_limit, False, "cancel_unconfirmed", reposts, steps)

        # Confirmed cancelled, zero fill -> SAFE to re-post the full qty.
        current_limit = new_limit
        trade = ib.placeOrder(ib_contract, make_limit_order(qty, current_limit))
        await _safe_hook(on_order_placed, trade)
        reposts += 1
        steps.append({"action": "repost", "limit": current_limit, "order_id": _order_id(trade)})

    # Re-posts exhausted — one final poll on the last working order.
    outcome = await _poll_until_fill_or_interval(trade, qty, interval, poll, deadline_monotonic)
    f = _filled(trade)
    if f >= qty:
        return ChaseResult(trade, current_limit, False, "filled", reposts, steps)
    if f > 0:
        steps.append({"action": "partial_stop", "filled": f})
        return ChaseResult(trade, current_limit, False, "partial_stop", reposts, steps)
    if outcome == "terminal":
        steps.append({"action": "external_cancel"})
        return ChaseResult(trade, current_limit, False, "external_cancel", reposts, steps)
    if outcome == "deadline":
        # Deadline hit during the FINAL poll — must NOT fall through to a MKT
        # placed past the ceiling: that market order could rest with a ~0
        # entry-timeout budget = an UNMONITORED naked MKT (the 2026-07-16
        # hang). Leave the resting price-bounded limit for the timeout instead.
        steps.append({"action": "deadline"})
        return ChaseResult(trade, current_limit, False, "deadline", reposts, steps)

    if not do_mkt:
        # Chase-only: leave the last resting limit for the 45s timeout.
        steps.append({"action": "chase_only_rest"})
        return ChaseResult(trade, current_limit, False, "chase_exhausted_rest", reposts, steps)

    # FINAL MKT FALLBACK — the ONLY naked-MKT path. The caller passes do_mkt=
    # True ONLY when it will arm a live, positive-budget entry-timeout on the
    # returned MKT trade (see place_bracket): a market order is never left
    # unmonitored. Cancel + CONFIRM the working limit terminal first (never two
    # live BUYs), then place ONE MKT.
    cancelled, f_after = await _cancel_and_confirm(
        ib, trade, qty, c_wait, poll, deadline_monotonic)
    if f_after >= qty:
        steps.append({"action": "filled_during_cancel", "filled": f_after})
        return ChaseResult(trade, current_limit, False, "filled_during_cancel", reposts, steps)
    if f_after > 0:
        steps.append({"action": "partial_during_cancel", "filled": f_after})
        return ChaseResult(trade, current_limit, False, "partial_during_cancel", reposts, steps)
    if not cancelled:
        # Can't confirm the limit is dead -> a MKT now would be OVERSELL. Leave
        # the limit for the 45s ceiling instead of going naked-MKT blind.
        steps.append({"action": "mkt_skipped_cancel_unconfirmed"})
        return ChaseResult(trade, current_limit, False, "cancel_unconfirmed", reposts, steps)

    mkt_trade = ib.placeOrder(ib_contract, make_market_order(qty))
    await _safe_hook(on_order_placed, mkt_trade)
    steps.append({"action": "mkt_fallback", "order_id": _order_id(mkt_trade)})
    return ChaseResult(mkt_trade, None, True, "mkt_fallback", reposts, steps)
