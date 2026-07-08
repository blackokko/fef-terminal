"""Live pattern trigger — confirmed-bar pattern fires into the advisory
surface and (opt-in) the auto-trader.

Evaluates the SAME detectors as GET /chart/pattern_overlay (engine/
pattern_overlay.py — one source of truth for thresholds, seeded from
UIConfig pattern_* fields) on CONFIRMED bars only: 5m/15m/30m/4h streams
resampled from the 1-min capture base plus the stored daily ("1D") bars
(see TIMEFRAMES / FEF_PATTERN_TIMEFRAMES). A bar is confirmed when its
close time is in the past — no intrabar flicker fires.

Fire handling, per directional hit on a newly confirmed bar:
  1. ALWAYS append an audit_log row (category='pattern_overlay') — the
     advisory/history surface. Neutral hits (inside coil / doji) are
     per-bar states, not fires, and are NOT logged.
  2. If the fire's trigger type is armed in state.auto_trade_trigger_types
     (i.e. the user applied a preset like "Pattern signals v1"):
       - write a SignalIter row (reasoning "PATTERN: <kind> <tf> ...") so
         the fire is fully auditable in the app's signal history, and
       - offer it to maybe_auto_trade(), which applies EVERY existing
         guardrail unchanged (enable toggle, FEF_LIVE_TRADING env,
         calendar pause, per-symbol cool-down + session cap, option
         pricing sanity, duplicate-parent guard, qty caps).
     When the trigger types are NOT armed (the default), fires never touch
     signal_iter — daily-review / expectancy metrics stay clean.

Trigger types:
    flag_bull / flag_bear       — measured-move flag breakouts
    pattern_bull / pattern_bear — candle patterns, further filtered by
                                  UIConfig.pattern_trade_kinds (default
                                  ["engulfing"]).

Strike/targets: build_plan() — the same wall-anchored geometry FEF fires
use — so downstream exit watchers behave identically. The flag's measured
target is recorded in reasoning + extras (advisory), not as the bracket T1.

Trade wiring is SPX-only (the option pipeline is SPX 0DTE); other active
symbols still get advisory audit rows.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from fef_sidecar.config_store import UIConfig, load_config
from fef_sidecar.db import FEFConfidence, FEFDirection, SignalIterRepository
from fef_sidecar.engine.pattern_overlay import (
    PatternHit,
    PatternOverlayConfig,
    detect_candles,
    detect_flags,
    resample_bars,
)
from fef_sidecar.engine.strikes import build_plan

if TYPE_CHECKING:
    from fef_sidecar.state import AppState

logger = logging.getLogger(__name__)

# Confirmed-bar grace: a bar counts as closed this many ms after its
# nominal end so a laggy final update can't be evaluated mid-flight.
_CONFIRM_GRACE_MS = 2_000

# (label, minutes) streams evaluated each cycle, resampled from the 1-min
# capture base. "1D" reads the stored daily-bar stream instead of resampling.
# USER DECISION 2026-07-02: 1-min patterns are OFF for trading — a 1m signal
# is stale before the 300s trade cooldown clears (proven by the first live
# pattern fire: 1m engulf loss, then the correct opposite 1m signal arrived
# 2 min later and was cooldown-blocked). Trade timeframes start at 5m.
# Overridable via FEF_PATTERN_TIMEFRAMES (comma list from: 1m,5m,15m,30m,1h,4h,1D).
_TF_MINUTES: dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1D": 1440,
}
_DEFAULT_TIMEFRAMES = "5m,15m,30m,4h,1D"


def _parse_timeframes() -> tuple[tuple[str, int], ...]:
    raw = os.environ.get("FEF_PATTERN_TIMEFRAMES", _DEFAULT_TIMEFRAMES)
    out: list[tuple[str, int]] = []
    for tok in raw.split(","):
        t = tok.strip()
        key = "1D" if t.upper() == "1D" else t.lower()
        if key in _TF_MINUTES and key not in [lbl for lbl, _ in out]:
            out.append((key, _TF_MINUTES[key]))
    return tuple(out) if out else (("5m", 5),)


TIMEFRAMES: tuple[tuple[str, int], ...] = _parse_timeframes()

# Directional candle-pattern kinds -> trade direction. Flags handled apart.
_CANDLE_DIRECTION: dict[str, str] = {
    "engulf_bull": "LONG_CALLS", "engulf_bear": "LONG_PUTS",
    "hammer": "LONG_CALLS", "shooting_star": "LONG_PUTS",
    "soldiers": "LONG_CALLS", "crows": "LONG_PUTS",
    "morning_star": "LONG_CALLS", "evening_star": "LONG_PUTS",
}

# Candle kind -> family name used in UIConfig.pattern_trade_kinds.
_KIND_FAMILY: dict[str, str] = {
    "engulf_bull": "engulfing", "engulf_bear": "engulfing",
    "hammer": "hammer", "shooting_star": "hammer",
    "soldiers": "soldiers", "crows": "soldiers",
    "morning_star": "star", "evening_star": "star",
}

PATTERN_TRIGGER_TYPES = ("flag_bull", "flag_bear", "pattern_bull", "pattern_bear")


def overlay_config_from_ui(cfg: UIConfig, *, include_doji: bool = False) -> PatternOverlayConfig:
    """Map UIConfig pattern_* fields onto the shared detector config."""
    return PatternOverlayConfig(
        show_doji=include_doji,
        hammer_wick_frac=cfg.pattern_hammer_wick_frac,
        hammer_other_wick_max=cfg.pattern_hammer_other_wick_max,
        soldier_body_frac=cfg.pattern_soldier_body_frac,
        star_mid_body_max=cfg.pattern_star_mid_body_max,
        doji_body_max=cfg.pattern_doji_body_max,
        pole_bars=cfg.pattern_pole_bars,
        pole_min_pts=cfg.pattern_pole_min_pts,
        flag_frac=cfg.pattern_flag_frac,
        flag_min_bars=cfg.pattern_flag_min_bars,
        flag_max_bars=cfg.pattern_flag_max_bars,
        refire_cooldown_bars=cfg.pattern_refire_cooldown_bars,
        pivot_strength=cfg.pattern_pivot_strength,
    )


def fire_trigger_type(hit: PatternHit) -> str | None:
    """Trigger-type name for a hit, or None for non-directional hits."""
    if hit.kind in ("flag_bull", "flag_bear"):
        return hit.kind
    d = _CANDLE_DIRECTION.get(hit.kind)
    if d is None:
        return None
    return "pattern_bull" if d == "LONG_CALLS" else "pattern_bear"


def fire_direction(hit: PatternHit) -> str | None:
    if hit.kind == "flag_bull":
        return "LONG_CALLS"
    if hit.kind == "flag_bear":
        return "LONG_PUTS"
    return _CANDLE_DIRECTION.get(hit.kind)


def is_trade_eligible_kind(hit: PatternHit, ui_cfg: UIConfig) -> bool:
    """Flags are always trade-eligible (their trigger types gate them);
    candle patterns must also be in pattern_trade_kinds."""
    if hit.kind in ("flag_bull", "flag_bear"):
        return True
    family = _KIND_FAMILY.get(hit.kind)
    return family is not None and family in (ui_cfg.pattern_trade_kinds or [])


def confirmed_bars(bars: list[dict[str, Any]], minutes: int, now_ms: int) -> list[dict[str, Any]]:
    """Bars whose nominal close (start + minutes) is at least the grace
    period in the past."""
    horizon = now_ms - _CONFIRM_GRACE_MS
    span = minutes * 60_000
    return [b for b in bars if int(b["timestamp_ms"]) + span <= horizon]


def collect_new_fires(
    bars: list[dict[str, Any]],
    cfg: PatternOverlayConfig,
    after_ms: int,
) -> list[PatternHit]:
    """Directional pattern + flag hits on bars strictly newer than after_ms.

    Detectors run over the whole confirmed window (flags need pole/coil
    history and the refire cooldown), then hits are filtered to new bars."""
    hits = detect_candles(bars, cfg) + detect_flags(bars, cfg)
    out = [
        h for h in hits
        if h.timestamp_ms > after_ms and fire_direction(h) is not None
    ]
    out.sort(key=lambda h: (h.timestamp_ms, h.kind))
    return out


async def _audit_fire(state: AppState, hit: PatternHit, tf_label: str, symbol: str) -> None:
    direction = fire_direction(hit) or "NEUTRAL"
    summary = f"{hit.kind} {tf_label} {symbol} @ {hit.price:.2f}"
    if hit.target is not None:
        summary += f" -> target {hit.target:.2f}"
    details: dict[str, Any] = {
        "kind": hit.kind,
        "timeframe": tf_label,
        "symbol": symbol,
        "bar_timestamp_ms": hit.timestamp_ms,
        "price": hit.price,
        "direction": direction,
        "label": hit.label,
    }
    if hit.target is not None:
        details["measured_target"] = hit.target
    if hit.zone is not None:
        details["flag_zone"] = hit.zone
    await SignalIterRepository.write_audit_log(
        category="pattern_overlay", actor="system",
        summary=summary, details=details,
    )


async def _maybe_trade_fire(
    state: AppState,
    hit: PatternHit,
    tf_label: str,
    symbol: str,
    ui_cfg: UIConfig,
) -> None:
    """Signal persistence + auto-trade offer for one armed pattern fire."""
    from fef_sidecar.engine.auto_trader import maybe_auto_trade
    # Lazy import: loop.py imports are heavy and loop.py must stay import-
    # safe from here (no circular import at module load).
    from fef_sidecar.loop import (
        _current_account_class,
        _current_provider_mode,
        _today_et,
        _walls_json,
        fetch_option_targets,
        _option_pricing_dict,
    )

    direction_value = fire_direction(hit)
    trigger_type = fire_trigger_type(hit)
    if direction_value is None or trigger_type is None:
        return

    direction = FEFDirection.LONG_CALLS if direction_value == "LONG_CALLS" else FEFDirection.LONG_PUTS

    # Freshest spot: live tick if <60s old, else the fire bar's close.
    now_ms = int(time.time() * 1000)
    spot = hit.price
    if state.last_live_tick_spx > 0 and (now_ms - state.last_live_tick_ms) < 60_000:
        spot = state.last_live_tick_spx

    # Wall-anchored strike/target geometry — identical to FEF fires so the
    # exit watchers treat pattern positions the same way.
    inputs = None
    try:
        if state.last_snapshot is not None:
            from fef_sidecar.engine.fef import snapshot_to_inputs
            inputs = snapshot_to_inputs(state.last_snapshot)  # type: ignore[arg-type]
    except Exception:
        inputs = None
    plan = build_plan(
        spot, direction,
        top_gex_walls=(inputs.top_gex_walls if inputs else []),
        top_dex_walls=(inputs.top_dex_walls if inputs else []),
        ntm_max_pain=(inputs.ntm_max_pain if inputs else None),
    )

    option_targets = None
    try:
        option_targets = await fetch_option_targets(
            state,
            direction_value=direction_value,
            strike_rec=plan.strike_rec,
            t1=plan.t1, t2=plan.t2, t3=plan.t3, stop=plan.stop,
            spx_price=spot,
        )
    except Exception:
        logger.exception("pattern trigger: option targets fetch failed")

    reasoning = (
        f"PATTERN: {hit.kind} {tf_label} — {hit.label} @ {hit.price:.2f}"
        + (f", measured target {hit.target:.2f}" if hit.target is not None else "")
    )
    option_pricing = _option_pricing_dict(option_targets)

    signal = await SignalIterRepository.write_iter(
        spx_price=spot,
        direction=direction,
        confidence=FEFConfidence.LOW,
        reasoning=reasoning,
        drift_cum_net_m=(inputs.drift_cum_net_m if inputs else 0.0),
        drift_bias=(inputs.drift_bias if inputs else "NEUTRAL"),
        ntm_max_pain=(inputs.ntm_max_pain if inputs else 0.0),
        raw_max_pain=(inputs.raw_max_pain if inputs else 0.0),
        top_gex_walls=_walls_json(inputs.top_gex_walls) if inputs else [],
        top_dex_walls=_walls_json(inputs.top_dex_walls) if inputs else [],
        extras={
            "source": "pattern",
            "trigger_type": trigger_type,
            "pattern_trigger": {
                "kind": hit.kind,
                "timeframe": tf_label,
                "bar_timestamp_ms": hit.timestamp_ms,
                "bar_close": hit.price,
                "measured_target": hit.target,
                "flag_zone": hit.zone,
            },
            "option_pricing": option_pricing,
            "account_class": _current_account_class(state),
            "account_id": getattr(state.ibkr, "account_id", "") or "",
            "vix_at_fire": state.last_vix,
        },
        session_date=_today_et(),
        provider_mode=_current_provider_mode(state),
    )
    logger.info(
        "Pattern fire persisted: %s (%s) iter #%d strike %.0f",
        hit.kind, tf_label, signal.iter_num, plan.strike_rec,
    )

    verdict_dict: dict[str, Any] = {
        "direction": direction_value,
        "confidence": "LOW",
        "reasoning": reasoning,
        "strike_rec": plan.strike_rec,
        "t1": plan.t1, "t2": plan.t2, "t3": plan.t3, "stop": plan.stop,
        "option_pricing": option_pricing,
        "suggested_qty": 5,  # LOW-confidence engine default; preset qty_override wins
        "trigger_type": trigger_type,
    }
    try:
        decision = await maybe_auto_trade(
            state, verdict_dict, symbol=symbol, source="auto_pattern",
            # Counterfactual gates (floor/cap) tag the row persisted above.
            signal_iter_id=signal.id,
        )
        state.auto_trade_last_block_reason = decision.reason
        if decision.fired:
            logger.warning("PATTERN AUTO-TRADE fired: %s -> %s", hit.kind, decision.order_result)
        elif state.auto_trade_enabled:
            await SignalIterRepository.write_audit_log(
                category="auto_trade_block", actor="system",
                summary=f"BLOCKED PATTERN {direction_value} ({hit.kind} {tf_label}): {decision.reason}",
                details={
                    "source": "auto_pattern",
                    "direction": direction_value,
                    "trigger_type": trigger_type,
                    "kind": hit.kind,
                    "timeframe": tf_label,
                    "strike_rec": plan.strike_rec,
                    "reason": decision.reason,
                    "reason_code": decision.reason_code,
                    "signal_iter_id": signal.id,
                },
                block_reason_code=decision.reason_code,
            )
    except Exception:
        logger.exception("pattern trigger: auto-trade evaluation failed")


async def run_pattern_cycle(
    state: AppState,
    last_processed: dict[str, int],
    *,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """One evaluation pass over the configured confirmed-bar streams.

    Mutates last_processed (per-timeframe newest confirmed bar handled).
    First pass per timeframe ARMS without firing so an app restart
    mid-session doesn't replay the morning's patterns. Returns a summary
    of the fires handled this pass (for logging/tests)."""
    symbol = state.active_symbol
    ui_cfg = load_config()
    cfg = overlay_config_from_ui(ui_cfg)
    # Fetch enough 1-min history for the largest resampled timeframe to
    # clear the 25-confirmed-bar detector floor (4h needs ~2 weeks).
    intraday_minutes = [m for _, m in TIMEFRAMES if m < 1440]
    max_min = max(intraday_minutes) if intraday_minutes else 5
    rows = await SignalIterRepository.query_recent_bars(
        symbol, "1 min", limit=max(400, max_min * 40)
    )
    bars_1m = [
        {"timestamp_ms": r.timestamp_ms, "open": r.open, "high": r.high,
         "low": r.low, "close": r.close, "volume": r.volume}
        for r in rows
    ]
    bars_1d: list[dict[str, Any]] = []
    if any(lbl == "1D" for lbl, _ in TIMEFRAMES):
        drows = await SignalIterRepository.query_recent_bars(symbol, "1 day", limit=60)
        bars_1d = [
            {"timestamp_ms": r.timestamp_ms, "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "volume": r.volume}
            for r in drows
        ]
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    handled: list[dict[str, Any]] = []
    for tf_label, minutes in TIMEFRAMES:
        if tf_label == "1D":
            series = bars_1d
        else:
            series = bars_1m if minutes == 1 else resample_bars(bars_1m, minutes)
        conf = confirmed_bars(series, minutes, now_ms)
        if len(conf) < 25:
            continue
        newest_ms = int(conf[-1]["timestamp_ms"])
        prev = last_processed.get(tf_label)
        if prev is None:
            last_processed[tf_label] = newest_ms  # arm without replay
            continue
        if newest_ms <= prev:
            continue
        fires = collect_new_fires(conf, cfg, prev)
        last_processed[tf_label] = newest_ms
        for hit in fires:
            await _audit_fire(state, hit, tf_label, symbol)
            trigger_type = fire_trigger_type(hit)
            armed = trigger_type in (state.auto_trade_trigger_types or [])
            traded = False
            if (
                armed
                and is_trade_eligible_kind(hit, ui_cfg)
                and symbol == "SPX"  # option trade pipeline is SPX 0DTE only
            ):
                await _maybe_trade_fire(state, hit, tf_label, symbol, ui_cfg)
                traded = True
            handled.append({
                "kind": hit.kind, "timeframe": tf_label,
                "bar_timestamp_ms": hit.timestamp_ms,
                "trigger_type": trigger_type, "armed": armed,
                "offered_to_auto_trader": traded,
            })
    return handled


async def pattern_trigger_loop(state: AppState) -> None:
    """Poll historical_bar for newly CONFIRMED bars on each configured
    timeframe and handle pattern fires. Config is re-read every cycle so
    sensitivity edits via POST /config apply without a restart. Mock mode
    runs regardless of market hours so dev sessions see fires."""
    from fef_sidecar.loop import _in_market_hours_et

    poll_sec = float(os.environ.get("FEF_PATTERN_TRIGGER_POLL_SEC", "20"))
    symbol = state.active_symbol
    is_mock = type(state.ibkr).__name__ == "MockIBKRProvider"
    last_processed: dict[str, int] = {}
    logger.info("Pattern trigger loop started for %s (poll=%.0fs)", symbol, poll_sec)

    while True:
        try:
            if is_mock or _in_market_hours_et():
                fires = await run_pattern_cycle(state, last_processed)
                for f in fires:
                    logger.info(
                        "Pattern fire: %s %s armed=%s offered=%s",
                        f["kind"], f["timeframe"], f["armed"], f["offered_to_auto_trader"],
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("pattern trigger iteration failed")
        await asyncio.sleep(poll_sec)
