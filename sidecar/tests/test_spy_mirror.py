"""SPY mirror vehicle (2026-07-17) — unit + integration tests.

Contract under test (engine/spy_mirror.py + the watcher/auto_trader wiring):
  - Strike map = the VALIDATED spot-adjusted map (same percentage moneyness,
    round-half-up to SPY's $1 grid) — never K/10.
  - Ladder map: SPY level = SPY_entry_spot + (SPX offset / 10), cents.
  - Ex-div guard: hardcoded 2026 quarterly dates skip; unknown-year
    quarterly months skip (fail-safe); normal sessions pass.
  - Config default ON, qty 10; FEF_SPY_MIRROR_ENABLED env kill switch wins
    over config; config-read failure disables (fail-safe).
  - maybe_place_spy_mirror off-paths NEVER raise and NEVER place: disabled,
    unsupported strategy, non-today expiry, ex-div, SPY session cap, missing
    SPX spot, SPY spot fetch failure, implausible spot ratio — each leaves a
    category='spy_mirror' audit row and no LiveOrder.
  - Success path (DRY): SPY parent written with mirror_of=<SPX parent id> +
    notes tag, mapped strike, 50/30/20 tier split, /10-scaled ladder,
    scaled dollar_loss_cap; SPY session budget consumed.
  - Ledger separation: SPX book queries exclude mirrors structurally.
  - Cross-symbol cap ignores mirror parents (an open SPY mirror must not
    block the next same-right SPX fire).
  - EOD assignment flat: equity parents expiring today flatten from
    15:40 ET (audit category 'spy_eod_flat'); SPX parents never; refused
    sells roll the CLOSED latch back (parent stays managed).
  - Watcher stock-quote spot fallback resolves a SPY spot when no scanner
    snapshot / bar exists (the mirror runs without the SPY scanner).
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from sqlmodel import select

from fef_sidecar.config_store import UIConfig
from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import LiveOrder
from fef_sidecar.engine import spy_mirror as sm
from fef_sidecar.engine import spx_target_watcher as watcher
from fef_sidecar.engine.spy_mirror import (
    map_spy_level,
    map_spy_strike,
    maybe_place_spy_mirror,
    mirror_enabled,
    mirror_notional_scale,
    mirror_tier_split,
    scale_mirror_dollars,
    spy_ex_div_skip,
)


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FEF_SPY_MIRROR_ENABLED", raising=False)
    monkeypatch.delenv("FEF_LIVE_TRADING", raising=False)


def _patch_config(monkeypatch: pytest.MonkeyPatch, **kw):
    import fef_sidecar.config_store as cs
    cfg = UIConfig(**kw)
    monkeypatch.setattr(cs, "load_config", lambda: cfg)
    monkeypatch.setattr(cs, "config_file_status", lambda: "ok")
    return cfg


# ---------------------------------------------------------------------------
# 1. Strike mapping — the validated spot-adjusted map
# ---------------------------------------------------------------------------


def test_map_strike_call_otm():
    # SPX 7540 @ spot 7500 = +0.5333% moneyness; SPY 627.50 -> 630.846 -> 631
    assert map_spy_strike(7540.0, 7500.0, 627.50) == 631.0


def test_map_strike_put_side():
    # SPX 7460 @ 7500 = -0.5333%; SPY 627.50 -> 624.153 -> 624
    assert map_spy_strike(7460.0, 7500.0, 627.50) == 624.0


def test_map_strike_atm_rounds_to_grid():
    assert map_spy_strike(7500.0, 7500.0, 627.49) == 627.0
    assert map_spy_strike(7500.0, 7500.0, 627.51) == 628.0


def test_map_strike_round_half_up():
    # Exactly x.5 rounds UP (the study's round-half-up, not banker's).
    # 1000 * (1 + 3.75/7500) = 1000.5
    assert map_spy_strike(7503.75, 7500.0, 1000.0) == 1001.0


def test_map_strike_not_k_over_10():
    # The killed K/10 confound: SPY trades ~0.6% under SPX/10 — the map must
    # follow the moneyness, not the /10 of the strike.
    spy_spot = 745.0  # ~0.67% under 7500/10
    k10 = 7540.0 / 10.0  # 754 — the OLD broken map
    mapped = map_spy_strike(7540.0, 7500.0, spy_spot)
    assert mapped == 749.0  # 745 * 1.005333 = 748.97
    assert mapped != round(k10)


def test_map_strike_rejects_bad_spots():
    with pytest.raises(ValueError):
        map_spy_strike(7540.0, 0.0, 627.5)
    with pytest.raises(ValueError):
        map_spy_strike(7540.0, 7500.0, -1.0)


def test_map_level_divides_offsets_by_ten():
    # SPX T1 +15pt -> SPY +1.5; stop -10pt -> SPY -1.0
    assert map_spy_level(7515.0, 7500.0, 627.50) == 629.0
    assert map_spy_level(7490.0, 7500.0, 627.50) == 626.5
    # Put-side target below entry maps below SPY spot symmetrically.
    assert map_spy_level(7485.0, 7500.0, 627.50) == 626.0


# ---------------------------------------------------------------------------
# 2. Ex-div guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("d", ["20260320", "20260619", "20260918", "20261218"])
def test_ex_div_known_2026_dates_skip(d: str):
    skip, why = spy_ex_div_skip(d)
    assert skip and "ex-dividend" in why


@pytest.mark.parametrize("d", ["20260319", "20260618", "20260717", "20261217"])
def test_ex_div_normal_2026_sessions_pass(d: str):
    skip, _ = spy_ex_div_skip(d)
    assert not skip


def test_ex_div_unknown_year_quarterly_month_fails_safe():
    skip, why = spy_ex_div_skip("20270319")
    assert skip and "fail-safe" in why


def test_ex_div_unknown_year_non_quarterly_passes():
    skip, _ = spy_ex_div_skip("20270416")
    assert not skip


@pytest.mark.parametrize("d", ["", "garbage", "2026-06-19", "99999999"])
def test_ex_div_unparseable_fails_safe(d: str):
    skip, why = spy_ex_div_skip(d)
    assert skip


# ---------------------------------------------------------------------------
# 3. Tier split + dollar scaling
# ---------------------------------------------------------------------------


def test_tier_split_default_qty_is_50_30_20():
    assert mirror_tier_split(10) == (5, 3, 2)


@pytest.mark.parametrize("q,expected", [
    (0, (0, 0, 0)), (1, (1, 0, 0)), (2, (1, 1, 0)),
])
def test_tier_split_small_qty_degeneracy(q, expected):
    assert mirror_tier_split(q) == expected


@pytest.mark.parametrize("q", list(range(1, 21)))
def test_tier_split_sums_and_nonnegative(q):
    t1, t2, t3 = mirror_tier_split(q)
    assert t1 + t2 + t3 == q
    assert t1 >= 1 and t2 >= 0 and t3 >= 0


def test_notional_scale():
    # 10 SPY = 1 SPX-equivalent; over a 3-lot SPX fire -> 1/3 dollar scale.
    assert mirror_notional_scale(10, 3) == pytest.approx(1.0 / 3.0)
    assert mirror_notional_scale(10, 1) == pytest.approx(1.0)
    assert mirror_notional_scale(5, 3) == pytest.approx(0.5 / 3.0)


def test_scale_mirror_dollars():
    s = mirror_notional_scale(10, 3)
    assert scale_mirror_dollars(1000.0, s) == 333.0
    assert scale_mirror_dollars(-1000.0, s) == -333.0
    assert scale_mirror_dollars(1800.0, s) == 600.0
    # $10 minimum magnitude, sign preserved
    assert scale_mirror_dollars(-20.0, 0.01) == -10.0


# ---------------------------------------------------------------------------
# 4. Config defaults + kill switch
# ---------------------------------------------------------------------------


def test_config_defaults():
    cfg = UIConfig()
    assert cfg.spy_mirror_enabled is True
    assert cfg.spy_mirror_qty == 10


def test_mirror_enabled_env_kill_switch_wins(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch, spy_mirror_enabled=True)
    monkeypatch.setenv("FEF_SPY_MIRROR_ENABLED", "false")
    enabled, why = mirror_enabled()
    assert not enabled and "kill switch" in why


def test_mirror_enabled_env_true_defers_to_config(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch, spy_mirror_enabled=True)
    monkeypatch.setenv("FEF_SPY_MIRROR_ENABLED", "true")
    assert mirror_enabled()[0] is True


def test_mirror_enabled_config_off(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch, spy_mirror_enabled=False)
    enabled, why = mirror_enabled()
    assert not enabled and "config" in why


def test_mirror_enabled_config_read_failure_fails_safe(
    monkeypatch: pytest.MonkeyPatch,
):
    import fef_sidecar.config_store as cs

    def boom():
        raise RuntimeError("corrupt")

    monkeypatch.setattr(cs, "load_config", boom)
    enabled, why = mirror_enabled()
    assert not enabled and "fail-safe" in why


# ---------------------------------------------------------------------------
# 5. maybe_place_spy_mirror — off-paths (fail-soft skips, never raise)
# ---------------------------------------------------------------------------

_TODAY = time.strftime("%Y%m%d")


def _mirror_state(**kw) -> SimpleNamespace:
    d = dict(
        auto_trade_count_by_symbol={},
        auto_trade_last_at_ms_by_symbol={},
        auto_trade_count_session=0,
        auto_trade_max_per_symbol=2,
        last_live_tick_spx=7500.0,
        last_spx_price=7500.0,
        exit_dollar_t1=1000.0, exit_dollar_t2=2500.0,
        exit_dollar_t3=5000.0, exit_dollar_stop=-1000.0,
        ibkr=SimpleNamespace(),  # bare — spot fetch raises => fail-soft
    )
    d.update(kw)
    return SimpleNamespace(**d)


def _mirror_kwargs(**kw) -> dict:
    d = dict(
        direction="LONG_CALLS", spx_strike=7540.0, spx_qty=3,
        spx_parent_id=42, source="auto_scanner", strategy="spx_points",
        spx_t1=7515.0, spx_t2=7525.0, spx_t3=7550.0, spx_stop=7490.0,
        entry_timeout_sec=45.0,
    )
    d.update(kw)
    return d


def _patch_expiry(monkeypatch: pytest.MonkeyPatch, expiry: str = _TODAY,
                  today: str = _TODAY):
    import fef_sidecar.engine.expiry_resolver as er

    async def fake_resolve(state, symbol):
        return expiry

    monkeypatch.setattr(er, "resolve_nearest_expiry", fake_resolve)
    monkeypatch.setattr(er, "_today_et_ymd", lambda: today)


async def _mirror_audit_rows():
    return await SignalIterRepository.query_audit_log(category="spy_mirror")


async def _live_orders() -> list[LiveOrder]:
    async with get_session() as session:
        return list((await session.exec(select(LiveOrder))).all())


async def test_skip_when_disabled(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch, spy_mirror_enabled=False)
    r = await maybe_place_spy_mirror(_mirror_state(), **_mirror_kwargs())
    assert r == {"ok": False, "skipped": "disabled",
                 "reason": "spy_mirror_enabled=false in config"}
    rows = await _mirror_audit_rows()
    assert len(rows) == 1 and rows[0].details["skip_reason"] == "disabled"
    assert await _live_orders() == []


async def test_skip_env_kill_switch(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch, spy_mirror_enabled=True)
    monkeypatch.setenv("FEF_SPY_MIRROR_ENABLED", "0")
    r = await maybe_place_spy_mirror(_mirror_state(), **_mirror_kwargs())
    assert r["skipped"] == "disabled"
    assert await _live_orders() == []


async def test_skip_unsupported_strategy(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch)
    r = await maybe_place_spy_mirror(
        _mirror_state(), **_mirror_kwargs(strategy="option_price"),
    )
    assert r["skipped"] == "strategy_unsupported"
    assert await _live_orders() == []


async def test_skip_no_same_day_expiry(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch, expiry="20991231", today=_TODAY)
    r = await maybe_place_spy_mirror(_mirror_state(), **_mirror_kwargs())
    assert r["skipped"] == "no_same_day_expiry"
    assert await _live_orders() == []


async def test_skip_ex_div_expiry(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch, expiry="20260619", today="20260619")
    r = await maybe_place_spy_mirror(_mirror_state(), **_mirror_kwargs())
    assert r["skipped"] == "ex_div"
    rows = await _mirror_audit_rows()
    assert rows and rows[-1].details["skip_reason"] == "ex_div"
    assert await _live_orders() == []


async def test_skip_symbol_cap(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)
    state = _mirror_state(auto_trade_count_by_symbol={"SPY": 2})
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["skipped"] == "symbol_cap"
    assert await _live_orders() == []


async def test_skip_no_spx_spot(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)
    state = _mirror_state(last_live_tick_spx=0.0, last_spx_price=0.0)
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["skipped"] == "no_spx_spot"
    assert await _live_orders() == []


async def test_skip_spot_fetch_failure_never_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    """The core fail-soft: SPY spot unavailable -> skip + audit, no order —
    the SPX fire (already placed by the caller) is untouched."""
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)

    async def broken_quote(contract):
        raise ConnectionError("TWS gone")

    state = _mirror_state(ibkr=SimpleNamespace(get_last_quote=broken_quote))
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["skipped"] == "spot_fetch_failed"
    rows = await _mirror_audit_rows()
    assert rows and rows[-1].details["skip_reason"] == "spot_fetch_failed"
    assert await _live_orders() == []
    # No SPY budget consumed on a skip.
    assert state.auto_trade_count_by_symbol == {}


async def test_skip_implausible_spot_ratio(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)

    async def stale_quote(contract):
        # SPY quote at 100 while SPX at 7500 -> ratio 75, way off ~10.
        return SimpleNamespace(bid=100.0, ask=100.02, last=100.01)

    state = _mirror_state(ibkr=SimpleNamespace(get_last_quote=stale_quote))
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["skipped"] == "spot_ratio_implausible"
    assert await _live_orders() == []


async def test_skip_duplicate_spy_parent(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)
    # Active SPY parent already at the mapped strike (631C, from 7540@7500
    # on SPY 627.50).
    await SignalIterRepository.write_live_order(LiveOrder(
        symbol="SPY", strike=631.0, right="C", expiry=_TODAY,
        action="BUY", qty=10, order_type="LMT", limit_price=0.6,
        bracket_role="parent", status="FILLED", exit_strategy="spx_points",
        dry_run=False,
    ))

    async def spy_quote(contract):
        return SimpleNamespace(bid=627.49, ask=627.51, last=627.50)

    state = _mirror_state(ibkr=SimpleNamespace(get_last_quote=spy_quote))
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["skipped"] == "duplicate_parent"


async def test_skip_no_fresh_option_quote(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)

    async def spy_quote(contract):
        return SimpleNamespace(bid=627.49, ask=627.51, last=627.50)

    async def dead_option_quote(contract):
        return SimpleNamespace(bid=None, ask=None, last=None)

    state = _mirror_state(ibkr=SimpleNamespace(
        get_last_quote=spy_quote, get_option_quote=dead_option_quote,
    ))
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["skipped"] == "no_fresh_quote"
    assert await _live_orders() == []


# ---------------------------------------------------------------------------
# 6. Success path (DRY) — mapping, tagging, budget
# ---------------------------------------------------------------------------


def _working_ibkr() -> SimpleNamespace:
    async def spy_quote(contract):
        assert contract.symbol == "SPY"
        return SimpleNamespace(bid=627.49, ask=627.51, last=627.50)

    async def option_quote(contract):
        assert contract.symbol == "SPY"
        assert contract.exchange == "SMART"
        return SimpleNamespace(bid=0.50, ask=0.55, last=0.52)

    return SimpleNamespace(get_last_quote=spy_quote, get_option_quote=option_quote)


async def test_mirror_places_spy_parent_dry(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch)  # spy_mirror defaults: enabled, qty 10
    _patch_expiry(monkeypatch)
    state = _mirror_state(ibkr=_working_ibkr())

    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["ok"] is True

    orders = await _live_orders()
    assert len(orders) == 1
    p = orders[0]
    assert p.symbol == "SPY"
    assert p.strike == 631.0                      # validated map, not 754
    assert p.right == "C"
    assert p.qty == 10
    assert p.expiry == _TODAY
    assert p.exit_strategy == "spx_points"
    # Ledger tagging — BOTH the column and the notes tag.
    assert p.mirror_of == 42
    assert "mirror_of=42;" in p.notes
    # 50/30/20 partials at qty 10 -> 5/3/2.
    assert (p.spx_target_t1_qty, p.spx_target_t2_qty, p.spx_target_t3_qty) == (5, 3, 2)
    # Ladder = SPY spot +/- SPX offsets / 10 (spot mid 627.50).
    assert p.spx_target_t1 == pytest.approx(629.0)
    assert p.spx_target_t2 == pytest.approx(630.0)
    assert p.spx_target_t3 == pytest.approx(632.5)
    assert p.spx_target_stop == pytest.approx(626.5)
    assert p.entry_spx_price == pytest.approx(627.50)
    assert p.dynamic_stop_level == pytest.approx(626.5)
    # Catastrophe cap at the mirror's own scale: 1800 * (10/10)/3 = 600.
    assert p.dollar_loss_cap == pytest.approx(-600.0)
    # Marketable LMT off the SPY ask: 0.55 + max(0.10, 2%) = 0.65 on pennies.
    assert p.limit_price == pytest.approx(0.65)
    # DRY (no FEF_LIVE_TRADING in tests).
    assert p.dry_run is True and p.status == "DRY_RUN"

    # Budget consumed on the SPY book.
    assert state.auto_trade_count_by_symbol == {"SPY": 1}
    assert state.auto_trade_count_session == 1
    assert state.auto_trade_last_at_ms_by_symbol["SPY"] > 0

    rows = await _mirror_audit_rows()
    assert any("SPY MIRROR PLACED" in r_.summary for r_ in rows)


async def test_mirror_fixed_dollar_scales_targets(monkeypatch: pytest.MonkeyPatch):
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)
    state = _mirror_state(ibkr=_working_ibkr())

    r = await maybe_place_spy_mirror(
        state, **_mirror_kwargs(strategy="fixed_dollar"),
    )
    assert r["ok"] is True
    p = (await _live_orders())[0]
    assert p.exit_strategy == "fixed_dollar"
    # $ ladder scaled by (10/10)/3: 1000/2500/5000/-1000 -> 333/833/1667/-333
    assert p.dollar_target_t1 == pytest.approx(333.0)
    assert p.dollar_target_t2 == pytest.approx(833.0)
    assert p.dollar_target_t3 == pytest.approx(1667.0)
    assert p.dollar_target_stop == pytest.approx(-333.0)
    assert p.entry_spx_price is None  # BE ladder is spx_points-only


async def test_ledger_separation_queries(monkeypatch: pytest.MonkeyPatch):
    """The SPX verdict window must be computable excluding mirrors."""
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)
    # The SPX fire's own parent...
    spx_p = await SignalIterRepository.write_live_order(LiveOrder(
        symbol="SPX", strike=7540.0, right="C", expiry=_TODAY,
        action="BUY", qty=3, order_type="LMT", limit_price=5.1,
        bracket_role="parent", status="FILLED", exit_strategy="spx_points",
        dry_run=False,
    ))
    # ...and its mirror.
    state = _mirror_state(ibkr=_working_ibkr())
    r = await maybe_place_spy_mirror(
        state, **_mirror_kwargs(spx_parent_id=spx_p.id),
    )
    assert r["ok"] is True

    async with get_session() as session:
        spx_book = list((await session.exec(
            select(LiveOrder)
            .where(LiveOrder.symbol == "SPX")
            .where(LiveOrder.mirror_of.is_(None))
        )).all())
        mirror_book = list((await session.exec(
            select(LiveOrder).where(LiveOrder.mirror_of.is_not(None))
        )).all())
    assert [o.id for o in spx_book] == [spx_p.id]
    assert len(mirror_book) == 1
    assert mirror_book[0].symbol == "SPY"
    assert mirror_book[0].mirror_of == spx_p.id


# ---------------------------------------------------------------------------
# 7. Cross-symbol cap ignores mirrors
# ---------------------------------------------------------------------------


async def test_cross_symbol_cap_ignores_mirror_parents(
    monkeypatch: pytest.MonkeyPatch,
):
    """An OPEN SPY mirror must not cross-symbol-block the next same-right
    SPX fire (it is the same trade by design, not independent exposure).
    A NON-mirror SPY parent still blocks."""
    from fef_sidecar.engine.auto_trader import BlockReason, maybe_auto_trade

    monkeypatch.setattr(
        "fef_sidecar.engine.live_trading.is_live_trading_enabled", lambda: True,
    )
    monkeypatch.delenv("FEF_DISABLE_CONFIDENCE_FLOOR", raising=False)
    _patch_config(monkeypatch, auto_trade_confidence_floor="LOW",
                  cross_symbol_cap_enabled=True)

    calls: list = []

    async def fake_place_bracket(ibkr, spec, *, confirm=False, ticket_id=None, **kwargs):
        calls.append(spec)
        return {"ok": True, "parent_order_id": len(calls), "dry_run": True}

    import fef_sidecar.engine.live_trading as lt
    monkeypatch.setattr(lt, "place_bracket", fake_place_bracket)
    # Neutralize the mirror hook itself (separately tested) so this test
    # isolates the cap gate.
    monkeypatch.setenv("FEF_SPY_MIRROR_ENABLED", "false")

    # An open SPY MIRROR parent, same right.
    await SignalIterRepository.write_live_order(LiveOrder(
        symbol="SPY", strike=631.0, right="C", expiry=_TODAY,
        action="BUY", qty=10, order_type="LMT", limit_price=0.65,
        bracket_role="parent", status="FILLED", exit_strategy="spx_points",
        dry_run=False, mirror_of=999,
    ))

    def _gate_state():
        return SimpleNamespace(
            auto_trade_enabled=True, active_symbol="SPX",
            auto_trade_trigger_types=["confluence", "aligned_only"],
            auto_trade_cool_down_sec=300,
            auto_trade_last_at_ms_by_symbol={}, auto_trade_count_by_symbol={},
            auto_trade_max_per_symbol=2, auto_trade_qty_override=0,
            auto_trade_last_at_ms=0, auto_trade_count_session=0,
            last_spx_price=7440.0, ibkr=SimpleNamespace(),
        )

    verdict = {
        "direction": "LONG_CALLS", "confidence": "HIGH",
        "reasoning": "ALIGNED LONG_CALLS — test", "aligned": True,
        "wall_collapse_trigger": False,
        "delta_price": 2.1, "delta_drift_m": 30.0, "fe_ratio": 1.2,
        "drift_bias": "NEUTRAL", "strike_rec": 7450.0,
        "t1": 7455.0, "t2": 7465.0, "t3": 7490.0, "stop": 7430.0,
        "option_pricing": {
            "contract_strike": 7450.0, "right": "C", "entry_limit": 5.1,
            "delta": 0.4, "underlying_price_at_quote": 7440.0,
            "t1": 11.1, "t2": 15.1, "t3": 25.1, "stop": 1.1,
            "bid": 4.9, "ask": 5.1, "quote_ts_ms": int(time.time() * 1000),
        },
        "suggested_qty": 3,
    }

    d = await maybe_auto_trade(_gate_state(), dict(verdict))
    assert d.fired, f"mirror parent must not trigger the cap: {d.reason}"
    assert len(calls) == 1

    # Control: the same parent WITHOUT the mirror tag blocks.
    async with get_session() as session:
        row = (await session.exec(
            select(LiveOrder).where(LiveOrder.symbol == "SPY")
        )).first()
        row.mirror_of = None
        session.add(row)
    d2 = await maybe_auto_trade(_gate_state(), dict(verdict))
    assert not d2.fired
    assert d2.reason_code == BlockReason.CROSS_SYMBOL_CAP_SKIP


# ---------------------------------------------------------------------------
# 8. EOD assignment flat
# ---------------------------------------------------------------------------


def _watcher_state() -> SimpleNamespace:
    return SimpleNamespace(
        firing_parent_ids=set(), firing_contracts={},
        ibkr=SimpleNamespace(),
    )


async def _spy_parent(expiry: str, *, symbol: str = "SPY",
                      status: str = "FILLED") -> LiveOrder:
    return await SignalIterRepository.write_live_order(LiveOrder(
        symbol=symbol, strike=631.0 if symbol == "SPY" else 7540.0,
        right="C", expiry=expiry, action="BUY", qty=10, order_type="LMT",
        limit_price=0.65, bracket_role="parent", status=status,
        exit_strategy="spx_points", dry_run=False,
        spx_target_t1_qty=5, spx_target_t2_qty=3, spx_target_t3_qty=2,
        filled_qty=10, avg_fill_price=0.60,
    ))


def test_eod_flat_due_window():
    p_spy = SimpleNamespace(symbol="SPY", expiry="20260717")
    assert watcher._eod_flat_due(p_spy, "15:40", "20260717") is True
    assert watcher._eod_flat_due(p_spy, "15:39", "20260717") is False
    assert watcher._eod_flat_due(p_spy, "16:15", "20260717") is False
    # Not expiring today -> never
    assert watcher._eod_flat_due(
        SimpleNamespace(symbol="SPY", expiry="20260718"), "15:45", "20260717",
    ) is False
    # SPX (European/cash) -> never
    assert watcher._eod_flat_due(
        SimpleNamespace(symbol="SPX", expiry="20260717"), "15:45", "20260717",
    ) is False


def test_eod_flat_start_env_validation(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FEF_EOD_FLAT_START_ET", "15:30")
    assert watcher._eod_flat_start_et() == "15:30"
    monkeypatch.setenv("FEF_EOD_FLAT_START_ET", "garbage")
    assert watcher._eod_flat_start_et() == "15:40"
    monkeypatch.setenv("FEF_EOD_FLAT_START_ET", "17:00")  # past end -> default
    assert watcher._eod_flat_start_et() == "15:40"
    monkeypatch.delenv("FEF_EOD_FLAT_START_ET", raising=False)
    assert watcher._eod_flat_start_et() == "15:40"


async def test_eod_flat_sells_remaining_and_audits(
    monkeypatch: pytest.MonkeyPatch,
):
    p = await _spy_parent("20260717")
    spx = await _spy_parent("20260717", symbol="SPX")
    state = _watcher_state()
    monkeypatch.setattr(watcher, "_et_now_hhmm_ymd",
                        lambda: ("15:41", "20260717"))

    sells: list = []

    async def fake_safe_sell(state_, p_, qty, tier_label, ref_desc, outcome=None):
        sells.append((p_.id, qty, tier_label))
        if outcome is not None:
            outcome["kind"] = "placed"
        return True

    monkeypatch.setattr(watcher, "_watcher_safe_sell", fake_safe_sell)

    handled = await watcher._eod_assignment_flat_cycle(state, [p, spx])
    assert handled == {p.id}
    assert sells == [(p.id, 10, "EOD_FLAT")]  # SPX parent untouched

    async with get_session() as session:
        rowp = (await session.exec(
            select(LiveOrder).where(LiveOrder.id == p.id))).first()
        rowx = (await session.exec(
            select(LiveOrder).where(LiveOrder.id == spx.id))).first()
    assert rowp.status == "CLOSED"
    assert rowx.status == "FILLED"

    rows = await SignalIterRepository.query_audit_log(category="spy_eod_flat")
    assert len(rows) == 1
    assert "EOD FLAT" in rows[0].summary and "SPY" in rows[0].summary
    assert rows[0].details["qty"] == 10


async def test_eod_flat_respects_banked_partials(
    monkeypatch: pytest.MonkeyPatch,
):
    """Only the UNFIRED remainder is sold — banked tiers stay banked."""
    p = await _spy_parent("20260717")
    await SignalIterRepository.update_live_order(p.id, spx_target_t1_fired=True)
    p.spx_target_t1_fired = True
    state = _watcher_state()
    monkeypatch.setattr(watcher, "_et_now_hhmm_ymd",
                        lambda: ("15:45", "20260717"))
    sells: list = []

    async def fake_safe_sell(state_, p_, qty, tier_label, ref_desc, outcome=None):
        sells.append(qty)
        if outcome is not None:
            outcome["kind"] = "placed"
        return True

    monkeypatch.setattr(watcher, "_watcher_safe_sell", fake_safe_sell)
    handled = await watcher._eod_assignment_flat_cycle(state, [p])
    assert handled == {p.id}
    assert sells == [5]  # T2 (3) + T3 (2); T1's 5 already banked


async def test_eod_flat_before_window_noop(monkeypatch: pytest.MonkeyPatch):
    p = await _spy_parent("20260717")
    state = _watcher_state()
    monkeypatch.setattr(watcher, "_et_now_hhmm_ymd",
                        lambda: ("15:39", "20260717"))
    called = []

    async def fake_safe_sell(*a, **k):
        called.append(1)
        return True

    monkeypatch.setattr(watcher, "_watcher_safe_sell", fake_safe_sell)
    handled = await watcher._eod_assignment_flat_cycle(state, [p])
    assert handled == set() and called == []
    async with get_session() as session:
        row = (await session.exec(
            select(LiveOrder).where(LiveOrder.id == p.id))).first()
    assert row.status == "FILLED"


async def test_eod_flat_refused_sell_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
):
    """A refused sell (fill not yet propagated) must NOT strand the parent
    behind the CLOSED latch — the 7/7 eviction class."""
    p = await _spy_parent("20260717")
    state = _watcher_state()
    monkeypatch.setattr(watcher, "_et_now_hhmm_ymd",
                        lambda: ("15:50", "20260717"))

    async def refusing_safe_sell(state_, p_, qty, tier_label, ref_desc, outcome=None):
        if outcome is not None:
            outcome["kind"] = "refused_guard"
        return False

    monkeypatch.setattr(watcher, "_watcher_safe_sell", refusing_safe_sell)
    handled = await watcher._eod_assignment_flat_cycle(state, [p])
    assert handled == set()

    async with get_session() as session:
        row = (await session.exec(
            select(LiveOrder).where(LiveOrder.id == p.id))).first()
    assert row.status == "FILLED"  # latch rolled back — parent stays managed
    assert not await SignalIterRepository.query_audit_log(category="spy_eod_flat")
    retry = await SignalIterRepository.query_audit_log(
        category="watcher_refusal_retry")
    assert retry and "EOD_FLAT" in retry[0].summary
    assert p.id not in state.firing_parent_ids  # latch released


async def test_eod_flat_skips_parent_with_fire_in_flight(
    monkeypatch: pytest.MonkeyPatch,
):
    p = await _spy_parent("20260717")
    state = _watcher_state()
    state.firing_parent_ids.add(p.id)  # a stop fire owns this parent
    monkeypatch.setattr(watcher, "_et_now_hhmm_ymd",
                        lambda: ("15:50", "20260717"))
    called = []

    async def fake_safe_sell(*a, **k):
        called.append(1)
        return True

    monkeypatch.setattr(watcher, "_watcher_safe_sell", fake_safe_sell)
    handled = await watcher._eod_assignment_flat_cycle(state, [p])
    assert handled == set() and called == []


# ---------------------------------------------------------------------------
# 9. Watcher stock-quote spot fallback
# ---------------------------------------------------------------------------


async def test_stock_quote_spot_fallback_resolves_spy(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []

    async def spy_quote(contract):
        calls.append(contract.symbol)
        return SimpleNamespace(bid=627.40, ask=627.60, last=627.50)

    state = SimpleNamespace(ibkr=SimpleNamespace(get_last_quote=spy_quote))
    spot, src = await watcher._watcher_stock_quote_spot(state, "SPY")
    assert spot == pytest.approx(627.50)
    assert src == "ibkr_stock_quote"
    # TTL cache: second call answers without a round-trip.
    spot2, _ = await watcher._watcher_stock_quote_spot(state, "SPY")
    assert spot2 == pytest.approx(627.50)
    assert calls == ["SPY"]


async def test_stock_quote_spot_non_stock_symbol_none():
    state = SimpleNamespace(ibkr=SimpleNamespace())
    assert await watcher._watcher_stock_quote_spot(state, "SPX") == (None, None)


async def test_stock_quote_spot_failure_cached(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []

    async def broken(contract):
        calls.append(1)
        raise ConnectionError("down")

    state = SimpleNamespace(ibkr=SimpleNamespace(get_last_quote=broken))
    assert await watcher._watcher_stock_quote_spot(state, "SPY") == (None, None)
    assert await watcher._watcher_stock_quote_spot(state, "SPY") == (None, None)
    assert len(calls) == 1  # failure TTL-cached — no hammering


async def test_watcher_symbol_spot_uses_stock_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    """Full resolution chain: active symbol SPX, no scanner snapshot, no
    bars — a SPY mirror parent still gets a symbol-correct spot."""
    async def spy_quote(contract):
        return SimpleNamespace(bid=627.40, ask=627.60, last=627.50)

    state = SimpleNamespace(
        active_symbol="SPX",
        last_scanner_verdict_by_symbol={},
        ibkr=SimpleNamespace(get_last_quote=spy_quote),
    )
    p = SimpleNamespace(id=1, symbol="SPY", strike=631.0, right="C",
                        expiry="20260717")
    spot, src = await watcher._watcher_symbol_spot(state, p, 7500.0, {})
    assert spot == pytest.approx(627.50)
    assert src == "ibkr_stock_quote"
