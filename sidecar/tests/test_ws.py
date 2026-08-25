"""End-to-end WS + REST tests against the in-process FastAPI app.

Uses mock providers (default env). Verifies:
- /healthz responds
- /status reflects the mock provider
- WS connection receives an initial StatusEvent
- /history/signals serves persisted SignalIter rows

[REMOVED 2026-08-25] QuantData subscription CANCELLED.
  * test_run_iter_now_returns_verdict is DELETED. Its whole subject was
    POST /run_iter_now, i.e. "fetch a fresh QuantData snapshot and run the FEF
    engine on it now" — endpoint, fetch and engine call are all gone.
  * test_status_reports_mock_providers no longer asserts on data["quantdata"]
    (the key is gone from /status); it still pins the IBKR provider.
  * test_history_signals_returns_persisted_iters no longer creates its rows by
    driving a FEF iter, and no longer asserts on the QuantData-sourced columns
    (top_gex_walls / ntm_max_pain) that /history/signals stopped serving. It
    writes rows through SignalIterRepository directly — which is exactly how
    the surviving writer (engine/pattern_trigger.py) produces them — so the
    ENDPOINT stays covered.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["FEF_PROVIDER_IBKR"] = "mock"
# Disable background loops in tests — we drive iters manually via REST.
os.environ["FEF_DISABLE_AUTO_LOOP"] = "true"


@pytest.fixture
async def app_with_lifespan():
    from fef_sidecar.main import app

    async with app.router.lifespan_context(app):
        yield app


@pytest.mark.asyncio
async def test_healthz(app_with_lifespan) -> None:
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_status_reports_mock_providers(app_with_lifespan) -> None:
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/status")
        assert r.status_code == 200
        data = r.json()
        assert "Mock" in data["ibkr_provider"]
        assert data["all_mock"] is True


@pytest.mark.asyncio
async def test_history_signals_returns_persisted_iters(app_with_lifespan) -> None:
    from fef_sidecar.db import SignalIterRepository
    from fef_sidecar.db.models import FEFConfidence, FEFDirection

    for i in range(3):
        await SignalIterRepository.write_iter(
            spx_price=7400.0 + i,
            direction=FEFDirection.WAIT,
            confidence=FEFConfidence.NONE,
            reasoning=f"row {i}",
            drift_cum_net_m=0.0,
            drift_bias="NEUTRAL",
            ntm_max_pain=0.0,
            raw_max_pain=0.0,
            top_gex_walls=[],
            top_dex_walls=[],
        )

    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/history/signals?limit=10")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 3
        assert rows[0]["iter_num"] == 3  # newest first
        assert rows[0]["spx_price"] == 7402.0
        assert "extras" in rows[0]
        # QuantData-sourced columns are no longer served (2026-08-25).
        assert "top_gex_walls" not in rows[0]
        assert "ntm_max_pain" not in rows[0]


@pytest.mark.asyncio
async def test_broadcaster_fan_out_to_subscribers() -> None:
    """Direct test of EventBroadcaster without going through WS — keeps it fast."""
    from fef_sidecar.events import EventBroadcaster, TickEvent

    bc = EventBroadcaster(queue_maxsize=4)

    async def consume(label: str) -> list:
        received: list = []
        async with bc.subscribe() as q:
            for _ in range(3):
                event = await asyncio.wait_for(q.get(), timeout=1.0)
                received.append((label, event.type))
        return received

    async def produce() -> None:
        await asyncio.sleep(0.05)
        for i in range(3):
            await bc.publish(TickEvent(spx_price=7410.0 + i))
            await asyncio.sleep(0.01)

    a_task = asyncio.create_task(consume("a"))
    b_task = asyncio.create_task(consume("b"))
    await produce()
    a_received = await a_task
    b_received = await b_task

    assert len(a_received) == 3
    assert len(b_received) == 3
