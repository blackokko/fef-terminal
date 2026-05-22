"""Tests for ML pattern discovery — R-multiple regression via TimeSeriesSplit."""

from __future__ import annotations

import os
import random
from datetime import datetime, timedelta

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
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import SignalIter


@pytest.fixture(autouse=True)
async def _db_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("FEF_MODEL_DIR", str(tmp_path / "models"))
    _iter_counter["n"] = 0
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


_iter_counter = {"n": 0}


async def _seed_signal_and_ticket(
    *,
    timestamp: datetime,
    drift_cum_net_m: float,
    pnl_dollars: float,
    entry_limit: float = 4.0,
    stop: float = 2.0,
    qty: int = 10,
    direction: FEFDirection = FEFDirection.LONG_CALLS,
) -> None:
    """Insert one SignalIter + linked closed TradeTicket with a controlled timestamp."""
    _iter_counter["n"] += 1
    async with get_session() as session:
        sig = SignalIter(
            session_date=timestamp.strftime("%Y-%m-%d"),
            iter_num=_iter_counter["n"],
            timestamp=timestamp,
            spx_price=7400.0,
            direction=direction,
            confidence=FEFConfidence.MEDIUM,
            reasoning="seed",
            drift_cum_net_m=drift_cum_net_m,
            drift_bias="BULLISH",
            ntm_max_pain=7395.0,
            raw_max_pain=7300.0,
            top_gex_walls=[{"strike": 7410.0, "net_m": 50.0, "side": "CALL"}],
            top_dex_walls=[{"strike": 7390.0, "net_m": 100.0, "side": "PUT"}],
            aggression_call_pct=55.0,
            aggression_put_pct=45.0,
            iv_rank_call_pct=60.0,
            iv_rank_put_pct=40.0,
            extras={"fe_ratio": 3.0, "delta_price": 2.0, "delta_drift_m": 5.0},
        )
        session.add(sig)
        await session.flush()
        ticket = TradeTicket(
            signal_iter_id=sig.id,
            direction=direction,
            contract_strike=7400.0,
            contract_expiry=timestamp.strftime("%Y-%m-%d"),
            qty=qty,
            entry_limit=entry_limit,
            t1=entry_limit * 1.5,
            t2=entry_limit * 2.0,
            t3=entry_limit * 2.5,
            stop=stop,
            status=TicketStatus.CLOSED,
            pnl_dollars=pnl_dollars,
        )
        session.add(ticket)


@pytest.mark.asyncio
async def test_train_under_min_rows_returns_zero_folds() -> None:
    from fef_sidecar.engine.ml_patterns import train_model

    base = datetime(2026, 5, 1, 10, 0, 0)
    for i in range(5):
        await _seed_signal_and_ticket(
            timestamp=base + timedelta(minutes=i),
            drift_cum_net_m=10.0 + i,
            pnl_dollars=100.0,
        )
    result = await train_model(min_rows=30)
    assert result is not None
    assert result.n_training_rows == 5
    assert result.cv_n_folds == 0
    assert result.model_path is None  # nothing persisted under min_rows


@pytest.mark.asyncio
async def test_train_with_enough_rows_returns_cv_metrics_and_persists_model() -> None:
    from fef_sidecar.engine.ml_patterns import train_model

    base = datetime(2026, 5, 1, 10, 0, 0)
    # Seed 40 rows where pnl is a noisy function of drift — model should
    # learn that drift_cum_net_m is the most important feature.
    random.seed(7)
    for i in range(40):
        drift = float(i)  # monotonic feature
        # risk per ticket = qty * (entry_limit - stop) * 100 = 10 * 2 * 100 = $2000
        # so y (R) = pnl / 2000
        pnl = 50.0 * drift + random.gauss(0, 100.0)
        await _seed_signal_and_ticket(
            timestamp=base + timedelta(minutes=i),
            drift_cum_net_m=drift,
            pnl_dollars=pnl,
        )

    result = await train_model(min_rows=30, n_splits=5)
    assert result is not None
    assert result.n_training_rows == 40
    assert result.n_features > 0
    assert result.cv_n_folds >= 2
    assert result.cv_mae is not None
    # Model should have learned *something* — MAE shouldn't be wildly bad.
    assert result.cv_mae < 5.0
    assert result.model_path is not None
    # Model file actually exists and loads back.
    import joblib
    payload = joblib.load(result.model_path)
    assert "estimators" in payload and "feature_names" in payload
    assert "global" in payload["estimators"]
    # drift_cum_net_m should be among the top features
    top_names = [f["name"] for f in result.feature_importances[:5]]
    assert "drift_cum_net_m" in top_names
    # bucket_row_counts should be populated
    assert result.bucket_row_counts is not None
    assert result.bucket_row_counts.get("global") == 40


@pytest.mark.asyncio
async def test_train_and_persist_writes_mlmodelrun_row() -> None:
    from fef_sidecar.engine.ml_patterns import train_and_persist

    base = datetime(2026, 5, 1, 10, 0, 0)
    for i in range(35):
        await _seed_signal_and_ticket(
            timestamp=base + timedelta(minutes=i),
            drift_cum_net_m=float(i),
            pnl_dollars=10.0 * i,
        )

    out = await train_and_persist()
    assert out["ok"] is True
    assert out["label_kind"] == "r_multiple"
    assert out["n_training_rows"] == 35
    assert out["model_path"] is not None

    runs = await SignalIterRepository.query_ml_model_runs(limit=5)
    assert len(runs) == 1
    assert runs[0].label_kind == "r_multiple"
    assert runs[0].cv_mae is not None
    assert runs[0].model_path == out["model_path"]


@pytest.mark.asyncio
async def test_degenerate_risk_tickets_are_skipped() -> None:
    """Tickets with entry_limit <= stop produce nonsensical R; should be filtered."""
    from fef_sidecar.engine.ml_patterns import _load_training_data

    base = datetime(2026, 5, 1, 10, 0, 0)
    # 3 good tickets
    for i in range(3):
        await _seed_signal_and_ticket(
            timestamp=base + timedelta(minutes=i),
            drift_cum_net_m=float(i),
            pnl_dollars=100.0,
        )
    # 1 bad ticket: stop > entry_limit
    await _seed_signal_and_ticket(
        timestamp=base + timedelta(minutes=99),
        drift_cum_net_m=99.0,
        pnl_dollars=100.0,
        entry_limit=2.0,
        stop=4.0,
    )

    X, y, _names, ts = await _load_training_data()
    assert len(X) == 3
    assert len(y) == 3
    # And the rows are in chronological order
    assert ts == sorted(ts)
