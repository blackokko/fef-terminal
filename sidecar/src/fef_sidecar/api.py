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
    }


# IMPROVEMENTS #9: engine state introspection — "why did/didn't FEF fire?"
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
        bars = _aggregate_scripted_bars(symbol, bar_size)
    elif type(s.ibkr).__name__ == "MockIBKRProvider":
        # Static morning bars from mock, optionally offset for ES
        offset = -5.0 if symbol.upper() == "ES" else 0.0
        bars = [
            {
                "timestamp_ms": int(b.timestamp.timestamp() * 1000),
                "open": b.open + offset,
                "high": b.high + offset,
                "low": b.low + offset,
                "close": b.close + offset,
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


def _aggregate_scripted_bars(symbol: str, bar_size: str) -> list[dict[str, Any]]:
    """Aggregate the scripted day script into bars at the requested timeframe.

    Each frame is 2 simulated minutes. ES adds a -5pt basis offset.
    """
    from datetime import datetime, time, timedelta

    from fef_sidecar.quantdata.mock_day import DAY_SCRIPT

    # Map bar_size -> minutes per bar
    size_minutes = {
        "1 min": 1, "2 mins": 2, "5 mins": 5, "15 mins": 15, "30 mins": 30,
        "1 hour": 60, "4 hours": 240, "1 day": 390, "1 week": 1950,
    }.get(bar_size, 2)
    # Frames per bar (each frame = 2 sim minutes)
    frames_per_bar = max(1, round(size_minutes / 2))

    offset = -5.0 if symbol.upper() == "ES" else 0.0
    today = datetime.now().date()
    market_open = datetime.combine(today, time(9, 30))

    out: list[dict[str, Any]] = []
    for i in range(0, len(DAY_SCRIPT), frames_per_bar):
        chunk = DAY_SCRIPT[i : i + frames_per_bar]
        if not chunk:
            continue
        opens = chunk[0].spx + offset
        closes = chunk[-1].spx + offset
        highs = max(f.spx for f in chunk) + offset + 0.5
        lows = min(f.spx for f in chunk) + offset - 0.5
        ts = market_open + timedelta(minutes=chunk[0].minutes_since_open)
        out.append({
            "timestamp_ms": int(ts.timestamp() * 1000),
            "open": round(opens, 2),
            "high": round(highs, 2),
            "low": round(lows, 2),
            "close": round(closes, 2),
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
