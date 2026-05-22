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
from fef_sidecar.engine.fef import snapshot_to_inputs

logger = logging.getLogger(__name__)


OPTION_SYMBOLS = {"SPX", "SPY", "QQQ", "IWM", "GLD", "SMH", "TLT"}


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

    # Cache the verdict for the FE Scanner panel (it polls /scanner/symbols).
    # We do NOT broadcast IterEvent from the scanner — that would push the
    # other symbol's walls / max-pain / drift onto the active chart since
    # the chart subscribes to ALL iter events. The user wants scanner work
    # to stay in the background, not pollute the chart.
    state.last_scanner_verdict_by_symbol[symbol] = {
        "direction": verdict.direction.value,
        "confidence": verdict.confidence.value,
        "reasoning": verdict.reasoning,
        "spx_price": inputs.spx_price,
        "strike_rec": verdict.strike_rec,
        "timestamp_ms": int(_time.time() * 1000),
    }

    # Build a verdict dict for the auto-trader (not broadcast — local only).
    verdict_dict = {
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
    }
    # Auto-trader needs option_pricing to pass gate 8. Fetch the option
    # quote for the scanner-recommended strike here (same path as the
    # main iter loop's _maybe_fetch_option_targets does for SPX).
    if verdict.direction.value in ("LONG_CALLS", "LONG_PUTS") and verdict.strike_rec:
        try:
            from fef_sidecar.engine.option_targets import build_option_targets
            from fef_sidecar.engine.expiry_resolver import resolve_nearest_expiry
            from fef_sidecar.ibkr.models import Contract, ContractKind
            # Per-symbol nearest-expiry resolution. Fixes the bug where
            # GLD/SMH/TLT (no 0DTE) and SPY/QQQ/IWM on Tu/Th silently
            # failed Gate 8 because today's expiry didn't exist.
            expiry = await resolve_nearest_expiry(state, symbol)
            right = "C" if verdict.direction.value == "LONG_CALLS" else "P"
            opt_contract = Contract(
                symbol=symbol, kind=ContractKind.OPTION,
                exchange="CBOE" if symbol == "SPX" else "SMART",
                expiry=expiry, strike=verdict.strike_rec, right=right,
            )
            quote = await state.ibkr.get_option_quote(opt_contract)
            targets = build_option_targets(
                quote=quote, current_spx=inputs.spx_price,
                spx_t1=verdict.t1, spx_t2=verdict.t2,
                spx_t3=verdict.t3, spx_stop=verdict.stop,
            )
            if targets is not None:
                verdict_dict["option_pricing"] = {
                    "contract_strike": targets.contract_strike,
                    "right": targets.right,
                    "entry_limit": targets.entry_limit,
                    "delta": targets.delta,
                    "underlying_price_at_quote": targets.underlying_price_at_quote,
                    "t1": targets.t1, "t2": targets.t2,
                    "t3": targets.t3, "stop": targets.stop,
                }
        except Exception:
            logger.exception("scanner: option quote fetch failed for %s", symbol)

    if symbol in OPTION_SYMBOLS:
        try:
            from fef_sidecar.engine.auto_trader import maybe_auto_trade
            decision = await maybe_auto_trade(state, verdict_dict, symbol=symbol)
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
