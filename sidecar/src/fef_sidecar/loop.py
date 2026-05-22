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
from fef_sidecar.engine.mock_autotrader import advance_pending, maybe_create_ticket
from fef_sidecar.engine.option_targets import OptionTargets, build_option_targets
from fef_sidecar.engine.patterns import evaluate_pattern, extract_fields
from fef_sidecar.engine.regime import RegimeSample, now_minutes_since_open_et
from fef_sidecar.engine.tournament import consensus
from fef_sidecar.events import (
    IterEvent,
    PatternMatchEvent,
    RegimeEvent,
    SnapshotEvent,
    TickEvent,
    TournamentEvent,
)
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


def _expiry_rows_json(rows: Any) -> list[dict[str, Any]]:
    """Serialise ExpirationExposureRow list to JSON-safe dicts."""
    return [
        {
            "expiration": r.expiration,
            "call_m": round(r.call_m, 4),
            "put_m": round(r.put_m, 4),
            "net_m": round(r.net_m, 4),
        }
        for r in (rows or [])
    ]


def _orders_json(orders: Any) -> list[dict[str, Any]]:
    """Serialise OrderFlowRow list to JSON-safe dicts (top 20 most recent)."""
    out = []
    for o in (orders or [])[:20]:
        out.append({
            "timestamp_ms": o.timestamp_ms,
            "strike": o.strike,
            "right": o.right,
            "expiration": o.expiration,
            "premium_dollars": round(o.premium_dollars, 2),
            "side_aggression": o.side_aggression,
            "is_whale": o.is_whale,
            "is_sweep": o.is_sweep,
            "is_golden_sweep": o.is_golden_sweep,
            "is_block": o.is_block,
            "other_flags": list(o.other_flags),
        })
    return out


# Per-symbol plausible price range — anything outside is rejected as
# garbage (stale ticker route, wrong-symbol QuantData response, etc.).
# Catches the 5/18-5/20 corruption where active_symbol=ES caused
# QuantData to return spx_price≈67 which then cascaded into
# delta_price=-7341 / nearest_wall_distance=7300+ across hundreds of
# SignalIter rows. Guard prevents new bad rows; old rows are filtered
# downstream via the same range.
SYMBOL_PRICE_BOUNDS: dict[str, tuple[float, float]] = {
    "SPX": (1000.0, 20000.0),
    "SPY": (200.0, 1500.0),
    "QQQ": (200.0, 1500.0),
    "IWM": (100.0, 800.0),
    "GLD": (100.0, 500.0),
    "SMH": (100.0, 500.0),
    "TLT": (50.0, 300.0),
    "ES":  (1000.0, 20000.0),  # E-mini SPX futures — same band as SPX
    "NQ":  (5000.0, 50000.0),
    "RTY": (1000.0, 5000.0),
}


def _snapshot_plausible(snap: Any, symbol: str) -> tuple[bool, str]:
    """Validate a QuantData snapshot against per-symbol price bounds.
    Returns (ok, reason). When False, the snapshot is garbage and must
    NOT be cached, persisted, or fed into the FEF engine."""
    if snap is None:
        return False, "snap is None"
    spx = getattr(snap, "spx_price", None)
    if spx is None or spx <= 0:
        return False, f"spx_price={spx!r} non-positive"
    lo, hi = SYMBOL_PRICE_BOUNDS.get(symbol, (50.0, 50000.0))
    if not (lo <= spx <= hi):
        return False, f"spx_price={spx:.2f} outside {symbol} bounds [{lo:.0f}, {hi:.0f}]"
    return True, "ok"


# ---------------------------------------------------------------------------
# QuantData poll loop — refreshes the cached snapshot at high cadence
# ---------------------------------------------------------------------------


async def quantdata_poll_loop(state: AppState) -> None:
    interval = float(os.environ.get("FEF_QUANTDATA_POLL_INTERVAL_SEC", "15"))
    logger.info("Starting QuantData poll loop (interval=%.1fs, symbol=%s)", interval, state.active_symbol)

    while True:
        try:
            # Ensure QuantData page filter matches active symbol (idempotent)
            await state.quantdata.set_page_filter(ticker=state.active_symbol)  # type: ignore[attr-defined]
            snap = await state.quantdata.fetch_market_snapshot()  # type: ignore[func-returns-value]
            ok, reason = _snapshot_plausible(snap, state.active_symbol)
            if not ok:
                logger.warning(
                    "Discarded implausible snapshot for %s: %s — not caching or persisting",
                    state.active_symbol, reason,
                )
                await asyncio.sleep(interval)
                continue
            state.last_snapshot = snap
            state.last_spx_price = snap.spx_price  # type: ignore[union-attr]

            # Persist flow snapshot for historical replay + backtest
            try:
                # Compute order-flow 30m summary for the snapshot timestamp
                _of_orders = snap.order_flow.orders if snap.order_flow else []  # type: ignore[union-attr]
                _of_cutoff = snap.fetched_at_ms - 30 * 60 * 1000  # type: ignore[union-attr]
                _whale_30m = sum(1 for o in _of_orders if o.is_whale and o.timestamp_ms >= _of_cutoff)
                _gs_30m = sum(1 for o in _of_orders if o.is_golden_sweep and o.timestamp_ms >= _of_cutoff)

                await SignalIterRepository.write_flow_snapshot(
                    symbol=state.active_symbol,
                    session_date=_today_et(),
                    timestamp_ms=snap.fetched_at_ms,  # type: ignore[union-attr]
                    spx_price=snap.spx_price,  # type: ignore[union-attr]
                    top_gex_walls=_walls_json(snap.gex.top(5)),  # type: ignore[union-attr]
                    top_dex_walls=_walls_json(snap.dex.top(5)),  # type: ignore[union-attr]
                    top_cex_walls=_walls_json(snap.cex.top(5)) if snap.cex else [],  # type: ignore[union-attr]
                    top_vex_walls=_walls_json(snap.vex.top(5)) if snap.vex else [],  # type: ignore[union-attr]
                    top_gex_by_expiry=_expiry_rows_json(snap.gex_by_expiry.top(5)) if snap.gex_by_expiry else [],  # type: ignore[union-attr]
                    top_dex_by_expiry=_expiry_rows_json(snap.dex_by_expiry.top(5)) if snap.dex_by_expiry else [],  # type: ignore[union-attr]
                    zero_dte_gex_net_m=snap.gex_by_expiry.zero_dte().net_m if snap.gex_by_expiry and snap.gex_by_expiry.zero_dte() else None,  # type: ignore[union-attr]
                    zero_dte_dex_net_m=snap.dex_by_expiry.zero_dte().net_m if snap.dex_by_expiry and snap.dex_by_expiry.zero_dte() else None,  # type: ignore[union-attr]
                    recent_orders=_orders_json(snap.order_flow.recent(20)) if snap.order_flow else [],  # type: ignore[union-attr]
                    recent_whale_count_30m=_whale_30m,
                    recent_golden_sweep_count_30m=_gs_30m,
                    drift_cum_net_m=snap.drift.cumulative_net_m,  # type: ignore[union-attr]
                    drift_bias=snap.drift.bias,  # type: ignore[union-attr]
                    ntm_max_pain=_ntm_max_pain(snap),
                    raw_max_pain=snap.max_pain.max_pain,  # type: ignore[union-attr]
                    aggression_call_pct=snap.aggression.call.aggression_pct if snap.aggression and snap.aggression.call else None,  # type: ignore[union-attr]
                    aggression_put_pct=snap.aggression.put.aggression_pct if snap.aggression and snap.aggression.put else None,  # type: ignore[union-attr]
                    iv_rank_call_pct=snap.iv_rank.call.ivr_pct if snap.iv_rank and snap.iv_rank.call else None,  # type: ignore[union-attr]
                    iv_rank_put_pct=snap.iv_rank.put.ivr_pct if snap.iv_rank and snap.iv_rank.put else None,  # type: ignore[union-attr]
                )
            except Exception:
                logger.exception("Failed to persist flow snapshot")

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


async def _get_spread_bars_30m(state: AppState) -> list[Any]:
    """Return cached 30-min bars for the active symbol. Re-fetches if the
    cache is older than 25 min OR is missing entirely. Best-effort: returns
    [] if the fetch fails (recommender will then short-circuit to NONE).

    30-min bars are resampled from finer-grained 30-min bar requests when
    the broker supports it directly. For LiveIBKRProvider, we ask for
    '30 mins' directly. For mock, the same — its fetch_historical_bars
    synthesizes whatever bar size we request.
    """
    import time as _t
    from fef_sidecar.ibkr.models import Contract

    symbol = state.active_symbol
    now_ms = int(_t.time() * 1000)
    cached = state.spread_bars_by_symbol.get(symbol)
    fetched_at = state.spread_bars_fetched_ms.get(symbol, 0)
    if cached and (now_ms - fetched_at) < 25 * 60 * 1000:
        return cached  # type: ignore[return-value]

    try:
        bars = await state.ibkr.fetch_historical_bars(  # type: ignore[attr-defined]
            Contract.for_symbol(symbol),
            bar_size="30 mins",
            duration="5 D",
        )
    except Exception:
        logger.exception("fetch_historical_bars (30m) failed for %s", symbol)
        bars = []

    state.spread_bars_by_symbol[symbol] = bars  # type: ignore[assignment]
    state.spread_bars_fetched_ms[symbol] = now_ms
    return bars  # type: ignore[return-value]


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


def _current_provider_mode(state: AppState) -> str:
    """'live' / 'mock' / 'mixed' — tag for the SignalIter.provider_mode column.
    Captures whether QuantData + IBKR are real or mocked. Independent of the
    IBKR account class (paper vs live) — see _current_account_class below."""
    qd_mock = type(state.quantdata).__name__.startswith("Mock")
    ibkr_mock = type(state.ibkr).__name__.startswith("Mock")
    if qd_mock and ibkr_mock:
        return "mock"
    if not qd_mock and not ibkr_mock:
        return "live"
    return "mixed"


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    ema = sum(values[:period]) / period  # SMA seed
    for v in values[period:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


async def _alt_direction_signal(
    state: AppState,
    *,
    n_bars: int,
    ema_short: int,
    ema_long: int,
    label: str,
) -> dict | None:
    """Compute an independent SPX EMA-stack direction signal at the
    requested lookback. Stamped into signal_iter.extras at write time so
    we can correlate engine direction (drift_bias) vs alt signals across
    sessions WITHOUT gating any live trade on them.

    Reads the most recent n_bars SPX 1-min bars from HistoricalBar.
    Returns None on insufficient data. Cheap: indexed read, ~5ms per call.

    Built 2026-05-21 in response to the finding that drift_bias is
    88-100% BULLISH on 4 of 4 sessions regardless of actual SPX path.
    P3 (2026-05-21): now parametric so we can shadow at multiple
    timeframes (30m, 4h) and pick the most predictive one post-freeze."""
    try:
        from sqlmodel import select
        from fef_sidecar.db.engine import get_session
        from fef_sidecar.db.models import HistoricalBar
        async with get_session() as session:
            bars = list((await session.exec(
                select(HistoricalBar)
                .where(HistoricalBar.symbol == "SPX")
                .where(HistoricalBar.bar_size == "1 min")
                .order_by(HistoricalBar.timestamp_ms.desc())  # type: ignore[attr-defined]
                .limit(n_bars)
            )).all())
        if len(bars) < ema_long:
            return {"signal": "INSUFFICIENT_DATA", "n_bars": len(bars), "label": label}
        bars.reverse()  # chronological
        closes = [b.close for b in bars]

        ema_s = _ema(closes, ema_short)
        ema_l = _ema(closes, ema_long)
        if ema_s is None or ema_l is None:
            return None
        # Slope window scaled with the timeframe — 5 bars for short EMA-8
        # (5 min on 1-min bars), 30 bars for slow EMA-50 (30 min). Picks
        # up trend acceleration / deceleration regardless of timeframe.
        slope_window = max(5, ema_short // 2)
        ema_s_prev = _ema(closes[:-slope_window], ema_short) if len(closes) >= ema_short + slope_window else None
        ema_s_slope = ema_s - ema_s_prev if ema_s_prev is not None else 0
        current_spx = closes[-1]
        if ema_s > ema_l * 1.0005 and current_spx > ema_s:
            alignment = "BULLISH"
        elif ema_s < ema_l * 0.9995 and current_spx < ema_s:
            alignment = "BEARISH"
        else:
            alignment = "NEUTRAL"
        return {
            "signal": alignment,
            "label": label,
            f"ema_{ema_short}": round(ema_s, 2),
            f"ema_{ema_long}": round(ema_l, 2),
            f"ema_{ema_short}_slope_{slope_window}bar": round(ema_s_slope, 2),
            f"spx_vs_ema{ema_short}_pts": round(current_spx - ema_s, 2),
            "n_bars": len(bars),
        }
    except Exception:
        logger.exception("_alt_direction_signal(%s) failed", label)
        return None


async def _alt_direction_30m_signal(state: AppState) -> dict | None:
    """30-min shadow: 25 1-min bars, EMA-8/EMA-21."""
    return await _alt_direction_signal(
        state, n_bars=25, ema_short=8, ema_long=21, label="30m"
    )


async def _alt_direction_4h_signal(state: AppState) -> dict | None:
    """4-hour shadow: 240 1-min bars, EMA-50/EMA-200. Catches the
    slower trend regime that the 30m read can flip on the wrong side of.
    Needs 200+ stored 1-min bars (~3.3 hours of session) before it
    returns a non-INSUFFICIENT_DATA signal."""
    return await _alt_direction_signal(
        state, n_bars=240, ema_short=50, ema_long=200, label="4h"
    )


def _current_account_class(state: AppState) -> str:
    """'paper' / 'live' / 'mock' / 'unknown' — tag for which IBKR account
    class is connected. Stamped into SignalIter.extras and LiveOrder.notes
    so research scripts can keep paper and live data strictly separated
    once both flow through the same DB.

    Reads from the IBKR provider's account_class property which is
    populated on connect from managedAccounts(). Both LiveIBKRProvider
    and MockIBKRProvider expose this property uniformly."""
    return getattr(state.ibkr, "account_class", "unknown") or "unknown"


async def _run_one_iter(state: AppState, *, force_fresh: bool = False) -> None:
    """One FEF iter — uses cached snapshot if available, else fetches fresh.

    force_fresh=True bypasses the cache and fetches a new snapshot. Used by
    manual /run_iter_now triggers so rapid back-to-back calls actually see
    new data instead of replaying the same cached snapshot.
    """
    import time as _time

    perf: dict[str, float] = {}
    t_start = _time.perf_counter()

    # SPX sanity guard — defense in depth.
    # Layer 1 (absolute bounds): catches snapshots whose price is outside
    # the active symbol's plausible range. Doesn't require a live tick, so
    # it works pre-market and during IBKR reconnects. The 5/18-5/20 ES
    # corruption (spx_price≈67) is caught here.
    if state.last_snapshot is not None:
        ok, reason = _snapshot_plausible(state.last_snapshot, state.active_symbol)
        if not ok:
            logger.warning("Skip iter: cached snapshot failed plausibility check — %s", reason)
            state.last_snapshot = None
            return
    # Layer 2 (relative drift): catches snapshots that pass the absolute
    # bounds but diverge sharply from the live IBKR tick (stale data
    # mid-session). Requires a live tick reference.
    if state.last_snapshot is not None and state.last_live_tick_spx > 0:
        snap_spx = getattr(state.last_snapshot, "spx_price", 0)
        if snap_spx > 0 and abs(snap_spx - state.last_live_tick_spx) > state.last_live_tick_spx * 0.5:
            logger.warning(
                "Skip iter: snapshot spx_price=%.2f diverges from live tick %.2f "
                "(>50%% — stale/garbage data, likely from futures symbol)",
                snap_spx, state.last_live_tick_spx,
            )
            state.last_snapshot = None  # force re-fetch next iter
            return

    snap = state.last_snapshot
    if snap is None or force_fresh:
        t0 = _time.perf_counter()
        await state.quantdata.set_page_filter(ticker=state.active_symbol)  # type: ignore[attr-defined]
        snap = await state.quantdata.fetch_market_snapshot()  # type: ignore[func-returns-value]
        state.last_snapshot = snap
        perf["quantdata_fetch_ms"] = (_time.perf_counter() - t0) * 1000

    t0 = _time.perf_counter()
    inputs = snapshot_to_inputs(snap)  # type: ignore[arg-type]

    # Live-price overlay: prefer the freshest IBKR tick over the QuantData
    # snapshot's spx_price (which can be 15+s stale by the time we evaluate).
    # Keeps exposure structure (walls/drift/MP) from QuantData but uses live
    # spot for distance calculations so 'nearest_wall_distance' etc. are
    # accurate to the moment of the verdict.
    if state.last_live_tick_spx > 0 and state.last_live_tick_ms > 0:
        age_ms = int(_time.time() * 1000) - state.last_live_tick_ms
        if age_ms < 60_000:  # only trust ticks <60s old
            from dataclasses import replace
            snap_spx = inputs.spx_price
            live_spx = state.last_live_tick_spx
            # Only override if the live price is materially different (>0.5pt)
            # to avoid micro-jitter in the verdict reasoning
            if abs(live_spx - snap_spx) > 0.5:
                inputs = replace(inputs, spx_price=live_spx)
                perf["live_price_overlay_pts"] = round(live_spx - snap_spx, 2)

    verdict = state.engine.push(inputs)
    perf["fef_compute_ms"] = (_time.perf_counter() - t0) * 1000
    state.last_spx_price = inputs.spx_price

    # Shadow engine: parallel eval. Auto-trade ignores its output;
    # ShadowComparison row written for later analysis.
    if state.shadow_engine is not None:
        try:
            shadow_verdict = state.shadow_engine.push(inputs)  # type: ignore[attr-defined]
            from fef_sidecar.db.engine import get_session as _sgs
            from fef_sidecar.db.models import ShadowComparison
            async with _sgs() as _ss:
                _ss.add(ShadowComparison(
                    session_date=_today_et(),
                    iter_num=state.last_iter_num + 1,
                    production_direction=verdict.direction,
                    production_confidence=verdict.confidence,
                    production_reasoning=verdict.reasoning[:500],
                    shadow_label=state.shadow_label,
                    shadow_direction=shadow_verdict.direction,
                    shadow_confidence=shadow_verdict.confidence,
                    shadow_reasoning=shadow_verdict.reasoning[:500],
                    direction_agree=(verdict.direction == shadow_verdict.direction),
                    confidence_agree=(verdict.confidence == shadow_verdict.confidence),
                ))
                await _ss.commit()
        except Exception:
            logger.exception("shadow engine eval failed")

    # When verdict is actionable, also pull the option chain quote for the
    # recommended strike and compute target option prices via delta.
    t0 = _time.perf_counter()
    option_targets = await _maybe_fetch_option_targets(state, verdict, inputs)
    perf["option_quote_ms"] = (_time.perf_counter() - t0) * 1000

    # ML inference overlay — Phase 2. Observational only: stamps
    # ml_expected_r into SignalIter.extras + IterEvent without altering the
    # verdict object that auto_trader consumes. Engine-freeze-safe.
    ml_expected_r: float | None = None
    try:
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        ml_svc = getattr(state, "ml_inference", None)
        if ml_svc is not None and ml_svc.is_ready() and verdict.direction.value != "WAIT":
            et = _tz(_td(hours=-4))
            hour_et = int(_dt.now(et).strftime("%H"))
            ml_expected_r = ml_svc.score(
                {
                    "direction": verdict.direction.value,
                    "confidence": verdict.confidence.value,
                    "spx_price": inputs.spx_price,
                    "ntm_max_pain": inputs.ntm_max_pain,
                    "drift_cum_net_m": inputs.drift_cum_net_m,
                    "aggression_call_pct": inputs.aggression_call_pct,
                    "aggression_put_pct": inputs.aggression_put_pct,
                    "iv_rank_call_pct": inputs.iv_rank_call_pct,
                    "iv_rank_put_pct": inputs.iv_rank_put_pct,
                    "top_gex_walls": _walls_json(inputs.top_gex_walls),
                    "top_dex_walls": _walls_json(inputs.top_dex_walls),
                    "extras": {
                        "fe_ratio": verdict.fe_ratio,
                        "delta_price": verdict.delta_price,
                        "delta_drift_m": verdict.delta_drift_m,
                    },
                },
                hour_et=hour_et,
            )
    except Exception:
        logger.exception("ML inference scoring failed")
        ml_expected_r = None

    # Fire-order: count of preceding signals on this (direction, strike)
    # within the dedup window. fire_order=1 = first fire of a cluster,
    # 2+ = stacks. Lets analytics distinguish first-fire signal quality
    # from stack-fire signal quality.
    fire_order = 0
    try:
        from fef_sidecar.engine.fire_order import compute_fire_order_live
        if verdict.direction.value != "WAIT":
            fire_order = compute_fire_order_live(
                state, verdict.direction.value, verdict.strike_rec,
            )
    except Exception:
        logger.exception("fire_order computation failed")

    # Shadow-track an independent direction signal alongside drift_bias.
    # NOT a gate — purely observational so we can correlate engine
    # direction vs this alt signal across sessions. Built tonight after
    # finding drift_bias is structurally 88-100% BULLISH regardless of
    # actual SPX path; want data on whether an alt signal predicts
    # outcomes any better before deciding to gate on it (post-freeze).
    alt_direction = await _alt_direction_30m_signal(state)
    alt_direction_4h = await _alt_direction_4h_signal(state)

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
        top_cex_walls=_walls_json(inputs.top_cex_walls),
        top_vex_walls=_walls_json(inputs.top_vex_walls),
        top_gex_by_expiry=_expiry_rows_json(inputs.top_gex_by_expiry),
        top_dex_by_expiry=_expiry_rows_json(inputs.top_dex_by_expiry),
        zero_dte_gex_net_m=inputs.zero_dte_gex_net_m,
        zero_dte_dex_net_m=inputs.zero_dte_dex_net_m,
        recent_orders=_orders_json(inputs.recent_orders),
        recent_whale_count_30m=inputs.recent_whale_count_30m,
        recent_golden_sweep_count_30m=inputs.recent_golden_sweep_count_30m,
        aggression_call_pct=inputs.aggression_call_pct,
        aggression_put_pct=inputs.aggression_put_pct,
        iv_rank_call_pct=inputs.iv_rank_call_pct,
        iv_rank_put_pct=inputs.iv_rank_put_pct,
        extras={
            **verdict.to_log_extras(),
            "option_pricing": _option_pricing_dict(option_targets),
            "regime_at_fire": state.last_regime.regime if state.last_regime else None,
            "ml_expected_r": ml_expected_r,
            "fire_order": fire_order,
            # Account-class tag (paper / live / mock / unknown). Lets
            # research scripts keep paper and live data strictly separated
            # once both flow through the same DB. Independent of
            # provider_mode (which captures mock-vs-IBKR, not paper-vs-live).
            "account_class": _current_account_class(state),
            "account_id": getattr(state.ibkr, "account_id", "") or "",
            # Shadow alt direction signals — SPX EMA stack at multiple
            # timeframes. Observational only; NOT used to gate any trade.
            # Compare against drift_bias outcomes across N sessions to
            # decide whether to gate on either post-freeze.
            #   30m = 25 1-min bars, EMA-8/EMA-21 (immediate context)
            #   4h  = 240 1-min bars, EMA-50/EMA-200 (session-trend context)
            # Two timeframes so we can spot regime conflict: 30m BEARISH
            # vs 4h BULLISH suggests a pullback within an uptrend, not a
            # full reversal — exit-only gating may be more appropriate
            # than block-fire gating in that case.
            "alt_direction_30m": alt_direction,
            "alt_direction_4h": alt_direction_4h,
            # Higher-order Greek walls (CEX/VEX) are now top-level columns
            # on SignalIter — not duplicated here.
            # Snapshot the latest HFT + microstructure state at fire time so
            # offline analysis (ML pattern discovery, TuningRecommendation)
            # can correlate signal outcomes with HFT/microstructure context
            # without doing a time-window join.
            "hft_at_fire": dict(state.last_hft_state) if state.last_hft_state else None,
            "microstructure_at_fire": dict(state.last_microstructure_state) if state.last_microstructure_state else None,
            # VIX at fire time — enables regime-conditional backtests + theta
            # projection. Captured from the global VIX poller's last reading.
            "vix_at_fire": state.last_vix,
            # Auto-trader gate state at iter time — was a fire eligible? if not
            # which gate would have blocked it? Used by backtester to replay
            # full live behavior, not just FEF verdicts.
            "gate_state_at_fire": {
                "auto_trade_enabled": state.auto_trade_enabled,
                "session_count": state.auto_trade_count_session,
                "cool_down_remaining_sec_global": max(
                    0, state.auto_trade_cool_down_sec - int(
                        (int(_time.time() * 1000) - state.auto_trade_last_at_ms) / 1000
                    )
                ) if state.auto_trade_last_at_ms else 0,
                "count_by_symbol": dict(state.auto_trade_count_by_symbol),
                "max_per_symbol": state.auto_trade_max_per_symbol,
                "trigger_types_enabled": list(state.auto_trade_trigger_types),
            },
        },
        session_date=_today_et(),
        provider_mode=_current_provider_mode(state),
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
            "ml_expected_r": ml_expected_r,
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
    # Calendar pause window — demote actionable verdicts to WAIT during
    # FOMC/CPI/NFP windows. Engine still records the original direction in
    # extras so backtest analysis can see what the verdict would've been.
    try:
        from fef_sidecar.engine.calendar import is_pause_window
        pause = await is_pause_window()
        if pause.paused and verdict.direction.value != "WAIT":
            event.verdict["original_direction"] = verdict.direction.value
            event.verdict["direction"] = "WAIT"
            event.verdict["reasoning"] = f"PAUSED — {pause.reason}"
            event.verdict["calendar_pause"] = {
                "reason": pause.reason, "event": pause.event,
            }
    except Exception:
        logger.exception("Calendar pause check failed")

    # Regime gate — soft demotion when prior-iter regime was CHOP / PIN_DAY /
    # WALL_TEST. Today's data: aligned-only signals during chop regimes had
    # ~17% win. Demoting them keeps the verdict visible (we can still see it
    # in the UI for manual interpretation) but lowers conviction so the
    # auto-trade confidence floor gates them out.
    try:
        if (state.last_regime is not None
            and event.verdict.get("direction") in ("LONG_CALLS", "LONG_PUTS")
            and state.last_regime.regime in ("CHOP", "PIN_DAY", "WALL_TEST")):
            prev_conf = event.verdict.get("confidence", "NONE")
            new_conf = (
                "MEDIUM" if prev_conf == "HIGH" else
                "LOW" if prev_conf == "MEDIUM" else
                prev_conf
            )
            if new_conf != prev_conf:
                event.verdict["confidence"] = new_conf
                event.verdict["reasoning"] = (event.verdict.get("reasoning", "")
                    + f" [regime={state.last_regime.regime}: demoted {prev_conf}->{new_conf}]")
                event.verdict["regime_demoted"] = {
                    "regime": state.last_regime.regime, "from": prev_conf, "to": new_conf,
                }
    except Exception:
        logger.exception("Regime gate check failed")

    # Spread recommendation — TA-driven entries (EMA 8/21 cross + pullback
    # engulfing on StdDev channel) with delta-targeted strikes (<=0.15 delta,
    # >=$0.70 premium). Reads cached 30-min bars; refreshes them at most
    # every 25 minutes per symbol. Independent of FEF direction.
    try:
        from fef_sidecar.engine.spread_recommender import recommend_from_bars
        from fef_sidecar.engine.spread_entry import SpreadEntryTrigger  # noqa: F401

        bars_30m = await _get_spread_bars_30m(state)
        if not bars_30m:
            event.verdict["spread_recommendation"] = {
                "kind": "NONE", "reasoning": "30-min bar history not yet available",
                "short_strike": None, "long_strike": None, "spread_width": None,
                "estimated_credit_per_contract": None,
                "max_profit_per_contract": None,
                "max_loss_per_contract": None,
                "breakeven": None,
            }
        else:
            today_ymd = _today_et().replace("-", "")
            spread_rec = await recommend_from_bars(
                bars_30m,
                ibkr=state.ibkr,
                symbol=state.active_symbol,
                expiry=today_ymd,
                spot=inputs.spx_price,
            )
            event.verdict["spread_recommendation"] = {
                "kind": spread_rec.kind,
                "reasoning": spread_rec.reasoning,
                "short_strike": spread_rec.short_strike,
                "long_strike": spread_rec.long_strike,
                "spread_width": spread_rec.spread_width,
                "estimated_credit_per_contract": spread_rec.estimated_credit_per_contract,
                "max_profit_per_contract": spread_rec.max_profit_per_contract,
                "max_loss_per_contract": spread_rec.max_loss_per_contract,
                "breakeven": spread_rec.breakeven,
                "trigger_bar_close_ms": spread_rec.trigger_bar_close_ms,
                "trigger_signature": spread_rec.trigger_signature,
                "short_delta": spread_rec.short_delta,
                "fail_reasons": spread_rec.fail_reasons,
                "fresh_trigger": bool(
                    spread_rec.trigger_signature
                    and spread_rec.trigger_signature != state.last_spread_trigger_signature
                ),
            }
            if spread_rec.trigger_signature:
                state.last_spread_trigger_signature = spread_rec.trigger_signature
            state.last_spread_recommendation = event.verdict["spread_recommendation"]
    except Exception:
        logger.exception("Spread recommendation failed")

    state.last_verdict = event.verdict
    t0 = _time.perf_counter()
    await state.broadcaster.publish(event)
    perf["broadcast_ms"] = (_time.perf_counter() - t0) * 1000

    # Auto-trade evaluation — checks all gates, may place a bracket order.
    # Defensive: never let an auto-trade exception break the iter pipeline.
    try:
        from fef_sidecar.engine.auto_trader import maybe_auto_trade
        decision = await maybe_auto_trade(state, event.verdict)
        state.auto_trade_last_block_reason = decision.reason
        if decision.fired and decision.order_result:
            logger.info("auto_trade fired: %s", decision.order_result)
        elif (
            state.auto_trade_enabled
            and event.verdict.get("direction") in ("LONG_CALLS", "LONG_PUTS")
            and decision.reason != "auto_trade disabled"
        ):
            # Auto-trade was on AND verdict was actionable but we blocked it.
            # Write to audit so the user can see WHY without polling status.
            try:
                await SignalIterRepository.write_audit_log(
                    category="auto_trade_block", actor="system",
                    summary=f"BLOCKED {event.verdict.get('direction')} {event.verdict.get('confidence')}: {decision.reason}",
                    details={
                        "direction": event.verdict.get("direction"),
                        "confidence": event.verdict.get("confidence"),
                        "strike_rec": event.verdict.get("strike_rec"),
                        "reason": decision.reason,
                        "reason_code": decision.reason_code,
                    },
                )
            except Exception:
                pass
    except Exception:
        logger.exception("Auto-trade evaluation failed")

    # IMPROVEMENTS #R: evaluate user-defined patterns against this iter
    t0 = _time.perf_counter()
    await _evaluate_patterns(state, inputs, verdict, signal.id, signal.iter_num)
    perf["pattern_eval_ms"] = (_time.perf_counter() - t0) * 1000

    # Tournament: parallel strategies
    t0 = _time.perf_counter()
    variant_verdicts = state.tournament.push(inputs)
    cons = consensus(variant_verdicts)
    await state.broadcaster.publish(
        TournamentEvent(
            iter_num=signal.iter_num,
            spx_price=inputs.spx_price,
            variants=[
                {
                    "name": v.name,
                    "direction": v.direction,
                    "confidence": v.confidence,
                    "reasoning": v.reasoning,
                    "aligned": v.aligned,
                    "absorption": v.absorption,
                }
                for v in variant_verdicts
            ],
            consensus=cons,
        )
    )
    perf["tournament_ms"] = (_time.perf_counter() - t0) * 1000

    # Mock auto-trader (opt-in via env var, default ON when scripted day active)
    auto_trade = os.environ.get(
        "FEF_MOCK_AUTO_TRADE",
        os.environ.get("FEF_MOCK_SCRIPTED_DAY", "false")
    ).lower() in ("true", "1", "yes")
    if auto_trade and signal.id is not None:
        try:
            await maybe_create_ticket(
                state.autotrader, verdict, inputs, signal.id, signal.iter_num
            )
            await advance_pending(state.autotrader, signal.iter_num, inputs.spx_price)
        except Exception:
            logger.exception("Mock auto-trader iteration failed")

    # Market regime detection — push this iter, broadcast updated assessment
    t0 = _time.perf_counter()
    # Honor simulated time of day from the mock day script if present
    sim_minutes = getattr(snap, "simulated_minutes_since_open", None)
    sim_vix = getattr(snap, "simulated_vix", None)
    minutes_for_regime = sim_minutes if sim_minutes is not None else now_minutes_since_open_et()
    vix_for_regime = sim_vix if sim_vix is not None else state.last_vix
    state.regime_detector.push(
        RegimeSample(
            spx_price=inputs.spx_price,
            drift_cum_net_m=inputs.drift_cum_net_m,
            drift_bias=inputs.drift_bias,
            wall_proximity_pts=verdict.nearest_wall_distance,
            aligned=verdict.aligned,
            absorption=verdict.absorption,
            direction=verdict.direction.value,
            confidence=verdict.confidence.value,
            fe_ratio=verdict.fe_ratio,
            minutes_since_open=minutes_for_regime,
            vix=vix_for_regime,
        )
    )
    assessment = state.regime_detector.assess()
    assessment.detected_at_iter = signal.iter_num
    state.last_regime = assessment
    await state.broadcaster.publish(
        RegimeEvent(
            regime=assessment.regime,
            label=assessment.label,
            confidence=assessment.confidence,
            rationale=assessment.rationale,
            recommended_preset_hint=assessment.recommended_preset_hint,
            recommended_patterns=assessment.recommended_patterns,
            bias=assessment.bias,
            sizing=assessment.sizing,
        )
    )
    perf["regime_ms"] = (_time.perf_counter() - t0) * 1000

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


async def _evaluate_patterns(
    state: AppState,
    inputs: Any,
    verdict: Any,
    signal_iter_id: int | None,
    iter_num: int,
) -> None:
    """Walk enabled patterns; fire PatternMatchEvent + log match for each hit."""
    if signal_iter_id is None:
        return

    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import Pattern, PatternMatch

    field_values = extract_fields(
        inputs, verdict, vix=state.last_vix, iter_num=iter_num
    )

    async with get_session() as session:
        patterns = (
            await session.exec(select(Pattern).where(Pattern.enabled == True))  # noqa: E712
        ).all()

        for pattern in patterns:
            matched, _ = evaluate_pattern(pattern.conditions or [], field_values)
            if not matched:
                continue

            from datetime import UTC, datetime

            pattern.match_count += 1
            pattern.last_matched_at = datetime.now(UTC)
            session.add(pattern)

            session.add(
                PatternMatch(
                    pattern_id=pattern.id,
                    signal_iter_id=signal_iter_id,
                    fields_snapshot={
                        k: v for k, v in field_values.items() if v is not None
                    },
                )
            )

            await state.broadcaster.publish(
                PatternMatchEvent(
                    pattern_id=pattern.id,
                    pattern_name=pattern.name,
                    pattern_description=pattern.description,
                    signal_iter_id=signal_iter_id,
                    iter_num=iter_num,
                    spx_price=inputs.spx_price,
                    audio_alert=pattern.audio_alert,
                    toast_alert=pattern.toast_alert,
                )
            )
            logger.info(
                "Pattern matched: '%s' on iter #%d (SPX $%.2f)",
                pattern.name,
                iter_num,
                inputs.spx_price,
            )


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
    except Exception as e:
        logger.warning("Could not fetch option quote for %s: %s", contract, e)
        try:
            await SignalIterRepository.write_audit_log(
                category="option_quote_fail", actor="system",
                summary=f"get_option_quote raised: SPX {expiry} {verdict.strike_rec}{right}: {type(e).__name__}: {e}",
                details={"strike": verdict.strike_rec, "right": right, "expiry": expiry,
                         "error_type": type(e).__name__, "error": str(e)},
            )
        except Exception:
            pass
        return None

    targets = build_option_targets(
        quote=quote, current_spx=inputs.spx_price,
        spx_t1=verdict.t1, spx_t2=verdict.t2, spx_t3=verdict.t3, spx_stop=verdict.stop,
    )
    if targets is None:
        try:
            await SignalIterRepository.write_audit_log(
                category="option_quote_fail", actor="system",
                summary=(
                    f"option quote unusable for SPX {expiry} {verdict.strike_rec}{right}: "
                    f"last={quote.last} bid={quote.bid} ask={quote.ask} delta={quote.delta} "
                    f"(NaN/0 — IBKR didn't return valid pricing for this contract)"
                ),
                details={"strike": verdict.strike_rec, "right": right, "expiry": expiry,
                         "last": quote.last, "bid": quote.bid, "ask": quote.ask,
                         "delta": quote.delta},
            )
        except Exception:
            pass
    return targets


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
        # Full greeks + bid/ask now captured at fire time so backtester can
        # model theta drag + real slippage. Forward-only (expired contracts
        # can't be backfilled).
        "gamma": targets.gamma,
        "vega": targets.vega,
        "theta": targets.theta,
        "iv": targets.iv,
        "bid": targets.bid,
        "ask": targets.ask,
    }


async def bar_stream_loop(state: AppState) -> None:
    symbol = state.active_symbol
    bar_size = os.environ.get("FEF_BAR_SIZE", "1 min")
    logger.info("Starting bar stream loop for %s (%s)", symbol, bar_size)
    while True:
        try:
            await state.ibkr.connect()
            async for bar in state.ibkr.stream_bars(Contract.for_symbol(symbol)):
                state.last_spx_price = bar.close
                # Persist bar for historical chart + backtest replay
                try:
                    await SignalIterRepository.write_bar(
                        symbol=symbol, bar_size=bar_size, timestamp_ms=bar.epoch_ms,
                        open_=bar.open, high=bar.high, low=bar.low, close=bar.close,
                        volume=bar.volume,
                    )
                except Exception:
                    logger.exception("Failed to persist bar")
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
                    prior_close=state.prior_close_by_symbol.get(symbol),
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


# ---------------------------------------------------------------------------
# VIX poll loop — periodic VIX index quote for the top-bar IV regime indicator
# ---------------------------------------------------------------------------


async def vix_poll_loop(state: AppState) -> None:
    """IMPROVEMENTS #M — refresh VIX every FEF_VIX_POLL_INTERVAL_SEC (default 30s).

    Primary source: IBKR ^VIX index quote. Fallback: QuantData iv_rank.last_iv
    (SPX ~30-day ATM IV, same scale as VIX within ~1-2 points). The fallback
    kicks in when the IBKR VIX subscription is unavailable (paper accounts
    often lack it intermittently). When neither source has a value, last_vix
    stays at its prior reading.
    """
    from fef_sidecar.events import StatusEvent
    import time as _time

    interval = float(os.environ.get("FEF_VIX_POLL_INTERVAL_SEC", "30"))
    logger.info("Starting VIX poll loop (interval=%.1fs)", interval)
    while True:
        try:
            vix_value: float | None = None
            vix_ts_ms: int | None = None
            source: str | None = None

            quote = await state.ibkr.get_last_quote(Contract.vix_index())
            if quote and quote.last and quote.last > 0:
                vix_value = float(quote.last)
                vix_ts_ms = int(quote.timestamp.timestamp() * 1000)
                source = "ibkr"
            else:
                # Fallback to QuantData iv_rank proxy. last_snapshot is the most
                # recent MarketSnapshot from the QuantData poll loop, refreshed
                # every ~15s. Its iv_rank field is parsed from options/iv-rank.
                snap = getattr(state, "last_snapshot", None)
                iv = getattr(snap, "iv_rank", None) if snap is not None else None
                proxy = iv.vix_proxy if iv is not None else None
                if proxy is not None and proxy > 0:
                    vix_value = float(proxy)
                    vix_ts_ms = int(_time.time() * 1000)
                    source = "quantdata_iv_proxy"

            if vix_value is not None:
                state.last_vix = vix_value
                try:
                    await SignalIterRepository.write_vix(
                        timestamp_ms=vix_ts_ms or int(_time.time() * 1000),
                        value=vix_value,
                    )
                except Exception:
                    logger.exception("Failed to persist VIX sample")
                # Broadcast updated status with VIX value
                await state.broadcaster.publish(
                    StatusEvent(
                        quantdata=type(state.quantdata).__name__,
                        ibkr=state.ibkr_status if state.ibkr_status != "unknown" else "connected",
                        engine="running" if state.loop_task is not None else "stopped",
                        history_size=state.engine.history_size,
                        vix=state.last_vix,
                    )
                )
                if source == "quantdata_iv_proxy":
                    logger.debug("VIX from QuantData iv_rank proxy: %.2f", vix_value)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("VIX poll iteration failed")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Account poll loop — live broker scorecard (realized + unrealized P/L)
# ---------------------------------------------------------------------------


async def account_poll_loop(state: AppState) -> None:
    """Poll IBKR account summary every FEF_ACCOUNT_POLL_INTERVAL_SEC (default 15s).

    IBKR is the source of truth for P/L — no need to reconcile from our
    TradeTicket records. Broadcasts AccountEvent on every poll.
    """
    from fef_sidecar.events import AccountEvent

    interval = float(os.environ.get("FEF_ACCOUNT_POLL_INTERVAL_SEC", "15"))
    logger.info("Starting account poll loop (interval=%.1fs)", interval)
    while True:
        try:
            summary = await state.ibkr.get_account_summary()
            if summary is None:
                await asyncio.sleep(interval)
                continue
            state.last_account = summary
            await state.broadcaster.publish(
                AccountEvent(
                    timestamp_ms=summary.timestamp_ms,
                    account=summary.account,
                    net_liquidation=summary.net_liquidation,
                    buying_power=summary.buying_power,
                    available_funds=summary.available_funds,
                    daily_pnl=summary.daily_pnl,
                    realized_pnl=summary.realized_pnl,
                    unrealized_pnl=summary.unrealized_pnl,
                    gross_position_value=summary.gross_position_value,
                    open_positions_count=summary.open_positions_count,
                    open_options_count=summary.open_options_count,
                    currency=summary.currency,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Account poll iteration failed")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Microstructure loop — sub-second OFI / AVI / Microprice signal
# ---------------------------------------------------------------------------


async def hft_loop(state: AppState) -> None:
    """Combined HFT signal loop — realized vol + quote stuffing + L2 depth.

    Runs at HFT_LOOP_INTERVAL_SEC (default 0.25s = 4Hz). Event-driven on top
    of that: if vol_regime, stuffing_severity, or book_direction flips
    between polls, fires immediately instead of waiting.
    """
    import asyncio as _asyncio
    import random as _random
    import time as _time

    from fef_sidecar.engine.depth import BookLevel, DepthState, assess as depth_assess, update_book
    from fef_sidecar.engine.quote_stuffing import StuffingState, assess as stuffing_assess, push_quote as stuffing_push_quote, push_trade as stuffing_push_trade
    from fef_sidecar.engine.realized_vol import RealizedVolState, assess as vol_assess, push_tick as vol_push
    from fef_sidecar.events import HFTEvent
    from fef_sidecar.ibkr.live import LiveIBKRProvider

    interval = float(os.environ.get("HFT_LOOP_INTERVAL_SEC", "0.25"))
    persist_interval = float(os.environ.get("FEF_HFT_PERSIST_SEC", "1.0"))
    last_persist_ms = [0]  # mutable closure ref so inner block can update
    rng = _random.Random()

    state.vol_state = RealizedVolState()
    state.stuffing_state = StuffingState()
    state.depth_state = DepthState()

    cancel_depth = None
    if isinstance(state.ibkr, LiveIBKRProvider):
        # Live: subscribe to L2 depth on the active symbol (or proxy)
        from fef_sidecar.ibkr.models import Contract
        depth_symbol = {"SPX": "ES", "SPY": "ES", "QQQ": "NQ", "IWM": "RTY"}.get(
            state.active_symbol, state.active_symbol)
        try:
            await state.ibkr.connect()

            def _on_book(ts, b, a):
                update_book(state.depth_state, b, a, ts)
                state.depth_update_count_live += 1

            cancel_depth = await state.ibkr.subscribe_depth(
                Contract.for_symbol(depth_symbol), _on_book,
            )
            logger.info("HFT loop subscribed to LIVE depth on %s", depth_symbol)

            # Watchdog: if no depth events arrive within 30s, your account
            # likely lacks the CME depth data package. Log a clear warning
            # so you know the panel showing 0/BALANCED is data-permission,
            # not a code bug.
            async def _depth_subscription_watchdog():
                await asyncio.sleep(30)
                if state.depth_update_count_live == 0:
                    logger.warning(
                        "L2 depth subscription on %s delivered 0 events in 30s. "
                        "Likely missing CME depth data subscription on this IBKR "
                        "account. HFT panel L2 Depth will show 0/BALANCED until "
                        "the subscription is added.", depth_symbol,
                    )
                else:
                    logger.info(
                        "L2 depth subscription on %s healthy: %d events in 30s",
                        depth_symbol, state.depth_update_count_live,
                    )
            asyncio.create_task(_depth_subscription_watchdog(), name="depth-watchdog")
        except Exception:
            logger.exception("Live depth subscription failed — using synth")
            cancel_depth = None
    else:
        logger.info("HFT loop in MOCK mode (synth vol/stuffing/depth, interval=%.2fs)", interval)

    # Track last direction strings for event-driven fire detection
    last_vol_regime = "NORMAL"
    last_stuffing_sev = "NORMAL"
    last_book_dir = "BALANCED"

    try:
        while True:
            try:
                now_ms = int(_time.time() * 1000)
                spx = state.last_spx_price or 7400.0

                # Mock: synthesize new tick + quote + book
                if cancel_depth is None:
                    # Tick: random walk
                    tick_price = round(spx + rng.uniform(-0.5, 0.5), 2)
                    vol_push(state.vol_state, now_ms, tick_price)
                    # Quote: 70% of polls add a quote update, 20% add a trade
                    stuffing_push_quote(state.stuffing_state, now_ms)
                    if rng.random() < 0.2:
                        stuffing_push_trade(state.stuffing_state, now_ms)
                    # Synth book — 10 levels each side, sizes biased by recent direction
                    bids = [BookLevel(price=round(spx - i * 0.25, 2),
                                      size=float(rng.randint(50, 500)))
                            for i in range(1, 11)]
                    asks = [BookLevel(price=round(spx + i * 0.25, 2),
                                      size=float(rng.randint(50, 500)))
                            for i in range(1, 11)]
                    update_book(state.depth_state, bids, asks, now_ms)
                else:
                    # Live: push current spx for vol calc. Stuffing state is
                    # already being fed from microstructure_loop's tick
                    # subscription (real quotes + real trades) — DON'T push
                    # synthetic quote ticks here or we'd double-count.
                    vol_push(state.vol_state, now_ms, spx)

                vol = vol_assess(state.vol_state, now_ms)
                stuff = stuffing_assess(state.stuffing_state, now_ms)
                depth = depth_assess(state.depth_state, now_ms)

                # Event-driven: fire on direction change OR poll interval
                flips = []
                if vol.regime != last_vol_regime:
                    flips.append(f"vol_regime:{last_vol_regime}→{vol.regime}")
                    last_vol_regime = vol.regime
                if stuff.severity != last_stuffing_sev:
                    flips.append(f"stuffing:{last_stuffing_sev}→{stuff.severity}")
                    last_stuffing_sev = stuff.severity
                if depth.direction != last_book_dir:
                    flips.append(f"book:{last_book_dir}→{depth.direction}")
                    last_book_dir = depth.direction

                # Composite direction: book trumps vol unless stuffing is high
                if stuff.severity == "STUFFING":
                    composite = "FADE"  # stuffing often precedes reversion
                elif depth.direction == "BID_HEAVY" and vol.regime != "EXPANSION":
                    composite = "BULLISH"
                elif depth.direction == "ASK_HEAVY" and vol.regime != "EXPANSION":
                    composite = "BEARISH"
                elif vol.regime == "EXPANSION":
                    composite = "VOLATILE"
                else:
                    composite = "NEUTRAL"

                fired_on = ",".join(flips) if flips else "poll"

                # Data source determination for the HFT panel badges:
                is_live = isinstance(state.ibkr, LiveIBKRProvider)
                vol_source = "live_sampled" if is_live else "synth"
                if is_live:
                    # If the tick subscription is delivering quotes + trades,
                    # stuffing is live. If quotes but no trades, EMPTY
                    # (tick-by-tick subscription missing for trades).
                    if state.tick_quote_count_live > 0 and state.tick_trade_count_live > 0:
                        stuffing_source = "live"
                    elif state.tick_quote_count_live > 0:
                        stuffing_source = "empty_trades"
                    else:
                        stuffing_source = "empty"
                    if state.depth_update_count_live > 0:
                        depth_source = "live"
                    else:
                        depth_source = "empty"
                else:
                    stuffing_source = "synth"
                    depth_source = "synth"

                await state.broadcaster.publish(HFTEvent(
                    timestamp_ms=now_ms,
                    short_vol_pts=vol.short_vol_pts, long_vol_pts=vol.long_vol_pts,
                    vol_ratio=vol.ratio, vol_regime=vol.regime,
                    cancel_to_trade_ratio=stuff.cancel_to_trade_ratio,
                    quote_intensity_per_sec=stuff.quote_intensity_per_sec,
                    stuffing_severity=stuff.severity,
                    book_imbalance=depth.imbalance, book_imbalance_5lvl=depth.imbalance_5lvl,
                    book_direction=depth.direction, book_confidence=depth.confidence,
                    weighted_mid_price=depth.weighted_mid_price, book_spread=depth.spread,
                    composite_direction=composite, fired_on=fired_on,
                    vol_source=vol_source, stuffing_source=stuffing_source,
                    depth_source=depth_source,
                ))

                # Cache latest state on AppState so SignalIter can snapshot
                # at fire time (correlates HFT to each signal).
                state.last_hft_state = {
                    "timestamp_ms": now_ms,
                    "short_vol_pts": vol.short_vol_pts, "long_vol_pts": vol.long_vol_pts,
                    "vol_ratio": vol.ratio, "vol_regime": vol.regime,
                    "cancel_to_trade_ratio": stuff.cancel_to_trade_ratio,
                    "quote_intensity_per_sec": stuff.quote_intensity_per_sec,
                    "stuffing_severity": stuff.severity,
                    "book_imbalance": depth.imbalance,
                    "book_imbalance_5lvl": depth.imbalance_5lvl,
                    "book_direction": depth.direction,
                    "book_confidence": depth.confidence,
                    "weighted_mid_price": depth.weighted_mid_price,
                    "book_spread": depth.spread,
                    "composite_direction": composite,
                }

                # Persist HFTSample at FEF_HFT_PERSIST_SEC cadence (default 1s)
                if (now_ms - last_persist_ms[0]) >= persist_interval * 1000:
                    last_persist_ms[0] = now_ms
                    try:
                        from fef_sidecar.db.models import HFTSample
                        from fef_sidecar.db.engine import get_session as _gs
                        sample = HFTSample(
                            symbol=state.active_symbol,
                            timestamp_ms=now_ms,
                            short_vol_pts=vol.short_vol_pts, long_vol_pts=vol.long_vol_pts,
                            vol_ratio=vol.ratio, vol_regime=vol.regime,
                            cancel_to_trade_ratio=stuff.cancel_to_trade_ratio,
                            quote_intensity_per_sec=stuff.quote_intensity_per_sec,
                            stuffing_severity=stuff.severity,
                            book_imbalance=depth.imbalance,
                            book_imbalance_5lvl=depth.imbalance_5lvl,
                            book_direction=depth.direction,
                            book_confidence=depth.confidence,
                            weighted_mid_price=depth.weighted_mid_price,
                            book_spread=depth.spread,
                            composite_direction=composite,
                            vol_source=vol_source,
                            stuffing_source=stuffing_source,
                            depth_source=depth_source,
                        )
                        async with _gs() as _s:
                            _s.add(sample)
                            await _s.commit()
                    except Exception:
                        logger.exception("HFTSample persist failed")
            except _asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("HFT iteration failed")
            await _asyncio.sleep(interval)
    finally:
        if cancel_depth is not None:
            try:
                await cancel_depth()
            except Exception:
                logger.exception("Depth cancel failed")


async def microstructure_loop(state: AppState) -> None:
    """Continuously broadcast a microstructure signal every
    FEF_MICRO_INTERVAL_SEC (default 0.5s).

    Live mode (LiveIBKRProvider): subscribes to reqTickByTickData on the
    active symbol's underlying (uses SPY when active_symbol == SPX since
    SPX is an index without a trade tape). Real bid/ask/trade events
    feed the OFI/AVI/microprice calculations.

    Mock mode: synthesizes plausible quote + trade events around the
    current price anchor so the UI populates without a live feed.
    """
    import asyncio as _asyncio
    import random as _random
    import time as _time

    from fef_sidecar.engine.microstructure import (
        QuoteUpdate,
        TradePrint,
        assess,
        classify_trade,
        push_quote,
        push_trade,
    )
    from fef_sidecar.events import MicrostructureEvent
    from fef_sidecar.ibkr.live import LiveIBKRProvider

    interval = float(os.environ.get("FEF_MICRO_INTERVAL_SEC", "0.5"))
    persist_every_ms = int(float(os.environ.get("FEF_MICRO_PERSIST_SEC", "1.0")) * 1000)
    last_persisted_ms = 0
    rng = _random.Random()

    # Both callbacks ALSO feed the stuffing state (single tick stream,
    # multiple consumers). HFT loop reads state.stuffing_state for its
    # cancel-to-trade ratio — without this, live stuffing detection was
    # broken (only quotes, never trades).
    from fef_sidecar.engine.quote_stuffing import (
        push_quote as _stuff_push_quote,
        push_trade as _stuff_push_trade,
    )

    def _on_quote(ts_ms: int, bid: float, ask: float, bs: float, as_: float) -> None:
        push_quote(state.microstructure_state, QuoteUpdate(
            timestamp_ms=ts_ms, bid=bid, ask=ask, bid_size=bs, ask_size=as_,
        ))
        if state.stuffing_state is not None:
            _stuff_push_quote(state.stuffing_state, ts_ms)
        state.tick_quote_count_live += 1

    def _on_trade(ts_ms: int, price: float, size: float, aggressor: str) -> None:
        push_trade(state.microstructure_state, TradePrint(
            timestamp_ms=ts_ms, price=price, size=size, aggressor=aggressor,  # type: ignore[arg-type]
        ))
        if state.stuffing_state is not None:
            _stuff_push_trade(state.stuffing_state, ts_ms)
        state.tick_trade_count_live += 1

    # Microstructure proxy mapping — futures lead their cash/ETF counterparts.
    # ES is the source of truth for S&P 500 order flow (24h tape, consolidated
    # CME book, professional liquidity); SPY is fragmented retail tape across
    # ~15 exchanges. Same logic for NQ vs QQQ and RTY vs IWM. SPX is an index
    # with no tape so we MUST use a tradable proxy.
    MICRO_PROXY = {
        "SPX": "ES",
        "SPY": "ES",
        "QQQ": "NQ",
        "IWM": "RTY",
    }
    cancel_live = None
    if isinstance(state.ibkr, LiveIBKRProvider):
        micro_symbol = MICRO_PROXY.get(state.active_symbol, state.active_symbol)
        try:
            await state.ibkr.connect()
            cancel_live = await state.ibkr.subscribe_tick_by_tick(
                Contract.for_symbol(micro_symbol), _on_quote, _on_trade,
            )
            logger.info("Microstructure loop subscribed to LIVE %s ticks (interval=%.2fs)", micro_symbol, interval)
        except Exception:
            logger.exception("Live microstructure subscription failed — falling back to synth")
            cancel_live = None
    else:
        logger.info("Microstructure loop in MOCK mode (synthesizing ticks, interval=%.2fs)", interval)

    try:
        while True:
            try:
                if cancel_live is None:
                    # MOCK: synthesize a quote + a trade per tick interval
                    spx = state.last_spx_price or 7400.0
                    now_ms = int(_time.time() * 1000)
                    spread = round(rng.uniform(0.05, 0.25), 2)
                    bid = round(spx - spread / 2, 2)
                    ask = round(spx + spread / 2, 2)
                    bid_size = float(rng.randint(50, 500))
                    ask_size = float(rng.randint(50, 500))
                    push_quote(state.microstructure_state, QuoteUpdate(
                        timestamp_ms=now_ms, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size,
                    ))
                    trade_price = round(spx + rng.uniform(-spread / 2, spread / 2), 2)
                    trade_size = float(rng.randint(1, 50))
                    agg = classify_trade(trade_price, bid, ask)
                    push_trade(state.microstructure_state, TradePrint(
                        timestamp_ms=now_ms, price=trade_price, size=trade_size, aggressor=agg,
                    ))

                now_ms = int(_time.time() * 1000)
                sig = assess(state.microstructure_state, now_ms)
                # Cache for SignalIter snapshot at fire time
                state.last_microstructure_state = {
                    "timestamp_ms": sig.timestamp_ms,
                    "ofi_window": sig.ofi_window,
                    "avi_pct": sig.avi_pct,
                    "microprice": sig.microprice,
                    "microprice_deviation_bp": sig.microprice_deviation_bp,
                    "direction": sig.direction,
                    "confidence": sig.confidence,
                }
                await state.broadcaster.publish(MicrostructureEvent(
                    timestamp_ms=sig.timestamp_ms,
                    ofi_window=sig.ofi_window,
                    avi_pct=sig.avi_pct,
                    aggressive_buy_volume=sig.aggressive_buy_volume,
                    aggressive_sell_volume=sig.aggressive_sell_volume,
                    microprice=sig.microprice,
                    last_trade_price=sig.last_trade_price,
                    microprice_deviation_bp=sig.microprice_deviation_bp,
                    direction=sig.direction,
                    confidence=sig.confidence,
                    reasoning=sig.reasoning,
                    quote_count=sig.quote_count,
                    trade_count=sig.trade_count,
                ))
                # Persist sampled (every FEF_MICRO_PERSIST_SEC, default 1s)
                if now_ms - last_persisted_ms >= persist_every_ms:
                    try:
                        proxy = MICRO_PROXY.get(state.active_symbol, state.active_symbol) if cancel_live else state.active_symbol
                        await SignalIterRepository.write_microstructure(
                            symbol=proxy, timestamp_ms=sig.timestamp_ms,
                            ofi_window=sig.ofi_window, avi_pct=sig.avi_pct,
                            aggressive_buy_volume=sig.aggressive_buy_volume,
                            aggressive_sell_volume=sig.aggressive_sell_volume,
                            microprice=sig.microprice,
                            last_trade_price=sig.last_trade_price,
                            microprice_deviation_bp=sig.microprice_deviation_bp,
                            direction=sig.direction, confidence=sig.confidence,
                        )
                        last_persisted_ms = now_ms
                    except Exception:
                        logger.exception("Failed to persist microstructure sample")
            except _asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Microstructure iteration failed")
            await _asyncio.sleep(interval)
    finally:
        if cancel_live is not None:
            try:
                await cancel_live()
            except Exception:
                logger.exception("Microstructure live cancel failed")


async def tick_stream_loop(state: AppState) -> None:
    """Stream last-price ticks. Watchdog: if no tick arrives within
    TICK_STALL_THRESHOLD_SEC we treat the IBKR subscription as wedged
    and force a reconnect — silent stalls used to leave the Live Quote
    panel frozen until manual app restart.
    """
    import time as _t
    symbol = state.active_symbol
    stall_threshold = int(os.environ.get("FEF_TICK_STALL_THRESHOLD_SEC", "30"))
    logger.info("Starting tick stream loop for %s (stall watchdog=%ds)", symbol, stall_threshold)
    while True:
        try:
            await state.ibkr.connect()
            async def _stream():
                async for q in state.ibkr.stream_last_price(Contract.for_symbol(symbol)):
                    yield q
            agen = _stream()
            try:
                while True:
                    try:
                        quote = await asyncio.wait_for(
                            agen.__anext__(), timeout=stall_threshold,
                        )
                    except (asyncio.TimeoutError, TimeoutError):
                        logger.warning(
                            "Tick stream stalled for >%ds on %s — forcing reconnect",
                            stall_threshold, symbol,
                        )
                        raise
                    except StopAsyncIteration:
                        logger.info("Tick stream generator ended — restarting")
                        break
                    state.last_spx_price = quote.last
                    state.last_live_tick_spx = quote.last
                    state.last_live_tick_ms = int(quote.timestamp.timestamp() * 1000)
                    event = TickEvent(
                        timestamp_ms=int(quote.timestamp.timestamp() * 1000),
                        spx_price=quote.last, bar=None,
                        bid=quote.bid, ask=quote.ask,
                        bid_size=quote.bid_size, ask_size=quote.ask_size,
                        symbol=symbol,
                        prior_close=state.prior_close_by_symbol.get(symbol),
                    )
                    await state.broadcaster.publish(event)
            finally:
                try:
                    await agen.aclose()
                except Exception:
                    pass
        except asyncio.CancelledError:
            logger.info("Tick stream cancelled — exiting")
            raise
        except Exception:
            logger.exception("Tick stream failed — reconnecting in 5s")
            # Force a fresh main IBKR connection so the next stream_last_price
            # subscription gets a clean reqMktData slot.
            try:
                if hasattr(state.ibkr, "_ib") and state.ibkr._ib is not None:  # type: ignore[attr-defined]
                    state.ibkr._ib.disconnect()  # type: ignore[attr-defined]
            except Exception:
                pass
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

    # Wait for the initial connect race (bar/tick/quantdata loops all calling
    # connect() simultaneously) to settle before starting health checks.
    # Without this, ping fires while ib_async is mid-connect and returns
    # False → forces a disconnect that fights with the in-flight connect,
    # cascading into a never-stable state.
    await asyncio.sleep(interval)

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
            # Keep state.account_class / account_id in sync with the provider.
            # The provider populates them on connect; mirroring on state makes
            # them accessible to read paths (UI / audit) without a provider ref.
            state.account_class = getattr(state.ibkr, "account_class", "unknown") or "unknown"
            state.account_id = getattr(state.ibkr, "account_id", "") or ""

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
