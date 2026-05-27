"""FIFO pair-off tests for ``daily_review.actual_wins/actual_losses/actual_total_R``.

Covers the 2026-05-23 fix where the per-parent W/L attribution moved from
the ``LiveOrder.spx_target_t{1,2,3}_fired`` boolean flags (intent) to
broker-confirmed ``IBKRExecution`` fills (ground truth), via
``engine/exec_pair_off::compute_parent_wl_from_fills``.

Three scenarios:

* (a) Parent with clean T1+T2+T3 pair-off = winner
* (b) Parent with stop-out before any T fires = loser
* (c) Parent still open at EOD = no W/L counted but realized portion (if any) included

A fourth test pins the fallback path: no execs for a session -> the
``_estimate_parent_r`` flag-based estimator is used so dry-run / paper-
only sessions still get a signed count.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import (
    FEFConfidence,
    FEFDirection,
    IBKRExecution,
    LiveOrder,
    SignalIter,
)
from fef_sidecar.engine.daily_review import compute_daily_review
from fef_sidecar.engine.exec_pair_off import compute_parent_wl_from_fills


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


def _make_parent(
    *,
    submitted_at: datetime,
    ib_order_id: int,
    qty: int = 5,
    strike: float = 7460.0,
    right: str = "C",
    expiry: str = "20260522",
    entry_spx: float | None = 7460.0,
    spx_stop: float | None = 7456.0,
    status: str = "CLOSED",
    t1_fired: bool = False,
    t2_fired: bool = False,
    t3_fired: bool = False,
) -> LiveOrder:
    """Synthesize a parent LiveOrder row for testing.

    All synthetic execs use 1-share fills so realized $ math is intuitive
    (price * 100 dollars per "lot").
    """
    return LiveOrder(
        submitted_at=submitted_at,
        symbol="SPX",
        strike=strike,
        right=right,
        expiry=expiry,
        action="BUY",
        qty=qty,
        order_type="MKT",
        bracket_role="parent",
        status=status,
        filled_qty=qty,
        avg_fill_price=5.0,
        ib_order_id=ib_order_id,
        dry_run=False,
        engine_version=2,
        exit_strategy="spx_points",
        entry_spx_price=entry_spx,
        spx_target_stop=spx_stop,
        dynamic_stop_level=spx_stop,
        spx_target_t1_fired=t1_fired,
        spx_target_t2_fired=t2_fired,
        spx_target_t3_fired=t3_fired,
        spx_target_t1_qty=int(qty * 0.5),
        spx_target_t2_qty=int(qty * 0.3),
        spx_target_t3_qty=qty - int(qty * 0.5) - int(qty * 0.3),
    )


def _make_exec(
    *,
    exec_id: str,
    timestamp: datetime,
    session_date: str,
    side: str,
    shares: float,
    price: float,
    ib_order_id: int | None,
    strike: float = 7460.0,
    right: str = "C",
    expiry: str = "20260522",
) -> IBKRExecution:
    return IBKRExecution(
        exec_id=exec_id,
        timestamp=timestamp,
        session_date=session_date,
        symbol="SPX",
        strike=strike,
        right=right,
        expiry=expiry,
        side=side,
        shares=shares,
        price=price,
        ib_order_id=ib_order_id,
    )


async def _insert(*rows) -> None:
    async with get_session() as s:
        for r in rows:
            s.add(r)
        await s.commit()


# ---------------------------------------------------------------------------
# Scenario (a): clean pair-off, all three targets fire = winner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fifo_pair_off_winner_t1_t2_t3() -> None:
    """Parent BOT 5 @ $5.00, then SLD 2 @ $6, SLD 2 @ $7, SLD 1 @ $8.
    Realized = (6-5)*2 + (7-5)*2 + (8-5)*1 = 2 + 4 + 3 = $9 (x100 = $900)."""
    session_date = "2026-05-22"
    base_ts = datetime(2026, 5, 22, 14, 0, 0, tzinfo=UTC)
    parent = _make_parent(
        submitted_at=base_ts, ib_order_id=1000, qty=5,
        status="CLOSED",
        t1_fired=True, t2_fired=True, t3_fired=True,
    )
    await _insert(parent)

    await _insert(
        _make_exec(exec_id="e1", timestamp=base_ts, session_date=session_date,
                   side="BOT", shares=5.0, price=5.0, ib_order_id=1000),
        _make_exec(exec_id="e2", timestamp=base_ts + timedelta(minutes=5),
                   session_date=session_date,
                   side="SLD", shares=2.0, price=6.0, ib_order_id=None),
        _make_exec(exec_id="e3", timestamp=base_ts + timedelta(minutes=10),
                   session_date=session_date,
                   side="SLD", shares=2.0, price=7.0, ib_order_id=None),
        _make_exec(exec_id="e4", timestamp=base_ts + timedelta(minutes=15),
                   session_date=session_date,
                   side="SLD", shares=1.0, price=8.0, ib_order_id=None),
    )

    summary = await compute_parent_wl_from_fills(session_date)
    assert summary.wins == 1
    assert summary.losses == 0
    assert summary.flats == 0
    assert summary.open_at_eod == 0
    assert summary.total_realized_dollars == pytest.approx(900.0)
    # Refetch parent to get its persisted id
    async with get_session() as s:
        from sqlmodel import select as _sel
        p = (await s.exec(_sel(LiveOrder).where(LiveOrder.ib_order_id == 1000))).one()
    pid = p.id
    assert pid in summary.by_parent
    assert summary.by_parent[pid].classification == "win"
    assert summary.by_parent[pid].realized_dollars == pytest.approx(900.0)
    assert summary.by_parent[pid].open_shares == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Scenario (b): stop-out before any T fires = loser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fifo_pair_off_loser_stopped_out() -> None:
    """Parent BOT 5 @ $5.00, then SLD 5 @ $3 (stop fired before any T).
    Realized = (3-5)*5 = -10 (x100 = -$1000)."""
    session_date = "2026-05-22"
    base_ts = datetime(2026, 5, 22, 14, 0, 0, tzinfo=UTC)
    parent = _make_parent(
        submitted_at=base_ts, ib_order_id=2000, qty=5,
        status="STOPPED",
        t1_fired=False, t2_fired=False, t3_fired=False,
    )
    await _insert(parent)

    await _insert(
        _make_exec(exec_id="e1", timestamp=base_ts, session_date=session_date,
                   side="BOT", shares=5.0, price=5.0, ib_order_id=2000),
        _make_exec(exec_id="e2", timestamp=base_ts + timedelta(minutes=5),
                   session_date=session_date,
                   side="SLD", shares=5.0, price=3.0, ib_order_id=None),
    )

    summary = await compute_parent_wl_from_fills(session_date)
    assert summary.wins == 0
    assert summary.losses == 1
    assert summary.total_realized_dollars == pytest.approx(-1000.0)
    async with get_session() as s:
        from sqlmodel import select as _sel
        p = (await s.exec(_sel(LiveOrder).where(LiveOrder.ib_order_id == 2000))).one()
    assert summary.by_parent[p.id].classification == "loss"
    assert summary.by_parent[p.id].realized_dollars == pytest.approx(-1000.0)


# ---------------------------------------------------------------------------
# Scenario (c): parent still open at EOD = classified 'open' (no W/L)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fifo_pair_off_open_at_eod() -> None:
    """Parent BOT 5 @ $5.00, NO SLD execs. Still long at EOD.
    Should be classified 'open' — NOT counted as a win OR a loss."""
    session_date = "2026-05-22"
    base_ts = datetime(2026, 5, 22, 14, 0, 0, tzinfo=UTC)
    parent = _make_parent(
        submitted_at=base_ts, ib_order_id=3000, qty=5,
        status="FILLED",  # still open
    )
    await _insert(parent)

    await _insert(
        _make_exec(exec_id="e1", timestamp=base_ts, session_date=session_date,
                   side="BOT", shares=5.0, price=5.0, ib_order_id=3000),
    )

    summary = await compute_parent_wl_from_fills(session_date)
    assert summary.wins == 0
    assert summary.losses == 0
    # No SLD pairings -> realized total is 0 (the open BOT lot isn't a loss).
    assert summary.total_realized_dollars == pytest.approx(0.0)
    assert summary.open_at_eod == 1
    async with get_session() as s:
        from sqlmodel import select as _sel
        p = (await s.exec(_sel(LiveOrder).where(LiveOrder.ib_order_id == 3000))).one()
    assert summary.by_parent[p.id].classification == "open"
    assert summary.by_parent[p.id].open_shares == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Scenario (d): partial-scale-out, realized portion classifies the parent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fifo_pair_off_partial_scaleout_classifies_on_realized() -> None:
    """Parent BOT 5 @ $5.00, then SLD 3 @ $7 (partial win, 2 still open).
    Realized = (7-5)*3 = $6 (x100 = $600). Parent should be classified
    as 'win' on the realized portion (the residual is reported in
    open_shares but doesn't preempt the W/L label)."""
    session_date = "2026-05-22"
    base_ts = datetime(2026, 5, 22, 14, 0, 0, tzinfo=UTC)
    parent = _make_parent(
        submitted_at=base_ts, ib_order_id=4000, qty=5,
        status="PARTIAL",
        t1_fired=True,
    )
    await _insert(parent)

    await _insert(
        _make_exec(exec_id="e1", timestamp=base_ts, session_date=session_date,
                   side="BOT", shares=5.0, price=5.0, ib_order_id=4000),
        _make_exec(exec_id="e2", timestamp=base_ts + timedelta(minutes=5),
                   session_date=session_date,
                   side="SLD", shares=3.0, price=7.0, ib_order_id=None),
    )

    summary = await compute_parent_wl_from_fills(session_date)
    assert summary.wins == 1
    assert summary.losses == 0
    assert summary.total_realized_dollars == pytest.approx(600.0)
    # 2 shares still long => open_at_eod counts this parent too
    assert summary.open_at_eod == 1
    async with get_session() as s:
        from sqlmodel import select as _sel
        p = (await s.exec(_sel(LiveOrder).where(LiveOrder.ib_order_id == 4000))).one()
    assert summary.by_parent[p.id].classification == "win"
    assert summary.by_parent[p.id].realized_dollars == pytest.approx(600.0)
    assert summary.by_parent[p.id].open_shares == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# End-to-end: compute_daily_review uses FIFO, daily_review and daily_session agree
# ---------------------------------------------------------------------------


async def _write_signal_for_session(session_date: str, ts: datetime) -> None:
    """Insert a minimal SignalIter so compute_daily_review doesn't bail."""
    from fef_sidecar.db.repository import SignalIterRepository
    await SignalIterRepository.write_iter(
        spx_price=7460.0,
        direction=FEFDirection.WAIT,
        confidence=FEFConfidence.NONE,
        reasoning="testing fifo",
        drift_cum_net_m=0.0,
        drift_bias="NEUTRAL",
        ntm_max_pain=7400.0,
        raw_max_pain=7000.0,
        top_gex_walls=[],
        top_dex_walls=[],
        session_date=session_date,
    )


@pytest.mark.asyncio
async def test_compute_daily_review_uses_fifo_w_l_from_fills() -> None:
    """daily_review.actual_wins/losses come from FIFO pair-off, agreeing
    with the per-parent classification of compute_parent_wl_from_fills."""
    session_date = "2026-05-22"
    base_ts = datetime(2026, 5, 22, 14, 0, 0, tzinfo=UTC)
    await _write_signal_for_session(session_date, base_ts)

    # Two parents: one winner (BOT 5 @ $5 / SLD 5 @ $7 = +$1000) and one
    # loser (BOT 5 @ $5 / SLD 5 @ $3 = -$1000).
    parent_w = _make_parent(
        submitted_at=base_ts, ib_order_id=10001, qty=5,
        strike=7460.0, status="CLOSED",
        t1_fired=True, t2_fired=True, t3_fired=True,
    )
    parent_l = _make_parent(
        submitted_at=base_ts + timedelta(minutes=1), ib_order_id=10002, qty=5,
        strike=7470.0, status="STOPPED",
    )
    await _insert(parent_w, parent_l)

    await _insert(
        _make_exec(exec_id="e1", timestamp=base_ts, session_date=session_date,
                   side="BOT", shares=5.0, price=5.0, ib_order_id=10001,
                   strike=7460.0),
        _make_exec(exec_id="e2", timestamp=base_ts + timedelta(minutes=5),
                   session_date=session_date,
                   side="SLD", shares=5.0, price=7.0, ib_order_id=None,
                   strike=7460.0),
        _make_exec(exec_id="e3", timestamp=base_ts + timedelta(minutes=1),
                   session_date=session_date,
                   side="BOT", shares=5.0, price=5.0, ib_order_id=10002,
                   strike=7470.0),
        _make_exec(exec_id="e4", timestamp=base_ts + timedelta(minutes=6),
                   session_date=session_date,
                   side="SLD", shares=5.0, price=3.0, ib_order_id=None,
                   strike=7470.0),
    )

    review = await compute_daily_review(session_date)
    assert review.actual_wins == 1
    assert review.actual_losses == 1
    # Net realized = +$1000 - $1000 = $0 -> actual_total_R = 0.0
    assert review.actual_total_R == pytest.approx(0.0)
    assert review.actual_trades_taken == 2

    # daily_session cascade picks up the same numbers.
    from fef_sidecar.db.models import DailySession
    from sqlmodel import select as _sel
    async with get_session() as s:
        ds = (await s.exec(
            _sel(DailySession).where(DailySession.session_date == session_date)
        )).one()
    assert ds.win_count == review.actual_wins
    assert ds.loss_count == review.actual_losses
    assert ds.gross_pnl == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_compute_daily_review_falls_back_to_estimator_when_no_execs() -> None:
    """No ibkr_execution rows for the session -> fall back to
    _estimate_parent_r so dry-run / paper-only sessions still get
    signed W/L counts."""
    session_date = "2026-05-22"
    base_ts = datetime(2026, 5, 22, 14, 0, 0, tzinfo=UTC)
    await _write_signal_for_session(session_date, base_ts)

    # Parent that "won" via the flag estimator: status=CLOSED with all
    # three targets fired -> _estimate_parent_r returns a positive R.
    parent_w = _make_parent(
        submitted_at=base_ts, ib_order_id=20001, qty=5,
        status="CLOSED", t1_fired=True, t2_fired=True, t3_fired=True,
    )
    # Parent that "lost" via the flag estimator: STOPPED, no targets,
    # dyn_stop equals spx_stop = -1R for the whole position.
    parent_l = _make_parent(
        submitted_at=base_ts + timedelta(minutes=1), ib_order_id=20002, qty=5,
        status="STOPPED", entry_spx=7460.0, spx_stop=7456.0,
    )
    await _insert(parent_w, parent_l)

    # Critically: NO IBKRExecution rows for this session.
    review = await compute_daily_review(session_date)
    # Fallback counts the parents based on flag estimator
    assert review.actual_wins == 1
    assert review.actual_losses == 1
    # No execs -> realized dollars stays 0 -> actual_total_R = 0
    assert review.actual_total_R == pytest.approx(0.0)
