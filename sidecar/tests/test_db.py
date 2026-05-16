"""DB layer tests against in-memory SQLite."""

from __future__ import annotations

import os

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.db import (
    FEFConfidence,
    FEFDirection,
    SignalIterRepository,
    TicketStatus,
    TradeTicket,
    dispose_db,
    init_db,
)


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.mark.asyncio
async def test_write_iter_creates_signal_and_bumps_daily() -> None:
    signal = await SignalIterRepository.write_iter(
        spx_price=7410.0,
        direction=FEFDirection.WAIT,
        confidence=FEFConfidence.NONE,
        reasoning="pin regime — no edge",
        drift_cum_net_m=12.3,
        drift_bias="BULLISH",
        ntm_max_pain=7400.0,
        raw_max_pain=7000.0,
        top_gex_walls=[{"strike": 7400.0, "net_m": 32703.22, "side": "CALL"}],
        top_dex_walls=[{"strike": 7400.0, "net_m": 1336333.38, "side": "CALL"}],
        aggression_call_pct=40.3,
        aggression_put_pct=38.4,
        session_date="2026-05-15",
    )

    assert signal.id is not None
    assert signal.iter_num == 1
    assert signal.session_date == "2026-05-15"
    assert signal.direction == FEFDirection.WAIT
    assert signal.top_gex_walls[0]["strike"] == 7400.0


@pytest.mark.asyncio
async def test_iter_num_increments_within_session() -> None:
    for _ in range(3):
        await SignalIterRepository.write_iter(
            spx_price=7410.0,
            direction=FEFDirection.WAIT,
            confidence=FEFConfidence.NONE,
            reasoning="",
            drift_cum_net_m=0.0,
            drift_bias="NEUTRAL",
            ntm_max_pain=7400.0,
            raw_max_pain=7000.0,
            top_gex_walls=[],
            top_dex_walls=[],
            session_date="2026-05-15",
        )

    iters = await SignalIterRepository.recent(session_date="2026-05-15")
    assert [s.iter_num for s in iters] == [3, 2, 1]


@pytest.mark.asyncio
async def test_actionable_filter() -> None:
    await SignalIterRepository.write_iter(
        spx_price=7410.0,
        direction=FEFDirection.WAIT,
        confidence=FEFConfidence.NONE,
        reasoning="chop",
        drift_cum_net_m=0.0,
        drift_bias="NEUTRAL",
        ntm_max_pain=7400.0,
        raw_max_pain=7000.0,
        top_gex_walls=[],
        top_dex_walls=[],
        session_date="2026-05-15",
    )
    actionable = await SignalIterRepository.write_iter(
        spx_price=7420.0,
        direction=FEFDirection.LONG_CALLS,
        confidence=FEFConfidence.HIGH,
        reasoning="wall test bounce + bullish drift alignment",
        drift_cum_net_m=30.19,
        drift_bias="BULLISH",
        ntm_max_pain=7400.0,
        raw_max_pain=7000.0,
        top_gex_walls=[{"strike": 7400.0, "net_m": 32703.22, "side": "CALL"}],
        top_dex_walls=[{"strike": 7400.0, "net_m": 1336333.38, "side": "CALL"}],
        session_date="2026-05-15",
    )

    rows = await SignalIterRepository.actionable_today(session_date="2026-05-15")
    assert len(rows) == 1
    assert rows[0].id == actionable.id
    assert rows[0].direction == FEFDirection.LONG_CALLS


@pytest.mark.asyncio
async def test_attach_ticket_increments_trades_taken() -> None:
    signal = await SignalIterRepository.write_iter(
        spx_price=7420.0,
        direction=FEFDirection.LONG_CALLS,
        confidence=FEFConfidence.HIGH,
        reasoning="bounce off mega call wall",
        drift_cum_net_m=30.19,
        drift_bias="BULLISH",
        ntm_max_pain=7400.0,
        raw_max_pain=7000.0,
        top_gex_walls=[],
        top_dex_walls=[],
        session_date="2026-05-15",
    )
    assert signal.id is not None

    ticket = TradeTicket(
        signal_iter_id=signal.id,
        direction=FEFDirection.LONG_CALLS,
        contract_strike=7450.0,
        contract_expiry="2026-05-15",
        qty=15,
        entry_limit=7.84,
        t1=9.0,
        t2=9.2,
        t3=9.6,
        stop=5.0,
        status=TicketStatus.DRAFT,
    )
    saved = await SignalIterRepository.attach_ticket(signal.id, ticket)
    assert saved.id is not None
    assert saved.signal_iter_id == signal.id
