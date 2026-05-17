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
    return {"ok": True, "active_symbol": s.active_symbol, "changed": True}


# IMPROVEMENTS #9: engine state introspection — "why did/didn't FEF fire?"
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


@router.post("/live/place_bracket")
async def live_place_bracket(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Place a bracket order. Two-gate safety:
       1. FEF_LIVE_TRADING=true env required
       2. body.confirm=true required
    Without both → DRY_RUN (writes records but doesn't hit IBKR)."""
    from fef_sidecar.engine.live_trading import BracketSpec, place_bracket
    s = _state(request)
    spec = BracketSpec(
        symbol=body["symbol"], strike=float(body["strike"]),
        right=body["right"], expiry=body["expiry"],
        qty=int(body["qty"]),
        entry_limit=float(body["entry_limit"]),
        t1_limit=float(body["t1_limit"]), t2_limit=float(body["t2_limit"]),
        t3_limit=float(body["t3_limit"]), stop_limit=float(body["stop_limit"]),
        t1_qty=int(body.get("t1_qty", body["qty"] // 3)),
        t2_qty=int(body.get("t2_qty", body["qty"] // 3)),
        t3_qty=int(body.get("t3_qty", body["qty"] - 2 * (body["qty"] // 3))),
    )
    return await place_bracket(
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
         "commission": r.commission, "dry_run": r.dry_run, "ib_order_id": r.ib_order_id}
        for r in rows
    ]


@router.get("/live/positions")
async def live_positions(limit: int = 100) -> list[dict[str, Any]]:
    rows = await SignalIterRepository.query_live_positions(limit=limit)
    return [
        {"id": r.id, "snapshot_ts": r.snapshot_ts.isoformat(),
         "symbol": r.symbol, "strike": r.strike, "right": r.right, "expiry": r.expiry,
         "qty": r.qty, "avg_cost": r.avg_cost, "market_price": r.market_price,
         "unrealized_pnl": r.unrealized_pnl, "delta": r.delta, "gamma": r.gamma,
         "theta": r.theta, "vega": r.vega, "iv": r.iv}
        for r in rows
    ]


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

    if _os.environ.get("FEF_MOCK_SCRIPTED_DAY", "false").lower() in ("true", "1", "yes"):
        max_frame = getattr(s.ibkr, "_scripted_index", 0) + 1
        bars = _aggregate_scripted_bars(symbol, bar_size, max_frame=max_frame)
    elif type(s.ibkr).__name__ == "MockIBKRProvider":
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
        # Live IBKR: do a one-shot historical fetch
        from ib_async import util as ib_util  # type: ignore[import-not-found]

        ib_contract = type(s.ibkr)._to_ib_contract(contract)  # type: ignore[attr-defined]
        await s.ibkr._ensure_connected()  # type: ignore[attr-defined]
        ib = s.ibkr._ib  # type: ignore[attr-defined]
        ib_bars = await ib.reqHistoricalDataAsync(
            ib_contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES" if symbol.upper() in ("ES",) else "MIDPOINT",
            useRTH=True,
            formatDate=2,
        )
        bars = [
            {
                "timestamp_ms": int(b.date.timestamp() * 1000) if hasattr(b.date, "timestamp") else 0,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": float(b.volume or 0),
            }
            for b in ib_bars
        ]
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
