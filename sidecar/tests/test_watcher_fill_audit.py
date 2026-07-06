"""Watcher tier-fire audit math (banked-per-fill vs position mark) +
place_bracket source attribution (manual_ticket vs auto_trader)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.db.models import LiveOrder
from fef_sidecar.engine.live_trading import BracketSpec, place_bracket
from fef_sidecar.engine.spx_target_watcher import (
    _fmt_money,
    _write_tier_fill_audit,
)
from fef_sidecar.ibkr import MockIBKRProvider


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


def _fake_result(sell_qty: int, fill_price: float, order_id: int = 900):
    """Mimics safe_market_sell's result: .sell_qty + .trade with an
    orderStatus carrying avgFillPrice (so _premium_for_log returns 'fill'
    immediately, no quote fallback / no sleep-poll beyond first check)."""
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=order_id),
        orderStatus=SimpleNamespace(avgFillPrice=fill_price),
    )
    return SimpleNamespace(placed=True, sell_qty=sell_qty, trade=trade)


def _fake_parent(qty: int = 3, entry: float = 12.60):
    return SimpleNamespace(
        id=77, symbol="SPX", strike=7490.0, right="C", expiry="20260702",
        qty=qty, limit_price=entry, avg_fill_price=entry,
    )


def test_fmt_money() -> None:
    assert _fmt_money(290.4) == "+$290"
    assert _fmt_money(-120.6) == "-$121"
    assert _fmt_money(0) == "+$0"
    assert _fmt_money(1470.0) == "+$1,470"


async def test_three_lot_partial_sequence_math() -> None:
    """T1/T2/T3 on a 3-lot: each audit line reports THIS fill's banked $
    and the running cumulative; the whole-position mark is clearly labeled
    (this exact mislabel caused a triple-count misread live)."""
    state = SimpleNamespace(ibkr=None)  # quote fallback never reached (fill known)
    p = _fake_parent(qty=3, entry=12.60)

    await _write_tier_fill_audit(state, p, "T1", _fake_result(1, 15.50, 901), "SPX 7491.20")
    await _write_tier_fill_audit(state, p, "T2", _fake_result(1, 17.00, 902), "SPX 7501.70")
    await _write_tier_fill_audit(state, p, "T3", _fake_result(1, 20.00, 903), "SPX 7522.30")

    rows = await SignalIterRepository.query_audit_log(category="spx_watcher")
    rows = list(reversed(rows))  # oldest first
    assert len(rows) == 3

    # T1: (15.50-12.60)*100*1 = +290 banked; mark = *3 = +870
    assert "T1: sold 1x SPX 7490.0C @ 15.50" in rows[0].summary
    assert "banked +$290 this fill" in rows[0].summary
    assert "(cum +$290; position mark +$870)" in rows[0].summary
    d0 = rows[0].details
    assert d0["banked_this_fill"] == pytest.approx(290.0)
    assert d0["cumulative_banked"] == pytest.approx(290.0)
    assert d0["position_mark"] == pytest.approx(870.0)
    assert d0["premium_source"] == "fill"
    assert d0["sell_qty"] == 1 and d0["original_qty"] == 3

    # T2: +440 banked; cum 730; mark 1320
    assert "banked +$440 this fill" in rows[1].summary
    assert "(cum +$730;" in rows[1].summary
    assert rows[1].details["position_mark"] == pytest.approx(1320.0)

    # T3: +740 banked; cum 1470; mark 2220
    assert "banked +$740 this fill" in rows[2].summary
    assert "(cum +$1,470;" in rows[2].summary
    assert rows[2].details["cumulative_banked"] == pytest.approx(1470.0)


async def test_quote_fallback_is_labeled() -> None:
    """No fill price at log time -> premium comes from a live quote and the
    summary labels it '(quote)' so it can't be read as a realized fill."""
    from datetime import datetime
    from fef_sidecar.ibkr.models import Contract, ContractKind, OptionQuote

    async def fake_quote(contract):
        c = Contract(symbol="SPX", kind=ContractKind.OPTION, exchange="CBOE",
                     expiry="20260702", strike=7490.0, right="C")
        return OptionQuote(timestamp=datetime.now(), contract=c,
                           last=14.0, bid=13.9, ask=14.1, delta=0.5)

    state = SimpleNamespace(ibkr=SimpleNamespace(get_option_quote=fake_quote))
    p = _fake_parent(qty=2, entry=12.0)
    result = _fake_result(1, 0.0, 905)  # avgFillPrice not yet populated

    # Patch the fill-poll wait down so the test doesn't sleep 2s
    import fef_sidecar.engine.spx_target_watcher as w
    orig = w._premium_for_log

    async def fast_premium(state_, trade, parent):
        st = getattr(trade, "orderStatus", None)
        avg = float(getattr(st, "avgFillPrice", 0) or 0)
        if avg > 0:
            return avg, "fill"
        q = await state_.ibkr.get_option_quote(SimpleNamespace(right=parent.right))
        return float(q.mid), "quote"

    w._premium_for_log = fast_premium
    try:
        await _write_tier_fill_audit(state, p, "STOP", result, "SPX 7460.00")
    finally:
        w._premium_for_log = orig

    rows = await SignalIterRepository.query_audit_log(category="spx_watcher")
    assert len(rows) == 1
    assert "@ 14.00 (quote)" in rows[0].summary
    assert rows[0].details["premium_source"] == "quote"
    # banked = (14-12)*100*1 = +200; mark = *2 = +400
    assert "banked +$200 this fill" in rows[0].summary
    assert "position mark +$400" in rows[0].summary


async def test_extra_summary_and_missing_premium() -> None:
    """Premium unavailable -> honest 'premium n/a' line, extra summary
    context (tick path) still appended, details keep the nulls."""
    async def no_quote(contract):
        raise RuntimeError("no data")

    state = SimpleNamespace(ibkr=SimpleNamespace(get_option_quote=no_quote))
    p = _fake_parent()

    import fef_sidecar.engine.spx_target_watcher as w
    orig = w._premium_for_log

    async def none_premium(state_, trade, parent):
        return None, "unavailable"

    w._premium_for_log = none_premium
    try:
        await _write_tier_fill_audit(
            state, p, "STOP/TICK", _fake_result(3, 0.0, 906), "SPX 7439.50",
            extra_summary="stop=7440.00, tick_age=45ms",
            extra_details={"stop_level": 7440.0, "tick_age_ms": 45},
        )
    finally:
        w._premium_for_log = orig

    rows = await SignalIterRepository.query_audit_log(category="spx_watcher")
    assert len(rows) == 1
    s = rows[0].summary
    assert "premium n/a at log time" in s
    assert "stop=7440.00, tick_age=45ms" in s
    assert rows[0].details["premium"] is None
    assert rows[0].details["stop_level"] == 7440.0


# ---------------------------------------------------------------------------
# place_bracket source attribution
# ---------------------------------------------------------------------------

def _spec() -> BracketSpec:
    return BracketSpec(
        symbol="SPX", strike=7490.0, right="C", expiry="20260702", qty=3,
        entry_limit=12.60, t1_limit=15.5, t2_limit=17.0, t3_limit=20.0,
        stop_limit=8.6, t1_qty=1, t2_qty=1, t3_qty=1,
    )


async def _parent_row() -> LiveOrder:
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    async with get_session() as session:
        rows = (await session.exec(
            select(LiveOrder).where(LiveOrder.bracket_role == "parent")
        )).all()
    assert len(rows) == 1
    return rows[0]


async def test_place_bracket_manual_source_default() -> None:
    result = await place_bracket(MockIBKRProvider(), _spec(), confirm=False)
    assert result["ok"] and result["dry_run"]
    orders = await SignalIterRepository.query_audit_log(category="order")
    assert len(orders) == 1
    row = orders[0]
    assert row.actor == "system"  # confirm=False stays system (unchanged rule)
    assert not row.summary.startswith("AUTO-TRADE")
    assert row.details["source"] == "manual_ticket"
    assert row.details["signal_ref"] is None
    parent = await _parent_row()
    assert "source=manual_ticket;" in parent.notes


async def test_place_bracket_auto_source_stamps_everything() -> None:
    ref = {"direction": "LONG_CALLS", "trigger_type": "flag_bull",
           "confidence": "LOW", "strike_rec": 7490.0, "reasoning": "PATTERN: flag_bull 1m"}
    result = await place_bracket(
        MockIBKRProvider(), _spec(), confirm=True,
        source="auto_pattern", signal_ref=ref,
    )
    assert result["ok"] and result["dry_run"]  # mock provider -> still dry-run
    orders = await SignalIterRepository.query_audit_log(category="order")
    assert len(orders) == 1
    row = orders[0]
    assert row.actor == "system"
    assert row.summary.startswith("AUTO-TRADE[auto_pattern] ")
    assert row.details["source"] == "auto_pattern"
    assert row.details["signal_ref"]["trigger_type"] == "flag_bull"
    parent = await _parent_row()
    assert "source=auto_pattern;" in parent.notes


async def test_place_bracket_manual_confirm_keeps_user_actor() -> None:
    result = await place_bracket(MockIBKRProvider(), _spec(), confirm=True)
    assert result["ok"]
    orders = await SignalIterRepository.query_audit_log(category="order")
    assert orders[0].actor == "user"
    assert orders[0].details["source"] == "manual_ticket"
