"""REST routes — history reads + manual iter trigger."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from fastapi import Body

from fef_sidecar.config_store import UIConfig, load_config, save_config
from fef_sidecar.db import SignalIterRepository
from fef_sidecar.loop import _run_one_iter
from fef_sidecar.state import AppState

router = APIRouter()


def _state(request: Request) -> AppState:
    return request.app.state.fef


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    s = _state(request)
    is_quantdata_mock = type(s.quantdata).__name__ == "MockQuantDataClient"
    is_ibkr_mock = type(s.ibkr).__name__ == "MockIBKRProvider"
    return {
        "quantdata": type(s.quantdata).__name__,
        "ibkr": s.ibkr_status if s.ibkr_status != "unknown" else (
            "connected" if s.ibkr.is_connected else "disconnected"
        ),
        "ibkr_provider": type(s.ibkr).__name__,
        "engine_history_size": s.engine.history_size,
        "last_spx_price": s.last_spx_price,
        "last_iter_num": s.last_iter_num,
        "subscriber_count": s.broadcaster.subscriber_count,
        "all_mock": is_quantdata_mock and is_ibkr_mock,  # IMPROVEMENTS #13
        "last_iter_perf_ms": s.last_iter_perf_ms,  # IMPROVEMENTS #10
        "active_symbol": s.active_symbol,
        "available_symbols": s.available_symbols,
        "live_data_health": {
            "tick_quote_count": s.tick_quote_count_live,
            "tick_trade_count": s.tick_trade_count_live,
            "depth_update_count": s.depth_update_count_live,
        },
    }


@router.get("/diag/depth")
async def diag_depth(request: Request, symbol: str = "ES", wait_seconds: int = 5) -> dict[str, Any]:
    """One-shot diagnostic: subscribe to L2 depth for `wait_seconds`, report
    how many book updates were received. Use this to verify your IBKR account
    has the depth data subscription for the exchange."""
    import asyncio as _asyncio
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from fef_sidecar.ibkr.models import Contract

    s = _state(request)
    if not isinstance(s.ibkr, LiveIBKRProvider):
        return {"ok": False, "error": "Not in live IBKR mode"}

    events: list[dict[str, Any]] = []
    def _on_book(ts, bids, asks):
        events.append({
            "ts": ts, "n_bids": len(bids), "n_asks": len(asks),
            "top_bid": (bids[0].price if bids else None),
            "top_ask": (asks[0].price if asks else None),
        })

    try:
        cancel = await s.ibkr.subscribe_depth(Contract.for_symbol(symbol), _on_book)
        await _asyncio.sleep(wait_seconds)
        await cancel()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok": True, "symbol": symbol, "wait_seconds": wait_seconds,
        "events_received": len(events),
        "events_per_sec": round(len(events) / max(1, wait_seconds), 1),
        "first_event": events[0] if events else None,
        "last_event": events[-1] if events else None,
        "verdict": (
            "SUBSCRIBED — depth feed is live" if len(events) > 0
            else "NOT SUBSCRIBED — IBKR returned 0 events, your account likely lacks the depth data subscription for this exchange"
        ),
    }


@router.get("/config/symbol")
async def get_symbol(request: Request) -> dict[str, Any]:
    s = _state(request)
    return {"active_symbol": s.active_symbol, "available_symbols": s.available_symbols}


@router.post("/config/symbol")
async def set_symbol(request: Request, body: dict[str, str] = Body(...)) -> dict[str, Any]:
    """Switch the active symbol. Cancels in-flight data loops and respawns
    them so the new symbol is in effect within a second or two. The DB
    keeps tagging rows with whichever symbol is active at write time."""
    from fef_sidecar.main import cancel_data_loops, spawn_data_loops

    symbol = body.get("symbol", "").strip().upper()
    s = _state(request)
    if symbol not in s.available_symbols:
        raise HTTPException(400, f"Symbol {symbol!r} not in available_symbols ({s.available_symbols})")
    if symbol == s.active_symbol:
        return {"ok": True, "active_symbol": s.active_symbol, "changed": False}

    s.active_symbol = symbol
    s.last_snapshot = None  # force a fresh QuantData fetch on the new symbol
    await cancel_data_loops(s)
    spawn_data_loops(s)
    # Refresh prior close for the new symbol so day-change % is correct.
    from fef_sidecar.main import refresh_prior_close
    import asyncio as _aio
    _aio.create_task(refresh_prior_close(s), name="refresh-prior-close")
    return {"ok": True, "active_symbol": s.active_symbol, "changed": True}


# IMPROVEMENTS #9: engine state introspection — "why did/didn't FEF fire?"
@router.get("/regime/lookback")
async def get_regime_lookback(request: Request) -> dict[str, Any]:
    """Current regime lookback window size + an estimate of its time coverage
    based on the configured loop interval."""
    s = _state(request)
    import os as _os
    interval_sec = float(_os.environ.get("FEF_LOOP_INTERVAL_SEC", "120"))
    window_size = s.regime_detector._window_size  # type: ignore[attr-defined]
    return {
        "window_size": window_size,
        "loop_interval_sec": interval_sec,
        "minutes_of_context": round(window_size * interval_sec / 60, 1),
        "samples_currently": s.regime_detector.n,
    }


@router.post("/regime/lookback")
async def set_regime_lookback(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Resize the regime detector's rolling window at runtime + re-backfill
    from DB so the new (possibly larger) window is filled immediately
    without waiting for new iters."""
    from fef_sidecar.main import backfill_regime_from_db
    s = _state(request)
    new_size = int(body["window_size"])
    s.regime_detector.resize(new_size)
    await backfill_regime_from_db(s)
    return {
        "ok": True,
        "window_size": s.regime_detector._window_size,  # type: ignore[attr-defined]
        "samples_currently": s.regime_detector.n,
    }


@router.get("/regime/debug")
async def regime_debug(request: Request) -> dict[str, Any]:
    """Inspect the regime detector's rolling window + last assessment.
    Useful when 'UNCLEAR' is showing and you need to know why."""
    s = _state(request)
    r = s.regime_detector
    last = s.last_regime
    return {
        "window_size_max": r._window_size,  # type: ignore[attr-defined]
        "window_size_current": r.n,
        "window_samples": [
            {
                "spx_price": x.spx_price,
                "drift_cum_net_m": x.drift_cum_net_m,
                "drift_bias": x.drift_bias,
                "wall_proximity_pts": x.wall_proximity_pts,
                "aligned": x.aligned,
                "absorption": x.absorption,
                "direction": x.direction,
                "confidence": x.confidence,
                "minutes_since_open": x.minutes_since_open,
                "vix": x.vix,
            }
            for x in r._window  # type: ignore[attr-defined]
        ],
        "last_assessment": {
            "regime": last.regime if last else None,
            "label": last.label if last else None,
            "confidence": last.confidence if last else None,
            "rationale": last.rationale if last else None,
        } if last else None,
    }


@router.get("/engine/state")
async def engine_state(request: Request) -> dict[str, Any]:
    s = _state(request)
    eng = s.engine
    buffer = [
        {
            "timestamp_ms": fi.timestamp_ms,
            "spx_price": fi.spx_price,
            "drift_cum_net_m": fi.drift_cum_net_m,
            "drift_bias": fi.drift_bias,
            "ntm_max_pain": fi.ntm_max_pain,
            "raw_max_pain": fi.raw_max_pain,
            "top_gex_walls": [
                {"strike": w.strike, "net_m": round(w.net_m, 2), "side": w.side}
                for w in fi.top_gex_walls[:3]
            ],
        }
        for fi in list(eng._history)  # type: ignore[attr-defined]
    ]
    return {
        "history_size": eng.history_size,
        "history_window": eng._history_window,  # type: ignore[attr-defined]
        "wall_proximity_pts": eng._wall_proximity_pts,  # type: ignore[attr-defined]
        "wall_collapse_pct": eng._wall_collapse_pct,  # type: ignore[attr-defined]
        "wall_collapse_floor_m": eng._wall_collapse_floor_m,  # type: ignore[attr-defined]
        "threshold_scale": eng._threshold_scale,  # type: ignore[attr-defined]
        "absorption_fe_min": eng._absorption_fe_min,  # type: ignore[attr-defined]
        "buffer": buffer,
        "last_verdict": s.last_verdict,
    }


@router.post("/db/clear_mock_history")
async def clear_mock_history(request: Request) -> dict[str, Any]:
    """Delete every historical row tagged provider_mode='mock' from the
    persisted tables. Use this ONCE after switching to live mode to clear
    the accumulated mock testing data so analytics/attribution/walk-forward
    show only real signals.

    Rows with provider_mode=NULL (pre-tag) are ALSO deleted — they predate
    the tagging system and we can't tell live from mock retrospectively.

    Returns counts of deleted rows per table. Irreversible."""
    from sqlmodel import select, delete
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import (FlowSnapshot, HistoricalBar,
                                        MicrostructureSample, SignalIter,
                                        TradeTicket, VixSample)

    counts: dict[str, int] = {}
    async with get_session() as session:
        # Find signal_iter ids to delete (mock or untagged)
        sigs_to_delete = list((await session.exec(
            select(SignalIter).where(
                (SignalIter.provider_mode == "mock") | (SignalIter.provider_mode == None)  # noqa: E711
            )
        )).all())
        sig_ids = [s.id for s in sigs_to_delete if s.id is not None]
        counts["signal_iter"] = len(sig_ids)
        # Delete trade tickets attached to those signals
        if sig_ids:
            tickets = list((await session.exec(
                select(TradeTicket).where(TradeTicket.signal_iter_id.in_(sig_ids))  # type: ignore[attr-defined]
            )).all())
            counts["trade_ticket"] = len(tickets)
            for t in tickets:
                await session.delete(t)
        for s in sigs_to_delete:
            await session.delete(s)
        # All-tables clear of historical sidecar data — these aren't
        # per-row tagged but they're all from old mock sessions.
        for cls, name in ((HistoricalBar, "historical_bar"),
                          (FlowSnapshot, "flow_snapshot"),
                          (MicrostructureSample, "microstructure_sample"),
                          (VixSample, "vix_sample")):
            rows = list((await session.exec(select(cls))).all())
            counts[name] = len(rows)
            for r in rows:
                await session.delete(r)
    # Audit log entry
    from fef_sidecar.db.repository import SignalIterRepository as R
    await R.write_audit_log(
        category="db_clear", actor="user",
        summary=f"Cleared mock/untagged history: {sum(counts.values())} rows",
        details=counts,
    )
    return {"ok": True, "deleted": counts, "total": sum(counts.values())}


@router.get("/db/row_counts")
async def db_row_counts() -> dict[str, Any]:
    """Quick inventory of each table — useful before clearing mock data."""
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import (FlowSnapshot, HistoricalBar,
                                        MicrostructureSample, SignalIter,
                                        TradeTicket, VixSample)
    out: dict[str, int] = {}
    async with get_session() as session:
        for cls, name in (
            (SignalIter, "signal_iter"),
            (TradeTicket, "trade_ticket"),
            (HistoricalBar, "historical_bar"),
            (FlowSnapshot, "flow_snapshot"),
            (MicrostructureSample, "microstructure_sample"),
            (VixSample, "vix_sample"),
        ):
            out[name] = len(list((await session.exec(select(cls))).all()))
        # SignalIter by provider_mode
        mock_rows = list((await session.exec(
            select(SignalIter).where(SignalIter.provider_mode == "mock")
        )).all())
        live_rows = list((await session.exec(
            select(SignalIter).where(SignalIter.provider_mode == "live")
        )).all())
        untagged_rows = list((await session.exec(
            select(SignalIter).where(SignalIter.provider_mode == None)  # noqa: E711
        )).all())
        out["signal_iter_by_mode"] = {  # type: ignore[assignment]
            "live": len(live_rows), "mock": len(mock_rows),
            "untagged_pre_tag_system": len(untagged_rows),
        }
    return out


@router.post("/walk_forward/run_now")
async def walk_forward_run_now(request: Request) -> dict[str, Any]:
    from fef_sidecar.engine.auto_walk_forward import run_auto_walk_forward
    s = _state(request)
    run = await run_auto_walk_forward(s)
    return {
        "ok": True, "run_at": run.run_at.isoformat(),
        "drift_score": run.drift_score,
        "current": {
            "wall_proximity_pts": run.current_wall_proximity_pts,
            "wall_collapse_pct": run.current_wall_collapse_pct,
            "wall_collapse_floor_m": run.current_wall_collapse_floor_m,
            "absorption_fe_ratio_min": run.current_absorption_fe_ratio_min,
        },
        "optimal": {
            "wall_proximity_pts": run.optimal_wall_proximity_pts,
            "wall_collapse_pct": run.optimal_wall_collapse_pct,
            "wall_collapse_floor_m": run.optimal_wall_collapse_floor_m,
            "absorption_fe_ratio_min": run.optimal_absorption_fe_ratio_min,
            "sharpe": run.optimal_sharpe,
            "total_pnl": run.optimal_total_pnl,
            "win_rate_pct": run.optimal_win_rate,
        },
    }


@router.get("/walk_forward/runs")
async def walk_forward_runs(limit: int = 30) -> list[dict[str, Any]]:
    rows = await SignalIterRepository.query_walk_forward_runs(limit=limit)
    return [
        {"id": r.id, "run_at": r.run_at.isoformat(), "window_days": r.window_days,
         "drift_score": r.drift_score,
         "current": {
             "wall_proximity_pts": r.current_wall_proximity_pts,
             "wall_collapse_pct": r.current_wall_collapse_pct,
             "wall_collapse_floor_m": r.current_wall_collapse_floor_m,
             "absorption_fe_ratio_min": r.current_absorption_fe_ratio_min,
         },
         "optimal": {
             "wall_proximity_pts": r.optimal_wall_proximity_pts,
             "wall_collapse_pct": r.optimal_wall_collapse_pct,
             "wall_collapse_floor_m": r.optimal_wall_collapse_floor_m,
             "absorption_fe_ratio_min": r.optimal_absorption_fe_ratio_min,
             "sharpe": r.optimal_sharpe, "total_pnl": r.optimal_total_pnl,
             "win_rate_pct": r.optimal_win_rate,
         },
         "notes": r.notes}
        for r in rows
    ]


@router.post("/ml/train")
async def ml_train() -> dict[str, Any]:
    from fef_sidecar.engine.ml_patterns import train_and_persist
    return await train_and_persist()


@router.get("/ml/runs")
async def ml_runs(limit: int = 20) -> list[dict[str, Any]]:
    rows = await SignalIterRepository.query_ml_model_runs(limit=limit)
    return [
        {"id": r.id, "trained_at": r.trained_at.isoformat(), "algorithm": r.algorithm,
         "n_training_rows": r.n_training_rows, "n_features": r.n_features,
         "train_accuracy": r.train_accuracy, "test_accuracy": r.test_accuracy,
         "test_auc": r.test_auc, "feature_importances": r.feature_importances}
        for r in rows
    ]


@router.get("/multi_strategy/configs")
async def multi_strategy_configs() -> list[dict[str, Any]]:
    from fef_sidecar.engine.multi_strategy import DEFAULT_STRATEGIES
    return [
        {"name": s.name, "description": s.description,
         "wall_proximity_pts": s.wall_proximity_pts,
         "wall_collapse_pct": s.wall_collapse_pct,
         "wall_collapse_floor_m": s.wall_collapse_floor_m,
         "absorption_fe_ratio_min": s.absorption_fe_ratio_min,
         "base_qty": s.base_qty}
        for s in DEFAULT_STRATEGIES
    ]


@router.get("/quote/{symbol}")
async def quote(request: Request, symbol: str) -> dict[str, Any]:
    """Live bid/ask/last for any supported symbol.

    Works with whichever IBKR provider is configured (mock or live).
    For futures (ES, NQ, RTY) returns the front-month continuous contract.
    Bid-ask spread + spread% included for liquidity check before entering."""
    from fef_sidecar.ibkr.models import Contract

    s = _state(request)
    try:
        contract = Contract.for_symbol(symbol)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    try:
        q = await s.ibkr.get_last_quote(contract)
    except Exception as e:
        raise HTTPException(503, f"Quote fetch failed: {e}") from e

    bid = q.bid if q.bid is not None else None
    ask = q.ask if q.ask is not None else None
    last = q.last
    spread = (ask - bid) if (bid is not None and ask is not None) else None
    spread_pct = (spread / last * 100) if (spread is not None and last) else None

    provider_name = type(s.ibkr).__name__
    is_live = provider_name == "LiveIBKRProvider"

    return {
        "symbol": symbol.upper(),
        "exchange": contract.exchange,
        "kind": contract.kind.value if hasattr(contract.kind, "value") else str(contract.kind),
        "last": round(last, 2) if last else None,
        "bid": round(bid, 2) if bid is not None else None,
        "ask": round(ask, 2) if ask is not None else None,
        "bid_size": q.bid_size,
        "ask_size": q.ask_size,
        "spread": round(spread, 4) if spread is not None else None,
        "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
        "timestamp_ms": int(q.timestamp.timestamp() * 1000),
        "source": "live" if is_live else "mock",
        "provider": provider_name,
    }


@router.get("/auto_trade/status")
async def auto_trade_status(request: Request) -> dict[str, Any]:
    s = _state(request)
    from fef_sidecar.engine.live_trading import is_live_trading_enabled
    import time as _t
    now_ms = int(_t.time() * 1000)
    elapsed = (now_ms - s.auto_trade_last_at_ms) / 1000 if s.auto_trade_last_at_ms else None
    cool_remaining = max(0, s.auto_trade_cool_down_sec - elapsed) if elapsed is not None else 0
    return {
        "enabled": s.auto_trade_enabled,
        "live_trading_env": is_live_trading_enabled(),
        "confidence_floor": s.auto_trade_confidence_floor,
        "cool_down_sec": s.auto_trade_cool_down_sec,
        "cool_down_remaining_sec": int(cool_remaining),
        "max_per_session": s.auto_trade_max_per_session,
        "count_session": s.auto_trade_count_session,
        "last_at_ms": s.auto_trade_last_at_ms,
        "last_block_reason": s.auto_trade_last_block_reason,
        "session_cap_reached": s.auto_trade_count_session >= s.auto_trade_max_per_session,
        "qty_override": s.auto_trade_qty_override,
        "trigger_types": s.auto_trade_trigger_types,
        "runner_protect_enabled": s.runner_protect_enabled,
    }


@router.get("/exit_strategy")
async def get_exit_strategy(request: Request) -> dict[str, Any]:
    s = _state(request)
    return {
        "long": {
            "strategy": s.exit_strategy_long,
            "dollar_t1": s.exit_dollar_t1,
            "dollar_t2": s.exit_dollar_t2,
            "dollar_t3": s.exit_dollar_t3,
            "dollar_stop": s.exit_dollar_stop,
        },
        "spread": {
            "stop_multiplier": s.spread_stop_multiplier,
            "tp_close_debit": s.spread_tp_close_debit,
        },
    }


@router.post("/exit_strategy")
async def set_exit_strategy(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    s = _state(request)
    if "long_strategy" in body:
        v = str(body["long_strategy"])
        if v in ("spx_points", "fixed_dollar", "option_price"):
            s.exit_strategy_long = v
    if "dollar_t1" in body: s.exit_dollar_t1 = float(body["dollar_t1"])
    if "dollar_t2" in body: s.exit_dollar_t2 = float(body["dollar_t2"])
    if "dollar_t3" in body: s.exit_dollar_t3 = float(body["dollar_t3"])
    if "dollar_stop" in body: s.exit_dollar_stop = float(body["dollar_stop"])
    if "spread_stop_multiplier" in body: s.spread_stop_multiplier = float(body["spread_stop_multiplier"])
    if "spread_tp_close_debit" in body: s.spread_tp_close_debit = float(body["spread_tp_close_debit"])
    return {"ok": True}


@router.post("/auto_trade/runner_protect")
async def auto_trade_runner_protect(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Toggle the runner_protector loop. When ON (default), after T1 fills
    on a bracket, the remaining T2/T3 runners are market-exited if the
    option premium retraces back below T1 fill price."""
    from fef_sidecar.db.repository import SignalIterRepository
    s = _state(request)
    s.runner_protect_enabled = bool(body.get("enabled", True))
    await SignalIterRepository.write_audit_log(
        category="runner_protect_toggle", actor="user",
        summary=f"Runner protector {'enabled' if s.runner_protect_enabled else 'disabled'}",
    )
    return {"ok": True, "runner_protect_enabled": s.runner_protect_enabled}


@router.post("/auto_trade/toggle")
async def auto_trade_toggle(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Turn auto-trade on/off + optionally update settings.

    Body: {enabled: bool, confirm: bool, [confidence_floor], [cool_down_sec],
           [max_per_session]}

    confirm=true required when enabling (defensive — prevent accidental
    enabling via a stale fetch or replay)."""
    from fef_sidecar.engine.live_trading import is_live_trading_enabled
    from fef_sidecar.db.repository import SignalIterRepository

    s = _state(request)
    new_enabled = bool(body.get("enabled"))
    if new_enabled and not body.get("confirm"):
        raise HTTPException(400, "confirm=true required when enabling auto-trade")
    if new_enabled and not is_live_trading_enabled():
        raise HTTPException(
            400,
            "FEF_LIVE_TRADING env not set — auto-trade can't fire real orders. "
            "Restart sidecar with FEF_LIVE_TRADING=true first."
        )

    old_enabled = s.auto_trade_enabled
    s.auto_trade_enabled = new_enabled
    if "confidence_floor" in body:
        s.auto_trade_confidence_floor = str(body["confidence_floor"]).upper()
    if "cool_down_sec" in body:
        s.auto_trade_cool_down_sec = max(60, int(body["cool_down_sec"]))
    if "max_per_session" in body:
        s.auto_trade_max_per_session = max(1, min(50, int(body["max_per_session"])))
    if "qty_override" in body:
        s.auto_trade_qty_override = max(0, min(100, int(body["qty_override"])))
    if "trigger_types" in body:
        valid = {"confluence", "aligned_only", "wall_only"}
        types = [t for t in body["trigger_types"] if t in valid]
        if not types:
            types = ["confluence"]  # never allow empty — would block all fires silently
        s.auto_trade_trigger_types = types

    await SignalIterRepository.write_audit_log(
        category="auto_trade_toggle", actor="user",
        summary=f"Auto-trade {'ENABLED' if new_enabled else 'DISABLED'} "
                f"(was {'on' if old_enabled else 'off'}) — floor={s.auto_trade_confidence_floor}, "
                f"cool_down={s.auto_trade_cool_down_sec}s, max={s.auto_trade_max_per_session}",
        details={
            "enabled": new_enabled, "confidence_floor": s.auto_trade_confidence_floor,
            "cool_down_sec": s.auto_trade_cool_down_sec,
            "max_per_session": s.auto_trade_max_per_session,
        },
    )
    return {
        "ok": True, "enabled": s.auto_trade_enabled,
        "confidence_floor": s.auto_trade_confidence_floor,
        "cool_down_sec": s.auto_trade_cool_down_sec,
        "max_per_session": s.auto_trade_max_per_session,
    }


@router.get("/scanner/symbols")
async def scanner_symbols_get(request: Request) -> dict[str, Any]:
    """Return per-symbol scanner enable state + caps."""
    s = _state(request)
    import time as _t
    now_ms = int(_t.time() * 1000)
    cool_remaining_by_symbol: dict[str, int] = {}
    for sym, last in s.auto_trade_last_at_ms_by_symbol.items():
        if last <= 0:
            continue
        remaining = s.auto_trade_cool_down_sec - int((now_ms - last) / 1000)
        if remaining > 0:
            cool_remaining_by_symbol[sym] = remaining
    return {
        "enabled": dict(s.scan_enabled_symbols),
        "max_per_symbol": s.auto_trade_max_per_symbol,
        "max_per_session_global": s.auto_trade_max_per_session,
        "count_global": s.auto_trade_count_session,
        "count_by_symbol": dict(s.auto_trade_count_by_symbol),
        "cool_down_sec": s.auto_trade_cool_down_sec,
        "cool_remaining_by_symbol": cool_remaining_by_symbol,
        "last_verdict_by_symbol": dict(s.last_scanner_verdict_by_symbol),
    }


@router.post("/scanner/symbols")
async def scanner_symbols_set(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Toggle a scanner symbol on/off, or update the per-symbol cap.

    Body fields (any subset):
      enabled: {"SPX": true, "QQQ": false, ...} — partial dict; merges
      max_per_symbol: int (1..50)
    """
    s = _state(request)
    if "enabled" in body and isinstance(body["enabled"], dict):
        for sym, on in body["enabled"].items():
            sym_u = str(sym).upper()
            if sym_u in s.scan_enabled_symbols:
                s.scan_enabled_symbols[sym_u] = bool(on)
    if "max_per_symbol" in body:
        s.auto_trade_max_per_symbol = max(1, min(50, int(body["max_per_symbol"])))
    return {
        "ok": True,
        "enabled": dict(s.scan_enabled_symbols),
        "max_per_symbol": s.auto_trade_max_per_symbol,
    }


@router.get("/history/reviews")
async def history_reviews(limit: int = 30) -> list[dict[str, Any]]:
    """Return recent DailyReview rows (most-recent first)."""
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import DailyReview
    from sqlmodel import select as _s
    async with _gs() as session:
        rows = list((await session.exec(
            _s(DailyReview).order_by(DailyReview.session_date.desc()).limit(limit)  # type: ignore[attr-defined]
        )).all())
    return [
        {"id": r.id, "session_date": r.session_date,
         "computed_at": r.computed_at.isoformat(),
         "total_signals": r.total_signals,
         "actionable_signals": r.actionable_signals,
         "wins": r.wins, "stops": r.stops, "timeouts": r.timeouts,
         "win_rate_pct": r.win_rate_pct, "expectancy_r": r.expectancy_r,
         "t1_hits": r.t1_hits, "t2_hits": r.t2_hits, "t3_hits": r.t3_hits,
         "by_trigger": r.by_trigger, "by_confidence": r.by_confidence,
         "by_hour_ct": r.by_hour_ct}
        for r in rows
    ]


@router.get("/tuning/recommendations")
async def tuning_recommendations(status: str = "OPEN", limit: int = 50) -> list[dict[str, Any]]:
    """Return TuningRecommendation rows. Default status=OPEN returns the
    currently-active batch (most recent generation, not yet APPLIED/DISMISSED).
    Use status=all to see history.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import TuningRecommendation as _TR
    from sqlmodel import select as _s
    async with _gs() as session:
        stmt = _s(_TR).order_by(_TR.generated_at.desc()).limit(limit)  # type: ignore[attr-defined]
        if status != "all":
            stmt = stmt.where(_TR.status == status)
        rows = list((await session.exec(stmt)).all())
    return [
        {"id": r.id, "generated_at": r.generated_at.isoformat(),
         "based_on_sessions": r.based_on_sessions,
         "based_on_window_start": r.based_on_window_start,
         "based_on_window_end": r.based_on_window_end,
         "kind": r.kind, "priority": r.priority,
         "title": r.title, "body": r.body, "backing_data": r.backing_data,
         "status": r.status,
         "applied_engine_change_id": r.applied_engine_change_id,
         "decided_at": r.decided_at.isoformat() if r.decided_at else None,
         "decided_by": r.decided_by}
        for r in rows
    ]


@router.post("/tuning/recompute")
async def tuning_recompute(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Re-run the tuning recommender over a date range (or all data if no range)."""
    from fef_sidecar.engine.tuning_runner import run_tuning_recommendations
    start = body.get("start"); end = body.get("end")
    return await run_tuning_recommendations(start=start, end=end)


@router.post("/tuning/decide")
async def tuning_decide(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Mark a TuningRecommendation as APPLIED or DISMISSED.
    Body: {recommendation_id: int, decision: 'APPLIED'|'DISMISSED', applied_engine_change_id?: int}
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import TuningRecommendation as _TR
    from sqlmodel import select as _s
    import datetime as _dt
    rec_id = int(body["recommendation_id"])
    decision = body["decision"]
    if decision not in ("APPLIED", "DISMISSED"):
        raise HTTPException(400, "decision must be APPLIED or DISMISSED")
    async with _gs() as session:
        r = (await session.exec(_s(_TR).where(_TR.id == rec_id))).first()
        if r is None:
            raise HTTPException(404, f"recommendation {rec_id} not found")
        r.status = decision
        r.decided_at = _dt.datetime.now(_dt.timezone.utc)
        r.decided_by = body.get("decided_by", "user")
        if "applied_engine_change_id" in body:
            r.applied_engine_change_id = body["applied_engine_change_id"]
        session.add(r)
        await session.commit()
    return {"ok": True, "id": rec_id, "status": decision}


@router.get("/engine/freeze_state")
async def engine_freeze_state() -> dict[str, Any]:
    """Sessions since most recent EngineChange — drives the FREEZE N/10
    pill on the TopBar. Compares EngineChange.applied_at against
    DailyReview.session_date count after that date.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import DailyReview, EngineChange
    from sqlmodel import select as _s
    async with _gs() as session:
        latest = (await session.exec(
            _s(EngineChange).order_by(EngineChange.applied_at.desc()).limit(1)  # type: ignore[attr-defined]
        )).first()
        reviews = list((await session.exec(_s(DailyReview))).all())
    if latest is None:
        return {"sessions_since_change": len(reviews), "target": 10,
                "last_change": None, "last_change_at": None}
    last_change_date = latest.applied_at.strftime("%Y-%m-%d")
    sessions_after = sum(1 for r in reviews if r.session_date > last_change_date)
    return {
        "sessions_since_change": sessions_after,
        "target": 10,
        "last_change": latest.name,
        "last_change_at": latest.applied_at.isoformat(),
    }


@router.get("/engine/changes")
async def engine_changes(limit: int = 100) -> list[dict[str, Any]]:
    """Return EngineChange rows ordered by applied_at desc."""
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import EngineChange as _EC
    from sqlmodel import select as _s
    async with _gs() as session:
        rows = list((await session.exec(
            _s(_EC).order_by(_EC.applied_at.desc()).limit(limit)  # type: ignore[attr-defined]
        )).all())
    return [
        {"id": r.id, "applied_at": r.applied_at.isoformat(),
         "name": r.name, "description": r.description, "category": r.category,
         "expected_impact": r.expected_impact,
         "measured_impact_r": r.measured_impact_r,
         "rollback_condition": r.rollback_condition,
         "git_commit_sha": r.git_commit_sha}
        for r in rows
    ]


@router.post("/engine/changes")
async def engine_change_log(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Log a new EngineChange row. Called manually or from scripts."""
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import EngineChange as _EC
    row = _EC(
        name=body["name"], description=body.get("description", ""),
        category=body.get("category", "gate"),
        expected_impact=body.get("expected_impact", ""),
        rollback_condition=body.get("rollback_condition", ""),
        git_commit_sha=body.get("git_commit_sha"),
    )
    async with _gs() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return {"ok": True, "id": row.id}


@router.get("/diag/pnl")
async def diag_pnl(request: Request) -> dict[str, Any]:
    """Diagnostic: show raw IBKR PnL subscription state + AccountSummary
    tag dump. Used to investigate why Today's P/L shows 0.0 despite
    closed trades.
    """
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    s = _state(request)
    if not isinstance(s.ibkr, LiveIBKRProvider):
        return {"mode": "mock", "note": "no real PnL in mock mode"}
    ib = getattr(s.ibkr, "_ib", None)
    if ib is None or not ib.isConnected():
        return {"mode": "disconnected"}

    accounts = list(ib.managedAccounts())
    pnl_subs = getattr(s.ibkr, "_pnl_subscriptions", {})
    pnl_state = {}
    for acct, pnl_obj in pnl_subs.items():
        try:
            pnl_state[acct] = {
                "dailyPnL": pnl_obj.dailyPnL if pnl_obj.dailyPnL == pnl_obj.dailyPnL else None,
                "realizedPnL": pnl_obj.realizedPnL if pnl_obj.realizedPnL == pnl_obj.realizedPnL else None,
                "unrealizedPnL": pnl_obj.unrealizedPnL if pnl_obj.unrealizedPnL == pnl_obj.unrealizedPnL else None,
            }
        except Exception as e:
            pnl_state[acct] = {"error": str(e)}

    try:
        values = await ib.accountSummaryAsync()
        summary_tags = {v.tag: v.value for v in values if v.account == accounts[0]} if accounts else {}
    except Exception as e:
        summary_tags = {"error": str(e)}

    return {
        "mode": "live",
        "managed_accounts": accounts,
        "pnl_subscriptions": pnl_state,
        "account_summary_tags": summary_tags,
    }


@router.get("/diag/session_pnl_estimate")
async def diag_session_pnl_estimate(request: Request) -> dict[str, Any]:
    """Estimate today's session P/L from our own LiveOrder records when
    reqPnL is silent. Each CLOSED parent contributes a coarse +R estimate
    (using the BracketSpec's risk distance × spec qty × 100 × 0.6 as a
    rough average weighted-R win). STOPPED parents contribute the
    risk-distance loss. Better than always-zero; not a substitute for
    real IBKR P/L once that's working.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import LiveOrder
    from sqlmodel import select as _s
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    today_et = _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")
    async with _gs() as session:
        parents = list((await session.exec(
            _s(LiveOrder).where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.dry_run == False)  # noqa: E712
        )).all())
    today_parents = [p for p in parents
                     if p.submitted_at.strftime("%Y-%m-%d") == today_et]

    est_total = 0.0
    by_status: dict[str, int] = {}
    for p in today_parents:
        by_status[p.status] = by_status.get(p.status, 0) + 1
        if p.entry_spx_price is None or p.spx_target_stop is None:
            continue
        risk_pts = abs(p.entry_spx_price - p.spx_target_stop) or 10.0
        # Approximate per-contract $ at risk = (risk_pts × delta ≈ 0.3 × $100)
        # We don't have actual fills; use entry_limit (approximates option price)
        per_contract_risk = (p.limit_price or 1.0) * 100
        if p.status == "CLOSED":
            est_total += per_contract_risk * p.qty * 1.1   # +1.1R weighted
        elif p.status == "STOPPED":
            est_total -= per_contract_risk * p.qty * 1.0
        elif p.status == "PARTIAL":
            est_total += per_contract_risk * p.qty * 0.4   # T1 fired only

    return {
        "session_date": today_et,
        "parents_count": len(today_parents),
        "by_status": by_status,
        "estimated_session_pnl_usd": round(est_total, 0),
        "note": "approximation — uses entry_limit × qty × R-weighted outcome. "
                "If IBKR reqPnL is working, /account is authoritative.",
    }


@router.get("/diag/streams")
async def diag_streams(request: Request) -> dict[str, Any]:
    """Per-stream health snapshot. Returns last-activity timestamp + task
    status for each background loop. Used by the StreamHealthPanel."""
    import time as _t
    s = _state(request)
    now_ms = int(_t.time() * 1000)

    def _state_of(task) -> str:
        if task is None: return "NOT_STARTED"
        try:
            if task.done(): return "DONE"
            if task.cancelled(): return "CANCELLED"
            return "RUNNING"
        except Exception:
            return "UNKNOWN"

    tick_age_ms = (now_ms - s.last_live_tick_ms) if s.last_live_tick_ms > 0 else None
    return {
        "now_ms": now_ms,
        "loops": {
            "tick_stream": {
                "status": _state_of(s.tick_task),
                "tick_quote_count": s.tick_quote_count_live,
                "tick_trade_count": s.tick_trade_count_live,
                "last_tick_age_ms": tick_age_ms,
            },
            "bar_stream": {"status": _state_of(s.bar_task)},
            "quantdata_poll": {"status": _state_of(s.quantdata_poll_task)},
            "fef_tick": {"status": _state_of(s.loop_task)},
            "health_check": {"status": _state_of(s.health_check_task)},
            "vix_poll": {"status": _state_of(s.vix_task)},
            "account_poll": {"status": _state_of(s.account_task)},
            "microstructure": {"status": _state_of(s.microstructure_task)},
            "hft": {"status": _state_of(s.hft_task)},
            "position_monitor": {"status": _state_of(s.position_task)},
            "auto_walk_forward": {"status": _state_of(s.auto_wf_task)},
            "runner_protector": {"status": _state_of(s.runner_protect_task)},
            "spx_target_watcher": {"status": _state_of(s.spx_target_task)},
            "spread_exit_watcher": {"status": _state_of(s.spread_exit_task)},
            "scanner": {"status": _state_of(s.scanner_task)},
        },
        "depth_update_count": s.depth_update_count_live,
        "ibkr": s.ibkr_status,
    }


@router.post("/panic/flat")
async def panic_flat(request: Request) -> dict[str, Any]:
    """FLAT EVERYTHING. Composite action:
      1. Cancel all working orders (kill_all_orders)
      2. Disable auto-trade
      3. Market-sell every open option position
      4. Disable all scanner symbols
    Returns counts + per-step results. Idempotent."""
    from fef_sidecar.engine.live_trading import kill_all_orders
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    s = _state(request)
    result: dict[str, Any] = {"steps": {}}

    # 1. Cancel orders
    try:
        result["steps"]["cancel_orders"] = await kill_all_orders(s.ibkr)
    except Exception as e:
        result["steps"]["cancel_orders"] = {"ok": False, "error": str(e)}

    # 2. Disable auto-trade
    s.auto_trade_enabled = False
    result["steps"]["auto_trade_disabled"] = True

    # 3. Market-sell all open option positions
    sells = []
    if isinstance(s.ibkr, LiveIBKRProvider) and s.ibkr._ib is not None:  # type: ignore[attr-defined]
        ib = s.ibkr._ib  # type: ignore[attr-defined]
        if ib.isConnected():
            try:
                from ib_async import MarketOrder
                for p in ib.positions():
                    if getattr(p.contract, "secType", "") != "OPT": continue
                    qty = int(p.position)
                    if abs(qty) < 1: continue
                    action = "SELL" if qty > 0 else "BUY"
                    try:
                        order = MarketOrder(action, abs(qty), transmit=True)
                        t = ib.placeOrder(p.contract, order)
                        sells.append({
                            "contract": f"{p.contract.symbol} {p.contract.strike}{p.contract.right}",
                            "qty": abs(qty), "action": action,
                            "ib_order_id": t.order.orderId,
                        })
                    except Exception as e:
                        sells.append({"error": str(e)})
            except Exception as e:
                result["steps"]["flat_positions_error"] = str(e)
    result["steps"]["positions_flat"] = sells

    # 4. Disable scanner symbols
    for sym in list(s.scan_enabled_symbols.keys()):
        s.scan_enabled_symbols[sym] = False
    result["steps"]["scanner_disabled"] = True

    try:
        await SignalIterRepository.write_audit_log(
            category="panic_flat", actor="user",
            summary=f"PANIC FLAT triggered — cancelled orders, disabled auto-trade, "
                    f"market-sold {len(sells)} positions, disabled scanner",
            details=result,
        )
    except Exception:
        pass
    return {"ok": True, **result}


@router.post("/engine/backtest")
async def engine_backtest(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Replay signals through a proposed filter stack.

    Body: {filters: ['confluence_only', 'fe_band_1_3', ...], session_date?: 'YYYY-MM-DD'}

    Returns baseline vs filtered stats: win rate, expectancy R, total R,
    expectancy delta. Use to preview a gate before shipping it.
    """
    from fef_sidecar.engine.what_if_backtest import what_if, FILTERS
    filters = body.get("filters", [])
    if not isinstance(filters, list):
        raise HTTPException(400, "filters must be a list of filter names")
    try:
        return await what_if(filters, session_date=body.get("session_date"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/engine/backtest/filters")
async def engine_backtest_filters() -> list[str]:
    """Available filter names for the backtest endpoint."""
    from fef_sidecar.engine.what_if_backtest import FILTERS
    return list(FILTERS.keys())


@router.post("/shadow/enable")
async def shadow_enable(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Enable shadow engine with overridden parameters.
    Body: {label: 'tighter_collapse', wall_proximity_pts?, wall_collapse_pct?,
           wall_collapse_floor_m?, absorption_fe_ratio_min?}
    """
    from fef_sidecar.engine import FEFEngine
    s = _state(request)
    s.shadow_label = body.get("label", "shadow")
    s.shadow_engine = FEFEngine(
        wall_proximity_pts=float(body.get("wall_proximity_pts", 5.0)),
        wall_collapse_pct=float(body.get("wall_collapse_pct", 0.20)),
        wall_collapse_floor_m=float(body.get("wall_collapse_floor_m", 5.0)),
        absorption_fe_ratio_min=float(body.get("absorption_fe_ratio_min", 5.0)),
    )
    return {"ok": True, "label": s.shadow_label}


@router.post("/shadow/disable")
async def shadow_disable(request: Request) -> dict[str, Any]:
    s = _state(request)
    label = s.shadow_label
    s.shadow_engine = None
    s.shadow_label = ""
    return {"ok": True, "was_label": label}


@router.get("/shadow/comparisons")
async def shadow_comparisons(limit: int = 200, session_date: str | None = None) -> list[dict[str, Any]]:
    """List ShadowComparison rows. Filter by session_date or take latest N."""
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import ShadowComparison as _SC
    from sqlmodel import select as _s
    async with _gs() as session:
        stmt = _s(_SC).order_by(_SC.iter_timestamp.desc()).limit(limit)  # type: ignore[attr-defined]
        if session_date:
            stmt = stmt.where(_SC.session_date == session_date)
        rows = list((await session.exec(stmt)).all())
    return [
        {"id": r.id, "iter_timestamp": r.iter_timestamp.isoformat(),
         "session_date": r.session_date, "iter_num": r.iter_num,
         "production_direction": r.production_direction.value if hasattr(r.production_direction, "value") else str(r.production_direction),
         "production_confidence": r.production_confidence.value if hasattr(r.production_confidence, "value") else str(r.production_confidence),
         "shadow_label": r.shadow_label,
         "shadow_direction": r.shadow_direction.value if hasattr(r.shadow_direction, "value") else str(r.shadow_direction),
         "shadow_confidence": r.shadow_confidence.value if hasattr(r.shadow_confidence, "value") else str(r.shadow_confidence),
         "direction_agree": r.direction_agree,
         "confidence_agree": r.confidence_agree}
        for r in rows
    ]


@router.post("/admin/reconcile_positions")
async def admin_reconcile_positions(request: Request) -> dict[str, Any]:
    """Cross-reference IBKR positions vs our LiveOrder parents.
    Detects orphans (IBKR has, DB doesn't — bot can't manage)
    and phantoms (DB has, IBKR doesn't — auto-marked CLOSED).
    Returns counts + detailed lists. Idempotent.
    """
    from fef_sidecar.engine.position_reconcile import reconcile_positions
    return await reconcile_positions(_state(request))


@router.post("/admin/sweep_orphans")
async def admin_sweep_orphans(request: Request) -> dict[str, Any]:
    """⚠ DESTRUCTIVE. Market-sell-to-close every IBKR option position that
    has no matching managed parent in our DB. Use when reconcile shows
    orphans and you want them flat NOW. Requires confirm=true.
    """
    from fef_sidecar.engine.position_reconcile import sweep_orphan_positions
    return await sweep_orphan_positions(_state(request))


@router.post("/admin/backfill_regime")
async def admin_backfill_regime(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Backfill extras.regime_at_fire on historical signal_iter rows."""
    from fef_sidecar.engine.regime_backfill import backfill_regime_at_fire
    session_date = body.get("session_date")
    return await backfill_regime_at_fire(session_date=session_date)


@router.post("/admin/backfill_engine_changes")
async def admin_backfill_engine_changes() -> dict[str, Any]:
    """One-shot: insert EngineChange rows for the semantic changes shipped
    today (mid-session). Idempotent — skips rows whose name already exists."""
    from fef_sidecar.engine.engine_change_backfill import backfill_engine_changes
    return await backfill_engine_changes()


@router.post("/admin/daily_review")
async def admin_daily_review(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """On-demand DailyReview compute. Body: {session_date?: 'YYYY-MM-DD'}.
    Defaults to today (ET)."""
    from fef_sidecar.engine.daily_review import compute_daily_review
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    et = _tz(_td(hours=-4))
    session_date = body.get("session_date") or _dt.now(et).strftime("%Y-%m-%d")
    review = await compute_daily_review(session_date)
    return {"ok": True, "session_date": review.session_date,
            "win_rate_pct": review.win_rate_pct,
            "expectancy_r": review.expectancy_r,
            "actionable_signals": review.actionable_signals}


@router.post("/auto_trade/reset_session")
async def auto_trade_reset_session(request: Request) -> dict[str, Any]:
    """Reset the per-session auto-trade counter (lets you fire more if you've
    hit the cap and want to continue). Audit-logged."""
    from fef_sidecar.db.repository import SignalIterRepository
    s = _state(request)
    old = s.auto_trade_count_session
    s.auto_trade_count_session = 0
    s.auto_trade_count_by_symbol.clear()
    s.auto_trade_last_at_ms = 0
    s.auto_trade_last_at_ms_by_symbol.clear()
    await SignalIterRepository.write_audit_log(
        category="auto_trade_reset", actor="user",
        summary=f"Reset auto-trade session counter (was {old}/{s.auto_trade_max_per_session})",
    )
    return {"ok": True, "old_count": old}


@router.post("/live/place_bracket")
async def live_place_bracket(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Place a bracket order. Two-gate safety:
       1. FEF_LIVE_TRADING=true env required
       2. body.confirm=true required
    Without both → DRY_RUN (writes records but doesn't hit IBKR)."""
    from fef_sidecar.engine.live_trading import BracketSpec, place_bracket
    s = _state(request)
    # 85/10/5 default split per user trading style
    qty = int(body["qty"])
    t1_default = max(1, round(qty * 0.85))
    t2_default = max(1, round(qty * 0.10))
    t3_default = max(1, qty - t1_default - t2_default)
    if t1_default + t2_default + t3_default != qty:
        t1_default = qty - t2_default - t3_default
    spec = BracketSpec(
        symbol=body["symbol"], strike=float(body["strike"]),
        right=body["right"], expiry=body["expiry"], qty=qty,
        entry_limit=float(body["entry_limit"]),
        t1_limit=float(body["t1_limit"]), t2_limit=float(body["t2_limit"]),
        t3_limit=float(body["t3_limit"]), stop_limit=float(body["stop_limit"]),
        t1_qty=int(body.get("t1_qty", t1_default)),
        t2_qty=int(body.get("t2_qty", t2_default)),
        t3_qty=int(body.get("t3_qty", t3_default)),
    )
    return await place_bracket(
        s.ibkr, spec, confirm=body.get("confirm", False),
        ticket_id=body.get("ticket_id"),
    )


@router.get("/diag/option_quote")
async def diag_option_quote(
    request: Request,
    symbol: str = "SPX", strike: float = 7400, right: str = "C",
    expiry: str | None = None,
) -> dict[str, Any]:
    """Diagnostic: try to fetch an option quote and report what came back.

    Use this to test whether your IBKR account has the right market-data
    subscription. SPX options need 'Cboe One' or 'Cboe Hybrid' (not just
    OPRA). SPY options usually work with US Securities Snapshot bundle.

    Usage: GET /diag/option_quote?symbol=SPX&strike=7400&right=C
    """
    import time as _t
    from fef_sidecar.ibkr.models import Contract, ContractKind

    s = _state(request)
    expiry = expiry or _t.strftime("%Y%m%d")
    contract = Contract(
        symbol=symbol, kind=ContractKind.OPTION,
        exchange="CBOE" if symbol == "SPX" else "SMART",
        expiry=expiry, strike=strike, right=right,
    )
    t0 = _t.perf_counter()
    try:
        quote = await s.ibkr.get_option_quote(contract)
        elapsed_ms = round((_t.perf_counter() - t0) * 1000)
        return {
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "symbol": symbol, "strike": strike, "right": right, "expiry": expiry,
            "last": quote.last, "bid": quote.bid, "ask": quote.ask,
            "delta": quote.delta, "iv": quote.iv,
            "underlying_price": quote.underlying_price,
            "usable": bool(quote.bid and quote.ask and quote.bid > 0 and quote.ask > 0),
        }
    except Exception as e:
        elapsed_ms = round((_t.perf_counter() - t0) * 1000)
        return {
            "ok": False, "elapsed_ms": elapsed_ms,
            "error_type": type(e).__name__, "error": str(e),
            "hint": (
                "If error mentions 'not subscribed' / 354 / 10089: your IBKR "
                "account needs the relevant options market-data subscription. "
                "SPX requires 'Cboe One' or 'Cboe Hybrid'. SPY usually works "
                "with the default US Securities Snapshot bundle."
            ),
        }


@router.post("/live/place_spread")
async def live_place_spread(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Place a credit spread (CCS or PCS) via IBKR Bag/ComboLegs.
    Same two-gate safety as place_bracket: FEF_LIVE_TRADING=true + confirm=true."""
    from fef_sidecar.engine.live_trading import SpreadSpec, place_credit_spread

    s = _state(request)
    spec = SpreadSpec(
        symbol=body["symbol"],
        expiry=body["expiry"],
        kind=body["kind"],  # 'CCS' or 'PCS'
        short_strike=float(body["short_strike"]),
        long_strike=float(body["long_strike"]),
        qty=int(body["qty"]),
        entry_credit=float(body["entry_credit"]),
        stop_multiplier=float(body.get("stop_multiplier", s.spread_stop_multiplier)),
        tp_close_debit=float(body.get("tp_close_debit", s.spread_tp_close_debit)),
    )
    return await place_credit_spread(
        s.ibkr, spec, confirm=body.get("confirm", False),
        ticket_id=body.get("ticket_id"),
    )


@router.post("/live/kill")
async def live_kill(request: Request) -> dict[str, Any]:
    """Cancel every working IBKR order under this client_id."""
    from fef_sidecar.engine.live_trading import kill_all_orders
    s = _state(request)
    return await kill_all_orders(s.ibkr)


@router.get("/live/orders")
async def live_orders(status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    rows = await SignalIterRepository.query_live_orders(status=status, limit=limit)
    return [
        {"id": r.id, "submitted_at": r.submitted_at.isoformat(), "symbol": r.symbol,
         "strike": r.strike, "right": r.right, "expiry": r.expiry, "action": r.action,
         "qty": r.qty, "order_type": r.order_type, "limit_price": r.limit_price,
         "parent_id": r.parent_id, "bracket_role": r.bracket_role,
         "status": r.status, "filled_qty": r.filled_qty, "avg_fill_price": r.avg_fill_price,
         "commission": r.commission, "dry_run": r.dry_run, "ib_order_id": r.ib_order_id,
         # BE ladder state (parent rows only — surfaces on the UI)
         "entry_spx_price": r.entry_spx_price,
         "spx_target_stop": r.spx_target_stop,
         "dynamic_stop_level": r.dynamic_stop_level,
         "be_ladder_max_step_R": r.be_ladder_max_step_R}
        for r in rows
    ]


@router.get("/live/positions")
async def live_positions(request: Request, limit: int = 100) -> list[dict[str, Any]]:
    """Return currently-open positions from IBKR, enriched with the most
    recent LivePosition DB snapshot per contract (which has fresh greeks +
    market_price + unrealized_pnl populated by position_monitor_loop
    every ~5s).

    IBKR is authoritative for "is this open" (so closed positions disappear
    immediately); DB snapshot is authoritative for greeks + P/L numbers.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import LivePosition
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from sqlmodel import select as _s
    import time as _t

    s = _state(request)
    if not (isinstance(s.ibkr, LiveIBKRProvider) and getattr(s.ibkr, "_ib", None) is not None):
        return []
    ib = s.ibkr._ib  # type: ignore[attr-defined]
    if not ib.isConnected():
        return []

    # Get currently-open positions from IBKR
    open_positions = []
    for p in ib.positions():
        if abs(p.position) < 0.5:
            continue
        c = p.contract
        if getattr(c, "secType", "") != "OPT":
            continue
        open_positions.append({
            "symbol": getattr(c, "symbol", ""),
            "strike": float(getattr(c, "strike", 0)),
            "right": getattr(c, "right", ""),
            "expiry": getattr(c, "lastTradeDateOrContractMonth", ""),
            "qty": int(p.position),
            "avg_cost": float(p.avgCost) / 100.0 if p.avgCost else None,
        })

    if not open_positions:
        return []

    # Pull the most recent LivePosition snapshot per contract for greeks/PnL.
    # position_monitor_loop writes a fresh row every ~5s. We get the latest
    # per (symbol, strike, right, expiry) via a wide query + dict reduction.
    async with _gs() as session:
        recent_snaps = list((await session.exec(
            _s(LivePosition).order_by(LivePosition.snapshot_ts.desc()).limit(500)  # type: ignore[attr-defined]
        )).all())
    latest_snap: dict[str, LivePosition] = {}
    for snap in recent_snaps:
        key = f"{snap.symbol}|{snap.strike}|{snap.right}|{snap.expiry}"
        if key not in latest_snap:
            latest_snap[key] = snap

    # Merge: IBKR for liveness + qty, DB snapshot for greeks/PnL
    out: list[dict[str, Any]] = []
    for op in open_positions:
        key = f"{op['symbol']}|{op['strike']}|{op['right']}|{op['expiry']}"
        snap = latest_snap.get(key)
        # If the snapshot's qty matches IBKR's qty (sanity), trust the snapshot
        # for greeks/PnL. If qty mismatched (e.g. partial fill mid-snapshot),
        # still show the latest greeks since the contract is the same.
        out.append({
            "id": snap.id if snap else None,
            "snapshot_ts": (snap.snapshot_ts.isoformat() if snap
                            else _t.strftime("%Y-%m-%dT%H:%M:%S")),
            "symbol": op["symbol"], "strike": op["strike"],
            "right": op["right"], "expiry": op["expiry"],
            "qty": op["qty"],
            "avg_cost": op["avg_cost"] if op["avg_cost"] is not None
                        else (snap.avg_cost if snap else None),
            "market_price": snap.market_price if snap else None,
            "unrealized_pnl": (
                # Recompute with current qty if we have a market_price
                round((snap.market_price - (op["avg_cost"] or snap.avg_cost or 0))
                      * op["qty"] * 100, 2)
                if snap and snap.market_price is not None and (op["avg_cost"] is not None or snap.avg_cost)
                else None
            ),
            "delta": snap.delta if snap else None,
            "gamma": snap.gamma if snap else None,
            "theta": snap.theta if snap else None,
            "vega": snap.vega if snap else None,
            "iv": snap.iv if snap else None,
        })
    return out


@router.get("/live/status")
async def live_status() -> dict[str, Any]:
    """Show whether live trading is enabled + safety gates."""
    from fef_sidecar.engine.live_trading import is_live_trading_enabled
    return {
        "live_trading_enabled": is_live_trading_enabled(),
        "live_trading_env_var": "FEF_LIVE_TRADING",
        "confirm_required_per_order": True,
        "dry_run_default": True,
    }


@router.get("/calendar")
async def get_calendar(from_date: str | None = None, to_date: str | None = None) -> list[dict[str, Any]]:
    rows = await SignalIterRepository.query_calendar_events(from_date=from_date, to_date=to_date)
    return [
        {
            "id": r.id, "event_date": r.event_date, "event_time_et": r.event_time_et,
            "category": r.category, "title": r.title, "impact": r.impact,
            "auto_pause_minutes_before": r.auto_pause_minutes_before,
            "auto_pause_minutes_after": r.auto_pause_minutes_after, "notes": r.notes,
        }
        for r in rows
    ]


@router.post("/calendar")
async def create_calendar_event(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    from fef_sidecar.db.models import CalendarEvent
    event = CalendarEvent(
        event_date=body["event_date"],
        event_time_et=body.get("event_time_et"),
        category=body.get("category", "OTHER"),
        title=body["title"],
        impact=body.get("impact", "MEDIUM"),
        auto_pause_minutes_before=int(body.get("auto_pause_minutes_before", 15)),
        auto_pause_minutes_after=int(body.get("auto_pause_minutes_after", 30)),
        notes=body.get("notes", ""),
    )
    saved = await SignalIterRepository.upsert_calendar_event(event)
    await SignalIterRepository.write_audit_log(
        category="calendar", actor="user",
        summary=f"Added {event.category}: {event.title}",
        details={"event_id": saved.id},
    )
    return {"ok": True, "id": saved.id}


@router.get("/calendar/pause_check")
async def calendar_pause_check() -> dict[str, Any]:
    from fef_sidecar.engine.calendar import is_pause_window
    d = await is_pause_window()
    return {"paused": d.paused, "reason": d.reason, "event": d.event}


@router.get("/analytics/attribution")
async def analytics_attribution() -> dict[str, Any]:
    from fef_sidecar.engine.perf_attribution import attribution
    result = await attribution()
    return {
        k: [
            {"bucket": s.bucket, "trades": s.trades, "wins": s.wins, "losses": s.losses,
             "total_pnl": s.total_pnl, "avg_pnl": s.avg_pnl, "win_rate": s.win_rate}
            for s in v
        ]
        for k, v in result.items()
    }


@router.get("/cross_symbol")
async def cross_symbol(window_seconds: int = 120) -> dict[str, Any]:
    from fef_sidecar.engine.cross_symbol import compute_cross_symbol_view
    v = await compute_cross_symbol_view(window_seconds=window_seconds)
    return {
        "symbols_seen": v.symbols_seen,
        "alignment_score": v.alignment_score,
        "composite_direction": v.composite_direction,
        "composite_confidence": v.composite_confidence,
        "per_symbol": v.per_symbol,
    }


@router.get("/audit_log")
async def audit_log(category: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    rows = await SignalIterRepository.query_audit_log(category=category, limit=limit)
    return [
        {"id": r.id, "timestamp": r.timestamp.isoformat(), "category": r.category,
         "actor": r.actor, "summary": r.summary, "details": r.details}
        for r in rows
    ]


@router.get("/history/bars")
async def history_bars(
    symbol: str = "SPX",
    bar_size: str = "1 min",
    from_ms: int | None = None,
    to_ms: int | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Persisted OHLCV bars. Used by the chart date picker to render any
    past day without re-hitting IBKR."""
    rows = await SignalIterRepository.query_bars(
        symbol=symbol.upper(), bar_size=bar_size,
        from_ms=from_ms, to_ms=to_ms, limit=limit,
    )
    return [
        {
            "timestamp_ms": r.timestamp_ms,
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume,
        }
        for r in rows
    ]


@router.get("/history/flow")
async def history_flow(
    symbol: str = "SPX",
    session_date: str | None = None,
    from_ms: int | None = None,
    to_ms: int | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Persisted QuantData flow snapshots — walls, drift, max pain, IVR.
    Replay any past day's flow evolution."""
    rows = await SignalIterRepository.query_flow_snapshots(
        symbol=symbol.upper(), session_date=session_date,
        from_ms=from_ms, to_ms=to_ms, limit=limit,
    )
    return [
        {
            "timestamp_ms": r.timestamp_ms,
            "session_date": r.session_date,
            "spx_price": r.spx_price,
            "top_gex_walls": r.top_gex_walls,
            "top_dex_walls": r.top_dex_walls,
            "drift_cum_net_m": r.drift_cum_net_m,
            "drift_bias": r.drift_bias,
            "ntm_max_pain": r.ntm_max_pain,
            "raw_max_pain": r.raw_max_pain,
            "aggression_call_pct": r.aggression_call_pct,
            "aggression_put_pct": r.aggression_put_pct,
            "iv_rank_call_pct": r.iv_rank_call_pct,
            "iv_rank_put_pct": r.iv_rank_put_pct,
        }
        for r in rows
    ]


@router.get("/history/microstructure")
async def history_microstructure(
    symbol: str = "ES",
    from_ms: int | None = None,
    to_ms: int | None = None,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    """Persisted microstructure samples. Symbol here is the PROXY
    (ES/NQ/RTY/SPY/QQQ/IWM) the engine actually subscribed to."""
    rows = await SignalIterRepository.query_microstructure(
        symbol=symbol.upper(), from_ms=from_ms, to_ms=to_ms, limit=limit,
    )
    return [
        {
            "timestamp_ms": r.timestamp_ms,
            "ofi_window": r.ofi_window,
            "avi_pct": r.avi_pct,
            "aggressive_buy_volume": r.aggressive_buy_volume,
            "aggressive_sell_volume": r.aggressive_sell_volume,
            "microprice": r.microprice,
            "last_trade_price": r.last_trade_price,
            "microprice_deviation_bp": r.microprice_deviation_bp,
            "direction": r.direction,
            "confidence": r.confidence,
        }
        for r in rows
    ]


@router.get("/history/dates")
async def history_dates(symbol: str = "SPX") -> list[str]:
    """List YYYY-MM-DD dates with persisted flow snapshots for a symbol.
    Powers the chart's date picker."""
    return await SignalIterRepository.list_historical_dates(symbol.upper())


@router.get("/history/signals")
async def history_signals(
    request: Request,
    limit: int = 50,
    session_date: str | None = None,
) -> list[dict[str, Any]]:
    rows = await SignalIterRepository.recent(limit=limit, session_date=session_date)
    return [_signal_to_dict(r) for r in rows]


@router.get("/history/actionable")
async def history_actionable(
    request: Request,
    session_date: str | None = None,
) -> list[dict[str, Any]]:
    rows = await SignalIterRepository.actionable_today(session_date=session_date)
    return [_signal_to_dict(r) for r in rows]


@router.post("/run_iter_now")
async def run_iter_now(request: Request) -> dict[str, Any]:
    """Manually trigger one FEF iter (bypasses the 2-min cadence + cache)."""
    s = _state(request)
    try:
        await _run_one_iter(s, force_fresh=True)
        return {"ok": True, "iter_num": s.last_iter_num, "verdict": s.last_verdict}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# IMPROVEMENTS #24: UI-tunable config (no env-var editing needed)
@router.get("/config")
async def get_config() -> dict[str, Any]:
    return load_config().model_dump()


@router.post("/config")
async def update_config(
    request: Request,
    updates: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Update one or more settings + hot-reload engine thresholds."""
    s = _state(request)
    current = load_config()
    merged = UIConfig.model_validate(current.model_dump() | updates)
    save_config(merged)

    # Hot-reload engine thresholds (those that don't require restart)
    s.engine._wall_proximity_pts = merged.wall_proximity_pts  # type: ignore[attr-defined]
    s.engine._wall_collapse_pct = merged.wall_collapse_pct  # type: ignore[attr-defined]
    s.engine._wall_collapse_floor_m = merged.wall_collapse_floor_m  # type: ignore[attr-defined]

    return {
        "ok": True,
        "config": merged.model_dump(),
        "hot_reloaded": ["wall_proximity_pts", "wall_collapse_pct", "wall_collapse_floor_m"],
        "requires_restart": ["fef_loop_interval_sec", "quantdata_poll_interval_sec"],
    }


@router.get("/history/tickets")
async def history_tickets(limit: int = 100, session_date: str | None = None) -> list[dict[str, Any]]:
    """List TradeTicket rows. Use ?session_date=YYYY-MM-DD to filter to one
    session, or omit for the most recent N across all sessions.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import TradeTicket
    from sqlmodel import select as _s
    async with _gs() as session:
        stmt = _s(TradeTicket).order_by(TradeTicket.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
        rows = list((await session.exec(stmt)).all())
    out = []
    for r in rows:
        if session_date and r.created_at.strftime("%Y-%m-%d") != session_date:
            continue
        out.append({
            "id": r.id, "signal_iter_id": r.signal_iter_id,
            "created_at": r.created_at.isoformat(),
            "direction": r.direction.value if hasattr(r.direction, "value") else str(r.direction),
            "contract_strike": r.contract_strike, "contract_expiry": r.contract_expiry,
            "qty": r.qty, "entry_limit": r.entry_limit,
            "t1": r.t1, "t2": r.t2, "t3": r.t3, "stop": r.stop,
            "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            "filled_at": r.filled_at.isoformat() if r.filled_at else None,
            "fill_price": r.fill_price,
            "exit_t1_price": r.exit_t1_price, "exit_t2_price": r.exit_t2_price,
            "exit_t3_price": r.exit_t3_price, "exit_stop_price": r.exit_stop_price,
            "closed_at": r.closed_at.isoformat() if r.closed_at else None,
            "pnl_dollars": r.pnl_dollars, "notes": r.notes,
        })
    return out


# IMPROVEMENTS #20: create a ticket from the current actionable verdict
@router.post("/tickets")
async def create_ticket(
    request: Request,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Create a TradeTicket linked to a SignalIter.

    Frontend posts this when user clicks 'Copy to clipboard' so we have a
    record to later attach exit prices to via /tickets/{id}/close.
    """
    from fef_sidecar.db.models import FEFDirection, TicketStatus, TradeTicket

    signal_iter_id = int(body["signal_iter_id"])
    ticket = TradeTicket(
        signal_iter_id=signal_iter_id,
        direction=FEFDirection(body["direction"]),
        contract_strike=float(body["contract_strike"]),
        contract_expiry=str(body["contract_expiry"]),
        qty=int(body.get("qty", 15)),
        entry_limit=float(body["entry_limit"]),
        t1=float(body["t1"]),
        t2=float(body["t2"]),
        t3=float(body["t3"]),
        stop=float(body["stop"]),
        status=TicketStatus(body.get("status", "DRAFT")),
        notes=str(body.get("notes", "")),
    )
    saved = await SignalIterRepository.attach_ticket(signal_iter_id, ticket)
    return {"ok": True, "ticket_id": saved.id, "status": saved.status.value}


# IMPROVEMENTS #20: record trade outcome — updates ticket + linked signal
@router.post("/tickets/{ticket_id}/close")
async def close_ticket(
    ticket_id: int,
    request: Request,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Record exit prices + pnl on a ticket. Frontend posts when user closes a trade."""
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import TicketStatus, TradeTicket

    async with get_session() as session:
        ticket = (await session.exec(select(TradeTicket).where(TradeTicket.id == ticket_id))).first()
        if ticket is None:
            raise HTTPException(404, f"Ticket {ticket_id} not found")
        from datetime import UTC, datetime

        if "fill_price" in body:
            ticket.fill_price = body["fill_price"]
            ticket.filled_at = datetime.now(UTC)
        if "exit_t1_price" in body:
            ticket.exit_t1_price = body["exit_t1_price"]
        if "exit_t2_price" in body:
            ticket.exit_t2_price = body["exit_t2_price"]
        if "exit_t3_price" in body:
            ticket.exit_t3_price = body["exit_t3_price"]
        if "exit_stop_price" in body:
            ticket.exit_stop_price = body["exit_stop_price"]
            ticket.status = TicketStatus.STOPPED
        if "pnl_dollars" in body:
            ticket.pnl_dollars = body["pnl_dollars"]
        if "status" in body:
            ticket.status = TicketStatus(body["status"])
        if "notes" in body:
            ticket.notes = body["notes"]
        if ticket.status in (TicketStatus.CLOSED, TicketStatus.STOPPED):
            ticket.closed_at = datetime.now(UTC)
        session.add(ticket)
        await session.flush()
        return {"ok": True, "ticket_id": ticket_id, "status": ticket.status.value}


# IMPROVEMENTS #C: strategy presets — save/apply named threshold sets
@router.get("/presets")
async def list_presets() -> list[dict[str, Any]]:
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import StrategyPreset

    async with get_session() as session:
        rows = (await session.exec(select(StrategyPreset).order_by(StrategyPreset.name))).all()
        return [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "fef_loop_interval_sec": p.fef_loop_interval_sec,
                "wall_proximity_pts": p.wall_proximity_pts,
                "wall_collapse_pct": p.wall_collapse_pct,
                "wall_collapse_floor_m": p.wall_collapse_floor_m,
                "absorption_fe_ratio_min": p.absorption_fe_ratio_min,
            }
            for p in rows
        ]


@router.post("/presets")
async def create_preset(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    from datetime import UTC, datetime

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import StrategyPreset

    async with get_session() as session:
        preset = StrategyPreset(
            name=str(body["name"]),
            description=str(body.get("description", "")),
            fef_loop_interval_sec=int(body.get("fef_loop_interval_sec", 120)),
            wall_proximity_pts=float(body.get("wall_proximity_pts", 5.0)),
            wall_collapse_pct=float(body.get("wall_collapse_pct", 0.20)),
            wall_collapse_floor_m=float(body.get("wall_collapse_floor_m", 5.0)),
            absorption_fe_ratio_min=float(body.get("absorption_fe_ratio_min", 5.0)),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(preset)
        await session.flush()
        return {"ok": True, "preset_id": preset.id}


@router.delete("/presets/{preset_id}")
async def delete_preset(preset_id: int) -> dict[str, Any]:
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import StrategyPreset

    async with get_session() as session:
        preset = (await session.exec(select(StrategyPreset).where(StrategyPreset.id == preset_id))).first()
        if preset is None:
            raise HTTPException(404, f"Preset {preset_id} not found")
        await session.delete(preset)
        return {"ok": True}


@router.post("/presets/seed_defaults")
async def seed_default_presets() -> dict[str, Any]:
    """Insert built-in strategy presets if not already present (idempotent)."""
    from datetime import UTC, datetime

    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import StrategyPreset
    from fef_sidecar.engine.preset_templates import DEFAULT_PRESETS

    added = []
    async with get_session() as session:
        for tpl in DEFAULT_PRESETS:
            existing = (
                await session.exec(select(StrategyPreset).where(StrategyPreset.name == tpl["name"]))
            ).first()
            if existing:
                continue
            session.add(
                StrategyPreset(
                    name=tpl["name"],
                    description=tpl["description"],
                    fef_loop_interval_sec=tpl["fef_loop_interval_sec"],
                    wall_proximity_pts=tpl["wall_proximity_pts"],
                    wall_collapse_pct=tpl["wall_collapse_pct"],
                    wall_collapse_floor_m=tpl["wall_collapse_floor_m"],
                    absorption_fe_ratio_min=tpl["absorption_fe_ratio_min"],
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            added.append(tpl["name"])
    return {"ok": True, "added": added}


@router.post("/presets/{preset_id}/apply")
async def apply_preset(preset_id: int, request: Request) -> dict[str, Any]:
    """Hot-apply preset thresholds to the live engine."""
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import StrategyPreset

    s = _state(request)
    async with get_session() as session:
        preset = (await session.exec(select(StrategyPreset).where(StrategyPreset.id == preset_id))).first()
        if preset is None:
            raise HTTPException(404, f"Preset {preset_id} not found")
        s.engine._wall_proximity_pts = preset.wall_proximity_pts  # type: ignore[attr-defined]
        s.engine._wall_collapse_pct = preset.wall_collapse_pct  # type: ignore[attr-defined]
        s.engine._wall_collapse_floor_m = preset.wall_collapse_floor_m  # type: ignore[attr-defined]
        s.engine._absorption_fe_min = preset.absorption_fe_ratio_min  # type: ignore[attr-defined]
        return {
            "ok": True,
            "applied": preset.name,
            "hot_reloaded": ["wall_proximity_pts", "wall_collapse_pct", "wall_collapse_floor_m", "absorption_fe_ratio_min"],
            "requires_restart_for": ["fef_loop_interval_sec"],
        }


# IMPROVEMENTS #Q: chart annotations — user-marked horizontal levels
@router.get("/annotations")
async def list_annotations() -> list[dict[str, Any]]:
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import ChartAnnotation

    async with get_session() as session:
        rows = (await session.exec(select(ChartAnnotation))).all()
        return [
            {
                "id": a.id,
                "price": a.price,
                "label": a.label,
                "color": a.color,
                "line_style": a.line_style,
            }
            for a in rows
        ]


@router.post("/annotations")
async def create_annotation(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import ChartAnnotation

    async with get_session() as session:
        a = ChartAnnotation(
            price=float(body["price"]),
            label=str(body.get("label", "")),
            color=str(body.get("color", "#d29922")),
            line_style=str(body.get("line_style", "dashed")),
        )
        session.add(a)
        await session.flush()
        return {"ok": True, "id": a.id}


@router.delete("/annotations/{annotation_id}")
async def delete_annotation(annotation_id: int) -> dict[str, Any]:
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import ChartAnnotation

    async with get_session() as session:
        a = (await session.exec(select(ChartAnnotation).where(ChartAnnotation.id == annotation_id))).first()
        if a is None:
            raise HTTPException(404, f"Annotation {annotation_id} not found")
        await session.delete(a)
        return {"ok": True}


# Live broker scorecard — realized + unrealized P/L from IBKR
@router.get("/account")
async def get_account(request: Request) -> dict[str, Any] | None:
    s = _state(request)
    if s.last_account is None:
        # Fall back to a one-shot fetch if poll hasn't run yet
        summary = await s.ibkr.get_account_summary()
        if summary is None:
            return None
        s.last_account = summary
    a = s.last_account
    return {
        "account": a.account,
        "timestamp_ms": a.timestamp_ms,
        "net_liquidation": a.net_liquidation,
        "buying_power": a.buying_power,
        "available_funds": a.available_funds,
        "daily_pnl": a.daily_pnl,
        "realized_pnl": a.realized_pnl,
        "unrealized_pnl": a.unrealized_pnl,
        "gross_position_value": a.gross_position_value,
        "open_positions_count": a.open_positions_count,
        "open_options_count": a.open_options_count,
        "currency": a.currency,
    }


# Historical bars at any symbol + timeframe (for chart symbol/timeframe switcher)
@router.get("/bars")
async def get_bars(
    request: Request,
    symbol: str = "SPX",
    bar_size: str = "2 mins",
    duration: str = "1 D",
) -> list[dict[str, Any]]:
    """Return historical OHLCV bars.

    bar_size: '1 min', '2 mins', '5 mins', '15 mins', '30 mins', '1 hour',
              '4 hours', '1 day', '1 week'
    """
    from fef_sidecar.ibkr.models import Contract

    s = _state(request)
    try:
        contract = Contract.for_symbol(symbol)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    # For live IBKR we'd use reqHistoricalData with bar_size. For mock, we
    # aggregate the scripted day frames if scripted mode is on, else replay
    # the default static bars.
    import os as _os

    if type(s.ibkr).__name__ == "MockIBKRProvider":
        # Coherent mock — use PriceTrack so bar timestamps + prices align
        # with what the chart's live ticks see at the same moment.
        from fef_sidecar.mock import get_track
        track = get_track()
        bar_size_minutes = {
            "1 min": 1, "2 mins": 2, "5 mins": 5, "15 mins": 15, "30 mins": 30,
            "1 hour": 60, "4 hours": 240, "1 day": 390, "1 week": 1950,
        }.get(bar_size, 1)
        bars_raw = track.build_bars(bar_size_minutes=bar_size_minutes)
        sym = symbol.upper()
        if sym == "QQQ":
            transform = lambda v: round(v / 14.5, 2)
        elif sym == "IWM":
            transform = lambda v: round(v / 33.0, 2)
        elif sym == "SPY":
            transform = lambda v: round(v / 10.0, 2)
        elif sym == "ES":
            transform = lambda v: round(v - 5.0, 2)
        else:
            transform = lambda v: v
        bars = [
            {
                "timestamp_ms": b["timestamp_ms"],
                "open": transform(b["open"]),
                "high": transform(b["high"]),
                "low": transform(b["low"]),
                "close": transform(b["close"]),
                "volume": b["volume"],
            }
            for b in bars_raw
        ]
        # Skip the old aggregation branches below
        return bars
    elif False:
        # Static morning bars from mock with per-symbol offset/scale
        sym = symbol.upper()
        # QQQ ~ SPX / 14.5, IWM ~ SPX / 33, SPY ~ SPX / 10, ES = SPX - 5
        if sym == "QQQ":
            transform = lambda v: round(v / 14.5, 2)
        elif sym == "IWM":
            transform = lambda v: round(v / 33.0, 2)
        elif sym == "SPY":
            transform = lambda v: round(v / 10.0, 2)
        elif sym == "ES":
            transform = lambda v: round(v - 5.0, 2)
        else:
            transform = lambda v: v
        bars = [
            {
                "timestamp_ms": int(b.timestamp.timestamp() * 1000),
                "open": transform(b.open),
                "high": transform(b.high),
                "low": transform(b.low),
                "close": transform(b.close),
                "volume": b.volume,
            }
            for b in s.ibkr._bars  # type: ignore[attr-defined]
        ]
    else:
        # Live IBKR: use the dedicated historical connection (client_id+100)
        # so this doesn't conflict with the streaming bar_stream_loop's
        # keepUpToDate subscription on the main connection. Without this,
        # daily-timeframe requests on the same contract as the active
        # bar_stream silently return 0 bars.
        try:
            hist_bars = await s.ibkr.fetch_historical_bars(  # type: ignore[attr-defined]
                contract, bar_size=bar_size, duration=duration,
                use_rth=False, what_to_show="TRADES",
            )
        except TimeoutError as e:
            raise HTTPException(504, str(e)) from e
        except Exception as e:
            raise HTTPException(503, f"IBKR historical fetch failed for {symbol}: {e}") from e
        scale = 1.0  # no scaling — real prices
        # Re-wrap as objects with .date/.open/etc. attrs so the loop below
        # (shared with mock paths) doesn't need to branch
        ib_bars = [
            type("X", (), {"date": b.timestamp, "open": b.open, "high": b.high,
                            "low": b.low, "close": b.close, "volume": b.volume})()
            for b in hist_bars
        ]
        from datetime import datetime as _dt
        bars = []
        for b in ib_bars:
            ts = b.date
            if hasattr(ts, "timestamp"):
                ts_ms = int(ts.timestamp() * 1000)
            elif isinstance(ts, str):
                try:
                    ts_ms = int(_dt.fromisoformat(ts.replace(" ", "T")).timestamp() * 1000)
                except Exception:
                    continue
            else:
                continue
            bars.append({
                "timestamp_ms": ts_ms,
                "open": round(float(b.open) * scale, 2),
                "high": round(float(b.high) * scale, 2),
                "low": round(float(b.low) * scale, 2),
                "close": round(float(b.close) * scale, 2),
                "volume": float(b.volume or 0),
            })
    return bars


def _aggregate_scripted_bars(symbol: str, bar_size: str, max_frame: int | None = None) -> list[dict[str, Any]]:
    """Aggregate the scripted day script into bars at the requested timeframe.

    Each frame is 2 simulated minutes. Per-symbol transform: QQQ ~ SPX/14.5,
    IWM ~ SPX/33, SPY ~ SPX/10, ES = SPX - 5. max_frame: only include
    frames up to this index (1-based). None = all.
    """
    from datetime import datetime, time, timedelta

    from fef_sidecar.quantdata.mock_day import DAY_SCRIPT

    size_minutes = {
        "1 min": 1, "2 mins": 2, "5 mins": 5, "15 mins": 15, "30 mins": 30,
        "1 hour": 60, "4 hours": 240, "1 day": 390, "1 week": 1950,
    }.get(bar_size, 2)
    frames_per_bar = max(1, round(size_minutes / 2))

    sym = symbol.upper()
    if sym == "QQQ":
        transform = lambda v: round(v / 14.5, 2)
    elif sym == "IWM":
        transform = lambda v: round(v / 33.0, 2)
    elif sym == "SPY":
        transform = lambda v: round(v / 10.0, 2)
    elif sym == "ES":
        transform = lambda v: round(v - 5.0, 2)
    else:
        transform = lambda v: v
    today = datetime.now().date()
    market_open = datetime.combine(today, time(9, 30))

    limit = max_frame if max_frame is not None else len(DAY_SCRIPT)
    frames = DAY_SCRIPT[:limit]

    out: list[dict[str, Any]] = []
    for i in range(0, len(frames), frames_per_bar):
        chunk = frames[i : i + frames_per_bar]
        if not chunk:
            continue
        opens = transform(chunk[0].spx)
        closes = transform(chunk[-1].spx)
        highs = transform(max(f.spx for f in chunk)) + 0.5
        lows = transform(min(f.spx for f in chunk)) - 0.5
        ts = market_open + timedelta(minutes=chunk[0].minutes_since_open)
        out.append({
            "timestamp_ms": int(ts.timestamp() * 1000),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": 1000 * frames_per_bar,
        })
    return out


# IMPROVEMENTS #D: option chain ladder — N strikes around spot for 0DTE expiry
@router.get("/option_chain")
async def option_chain(
    request: Request,
    strike_range: int = 30,
    expiry: str | None = None,
) -> dict[str, Any]:
    """Return calls + puts at strikes within ±strike_range of current SPX.

    expiry: YYYYMMDD; defaults to today's session_date for 0DTE.
    """
    from datetime import datetime, timedelta, timezone

    from fef_sidecar.ibkr.models import Contract, ContractKind

    s = _state(request)
    if expiry is None:
        et = timezone(timedelta(hours=-4))
        expiry = datetime.now(et).strftime("%Y%m%d")

    spx = s.last_spx_price or 7400.0
    # Round to nearest 5 then ±strike_range in 5-pt steps
    center = round(spx / 5) * 5
    strikes = [center + d for d in range(-strike_range, strike_range + 1, 5)]

    calls: list[dict[str, Any]] = []
    puts: list[dict[str, Any]] = []

    async def _fetch(strike: float, right: str):
        contract = Contract(
            symbol="SPX",
            kind=ContractKind.OPTION,
            exchange="CBOE",
            expiry=expiry,
            strike=strike,
            right=right,
        )
        try:
            q = await s.ibkr.get_option_quote(contract)
            return {
                "strike": strike,
                "right": right,
                "last": q.last,
                "bid": q.bid,
                "ask": q.ask,
                "delta": q.delta,
            }
        except Exception:
            return None

    import asyncio as _asyncio

    call_results = await _asyncio.gather(*[_fetch(k, "C") for k in strikes])
    put_results = await _asyncio.gather(*[_fetch(k, "P") for k in strikes])
    for r in call_results:
        if r:
            calls.append(r)
    for r in put_results:
        if r:
            puts.append(r)

    return {"spx_price": spx, "expiry": expiry, "calls": calls, "puts": puts}


# IMPROVEMENTS #R: pattern library — user-defined setups
@router.get("/patterns/fields")
async def list_pattern_fields() -> list[dict[str, Any]]:
    """List available fields with type info for the condition builder UI."""
    from fef_sidecar.engine.patterns import FIELDS

    return [
        {
            "name": f.name,
            "label": f.label,
            "kind": f.kind,
            "enum_values": list(f.enum_values),
            "description": f.description,
        }
        for f in FIELDS
    ]


@router.get("/patterns")
async def list_patterns() -> list[dict[str, Any]]:
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import Pattern

    async with get_session() as session:
        rows = (await session.exec(select(Pattern).order_by(Pattern.name))).all()
        return [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "conditions": p.conditions or [],
                "enabled": p.enabled,
                "audio_alert": p.audio_alert,
                "toast_alert": p.toast_alert,
                "match_count": p.match_count,
                "last_matched_at": p.last_matched_at.isoformat() if p.last_matched_at else None,
            }
            for p in rows
        ]


@router.post("/patterns")
async def create_pattern(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import Pattern

    async with get_session() as session:
        p = Pattern(
            name=str(body["name"]),
            description=str(body.get("description", "")),
            conditions=body.get("conditions", []),
            enabled=bool(body.get("enabled", True)),
            audio_alert=bool(body.get("audio_alert", True)),
            toast_alert=bool(body.get("toast_alert", True)),
        )
        session.add(p)
        await session.flush()
        return {"ok": True, "id": p.id}


@router.put("/patterns/{pattern_id}")
async def update_pattern(pattern_id: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import Pattern

    async with get_session() as session:
        p = (await session.exec(select(Pattern).where(Pattern.id == pattern_id))).first()
        if p is None:
            raise HTTPException(404, f"Pattern {pattern_id} not found")
        for k in ("name", "description", "conditions", "enabled", "audio_alert", "toast_alert"):
            if k in body:
                setattr(p, k, body[k])
        session.add(p)
        return {"ok": True}


@router.delete("/patterns/{pattern_id}")
async def delete_pattern(pattern_id: int) -> dict[str, Any]:
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import Pattern

    async with get_session() as session:
        p = (await session.exec(select(Pattern).where(Pattern.id == pattern_id))).first()
        if p is None:
            raise HTTPException(404, f"Pattern {pattern_id} not found")
        await session.delete(p)
        return {"ok": True}


@router.post("/patterns/{pattern_id}/toggle")
async def toggle_pattern(pattern_id: int) -> dict[str, Any]:
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import Pattern

    async with get_session() as session:
        p = (await session.exec(select(Pattern).where(Pattern.id == pattern_id))).first()
        if p is None:
            raise HTTPException(404, f"Pattern {pattern_id} not found")
        p.enabled = not p.enabled
        session.add(p)
        return {"ok": True, "enabled": p.enabled}


@router.get("/patterns/{pattern_id}/matches")
async def pattern_matches(pattern_id: int, limit: int = 50) -> list[dict[str, Any]]:
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import PatternMatch

    async with get_session() as session:
        stmt = (
            select(PatternMatch)
            .where(PatternMatch.pattern_id == pattern_id)
            .order_by(PatternMatch.matched_at.desc())  # type: ignore[attr-defined]
            .limit(limit)
        )
        rows = (await session.exec(stmt)).all()
        return [
            {
                "id": m.id,
                "pattern_id": m.pattern_id,
                "signal_iter_id": m.signal_iter_id,
                "matched_at": m.matched_at.isoformat() if m.matched_at else None,
                "fields_snapshot": m.fields_snapshot,
            }
            for m in rows
        ]


@router.post("/patterns/test")
async def test_pattern(
    request: Request,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Test a conditions list against the most recent iter (returns matched + per-condition debug).

    Body: { 'conditions': [...] }
    """
    from fef_sidecar.engine.patterns import evaluate_pattern, extract_fields
    from fef_sidecar.engine.fef import snapshot_to_inputs

    s = _state(request)
    if s.last_snapshot is None or not s.last_verdict:
        raise HTTPException(400, "No iter to test against — run /run_iter_now first")

    inputs = snapshot_to_inputs(s.last_snapshot)  # type: ignore[arg-type]
    # Reconstruct a partial verdict for field extraction
    from types import SimpleNamespace

    fake_verdict = SimpleNamespace(
        direction=SimpleNamespace(value=s.last_verdict.get("direction", "WAIT")),
        confidence=SimpleNamespace(value=s.last_verdict.get("confidence", "NONE")),
        nearest_wall_distance=s.last_verdict.get("nearest_wall_distance"),
        delta_price=s.last_verdict.get("delta_price"),
        delta_drift_m=s.last_verdict.get("delta_drift_m"),
        fe_ratio=s.last_verdict.get("fe_ratio"),
        aligned=s.last_verdict.get("aligned", False),
        absorption=s.last_verdict.get("absorption", False),
    )

    fields = extract_fields(inputs, fake_verdict, vix=s.last_vix, iter_num=s.last_iter_num)
    matched, results = evaluate_pattern(body.get("conditions", []), fields)
    return {"matched": matched, "conditions": results, "field_values": fields}


@router.post("/patterns/seed_defaults")
async def seed_default_patterns() -> dict[str, Any]:
    """Insert built-in template patterns if not already present (idempotent)."""
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import Pattern
    from fef_sidecar.engine.patterns import DEFAULT_TEMPLATES

    added = []
    async with get_session() as session:
        for tpl in DEFAULT_TEMPLATES:
            existing = (
                await session.exec(select(Pattern).where(Pattern.name == tpl["name"]))
            ).first()
            if existing:
                continue
            session.add(
                Pattern(
                    name=tpl["name"],
                    description=tpl["description"],
                    conditions=tpl["conditions"],
                    enabled=True,
                )
            )
            added.append(tpl["name"])
    return {"ok": True, "added": added}


# Walk-forward parameter sweep — find threshold params that maximize Sharpe
@router.post("/optimize/sweep")
async def optimize_sweep(
    request: Request,
    body: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """Grid-search FEF thresholds over historical signals.

    Body: { 'days': 30, 'proximity': [3,5,8], 'collapse_pct': [0.15,0.20,0.30],
            'collapse_floor': [3,5,10], 'absorption_fe': [4,5,8],
            'lookahead_iters': 3, 'train_fraction': 0.6 }
    All keys optional — defaults used otherwise.
    """
    from datetime import UTC, datetime, timedelta
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import SignalIter
    from fef_sidecar.engine.optimization import SweepConfig, run_sweep

    days = int(body.get("days", 30))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    async with get_session() as session:
        rows = (await session.exec(
            select(SignalIter)
            .where(SignalIter.timestamp >= cutoff)  # type: ignore[arg-type]
            .order_by(SignalIter.timestamp.asc())  # type: ignore[attr-defined]
        )).all()

    iters_data = [
        {
            "spx_price": s.spx_price,
            "drift_cum_net_m": s.drift_cum_net_m,
            "drift_bias": s.drift_bias,
            "ntm_max_pain": s.ntm_max_pain,
            "raw_max_pain": s.raw_max_pain,
            "top_gex_walls": s.top_gex_walls,
            "top_dex_walls": s.top_dex_walls,
            "aggression_call_pct": s.aggression_call_pct,
            "aggression_put_pct": s.aggression_put_pct,
        }
        for s in rows
    ]
    if len(iters_data) < 10:
        return {"error": f"need >=10 iters, only have {len(iters_data)}"}

    cfg = SweepConfig(
        proximity_values=body.get("proximity", [3.0, 5.0, 8.0, 12.0]),
        collapse_pct_values=body.get("collapse_pct", [0.15, 0.20, 0.30]),
        collapse_floor_values=body.get("collapse_floor", [3.0, 5.0, 10.0]),
        absorption_fe_values=body.get("absorption_fe", [4.0, 5.0, 8.0]),
        lookahead_iters=int(body.get("lookahead_iters", 3)),
    )
    train_frac = float(body.get("train_fraction", 0.6))

    import time as _t
    t0 = _t.perf_counter()
    results = run_sweep(iters_data, cfg, train_fraction=train_frac)
    elapsed_ms = round((_t.perf_counter() - t0) * 1000, 1)

    return {
        "elapsed_ms": elapsed_ms,
        "total_combos": len(results),
        "iters_in_window": len(iters_data),
        "train_fraction": train_frac,
        "top_results": [
            {
                "wall_proximity_pts": r.wall_proximity_pts,
                "wall_collapse_pct": r.wall_collapse_pct,
                "wall_collapse_floor_m": r.wall_collapse_floor_m,
                "absorption_fe_ratio_min": r.absorption_fe_ratio_min,
                "train_sharpe": r.train_sharpe,
                "test_sharpe": r.test_sharpe,
                "train_trades": r.train_trades,
                "test_trades": r.test_trades,
                "train_win_rate_pct": r.train_win_rate_pct,
                "test_win_rate_pct": r.test_win_rate_pct,
                "train_total_pnl": r.train_total_pnl,
                "test_total_pnl": r.test_total_pnl,
            }
            for r in results[:20]
        ],
    }


# Pattern backtest — run conditions against historical signals + return stats
@router.post("/patterns/{pattern_id}/backtest")
async def backtest_pattern(pattern_id: int, days: int = 30) -> dict[str, Any]:
    """Walk every SignalIter in the window, eval pattern conditions, aggregate
    matches + outcomes from linked TradeTickets."""
    from datetime import UTC, datetime, timedelta
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import Pattern, SignalIter, TicketStatus, TradeTicket
    from fef_sidecar.engine.backtest import TradeOutcome, aggregate_backtest
    from fef_sidecar.engine.patterns import evaluate_pattern

    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    async with get_session() as session:
        pattern = (await session.exec(select(Pattern).where(Pattern.id == pattern_id))).first()
        if pattern is None:
            raise HTTPException(404, f"Pattern {pattern_id} not found")

        iters = (await session.exec(
            select(SignalIter).where(SignalIter.timestamp >= cutoff)  # type: ignore[arg-type]
        )).all()

        matches: list[int] = []
        for s in iters:
            # Reconstruct field_values from stored row (matches extract_fields output)
            extras = s.extras or {}
            fields = {
                "spx_price": s.spx_price,
                "drift_cum_net_m": s.drift_cum_net_m,
                "drift_bias": s.drift_bias,
                "ntm_max_pain": s.ntm_max_pain,
                "ntm_mp_distance": s.spx_price - s.ntm_max_pain,
                "ntm_mp_distance_abs": abs(s.spx_price - s.ntm_max_pain),
                "raw_max_pain": s.raw_max_pain,
                "top_gex_strike": (s.top_gex_walls[0]["strike"] if s.top_gex_walls else None),
                "top_gex_net_m": (s.top_gex_walls[0]["net_m"] if s.top_gex_walls else None),
                "top_dex_strike": (s.top_dex_walls[0]["strike"] if s.top_dex_walls else None),
                "top_dex_net_m": (s.top_dex_walls[0]["net_m"] if s.top_dex_walls else None),
                "wall_proximity_pts": extras.get("nearest_wall_distance"),
                "aggression_call_pct": s.aggression_call_pct,
                "aggression_put_pct": s.aggression_put_pct,
                "aggression_diff": (
                    s.aggression_call_pct - s.aggression_put_pct
                    if s.aggression_call_pct is not None and s.aggression_put_pct is not None
                    else None
                ),
                "iv_rank_call_pct": s.iv_rank_call_pct,
                "iv_rank_put_pct": s.iv_rank_put_pct,
                "delta_price": extras.get("delta_price"),
                "delta_drift_m": extras.get("delta_drift_m"),
                "fe_ratio": extras.get("fe_ratio"),
                "aligned": extras.get("aligned", False),
                "absorption": extras.get("absorption", False),
                "direction": s.direction.value,
                "confidence": s.confidence.value,
                "iter_num": s.iter_num,
                "minutes_since_open": None,  # not stored; skip time-based conditions
                "vix": None,                  # not stored per iter; skip vix conditions
            }
            matched, _ = evaluate_pattern(pattern.conditions or [], fields)
            if matched and s.id is not None:
                matches.append(s.id)

        # Pull outcomes for matched signal iters
        outcomes: list[TradeOutcome] = []
        if matches:
            tickets = (await session.exec(
                select(TradeTicket).where(
                    TradeTicket.signal_iter_id.in_(matches)  # type: ignore[attr-defined]
                )
            )).all()
            iter_by_id = {s.id: s for s in iters if s.id is not None}
            for t in tickets:
                if t.status not in (TicketStatus.CLOSED, TicketStatus.STOPPED):
                    continue
                pnl = t.pnl_dollars or 0.0
                sig = iter_by_id.get(t.signal_iter_id)
                if not sig:
                    continue
                outcomes.append(TradeOutcome(
                    iter_id=sig.id or 0,
                    iter_num=sig.iter_num,
                    session_date=sig.session_date,
                    pnl_dollars=pnl,
                    won=pnl > 0,
                ))

        unique_dates = len({s.session_date for s in iters})
        result = aggregate_backtest(
            pattern_id=pattern_id,
            pattern_name=pattern.name,
            window_days=days,
            total_iters_scanned=len(iters),
            matches=matches,
            outcomes=outcomes,
            unique_dates=unique_dates,
        )

        return {
            "pattern_id": result.pattern_id,
            "pattern_name": result.pattern_name,
            "window_days": result.window_days,
            "total_iters_scanned": result.total_iters_scanned,
            "total_matches": result.total_matches,
            "matches_with_trades": result.matches_with_trades,
            "matches_per_day": result.matches_per_day,
            "wins": result.wins,
            "losses": result.losses,
            "win_rate_pct": result.win_rate_pct,
            "win_rate_ci_low": result.win_rate_ci_low,
            "win_rate_ci_high": result.win_rate_ci_high,
            "total_pnl": result.total_pnl,
            "avg_pnl_per_trade": result.avg_pnl_per_trade,
            "best_trade": result.best_trade,
            "worst_trade": result.worst_trade,
            "max_drawdown": result.max_drawdown,
            "sharpe_ratio": result.sharpe_ratio,
            "t_stat_vs_zero": result.t_stat_vs_zero,
            "cumulative_pnl_curve": result.cumulative_pnl_curve,
        }


# Risk overlay — configurable daily loss cap, max position size, circuit breaker
@router.get("/risk/state")
async def get_risk_state(request: Request) -> dict[str, Any]:
    s = _state(request)
    from fef_sidecar.config_store import load_config

    cfg = load_config()
    daily_pnl = (s.last_account.daily_pnl if s.last_account else None) or 0.0
    cap = getattr(cfg, "daily_loss_cap_dollars", None)
    max_pos = getattr(cfg, "max_position_dollars", None)
    max_contracts = getattr(cfg, "max_contracts_per_trade", 25)

    cap_used_pct = None
    if cap and cap > 0:
        cap_used_pct = round(min(100.0, abs(min(0, daily_pnl)) / cap * 100), 1)
    breached = bool(cap and cap > 0 and daily_pnl <= -cap)

    return {
        "daily_pnl": daily_pnl,
        "daily_loss_cap": cap,
        "cap_used_pct": cap_used_pct,
        "breached": breached,
        "max_position_dollars": max_pos,
        "max_contracts_per_trade": max_contracts,
    }


# Analytics — aggregated stats across stored signals + trades
@router.get("/analytics/summary")
async def analytics_summary(days: int = 30) -> dict[str, Any]:
    """Aggregate stats over the last `days` sessions.

    Returns:
        win_rate_by_confidence: dict[confidence -> {wins, losses, total, pct}]
        win_rate_by_direction:  dict[direction -> {wins, losses, total, pct}]
        win_rate_by_pattern:    dict[pattern_name -> {wins, losses, total, pct}]
        pnl_by_hour:            dict[hour_et -> total_pnl_dollars]
        pattern_frequency:      dict[pattern_name -> match_count]
        recent_sessions:        list of {date, total_iters, actionable, pnl}
    """
    from datetime import UTC, datetime, timedelta
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import DailySession, Pattern, PatternMatch, SignalIter, TicketStatus, TradeTicket

    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    async with get_session() as session:
        # Pull all tickets in window with their linked signal
        tickets = (await session.exec(
            select(TradeTicket).where(TradeTicket.created_at >= cutoff)  # type: ignore[arg-type]
        )).all()

        # Map ticket -> signal for confidence/direction lookup
        signal_ids = list({t.signal_iter_id for t in tickets})
        signals_by_id: dict[int, SignalIter] = {}
        if signal_ids:
            sigs = (await session.exec(
                select(SignalIter).where(SignalIter.id.in_(signal_ids))  # type: ignore[attr-defined]
            )).all()
            signals_by_id = {s.id: s for s in sigs if s.id is not None}

        def _bucket(d: dict, key: str, won: bool, pnl: float) -> None:
            b = d.setdefault(key, {"wins": 0, "losses": 0, "total": 0, "pnl": 0.0})
            b["total"] += 1
            b["pnl"] += pnl
            if won:
                b["wins"] += 1
            else:
                b["losses"] += 1

        wr_conf: dict[str, dict[str, Any]] = {}
        wr_dir: dict[str, dict[str, Any]] = {}
        pnl_by_hour: dict[int, float] = {}

        for t in tickets:
            if t.status not in (TicketStatus.CLOSED, TicketStatus.STOPPED):
                continue
            pnl = t.pnl_dollars or 0.0
            won = pnl > 0
            sig = signals_by_id.get(t.signal_iter_id)
            if sig:
                _bucket(wr_conf, sig.confidence.value, won, pnl)
            _bucket(wr_dir, t.direction.value, won, pnl)
            if t.filled_at:
                hour = t.filled_at.hour
                pnl_by_hour[hour] = pnl_by_hour.get(hour, 0.0) + pnl

        # Add percentage fields
        def _add_pct(d: dict) -> dict:
            for v in d.values():
                v["pct"] = round((v["wins"] / v["total"] * 100), 1) if v["total"] > 0 else 0.0
            return d

        # Pattern win rate via PatternMatch -> SignalIter -> TradeTicket join
        wr_pattern: dict[str, dict[str, Any]] = {}
        matches = (await session.exec(
            select(PatternMatch).where(PatternMatch.matched_at >= cutoff)  # type: ignore[arg-type]
        )).all()
        patterns_by_id = {
            p.id: p for p in (await session.exec(select(Pattern))).all() if p.id is not None
        }
        pattern_frequency = {p.name: 0 for p in patterns_by_id.values()}
        # Index tickets by signal_iter_id for quick lookup
        tickets_by_signal: dict[int, list[TradeTicket]] = {}
        for t in tickets:
            tickets_by_signal.setdefault(t.signal_iter_id, []).append(t)

        for m in matches:
            p = patterns_by_id.get(m.pattern_id)
            if not p:
                continue
            pattern_frequency[p.name] = pattern_frequency.get(p.name, 0) + 1
            for t in tickets_by_signal.get(m.signal_iter_id, []):
                if t.status not in (TicketStatus.CLOSED, TicketStatus.STOPPED):
                    continue
                pnl = t.pnl_dollars or 0.0
                _bucket(wr_pattern, p.name, pnl > 0, pnl)

        # Recent daily sessions
        recent = (await session.exec(
            select(DailySession).order_by(DailySession.session_date.desc()).limit(days)  # type: ignore[attr-defined]
        )).all()

        return {
            "window_days": days,
            "win_rate_by_confidence": _add_pct(wr_conf),
            "win_rate_by_direction": _add_pct(wr_dir),
            "win_rate_by_pattern": _add_pct(wr_pattern),
            "pnl_by_hour_et": {str(h): round(v, 2) for h, v in sorted(pnl_by_hour.items())},
            "pattern_frequency": pattern_frequency,
            "recent_sessions": [
                {
                    "date": s.session_date,
                    "total_iters": s.total_iters,
                    "actionable": s.actionable_signals,
                    "trades": s.trades_taken,
                    "net_pnl": round(s.net_pnl, 2),
                    "wins": s.win_count,
                    "losses": s.loss_count,
                }
                for s in recent
            ],
        }


# IMPROVEMENTS #19: replay a past session — return all iters in order
@router.get("/history/replay/{date}")
async def replay_session(date: str) -> list[dict[str, Any]]:
    """Return all iters for the given session_date in chronological order."""
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import SignalIter

    async with get_session() as session:
        stmt = (
            select(SignalIter)
            .where(SignalIter.session_date == date)
            .order_by(SignalIter.iter_num.asc())  # type: ignore[attr-defined]
        )
        rows = (await session.exec(stmt)).all()
        return [_signal_to_dict(r) for r in rows]


def _signal_to_dict(r) -> dict[str, Any]:
    return {
        "id": r.id,
        "iter_num": r.iter_num,
        "session_date": r.session_date,
        "timestamp": r.timestamp.isoformat() if r.timestamp else None,
        "spx_price": r.spx_price,
        "direction": r.direction.value if r.direction else None,
        "confidence": r.confidence.value if r.confidence else None,
        "reasoning": r.reasoning,
        "drift_cum_net_m": r.drift_cum_net_m,
        "drift_bias": r.drift_bias,
        "ntm_max_pain": r.ntm_max_pain,
        "raw_max_pain": r.raw_max_pain,
        "top_gex_walls": r.top_gex_walls,
        "top_dex_walls": r.top_dex_walls,
        "aggression_call_pct": r.aggression_call_pct,
        "aggression_put_pct": r.aggression_put_pct,
        "extras": r.extras,
    }
