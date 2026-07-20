"""Bounded-chase entry placement (2026-07-20 fill-rate fix).

Contract under test (see engine/entry_chase.py):
  - Post the marketable LMT; if unfilled after CHASE_INTERVAL_SEC, cancel and
    re-post at the CURRENT (re-fetched) ask + buffer, up to MAX_REPOSTS times;
    then ONE MarketOrder as the FINAL fallback (disable-able).
  - NEVER OVERSELL: a new order is placed ONLY after the previous is confirmed
    terminal with a KNOWN zero fill. ANY fill (partial/full, incl. in the async
    cancel window) STOPS the chase; an unconfirmed cancel STOPS the chase.
  - NO-FRESH-QUOTE: a re-fetch that yields no usable ask stops the chase and
    does NOT blindly MKT — the resting price-bounded limit is left for the 45s
    timeout.
  - The overall 45s ceiling bounds everything via `deadline_monotonic`.

The chase helper is exercised directly against a scripted fake IB (the same
approach test_entry_timeout uses) — place_bracket's live block is only reached
under a real LiveIBKRProvider, so the invariant-critical logic is unit-tested
here where fills/cancels can be driven deterministically.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from sqlmodel import select

from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import LiveOrder
from fef_sidecar.engine import entry_chase
from fef_sidecar.engine.entry_chase import place_entry_with_chase
from fef_sidecar.engine.live_trading import BracketSpec, place_bracket
from fef_sidecar.ibkr.live import LiveIBKRProvider

_TERMINAL = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


def _mk_limit(q: int, px: float):
    return SimpleNamespace(kind="LMT", qty=q, price=px)


def _mk_market(q: int):
    return SimpleNamespace(kind="MKT", qty=q, price=None)


class ScriptedIB:
    """Duck-typed ib_async.IB. placeOrder returns a mutable Trade; cancelOrder
    drives it terminal. Fill/cancel behaviour is scripted by PLACEMENT INDEX
    (deterministic — no wall-clock dependence).

    `enforce_single_live` asserts, on every placement after the first, that the
    previous order is already terminal — a hard OVERSELL tripwire (two live
    BUYs for the same qty can never coexist)."""

    def __init__(self, qty: int, *, fill_on_index: int | None = None,
                 on_place=None, on_cancel=None, enforce_single_live: bool = True):
        self.qty = qty
        self.placed: list = []
        self.cancels: list = []
        self._next_id = 1000
        self.fill_on_index = fill_on_index
        self.on_place = on_place
        self.on_cancel = on_cancel
        self.enforce_single_live = enforce_single_live

    def placeOrder(self, contract, order):
        idx = len(self.placed)
        if self.enforce_single_live and idx > 0:
            prev = self.placed[idx - 1]
            assert prev.orderStatus.status in _TERMINAL, (
                f"OVERSELL: placed order #{idx} while #{idx - 1} still "
                f"{prev.orderStatus.status!r} (two live BUYs)"
            )
        tid = self._next_id
        self._next_id += 1
        trade = SimpleNamespace(
            order=SimpleNamespace(orderId=tid, permId=tid + 50000, action="BUY",
                                  _spec=order),
            orderStatus=SimpleNamespace(status="Submitted", filled=0, avgFillPrice=0.0),
        )
        self.placed.append(trade)
        if self.fill_on_index is not None and idx == self.fill_on_index:
            trade.orderStatus.filled = self.qty
            trade.orderStatus.status = "Filled"
            trade.orderStatus.avgFillPrice = getattr(order, "price", 0.0) or 0.0
        if self.on_place is not None:
            self.on_place(trade, idx)
        return trade

    def cancelOrder(self, order):
        self.cancels.append(order)
        trade = next((t for t in self.placed if t.order is order), None)
        if trade is None:
            return
        if self.on_cancel is not None:
            self.on_cancel(trade)
        elif trade.orderStatus.status not in _TERMINAL:
            trade.orderStatus.status = "Cancelled"  # default: clean confirmed cancel


def _fast(**over) -> dict:
    d = dict(interval_sec=0.02, poll_sec=0.005, cancel_wait_sec=0.05,
             max_reposts=3, mkt_fallback=True)
    d.update(over)
    return d


async def _run(ib, *, qty: int = 3, initial_limit: float = 5.70, refetch=None, **over):
    async def _default_refetch():
        return None
    return await place_entry_with_chase(
        ib=ib, ib_contract=SimpleNamespace(sym="SPX"), symbol="SPX", qty=qty,
        initial_limit=initial_limit, make_limit_order=_mk_limit,
        make_market_order=_mk_market, refetch_limit=(refetch or _default_refetch),
        **_fast(**over),
    )


def _kinds(ib) -> list[str]:
    return [t.order._spec.kind for t in ib.placed]


# ---------------------------------------------------------------------------
# 1. Fills on the first post — no chase needed.
# ---------------------------------------------------------------------------

async def test_fills_on_first_post_no_chase() -> None:
    ib = ScriptedIB(qty=3, fill_on_index=0)
    res = await _run(ib)
    assert res.outcome == "filled"
    assert res.reposts == 0
    assert res.used_market is False
    assert res.limit_price == pytest.approx(5.70)
    assert len(ib.placed) == 1
    assert ib.cancels == []
    assert res.trade is ib.placed[0]


# ---------------------------------------------------------------------------
# 2. Fills on a later chase re-post — the moving ask, caught by a re-post.
# ---------------------------------------------------------------------------

async def test_fills_on_later_repost() -> None:
    ib = ScriptedIB(qty=3, fill_on_index=2)  # the 2nd re-post fills at placement
    asks = iter([6.00, 6.40, 6.80, 7.20])

    async def refetch():
        return next(asks)  # strictly climbing -> each re-post chases up

    res = await _run(ib, refetch=refetch)
    assert res.outcome == "filled"
    assert res.reposts == 2
    assert len(ib.placed) == 3   # initial + 2 re-posts
    assert len(ib.cancels) == 2  # the 2 superseded unfilled orders
    assert res.trade is ib.placed[2]
    assert res.used_market is False
    assert res.limit_price == pytest.approx(6.40)  # limit of the filling re-post


# ---------------------------------------------------------------------------
# 3. No fill after N re-posts -> MKT fallback fires.
# ---------------------------------------------------------------------------

async def test_mkt_fallback_after_reposts() -> None:
    ib = ScriptedIB(qty=3, fill_on_index=None)  # never fills
    asks = iter([6.0, 6.4, 6.8, 7.2, 7.6, 8.0])

    async def refetch():
        return next(asks)

    res = await _run(ib, refetch=refetch, max_reposts=3, mkt_fallback=True)
    assert res.outcome == "mkt_fallback"
    assert res.used_market is True
    assert res.limit_price is None
    assert res.reposts == 3
    assert _kinds(ib) == ["LMT", "LMT", "LMT", "LMT", "MKT"]  # 4 limits + 1 MKT
    assert len(ib.cancels) == 4   # 3 superseded re-posts + the pre-MKT cancel
    assert res.trade is ib.placed[-1]


# ---------------------------------------------------------------------------
# 4. MKT-fallback disabled -> chase-only, no naked MKT.
# ---------------------------------------------------------------------------

async def test_mkt_fallback_disabled_chase_only() -> None:
    ib = ScriptedIB(qty=3, fill_on_index=None)
    asks = iter([6.0, 6.4, 6.8, 7.2])

    async def refetch():
        return next(asks)

    res = await _run(ib, refetch=refetch, max_reposts=3, mkt_fallback=False)
    assert res.outcome == "chase_exhausted_rest"
    assert res.used_market is False
    assert _kinds(ib) == ["LMT", "LMT", "LMT", "LMT"]  # never a MARKET order
    assert len(ib.placed) == 4          # initial + 3 re-posts
    assert res.reposts == 3
    assert len(ib.cancels) == 3         # last resting limit NOT cancelled here
    assert res.trade is ib.placed[-1]   # 45s timeout owns the resting limit


# ---------------------------------------------------------------------------
# 5. The overall 45s ceiling (deadline) bounds everything.
# ---------------------------------------------------------------------------

async def test_deadline_bounds_chase() -> None:
    ib = ScriptedIB(qty=3, fill_on_index=None)
    val = [6.0]

    async def refetch():
        val[0] += 0.10  # ever-climbing -> would chase forever if unbounded
        return val[0]

    start = time.monotonic()
    res = await place_entry_with_chase(
        ib=ib, ib_contract=SimpleNamespace(), symbol="SPX", qty=3,
        initial_limit=5.70, make_limit_order=_mk_limit,
        make_market_order=_mk_market, refetch_limit=refetch,
        interval_sec=0.02, poll_sec=0.005, cancel_wait_sec=0.02,
        max_reposts=10_000, mkt_fallback=True,
        deadline_monotonic=start + 0.1,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 1.0              # bounded — did NOT run 10k re-posts
    assert res.outcome == "deadline"
    assert res.used_market is False   # deadline stops BEFORE any naked MKT


async def test_final_poll_deadline_skips_mkt() -> None:
    # BLOCKER-1(b) regression guard: max_reposts=0 means the loop never runs,
    # so an expired deadline can ONLY surface in the FINAL poll — which must NOT
    # fall through to a MKT placed past the ceiling (a market order that could
    # rest with a ~0 monitor budget = the 7/16 hang). Pre-fix this placed a
    # naked MKT; post-fix it rests the limit for the timeout.
    ib = ScriptedIB(qty=3, fill_on_index=None)

    async def refetch():
        return 999.0  # irrelevant — no loop iterations at max_reposts=0

    start = time.monotonic()
    res = await place_entry_with_chase(
        ib=ib, ib_contract=SimpleNamespace(), symbol="SPX", qty=3,
        initial_limit=5.70, make_limit_order=_mk_limit,
        make_market_order=_mk_market, refetch_limit=refetch,
        interval_sec=0.05, poll_sec=0.005, cancel_wait_sec=0.02,
        max_reposts=0, mkt_fallback=True,
        deadline_monotonic=start + 0.02,
    )
    assert res.outcome == "deadline"
    assert res.used_market is False
    assert _kinds(ib) == ["LMT"]   # only the initial limit — NO naked MKT
    assert ib.cancels == []


# ---------------------------------------------------------------------------
# 6. Partial-fill handling — never oversell (several async-race shapes).
# ---------------------------------------------------------------------------

async def test_partial_while_working_stops_no_oversell() -> None:
    def on_place(trade, idx):
        if idx == 0:
            trade.orderStatus.filled = 1  # 1 of 3 filled, remainder resting
            trade.orderStatus.avgFillPrice = 5.68

    ib = ScriptedIB(qty=3, on_place=on_place)

    async def refetch():
        raise AssertionError("must not chase a partially-filled order")

    res = await _run(ib, refetch=refetch)
    assert res.outcome == "partial_stop"
    assert res.used_market is False
    assert len(ib.placed) == 1   # never placed a 2nd order -> no oversell
    assert ib.cancels == []      # left working for the 45s timeout
    assert res.trade.orderStatus.filled == 1


async def test_fill_completes_during_cancel_window() -> None:
    def on_cancel(trade):
        trade.orderStatus.filled = 3  # 'too late to cancel' — completes instead
        trade.orderStatus.status = "Filled"

    ib = ScriptedIB(qty=3, on_cancel=on_cancel)
    asks = iter([6.0, 6.4, 6.8])

    async def refetch():
        return next(asks)

    res = await _run(ib, refetch=refetch)
    assert res.outcome == "filled_during_cancel"
    assert len(ib.placed) == 1   # never reposted after the fill -> no oversell
    assert len(ib.cancels) == 1
    assert res.trade is ib.placed[0]


async def test_partial_lands_during_cancel_window() -> None:
    def on_cancel(trade):
        trade.orderStatus.filled = 1  # remainder cancelled, 1 lot filled
        trade.orderStatus.status = "Cancelled"

    ib = ScriptedIB(qty=3, on_cancel=on_cancel)

    async def refetch():
        return 6.20

    res = await _run(ib, refetch=refetch)
    assert res.outcome == "partial_during_cancel"
    assert len(ib.placed) == 1   # no re-post after a partial -> no oversell
    assert res.trade.orderStatus.filled == 1


# ---------------------------------------------------------------------------
# 7. NO-FRESH-QUOTE still blocks — no stale chase, no blind MKT.
# ---------------------------------------------------------------------------

async def test_no_fresh_quote_stops_no_mkt() -> None:
    ib = ScriptedIB(qty=3, fill_on_index=None)

    async def refetch():
        return None  # no usable/fresh ask

    res = await _run(ib, refetch=refetch, mkt_fallback=True)  # MKT enabled, still no MKT
    assert res.outcome == "no_fresh_quote"
    assert res.used_market is False   # did NOT blindly MKT off a stale quote
    assert len(ib.placed) == 1        # only the initial price-bounded limit
    assert ib.cancels == []           # left resting for the 45s timeout


# ---------------------------------------------------------------------------
# 8. Cancel unconfirmed -> stop (a 2nd live BUY would be oversell).
# ---------------------------------------------------------------------------

async def test_cancel_unconfirmed_stops_no_oversell() -> None:
    def on_cancel(trade):
        pass  # cancel never confirms — status stays 'Submitted'

    ib = ScriptedIB(qty=3, on_cancel=on_cancel)

    async def refetch():
        return 6.20

    res = await _run(ib, refetch=refetch, cancel_wait_sec=0.03)
    assert res.outcome == "cancel_unconfirmed"
    assert len(ib.placed) == 1   # never placed beside an un-cancelled order


async def test_final_mkt_skipped_if_cancel_unconfirmed() -> None:
    def on_cancel(trade):
        pass  # the pre-MKT cancel won't confirm

    ib = ScriptedIB(qty=3, on_cancel=on_cancel)

    async def refetch():
        return 6.20

    res = await _run(ib, refetch=refetch, max_reposts=0, mkt_fallback=True)
    assert res.outcome == "cancel_unconfirmed"
    assert res.used_market is False   # did NOT go naked-MKT beside a live limit
    assert len(ib.placed) == 1


# ---------------------------------------------------------------------------
# 9. External cancel (kill-switch / TWS) mid-chase -> stop, do not re-post.
# ---------------------------------------------------------------------------

async def test_external_cancel_stops() -> None:
    def on_place(trade, idx):
        if idx == 0:
            trade.orderStatus.status = "Cancelled"  # cancelled by something else

    ib = ScriptedIB(qty=3, on_place=on_place)

    async def refetch():
        raise AssertionError("must not chase after an external cancel")

    res = await _run(ib, refetch=refetch)
    assert res.outcome == "external_cancel"
    assert len(ib.placed) == 1   # no fresh BUY against a deliberate cancel
    assert ib.cancels == []


# ---------------------------------------------------------------------------
# 10. Ask not climbing -> no pointless cancel/re-post.
# ---------------------------------------------------------------------------

async def test_ask_not_climbing_no_pointless_repost() -> None:
    ib = ScriptedIB(qty=3, fill_on_index=None)

    async def refetch():
        return 5.60  # never above the resting 5.70 -> never a climb

    res = await _run(ib, refetch=refetch, max_reposts=3, mkt_fallback=True)
    assert res.reposts == 0            # never cancel/re-post when ask isn't climbing
    assert res.outcome == "mkt_fallback"
    assert _kinds(ib) == ["LMT", "MKT"]  # initial limit + final MKT only
    assert len(ib.cancels) == 1        # just the pre-MKT cancel


# ---------------------------------------------------------------------------
# 11. Knobs are env-tunable (constants overridable without a code change).
# ---------------------------------------------------------------------------

def test_knobs_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("FEF_ENTRY_CHASE_ENABLED", "FEF_ENTRY_CHASE_INTERVAL_SEC",
              "FEF_ENTRY_CHASE_MAX_REPOSTS", "FEF_ENTRY_CHASE_MKT_FALLBACK",
              "FEF_ENTRY_CHASE_CANCEL_WAIT_SEC", "FEF_ENTRY_CHASE_REFETCH_TIMEOUT_SEC",
              "FEF_ENTRY_CHASE_HARD_BUDGET_SEC", "FEF_ENTRY_CHASE_MONITOR_FLOOR_SEC"):
        monkeypatch.delenv(k, raising=False)
    # Defaults
    assert entry_chase.chase_enabled() is True
    assert entry_chase.chase_interval_sec() == pytest.approx(0.7)
    assert entry_chase.chase_max_reposts() == 3
    assert entry_chase.chase_mkt_fallback() is True
    assert entry_chase.chase_cancel_wait_sec() == pytest.approx(1.0)
    assert entry_chase.chase_refetch_timeout_sec() == pytest.approx(1.5)
    assert entry_chase.chase_hard_budget_sec() == pytest.approx(20.0)
    assert entry_chase.chase_monitor_floor_sec() == pytest.approx(10.0)
    # Overrides
    monkeypatch.setenv("FEF_ENTRY_CHASE_ENABLED", "false")
    monkeypatch.setenv("FEF_ENTRY_CHASE_INTERVAL_SEC", "0.9")
    monkeypatch.setenv("FEF_ENTRY_CHASE_MAX_REPOSTS", "5")
    monkeypatch.setenv("FEF_ENTRY_CHASE_MKT_FALLBACK", "0")
    assert entry_chase.chase_enabled() is False
    assert entry_chase.chase_interval_sec() == pytest.approx(0.9)
    assert entry_chase.chase_max_reposts() == 5
    assert entry_chase.chase_mkt_fallback() is False
    # Garbage falls back to the default (never crashes the fire path)
    monkeypatch.setenv("FEF_ENTRY_CHASE_MAX_REPOSTS", "garbage")
    assert entry_chase.chase_max_reposts() == 3


# ---------------------------------------------------------------------------
# 12-13. place_bracket wiring (MAJOR-2): the chase MKT fallback is ALWAYS armed
#        with a live positive-budget entry-timeout watchdog, and a DISABLED
#        timeout downgrades to chase-only (no unmonitorable naked MKT). Drives
#        place_bracket's live block via a fake LiveIBKRProvider — the arming
#        lives there, not in the helper, and was previously untested.
# ---------------------------------------------------------------------------

class _FakeLiveIB:
    """Duck-typed ib_async.IB for the live place_bracket path — orders never
    fill (forces chase exhaustion); cancelOrder confirms terminal."""

    def __init__(self):
        self.placed: list = []   # (order, trade)
        self.cancels: list = []
        self._next = 700

    async def qualifyContractsAsync(self, *contracts):
        return list(contracts)

    def placeOrder(self, contract, order):
        self._next += 1
        tr = SimpleNamespace(
            order=SimpleNamespace(orderId=self._next, permId=self._next + 9000,
                                  action="BUY"),
            orderStatus=SimpleNamespace(status="Submitted", filled=0, avgFillPrice=0.0),
        )
        self.placed.append((order, tr))
        return tr

    def cancelOrder(self, order):
        self.cancels.append(order)
        for _o, tr in self.placed:
            if tr.order is order and tr.orderStatus.status not in _TERMINAL:
                tr.orderStatus.status = "Cancelled"

    def market_orders(self) -> list:
        return [o for o, _ in self.placed if getattr(o, "orderType", None) == "MKT"]


class _FakeLiveProvider(LiveIBKRProvider):
    """Passes place_bracket's isinstance(LiveIBKRProvider) gate without a TWS
    connection; serves a climbing ask so the chase re-posts."""

    def __init__(self, ib, quote_asks):
        self._ib = ib
        self._account_id = "DU_TEST"
        self._account_class = "paper"
        self._asks = iter(quote_asks)

    async def get_option_quote(self, contract):
        try:
            a = next(self._asks)
        except StopIteration:
            a = None
        return SimpleNamespace(ask=a, bid=(a - 0.1 if a else None), last=a)


@pytest.fixture
async def _db():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


def _bracket_spec(entry_timeout_sec: float) -> BracketSpec:
    return BracketSpec(
        symbol="SPX", strike=7520.0, right="P", expiry="20260720", qty=3,
        entry_limit=5.70, t1_limit=5.70, t2_limit=5.70, t3_limit=5.70,
        stop_limit=5.70, t1_qty=1, t2_qty=1, t3_qty=1,
        exit_strategy="spx_points",
        spx_t1=7510.0, spx_t2=7500.0, spx_t3=7480.0, spx_stop=7525.0,
        entry_timeout_sec=entry_timeout_sec,
    )


def _wire_live(monkeypatch: pytest.MonkeyPatch, captured: list) -> None:
    monkeypatch.setattr("fef_sidecar.engine.live_trading.is_live_trading_enabled",
                        lambda: True)
    monkeypatch.setattr("fef_sidecar.engine.live_trading.require_paper_account",
                        lambda: False)
    for k, v in dict(
        FEF_ENTRY_CHASE_ENABLED="true", FEF_ENTRY_CHASE_MKT_FALLBACK="true",
        FEF_ENTRY_CHASE_MAX_REPOSTS="1", FEF_ENTRY_CHASE_INTERVAL_SEC="0.02",
        FEF_ENTRY_CHASE_CANCEL_WAIT_SEC="0.05", FEF_ENTRY_CHASE_POLL_SEC="0.005",
        FEF_ENTRY_CHASE_REFETCH_TIMEOUT_SEC="0.5",
        FEF_ENTRY_CHASE_HARD_BUDGET_SEC="2.0",
        FEF_ENTRY_CHASE_MONITOR_FLOOR_SEC="1.0",
    ).items():
        monkeypatch.setenv(k, v)

    def _capture_spawn(**kwargs):
        captured.append(kwargs)
        return None
    monkeypatch.setattr(
        "fef_sidecar.engine.entry_timeout.spawn_entry_timeout_monitor",
        _capture_spawn)


async def test_place_bracket_mkt_fallback_is_armed_with_watchdog(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    captured: list = []
    _wire_live(monkeypatch, captured)
    ib = _FakeLiveIB()
    provider = _FakeLiveProvider(ib, quote_asks=[6.5, 7.0, 7.5])  # climbing -> reposts
    state = SimpleNamespace(auto_trade_count_by_symbol={}, auto_trade_count_session=0)

    result = await place_bracket(
        provider, _bracket_spec(entry_timeout_sec=10.0),
        confirm=True, source="auto_main", state=state,
    )
    assert result["ok"] is True
    assert result["entry_chase"]["outcome"] == "mkt_fallback"
    assert result["entry_chase"]["used_market"] is True
    # A real MarketOrder was placed as the fallback...
    assert len(ib.market_orders()) == 1
    mkt_trade = ib.placed[-1][1]
    # ...and the entry-timeout watchdog was armed ON THAT MKT with budget > 0.
    assert len(captured) == 1
    spawn = captured[0]
    assert spawn["timeout_sec"] > 0
    assert spawn["timeout_sec"] >= 1.0            # >= the reserved monitor floor
    assert spawn["limit_price"] is None           # MKT carries no limit
    assert spawn["ib_order_id"] == mkt_trade.order.orderId
    # DB row re-stamped to the final MKT order (MINOR-3).
    async with get_session() as session:
        row = (await session.exec(
            select(LiveOrder).where(LiveOrder.id == result["parent_order_id"])
        )).first()
    assert row.ib_order_id == mkt_trade.order.orderId
    assert row.order_type == "MKT"


async def test_place_bracket_timeout_disabled_downgrades_to_chase_only(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    captured: list = []
    _wire_live(monkeypatch, captured)
    ib = _FakeLiveIB()
    provider = _FakeLiveProvider(ib, quote_asks=[6.5, 7.0, 7.5])
    state = SimpleNamespace(auto_trade_count_by_symbol={}, auto_trade_count_session=0)

    result = await place_bracket(
        provider, _bracket_spec(entry_timeout_sec=0.0),  # timeout DISABLED
        confirm=True, source="auto_main", state=state,
    )
    assert result["ok"] is True
    # Chase-only: NO market order was ever placed (a naked MKT with no watchdog
    # can hang — 7/16). The last LIMIT rests; a limit cannot hang.
    assert ib.market_orders() == []
    assert result["entry_chase"]["used_market"] is False
    assert result["entry_chase"]["outcome"] == "chase_exhausted_rest"
    # No watchdog spawned (timeout disabled) — consistent with a resting limit.
    assert captured == []
