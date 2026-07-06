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
    from fef_sidecar.db.models import AutoTradePreset, Pattern, StrategyPreset
    from fef_sidecar.engine.patterns import DEFAULT_TEMPLATES
    from fef_sidecar.engine.preset_templates import DEFAULT_PRESETS

    from datetime import UTC, datetime

    async with get_session() as session:
        pattern_count = len((await session.exec(select(Pattern))).all())
        preset_count = len((await session.exec(select(StrategyPreset))).all())
        at_preset_count = len((await session.exec(select(AutoTradePreset))).all())

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

        if at_preset_count == 0:
            # Built-in auto-trade presets — lets user swap settings with one click
            # instead of re-entering confidence/cool-down/triggers/qty/runner-protect
            # every session. Definitions duplicated from api.py to avoid circular
            # import on startup.
            defaults = [
                ("Conservative", "HIGH conviction only, longer cool-down, confluence trigger — for choppy or uncertain sessions",
                 "HIGH", 600, ["confluence"], 10, True),
                ("Standard", "HIGH floor, 5-min cool-down, confluence + aligned triggers, FEF auto-qty by confidence",
                 "HIGH", 300, ["confluence", "aligned_only"], 0, True),
                ("Aggressive", "MEDIUM floor, 3-min cool-down, all triggers, larger size — trending high-confidence days",
                 "MEDIUM", 180, ["confluence", "aligned_only"], 15, False),
                ("Pattern signals v1",
                 "Chart-pattern trigger: flag breakouts + engulfing (per pattern_trade_kinds config), "
                 "1m/5m confirmed bars, qty 3, 5-min cool-down. Apply to ARM pattern auto-trading; "
                 "FEF flow triggers are NOT enabled by this preset.",
                 "LOW", 300, ["flag_bull", "flag_bear", "pattern_bull", "pattern_bear"], 3, True),
            ]
            for name, desc, floor, cool, triggers, qty, runner in defaults:
                session.add(AutoTradePreset(
                    name=name, description=desc,
                    confidence_floor=floor, cool_down_sec=cool,
                    trigger_types=list(triggers), qty_override=qty,
                    runner_protect_enabled=runner,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                ))
            logger.info("Seeded %d default auto-trade presets", len(defaults))


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


async def daily_session_audit_runner(state: AppState) -> None:
    """Daily loop that computes + persists the DailyReview row at session
    close. Runs at FEF_AUDIT_RUN_HOUR (default 15 = 3pm CT, which equals
    4pm ET = market close). Survives missed days via startup catch-up
    (see `_startup_audit_catchup`).

    Why this matters: DailyReview rows drive the freeze counter
    (`engine_freeze_state`) + the per-session baselines the user reviews
    each morning. Forgetting to hit `/diag/signal_execution_audit`
    manually = freeze counter stalls and baselines aren't captured.

    America/Chicago zoneinfo so DST handles itself. After market close
    we don't care about post-market movement — by 3pm CT all 0DTEs are
    expired and IBKR fills for the session are settled.
    """
    import os
    from datetime import datetime, timedelta
    try:
        from zoneinfo import ZoneInfo
        chi = ZoneInfo("America/Chicago")
    except Exception:
        from datetime import timezone
        chi = timezone(timedelta(hours=-6))

    hour = int(os.environ.get("FEF_AUDIT_RUN_HOUR", "15"))
    minute = int(os.environ.get("FEF_AUDIT_RUN_MINUTE", "5"))  # 3:05pm CT — let last fills settle
    logger.info("Daily session-audit runner scheduled for %d:%02d CT", hour, minute)

    while True:
        try:
            now = datetime.now(chi)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            sleep_sec = (target - now).total_seconds()
            await asyncio.sleep(sleep_sec)
            await _run_session_audit(state)
            logger.info("Daily session audit fired at %s", datetime.now(chi).strftime("%I:%M%p CT"))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("daily_session_audit_runner iteration failed")
            await asyncio.sleep(60)


async def _run_session_audit(state: AppState) -> None:
    """Compute + persist DailyReview for today's session_date (ET)."""
    from datetime import datetime, timedelta, timezone
    from fef_sidecar.engine.daily_review import compute_daily_review
    today_et = datetime.now(timezone(timedelta(hours=-4))).strftime("%Y-%m-%d")
    try:
        review = await compute_daily_review(today_et)
        logger.info(
            "DailyReview persisted for %s: actionable=%s wins=%s losses=%s expectancy_r=%.3f",
            today_et,
            review.actionable_signals,
            review.wins,
            review.losses,
            review.expectancy_r or 0.0,
        )
    except Exception:
        logger.exception("compute_daily_review failed for %s", today_et)


async def _startup_audit_catchup(state: AppState) -> None:
    """On startup, if the most recent DailyReview is older than yesterday
    AND it's already past today's audit time, run the audit now. Covers
    the "user closed the app before 3pm CT and reopens the next morning"
    case so missed days still get a review row.
    """
    import os
    from datetime import datetime, timedelta, timezone
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import DailyReview
    from sqlmodel import select as _s

    # Wait a few seconds for IBKR + DB to be fully up
    await asyncio.sleep(5)

    try:
        from zoneinfo import ZoneInfo
        chi = ZoneInfo("America/Chicago")
    except Exception:
        chi = timezone(timedelta(hours=-6))

    hour = int(os.environ.get("FEF_AUDIT_RUN_HOUR", "15"))
    minute = int(os.environ.get("FEF_AUDIT_RUN_MINUTE", "5"))
    now_chi = datetime.now(chi)
    today_et = datetime.now(timezone(timedelta(hours=-4))).strftime("%Y-%m-%d")
    past_run_time_today = (now_chi.hour, now_chi.minute) >= (hour, minute)

    async with _gs() as session:
        existing = (await session.exec(
            _s(DailyReview)
            .where(DailyReview.session_date == today_et)  # type: ignore[attr-defined]
        )).first()

    if existing is None and past_run_time_today:
        logger.info("Startup audit catch-up: missed today's 3:05pm CT run — computing now")
        await _run_session_audit(state)


async def _load_ml_inference(state: AppState) -> None:
    """Best-effort startup load of the most recent persisted ML estimator.

    Quiet on failure — system runs fine without an ML model; the
    `ml_expected_r` overlay just stays None.
    """
    try:
        from fef_sidecar.engine.ml_inference import MLInferenceService
        svc = await MLInferenceService.from_latest_run()
        state.ml_inference = svc
        if svc.is_ready():
            buckets = list((svc.estimators or {}).keys())
            logger.info("ML inference loaded from %s (buckets=%s)", svc.model_path, buckets)
        else:
            logger.info("ML inference: no persisted model yet — overlay disabled")
    except Exception:
        logger.exception("ML inference startup load failed")


async def daily_prior_close_refresher(state: AppState) -> None:
    """Daily-at-7pm-CT loop that re-fetches prior_close for the active symbol.

    For futures (ES/NQ/RTY), this picks up the new session's prior close
    right after the daily break + reopen. For cash equities, harmless —
    SPX doesn't trade overnight so prior_close stays the same anyway.

    Time configurable via FEF_PRIOR_CLOSE_REFRESH_HOUR (default 19 = 7pm CT).
    Uses America/Chicago zoneinfo so DST is handled correctly.
    """
    import os
    from datetime import datetime, timedelta
    try:
        from zoneinfo import ZoneInfo
        chi = ZoneInfo("America/Chicago")
    except Exception:
        from datetime import timezone
        chi = timezone(timedelta(hours=-6))  # fallback fixed offset

    hour = int(os.environ.get("FEF_PRIOR_CLOSE_REFRESH_HOUR", "19"))
    logger.info("Daily prior-close refresher scheduled for %d:00 CT", hour)

    while True:
        try:
            now = datetime.now(chi)
            target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            sleep_sec = (target - now).total_seconds()
            await asyncio.sleep(sleep_sec)
            await refresh_prior_close(state)
            logger.info("Daily prior-close refresh fired at %s", datetime.now(chi).strftime("%I:%M%p CT"))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("daily_prior_close_refresher iteration failed")
            await asyncio.sleep(60)  # back off a minute before retrying


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
    from fef_sidecar.engine.spx_target_watcher import (
        spx_stop_tick_watcher_loop,
        spx_target_watcher_loop,
    )
    state.spx_target_task = asyncio.create_task(
        spx_target_watcher_loop(state), name="spx-target-watcher-loop"
    )
    state.spx_stop_tick_task = asyncio.create_task(
        spx_stop_tick_watcher_loop(state), name="spx-stop-tick-watcher"
    )
    from fef_sidecar.engine.spread_exit_watcher import spread_exit_watcher_loop
    state.spread_exit_task = asyncio.create_task(
        spread_exit_watcher_loop(state), name="spread-exit-watcher-loop"
    )
    from fef_sidecar.engine.multi_symbol_scanner import multi_symbol_scanner_loop
    state.scanner_task = asyncio.create_task(
        multi_symbol_scanner_loop(state), name="multi-symbol-scanner-loop"
    )
    # Option chain capture — snapshots ATM±25pt × {C,P} via IBKR every 2 min
    # so shadow variants can replay alternate-strike outcomes. Isolated task:
    # failure won't affect signal/auto-trade loops. Disable via
    # FEF_CHAIN_CAPTURE_ENABLED=false.
    from fef_sidecar.engine.option_chain_capture import option_chain_capture_loop
    state.option_chain_capture_task = asyncio.create_task(
        option_chain_capture_loop(state), name="option-chain-capture-loop"
    )
    # ES session-anchored VWAP capture (direction/trend anchor for the post-FEF
    # strategy). Independent best-effort task; gated by FEF_CAPTURE_ENABLED.
    from fef_sidecar.loop import es_vwap_loop
    state.es_vwap_task = asyncio.create_task(
        es_vwap_loop(state), name="es-vwap-loop"
    )
    # Pattern trigger — confirmed-bar candlestick/flag fires into the
    # advisory audit surface; auto-trades ONLY when the user arms the
    # pattern trigger types via a preset (inert by default). Isolated task.
    from fef_sidecar.engine.pattern_trigger import pattern_trigger_loop
    state.pattern_trigger_task = asyncio.create_task(
        pattern_trigger_loop(state), name="pattern-trigger-loop"
    )


async def cancel_data_loops(state: AppState) -> None:
    """Cancel the per-symbol data loops. Awaits each to ensure clean
    teardown before respawning."""
    tasks = [
        state.loop_task, state.bar_task, state.tick_task,
        state.quantdata_poll_task, state.health_check_task,
        state.vix_task, state.account_task, state.microstructure_task,
        state.hft_task, state.pattern_trigger_task,
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
    state.pattern_trigger_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("fef-sidecar starting up")
    await init_db()
    await _seed_defaults_on_first_run()
    from fef_sidecar.engine.calendar import seed_calendar_if_empty
    await seed_calendar_if_empty()
    # Engine-change ledger backfill (idempotent by change name). The
    # 2026-07-06 EOD flagged the empty engine_change table as a
    # traceability gap — running the backfill on every boot keeps the
    # ledger complete without a manual /admin call.
    try:
        from fef_sidecar.engine.engine_change_backfill import backfill_engine_changes
        result = await backfill_engine_changes()
        if result.get("inserted"):
            logger.info("EngineChange ledger backfill: %s", result)
    except Exception:
        logger.exception("EngineChange ledger startup backfill failed")

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
        asyncio.create_task(daily_prior_close_refresher(state), name="daily-prior-close-refresher")
        asyncio.create_task(backfill_regime_from_db(state), name="backfill-regime")
        asyncio.create_task(_load_ml_inference(state), name="load-ml-inference")
        asyncio.create_task(daily_session_audit_runner(state), name="daily-session-audit-runner")
        asyncio.create_task(_startup_audit_catchup(state), name="startup-audit-catchup")
        from fef_sidecar.engine.signal_lookahead import lookahead_worker
        asyncio.create_task(lookahead_worker(state), name="signal-lookahead-worker")

        # Position reconciliation on startup — detects orphans (IBKR has,
        # DB doesn't) and phantoms (DB has, IBKR doesn't). Critical for
        # crash recovery; runs after IBKR is connected.
        async def _reconcile_after_connect():
            # Wait for IBKR + first iter to settle, then reconcile
            await asyncio.sleep(8)
            from fef_sidecar.engine.position_reconcile import reconcile_positions
            try:
                await reconcile_positions(state)
            except Exception:
                logger.exception("startup position reconciliation failed")
        asyncio.create_task(_reconcile_after_connect(), name="position-reconcile-startup")

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
            state.option_chain_capture_task,
            state.pattern_trigger_task,
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
