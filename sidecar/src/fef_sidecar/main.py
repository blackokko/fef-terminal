"""FastAPI app entrypoint.

Lifecycle (per FastAPI lifespan):
    startup:
        1. init SQLite schema
        2. build AppState (providers, engine, broadcaster)
        3. spawn FEF tick loop + IBKR bar stream as background tasks
    shutdown:
        cancel tasks, disconnect providers, dispose DB engine

Run via `fef-sidecar` console script or `python -m fef_sidecar.main`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from fef_sidecar.api import router as api_router
from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.loop import bar_stream_loop, fef_tick_loop
from fef_sidecar.state import AppState
from fef_sidecar.ws import router as ws_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("fef-sidecar starting up")
    await init_db()

    state = AppState.build()
    app.state.fef = state

    auto_loops = os.environ.get("FEF_DISABLE_AUTO_LOOP", "false").lower() not in (
        "true",
        "1",
        "yes",
        "on",
    )
    if auto_loops:
        state.loop_task = asyncio.create_task(fef_tick_loop(state), name="fef-tick-loop")
        state.bar_task = asyncio.create_task(bar_stream_loop(state), name="bar-stream-loop")

    logger.info(
        "fef-sidecar ready (QD=%s, IBKR=%s, auto_loops=%s)",
        type(state.quantdata).__name__,
        type(state.ibkr).__name__,
        auto_loops,
    )

    try:
        yield
    finally:
        logger.info("fef-sidecar shutting down")
        for task in (state.loop_task, state.bar_task):
            if task is not None:
                task.cancel()  # type: ignore[attr-defined]
                try:
                    await task  # type: ignore[misc]
                except (asyncio.CancelledError, Exception):
                    pass

        try:
            await state.ibkr.disconnect()
        except Exception:
            logger.exception("Failed to disconnect IBKR cleanly")

        try:
            await state.quantdata.aclose()
        except Exception:
            logger.exception("Failed to close QuantData client cleanly")

        await dispose_db()
        logger.info("fef-sidecar shutdown complete")


app = FastAPI(title="fef-sidecar", lifespan=lifespan)
app.include_router(api_router)
app.include_router(ws_router)


def run() -> None:
    uvicorn.run(
        "fef_sidecar.main:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
