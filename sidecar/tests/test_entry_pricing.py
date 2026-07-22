"""Marketable-limit entry (2026-07-16 incident fix, part 1).

Contract under test:
  - Buffer: max($0.10, 2% of ask), capped at $0.50 (entry_buffer).
  - Limit = ask + buffer, rounded UP to the contract's min price increment
    (SPX: $0.05 below $3 premium, $0.10 at/above; penny-program symbols $0.01)
    — IBKR rejects off-tick limits, and rounding down would kill
    marketability. Tick round-up may exceed the $0.50 cap by <= one tick.
  - Freshness: fire-time quote (option_pricing.ask + quote_ts_ms) used when
    <=10s old; stale/unstamped -> exactly ONE re-fetch; still unusable ->
    the fire is BLOCKED (NO_FRESH_QUOTE) with counterfactual tagging like
    the other gates — NEVER a silent fall back to MKT.
  - Under spx_entry_order_type="chase_limit" the auto-trader passes the
    marketable limit into BracketSpec with use_market_entry=False and
    entry_timeout_sec=45 (see test_entry_timeout for the watchdog itself).
    Under the 2026-07-22 default "market" the SAME resolution runs (fresh-
    quote block preserved) but the limit is informational — that mode's
    wiring is covered in test_market_entry.py.
"""

from __future__ import annotations

import math
import os
import time
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from sqlmodel import select

from fef_sidecar.config_store import UIConfig
from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import AuditLog, SignalIter
from fef_sidecar.db.repository import SignalIterRepository
from fef_sidecar.engine.auto_trader import BlockReason, maybe_auto_trade
from fef_sidecar.engine.entry_pricing import (
    entry_buffer,
    entry_tick,
    marketable_entry_limit,
    resolve_entry_ask,
)


# ---------------------------------------------------------------------------
# Pure math — buffer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ask,expected", [
    (0.50, 0.10),   # 2% = $0.01 -> floor $0.10
    (1.00, 0.10),   # floor
    (5.00, 0.10),   # 2% = $0.10 == floor
    (6.00, 0.12),   # 2% region
    (10.00, 0.20),
    (25.00, 0.50),  # 2% = $0.50 == cap
    (30.00, 0.50),  # cap binds
    (100.00, 0.50),
])
def test_entry_buffer(ask: float, expected: float) -> None:
    assert entry_buffer(ask) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Pure math — tick grid
# ---------------------------------------------------------------------------

def test_entry_tick_spx_regimes() -> None:
    assert entry_tick("SPX", 2.95) == 0.05
    assert entry_tick("SPX", 2.999) == 0.05
    assert entry_tick("SPX", 3.00) == 0.10
    assert entry_tick("SPX", 8.10) == 0.10
    assert entry_tick("spx", 8.10) == 0.10  # case-insensitive


def test_entry_tick_penny_symbols() -> None:
    # SPY/QQQ/IWM quote in pennies at ALL prices.
    for sym in ("SPY", "QQQ", "IWM"):
        assert entry_tick(sym, 2.50) == 0.01
        assert entry_tick(sym, 8.00) == 0.01
    # Other penny-program names (GLD/SMH/TLT) are $0.01 only below $3.00 —
    # $0.05 at/above (an off-tier limit is rejected by IBKR outright).
    for sym in ("GLD", "SMH", "TLT"):
        assert entry_tick(sym, 2.50) == 0.01
        assert entry_tick(sym, 3.00) == 0.05
        assert entry_tick(sym, 8.00) == 0.05


# ---------------------------------------------------------------------------
# Pure math — marketable limit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol,ask,expected", [
    # SPX >= $3 regime (tick 0.10)
    ("SPX", 5.50, 5.70),    # 5.50 + 0.11 = 5.61 -> up to 5.70
    ("SPX", 5.00, 5.10),    # 5.00 + 0.10 = 5.10 already on-tick (no extra bump)
    ("SPX", 6.40, 6.60),    # 6.40 + 0.128 = 6.528 -> 6.60
    ("SPX", 30.00, 30.50),  # cap: 30.00 + 0.50, on-tick
    # SPX < $3 regime (tick 0.05)
    ("SPX", 0.50, 0.60),    # 0.50 + 0.10, on 0.05 grid
    ("SPX", 1.25, 1.35),
    # Regime boundary crossed by the buffer: limit priced on the >=3 grid
    ("SPX", 2.95, 3.10),    # 2.95 + 0.10 = 3.05 -> >=3 tick 0.10 -> 3.10
    # Penny symbols
    ("SPY", 2.00, 2.10),
    ("QQQ", 6.25, 6.38),    # 6.25 + 0.125 = 6.375 -> 6.38 (penny at all prices)
    ("GLD", 6.25, 6.40),    # same raw 6.375 but GLD is $0.05 at/above $3
    ("GLD", 2.00, 2.10),    # below $3: penny grid
])
def test_marketable_entry_limit(symbol: str, ask: float, expected: float) -> None:
    assert marketable_entry_limit(symbol, ask) == pytest.approx(expected)


def test_marketable_limit_invariants() -> None:
    """Limit always > ask; overshoot never exceeds cap + one tick; on-grid."""
    for ask in [0.05, 0.35, 1.10, 2.90, 3.00, 4.85, 7.77, 12.34, 28.00, 55.5]:
        lim = marketable_entry_limit("SPX", ask)
        tick = entry_tick("SPX", lim)
        assert lim > ask
        assert lim - ask <= 0.50 + tick + 1e-9
        # on the grid at the limit's own regime
        assert abs((lim / tick) - round(lim / tick)) < 1e-6


# ---------------------------------------------------------------------------
# resolve_entry_ask — freshness / one-shot re-fetch / hard block
# ---------------------------------------------------------------------------

def _quote(ask: float | None):
    return SimpleNamespace(ask=ask, bid=(ask - 0.1) if ask else None, last=ask)


def _fake_ibkr(ask: float | None = None, raise_exc: Exception | None = None):
    calls: list[object] = []

    async def get_option_quote(contract):
        calls.append(contract)
        if raise_exc is not None:
            raise raise_exc
        return _quote(ask)

    return SimpleNamespace(get_option_quote=get_option_quote), calls


async def test_fresh_quote_used_without_refetch() -> None:
    ibkr, calls = _fake_ibkr(ask=9.99)
    state = SimpleNamespace(ibkr=ibkr)
    now = int(time.time() * 1000)
    res = await resolve_entry_ask(
        state, symbol="SPX", strike=7520.0, right="C", expiry="20260716",
        opt={"ask": 5.50, "quote_ts_ms": now - 5_000}, now_ms=now,
    )
    assert res.ask == 5.50
    assert res.limit_price == pytest.approx(5.70)
    assert res.source == "fire_quote"
    assert res.refetched is False
    assert calls == []  # no IBKR round-trip on a fresh quote


async def test_stale_quote_refetched_once() -> None:
    ibkr, calls = _fake_ibkr(ask=6.20)
    state = SimpleNamespace(ibkr=ibkr)
    now = int(time.time() * 1000)
    res = await resolve_entry_ask(
        state, symbol="SPX", strike=7520.0, right="C", expiry="20260716",
        opt={"ask": 5.50, "quote_ts_ms": now - 20_000}, now_ms=now,
    )
    assert len(calls) == 1  # exactly one re-fetch
    assert res.ask == 6.20
    assert res.source == "refetch"
    assert res.limit_price == pytest.approx(
        marketable_entry_limit("SPX", 6.20))


async def test_unstamped_quote_triggers_refetch() -> None:
    ibkr, calls = _fake_ibkr(ask=4.00)
    state = SimpleNamespace(ibkr=ibkr)
    res = await resolve_entry_ask(
        state, symbol="SPX", strike=7520.0, right="C", expiry="20260716",
        opt={"ask": 5.50},  # no quote_ts_ms (older payloads)
    )
    assert len(calls) == 1
    assert res.ask == 4.00


async def test_refetch_failure_blocks() -> None:
    ibkr, calls = _fake_ibkr(raise_exc=RuntimeError("IBKR error 354"))
    state = SimpleNamespace(ibkr=ibkr)
    now = int(time.time() * 1000)
    res = await resolve_entry_ask(
        state, symbol="SPX", strike=7520.0, right="C", expiry="20260716",
        opt={"ask": 5.50, "quote_ts_ms": now - 60_000}, now_ms=now,
    )
    assert len(calls) == 1
    assert res.ask is None and res.limit_price is None
    assert "354" in (res.error or "")


async def test_refetch_nan_ask_blocks() -> None:
    ibkr, _ = _fake_ibkr(ask=math.nan)
    state = SimpleNamespace(ibkr=ibkr)
    res = await resolve_entry_ask(
        state, symbol="SPX", strike=7520.0, right="C", expiry="20260716",
        opt={"ask": None, "quote_ts_ms": None},
    )
    assert res.limit_price is None


async def test_refetch_chase_bound_blocks_runaway_tape() -> None:
    """A stale quote re-fetched into a runaway ask must BLOCK, not chase —
    paying the re-fetched price is the incident fill without the hang
    (fire-quote 5.50 -> refetch 8.10 is the exact 7/16 shape)."""
    ibkr, calls = _fake_ibkr(ask=8.10)
    state = SimpleNamespace(ibkr=ibkr)
    now = int(time.time() * 1000)
    res = await resolve_entry_ask(
        state, symbol="SPX", strike=7520.0, right="C", expiry="20260716",
        opt={"ask": 5.50, "entry_limit": 5.50, "quote_ts_ms": now - 20_000},
        now_ms=now,
    )
    assert len(calls) == 1
    assert res.limit_price is None
    assert "chase bound" in (res.error or "")


async def test_refetch_within_chase_bound_allowed() -> None:
    ibkr, _ = _fake_ibkr(ask=6.20)  # 6.20 <= 5.50 * 1.15 = 6.325
    state = SimpleNamespace(ibkr=ibkr)
    now = int(time.time() * 1000)
    res = await resolve_entry_ask(
        state, symbol="SPX", strike=7520.0, right="C", expiry="20260716",
        opt={"ask": 5.50, "entry_limit": 5.50, "quote_ts_ms": now - 20_000},
        now_ms=now,
    )
    assert res.ask == 6.20
    assert res.source == "refetch"


# ---------------------------------------------------------------------------
# Auto-trader integration — gate 9.5 wiring
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.fixture(autouse=True)
def _live(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "fef_sidecar.engine.live_trading.is_live_trading_enabled", lambda: True
    )
    monkeypatch.delenv("FEF_DISABLE_CONFIDENCE_FLOOR", raising=False)
    monkeypatch.delenv("FEF_ENTRY_TIMEOUT_SEC", raising=False)
    # These tests isolate the SPX ENTRY leg. Since the 2026-07-20 non-blocking
    # spot fix, a committed SPX fire's SPY mirror reliably fires too (it now
    # DERIVES its spot off the SPX underlying instead of skipping on a missing
    # live SPY quote) and would otherwise share this file's mocked place_bracket
    # / get_option_quote, polluting the SPX-only assertions. The mirror has its
    # own suite (test_spy_mirror.py) — disable it here.
    monkeypatch.setenv("FEF_SPY_MIRROR_ENABLED", "false")
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(cs, "load_config", lambda: UIConfig(
        auto_trade_confidence_floor="LOW", cross_symbol_cap_enabled=False,
    ))
    monkeypatch.setattr(cs, "config_file_status", lambda: "ok")


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


def _verdict(ask: float | None = 5.50, quote_ts_ms: int | None = None) -> dict:
    now = int(time.time() * 1000)
    return {
        "direction": "LONG_CALLS",
        "confidence": "HIGH",
        "reasoning": "ALIGNED LONG_CALLS — entry-pricing test",
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
            "quote_ts_ms": now - 2_000 if quote_ts_ms is None else quote_ts_ms,
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


async def test_fire_uses_marketable_limit_not_market(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spx_entry_order_type='chase_limit' preserves the limit-era wiring
    byte-identically (assertions unchanged from before the 2026-07-22
    'market' revert — the new default's wiring lives in
    test_market_entry.py)."""
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(cs, "load_config", lambda: UIConfig(
        auto_trade_confidence_floor="LOW", cross_symbol_cap_enabled=False,
        spx_entry_order_type="chase_limit",
    ))
    captured: dict = {}
    _capture_place_bracket(monkeypatch, captured)
    state = _state()
    decision = await maybe_auto_trade(state, _verdict(ask=5.50), source="auto_main")
    assert decision.fired, decision.reason
    spec = captured["spec"]
    assert spec.use_market_entry is False           # MKT entries retired
    assert spec.entry_limit == pytest.approx(5.70)  # ask 5.50 + buffer, tick-rounded
    assert spec.entry_timeout_sec == pytest.approx(45.0)
    assert captured["kwargs"]["state"] is state     # budget-release handle
    assert "signal_iter_id" in captured["kwargs"]["signal_ref"]


async def test_stale_quote_refetches_and_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    _capture_place_bracket(monkeypatch, captured)
    calls: list = []

    async def get_option_quote(contract):
        calls.append(contract)
        return _quote(6.20)

    state = _state(ibkr=SimpleNamespace(get_option_quote=get_option_quote))
    stale = int(time.time() * 1000) - 30_000
    decision = await maybe_auto_trade(
        state, _verdict(ask=5.50, quote_ts_ms=stale), source="auto_main",
    )
    assert decision.fired, decision.reason
    assert len(calls) == 1
    assert captured["spec"].entry_limit == pytest.approx(
        marketable_entry_limit("SPX", 6.20))


async def test_no_fresh_quote_blocks_never_falls_back_to_mkt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    _capture_place_bracket(monkeypatch, captured)

    async def get_option_quote(contract):
        raise RuntimeError("IBKR error 354: not subscribed")

    state = _state(ibkr=SimpleNamespace(get_option_quote=get_option_quote))

    # Persist a signal row so the counterfactual tag has a target.
    async with get_session() as session:
        sig = SignalIter(session_date="2026-07-16", iter_num=1, spx_price=7515.0)
        session.add(sig)
        await session.flush()
        sig_id = sig.id

    stale = int(time.time() * 1000) - 30_000
    decision = await maybe_auto_trade(
        state, _verdict(ask=None, quote_ts_ms=stale),
        source="auto_main", signal_iter_id=sig_id,
    )
    assert not decision.fired
    assert decision.reason_code == BlockReason.NO_FRESH_QUOTE
    assert "spec" not in captured  # place_bracket never called — no MKT fallback

    # Counterfactual contract: audit row + signal tag, like the other gates.
    async with get_session() as session:
        rows = list((await session.exec(
            select(AuditLog).where(AuditLog.block_reason_code == BlockReason.NO_FRESH_QUOTE)
        )).all())
        assert len(rows) == 1
        assert rows[0].category == "auto_trade_block"
        sig_row = (await session.exec(
            select(SignalIter).where(SignalIter.id == sig_id)
        )).first()
        assert (sig_row.extras or {}).get("skipped_by") == "no_fresh_quote"
        assert "NO_FRESH_QUOTE" in (sig_row.reasoning or "")

    # No cooldown consumed, no budget spent — a blocked fire must stay free.
    assert state.auto_trade_count_session == 0
    assert state.auto_trade_last_at_ms_by_symbol == {}
