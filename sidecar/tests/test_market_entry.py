"""Static-MKT entry revert (2026-07-22 operator decision).

"just go back to market orders on SPX. order hangup is just what will come
with it." — the marketable-limit -> bounded-chase -> streaming chain never
solved the momentum-fire fill problem (2-of-8 across three sessions), so
auto-trade entries revert to MARKET orders, with the hang risk BOUNDED by
the 45s entry-timeout watchdog (which did not exist for the original 7/16
hang).

Contract under test:
  - UIConfig.spx_entry_order_type: Literal["market","chase_limit"],
    default "market"; hot-read at fire time.
  - "market": the auto-trader stamps use_market_entry=True on the spec ->
    place_bracket's EXISTING static-MKT path (did_chase False): chase +
    streaming fully dormant (no subscription ever opened), _mk_market
    places the parent, and the entry-timeout watchdog arms with the FULL
    spec budget and limit_price=None. Gate 9.5 still runs (fresh-quote
    block preserved; the marketable limit becomes informational).
  - NEVER a naked MKT from the auto path: timeout disabled
    (FEF_ENTRY_TIMEOUT_SEC<=0) -> the auto-trader/mirror refuse static-MKT
    and keep chase_limit behavior (same invariant as the chase fallback).
  - "chase_limit": the prior chase/streaming behavior, byte-identical
    (see test_entry_pricing / test_entry_chase / test_streaming_quotes).
  - A hung MKT (never fills) is cancelled by the timeout: zero-fill
    stand-down (parent CANCELLED, budget released) — the 7/16 shape, now
    bounded end-to-end.
  - The SPY mirror follows the same config: MKT under "market" (penny grid
    fills trivially), LMT under "chase_limit", downgrade guard identical.
"""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from pydantic import ValidationError
from sqlmodel import select

from fef_sidecar.config_store import UIConfig, merge_partial
from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import AuditLog, LiveOrder
from fef_sidecar.db.repository import SignalIterRepository
from fef_sidecar.engine import entry_timeout as et
from fef_sidecar.engine import spy_mirror as sm
from fef_sidecar.engine.auto_trader import maybe_auto_trade
from fef_sidecar.engine.live_trading import BracketSpec, place_bracket
from fef_sidecar.engine.spy_mirror import maybe_place_spy_mirror
from fef_sidecar.ibkr.live import LiveIBKRProvider

_TERMINAL = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


# ---------------------------------------------------------------------------
# Config field
# ---------------------------------------------------------------------------

def test_config_default_is_market() -> None:
    assert UIConfig().spx_entry_order_type == "market"


def test_config_accepts_chase_limit() -> None:
    assert UIConfig(spx_entry_order_type="chase_limit").spx_entry_order_type == "chase_limit"


def test_config_rejects_unknown_entry_type() -> None:
    with pytest.raises(ValidationError):
        UIConfig(spx_entry_order_type="limit")
    with pytest.raises(ValidationError):
        UIConfig(spx_entry_order_type="MKT")


def test_config_merge_partial_roundtrip() -> None:
    cfg = merge_partial(UIConfig(), {"spx_entry_order_type": "chase_limit"})
    assert cfg.spx_entry_order_type == "chase_limit"
    # Untouched updates keep the field.
    cfg2 = merge_partial(cfg, {"default_qty": 5})
    assert cfg2.spx_entry_order_type == "chase_limit"


# ---------------------------------------------------------------------------
# Shared fixtures (auto-trader wiring — same harness as test_entry_pricing)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "fef_sidecar.engine.live_trading.is_live_trading_enabled", lambda: True
    )
    monkeypatch.delenv("FEF_DISABLE_CONFIDENCE_FLOOR", raising=False)
    monkeypatch.delenv("FEF_ENTRY_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("FEF_LIVE_TRADING", raising=False)
    monkeypatch.delenv("FEF_SPY_MIRROR_ENABLED", raising=False)
    # SPX-entry tests isolate the SPX leg (the mirror has its own section).
    monkeypatch.setenv("FEF_SPY_MIRROR_ENABLED", "false")


def _patch_config(monkeypatch: pytest.MonkeyPatch, **kw) -> UIConfig:
    import fef_sidecar.config_store as cs
    cfg = UIConfig(auto_trade_confidence_floor="LOW",
                   cross_symbol_cap_enabled=False, **kw)
    monkeypatch.setattr(cs, "load_config", lambda: cfg)
    monkeypatch.setattr(cs, "config_file_status", lambda: "ok")
    return cfg


def _state(**kw) -> SimpleNamespace:
    d = dict(
        auto_trade_enabled=True,
        active_symbol="SPX",
        auto_trade_trigger_types=["confluence", "aligned_only"],
        auto_trade_cool_down_sec=300,
        auto_trade_last_at_ms_by_symbol={},
        auto_trade_count_by_symbol={},
        auto_trade_max_per_symbol=2,
        auto_trade_qty_override=0,
        auto_trade_last_at_ms=0,
        auto_trade_count_session=0,
        last_spx_price=7515.0,
        exit_strategy_long="spx_points",
        ibkr=SimpleNamespace(),
    )
    d.update(kw)
    return SimpleNamespace(**d)


def _verdict(ask: float = 5.50) -> dict:
    now = int(time.time() * 1000)
    return {
        "direction": "LONG_CALLS",
        "confidence": "HIGH",
        "reasoning": "ALIGNED LONG_CALLS — market-entry test",
        "aligned": True,
        "wall_collapse_trigger": False,
        "strike_rec": 7520.0,
        "t1": 7530.0, "t2": 7540.0, "t3": 7565.0, "stop": 7505.0,
        "option_pricing": {
            "contract_strike": 7520.0, "right": "C",
            "entry_limit": 5.50, "delta": 0.45,
            "underlying_price_at_quote": 7515.0,
            "t1": 10.0, "t2": 14.5, "t3": 25.75, "stop": 1.0,
            "bid": 5.30, "ask": ask,
            "quote_ts_ms": now - 2_000,
        },
    }


def _capture_place_bracket(monkeypatch: pytest.MonkeyPatch, captured: dict):
    async def fake_place_bracket(ibkr, spec, **kwargs):
        captured["spec"] = spec
        captured["kwargs"] = kwargs
        return {"ok": True, "dry_run": False, "parent_order_id": 1}
    monkeypatch.setattr(
        "fef_sidecar.engine.live_trading.place_bracket", fake_place_bracket
    )


# ---------------------------------------------------------------------------
# Auto-trader wiring — spx_entry_order_type -> BracketSpec.use_market_entry
# ---------------------------------------------------------------------------

async def test_fire_default_config_places_market_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEFAULT config ("market") -> scanner fire rides the static-MKT path:
    use_market_entry=True with the 45s watchdog budget on the spec, and the
    gate-9.5 marketable limit still resolved (informational)."""
    _patch_config(monkeypatch)  # spx_entry_order_type defaults to "market"
    captured: dict = {}
    _capture_place_bracket(monkeypatch, captured)
    decision = await maybe_auto_trade(_state(), _verdict(), source="auto_main")
    assert decision.fired, decision.reason
    spec = captured["spec"]
    assert spec.use_market_entry is True                # the revert
    assert spec.entry_timeout_sec == pytest.approx(45.0)  # watchdog budget
    assert spec.entry_limit == pytest.approx(5.70)      # informational only
    assert captured["kwargs"]["state"] is not None      # budget-release handle


async def test_fire_chase_limit_config_preserves_limit_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"chase_limit" keeps the prior behavior byte-identically: the spec is
    exactly what the limit era produced (use_market_entry=False), so
    place_bracket runs chase + streaming as before (their own suites pin
    that machinery unmodified)."""
    _patch_config(monkeypatch, spx_entry_order_type="chase_limit")
    captured: dict = {}
    _capture_place_bracket(monkeypatch, captured)
    decision = await maybe_auto_trade(_state(), _verdict(), source="auto_main")
    assert decision.fired, decision.reason
    spec = captured["spec"]
    assert spec.use_market_entry is False
    assert spec.entry_limit == pytest.approx(5.70)
    assert spec.entry_timeout_sec == pytest.approx(45.0)


async def test_market_downgrades_to_chase_limit_when_timeout_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto path NEVER builds a naked MKT: timeout disabled -> refuse
    static-MKT, keep chase_limit behavior (place_bracket's chase then
    further downgrades to chase-only — no MKT anywhere, 7/16-proof)."""
    _patch_config(monkeypatch)  # "market"
    monkeypatch.setenv("FEF_ENTRY_TIMEOUT_SEC", "0")
    captured: dict = {}
    _capture_place_bracket(monkeypatch, captured)
    decision = await maybe_auto_trade(_state(), _verdict(), source="auto_main")
    assert decision.fired, decision.reason
    spec = captured["spec"]
    assert spec.use_market_entry is False   # downgraded — never unwatched MKT
    assert spec.entry_timeout_sec == pytest.approx(0.0)


async def test_config_read_failure_fails_safe_to_chase_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt/unreadable config must not choose MKT: fail-safe is the
    limit path (a resting limit cannot hang)."""
    import fef_sidecar.config_store as cs

    def boom():
        raise RuntimeError("corrupt config")

    monkeypatch.setattr(cs, "load_config", boom)
    monkeypatch.setattr(cs, "config_file_status", lambda: "invalid")
    captured: dict = {}
    _capture_place_bracket(monkeypatch, captured)
    # Floor read also fails -> restrictive HIGH; verdict is HIGH so it fires.
    decision = await maybe_auto_trade(_state(), _verdict(), source="auto_main")
    assert decision.fired, decision.reason
    assert captured["spec"].use_market_entry is False


async def test_no_fresh_quote_still_blocks_under_market(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate 9.5 is retained under "market": no usable quote -> the fire is
    BLOCKED, never a blind MKT into an unquoted market."""
    from fef_sidecar.engine.auto_trader import BlockReason
    _patch_config(monkeypatch)  # "market"
    captured: dict = {}
    _capture_place_bracket(monkeypatch, captured)

    async def get_option_quote(contract):
        raise RuntimeError("IBKR error 354: not subscribed")

    state = _state(ibkr=SimpleNamespace(get_option_quote=get_option_quote))
    verdict = _verdict()
    verdict["option_pricing"]["ask"] = None
    verdict["option_pricing"]["quote_ts_ms"] = int(time.time() * 1000) - 30_000
    decision = await maybe_auto_trade(state, verdict, source="auto_main")
    assert not decision.fired
    assert decision.reason_code == BlockReason.NO_FRESH_QUOTE
    assert "spec" not in captured


# ---------------------------------------------------------------------------
# place_bracket static-MKT live path (the R2-N3 "non-reachable" path is now
# reachable — prove the watchdog covers it). Same fake-live harness as
# test_entry_chase.
# ---------------------------------------------------------------------------

class _FakeLiveIB:
    """Duck-typed ib_async.IB. fill_after_cancel=False => the order NEVER
    fills (a hung MKT); cancelOrder confirms terminal."""

    def __init__(self):
        self.placed: list = []   # (order, trade)
        self.cancels: list = []
        self._next = 900

    async def qualifyContractsAsync(self, *contracts):
        return list(contracts)

    def placeOrder(self, contract, order):
        self._next += 1
        tr = SimpleNamespace(
            order=SimpleNamespace(orderId=self._next, permId=self._next + 9000,
                                  action="BUY"),
            orderStatus=SimpleNamespace(status="Submitted", filled=0,
                                        avgFillPrice=0.0),
        )
        self.placed.append((order, tr))
        return tr

    def trades(self):
        return [tr for _o, tr in self.placed]

    def cancelOrder(self, order):
        self.cancels.append(order)
        for _o, tr in self.placed:
            if tr.order is order and tr.orderStatus.status not in _TERMINAL:
                tr.orderStatus.status = "Cancelled"

    def market_orders(self) -> list:
        return [o for o, _ in self.placed if getattr(o, "orderType", None) == "MKT"]


class _FakeLiveProvider(LiveIBKRProvider):
    """Passes place_bracket's isinstance gate without a TWS connection.
    Counts quote-stream subscriptions so the dormancy invariant is
    provable."""

    def __init__(self, ib):
        self._ib = ib
        self._account_id = "DU_TEST"
        self._account_class = "paper"
        self.stream_opens: list = []

    async def get_option_quote(self, contract):
        return SimpleNamespace(ask=6.0, bid=5.8, last=5.9)

    async def open_option_quote_stream(self, contract, ib_contract=None):
        self.stream_opens.append(contract)
        raise AssertionError(
            "quote stream must NOT be subscribed on the static-MKT path")


def _mkt_spec(entry_timeout_sec: float, exit_strategy: str = "spx_points") -> BracketSpec:
    return BracketSpec(
        symbol="SPX", strike=7520.0, right="C", expiry="20260722", qty=3,
        entry_limit=5.70, t1_limit=10.0, t2_limit=14.5, t3_limit=25.75,
        stop_limit=1.0, t1_qty=1, t2_qty=1, t3_qty=1,
        exit_strategy=exit_strategy,
        spx_t1=7530.0, spx_t2=7540.0, spx_t3=7565.0, spx_stop=7505.0,
        use_market_entry=True,
        entry_timeout_sec=entry_timeout_sec,
    )


def _wire_live(monkeypatch: pytest.MonkeyPatch, spawn_captured: list | None):
    monkeypatch.setattr("fef_sidecar.engine.live_trading.is_live_trading_enabled",
                        lambda: True)
    monkeypatch.setattr("fef_sidecar.engine.live_trading.require_paper_account",
                        lambda: False)
    # Chase master-switch deliberately left ON: the bypass must be driven by
    # use_market_entry, not by the env kill switch.
    monkeypatch.setenv("FEF_ENTRY_CHASE_ENABLED", "true")

    from fef_sidecar.engine import entry_chase

    async def _chase_must_not_run(**kwargs):
        raise AssertionError("chase must not run under static-MKT entry")

    monkeypatch.setattr(entry_chase, "place_entry_with_chase", _chase_must_not_run)
    if spawn_captured is not None:
        def _capture_spawn(**kwargs):
            spawn_captured.append(kwargs)
            return None
        monkeypatch.setattr(
            "fef_sidecar.engine.entry_timeout.spawn_entry_timeout_monitor",
            _capture_spawn)


async def test_static_market_entry_places_mkt_with_full_budget_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list = []
    _wire_live(monkeypatch, spawned)
    ib = _FakeLiveIB()
    provider = _FakeLiveProvider(ib)
    state = SimpleNamespace(auto_trade_count_by_symbol={"SPX": 1},
                            auto_trade_count_session=1)

    result = await place_bracket(provider, _mkt_spec(entry_timeout_sec=10.0),
                                 confirm=True, source="auto_scanner", state=state)
    assert result["ok"] is True
    assert result["entry_chase"]["outcome"] == "static_market"
    assert result["entry_chase"]["used_market"] is True
    assert result["entry_chase"]["reposts"] == 0
    # Exactly ONE order: the MKT parent. No chase re-posts, no cancels.
    assert len(ib.placed) == 1
    assert len(ib.market_orders()) == 1
    assert ib.cancels == []
    # Chase/streaming fully dormant: no subscription was ever opened.
    assert provider.stream_opens == []
    # Watchdog armed ON the MKT with the FULL spec budget and no limit.
    mkt_trade = ib.placed[0][1]
    assert len(spawned) == 1
    spawn = spawned[0]
    assert spawn["timeout_sec"] == pytest.approx(10.0)  # full budget, no chase spend
    assert spawn["limit_price"] is None                 # MKT carries no limit
    assert spawn["ib_order_id"] == mkt_trade.order.orderId
    assert spawn["qty"] == 3
    # DB parent row records the MKT entry (limit kept as informational).
    async with get_session() as session:
        row = (await session.exec(
            select(LiveOrder).where(LiveOrder.id == result["parent_order_id"])
        )).first()
    assert row.order_type == "MKT"
    assert row.ib_order_id == mkt_trade.order.orderId


async def test_static_market_without_timeout_spawns_no_watchdog_manual_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documents the pre-existing MANUAL-path semantics place_bracket keeps:
    no timeout on the spec -> no watchdog (manual tickets are operator-
    supervised). The AUTO path can never build this spec — the downgrade
    guard (test_market_downgrades_to_chase_limit_when_timeout_disabled)
    refuses use_market_entry without a positive timeout."""
    spawned: list = []
    _wire_live(monkeypatch, spawned)
    ib = _FakeLiveIB()
    provider = _FakeLiveProvider(ib)
    result = await place_bracket(provider, _mkt_spec(entry_timeout_sec=0.0),
                                 confirm=True, source="manual_ticket", state=None)
    assert result["ok"] is True
    assert len(ib.market_orders()) == 1
    assert spawned == []  # documented manual behavior, unchanged


async def test_option_price_static_market_arms_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy option_price bracket honors use_market_entry too (parent
    MKT + attached children) and arms the watchdog via the non-chase
    derivation (full budget, limit None)."""
    spawned: list = []
    _wire_live(monkeypatch, spawned)
    ib = _FakeLiveIB()
    provider = _FakeLiveProvider(ib)
    state = SimpleNamespace(auto_trade_count_by_symbol={}, auto_trade_count_session=0)

    result = await place_bracket(
        provider, _mkt_spec(entry_timeout_sec=10.0, exit_strategy="option_price"),
        confirm=True, source="auto_scanner", state=state)
    assert result["ok"] is True
    # Parent + T1/T2/T3/STOP children all placed; parent is the MKT.
    assert len(ib.placed) == 5
    assert len(ib.market_orders()) == 1
    parent_trade = ib.placed[0][1]
    assert len(spawned) == 1
    spawn = spawned[0]
    assert spawn["timeout_sec"] == pytest.approx(10.0)
    assert spawn["limit_price"] is None
    assert spawn["ib_order_id"] == parent_trade.order.orderId
    assert provider.stream_opens == []


async def test_hung_market_entry_cancelled_by_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """END-TO-END 7/16 shape, now bounded: a static-MKT entry that NEVER
    fills is cancelled by the real entry-timeout monitor -> zero-fill
    stand-down (parent CANCELLED, session-cap slot released, audit row)."""
    _wire_live(monkeypatch, spawn_captured=None)  # REAL watchdog spawn
    ib = _FakeLiveIB()
    provider = _FakeLiveProvider(ib)
    state = SimpleNamespace(auto_trade_count_by_symbol={"SPX": 1},
                            auto_trade_count_session=1)

    before = set(et._BACKGROUND_TASKS)
    result = await place_bracket(provider, _mkt_spec(entry_timeout_sec=0.05),
                                 confirm=True, source="auto_scanner", state=state)
    assert result["ok"] is True
    assert len(ib.market_orders()) == 1
    new_tasks = set(et._BACKGROUND_TASKS) - before
    assert len(new_tasks) == 1  # the watchdog is live on the MKT
    outcome = await asyncio.wait_for(next(iter(new_tasks)), timeout=5.0)
    assert outcome["outcome"] == "cancelled_stand_down"
    # The hung MKT was cancelled at the broker...
    assert len(ib.cancels) == 1
    mkt_trade = ib.placed[0][1]
    assert mkt_trade.orderStatus.status == "Cancelled"
    # ...the parent row stood down, and the fire budget was released.
    async with get_session() as session:
        row = (await session.exec(
            select(LiveOrder).where(LiveOrder.id == result["parent_order_id"])
        )).first()
        audits = list((await session.exec(
            select(AuditLog).where(AuditLog.category == "entry_timeout")
        )).all())
    assert row.status == "CANCELLED"
    assert state.auto_trade_count_by_symbol["SPX"] == 0
    assert state.auto_trade_count_session == 0
    assert any(a.details.get("outcome") == "cancelled_stand_down" for a in audits)


# ---------------------------------------------------------------------------
# SPY mirror under the config (mirror builds its own spec — verify it
# follows spx_entry_order_type; DRY-RUN through the REAL place_bracket).
# ---------------------------------------------------------------------------

_TODAY = time.strftime("%Y%m%d")
_SPY_SPOT = 745.00


def _mirror_state(**kw) -> SimpleNamespace:
    async def option_quote(contract):
        assert contract.symbol == "SPY"
        return SimpleNamespace(bid=0.50, ask=0.55, last=0.52)

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
        ibkr=SimpleNamespace(get_option_quote=option_quote),
    )
    d.update(kw)
    state = SimpleNamespace(**d)
    now_ms = int(time.time() * 1000)
    state._watcher_stock_spot_cache = {"SPY": (now_ms, _SPY_SPOT)}
    state._watcher_stock_spot_refreshing = set()
    state._watcher_stock_spot_tasks = set()
    return state


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


def _patch_mirror_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FEF_SPY_MIRROR_ENABLED", raising=False)
    import fef_sidecar.engine.expiry_resolver as er

    async def fake_resolve(state, symbol):
        return _TODAY

    monkeypatch.setattr(er, "resolve_nearest_expiry", fake_resolve)
    monkeypatch.setattr(er, "_today_et_ymd", lambda: _TODAY)
    monkeypatch.setattr(sm, "_now_et_hhmm_ymd", lambda: ("10:00", _TODAY))


async def _mirror_parent_row() -> LiveOrder:
    async with get_session() as session:
        rows = list((await session.exec(
            select(LiveOrder).where(LiveOrder.symbol == "SPY")
            .where(LiveOrder.bracket_role == "parent")
        )).all())
    assert len(rows) == 1
    return rows[0]


async def _mirror_placed_summary() -> str:
    rows = await SignalIterRepository.query_audit_log(category="spy_mirror")
    placed = [x for x in rows if "SPY MIRROR PLACED" in x.summary]
    assert len(placed) == 1
    return placed[0].summary


async def test_mirror_goes_mkt_under_market_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_mirror_env(monkeypatch)
    _patch_config(monkeypatch)  # default "market"
    r = await maybe_place_spy_mirror(_mirror_state(), **_mirror_kwargs())
    assert r["ok"] is True
    row = await _mirror_parent_row()
    assert row.order_type == "MKT"
    assert row.limit_price == pytest.approx(0.65)  # informational marketable ref
    summary = await _mirror_placed_summary()
    assert " MKT " in summary
    assert "LMT $" not in summary


async def test_mirror_keeps_lmt_under_chase_limit_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_mirror_env(monkeypatch)
    _patch_config(monkeypatch, spx_entry_order_type="chase_limit")
    r = await maybe_place_spy_mirror(_mirror_state(), **_mirror_kwargs())
    assert r["ok"] is True
    row = await _mirror_parent_row()
    assert row.order_type == "LMT"
    assert row.limit_price == pytest.approx(0.65)
    summary = await _mirror_placed_summary()
    assert "LMT $0.65" in summary


async def test_mirror_downgrades_without_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same no-naked-MKT invariant on the mirror leg: timeout disabled ->
    the mirror refuses MKT and stays on the limit path."""
    _patch_mirror_env(monkeypatch)
    _patch_config(monkeypatch)  # "market"
    r = await maybe_place_spy_mirror(
        _mirror_state(), **_mirror_kwargs(entry_timeout_sec=0.0))
    assert r["ok"] is True
    row = await _mirror_parent_row()
    assert row.order_type == "LMT"
