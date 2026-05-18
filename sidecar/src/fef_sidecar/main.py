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


async def backfill_regime_from_db(state: AppState) -> None:
    """Reconstruct RegimeSamples from today's SignalIter rows and seed the
    regime detector. Lets the regime panel show a real assessment on app
    load instead of waiting through warmup iters."""
    from datetime import datetime, timezone
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import SignalIter
    from fef_sidecar.engine.regime import RegimeSample
    from sqlmodel import select

    def _today_et() -> str:
        et = timezone(__import__("datetime").timedelta(hours=-4))
        return datetime.now(et).strftime("%Y-%m-%d")

    try:
        date = _today_et()
        async with get_session() as session:
            stmt = (
                select(SignalIter)
                .where(SignalIter.session_date == date)
                .order_by(SignalIter.id.desc())  # type: ignore[attr-defined]
                .limit(state.regime_detector._window_size)
            )
            rows = list((await session.exec(stmt)).all())
        rows.reverse()  # oldest -> newest
        samples: list[RegimeSample] = []
        for r in rows:
            extras = r.extras or {}
            ts = r.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            et = ts.astimezone(timezone(__import__("datetime").timedelta(hours=-4)))
            mins = (et.hour - 9) * 60 + (et.minute - 30)
            samples.append(RegimeSample(
                spx_price=r.spx_price,
                drift_cum_net_m=r.drift_cum_net_m,
                drift_bias=r.drift_bias,
                wall_proximity_pts=extras.get("nearest_wall_distance"),
                aligned=bool(extras.get("aligned", False)),
                absorption=bool(extras.get("absorption", False)),
                direction=r.direction.value if hasattr(r.direction, "value") else str(r.direction),
                confidence=r.confidence.value if hasattr(r.confidence, "value") else str(r.confidence),
                fe_ratio=extras.get("fe_ratio"),
                minutes_since_open=max(0, mins),
                vix=None,
            ))
        state.regime_detector.backfill(samples)
        logger.info("Regime detector backfilled with %d samples from DB", len(samples))
    except Exception:
        logger.exception("backfill_regime_from_db failed")


async def refresh_prior_close(state: AppState) -> None:
    """Fetch prior-day close for the active symbol and cache it on state.
    Best-effort: silently leaves None on failure (panel falls back to no %).
    """
    from fef_sidecar.ibkr import Contract
    symbol = state.active_symbol
    try:
        await state.ibkr.connect()
        pc = await state.ibkr.get_prior_close(Contract.for_symbol(symbol))
        if pc is not None and pc > 0:
            state.prior_close_by_symbol[symbol] = pc
            logger.info("Prior close for %s: %.2f", symbol, pc)
    except Exception:
        logger.exception("refresh_prior_close failed for %s", symbol)


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
        asyncio.create_task(refresh_prior_close(state), name="refresh-prior-close")
        asyncio.create_task(backfill_regime_from_db(state), name="backfill-regime")

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
