"""Tests for ML-driven pattern proposal scanning + inference service."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.db import (
    FEFConfidence,
    FEFDirection,
    TicketStatus,
    TradeTicket,
    dispose_db,
    init_db,
)
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import SignalIter


_iter_counter = {"n": 0}


@pytest.fixture(autouse=True)
async def _db_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("FEF_MODEL_DIR", str(tmp_path / "models"))
    _iter_counter["n"] = 0
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


async def _seed(
    *,
    timestamp: datetime,
    drift_cum_net_m: float,
    pnl_dollars: float,
    entry_limit: float = 4.0,
    stop: float = 2.0,
    qty: int = 10,
    direction: FEFDirection = FEFDirection.LONG_CALLS,
) -> None:
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
async def test_propose_patterns_finds_strong_threshold_rule() -> None:
    """Seed data where high drift = wins, low drift = losses; proposal scan
    should surface drift_cum_net_m as a candidate threshold rule."""
    from fef_sidecar.engine.pattern_proposals import propose_patterns

    base = datetime(2026, 5, 1, 10, 0, 0)
    # 25 rows with drift < 5 → losses (-$500)
    # 25 rows with drift > 15 → big wins (+$3000)
    for i in range(25):
        await _seed(timestamp=base + timedelta(minutes=i), drift_cum_net_m=float(i % 5), pnl_dollars=-500.0)
    for i in range(25):
        await _seed(timestamp=base + timedelta(minutes=100 + i), drift_cum_net_m=15.0 + (i % 5), pnl_dollars=3000.0)

    proposals = await propose_patterns(top_k=10)
    assert len(proposals) > 0
    # The strongest single-condition rule should involve drift_cum_net_m with operator >=
    top = proposals[0]
    assert top["expected_R"] > 0.10
    assert top["r_lift_vs_baseline"] > 0.10
    assert top["n_match"] >= 12
    # At least one of the top proposals should be drift_cum_net_m >= X
    features_seen = {p["feature"] for p in proposals[:5]}
    assert "drift_cum_net_m" in features_seen


@pytest.mark.asyncio
async def test_propose_patterns_returns_empty_when_no_lift() -> None:
    """If all trades have similar outcomes, no rule should clear the lift bar."""
    from fef_sidecar.engine.pattern_proposals import propose_patterns

    base = datetime(2026, 5, 1, 10, 0, 0)
    for i in range(40):
        # All pnl values ~zero — no feature distinguishes outcomes
        await _seed(timestamp=base + timedelta(minutes=i), drift_cum_net_m=float(i), pnl_dollars=0.0)

    proposals = await propose_patterns(top_k=10)
    assert proposals == []


@pytest.mark.asyncio
async def test_propose_patterns_under_min_data_returns_empty() -> None:
    from fef_sidecar.engine.pattern_proposals import propose_patterns

    base = datetime(2026, 5, 1, 10, 0, 0)
    for i in range(5):
        await _seed(timestamp=base + timedelta(minutes=i), drift_cum_net_m=float(i), pnl_dollars=500.0)
    proposals = await propose_patterns(top_k=10)
    assert proposals == []


@pytest.mark.asyncio
async def test_ml_inference_loads_persisted_model_and_scores() -> None:
    """After train_and_persist, inference service should load the joblib
    and return a numeric prediction for a live signal payload."""
    from fef_sidecar.engine.ml_inference import MLInferenceService
    from fef_sidecar.engine.ml_patterns import train_and_persist

    base = datetime(2026, 5, 1, 10, 0, 0)
    for i in range(40):
        await _seed(
            timestamp=base + timedelta(minutes=i),
            drift_cum_net_m=float(i),
            pnl_dollars=50.0 * float(i),
        )

    out = await train_and_persist()
    assert out["ok"]
    assert out["model_path"] is not None

    svc = await MLInferenceService.from_latest_run()
    assert svc.is_ready()

    pred = svc.score(
        {
            "direction": "LONG_CALLS",
            "confidence": "MEDIUM",
            "spx_price": 7400.0,
            "ntm_max_pain": 7395.0,
            "drift_cum_net_m": 20.0,
            "aggression_call_pct": 60.0,
            "aggression_put_pct": 40.0,
            "iv_rank_call_pct": 55.0,
            "iv_rank_put_pct": 45.0,
            "top_gex_walls": [{"strike": 7410.0, "net_m": 50.0}],
            "top_dex_walls": [{"strike": 7390.0, "net_m": 100.0}],
            "extras": {"fe_ratio": 3.0, "delta_price": 2.0, "delta_drift_m": 5.0},
        },
        hour_et=10,
    )
    assert pred is not None
    assert isinstance(pred, float)


@pytest.mark.asyncio
async def test_ml_inference_wait_direction_returns_none() -> None:
    """WAIT signals never get scored — feature extractor returns None."""
    from fef_sidecar.engine.ml_inference import MLInferenceService
    from fef_sidecar.engine.ml_patterns import train_and_persist

    base = datetime(2026, 5, 1, 10, 0, 0)
    for i in range(40):
        await _seed(
            timestamp=base + timedelta(minutes=i),
            drift_cum_net_m=float(i),
            pnl_dollars=50.0 * float(i),
        )
    await train_and_persist()
    svc = await MLInferenceService.from_latest_run()
    pred = svc.score(
        {"direction": "WAIT", "spx_price": 7400.0, "ntm_max_pain": 7395.0,
         "drift_cum_net_m": 10.0, "extras": {}},
        hour_et=10,
    )
    assert pred is None


@pytest.mark.asyncio
async def test_ml_inference_no_model_returns_none() -> None:
    from fef_sidecar.engine.ml_inference import MLInferenceService

    svc = await MLInferenceService.from_latest_run()
    assert not svc.is_ready()
    pred = svc.score(
        {"direction": "LONG_CALLS", "spx_price": 7400.0, "ntm_max_pain": 7395.0, "extras": {}},
        hour_et=10,
    )
    assert pred is None
