"""Streaming fire-time quotes (2026-07-21 starvation fix) — tests.

Contract under test (ibkr/quote_stream.py + ibkr/live.py + ibkr/mock.py +
the live_trading chase wiring + the spy_mirror entry-ask wiring):

  * At fire-commit place_bracket opens a STREAMING reqMktData subscription
    for the entry contract; the chase's re-post asks are MEMORY reads off
    the streamed ticker (values that MUTATE during the chase are picked up
    with zero round-trips).
  * Stale stream (ticked, then no fresh tick within the age gate) -> the
    SAME refusal as today: no_fresh_quote outcome, no stale re-post, no
    blind MKT — even with the MKT fallback enabled.
  * Subscribe failure OR a stream that NEVER ticks -> the legacy snapshot
    re-fetch path (today's behaviour), then today's refusal.
  * Unsubscribe on EVERY resolution path — fill / external cancel /
    deadline (timeout) / rest / exception — exactly once (close is
    idempotent; the finally must not double-cancel).
  * The SPY mirror prices its entry off ITS streamed ask with the premium
    guard unchanged, closes the stream BEFORE place_bracket (never two
    subscriptions on one contract), and falls back to resolve_entry_ask on
    any stream problem.
  * f60fb6f watchdog invariants hold on the NEW stream path: a chase MKT
    fallback is always armed with a live positive-budget entry-timeout
    (>= the monitor floor); a disabled timeout downgrades to chase-only.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from sqlmodel import select

from fef_sidecar.config_store import UIConfig
from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import LiveOrder
from fef_sidecar.engine import spy_mirror as sm
from fef_sidecar.engine.entry_pricing import marketable_entry_limit
from fef_sidecar.engine.live_trading import BracketSpec, place_bracket
from fef_sidecar.engine.spy_mirror import maybe_place_spy_mirror
from fef_sidecar.ibkr import quote_stream as qs
from fef_sidecar.ibkr.live import LiveIBKRProvider
from fef_sidecar.ibkr.mock import MockIBKRProvider, MockOptionQuoteStream

_TERMINAL = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


@pytest.fixture
async def _db():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.fixture(autouse=True)
def _clean_stream_env(monkeypatch: pytest.MonkeyPatch):
    for k in ("FEF_QUOTE_STREAM_MAX_AGE_SEC",
              "FEF_QUOTE_STREAM_FIRST_TICK_WAIT_SEC",
              "FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC"):
        monkeypatch.delenv(k, raising=False)


# ===========================================================================
# A. Handle semantics (QuoteStreamBase via the mock transport)
# ===========================================================================


def test_fresh_ask_lifecycle():
    s = MockOptionQuoteStream()
    # Never ticked: not fresh, and distinguishable as never-ticked.
    assert s.fresh_ask() is None
    assert s.ever_ticked is False
    assert s.tick_age_sec() is None
    # A tick arrives -> fresh memory read.
    s.push_quote(5.60, 5.70)
    assert s.ever_ticked is True
    assert s.fresh_ask() == pytest.approx(5.70)
    assert s.fresh_bid() == pytest.approx(5.60)
    assert s.tick_age_sec() is not None and s.tick_age_sec() < 1.0
    # Ages past the gate -> stale (ever_ticked stays True: refusal, not
    # fallback, is the correct consumer response).
    s.rewind_tick_age(10.0)
    assert s.fresh_ask() is None
    assert s.ever_ticked is True
    # A new tick refreshes.
    s.push_quote(6.00, 6.10)
    assert s.fresh_ask() == pytest.approx(6.10)
    # Closed handles are never fresh; close is idempotent.
    s.close()
    assert s.closed is True
    assert s.fresh_ask() is None
    assert s.close_count == 1
    s.close()
    assert s.close_count == 1


def test_stamp_requires_usable_ask():
    s = MockOptionQuoteStream()
    s.push_quote(1.00, None)      # askless -> no freshness
    s.push_quote(1.00, -1)        # IBKR 'no quote' sentinel -> no freshness
    s.push_quote(1.00, float("nan"))
    assert s.ever_ticked is False
    assert s.fresh_ask() is None
    s.push_quote(None, 0.55)      # one-sided but ask present -> fresh
    assert s.fresh_ask() == pytest.approx(0.55)
    assert s.fresh_bid() is None  # no usable bid was ever seen


def test_stream_env_knobs(monkeypatch: pytest.MonkeyPatch):
    assert qs.quote_stream_max_age_sec() == pytest.approx(2.0)
    assert qs.quote_stream_first_tick_wait_sec() == pytest.approx(2.0)
    # 8.0 since 2026-07-30 (was 3.0 — starved every mirror fire from 7/28 on).
    # See test_qualify_cache.py for the budget's own tests.
    assert qs.quote_stream_qualify_timeout_sec() == pytest.approx(8.0)
    monkeypatch.setenv("FEF_QUOTE_STREAM_MAX_AGE_SEC", "0.5")
    monkeypatch.setenv("FEF_QUOTE_STREAM_FIRST_TICK_WAIT_SEC", "0.1")
    monkeypatch.setenv("FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC", "1.5")
    assert qs.quote_stream_max_age_sec() == pytest.approx(0.5)
    assert qs.quote_stream_first_tick_wait_sec() == pytest.approx(0.1)
    assert qs.quote_stream_qualify_timeout_sec() == pytest.approx(1.5)
    # Garbage never crashes the fire path — falls back to defaults.
    monkeypatch.setenv("FEF_QUOTE_STREAM_MAX_AGE_SEC", "garbage")
    assert qs.quote_stream_max_age_sec() == pytest.approx(2.0)
    # The stale gate drives fresh_ask when no explicit age is passed.
    monkeypatch.setenv("FEF_QUOTE_STREAM_MAX_AGE_SEC", "0.2")
    s = MockOptionQuoteStream()
    s.push_quote(5.0, 5.1)
    s.rewind_tick_age(0.3)
    assert s.fresh_ask() is None
    assert s.fresh_ask(max_age_sec=1.0) == pytest.approx(5.1)


# ===========================================================================
# B. place_bracket chase wiring (the SPX leg's fire-time consumer)
# ===========================================================================


class _FakeIB:
    """Duck-typed ib_async.IB for place_bracket's live block. Orders fill /
    cancel per hooks; `enforce_single_live` is the oversell tripwire."""

    def __init__(self, qty: int = 3, *, fill_on_index: int | None = None,
                 on_place=None, raise_on_place: bool = False):
        self.qty = qty
        self.placed: list = []
        self.cancels: list = []
        self._next = 900
        self.fill_on_index = fill_on_index
        self.on_place = on_place
        self.raise_on_place = raise_on_place

    async def qualifyContractsAsync(self, *contracts):
        return list(contracts)

    def placeOrder(self, contract, order):
        if self.raise_on_place:
            raise RuntimeError("TWS rejected the placement (scripted)")
        idx = len(self.placed)
        if idx > 0:
            prev = self.placed[idx - 1][1]
            assert prev.orderStatus.status in _TERMINAL, (
                f"OVERSELL: order #{idx} placed while #{idx - 1} still "
                f"{prev.orderStatus.status!r}"
            )
        self._next += 1
        tr = SimpleNamespace(
            order=SimpleNamespace(orderId=self._next, permId=self._next + 7000,
                                  action="BUY"),
            orderStatus=SimpleNamespace(status="Submitted", filled=0,
                                        avgFillPrice=0.0),
        )
        self.placed.append((order, tr))
        if self.fill_on_index is not None and idx == self.fill_on_index:
            tr.orderStatus.filled = self.qty
            tr.orderStatus.status = "Filled"
        if self.on_place is not None:
            self.on_place(order, tr, idx)
        return tr

    def cancelOrder(self, order):
        self.cancels.append(order)
        for _o, tr in self.placed:
            if tr.order is order and tr.orderStatus.status not in _TERMINAL:
                tr.orderStatus.status = "Cancelled"

    def market_orders(self) -> list:
        return [o for o, _ in self.placed if getattr(o, "orderType", None) == "MKT"]

    def limit_prices(self) -> list[float]:
        return [float(o.lmtPrice) for o, _ in self.placed
                if getattr(o, "orderType", None) == "LMT"]


class _StreamProvider(LiveIBKRProvider):
    """Passes place_bracket's isinstance gate without any TWS connection;
    serves a controllable stream handle + a scripted legacy snapshot quote."""

    def __init__(self, ib, *, stream=None, open_error: Exception | None = None,
                 quote_asks=()):
        self._ib = ib
        self._account_id = "DU_TEST"
        self._account_class = "paper"
        self.stream = stream
        self.open_error = open_error
        self._asks = iter(quote_asks)
        self.open_calls = 0
        self.legacy_calls = 0

    async def open_option_quote_stream(self, contract, *, ib_contract=None,
                                       qualify_timeout_sec=None):
        self.open_calls += 1
        if self.open_error is not None:
            raise self.open_error
        return self.stream

    async def get_option_quote(self, contract):
        self.legacy_calls += 1
        try:
            a = next(self._asks)
        except StopIteration:
            a = None
        return SimpleNamespace(ask=a, bid=(a - 0.1) if a else None, last=a)


def _spec(entry_timeout_sec: float = 10.0) -> BracketSpec:
    return BracketSpec(
        symbol="SPX", strike=7520.0, right="P", expiry="20260721", qty=3,
        entry_limit=5.70, t1_limit=5.70, t2_limit=5.70, t3_limit=5.70,
        stop_limit=5.70, t1_qty=1, t2_qty=1, t3_qty=1,
        exit_strategy="spx_points",
        spx_t1=7510.0, spx_t2=7500.0, spx_t3=7480.0, spx_stop=7525.0,
        entry_timeout_sec=entry_timeout_sec,
    )


def _wire_live(monkeypatch: pytest.MonkeyPatch, captured: list, **env) -> None:
    monkeypatch.setattr(
        "fef_sidecar.engine.live_trading.is_live_trading_enabled", lambda: True)
    monkeypatch.setattr(
        "fef_sidecar.engine.live_trading.require_paper_account", lambda: False)
    defaults = dict(
        FEF_ENTRY_CHASE_ENABLED="true", FEF_ENTRY_CHASE_MKT_FALLBACK="true",
        FEF_ENTRY_CHASE_MAX_REPOSTS="2", FEF_ENTRY_CHASE_INTERVAL_SEC="0.02",
        FEF_ENTRY_CHASE_CANCEL_WAIT_SEC="0.05", FEF_ENTRY_CHASE_POLL_SEC="0.005",
        FEF_ENTRY_CHASE_REFETCH_TIMEOUT_SEC="0.5",
        FEF_ENTRY_CHASE_HARD_BUDGET_SEC="2.0",
        FEF_ENTRY_CHASE_MONITOR_FLOOR_SEC="1.0",
    )
    defaults.update(env)
    for k, v in defaults.items():
        monkeypatch.setenv(k, v)

    def _capture_spawn(**kwargs):
        captured.append(kwargs)
        return None
    monkeypatch.setattr(
        "fef_sidecar.engine.entry_timeout.spawn_entry_timeout_monitor",
        _capture_spawn)


def _state() -> SimpleNamespace:
    return SimpleNamespace(auto_trade_count_by_symbol={},
                           auto_trade_count_session=0)


async def test_chase_reposts_off_mutating_stream_ticker(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """The core fix: re-post asks come from the streamed ticker, whose values
    MUTATE while the chase runs (pushed between iterations by the fake feed).
    The legacy snapshot re-fetch is NEVER touched."""
    captured: list = []
    _wire_live(monkeypatch, captured, FEF_ENTRY_CHASE_MKT_FALLBACK="false")
    stream = MockOptionQuoteStream()
    stream.push_quote(5.50, 5.60)  # first ticks arrive right after subscribe

    climbing = iter([6.50, 7.00, 7.40])

    def on_place(order, tr, idx):
        # A fresh tick lands DURING the chase, after each placement.
        try:
            a = next(climbing)
        except StopIteration:
            return
        stream.push_quote(a - 0.10, a)

    ib = _FakeIB(on_place=on_place)
    provider = _StreamProvider(ib, stream=stream,
                               quote_asks=[99.0])  # sentinel: must stay unread

    result = await place_bracket(provider, _spec(), confirm=True,
                                 source="auto_main", state=_state())
    assert result["ok"] is True
    assert result["entry_chase"]["outcome"] == "chase_exhausted_rest"
    assert result["entry_chase"]["reposts"] == 2
    # Initial post at the spec limit; each re-post priced off the ask the
    # stream carried AT THAT ITERATION (6.50 -> 6.70, 7.00 -> 7.20 on the
    # SPX dime grid with the max($0.10, 2%) buffer).
    assert ib.limit_prices() == [
        pytest.approx(5.70),
        pytest.approx(marketable_entry_limit("SPX", 6.50)),
        pytest.approx(marketable_entry_limit("SPX", 7.00)),
    ]
    assert ib.limit_prices()[1] == pytest.approx(6.70)
    assert ib.limit_prices()[2] == pytest.approx(7.20)
    # Zero request/response quote fetches at fire time.
    assert provider.legacy_calls == 0
    assert provider.open_calls == 1
    # Unsubscribed exactly once on resolution.
    assert stream.close_count == 1


async def test_stale_stream_refuses_no_stale_repost_no_blind_mkt(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """NO_FRESH_QUOTE semantics preserved EXACTLY: a stream that ticked but
    has no fresh tick at re-post time refuses (rests the last price-bounded
    limit) — no stale re-post, no MKT even though the fallback is enabled,
    and NO legacy snapshot call (that's the starving path we're replacing)."""
    captured: list = []
    _wire_live(monkeypatch, captured)  # MKT fallback ENABLED
    stream = MockOptionQuoteStream()
    stream.push_quote(5.50, 5.60)
    stream.rewind_tick_age(10.0)  # ticked once, long ago -> stale at read time

    ib = _FakeIB()
    provider = _StreamProvider(ib, stream=stream, quote_asks=[99.0])

    result = await place_bracket(provider, _spec(), confirm=True,
                                 source="auto_main", state=_state())
    assert result["ok"] is True
    assert result["entry_chase"]["outcome"] == "no_fresh_quote"
    assert result["entry_chase"]["used_market"] is False
    assert result["entry_chase"]["reposts"] == 0
    assert ib.market_orders() == []          # never blind-MKT off a stale quote
    assert len(ib.placed) == 1               # only the initial bounded limit
    assert ib.cancels == []                  # left resting for the 45s ceiling
    assert provider.legacy_calls == 0        # refusal, NOT a snapshot fallback
    assert stream.close_count == 1
    # The resting limit is still under the entry-timeout watchdog.
    assert len(captured) == 1
    assert captured[0]["timeout_sec"] > 0
    assert captured[0]["limit_price"] == pytest.approx(5.70)


async def test_subscribe_failure_falls_back_to_legacy_refetch(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """Fail-soft: the subscribe raising leaves the chase on the legacy
    snapshot re-fetch path — behaviour identical to before this fix,
    including the f60fb6f MKT-fallback watchdog invariant."""
    captured: list = []
    _wire_live(monkeypatch, captured, FEF_ENTRY_CHASE_MAX_REPOSTS="1")
    ib = _FakeIB()
    provider = _StreamProvider(ib, open_error=RuntimeError("no md line"),
                               quote_asks=[6.50, 7.00, 7.50])

    result = await place_bracket(provider, _spec(entry_timeout_sec=10.0),
                                 confirm=True, source="auto_main",
                                 state=_state())
    assert result["ok"] is True
    assert provider.open_calls == 1
    assert provider.legacy_calls >= 1        # legacy path exercised
    assert result["entry_chase"]["reposts"] == 1
    assert result["entry_chase"]["outcome"] == "mkt_fallback"
    # Re-post priced off the legacy re-fetched ask.
    assert ib.limit_prices()[1] == pytest.approx(marketable_entry_limit("SPX", 6.50))
    # f60fb6f invariant on this path: the MKT is armed with a live,
    # positive-budget watchdog >= the reserved monitor floor.
    assert len(ib.market_orders()) == 1
    assert len(captured) == 1
    assert captured[0]["timeout_sec"] >= 1.0
    assert captured[0]["limit_price"] is None


async def test_never_ticked_stream_closed_then_legacy_used(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """A subscription that never delivers a tick is a subscription ERROR,
    not a stale market: the chase closes it (no leaked md line while the
    legacy path re-subscribes the same contract) and falls back to the
    legacy re-fetch."""
    captured: list = []
    _wire_live(monkeypatch, captured, FEF_ENTRY_CHASE_MAX_REPOSTS="1",
               FEF_ENTRY_CHASE_MKT_FALLBACK="false")
    stream = MockOptionQuoteStream()  # never ticks
    ib = _FakeIB()
    provider = _StreamProvider(ib, stream=stream, quote_asks=[6.50, 7.00])

    result = await place_bracket(provider, _spec(), confirm=True,
                                 source="auto_main", state=_state())
    assert result["ok"] is True
    assert provider.legacy_calls >= 1
    assert result["entry_chase"]["reposts"] == 1
    assert ib.limit_prices()[1] == pytest.approx(marketable_entry_limit("SPX", 6.50))
    # Closed exactly once — when abandoned, and the finally didn't double it.
    assert stream.close_count == 1


async def test_unsubscribe_on_fill(monkeypatch: pytest.MonkeyPatch, _db) -> None:
    captured: list = []
    _wire_live(monkeypatch, captured)
    stream = MockOptionQuoteStream()
    stream.push_quote(5.50, 5.60)
    ib = _FakeIB(fill_on_index=0)  # fills on the initial post
    provider = _StreamProvider(ib, stream=stream)

    result = await place_bracket(provider, _spec(), confirm=True,
                                 source="auto_main", state=_state())
    assert result["ok"] is True
    assert result["entry_chase"]["outcome"] == "filled"
    assert stream.close_count == 1
    assert stream.closed is True


async def test_unsubscribe_on_external_cancel(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    captured: list = []
    _wire_live(monkeypatch, captured)
    stream = MockOptionQuoteStream()
    stream.push_quote(5.50, 5.60)

    def on_place(order, tr, idx):
        if idx == 0:
            tr.orderStatus.status = "Cancelled"  # kill-switch / TWS cancel

    ib = _FakeIB(on_place=on_place)
    provider = _StreamProvider(ib, stream=stream)

    result = await place_bracket(provider, _spec(), confirm=True,
                                 source="auto_main", state=_state())
    assert result["ok"] is True
    assert result["entry_chase"]["outcome"] == "external_cancel"
    assert stream.close_count == 1


async def test_unsubscribe_on_deadline_timeout(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """Hard-budget deadline (the chase's timeout resolution) unsubscribes.
    Chase cap here = min(hard_budget=1s floor, timeout-floor) = 1s."""
    captured: list = []
    _wire_live(monkeypatch, captured, FEF_ENTRY_CHASE_MAX_REPOSTS="10000",
               FEF_ENTRY_CHASE_HARD_BUDGET_SEC="1.0",
               FEF_ENTRY_CHASE_INTERVAL_SEC="0.01")
    stream = MockOptionQuoteStream()
    stream.push_quote(5.50, 5.60)
    val = [6.0]

    def on_place(order, tr, idx):
        val[0] += 0.25
        stream.push_quote(val[0] - 0.1, val[0])  # ever-climbing, always fresh

    ib = _FakeIB(on_place=on_place)
    provider = _StreamProvider(ib, stream=stream)

    t0 = time.monotonic()
    result = await place_bracket(provider, _spec(entry_timeout_sec=10.0),
                                 confirm=True, source="auto_main",
                                 state=_state())
    assert time.monotonic() - t0 < 5.0
    assert result["ok"] is True
    assert result["entry_chase"]["outcome"] == "deadline"
    assert result["entry_chase"]["used_market"] is False
    assert stream.close_count == 1


async def test_unsubscribe_on_exception(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """placeOrder blowing up mid-fire still unsubscribes (try/finally) and
    surfaces the existing REJECTED result."""
    captured: list = []
    _wire_live(monkeypatch, captured)
    stream = MockOptionQuoteStream()
    stream.push_quote(5.50, 5.60)
    ib = _FakeIB(raise_on_place=True)
    provider = _StreamProvider(ib, stream=stream)

    result = await place_bracket(provider, _spec(), confirm=True,
                                 source="auto_main", state=_state())
    assert result["ok"] is False
    assert "scripted" in result["error"]
    assert stream.close_count == 1
    async with get_session() as session:
        row = (await session.exec(select(LiveOrder))).first()
    assert row.status == "REJECTED"


async def test_stream_mkt_fallback_keeps_watchdog_floor(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """f60fb6f invariants intact ON the stream path: the chase exhausts off
    streamed (mutating) asks, the ONE MKT fallback fires, and it is armed
    with a live entry-timeout budget >= the reserved monitor floor."""
    captured: list = []
    _wire_live(monkeypatch, captured, FEF_ENTRY_CHASE_MAX_REPOSTS="1")
    stream = MockOptionQuoteStream()
    stream.push_quote(5.50, 5.60)
    climbing = iter([6.50, 7.00, 7.50])

    def on_place(order, tr, idx):
        try:
            a = next(climbing)
        except StopIteration:
            return
        stream.push_quote(a - 0.1, a)

    ib = _FakeIB(on_place=on_place)
    provider = _StreamProvider(ib, stream=stream, quote_asks=[99.0])

    result = await place_bracket(provider, _spec(entry_timeout_sec=10.0),
                                 confirm=True, source="auto_main",
                                 state=_state())
    assert result["ok"] is True
    assert result["entry_chase"]["outcome"] == "mkt_fallback"
    assert result["entry_chase"]["used_market"] is True
    assert len(ib.market_orders()) == 1
    mkt_trade = ib.placed[-1][1]
    assert len(captured) == 1
    assert captured[0]["timeout_sec"] >= 1.0          # >= monitor floor
    assert captured[0]["limit_price"] is None
    assert captured[0]["ib_order_id"] == mkt_trade.order.orderId
    assert provider.legacy_calls == 0                 # stream fed every re-post
    assert stream.close_count == 1
    async with get_session() as session:
        row = (await session.exec(
            select(LiveOrder).where(LiveOrder.id == result["parent_order_id"])
        )).first()
    assert row.order_type == "MKT"
    assert row.ib_order_id == mkt_trade.order.orderId


async def test_stream_timeout_disabled_still_downgrades_to_chase_only(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """f60fb6f downgrade preserved on the stream path: timeout disabled ->
    NO MKT (chase-only), no watchdog spawned, stream still unsubscribed."""
    captured: list = []
    _wire_live(monkeypatch, captured, FEF_ENTRY_CHASE_MAX_REPOSTS="1")
    stream = MockOptionQuoteStream()
    stream.push_quote(5.50, 5.60)
    climbing = iter([6.50, 7.00])

    def on_place(order, tr, idx):
        try:
            a = next(climbing)
        except StopIteration:
            return
        stream.push_quote(a - 0.1, a)

    ib = _FakeIB(on_place=on_place)
    provider = _StreamProvider(ib, stream=stream)

    result = await place_bracket(provider, _spec(entry_timeout_sec=0.0),
                                 confirm=True, source="auto_main",
                                 state=_state())
    assert result["ok"] is True
    assert ib.market_orders() == []
    assert result["entry_chase"]["used_market"] is False
    assert result["entry_chase"]["outcome"] == "chase_exhausted_rest"
    assert captured == []
    assert stream.close_count == 1


async def test_static_path_opens_no_stream(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """Chase disabled -> single static post, and NO subscription is opened
    (fire-time consumers only)."""
    captured: list = []
    _wire_live(monkeypatch, captured, FEF_ENTRY_CHASE_ENABLED="false")
    ib = _FakeIB(fill_on_index=0)
    provider = _StreamProvider(ib, stream=MockOptionQuoteStream())

    result = await place_bracket(provider, _spec(), confirm=True,
                                 source="auto_main", state=_state())
    assert result["ok"] is True
    assert provider.open_calls == 0


# ===========================================================================
# C. SPY mirror — streamed entry ask, premium guard unchanged, fail-soft
# ===========================================================================

_TODAY = time.strftime("%Y%m%d")
_SPY_SPOT = 745.00


def _patch_config(monkeypatch: pytest.MonkeyPatch, **kw):
    import fef_sidecar.config_store as cs
    cfg = UIConfig(**kw)
    monkeypatch.setattr(cs, "load_config", lambda: cfg)
    return cfg


def _patch_expiry(monkeypatch: pytest.MonkeyPatch, expiry: str = _TODAY,
                  today: str = _TODAY, hhmm: str = "10:00"):
    import fef_sidecar.engine.expiry_resolver as er

    async def fake_resolve(state, symbol):
        return expiry

    monkeypatch.setattr(er, "resolve_nearest_expiry", fake_resolve)
    monkeypatch.setattr(er, "_today_et_ymd", lambda: today)
    monkeypatch.setattr(sm, "_now_et_hhmm_ymd", lambda: (hhmm, today))


def _seed_spy_cache(state, spot) -> None:
    now_ms = int(time.time() * 1000)
    state._watcher_stock_spot_cache = {"SPY": (now_ms, spot)}
    state._watcher_stock_spot_refreshing = set()
    state._watcher_stock_spot_tasks = set()


def _mirror_state(**kw) -> SimpleNamespace:
    d = dict(
        auto_trade_count_by_symbol={},
        auto_trade_last_at_ms_by_symbol={},
        auto_trade_count_session=0,
        auto_trade_max_per_symbol=2,
        active_symbol="SPX",
        last_live_tick_spx=7500.0,
        last_spx_price=7500.0,
        exit_dollar_t1=1000.0, exit_dollar_t2=2500.0,
        exit_dollar_t3=5000.0, exit_dollar_stop=-1000.0,
        ibkr=SimpleNamespace(),
    )
    d.update(kw)
    s = SimpleNamespace(**d)
    _seed_spy_cache(s, _SPY_SPOT)
    return s


def _mirror_kwargs(**kw) -> dict:
    d = dict(
        direction="LONG_CALLS", spx_strike=7540.0, spx_qty=3,
        spx_parent_id=42, source="auto_scanner", strategy="spx_points",
        spx_t1=7515.0, spx_t2=7525.0, spx_t3=7550.0, spx_stop=7490.0,
        entry_timeout_sec=45.0,
        spx_spot_ref=7500.0,
        spx_entry_ask=5.1,
    )
    d.update(kw)
    return d


async def _live_orders() -> list[LiveOrder]:
    async with get_session() as session:
        return list((await session.exec(select(LiveOrder))).all())


def _stream_opener(handle: MockOptionQuoteStream, seen: list | None = None):
    async def opener(contract, *, ib_contract=None, qualify_timeout_sec=None):
        if seen is not None:
            seen.append(contract)
        return handle
    return opener


async def _legacy_must_not_run(contract):
    raise AssertionError("legacy snapshot fetch must not run when the "
                         "stream serves a fresh ask")


@pytest.fixture(autouse=True)
def _clean_mirror_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FEF_SPY_MIRROR_ENABLED", raising=False)
    monkeypatch.delenv("FEF_LIVE_TRADING", raising=False)


async def test_mirror_prices_off_streamed_ask(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """The mirror's entry ask is a memory read off its own streamed ticker —
    the starving snapshot path is never touched — and the resulting parent
    is priced marketable off that ask on SPY's penny grid."""
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)
    handle = MockOptionQuoteStream()
    handle.push_quote(0.50, 0.55)
    seen: list = []
    state = _mirror_state(ibkr=SimpleNamespace(
        open_option_quote_stream=_stream_opener(handle, seen),
        get_option_quote=_legacy_must_not_run,
    ))

    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["ok"] is True
    # Subscribed to the MAPPED contract (7540 @ 7500 on SPY 745 -> 749C).
    assert len(seen) == 1
    assert seen[0].symbol == "SPY" and seen[0].strike == 749.0
    assert seen[0].right == "C" and seen[0].expiry == _TODAY
    # Entry limit = marketable off the STREAMED ask (0.55 + $0.10 floor).
    p = (await _live_orders())[0]
    assert p.symbol == "SPY"
    assert p.limit_price == pytest.approx(marketable_entry_limit("SPY", 0.55))
    assert p.limit_price == pytest.approx(0.65)
    # Unsubscribed exactly once, BEFORE place_bracket ran.
    assert handle.close_count == 1


async def test_mirror_stream_closed_before_place_bracket(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """At most ONE subscription per contract: the mirror's stream must be
    CLOSED before place_bracket (whose chase opens its own stream on the
    same SPY contract) runs."""
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)
    handle = MockOptionQuoteStream()
    handle.push_quote(0.50, 0.55)
    state = _mirror_state(ibkr=SimpleNamespace(
        open_option_quote_stream=_stream_opener(handle),
        get_option_quote=_legacy_must_not_run,
    ))
    observed: dict = {}

    async def fake_place_bracket(ibkr, spec, **kwargs):
        observed["closed_at_place"] = handle.closed
        observed["entry_limit"] = spec.entry_limit
        return {"ok": True, "parent_order_id": 777}

    monkeypatch.setattr("fef_sidecar.engine.live_trading.place_bracket",
                        fake_place_bracket)
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["ok"] is True
    assert observed["closed_at_place"] is True
    assert observed["entry_limit"] == pytest.approx(0.65)
    assert handle.close_count == 1


async def test_mirror_premium_guard_unchanged_on_streamed_ask(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """The premium plausibility guard applies to the STREAMED ask exactly as
    it did to the fetched one: 2.50 vs expected ~0.51 (SPX ask 5.1 / 10)
    -> skip, no order, stream still unsubscribed."""
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)
    handle = MockOptionQuoteStream()
    handle.push_quote(2.45, 2.50)
    state = _mirror_state(ibkr=SimpleNamespace(
        open_option_quote_stream=_stream_opener(handle),
        get_option_quote=_legacy_must_not_run,
    ))
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs(spx_entry_ask=5.1))
    assert r["skipped"] == "premium_implausible"
    assert await _live_orders() == []
    assert handle.close_count == 1


async def test_mirror_subscribe_failure_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """Subscribe failure -> the EXISTING resolve_entry_ask snapshot path
    places the mirror exactly as before the fix."""
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)

    async def broken_opener(contract, *, ib_contract=None,
                            qualify_timeout_sec=None):
        raise ConnectionError("no md line")

    async def option_quote(contract):
        return SimpleNamespace(bid=0.50, ask=0.55, last=0.52)

    state = _mirror_state(ibkr=SimpleNamespace(
        open_option_quote_stream=broken_opener,
        get_option_quote=option_quote,
    ))
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["ok"] is True
    p = (await _live_orders())[0]
    assert p.limit_price == pytest.approx(0.65)


async def test_mirror_stream_never_ticks_falls_back_then_refusal_preserved(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """No fresh tick within the bounded wait -> legacy path; and when the
    legacy path ALSO finds no usable ask, the EXISTING no_fresh_quote skip
    fires — the refusal semantics are bit-identical to today."""
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)
    monkeypatch.setenv("FEF_QUOTE_STREAM_FIRST_TICK_WAIT_SEC", "0.05")
    handle = MockOptionQuoteStream()  # never ticks

    async def option_quote(contract):
        return SimpleNamespace(bid=0.50, ask=0.55, last=0.52)

    state = _mirror_state(ibkr=SimpleNamespace(
        open_option_quote_stream=_stream_opener(handle),
        get_option_quote=option_quote,
    ))
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["ok"] is True                     # legacy fallback placed it
    assert handle.close_count == 1

    # Same shape but the legacy path is dead too -> no_fresh_quote skip.
    handle2 = MockOptionQuoteStream()

    async def dead_quote(contract):
        return SimpleNamespace(bid=None, ask=None, last=None)

    state2 = _mirror_state(ibkr=SimpleNamespace(
        open_option_quote_stream=_stream_opener(handle2),
        get_option_quote=dead_quote,
    ))
    r2 = await maybe_place_spy_mirror(state2, **_mirror_kwargs())
    assert r2["skipped"] == "no_fresh_quote"
    assert handle2.close_count == 1
    assert len(await _live_orders()) == 1      # only the first test's parent


async def test_mirror_stale_stream_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """A mirror stream whose only tick is already stale behaves like
    never-fresh: bounded wait expires -> legacy path (the mirror has no
    resting order to protect, so fallback — not refusal — is today's
    behaviour here)."""
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)
    monkeypatch.setenv("FEF_QUOTE_STREAM_FIRST_TICK_WAIT_SEC", "0.05")
    handle = MockOptionQuoteStream()
    handle.push_quote(0.50, 0.55)
    handle.rewind_tick_age(30.0)

    async def option_quote(contract):
        return SimpleNamespace(bid=0.60, ask=0.66, last=0.62)

    state = _mirror_state(ibkr=SimpleNamespace(
        open_option_quote_stream=_stream_opener(handle),
        get_option_quote=option_quote,
    ))
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["ok"] is True
    p = (await _live_orders())[0]
    # Priced off the LEGACY ask (0.66), never the stale streamed 0.55.
    assert p.limit_price == pytest.approx(marketable_entry_limit("SPY", 0.66))
    assert handle.close_count == 1


async def test_mirror_without_stream_support_unchanged(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """Providers without open_option_quote_stream (duck-typed test doubles,
    older providers) take the legacy path untouched."""
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)

    async def option_quote(contract):
        return SimpleNamespace(bid=0.50, ask=0.55, last=0.52)

    state = _mirror_state(ibkr=SimpleNamespace(get_option_quote=option_quote))
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["ok"] is True
    p = (await _live_orders())[0]
    assert p.limit_price == pytest.approx(0.65)


# ===========================================================================
# D. Mock provider integration (the mock counterpart contract)
# ===========================================================================


async def test_mock_provider_opens_seeded_fresh_stream() -> None:
    from fef_sidecar.ibkr.models import Contract, ContractKind

    provider = MockIBKRProvider()
    c = Contract(symbol="SPX", kind=ContractKind.OPTION, exchange="CBOE",
                 expiry=_TODAY, strike=7500.0, right="C")
    s = await provider.open_option_quote_stream(c)
    assert isinstance(s, MockOptionQuoteStream)
    assert s.fresh_ask() is not None and s.fresh_ask() > 0
    assert provider.opened_streams == [s]
    s.close()
    assert s.close_count == 1
    s.close()
    assert s.close_count == 1
