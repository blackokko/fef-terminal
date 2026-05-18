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
from fastapi.middleware.cors import CORSMiddleware

from fef_sidecar.api import router as api_router
from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.loop import (
    account_poll_loop,
    bar_stream_loop,
    fef_tick_loop,
    hft_loop,
    ibkr_health_check_loop,
    microstructure_loop,
    quantdata_poll_loop,
    tick_stream_loop,
    vix_poll_loop,
)
from fef_sidecar.state import AppState
from fef_sidecar.ws import router as ws_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _seed_defaults_on_first_run() -> None:
    """On startup, ensure the DB has the built-in patterns + presets. Idempotent."""
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import Pattern, StrategyPreset
    from fef_sidecar.engine.patterns import DEFAULT_TEMPLATES
    from fef_sidecar.engine.preset_templates import DEFAULT_PRESETS

    from datetime import UTC, datetime

    async with get_session() as session:
        pattern_count = len((await session.exec(select(Pattern))).all())
        preset_count = len((await session.exec(select(StrategyPreset))).all())

        if pattern_count == 0:
            for tpl in DEFAULT_TEMPLATES:
                session.add(Pattern(
                    name=tpl["name"],
                    description=tpl["description"],
                    conditions=tpl["conditions"],
                    enabled=True,
                ))
            logger.info("Seeded %d default patterns", len(DEFAULT_TEMPLATES))

        if preset_count == 0:
            for tpl in DEFAULT_PRESETS:
                session.add(StrategyPreset(
                    name=tpl["name"],
                    description=tpl["description"],
                    fef_loop_interval_sec=tpl["fef_loop_interval_sec"],
                    wall_proximity_pts=tpl["wall_proximity_pts"],
                    wall_collapse_pct=tpl["wall_collapse_pct"],
                    wall_collapse_floor_m=tpl["wall_collapse_floor_m"],
                    absorption_fe_ratio_min=tpl["absorption_fe_ratio_min"],
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                ))
            logger.info("Seeded %d default presets", len(DEFAULT_PRESETS))


def spawn_data_loops(state: AppState) -> None:
    """(Re-)spawn the per-symbol data loops. Called on startup and whenever
    the active symbol is changed via /config/symbol so the loops pick up
    the new symbol immediately."""
    state.quantdata_poll_task = asyncio.create_task(
        quantdata_poll_loop(state), name="quantdata-poll-loop"
    )
    state.loop_task = asyncio.create_task(fef_tick_loop(state), name="fef-tick-loop")
    state.bar_task = asyncio.create_task(bar_stream_loop(state), name="bar-stream-loop")
    state.tick_task = asyncio.create_task(tick_stream_loop(state), name="tick-stream-loop")
    state.health_check_task = asyncio.create_task(
        ibkr_health_check_loop(state), name="ibkr-health-check-loop"
    )
    state.vix_task = asyncio.create_task(vix_poll_loop(state), name="vix-poll-loop")
    state.account_task = asyncio.create_task(account_poll_loop(state), name="account-poll-loop")
    state.microstructure_task = asyncio.create_task(
        microstructure_loop(state), name="microstructure-loop"
    )
    state.hft_task = asyncio.create_task(hft_loop(state), name="hft-loop")
    from fef_sidecar.engine.position_monitor import position_monitor_loop
    state.position_task = asyncio.create_task(
        position_monitor_loop(state), name="position-monitor-loop"
    )
    from fef_sidecar.engine.auto_walk_forward import nightly_scheduler
    state.auto_wf_task = asyncio.create_task(
        nightly_scheduler(state), name="auto-walk-forward-scheduler"
    )
    from fef_sidecar.engine.runner_protector import runner_protector_loop
    state.runner_protect_task = asyncio.create_task(
        runner_protector_loop(state), name="runner-protector-loop"
    )
    from fef_sidecar.engine.spx_target_watcher import spx_target_watcher_loop
    state.spx_target_task = asyncio.create_task(
        spx_target_watcher_loop(state), name="spx-target-watcher-loop"
    )
    from fef_sidecar.engine.spread_exit_watcher import spread_exit_watcher_loop
    state.spread_exit_task = asyncio.create_task(
        spread_exit_watcher_loop(state), name="spread-exit-watcher-loop"
    )


async def cancel_data_loops(state: AppState) -> None:
    """Cancel the per-symbol data loops. Awaits each to ensure clean
    teardown before respawning."""
    tasks = [
        state.loop_task, state.bar_task, state.tick_task,
        state.quantdata_poll_task, state.health_check_task,
        state.vix_task, state.account_task, state.microstructure_task,
        state.hft_task,
    ]
    for t in tasks:
        if t is not None:
            t.cancel()  # type: ignore[attr-defined]
    for t in tasks:
        if t is not None:
            try:
                await t  # type: ignore[misc]
            except (asyncio.CancelledError, Exception):
                pass
    state.loop_task = state.bar_task = state.tick_task = None
    state.quantdata_poll_task = state.health_check_task = None
    state.vix_task = state.account_task = state.microstructure_task = None
    state.hft_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("fef-sidecar starting up")
    await init_db()
    await _seed_defaults_on_first_run()
    from fef_sidecar.engine.calendar import seed_calendar_if_empty
    await seed_calendar_if_empty()

    state = AppState.build()
    app.state.fef = state

    # Live QuantDataClient needs its httpx session initialized via __aenter__.
    # Mock is a no-op. Done here so all loops can call set_page_filter/fetch
    # without each one wrapping in 'async with'.
    if hasattr(state.quantdata, "__aenter__"):
        try:
            await state.quantdata.__aenter__()
        except Exception:
            logger.exception("QuantData __aenter__ failed — calls will error")

    auto_loops = os.environ.get("FEF_DISABLE_AUTO_LOOP", "false").lower() not in (
        "true",
        "1",
        "yes",
        "on",
    )
    if auto_loops:
        spawn_data_loops(state)

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
        for task in (
            state.loop_task,
            state.bar_task,
            state.tick_task,
            state.quantdata_poll_task,
            state.health_check_task,
            state.vix_task,
            state.account_task,
            state.microstructure_task,
            state.hft_task,
        ):
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
app.add_middleware(
    CORSMiddleware,
    # Sidecar binds 127.0.0.1 only — no risk from external origins. Wildcard
    # covers every Tauri scheme/host combination across Windows/macOS/Linux,
    # dev mode, release mode, and any future origin changes from Tauri.
    allow_origins=["*"],
    allow_credentials=False,  # can't be True with wildcard origin
    allow_methods=["*"],
    allow_headers=["*"],
)
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
