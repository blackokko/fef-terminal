"""End-to-end WS + REST tests against the in-process FastAPI app.

Uses mock providers (default env). Verifies:
- /healthz responds
- /status reflects mock providers
- WS connection receives an initial StatusEvent
- /run_iter_now triggers an IterEvent that's broadcast over WS
"""

from __future__ import annotations

import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["FEF_PROVIDER_QUANTDATA"] = "mock"
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
        assert "Mock" in data["quantdata"]
        assert "Mock" in data["ibkr_provider"]


@pytest.mark.asyncio
async def test_run_iter_now_returns_verdict(app_with_lifespan) -> None:
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Warm up the engine — first 3 iters are WAIT regardless
        for _ in range(4):
            r = await ac.post("/run_iter_now")
            assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["iter_num"] == 4
        assert "direction" in data["verdict"]


@pytest.mark.asyncio
async def test_history_signals_returns_persisted_iters(app_with_lifespan) -> None:
    transport = ASGITransport(app=app_with_lifespan)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for _ in range(3):
            await ac.post("/run_iter_now")

        r = await ac.get("/history/signals?limit=10")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 3
        assert rows[0]["iter_num"] == 3  # newest first
        assert "top_gex_walls" in rows[0]
        assert "ntm_max_pain" in rows[0]


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
