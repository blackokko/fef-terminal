"""Multi-symbol FEF scanner.

Runs FEF iters in parallel for each symbol the user has enabled via
state.scan_enabled_symbols. Each symbol has its own FEFEngine instance
in state.scanner_engines so warmup, drift, and wall history are tracked
independently per ticker.

Independent of state.active_symbol (the chart symbol) — the scanner
runs in the background regardless of what the user is viewing.

For SPX / SPY / QQQ / IWM: full options auto-trade pipeline (same path
as the existing single-symbol fef_tick_loop, just per-symbol).

For MES / MNQ / M2K (CME micro futures): FEF verdict is computed and
broadcast, but auto-trade is currently SCAN-ONLY — futures position
sizing + bracket logic aren't wired yet. An audit row notes 'futures
auto-trade not implemented' when an actionable verdict fires on a
futures symbol.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from typing import Any

from fef_sidecar.db.repository import SignalIterRepository
from fef_sidecar.engine import FEFEngine
from fef_sidecar.events import IterEvent
from fef_sidecar.quantdata.adapters import snapshot_to_inputs

logger = logging.getLogger(__name__)


OPTION_SYMBOLS = {"SPX", "SPY", "QQQ", "IWM"}


def _make_engine_for(symbol: str) -> FEFEngine:
    """Build a FEFEngine with the standard thresholds. One per symbol."""
    interval = float(os.environ.get("FEF_LOOP_INTERVAL_SEC", "120"))
    threshold_scale = max(0.1, interval / 120.0)
    return FEFEngine(
        wall_proximity_pts=float(os.environ.get("FEF_WALL_PROXIMITY_PTS", "5.0")),
        wall_collapse_pct=float(os.environ.get("FEF_WALL_COLLAPSE_PCT", "0.20")),
        wall_collapse_floor_m=float(os.environ.get("FEF_WALL_COLLAPSE_FLOOR_M", "5.0")),
        absorption_fe_ratio_min=float(os.environ.get("FEF_ABSORPTION_FE_MIN", "5.0")),
        threshold_scale=threshold_scale,
    )


async def _scan_one_symbol(state, symbol: str) -> None:
    """Run one FEF iter for the given symbol. Broadcasts an IterEvent
    with symbol field set so the FE can route to the right per-symbol UI.
    """
    # Lazy-create the per-symbol engine
    if symbol not in state.scanner_engines:
        state.scanner_engines[symbol] = _make_engine_for(symbol)
    engine = state.scanner_engines[symbol]

    # Fetch QuantData snapshot for this symbol. Temporarily flip the page
    # filter; the global poll will reset it next cycle.
    try:
        await state.quantdata.set_page_filter(ticker=symbol)
        snap = await state.quantdata.fetch_market_snapshot()
    except Exception as e:
        logger.warning("scanner: QuantData fetch failed for %s: %s", symbol, e)
        return
    if snap is None:
        return

    inputs = snapshot_to_inputs(snap)
    verdict = engine.push(inputs)

    # Build the event (mirrors _run_one_iter's broadcast structure)
    from fef_sidecar.engine.types import FEFInputs  # noqa: F401
    event = IterEvent(
        iter_num=0,  # scanner uses 0 — DB iter_num is its own counter
        session_date=_today_et(),
        spx_price=inputs.spx_price,
        verdict={
            "direction": verdict.direction.value,
            "confidence": verdict.confidence.value,
            "reasoning": verdict.reasoning,
            "strike_rec": verdict.strike_rec,
            "t1": verdict.t1, "t2": verdict.t2, "t3": verdict.t3,
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
            "scanner_symbol": symbol,
        },
        snapshot={
            "top_gex": [{"strike": w.strike, "net_m": w.net_m, "side": w.side}
                        for w in inputs.top_gex_walls],
            "top_dex": [{"strike": w.strike, "net_m": w.net_m, "side": w.side}
                        for w in inputs.top_dex_walls],
            "drift": {
                "cum_net_m": inputs.drift_cum_net_m,
                "bias": inputs.drift_bias,
            },
            "ntm_max_pain": inputs.ntm_max_pain,
            "raw_max_pain": inputs.raw_max_pain,
            "aggression": None,
        },
    )
    # Cache the verdict so the FE Scanner panel can render the latest
    # state for each symbol without subscribing to every IterEvent.
    state.last_scanner_verdict_by_symbol[symbol] = {
        "direction": verdict.direction.value,
        "confidence": verdict.confidence.value,
        "reasoning": verdict.reasoning,
        "spx_price": inputs.spx_price,
        "strike_rec": verdict.strike_rec,
        "timestamp_ms": int(_time.time() * 1000),
    }
    await state.broadcaster.publish(event)

    if symbol in OPTION_SYMBOLS:
        try:
            from fef_sidecar.engine.auto_trader import maybe_auto_trade
            decision = await maybe_auto_trade(state, event.verdict, symbol=symbol)
            if decision.fired and decision.order_result:
                logger.info("scanner: %s auto-trade fired: %s", symbol, decision.order_result)
        except Exception:
            logger.exception("scanner: auto-trade evaluation failed for %s", symbol)


def _today_et() -> str:
    from datetime import datetime, timedelta, timezone
    et = timezone(timedelta(hours=-4))
    return datetime.now(et).strftime("%Y-%m-%d")


async def multi_symbol_scanner_loop(state) -> None:
    """Background loop. Every FEF_SCAN_INTERVAL_SEC, iterates through
    enabled symbols and runs a scan for each. Skips disabled symbols
    and never touches the active chart symbol's primary loop.
    """
    interval = float(os.environ.get("FEF_SCAN_INTERVAL_SEC", "60"))
    logger.info("multi-symbol scanner loop starting (interval=%.1fs)", interval)

    # Wait a few seconds for the main fef_tick_loop's warmup to settle
    await asyncio.sleep(15)

    while True:
        try:
            enabled = [
                s for s, on in state.scan_enabled_symbols.items() if on
            ]
            for symbol in enabled:
                try:
                    await _scan_one_symbol(state, symbol)
                    await asyncio.sleep(0.5)  # spread QD load
                except Exception:
                    logger.exception("scanner: %s scan failed", symbol)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("multi-symbol scanner loop iteration failed")
        await asyncio.sleep(interval)
