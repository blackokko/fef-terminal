"""Background loops: FEF tick loop + IBKR bar stream.

Both are spawned as asyncio tasks in the FastAPI lifespan and cancelled on
shutdown. The FEF loop ticks every `FEF_LOOP_INTERVAL_SEC` (default 120s)
and publishes IterEvents. The bar stream runs continuously and publishes
TickEvents as each 2-min bar closes.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from fef_sidecar.db import (
    FEFConfidence,
    FEFDirection,
    SignalIterRepository,
)
from fef_sidecar.engine.fef import snapshot_to_inputs
from fef_sidecar.events import IterEvent, TickEvent
from fef_sidecar.ibkr import Contract

if TYPE_CHECKING:
    from fef_sidecar.state import AppState

logger = logging.getLogger(__name__)


def _market_hours_et() -> tuple[int, int]:
    """Return (open_minute, close_minute) for ET market hours, midnight-relative."""
    return 9 * 60 + 30, 16 * 60  # 9:30 - 16:00


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


async def fef_tick_loop(state: AppState) -> None:
    """Pull a market snapshot, run the FEF engine, persist + broadcast — forever."""
    interval = int(os.environ.get("FEF_LOOP_INTERVAL_SEC", "120"))
    market_hours_only = os.environ.get("FEF_LOOP_MARKET_HOURS_ONLY", "false").lower() in (
        "true",
        "1",
        "yes",
        "on",
    )

    logger.info(
        "Starting FEF tick loop (interval=%ds, market_hours_only=%s)",
        interval,
        market_hours_only,
    )

    while True:
        try:
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


async def _run_one_iter(state: AppState) -> None:
    """Single iteration of the FEF loop — extracted so it's testable + manually triggerable."""
    snap = await state.quantdata.fetch_market_snapshot()  # type: ignore[func-returns-value]
    inputs = snapshot_to_inputs(snap)  # type: ignore[arg-type]
    verdict = state.engine.push(inputs)

    state.last_spx_price = inputs.spx_price

    def _walls_json(walls):
        return [
            {"strike": w.strike, "net_m": round(w.net_m, 2), "side": w.side}
            for w in walls
        ]

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
        extras=verdict.to_log_extras(),
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
    await state.broadcaster.publish(event)
    logger.debug(
        "FEF iter %d — SPX $%.2f — %s",
        signal.iter_num,
        inputs.spx_price,
        verdict.direction.value,
    )


async def bar_stream_loop(state: AppState) -> None:
    """Stream IBKR SPX 2-min bars and broadcast TickEvents on each close."""
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
            # Stream exhausted (e.g. mock provider) — wait before reconnecting
            # to avoid flooding the broadcaster with replays.
            logger.info("Bar stream completed — sleeping 60s before reconnect")
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info("Bar stream cancelled — exiting")
            raise
        except Exception:
            logger.exception("Bar stream failed — reconnecting in 5s")
            await asyncio.sleep(5)
