"""REST routes — history reads + manual iter trigger."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

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
