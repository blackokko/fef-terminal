"""Background loops:

- fef_tick_loop        — every FEF_LOOP_INTERVAL_SEC (default 120s): consumes
                         the cached snapshot, runs FEF, persists, broadcasts IterEvent
- bar_stream_loop      — continuously streams IBKR 2-min bars, broadcasts TickEvent
                         on each close
- tick_stream_loop     — sub-second last-price stream (live: ib_async updateEvent;
                         mock: synthetic ticks every FEF_TICK_INTERVAL_SEC). Broadcasts
                         TickEvent without a bar payload.
- quantdata_poll_loop  — every FEF_QUANTDATA_POLL_INTERVAL_SEC (default 15s): fetches
                         a fresh MarketSnapshot, caches it in AppState, broadcasts
                         SnapshotEvent so the sidebar refreshes ahead of the FEF cadence

All four spawn as asyncio tasks in the FastAPI lifespan and cancel on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from fef_sidecar.db import (
    FEFConfidence,
    FEFDirection,
    SignalIterRepository,
)
from fef_sidecar.engine.fef import snapshot_to_inputs
from fef_sidecar.engine.option_targets import OptionTargets, build_option_targets
from fef_sidecar.events import IterEvent, SnapshotEvent, TickEvent
from fef_sidecar.ibkr import Contract, ContractKind

if TYPE_CHECKING:
    from fef_sidecar.state import AppState

logger = logging.getLogger(__name__)


def _market_hours_et() -> tuple[int, int]:
    return 9 * 60 + 30, 16 * 60


def _in_market_hours_et() -> bool:
    et = timezone(timedelta(hours=-4))
    now = datetime.now(et)
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    open_m, close_m = _market_hours_et()
    return open_m <= minute <= close_m


def _today_et() -> str:
    et = timezone(timedelta(hours=-4))
    return datetime.now(et).strftime("%Y-%m-%d")


def _walls_json(walls: Any) -> list[dict[str, Any]]:
    return [
        {"strike": w.strike, "net_m": round(w.net_m, 2), "side": w.side}
        for w in walls
    ]


# ---------------------------------------------------------------------------
# QuantData poll loop — refreshes the cached snapshot at high cadence
# ---------------------------------------------------------------------------


async def quantdata_poll_loop(state: AppState) -> None:
    interval = float(os.environ.get("FEF_QUANTDATA_POLL_INTERVAL_SEC", "15"))
    logger.info("Starting QuantData poll loop (interval=%.1fs)", interval)

    while True:
        try:
            snap = await state.quantdata.fetch_market_snapshot()  # type: ignore[func-returns-value]
            state.last_snapshot = snap
            state.last_spx_price = snap.spx_price  # type: ignore[union-attr]

            event = SnapshotEvent(
                timestamp_ms=snap.fetched_at_ms,  # type: ignore[union-attr]
                spx_price=snap.spx_price,  # type: ignore[union-attr]
                top_gex=_walls_json(snap.gex.top(5)),  # type: ignore[union-attr]
                top_dex=_walls_json(snap.dex.top(5)),  # type: ignore[union-attr]
                drift={
                    "cum_net_m": snap.drift.cumulative_net_m,  # type: ignore[union-attr]
                    "bias": snap.drift.bias,  # type: ignore[union-attr]
                },
                ntm_max_pain=_ntm_max_pain(snap),
                raw_max_pain=snap.max_pain.max_pain,  # type: ignore[union-attr]
                aggression={
                    "call_pct": snap.aggression.call.aggression_pct,  # type: ignore[union-attr]
                    "put_pct": snap.aggression.put.aggression_pct,  # type: ignore[union-attr]
                }
                if snap.aggression is not None  # type: ignore[union-attr]
                else None,
            )
            await state.broadcaster.publish(event)
        except asyncio.CancelledError:
            logger.info("QuantData poll loop cancelled — exiting")
            raise
        except Exception:
            logger.exception("QuantData poll iteration failed")
        await asyncio.sleep(interval)


def _ntm_max_pain(snap: Any) -> float:
    """NTM max pain from the snapshot's OI rows, or fall back to raw chain MP."""
    if snap.oi is None:
        return snap.max_pain.max_pain
    from fef_sidecar.quantdata.max_pain import compute_ntm_max_pain

    return compute_ntm_max_pain(snap.oi.rows, snap.spx_price).max_pain


# ---------------------------------------------------------------------------
# FEF tick loop — consumes the cached snapshot, runs the engine
# ---------------------------------------------------------------------------


async def fef_tick_loop(state: AppState) -> None:
    interval = int(os.environ.get("FEF_LOOP_INTERVAL_SEC", "120"))
    market_hours_only = os.environ.get("FEF_LOOP_MARKET_HOURS_ONLY", "false").lower() in (
        "true",
        "1",
        "yes",
        "on",
    )
    warmup_iters = int(os.environ.get("FEF_WARMUP_ITERS", "3"))
    logger.info(
        "Starting FEF tick loop (interval=%ds, market_hours_only=%s)",
        interval,
        market_hours_only,
    )

    # IMPROVEMENTS #21: pre-market warmup — seed the engine buffer with
    # `warmup_iters` quick fetches so the first post-open iter isn't WAIT.
    if warmup_iters > 0:
        logger.info("Pre-warming FEF buffer with %d quick iters", warmup_iters)
        for i in range(warmup_iters):
            try:
                await _run_one_iter(state, force_fresh=True)
                if i < warmup_iters - 1:
                    await asyncio.sleep(1)
            except Exception:
                logger.exception("Warmup iter %d failed", i)

    while True:
        try:
            # IMPROVEMENTS #22: detect new session_date and reset engine
            current_date = _today_et()
            if state.session_date != current_date:
                if state.session_date != "":
                    logger.info(
                        "New session detected (%s -> %s) — resetting engine buffer",
                        state.session_date,
                        current_date,
                    )
                    state.engine.reset()
                state.session_date = current_date

            if market_hours_only and not _in_market_hours_et():
                logger.debug("Outside market hours — sleeping 60s")
                await asyncio.sleep(60)
                continue

            await _run_one_iter(state)
        except asyncio.CancelledError:
            logger.info("FEF tick loop cancelled — exiting")
            raise
        except Exception:
            logger.exception("FEF tick loop iteration failed")

        await asyncio.sleep(interval)


async def _run_one_iter(state: AppState, *, force_fresh: bool = False) -> None:
    """One FEF iter — uses cached snapshot if available, else fetches fresh.

    force_fresh=True bypasses the cache and fetches a new snapshot. Used by
    manual /run_iter_now triggers so rapid back-to-back calls actually see
    new data instead of replaying the same cached snapshot.
    """
    import time as _time

    perf: dict[str, float] = {}
    t_start = _time.perf_counter()

    snap = state.last_snapshot
    if snap is None or force_fresh:
        t0 = _time.perf_counter()
        snap = await state.quantdata.fetch_market_snapshot()  # type: ignore[func-returns-value]
        state.last_snapshot = snap
        perf["quantdata_fetch_ms"] = (_time.perf_counter() - t0) * 1000

    t0 = _time.perf_counter()
    inputs = snapshot_to_inputs(snap)  # type: ignore[arg-type]
    verdict = state.engine.push(inputs)
    perf["fef_compute_ms"] = (_time.perf_counter() - t0) * 1000
    state.last_spx_price = inputs.spx_price

    # When verdict is actionable, also pull the option chain quote for the
    # recommended strike and compute target option prices via delta.
    t0 = _time.perf_counter()
    option_targets = await _maybe_fetch_option_targets(state, verdict, inputs)
    perf["option_quote_ms"] = (_time.perf_counter() - t0) * 1000

    signal = await SignalIterRepository.write_iter(
        spx_price=inputs.spx_price,
        direction=verdict.direction,
        confidence=verdict.confidence,
        reasoning=verdict.reasoning,
        drift_cum_net_m=inputs.drift_cum_net_m,
        drift_bias=inputs.drift_bias,
        ntm_max_pain=inputs.ntm_max_pain,
        raw_max_pain=inputs.raw_max_pain,
        top_gex_walls=_walls_json(inputs.top_gex_walls),
        top_dex_walls=_walls_json(inputs.top_dex_walls),
        aggression_call_pct=inputs.aggression_call_pct,
        aggression_put_pct=inputs.aggression_put_pct,
        iv_rank_call_pct=inputs.iv_rank_call_pct,
        iv_rank_put_pct=inputs.iv_rank_put_pct,
        extras={
            **verdict.to_log_extras(),
            "option_pricing": _option_pricing_dict(option_targets),
        },
        session_date=_today_et(),
    )
    state.last_iter_num = signal.iter_num

    event = IterEvent(
        iter_num=signal.iter_num,
        session_date=signal.session_date,
        spx_price=inputs.spx_price,
        verdict={
            "direction": verdict.direction.value,
            "confidence": verdict.confidence.value,
            "reasoning": verdict.reasoning,
            "strike_rec": verdict.strike_rec,
            "t1": verdict.t1,
            "t2": verdict.t2,
            "t3": verdict.t3,
            "stop": verdict.stop,
            "delta_price": verdict.delta_price,
            "delta_drift_m": verdict.delta_drift_m,
            "fe_ratio": verdict.fe_ratio,
            "aligned": verdict.aligned,
            "absorption": verdict.absorption,
            "wall_proximity_skip": verdict.wall_proximity_skip,
            "wall_collapse_trigger": verdict.wall_collapse_trigger,
            "nearest_wall_strike": verdict.nearest_wall_strike,
            "nearest_wall_distance": verdict.nearest_wall_distance,
            "secondary_scores": verdict.secondary_scores,
            "option_pricing": _option_pricing_dict(option_targets),
        },
        snapshot={
            "top_gex": _walls_json(inputs.top_gex_walls),
            "top_dex": _walls_json(inputs.top_dex_walls),
            "drift": {
                "cum_net_m": inputs.drift_cum_net_m,
                "bias": inputs.drift_bias,
            },
            "ntm_max_pain": inputs.ntm_max_pain,
            "raw_max_pain": inputs.raw_max_pain,
            "aggression": {
                "call_pct": inputs.aggression_call_pct,
                "put_pct": inputs.aggression_put_pct,
            }
            if inputs.aggression_call_pct is not None
            else None,
        },
    )
    state.last_verdict = event.verdict
    t0 = _time.perf_counter()
    await state.broadcaster.publish(event)
    perf["broadcast_ms"] = (_time.perf_counter() - t0) * 1000
    perf["total_ms"] = (_time.perf_counter() - t_start) * 1000
    state.last_iter_perf_ms = {k: round(v, 2) for k, v in perf.items()}

    if perf["total_ms"] > 1000:
        logger.warning(
            "FEF iter %d took %.0fms (qd=%.0f / fef=%.0f / opt=%.0f / bcast=%.0f)",
            signal.iter_num,
            perf["total_ms"],
            perf.get("quantdata_fetch_ms", 0),
            perf["fef_compute_ms"],
            perf["option_quote_ms"],
            perf["broadcast_ms"],
        )
    else:
        logger.debug(
            "FEF iter %d — SPX $%.2f — %s (%.0fms)",
            signal.iter_num,
            inputs.spx_price,
            verdict.direction.value,
            perf["total_ms"],
        )


# ---------------------------------------------------------------------------
# Bar stream — chart candles
# ---------------------------------------------------------------------------


async def _maybe_fetch_option_targets(
    state: AppState,
    verdict: Any,
    inputs: Any,
) -> OptionTargets | None:
    """If verdict is actionable, fetch the option chain quote and build option-price targets."""
    if verdict.direction.value == "WAIT" or verdict.strike_rec is None:
        return None
    if verdict.t1 is None or verdict.t2 is None or verdict.t3 is None or verdict.stop is None:
        return None

    expiry = _today_et().replace("-", "")  # YYYYMMDD for IBKR
    right = "C" if verdict.direction.value == "LONG_CALLS" else "P"

    contract = Contract(
        symbol="SPX",
        kind=ContractKind.OPTION,
        exchange="CBOE",  # SPX options are CBOE-listed
        expiry=expiry,
        strike=verdict.strike_rec,
        right=right,
    )

    try:
        quote = await state.ibkr.get_option_quote(contract)
        return build_option_targets(
            quote=quote,
            current_spx=inputs.spx_price,
            spx_t1=verdict.t1,
            spx_t2=verdict.t2,
            spx_t3=verdict.t3,
            spx_stop=verdict.stop,
        )
    except Exception as e:
        logger.warning("Could not fetch option quote for %s: %s", contract, e)
        return None


def _option_pricing_dict(targets: OptionTargets | None) -> dict[str, Any] | None:
    if targets is None:
        return None
    return {
        "contract_strike": targets.contract_strike,
        "right": targets.right,
        "entry_limit": targets.entry_limit,
        "delta": targets.delta,
        "underlying_price_at_quote": targets.underlying_price_at_quote,
        "t1": targets.t1,
        "t2": targets.t2,
        "t3": targets.t3,
        "stop": targets.stop,
    }


async def bar_stream_loop(state: AppState) -> None:
    logger.info("Starting bar stream loop")
    while True:
        try:
            await state.ibkr.connect()
            async for bar in state.ibkr.stream_bars(Contract.spx_index()):
                state.last_spx_price = bar.close
                event = TickEvent(
                    timestamp_ms=bar.epoch_ms,
                    spx_price=bar.close,
                    bar={
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                    },
                )
                await state.broadcaster.publish(event)
            logger.info("Bar stream completed — sleeping 60s before reconnect")
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info("Bar stream cancelled — exiting")
            raise
        except Exception:
            logger.exception("Bar stream failed — reconnecting in 5s")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Tick stream — sub-second last price for the top-bar SPX banner
# ---------------------------------------------------------------------------


async def tick_stream_loop(state: AppState) -> None:
    logger.info("Starting tick stream loop")
    while True:
        try:
            await state.ibkr.connect()
            async for quote in state.ibkr.stream_last_price(Contract.spx_index()):
                state.last_spx_price = quote.last
                event = TickEvent(
                    timestamp_ms=int(quote.timestamp.timestamp() * 1000),
                    spx_price=quote.last,
                    bar=None,  # tick without a bar — just a price update
                )
                await state.broadcaster.publish(event)
        except asyncio.CancelledError:
            logger.info("Tick stream cancelled — exiting")
            raise
        except Exception:
            logger.exception("Tick stream failed — reconnecting in 5s")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# IBKR health check — pings every 30s, broadcasts StatusEvent on state change
# ---------------------------------------------------------------------------


async def ibkr_health_check_loop(state: AppState) -> None:
    """Ping IBKR every FEF_IBKR_HEALTHCHECK_SEC seconds. On state change,
    broadcast a StatusEvent so the UI dot updates without waiting for an
    iter or a manual refresh. On transition to disconnected, force a
    reconnect attempt — the streams will then catch their broken
    subscriptions and re-subscribe.
    """
    from fef_sidecar.events import StatusEvent

    interval = float(os.environ.get("FEF_IBKR_HEALTHCHECK_SEC", "30"))
    logger.info("Starting IBKR health check loop (interval=%.1fs)", interval)

    last_status = state.ibkr_status

    async def _publish(current: str) -> None:
        event = StatusEvent(
            quantdata=type(state.quantdata).__name__,
            ibkr=current,
            engine="running" if state.loop_task is not None else "stopped",
            history_size=state.engine.history_size,
        )
        await state.broadcaster.publish(event)

    while True:
        try:
            healthy = await state.ibkr.ping()
            current = "connected" if healthy else "disconnected"
            state.ibkr_status = current

            if current != last_status:
                logger.info("IBKR status change: %s -> %s", last_status, current)
                await _publish(current)
                last_status = current

                if current == "disconnected":
                    # Force reconnect — streams will re-subscribe on next iter
                    logger.warning("IBKR ping failed; forcing reconnect")
                    state.ibkr_status = "reconnecting"
                    await _publish("reconnecting")
                    try:
                        await state.ibkr.disconnect()
                    except Exception:
                        pass
                    try:
                        await state.ibkr.connect()
                        if await state.ibkr.ping():
                            state.ibkr_status = "connected"
                            last_status = "connected"
                            await _publish("connected")
                            logger.info("IBKR reconnect succeeded")
                        else:
                            logger.warning("Reconnect did not restore health")
                    except Exception:
                        logger.exception("Forced reconnect failed; streams will retry")
        except asyncio.CancelledError:
            logger.info("IBKR health check cancelled — exiting")
            raise
        except Exception:
            logger.exception("IBKR health check iteration failed")

        await asyncio.sleep(interval)
