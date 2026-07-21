"""Streaming fire-time option quotes (2026-07-21 starvation fix).

THE PROBLEM FAMILY (three live receipts, one root cause): fire-time
consumers — the entry CHASE's re-post ask (entry_chase.py via
live_trading.place_bracket) and the SPY MIRROR's entry ask (spy_mirror.py)
— fetched quotes via request/response SNAPSHOTS on the sidecar's single
shared IBKR connection at the exact moment it is busiest (entry placement,
confirms, watcher polls). Live 7/20-7/21: the chase re-fetch (1.5s) and the
mirror ask re-fetch (6s) both timed out AT FIRE TIME while TWS quotes were
healthy on a free connection. Safety held (no stale chase, no blind MKT)
but fills were lost.

THE FIX — subscribe, don't request: at fire-commit, open a STREAMING
`reqMktData(contract, snapshot=False)` subscription for the entry option
contract. ib_async updates the Ticker in-place as ticks arrive push-style
(the socket reader keeps draining even while request/response calls are
queued), so every read afterwards is a MEMORY read with zero round-trip.

This module holds the transport-agnostic handle base + the env-tunable
knobs so the live provider (ibkr/live.py) and the mock (ibkr/mock.py)
share identical freshness semantics:

  * `fresh_ask(max_age)` returns the streamed ask ONLY when a tick arrived
    on THIS subscription within `max_age` seconds (default ~2s,
    FEF_QUOTE_STREAM_MAX_AGE_SEC). No fresh tick -> None -> callers apply
    the SAME NO_FRESH_QUOTE refusal as today (never chase stale, never
    blind-MKT).
  * `ever_ticked` distinguishes a subscription that DIED/never delivered
    (-> callers fall back to the legacy snapshot re-fetch path) from one
    that ticked and went stale (-> refuse, exactly today's refusal).
  * `close()` is idempotent and never raises — callers unsubscribe in
    try/finally on every resolution path (fill/cancel/timeout/exception).

Freshness is stamped from the LOCAL monotonic clock at the moment an update
carrying a usable ask is observed — never from exchange timestamps (clock
skew) and never seeded from residual Ticker state left by an earlier,
already-cancelled subscription on the same contract (unknown age).
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def quote_stream_max_age_sec() -> float:
    """Staleness gate on a streamed tick: a read is 'fresh' only when the
    last tick on the subscription is at most this old. Read per-call so an
    env flip takes hold on the next fire (same pattern as entry_chase)."""
    return max(0.1, _env_float("FEF_QUOTE_STREAM_MAX_AGE_SEC", 2.0))


def quote_stream_first_tick_wait_sec() -> float:
    """How long the SPY mirror waits for a just-opened stream's first fresh
    tick before falling back to the legacy snapshot re-fetch. The chase never
    waits on this — its first read happens naturally one chase interval after
    subscribe (never blocks the SPX placement)."""
    return max(0.0, _env_float("FEF_QUOTE_STREAM_FIRST_TICK_WAIT_SEC", 2.0))


def quote_stream_qualify_timeout_sec() -> float:
    """Bound on the contract-qualify round-trip when the caller did NOT hand
    in an already-qualified contract (the mirror path). The SPX chase path
    always passes its pre-qualified contract, so subscribing there does zero
    awaited round-trips."""
    return max(0.5, _env_float("FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC", 3.0))


def _valid_price(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # NaN != NaN; IBKR uses -1/0 for 'no quote on this side'.
    if f != f or f <= 0:
        return None
    return f


class QuoteStreamBase:
    """Handle for one short-lived streaming option-quote subscription.

    Transport subclasses call `_stamp(bid, ask)` whenever an update with a
    usable ask is observed; consumers call `fresh_ask()` (memory read, never
    a round-trip) and `close()` (idempotent, never raises)."""

    def __init__(self) -> None:
        self._last_ask: float | None = None
        self._last_bid: float | None = None
        self._last_tick_mono: float | None = None
        self._closed = False

    # -- producer side (transport) ------------------------------------------
    def _stamp(self, bid: object, ask: object) -> None:
        """Record an observed update. Freshness advances ONLY when the update
        carries a usable ask (>0, finite) — a one-sided/askless market must
        read as NOT fresh so consumers refuse rather than chase it."""
        a = _valid_price(ask)
        if a is None:
            return
        self._last_ask = a
        b = _valid_price(bid)
        if b is not None:
            self._last_bid = b
        self._last_tick_mono = time.monotonic()

    # -- consumer side -------------------------------------------------------
    @property
    def ever_ticked(self) -> bool:
        """True once ANY usable tick arrived on this subscription. False
        means the subscription is dead/slow — callers fall back to the
        legacy snapshot re-fetch instead of refusing outright."""
        return self._last_tick_mono is not None

    @property
    def closed(self) -> bool:
        return self._closed

    def tick_age_sec(self) -> float | None:
        """Age of the last usable tick, or None if none ever arrived."""
        if self._last_tick_mono is None:
            return None
        return max(0.0, time.monotonic() - self._last_tick_mono)

    def fresh_ask(self, max_age_sec: float | None = None) -> float | None:
        """The streamed ask iff the last usable tick is within the age gate
        (default FEF_QUOTE_STREAM_MAX_AGE_SEC). Pure memory read. A closed
        handle is never fresh."""
        if self._closed:
            return None
        age = self.tick_age_sec()
        if age is None:
            return None
        gate = quote_stream_max_age_sec() if max_age_sec is None else max_age_sec
        if age > gate:
            return None
        return self._last_ask

    def fresh_bid(self, max_age_sec: float | None = None) -> float | None:
        """Streamed bid under the same freshness gate (diagnostics only —
        fire-time consumers price off the ask)."""
        if self.fresh_ask(max_age_sec) is None:
            return None
        return self._last_bid

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        """Unsubscribe. Idempotent; NEVER raises (this runs in the finally of
        the live order-placement path — an unsubscribe error must not mask or
        break the placement result)."""
        if self._closed:
            return
        self._closed = True
        try:
            self._do_close()
        except Exception:
            logger.exception("quote stream close failed (subscription may "
                             "linger until disconnect — harmless)")

    def _do_close(self) -> None:  # pragma: no cover - overridden by transports
        """Transport-specific unsubscribe (cancelMktData / mock bookkeeping)."""
