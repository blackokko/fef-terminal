"""REST routes — history reads + manual iter trigger."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from fastapi import Body

from fef_sidecar.config_store import UIConfig, load_config, save_config
from fef_sidecar.db import SignalIterRepository
from fef_sidecar.loop import _get_cached_bars, _run_one_iter

# NOTE (2026-07-08): this binding was MISSING while six handlers already
# called logger.exception(...) in their error paths — each was a latent
# NameError that would replace the real error. Defining it fixes all of
# them plus the F4 config-change audit's failure path.
logger = logging.getLogger(__name__)
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


@router.get("/theta_flow/status")
async def theta_flow_status(request: Request) -> dict[str, Any]:
    """Live ThetaData flow-ingest read (2026-07-15 input-parity fix): the
    holder published by theta_flow_ingest_loop (drift_theta_cum_m, staleness,
    finalized-minute stats) plus the active flow_source config. Read-only —
    additive observability for the shadow-validation window."""
    s = _state(request)
    from fef_sidecar.config_store import load_config
    try:
        flow_source = load_config().flow_source
    except Exception:
        flow_source = "qd"
    return {
        "flow_source": flow_source,
        "holder": dict(s.theta_flow) if isinstance(s.theta_flow, dict) else None,
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
    # Reset the regime detector window — samples carry the previous
    # symbol's `spx_price`, and mixing TLT (~$82) with SPX (~$7400) breaks
    # range-based regime calculations (e.g. OPEN_DRIVE rationale shows
    # 7292pt range). Regime thresholds are SPX-tuned anyway; pre-switch
    # samples are not meaningful cross-symbol.
    try:
        s.regime_detector.reset()
        s.last_regime = None
    except Exception:
        pass
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


@router.get("/regime/live")
async def regime_live(request: Request) -> dict[str, Any]:
    """VALIDATED live regime tiles — the Market Regime panel's data source.

    Replaces the retired 'vibe' classifier display (RegimeDetector's
    PIN_DAY/TREND_DAY/... labels — unvalidated, mislabeled 5/27, consumed by
    nothing in the trading path). Tiles: range_regime (chop gate r30 TOD
    percentile + live gate effect), expected_move (VIX tercile + 30m rvol
    percentile + post-capitulation flag), flow_bias (drift bias + veto armed),
    events (FOMC/CPI/NFP/OPEX/EOM calendar), dealer_gamma (exposure snapshot).
    Fail-open: every tile degrades to n/a on missing data; never 500s.

    The old /regime/lookback + /regime/debug endpoints stay functional for
    back-compat but the panel no longer uses them."""
    from fef_sidecar.engine.regime_live import build_regime_live
    return await build_regime_live(_state(request))


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
async def ml_train(request: Request) -> dict[str, Any]:
    from fef_sidecar.engine.ml_patterns import train_and_persist
    return await train_and_persist(state=_state(request))


@router.get("/ml/proposals")
async def ml_proposals(top_k: int = 10) -> dict[str, Any]:
    """Surface candidate Pattern rules mined from training data.

    These are NOT engine rules — they're proposals the user reviews and
    explicitly accepts into the Pattern table via /ml/proposals/accept.
    """
    from fef_sidecar.engine.pattern_proposals import propose_patterns
    items = await propose_patterns(top_k=top_k)
    return {"ok": True, "proposals": items}


@router.post("/ml/proposals/accept")
async def ml_proposals_accept(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Persist an accepted proposal as a row in the `pattern` table.

    Body: {feature, operator, threshold, name?, notes?}.
    Writes a single-condition Pattern that the live pattern-matcher will
    then evaluate alongside user-defined patterns.
    """
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import Pattern

    feature = body["feature"]
    operator = body["operator"]
    threshold = float(body["threshold"])
    default_name = f"ml: {feature} {operator} {threshold:g}"
    name = body.get("name") or default_name
    notes = body.get("notes") or "ML proposal"

    async with get_session() as session:
        existing = (await session.exec(
            select(Pattern).where(Pattern.name == name)  # type: ignore[attr-defined]
        )).first()
        if existing is not None:
            return {"ok": False, "error": f"Pattern '{name}' already exists", "pattern_id": existing.id}
        pat = Pattern(
            name=name,
            conditions=[{"field": feature, "operator": operator, "value": threshold}],
            description=notes,
            enabled=True,
        )
        session.add(pat)
        await session.flush()
        return {"ok": True, "pattern_id": pat.id, "name": pat.name}


@router.get("/ml/runs")
async def ml_runs(limit: int = 20) -> list[dict[str, Any]]:
    rows = await SignalIterRepository.query_ml_model_runs(limit=limit)
    return [
        {"id": r.id, "trained_at": r.trained_at.isoformat(), "algorithm": r.algorithm,
         "n_training_rows": r.n_training_rows, "n_features": r.n_features,
         "label_kind": getattr(r, "label_kind", "binary_win"),
         "cv_mae": getattr(r, "cv_mae", None),
         "cv_r2": getattr(r, "cv_r2", None),
         "cv_n_folds": getattr(r, "cv_n_folds", None),
         "model_path": getattr(r, "model_path", None),
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
    from fef_sidecar.engine.auto_trader import _floor_kill_switch_active
    from fef_sidecar.engine.live_trading import is_live_trading_enabled
    import time as _t
    now_ms = int(_t.time() * 1000)
    elapsed = (now_ms - s.auto_trade_last_at_ms) / 1000 if s.auto_trade_last_at_ms else None
    cool_remaining = max(0, s.auto_trade_cool_down_sec - elapsed) if elapsed is not None else 0
    # SOURCE OF TRUTH (2026-07-08): the floor the gate actually enforces is
    # the config_store value — report THAT, never the display-legacy
    # state.auto_trade_confidence_floor the panel toggle used to stomp.
    try:
        _cfg = load_config()
        cfg_floor = _cfg.auto_trade_confidence_floor
        cfg_qty = _cfg.auto_trade_qty_override
    except Exception:
        cfg_floor = "HIGH"  # gate fails restrictive the same way
        cfg_qty = 3         # gate falls back to the validated default size
    return {
        "enabled": s.auto_trade_enabled,
        "live_trading_env": is_live_trading_enabled(),
        "confidence_floor": cfg_floor,
        # False only under the FEF_DISABLE_CONFIDENCE_FLOOR kill switch
        # (fail-open, audited once per session by the gate itself).
        "floor_enforced": not _floor_kill_switch_active(),
        "cool_down_sec": s.auto_trade_cool_down_sec,
        "cool_down_remaining_sec": int(cool_remaining),
        "count_session": s.auto_trade_count_session,
        "last_at_ms": s.auto_trade_last_at_ms,
        "last_block_reason": s.auto_trade_last_block_reason,
        # SOURCE OF TRUTH (2026-07-09): the size the gate actually places is
        # the config_store value — report THAT, never the display-legacy
        # state.auto_trade_qty_override the panel toggle/preset used to set.
        "qty_override": cfg_qty,
        "trigger_types": s.auto_trade_trigger_types,
        "runner_protect_enabled": s.runner_protect_enabled,
        # STATUS TRUTH (2026-07-02): the scanner fires trades in parallel with
        # the main loop — surface its per-symbol activity here so the UI can
        # never again show 'verdict is WAIT' while scanner engines trade.
        "scanner": {
            "enabled_symbols": [
                sym for sym, on in s.scan_enabled_symbols.items() if on
            ],
            "max_per_symbol": s.auto_trade_max_per_symbol,
            "count_by_symbol": dict(s.auto_trade_count_by_symbol),
            "last_fire_at_ms_by_symbol": dict(s.auto_trade_last_at_ms_by_symbol),
            "last_verdict_by_symbol": dict(s.last_scanner_verdict_by_symbol),
        },
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
            "dollar_semantics": s.exit_dollar_semantics,
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
    if "dollar_semantics" in body:
        v = str(body["dollar_semantics"])
        if v in ("cumulative_realized", "whole_unrealized"):
            s.exit_dollar_semantics = v
    if "spread_stop_multiplier" in body: s.spread_stop_multiplier = float(body["spread_stop_multiplier"])
    if "spread_tp_close_debit" in body: s.spread_tp_close_debit = float(body["spread_tp_close_debit"])
    return {"ok": True}


# Phase-detector threshold profiles, calibrated by the 2026-07-01 backtest (866
# sessions, independent IBKR ruler). Both are FADE-only (ride_min=7 > 6-pillar
# max disables the losing max-thrust RIDE). "big_move" = high conviction (~6% of
# days, +8pt fav); "balanced" = more signals (min_move=12, ~0.4/session, +2.3pt,
# positive all 4 years). Small/"scalp" moves showed no edge and are not offered.
PHASE_PRESETS: dict[str, dict[str, float]] = {
    "big_move": {
        "window_sec": 300, "vel_sec": 75, "vel_ignite": 16.0, "vel_stall": 3.0,
        "min_move": 18.0, "bounce_pt": 3.0, "iv_delta": 0.006, "mp_overshoot": 8.0,
        "fade_min": 5, "ride_min": 7,
    },
    "balanced": {
        "window_sec": 300, "vel_sec": 75, "vel_ignite": 16.0, "vel_stall": 3.0,
        "min_move": 12.0, "bounce_pt": 3.0, "iv_delta": 0.006, "mp_overshoot": 8.0,
        "fade_min": 5, "ride_min": 7,
    },
}

_PHASE_KNOBS_F = ("vel_ignite", "vel_stall", "min_move", "bounce_pt", "iv_delta", "mp_overshoot")
_PHASE_KNOBS_I = ("window_sec", "vel_sec", "fade_min", "ride_min")


def _phase_config(s: Any) -> dict[str, Any]:
    return {
        "mode": s.phase_mode,
        "window_sec": s.phase_window_sec, "vel_sec": s.phase_vel_sec,
        "vel_ignite": s.phase_vel_ignite, "vel_stall": s.phase_vel_stall,
        "min_move": s.phase_min_move, "bounce_pt": s.phase_bounce_pt,
        "iv_delta": s.phase_iv_delta, "mp_overshoot": s.phase_mp_overshoot,
        "fade_min": s.phase_fade_min, "ride_min": s.phase_ride_min,
    }


@router.get("/phase_detector")
async def get_phase_detector(request: Request) -> dict[str, Any]:
    """Phase-detector config + latest live verdict. Feeds the settings panel."""
    s = _state(request)
    return {
        "config": _phase_config(s),
        "last_verdict": getattr(s, "phase_last_verdict", None),
        "presets": PHASE_PRESETS,
    }


@router.post("/phase_detector")
async def set_phase_detector(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Update phase-detector mode / thresholds, or apply a named preset. Nulls
    the live detector so it rebuilds with the new thresholds on the next poll."""
    from fef_sidecar.db.repository import SignalIterRepository
    s = _state(request)

    # Apply a named preset first (explicit knobs in the same body still win).
    preset_name = body.get("preset")
    if preset_name in PHASE_PRESETS:
        for k, v in PHASE_PRESETS[preset_name].items():
            setattr(s, f"phase_{k}", v)

    if "mode" in body:
        v = str(body["mode"]).lower()
        if v in ("off", "advisory", "active"):
            s.phase_mode = v
    for k in _PHASE_KNOBS_F:
        if k in body:
            setattr(s, f"phase_{k}", float(body[k]))
    for k in _PHASE_KNOBS_I:
        if k in body:
            setattr(s, f"phase_{k}", int(body[k]))

    # Force a rebuild with the new thresholds on the next poll-loop feed.
    s.phase_detector = None
    await SignalIterRepository.write_audit_log(
        category="phase_detector_config", actor="user",
        summary=(
            f"Phase detector mode={s.phase_mode}"
            + (f" preset={preset_name}" if preset_name in PHASE_PRESETS else "")
            + f" (min_move={s.phase_min_move}, fade_min={s.phase_fade_min}, ride_min={s.phase_ride_min})"
        ),
    )
    return {"ok": True, "config": _phase_config(s)}


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

    Body: {enabled: bool, confirm: bool, [cool_down_sec], [trigger_types]}

    confidence_floor in the body is DEPRECATED and IGNORED (2026-07-08):
    the panel's toggle stomped the in-memory floor 3+ times live. The
    enforced floor lives in config_store — change it ONLY via POST /config
    {"auto_trade_confidence_floor": ...} (persistent, hot-read by the gate).

    qty_override in the body is DEPRECATED and IGNORED (2026-07-09), same
    reason: it lived only in memory and reset to 0 on restart, and the gate's
    retired `or 15` fallback would then size a fire at 15 contracts (5x the
    operator's 3-lot). The enforced size lives in config_store — change it ONLY
    via POST /config {"auto_trade_qty_override": ...} (persistent, hot-read).

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

    try:
        cfg_floor = load_config().auto_trade_confidence_floor
    except Exception:
        cfg_floor = "HIGH"  # gate fails restrictive the same way

    old_enabled = s.auto_trade_enabled
    s.auto_trade_enabled = new_enabled
    if "confidence_floor" in body:
        # IGNORED by design — see docstring. One deprecation audit line when
        # the caller asked for something other than the enforced value, so a
        # stale panel build that still sends the field is visible in the log.
        requested_floor = str(body["confidence_floor"]).upper()
        if requested_floor != cfg_floor:
            await SignalIterRepository.write_audit_log(
                category="confidence_floor", actor="user",
                summary=(
                    f"DEPRECATED: /auto_trade/toggle sent confidence_floor="
                    f"{requested_floor} — IGNORED (enforced floor stays "
                    f"{cfg_floor}; change it via POST /config)"
                ),
                details={"requested": requested_floor, "config_floor": cfg_floor},
            )
    if "cool_down_sec" in body:
        s.auto_trade_cool_down_sec = max(60, int(body["cool_down_sec"]))
    if "qty_override" in body:
        # IGNORED by design (2026-07-09) — mirrors the confidence_floor field.
        # The enforced size lives in config_store (persistent, hot-read); the
        # toggle can no longer set it in-memory-only. One deprecation audit line
        # when a stale panel still sends the field and it differs from the
        # enforced value, so the divergence is visible in the log.
        try:
            cfg_qty = load_config().auto_trade_qty_override
        except Exception:
            cfg_qty = 3
        try:
            requested_qty = int(body["qty_override"])
        except (TypeError, ValueError):
            requested_qty = None
        if requested_qty is not None and requested_qty != cfg_qty:
            await SignalIterRepository.write_audit_log(
                category="auto_trade_qty", actor="user",
                summary=(
                    f"DEPRECATED: /auto_trade/toggle sent qty_override="
                    f"{requested_qty} — IGNORED (enforced size stays {cfg_qty}; "
                    f"change it via POST /config)"
                ),
                details={"requested": requested_qty, "config_qty": cfg_qty},
            )
    if "trigger_types" in body:
        valid = {"confluence", "aligned_only", "wall_only",
                 "flag_bull", "flag_bear", "pattern_bull", "pattern_bear"}
        types = [t for t in body["trigger_types"] if t in valid]
        if not types:
            types = ["confluence"]  # never allow empty — would block all fires silently
        s.auto_trade_trigger_types = types

    await SignalIterRepository.write_audit_log(
        category="auto_trade_toggle", actor="user",
        summary=f"Auto-trade {'ENABLED' if new_enabled else 'DISABLED'} "
                f"(was {'on' if old_enabled else 'off'}) — floor={cfg_floor} (config), "
                f"cool_down={s.auto_trade_cool_down_sec}s, per_symbol_cap={s.auto_trade_max_per_symbol}",
        details={
            "enabled": new_enabled, "confidence_floor": cfg_floor,
            "cool_down_sec": s.auto_trade_cool_down_sec,
            "max_per_symbol": s.auto_trade_max_per_symbol,
        },
    )
    return {
        "ok": True, "enabled": s.auto_trade_enabled,
        # The enforced (config_store) floor — the toggle can't change it.
        "confidence_floor": cfg_floor,
        "cool_down_sec": s.auto_trade_cool_down_sec,
        "max_per_symbol": s.auto_trade_max_per_symbol,
    }


# ---------------------------------------------------------------------------
# Auto-trade presets — save/load/apply named auto-trade settings
# Lets the user swap between Conservative/Standard/Aggressive (or custom) with
# one click instead of re-entering confidence_floor/cool_down/triggers/qty/
# runner-protect every session.
# ---------------------------------------------------------------------------

_AUTO_TRADE_DEFAULT_PRESETS = [
    {
        "name": "Conservative",
        "description": "HIGH conviction only, longer cool-down, confluence trigger — for choppy or uncertain sessions",
        "confidence_floor": "HIGH",
        "cool_down_sec": 600,
        "trigger_types": ["confluence"],
        "qty_override": 10,
        "runner_protect_enabled": True,
    },
    {
        "name": "Standard",
        "description": "HIGH floor, 5-min cool-down, confluence + aligned triggers, FEF auto-qty by confidence",
        "confidence_floor": "HIGH",
        "cool_down_sec": 300,
        "trigger_types": ["confluence", "aligned_only"],
        "qty_override": 0,
        "runner_protect_enabled": True,
    },
    {
        "name": "Aggressive",
        "description": "MEDIUM floor, 3-min cool-down, all triggers, larger size — trending high-confidence days",
        "confidence_floor": "MEDIUM",
        "cool_down_sec": 180,
        "trigger_types": ["confluence", "aligned_only"],
        "qty_override": 15,
        "runner_protect_enabled": False,
    },
    {
        # Pattern auto-trade — INERT until the user applies this preset (or
        # manually enables the pattern trigger types). Fires come from the
        # confirmed-bar pattern trigger loop (flags always; candle patterns
        # filtered by UIConfig.pattern_trade_kinds, default engulfing only)
        # and pass through every existing auto-trader guardrail.
        "name": "Pattern signals v1",
        "description": (
            "Chart-pattern trigger: flag breakouts + engulfing (per pattern_trade_kinds config), "
            "1m/5m confirmed bars, qty 3, 5-min cool-down. Apply to ARM pattern auto-trading; "
            "FEF flow triggers are NOT enabled by this preset."
        ),
        "confidence_floor": "LOW",
        "cool_down_sec": 300,
        "trigger_types": ["flag_bull", "flag_bear", "pattern_bull", "pattern_bear"],
        "qty_override": 3,
        "runner_protect_enabled": True,
    },
]


@router.get("/auto_trade/presets")
async def list_auto_trade_presets() -> list[dict[str, Any]]:
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import AutoTradePreset

    async with get_session() as session:
        rows = (await session.exec(select(AutoTradePreset).order_by(AutoTradePreset.name))).all()
        return [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "confidence_floor": p.confidence_floor,
                "cool_down_sec": p.cool_down_sec,
                "trigger_types": p.trigger_types,
                "qty_override": p.qty_override,
                "runner_protect_enabled": p.runner_protect_enabled,
            }
            for p in rows
        ]


@router.post("/auto_trade/presets")
async def create_auto_trade_preset(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    from datetime import UTC, datetime

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import AutoTradePreset

    async with get_session() as session:
        preset = AutoTradePreset(
            name=str(body["name"]).strip(),
            description=str(body.get("description", "")),
            confidence_floor=str(body.get("confidence_floor", "HIGH")).upper(),
            cool_down_sec=max(60, int(body.get("cool_down_sec", 300))),
            trigger_types=list(body.get("trigger_types", ["confluence"])) or ["confluence"],
            qty_override=max(0, min(100, int(body.get("qty_override", 0)))),
            runner_protect_enabled=bool(body.get("runner_protect_enabled", True)),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(preset)
        await session.flush()
        return {"ok": True, "preset_id": preset.id, "name": preset.name}


@router.delete("/auto_trade/presets/{preset_id}")
async def delete_auto_trade_preset(preset_id: int) -> dict[str, Any]:
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import AutoTradePreset

    async with get_session() as session:
        preset = (await session.exec(select(AutoTradePreset).where(AutoTradePreset.id == preset_id))).first()
        if preset is None:
            raise HTTPException(404, f"AutoTradePreset {preset_id} not found")
        name = preset.name
        await session.delete(preset)
        return {"ok": True, "deleted": name}


@router.post("/auto_trade/presets/seed_defaults")
async def seed_default_auto_trade_presets() -> dict[str, Any]:
    """Insert built-in auto-trade presets if not already present. Idempotent."""
    from datetime import UTC, datetime

    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import AutoTradePreset

    added: list[str] = []
    async with get_session() as session:
        for tpl in _AUTO_TRADE_DEFAULT_PRESETS:
            existing = (
                await session.exec(select(AutoTradePreset).where(AutoTradePreset.name == tpl["name"]))
            ).first()
            if existing:
                continue
            session.add(
                AutoTradePreset(
                    name=tpl["name"],
                    description=tpl["description"],
                    confidence_floor=tpl["confidence_floor"],
                    cool_down_sec=tpl["cool_down_sec"],
                    trigger_types=list(tpl["trigger_types"]),
                    qty_override=tpl["qty_override"],
                    runner_protect_enabled=tpl["runner_protect_enabled"],
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            added.append(tpl["name"])
    return {"ok": True, "added": added}


@router.post("/auto_trade/presets/{preset_id}/apply")
async def apply_auto_trade_preset(preset_id: int, request: Request) -> dict[str, Any]:
    """Apply a saved preset's settings to the live auto-trade state.

    Mutates state directly (same fields the toggle endpoint writes) plus the
    runner-protect flag. Does NOT enable auto-trade — user still controls
    enable/disable separately.
    """
    from sqlmodel import select

    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import AutoTradePreset
    from fef_sidecar.db.repository import SignalIterRepository

    s = _state(request)
    async with get_session() as session:
        preset = (await session.exec(select(AutoTradePreset).where(AutoTradePreset.id == preset_id))).first()
        if preset is None:
            raise HTTPException(404, f"AutoTradePreset {preset_id} not found")

        # NOTE (2026-07-08): state.auto_trade_confidence_floor is DISPLAY-
        # LEGACY — the armed floor gate reads config_store only, so a preset
        # can no longer lower enforcement. To actually change the floor:
        # POST /config {"auto_trade_confidence_floor": ...}. Review F3: the
        # response echoes the ENFORCED (config) floor, and a preset carrying
        # a different floor logs the same deprecation line as the toggle so
        # the display/enforcement divergence is visible in the audit log.
        # NOTE (2026-07-09): state.auto_trade_qty_override is DISPLAY-LEGACY
        # too — the gate reads the config_store size only, so a preset can no
        # longer set the traded size. Same deprecation line + POST /config
        # {"auto_trade_qty_override": ...} to actually change it.
        s.auto_trade_confidence_floor = preset.confidence_floor
        s.auto_trade_cool_down_sec = max(60, preset.cool_down_sec)
        s.auto_trade_qty_override = max(0, min(100, preset.qty_override))
        valid_triggers = {"confluence", "aligned_only", "wall_only",
                          "flag_bull", "flag_bear", "pattern_bull", "pattern_bear"}
        types = [t for t in preset.trigger_types if t in valid_triggers]
        s.auto_trade_trigger_types = types if types else ["confluence"]
        s.runner_protect_enabled = bool(preset.runner_protect_enabled)

        try:
            cfg_floor = load_config().auto_trade_confidence_floor
        except Exception:
            cfg_floor = "HIGH"  # gate fails restrictive the same way
        preset_floor = str(preset.confidence_floor or "").upper()
        if preset_floor and preset_floor != cfg_floor:
            await SignalIterRepository.write_audit_log(
                category="confidence_floor", actor="user",
                summary=(
                    f"DEPRECATED: preset '{preset.name}' carries confidence_floor="
                    f"{preset_floor} — IGNORED for enforcement (config floor stays "
                    f"{cfg_floor}; change it via POST /config)"
                ),
                details={"preset_id": preset_id, "requested": preset_floor,
                         "config_floor": cfg_floor},
            )

        # Same deprecation line for the size (2026-07-09): a preset can no
        # longer set the enforced contracts-per-fire (config_store owns it).
        try:
            cfg_qty = load_config().auto_trade_qty_override
        except Exception:
            cfg_qty = 3
        try:
            preset_qty = int(preset.qty_override)
        except (TypeError, ValueError):
            preset_qty = None
        if preset_qty is not None and preset_qty != cfg_qty:
            await SignalIterRepository.write_audit_log(
                category="auto_trade_qty", actor="user",
                summary=(
                    f"DEPRECATED: preset '{preset.name}' carries qty_override="
                    f"{preset_qty} — IGNORED for enforcement (config size stays "
                    f"{cfg_qty}; change it via POST /config)"
                ),
                details={"preset_id": preset_id, "requested": preset_qty,
                         "config_qty": cfg_qty},
            )

        await SignalIterRepository.write_audit_log(
            category="auto_trade_preset_apply", actor="user",
            summary=f"Applied auto-trade preset '{preset.name}'",
            details={
                "preset_id": preset_id, "name": preset.name,
                "confidence_floor": preset.confidence_floor,
                "cool_down_sec": preset.cool_down_sec,
                "trigger_types": preset.trigger_types,
                "qty_override": preset.qty_override,
                "runner_protect_enabled": preset.runner_protect_enabled,
            },
        )
        return {
            "ok": True,
            "applied": preset.name,
            "settings": {
                # The ENFORCED (config_store) floor + size — presets can't
                # change either; echo config, not the display-legacy state.
                "confidence_floor": cfg_floor,
                "cool_down_sec": s.auto_trade_cool_down_sec,
                "trigger_types": s.auto_trade_trigger_types,
                "qty_override": cfg_qty,
                "runner_protect_enabled": s.runner_protect_enabled,
            },
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
    enabled_count = sum(1 for on in s.scan_enabled_symbols.values() if on)
    implied_global = s.auto_trade_max_per_symbol * max(1, enabled_count)
    return {
        "enabled": dict(s.scan_enabled_symbols),
        "max_per_symbol": s.auto_trade_max_per_symbol,
        # Implied global = per-symbol cap × enabled symbols. No separate
        # global gate now; this field is for UI display only.
        "max_per_session_global": implied_global,
        "implied_global_cap": implied_global,
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
    before_enabled = dict(s.scan_enabled_symbols)
    before_cap = s.auto_trade_max_per_symbol
    if "enabled" in body and isinstance(body["enabled"], dict):
        for sym, on in body["enabled"].items():
            sym_u = str(sym).upper()
            if sym_u in s.scan_enabled_symbols:
                s.scan_enabled_symbols[sym_u] = bool(on)
    if "max_per_symbol" in body:
        s.auto_trade_max_per_symbol = max(1, min(100, int(body["max_per_symbol"])))
    # Audit scanner-enablement / cap changes. A max_per_symbol=50 change was
    # made invisibly on 7/1 (default is 2) — risk-relevant config edits must
    # leave a trail.
    changed_syms = {
        sym: on for sym, on in s.scan_enabled_symbols.items()
        if before_enabled.get(sym) != on
    }
    if changed_syms or before_cap != s.auto_trade_max_per_symbol:
        try:
            await SignalIterRepository.write_audit_log(
                category="config", actor="user",
                summary=(
                    "Scanner config changed: "
                    + (f"enabled {changed_syms} " if changed_syms else "")
                    + (
                        f"max_per_symbol {before_cap} -> {s.auto_trade_max_per_symbol}"
                        if before_cap != s.auto_trade_max_per_symbol else ""
                    )
                ).strip(),
                details={
                    "enabled_before": before_enabled,
                    "enabled_after": dict(s.scan_enabled_symbols),
                    "max_per_symbol_before": before_cap,
                    "max_per_symbol_after": s.auto_trade_max_per_symbol,
                },
            )
        except Exception:
            pass
    # Persist the enablement so a restart restores the pin instead of dropping
    # to all-False (2026-07-17 boot-stomp fix). Only which symbols the scanner
    # covers is persisted — NOT whether the trader is armed. Best-effort: a
    # failed write never blocks the toggle.
    try:
        from fef_sidecar.config_store import (
            load_config as _lc, merge_partial as _mp, save_config as _sc)
        _sc(_mp(_lc(), {"scan_enabled_symbols": dict(s.scan_enabled_symbols)}))
    except Exception:
        logger.exception("scanner/symbols: config persist failed")
    return {
        "ok": True,
        "enabled": dict(s.scan_enabled_symbols),
        "max_per_symbol": s.auto_trade_max_per_symbol,
    }


@router.get("/history/reviews_split")
async def history_reviews_split(limit: int = 30) -> list[dict[str, Any]]:
    """Per-session metrics with realized P/L split into paper vs live.

    Combines DailyReview rows (for dedup_expectancy_r / dedup_win_rate_pct
    — signal-quality metrics independent of execution channel) with
    per-session IBKRExecution sums split by account ID prefix:
      - account.startswith("DU") → paper
      - else → live

    Used by the ModelEvolution panel to render two graphs (paper + live).
    """
    from collections import defaultdict
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import DailyReview, IBKRExecution
    from sqlmodel import select as _s

    async with _gs() as session:
        reviews = list((await session.exec(
            _s(DailyReview).order_by(DailyReview.session_date.desc()).limit(limit)  # type: ignore[attr-defined]
        )).all())
        execs = list((await session.exec(_s(IBKRExecution))).all())

    def is_paper(acct: str | None) -> bool:
        return bool(acct and acct.startswith("DU"))

    # Group fills by (session_date, paper_or_live, contract_key) and
    # compute per-contract realized P/L (avg_sld - avg_bot) × paired × 100
    by_session: dict[tuple, dict] = defaultdict(
        lambda: {"bot_v": 0.0, "sld_v": 0.0, "bot_q": 0.0, "sld_q": 0.0}
    )
    for f in execs:
        channel = "paper" if is_paper(f.account) else "live"
        ckey = (f.symbol, f.strike, f.right, f.expiry)
        slot = by_session[(f.session_date, channel, ckey)]
        if f.side == "BOT":
            slot["bot_v"] += f.shares * f.price
            slot["bot_q"] += f.shares
        else:
            slot["sld_v"] += f.shares * f.price
            slot["sld_q"] += f.shares

    realized_by_session: dict[tuple, float] = defaultdict(float)
    for (sd, channel, _ck), slot in by_session.items():
        paired = min(slot["bot_q"], slot["sld_q"])
        if paired <= 0:
            continue
        avg_bot = slot["bot_v"] / slot["bot_q"] if slot["bot_q"] else 0
        avg_sld = slot["sld_v"] / slot["sld_q"] if slot["sld_q"] else 0
        realized_by_session[(sd, channel)] += (avg_sld - avg_bot) * paired * 100.0

    out = []
    for r in reviews:
        out.append({
            "session_date": r.session_date,
            "dedup_actionable": getattr(r, "dedup_actionable", 0),
            "dedup_wins": getattr(r, "dedup_wins", 0),
            "dedup_win_rate_pct": getattr(r, "dedup_win_rate_pct", 0.0),
            "dedup_expectancy_r": getattr(r, "dedup_expectancy_r", 0.0),
            "paper_realized_dollars": round(realized_by_session.get((r.session_date, "paper"), 0.0), 2),
            "live_realized_dollars": round(realized_by_session.get((r.session_date, "live"), 0.0), 2),
        })
    return out


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
         "by_hour_ct": r.by_hour_ct,
         "by_direction": getattr(r, "by_direction", {}),
         "by_regime": getattr(r, "by_regime", {}),
         "by_wall_distance": getattr(r, "by_wall_distance", {}),
         "by_fe_ratio": getattr(r, "by_fe_ratio", {}),
         "by_block_reason": getattr(r, "by_block_reason", {}),
         "actual_trades_taken": getattr(r, "actual_trades_taken", 0),
         "actual_wins": getattr(r, "actual_wins", 0),
         "actual_losses": getattr(r, "actual_losses", 0),
         "actual_total_R": getattr(r, "actual_total_R", 0.0),
         "dedup_actionable": getattr(r, "dedup_actionable", 0),
         "dedup_wins": getattr(r, "dedup_wins", 0),
         "dedup_stops": getattr(r, "dedup_stops", 0),
         "dedup_timeouts": getattr(r, "dedup_timeouts", 0),
         "dedup_win_rate_pct": getattr(r, "dedup_win_rate_pct", 0.0),
         "dedup_expectancy_r": getattr(r, "dedup_expectancy_r", 0.0),
         "sharpe_ratio": getattr(r, "sharpe_ratio", None),
         "sortino_ratio": getattr(r, "sortino_ratio", None),
         "max_drawdown_R": getattr(r, "max_drawdown_R", None),
         "profit_factor": getattr(r, "profit_factor", None),
         "ulcer_index": getattr(r, "ulcer_index", None)}
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
    # Use `>=` so the change-day itself counts as a clean session. The
    # engine change applies mid-day, but the user's convention is that
    # the day's overall data still represents a usable session. Was `>`
    # which made the change-day uncounted (off-by-one vs user tracking).
    sessions_after = sum(1 for r in reviews if r.session_date >= last_change_date)
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


@router.get("/diag/ibkr_executions")
async def diag_ibkr_executions(request: Request, days: int = 1) -> dict[str, Any]:
    """Pull every fill from IBKR for the last N days. Authoritative source
    for what actually executed — useful to cross-reference against our
    LiveOrder records. Returns raw executions + grouped by contract.
    """
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    s = _state(request)
    if not isinstance(s.ibkr, LiveIBKRProvider):
        return {"mode": "mock", "executions": [], "by_contract": {}}
    ib = getattr(s.ibkr, "_ib", None)
    if ib is None or not ib.isConnected():
        return {"mode": "disconnected"}

    try:
        # ib_async: reqExecutionsAsync with no filter returns all recent fills
        # (IBKR caps at ~7 days). For wider history we'd need /reqAllOpenOrders.
        fills = await ib.reqExecutionsAsync()
    except Exception as e:
        return {"mode": "live", "error": f"reqExecutionsAsync failed: {e}"}

    # Write-through persist to DB (idempotent by exec_id). Any caller hitting
    # this endpoint causes the persistence layer to update — no separate cron
    # needed for normal operation. Survives IBKR's 7-day cache eviction.
    persisted_new = 0
    try:
        persisted_new = await _persist_ibkr_executions(fills)
    except Exception:
        logger.exception("write-through persist of IBKR executions failed")

    cutoff = _dt.now(_tz.utc) - _td(days=days)
    out_exec: list[dict[str, Any]] = []
    by_contract: dict[str, list[dict]] = {}
    for f in fills:
        ex = f.execution
        c = f.contract
        if getattr(c, "secType", "") != "OPT":
            continue
        # ex.time is a datetime, tz-aware
        ts = ex.time if hasattr(ex, "time") and ex.time else None
        if ts is None: continue
        if ts.tzinfo is None: ts = ts.replace(tzinfo=_tz.utc)
        if ts < cutoff: continue

        contract_key = f"{c.symbol} {c.strike}{c.right} {c.lastTradeDateOrContractMonth}"
        # Pair the CommissionReport with the Fill (ib_async links them by execId).
        _cr = getattr(f, "commissionReport", None)
        comm = None
        rpnl = None
        if _cr is not None:
            _v = getattr(_cr, "commission", None)
            if _v is not None and not (isinstance(_v, float) and _v != _v):
                try: comm = float(_v)
                except (TypeError, ValueError): comm = None
            _r = getattr(_cr, "realizedPNL", None)
            if _r is not None and not (isinstance(_r, float) and _r != _r):
                try: rpnl = float(_r)
                except (TypeError, ValueError): rpnl = None
        rec = {
            "exec_id": ex.execId,
            "timestamp": ts.isoformat(),
            "contract": contract_key,
            "side": ex.side,  # 'BOT' or 'SLD'
            "shares": float(ex.shares),
            "price": float(ex.price),
            "ib_order_id": ex.orderId,
            "perm_id": ex.permId,
            "cum_qty": float(ex.cumQty),
            "avg_price": float(ex.avgPrice),
            "commission": comm,
            "realized_pnl": rpnl,
        }
        out_exec.append(rec)
        by_contract.setdefault(contract_key, []).append(rec)

    # Compute realized P/L per contract from the fills directly
    pnl_by_contract: dict[str, dict[str, Any]] = {}
    for k, fills_list in by_contract.items():
        bought = sum(f["shares"] * f["price"] for f in fills_list if f["side"] == "BOT")
        sold = sum(f["shares"] * f["price"] for f in fills_list if f["side"] == "SLD")
        bot_qty = sum(f["shares"] for f in fills_list if f["side"] == "BOT")
        sld_qty = sum(f["shares"] for f in fills_list if f["side"] == "SLD")
        # Realized P/L (only for fully closed positions; partial closes ratable)
        realized = (sold - bought) * 100 if bot_qty == sld_qty else None
        pnl_by_contract[k] = {
            "bot_qty": bot_qty,
            "sld_qty": sld_qty,
            "avg_bot_price": round(bought / bot_qty, 4) if bot_qty else None,
            "avg_sld_price": round(sold / sld_qty, 4) if sld_qty else None,
            "realized_pnl_dollars": round(realized, 2) if realized is not None else None,
            "fully_closed": bot_qty == sld_qty,
            "fills_count": len(fills_list),
        }

    # Session totals (today UTC for clean math; could refine to ET)
    total_realized = sum(p["realized_pnl_dollars"] or 0 for p in pnl_by_contract.values())

    return {
        "mode": "live",
        "days": days,
        "total_executions": len(out_exec),
        "contracts_touched": len(by_contract),
        "total_realized_pnl_dollars": round(total_realized, 2),
        "pnl_by_contract": pnl_by_contract,
        "executions": out_exec,
        "persisted_new_rows": persisted_new,
    }


async def _persist_ibkr_executions(fills) -> int:
    """Upsert IBKR fills into ibkr_execution table. Returns count of NEW
    rows added (existing exec_ids are skipped). Called write-through from
    /diag/ibkr_executions and the historical-query endpoint.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import IBKRExecution
    from sqlmodel import select as _s
    from datetime import timezone as _tz, timedelta as _td

    et_tz = _tz(_td(hours=-4))
    new_count = 0
    async with _gs() as session:
        existing = set((await session.exec(_s(IBKRExecution.exec_id))).all())
        for f in fills:
            ex = f.execution
            c = f.contract
            if getattr(c, "secType", "") != "OPT":
                continue
            if not ex.execId or ex.execId in existing:
                continue
            ts = getattr(ex, "time", None)
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_tz.utc)
            sess_date = ts.astimezone(et_tz).strftime("%Y-%m-%d")
            # CommissionReport is paired with each Fill by ib_async — extract
            # both commission and realizedPNL when present. Paper accounts
            # typically report 0.0 commission; live accounts report real fees.
            comm_report = getattr(f, "commissionReport", None)
            commission_val: float | None = None
            realized_pnl_val: float | None = None
            if comm_report is not None:
                _c = getattr(comm_report, "commission", None)
                if _c is not None and not (isinstance(_c, float) and _c != _c):  # not NaN
                    try:
                        commission_val = float(_c)
                    except (TypeError, ValueError):
                        commission_val = None
                _rp = getattr(comm_report, "realizedPNL", None)
                if _rp is not None and not (isinstance(_rp, float) and _rp != _rp):
                    try:
                        realized_pnl_val = float(_rp)
                    except (TypeError, ValueError):
                        realized_pnl_val = None

            row = IBKRExecution(
                exec_id=ex.execId,
                timestamp=ts,
                session_date=sess_date,
                symbol=getattr(c, "symbol", ""),
                strike=float(getattr(c, "strike", 0) or 0),
                right=getattr(c, "right", ""),
                expiry=getattr(c, "lastTradeDateOrContractMonth", ""),
                side=ex.side,
                shares=float(ex.shares),
                price=float(ex.price),
                cum_qty=float(getattr(ex, "cumQty", 0) or 0),
                avg_price=float(getattr(ex, "avgPrice", 0) or 0),
                ib_order_id=getattr(ex, "orderId", None),
                perm_id=getattr(ex, "permId", None),
                account=getattr(ex, "acctNumber", None),
                commission=commission_val,
                realized_pnl=realized_pnl_val,
            )
            session.add(row)
            new_count += 1
        if new_count:
            await session.commit()
    return new_count


@router.get("/diag/ibkr_executions_persisted")
async def diag_ibkr_executions_persisted(
    request: Request,
    session_date: str | None = None,
    days_back: int = 30,
) -> dict[str, Any]:
    """Query persisted IBKR executions from our DB. Survives IBKR's 7-day
    cache eviction. Use session_date=YYYY-MM-DD for a single day, or
    days_back=N for a rolling window (default 30).

    The DB is populated write-through whenever /diag/ibkr_executions is
    called (which the audit endpoint does each session), so the persisted
    record grows over time without an explicit cron.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import IBKRExecution
    from sqlmodel import select as _s
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    async with _gs() as session:
        stmt = _s(IBKRExecution)
        if session_date:
            stmt = stmt.where(IBKRExecution.session_date == session_date)
        else:
            cutoff = (_dt.now(_tz.utc) - _td(days=days_back)).strftime("%Y-%m-%d")
            stmt = stmt.where(IBKRExecution.session_date >= cutoff)
        stmt = stmt.order_by(IBKRExecution.timestamp)  # type: ignore[attr-defined]
        rows = list((await session.exec(stmt)).all())

    by_date: dict[str, list[dict]] = {}
    by_contract: dict[str, list[dict]] = {}
    for r in rows:
        contract_key = f"{r.symbol} {r.strike}{r.right} {r.expiry}"
        rec = {
            "exec_id": r.exec_id,
            "timestamp": r.timestamp.isoformat(),
            "session_date": r.session_date,
            "contract": contract_key,
            "side": r.side,
            "shares": r.shares,
            "price": r.price,
            "ib_order_id": r.ib_order_id,
            "perm_id": r.perm_id,
            "account": r.account,
        }
        by_date.setdefault(r.session_date, []).append(rec)
        by_contract.setdefault(contract_key, []).append(rec)

    # Aggregate P/L per session-date and per-contract
    pnl_by_date = {}
    for date, fills in by_date.items():
        bot = sum(f["shares"] * f["price"] for f in fills if f["side"] == "BOT")
        sld = sum(f["shares"] * f["price"] for f in fills if f["side"] == "SLD")
        bot_q = sum(f["shares"] for f in fills if f["side"] == "BOT")
        sld_q = sum(f["shares"] for f in fills if f["side"] == "SLD")
        pnl_by_date[date] = {
            "fills": len(fills),
            "bot_qty": bot_q,
            "sld_qty": sld_q,
            "avg_bot_price": round(bot / bot_q, 4) if bot_q else None,
            "avg_sld_price": round(sld / sld_q, 4) if sld_q else None,
            "realized_pnl_dollars": round((sld - bot) * 100, 2) if bot_q == sld_q and bot_q > 0 else None,
            "fully_closed": bot_q == sld_q and bot_q > 0,
        }

    pnl_by_contract = {}
    for ck, fills in by_contract.items():
        bot = sum(f["shares"] * f["price"] for f in fills if f["side"] == "BOT")
        sld = sum(f["shares"] * f["price"] for f in fills if f["side"] == "SLD")
        bot_q = sum(f["shares"] for f in fills if f["side"] == "BOT")
        sld_q = sum(f["shares"] for f in fills if f["side"] == "SLD")
        pnl_by_contract[ck] = {
            "bot_qty": bot_q, "sld_qty": sld_q,
            "avg_bot_price": round(bot / bot_q, 4) if bot_q else None,
            "avg_sld_price": round(sld / sld_q, 4) if sld_q else None,
            "realized_pnl_dollars": round((sld - bot) * 100, 2) if bot_q == sld_q and bot_q > 0 else None,
            "fully_closed": bot_q == sld_q and bot_q > 0,
        }

    return {
        "session_date": session_date,
        "days_back": days_back if not session_date else None,
        "total_executions": len(rows),
        "dates_covered": sorted(by_date.keys()),
        "contracts_touched": len(by_contract),
        "pnl_by_date": pnl_by_date,
        "pnl_by_contract": pnl_by_contract,
    }


@router.get("/diag/trade_reconcile")
async def diag_trade_reconcile(request: Request) -> dict[str, Any]:
    """Cross-reference our LiveOrder records with IBKR's authoritative
    execution log. Surfaces:
      - DB parents that have IBKR fills (matched)
      - DB parents that have NO IBKR fills (silent rejection / phantom)
      - IBKR fills that have NO matching DB parent (manual orders / DB wipe)
      - Real per-trade P/L from actual fill prices (vs our +1.1R/-1R approx)
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import LiveOrder
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from sqlmodel import select as _s
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    s = _state(request)
    if not isinstance(s.ibkr, LiveIBKRProvider):
        return {"mode": "mock"}
    ib = getattr(s.ibkr, "_ib", None)
    if ib is None or not ib.isConnected():
        return {"mode": "disconnected"}

    today_et = _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")

    async with _gs() as session:
        db_parents = list((await session.exec(
            _s(LiveOrder).where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.dry_run == False)  # noqa: E712
        )).all())
    db_parents_today = [p for p in db_parents
                        if p.submitted_at.strftime("%Y-%m-%d") == today_et]

    try:
        fills = await ib.reqExecutionsAsync()
    except Exception as e:
        return {"mode": "live", "error": f"reqExecutionsAsync failed: {e}"}

    # Index IBKR fills by (orderId, permId) and by contract
    ibkr_by_order_id: dict[int, list] = {}
    ibkr_by_perm_id: dict[int, list] = {}
    for f in fills:
        ex = f.execution
        oid = getattr(ex, "orderId", None)
        pid = getattr(ex, "permId", None)
        if oid is not None:
            ibkr_by_order_id.setdefault(oid, []).append(f)
        if pid is not None:
            ibkr_by_perm_id.setdefault(pid, []).append(f)

    matched: list[dict] = []
    db_without_ibkr: list[dict] = []
    for p in db_parents_today:
        ibkr_fills = []
        if p.ib_order_id and p.ib_order_id in ibkr_by_order_id:
            ibkr_fills = ibkr_by_order_id[p.ib_order_id]
        elif p.ib_perm_id and p.ib_perm_id in ibkr_by_perm_id:
            ibkr_fills = ibkr_by_perm_id[p.ib_perm_id]
        if not ibkr_fills:
            db_without_ibkr.append({
                "parent_id": p.id, "contract": f"{p.symbol} {p.strike}{p.right}",
                "qty": p.qty, "status": p.status,
                "ib_order_id": p.ib_order_id,
                "submitted_at": p.submitted_at.isoformat(),
            })
            continue
        fill_prices = [float(f.execution.price) for f in ibkr_fills]
        matched.append({
            "parent_id": p.id, "contract": f"{p.symbol} {p.strike}{p.right}",
            "db_qty": p.qty, "db_status": p.status,
            "db_limit_price": p.limit_price,
            "ibkr_fills_count": len(ibkr_fills),
            "ibkr_avg_fill_price": round(sum(fill_prices) / len(fill_prices), 4),
            "ibkr_first_fill_price": fill_prices[0],
        })

    # Find IBKR fills with no matching DB parent (only BOT side — SLD would be
    # exits managed by our watcher, which is expected and not orphan)
    db_known_order_ids = {p.ib_order_id for p in db_parents if p.ib_order_id}
    db_known_perm_ids = {p.ib_perm_id for p in db_parents if p.ib_perm_id}
    ibkr_without_db: list[dict] = []
    for f in fills:
        ex = f.execution
        c = f.contract
        if getattr(c, "secType", "") != "OPT": continue
        if ex.side != "BOT": continue  # only flag entries
        if ex.orderId in db_known_order_ids: continue
        if ex.permId in db_known_perm_ids: continue
        ts = ex.time if ex.time and ex.time.tzinfo else (
            ex.time.replace(tzinfo=_tz.utc) if ex.time else None)
        ibkr_without_db.append({
            "exec_id": ex.execId,
            "timestamp": ts.isoformat() if ts else None,
            "contract": f"{c.symbol} {c.strike}{c.right}",
            "side": ex.side,
            "qty": float(ex.shares),
            "price": float(ex.price),
            "ib_order_id": ex.orderId,
        })

    return {
        "mode": "live",
        "session_date_et": today_et,
        "db_parents_today": len(db_parents_today),
        "matched": len(matched),
        "db_without_ibkr_fill": len(db_without_ibkr),
        "ibkr_fill_without_db_parent": len(ibkr_without_db),
        "matched_detail": matched,
        "db_without_ibkr_detail": db_without_ibkr,
        "ibkr_without_db_detail": ibkr_without_db,
    }


@router.get("/diag/signal_execution_audit")
async def diag_signal_execution_audit(
    request: Request, session_date: str | None = None
) -> dict[str, Any]:
    """Per-signal execution audit. For each LiveOrder parent placed today:
      - The signal/verdict it was placed on (entry_limit, target SPX levels)
      - The actual IBKR BUY fill — entry slippage vs limit
      - The actual IBKR SELL fills on the same contract — exits + prices
      - Realized P/L per contract (aggregates handles stacking)
      - SPX overshoot at T1 (target vs actual SPX where T1 fired)

    Entry matching is exact (LiveOrder.ib_order_id ↔ IBKR fill.orderId).
    Exit fills are watcher-issued market orders that don't get LiveOrder
    rows (only audit log) — so they're matched per-contract, not per-parent.
    For stacked positions on the same strike, the per-parent entry slippage
    is still attributable, but realized P/L is per-contract aggregate only.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import LiveOrder
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from sqlmodel import select as _s
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    s = _state(request)
    if not isinstance(s.ibkr, LiveIBKRProvider):
        return {"mode": "mock"}
    ib = getattr(s.ibkr, "_ib", None)
    if ib is None or not ib.isConnected():
        return {"mode": "disconnected"}

    target_date = session_date or _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")

    async with _gs() as session:
        db_parents = list((await session.exec(
            _s(LiveOrder).where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.dry_run == False)  # noqa: E712
        )).all())
    parents = [p for p in db_parents
               if p.submitted_at.strftime("%Y-%m-%d") == target_date]

    try:
        fills = await ib.reqExecutionsAsync()
    except Exception as e:
        return {"mode": "live", "error": f"reqExecutionsAsync failed: {e}"}

    # Index fills both ways
    by_order_id: dict[int, list[dict]] = {}
    by_contract: dict[str, list[dict]] = {}
    for f in fills:
        ex = f.execution
        c = f.contract
        if getattr(c, "secType", "") != "OPT":
            continue
        ts = getattr(ex, "time", None)
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
        rec = {
            "exec_id": ex.execId,
            "timestamp": ts.isoformat(),
            "_ts": ts,
            "contract_key": f"{c.symbol} {c.strike}{c.right} {c.lastTradeDateOrContractMonth}",
            "side": ex.side,
            "shares": float(ex.shares),
            "price": float(ex.price),
            "ib_order_id": ex.orderId,
        }
        by_order_id.setdefault(ex.orderId, []).append(rec)
        by_contract.setdefault(rec["contract_key"], []).append(rec)
    for fl in by_contract.values():
        fl.sort(key=lambda r: r["_ts"])

    # Per-parent audit (entry-level)
    audit_rows: list[dict] = []
    for p in parents:
        contract_key = f"{p.symbol} {p.strike}{p.right} {p.expiry}"

        entry_fills = by_order_id.get(p.ib_order_id or -1, [])
        bot_fills = [f for f in entry_fills if f["side"] == "BOT"]
        bot_qty = sum(f["shares"] for f in bot_fills)
        avg_entry = (sum(f["shares"] * f["price"] for f in bot_fills) / bot_qty
                     if bot_qty else None)

        entry_slip_d = None
        entry_slip_pct = None
        if avg_entry is not None and p.limit_price:
            entry_slip_d = round(avg_entry - p.limit_price, 4)
            entry_slip_pct = round(entry_slip_d / p.limit_price * 100, 2)

        t1_overshoot = None
        if p.spx_target_t1 is not None and p.spx_target_t1_at_spx is not None:
            if p.right == "C":
                t1_overshoot = round(p.spx_target_t1_at_spx - p.spx_target_t1, 2)
            else:
                t1_overshoot = round(p.spx_target_t1 - p.spx_target_t1_at_spx, 2)

        audit_rows.append({
            "parent_id": p.id,
            "submitted_at": p.submitted_at.isoformat(),
            "contract": contract_key,
            "right": p.right,
            "qty_ordered": p.qty,
            "qty_filled": int(bot_qty),
            "status": p.status,
            "ib_order_id": p.ib_order_id,
            "signal": {
                "entry_limit": p.limit_price,
                "entry_spx": p.entry_spx_price,
                "t1_spx": p.spx_target_t1,
                "t2_spx": p.spx_target_t2,
                "t3_spx": p.spx_target_t3,
                "stop_spx": p.spx_target_stop,
                "dynamic_stop_spx": p.dynamic_stop_level,
                "be_step_reached_R": p.be_ladder_max_step_R,
            },
            "actual": {
                "avg_buy_price": round(avg_entry, 4) if avg_entry else None,
                "entry_slip_dollars": entry_slip_d,
                "entry_slip_pct": entry_slip_pct,
                "buy_fills_count": len(bot_fills),
            },
            "tiers": {
                "t1_fired": p.spx_target_t1_fired,
                "t2_fired": p.spx_target_t2_fired,
                "t3_fired": p.spx_target_t3_fired,
                "t1_actual_spx": p.spx_target_t1_at_spx,
                "t2_actual_spx": p.spx_target_t2_at_spx,
                "t3_actual_spx": p.spx_target_t3_at_spx,
                "t1_actually_hit": (
                    (p.spx_target_t1_at_spx or 0) > 0
                    or (p.status == "CLOSED" and p.spx_target_t1_fired)
                ),
                "t2_actually_hit": (
                    (p.spx_target_t2_at_spx or 0) > 0
                    or (p.status == "CLOSED" and p.spx_target_t2_fired)
                ),
                "t3_actually_hit": (
                    (p.spx_target_t3_at_spx or 0) > 0
                    or (p.status == "CLOSED" and p.spx_target_t3_fired)
                ),
                "t1_overshoot_spx": t1_overshoot,
            },
        })

    # Per-contract aggregation (handles stacking)
    contract_summary: dict[str, dict] = {}
    for ck, fl in by_contract.items():
        bots = [f for f in fl if f["side"] == "BOT"]
        slds = [f for f in fl if f["side"] == "SLD"]
        bot_q = sum(f["shares"] for f in bots)
        sld_q = sum(f["shares"] for f in slds)
        bot_t = sum(f["shares"] * f["price"] for f in bots)
        sld_t = sum(f["shares"] * f["price"] for f in slds)
        realized = (sld_t - bot_t) * 100 if bot_q == sld_q and bot_q > 0 else None
        contract_summary[ck] = {
            "buy_count": len(bots),
            "sell_count": len(slds),
            "bot_qty": bot_q,
            "sld_qty": sld_q,
            "avg_buy_price": round(bot_t / bot_q, 4) if bot_q else None,
            "avg_sell_price": round(sld_t / sld_q, 4) if sld_q else None,
            "best_sell_price": round(max(f["price"] for f in slds), 4) if slds else None,
            "worst_sell_price": round(min(f["price"] for f in slds), 4) if slds else None,
            "fully_closed": bot_q == sld_q and bot_q > 0,
            "realized_pnl_dollars": round(realized, 2) if realized is not None else None,
        }

    # Aggregate stats
    slips_d = [r["actual"]["entry_slip_dollars"] for r in audit_rows
               if r["actual"]["entry_slip_dollars"] is not None]
    slips_pct = [r["actual"]["entry_slip_pct"] for r in audit_rows
                 if r["actual"]["entry_slip_pct"] is not None]
    overshoots = [r["tiers"]["t1_overshoot_spx"] for r in audit_rows
                  if r["tiers"]["t1_overshoot_spx"] is not None]
    # Truth signal: spx_target_tN_at_spx > 0 means the tier ACTUALLY fired
    # (watcher writes the SPX value at fire time). The boolean t1/2/3_fired
    # flags get set to True by the STOP path too, so they're not reliable.
    #
    # Fallback for rows written before t2/t3_at_spx column existed:
    # status='CLOSED' means all 3 tiers truly fired (the watcher's only path
    # to CLOSED requires all 3 booleans True AND status was not already STOPPED,
    # so STOPPED rows stay STOPPED). status='STOPPED' is the ambiguous case —
    # we use t1_at_spx for T1 truth, and treat T2/T3 as "unknown" without
    # the dedicated fire-time SPX column.
    def _hit(r, tier: int) -> bool:
        actual = r["tiers"].get(f"t{tier}_actual_spx")
        if actual and actual > 0:
            return True
        # Pre-migration fallback: CLOSED status implies all tiers fired
        return r["status"] == "CLOSED" and r["tiers"].get(f"t{tier}_fired", False)
    t1_fires = sum(1 for r in audit_rows if _hit(r, 1))
    t2_fires = sum(1 for r in audit_rows if _hit(r, 2))
    t3_fires = sum(1 for r in audit_rows if _hit(r, 3))
    stops = sum(1 for r in audit_rows if r["status"] == "STOPPED")

    aggregate = {
        "parent_count": len(audit_rows),
        "entry_slip_avg_dollars": round(sum(slips_d) / len(slips_d), 4) if slips_d else None,
        "entry_slip_avg_pct": round(sum(slips_pct) / len(slips_pct), 2) if slips_pct else None,
        "entry_slip_max_dollars": max(slips_d, default=None),
        "entry_slip_min_dollars": min(slips_d, default=None),
        "t1_overshoot_avg_spx": round(sum(overshoots) / len(overshoots), 2) if overshoots else None,
        "t1_overshoot_max_spx": max(overshoots, default=None),
        "t1_overshoot_min_spx": min(overshoots, default=None),
        "t1_actually_hit_count": t1_fires,
        "t2_actually_hit_count": t2_fires,
        "t3_actually_hit_count": t3_fires,
        "stopped_count": stops,
        "total_realized_dollars": round(sum(
            c["realized_pnl_dollars"] or 0 for c in contract_summary.values()
        ), 2),
    }

    return {
        "mode": "live",
        "session_date": target_date,
        "aggregate": aggregate,
        "parents": audit_rows,
        "contract_summary": contract_summary,
    }


@router.get("/diag/portfolio_greeks")
async def diag_portfolio_greeks(request: Request) -> dict[str, Any]:
    """Sum greeks across ALL open IBKR option positions. Surfaces net
    portfolio exposure so you can see what you owe the market right now.

    Returns:
      - net_delta_dollars: position-weighted delta × 100 × spot (dollar exposure)
      - net_gamma: position-weighted gamma × 100
      - net_vega_dollars: position-weighted vega × 100 (dollar P/L per 1pt IV)
      - net_theta_dollars: position-weighted theta × 100 (dollar P/L per day)
      - per_position: per-contract breakdown

    Greeks come from IBKR's modelGreeks (computed from their vol surface).
    If a position has no model greeks (rare on liquid options), it's flagged
    in `missing_greeks` and excluded from the totals.
    """
    from fef_sidecar.ibkr.live import LiveIBKRProvider

    s = _state(request)
    if not isinstance(s.ibkr, LiveIBKRProvider):
        return {"mode": "mock", "positions": [], "net": {}}
    ib = getattr(s.ibkr, "_ib", None)
    if ib is None or not ib.isConnected():
        return {"mode": "disconnected"}

    positions = ib.positions()
    per_position: list[dict[str, Any]] = []
    missing_greeks: list[str] = []
    net_delta = 0.0
    net_gamma = 0.0
    net_vega = 0.0
    net_theta = 0.0

    for pos in positions:
        c = pos.contract
        if getattr(c, "secType", "") != "OPT":
            continue
        qty = float(pos.position)
        if abs(qty) < 0.001:
            continue
        # Get current quote with greeks. Request and await tick.
        from ib_async import Ticker
        try:
            await ib.qualifyContractsAsync(c)
            ticker: Ticker = ib.reqMktData(c, "", False, False)
            # Brief wait for greeks to arrive (model greeks have ~250ms latency)
            import asyncio as _aio
            await _aio.sleep(0.6)
            mg = ticker.modelGreeks
        except Exception as e:
            logger.warning("greeks fetch failed for %s: %s", c.localSymbol, e)
            mg = None
        finally:
            try:
                ib.cancelMktData(c)
            except Exception:
                pass

        contract_label = f"{c.symbol} {c.strike}{c.right} {c.lastTradeDateOrContractMonth}"
        if mg is None or mg.delta is None:
            missing_greeks.append(contract_label)
            per_position.append({
                "contract": contract_label, "qty": qty,
                "delta": None, "gamma": None, "vega": None, "theta": None,
                "model_greeks_available": False,
            })
            continue

        # Multiply by 100 (options multiplier) and by qty
        pos_delta = (mg.delta or 0) * qty * 100
        pos_gamma = (mg.gamma or 0) * qty * 100
        pos_vega = (mg.vega or 0) * qty * 100
        pos_theta = (mg.theta or 0) * qty * 100
        net_delta += pos_delta
        net_gamma += pos_gamma
        net_vega += pos_vega
        net_theta += pos_theta

        per_position.append({
            "contract": contract_label, "qty": qty,
            "delta": round(mg.delta or 0, 4),
            "gamma": round(mg.gamma or 0, 4),
            "vega": round(mg.vega or 0, 4),
            "theta": round(mg.theta or 0, 4),
            "iv": round(mg.impliedVol or 0, 4) if mg.impliedVol else None,
            "underlying_price": round(mg.undPrice or 0, 2) if mg.undPrice else None,
            "position_delta": round(pos_delta, 2),
            "position_gamma": round(pos_gamma, 2),
            "position_vega": round(pos_vega, 2),
            "position_theta": round(pos_theta, 2),
        })

    # Estimate dollar exposures using an SPX-like spot. Use the first
    # position's underlying as a rough proxy (or 0 if none).
    spot_proxy = next((p.get("underlying_price") for p in per_position
                       if p.get("underlying_price")), 0)
    net_delta_dollars = round(net_delta * spot_proxy, 2) if spot_proxy else None

    # Thresholds for warnings — purely informational
    warnings: list[str] = []
    if net_delta_dollars and abs(net_delta_dollars) > 100000:
        warnings.append(f"|net_delta_dollars| = ${abs(net_delta_dollars):,.0f} > $100k threshold")
    if net_theta and net_theta < -5000:
        warnings.append(f"net_theta = ${net_theta:,.0f}/day — large theta bleed")

    return {
        "mode": "live",
        "position_count": len(per_position),
        "missing_greeks_for": missing_greeks,
        "net": {
            "delta": round(net_delta, 2),
            "delta_dollars": net_delta_dollars,
            "gamma": round(net_gamma, 2),
            "vega": round(net_vega, 2),
            "theta": round(net_theta, 2),
            "spot_proxy_used": spot_proxy,
        },
        "warnings": warnings,
        "per_position": per_position,
    }


@router.get("/diag/symbol_correlations")
async def diag_symbol_correlations(
    request: Request, lookback_hours: int = 24,
) -> dict[str, Any]:
    """Pairwise Pearson correlation between symbols on price-action sequences.

    For each enabled scanner symbol, pulls recent SignalIter rows and
    computes correlation on the spx_price series (which is the symbol's
    own price when SignalIter was written under that symbol's scan).
    Use BEFORE enabling multiple uncorrelated symbols — if they're
    actually 0.9 correlated intraday, you're not getting diversification.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import SignalIter
    from sqlmodel import select as _s
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import statistics as _stat

    s = _state(request)
    enabled_syms = [sym for sym, on in s.scan_enabled_symbols.items() if on]
    enabled_syms.append(s.active_symbol)
    syms = sorted(set(enabled_syms))

    cutoff = _dt.now(_tz.utc) - _td(hours=lookback_hours)

    series: dict[str, list[tuple[_dt, float]]] = {}
    async with _gs() as session:
        for sym in syms:
            rows = list((await session.exec(
                _s(SignalIter).where(SignalIter.timestamp >= cutoff)  # type: ignore[arg-type]
                .order_by(SignalIter.timestamp)  # type: ignore[attr-defined]
            )).all())
            # Filter to rows where extras.scanner_symbol matches OR active_symbol
            filtered: list[tuple[_dt, float]] = []
            for r in rows:
                ex = r.extras or {}
                row_sym = ex.get("scanner_symbol") or s.active_symbol
                if row_sym == sym:
                    filtered.append((r.timestamp, r.spx_price))
            series[sym] = filtered

    # Build a unified time-aligned matrix. Resample each series to the
    # MIN timestamp set so correlations are computed on synchronous samples.
    # Crude approach: just use the timestamps of the smallest series and
    # nearest-neighbor lookup in the others. Good enough for intraday.
    series_lens = {sym: len(s_) for sym, s_ in series.items()}
    base_sym = min(syms, key=lambda x: series_lens[x]) if syms else None
    sample_count = 0
    matrix: dict[str, dict[str, float | None]] = {a: {b: None for b in syms} for a in syms}

    if base_sym and series.get(base_sym):
        base_times = [t for t, _ in series[base_sym]]
        sample_count = len(base_times)
        # Align other series by nearest timestamp
        aligned: dict[str, list[float]] = {}
        for sym in syms:
            if not series[sym]:
                continue
            sym_pairs = series[sym]
            j = 0
            out: list[float] = []
            for bt in base_times:
                while j + 1 < len(sym_pairs) and sym_pairs[j + 1][0] <= bt:
                    j += 1
                out.append(sym_pairs[j][1])
            aligned[sym] = out

        for a in syms:
            for b in syms:
                if a == b:
                    matrix[a][b] = 1.0
                    continue
                xs = aligned.get(a); ys = aligned.get(b)
                if not xs or not ys or len(xs) < 3 or len(ys) < 3:
                    continue
                # Pearson on returns (delta of price) rather than levels
                rx = [xs[i] - xs[i-1] for i in range(1, len(xs))]
                ry = [ys[i] - ys[i-1] for i in range(1, len(ys))]
                if len(rx) < 2:
                    continue
                try:
                    mx = _stat.mean(rx); my = _stat.mean(ry)
                    sx = _stat.stdev(rx); sy = _stat.stdev(ry)
                    if sx == 0 or sy == 0:
                        continue
                    cov = sum((rx[i]-mx)*(ry[i]-my) for i in range(len(rx))) / (len(rx)-1)
                    matrix[a][b] = round(cov / (sx * sy), 3)
                except Exception:
                    matrix[a][b] = None

    return {
        "lookback_hours": lookback_hours,
        "symbols": syms,
        "sample_count": sample_count,
        "series_lengths": series_lens,
        "correlation_matrix": matrix,
    }


@router.post("/backtest/save_tournament")
async def backtest_save_tournament(
    request: Request, body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Persist a Preset Tournament run + its per-preset results.

    Called by the panel after a successful tournament run so the leaderboard
    survives refresh. Lets you track preset performance day-over-day instead
    of every Run being ephemeral.

    Body:
      session_date_from: str
      session_date_to: str
      apply_be_ladder: bool
      commission_per_contract: float (optional)
      notes: str (optional)
      results: list of per-preset rows with all the metrics
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import TournamentRun, TournamentRunResult

    # Compute regime distribution for the window — count regime_at_fire
    # across SignalIter rows in the range, normalize to fractions.
    from fef_sidecar.db.models import SignalIter as _SI
    from sqlmodel import select as _s
    from collections import Counter as _Counter
    regime_dist: dict[str, float] = {}
    dominant_regime = "UNKNOWN"
    async with _gs() as session:
        date_from = body.get("session_date_from", "")
        date_to = body.get("session_date_to", date_from)
        iter_rows = list((await session.exec(
            _s(_SI)
            .where(_SI.session_date >= date_from)
            .where(_SI.session_date <= date_to)
        )).all())
        regime_counts: _Counter = _Counter()
        for it in iter_rows:
            ex = it.extras or {}
            regime = ex.get("regime_at_fire") or "NORMAL"
            regime_counts[regime] += 1
        total = sum(regime_counts.values())
        if total > 0:
            regime_dist = {k: round(v / total, 3) for k, v in regime_counts.items()}
            dominant_regime = max(regime_counts.items(), key=lambda kv: kv[1])[0]

    run = TournamentRun(
        session_date_from=body.get("session_date_from", ""),
        session_date_to=body.get("session_date_to", ""),
        apply_be_ladder=bool(body.get("apply_be_ladder", True)),
        commission_per_contract=float(body.get("commission_per_contract", 0.65)),
        preset_count=len(body.get("results") or []),
        notes=str(body.get("notes", "")),
        regime_distribution=regime_dist,
        dominant_regime=dominant_regime,
    )
    async with _gs() as session:
        session.add(run)
        await session.commit()
        await session.refresh(run)
        for i, r in enumerate(body.get("results") or [], start=1):
            session.add(TournamentRunResult(
                tournament_run_id=run.id,
                preset_id=int(r.get("preset_id", 0)),
                preset_name=str(r.get("preset_name", "")),
                rank=i if (r.get("trade_count") or 0) > 0 and not r.get("error") else 0,
                trade_count=int(r.get("trade_count") or 0),
                iters_scanned=int(r.get("iters_scanned") or 0),
                wins=int(r.get("wins") or 0),
                losses=int(r.get("losses") or 0),
                win_rate_pct=float(r.get("win_rate_pct") or 0),
                expectancy_r=float(r.get("expectancy_R") or 0),
                sharpe_ratio=r.get("sharpe_ratio"),
                max_drawdown_r=float(r.get("max_drawdown_R") or 0),
                profit_factor=r.get("profit_factor"),
                cumulative_r=float(r.get("cumulative_R") or 0),
                skipped=r.get("skipped") or {},
                error=r.get("error"),
            ))
        await session.commit()
    return {"ok": True, "tournament_run_id": run.id, "saved_results": run.preset_count}


@router.get("/backtest/tournament_runs")
async def backtest_tournament_runs(
    request: Request, limit: int = 30,
) -> list[dict[str, Any]]:
    """List recent tournament runs (newest first). Each entry includes
    the run metadata + a summary of the winning preset for that run.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import TournamentRun, TournamentRunResult
    from sqlmodel import select as _s

    async with _gs() as session:
        runs = list((await session.exec(
            _s(TournamentRun)
            .order_by(TournamentRun.run_at.desc())  # type: ignore[attr-defined]
            .limit(limit)
        )).all())
        out = []
        for run in runs:
            winner_rows = list((await session.exec(
                _s(TournamentRunResult)
                .where(TournamentRunResult.tournament_run_id == run.id)
                .where(TournamentRunResult.rank == 1)
            )).all())
            winner = winner_rows[0] if winner_rows else None
            out.append({
                "id": run.id,
                "run_at": run.run_at.isoformat(),
                "session_date_from": run.session_date_from,
                "session_date_to": run.session_date_to,
                "apply_be_ladder": run.apply_be_ladder,
                "preset_count": run.preset_count,
                "notes": run.notes,
                "winner_preset_name": winner.preset_name if winner else None,
                "winner_cumulative_r": winner.cumulative_r if winner else None,
                "winner_expectancy_r": winner.expectancy_r if winner else None,
                "winner_sharpe": winner.sharpe_ratio if winner else None,
                "regime_distribution": getattr(run, "regime_distribution", {}) or {},
                "dominant_regime": getattr(run, "dominant_regime", "UNKNOWN"),
            })
        return out


@router.get("/backtest/tournament_runs/{run_id}")
async def backtest_tournament_run_detail(
    request: Request, run_id: int,
) -> dict[str, Any]:
    """Full leaderboard for one historical tournament run."""
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import TournamentRun, TournamentRunResult
    from sqlmodel import select as _s

    async with _gs() as session:
        run = (await session.exec(
            _s(TournamentRun).where(TournamentRun.id == run_id)
        )).first()
        if run is None:
            return {"error": f"tournament_run {run_id} not found"}
        rows = list((await session.exec(
            _s(TournamentRunResult)
            .where(TournamentRunResult.tournament_run_id == run_id)
            .order_by(TournamentRunResult.rank)  # type: ignore[attr-defined]
        )).all())
        return {
            "id": run.id,
            "run_at": run.run_at.isoformat(),
            "session_date_from": run.session_date_from,
            "session_date_to": run.session_date_to,
            "apply_be_ladder": run.apply_be_ladder,
            "commission_per_contract": run.commission_per_contract,
            "preset_count": run.preset_count,
            "notes": run.notes,
            "results": [
                {
                    "preset_id": r.preset_id,
                    "preset_name": r.preset_name,
                    "rank": r.rank,
                    "trade_count": r.trade_count,
                    "iters_scanned": r.iters_scanned,
                    "wins": r.wins,
                    "losses": r.losses,
                    "win_rate_pct": r.win_rate_pct,
                    "expectancy_R": r.expectancy_r,
                    "sharpe_ratio": r.sharpe_ratio,
                    "max_drawdown_R": r.max_drawdown_r,
                    "profit_factor": r.profit_factor,
                    "cumulative_R": r.cumulative_r,
                    "skipped": r.skipped,
                    "error": r.error,
                } for r in rows
            ],
        }


@router.post("/diag/refresh_prior_close")
async def diag_refresh_prior_close(request: Request) -> dict[str, Any]:
    """Re-fetch prior-day close for the active symbol from IBKR. Used when
    the initial fetch failed (e.g. TWS was down at startup) so the Live
    Quote panel can show the day-change % once TWS is back up.
    """
    from fef_sidecar.main import refresh_prior_close
    s = _state(request)
    await refresh_prior_close(s)
    pc = s.prior_close_by_symbol.get(s.active_symbol)
    return {
        "ok": pc is not None,
        "active_symbol": s.active_symbol,
        "prior_close": pc,
    }


@router.post("/diag/backfill_spx_bars")
async def diag_backfill_spx_bars(
    request: Request,
    symbol: str = "SPX",
    bar_size: str = "1 min",
    duration: str = "5 D",
) -> dict[str, Any]:
    """Pull historical bars from IBKR and upsert into HistoricalBar.

    Default: last 5 trading days of 1-minute SPX bars. Used to give the
    backtester a higher-resolution SPX path than the 2-minute iter cadence
    so target/stop checks fire at realistic times.

    Idempotent: skips bars already in the DB (unique on symbol+bar_size+
    timestamp_ms).
    """
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from fef_sidecar.ibkr.models import Contract
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import HistoricalBar
    from sqlmodel import select as _s

    s = _state(request)
    if not isinstance(s.ibkr, LiveIBKRProvider):
        return {"mode": "mock", "added": 0}
    ib = getattr(s.ibkr, "_ib", None)
    if ib is None or not ib.isConnected():
        return {"mode": "disconnected"}

    try:
        contract = Contract.for_symbol(symbol)
        from fef_sidecar.ibkr.live import _to_ib_contract
        ib_contract = _to_ib_contract(contract)
        await ib.qualifyContractsAsync(ib_contract)
        # SPX uses TRADES bar type since it has no bid/ask (index).
        bars = await ib.reqHistoricalDataAsync(
            ib_contract,
            endDateTime="",  # empty = up to now
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES" if symbol == "SPX" else "MIDPOINT",
            useRTH=True,
            formatDate=2,  # epoch seconds
            keepUpToDate=False,
        )
    except Exception as e:
        return {"error": f"IBKR fetch failed: {type(e).__name__}: {e}"}

    added = 0; existing = 0
    async with _gs() as session:
        # Pull existing timestamps to avoid duplicates
        existing_rows = list((await session.exec(
            _s(HistoricalBar)
            .where(HistoricalBar.symbol == symbol)
            .where(HistoricalBar.bar_size == bar_size)
        )).all())
        existing_ts = {r.timestamp_ms for r in existing_rows}
        for b in bars:
            try:
                ts_ms = int(b.date.timestamp() * 1000) if hasattr(b.date, "timestamp") else int(b.date * 1000)
            except Exception:
                continue
            if ts_ms in existing_ts:
                existing += 1
                continue
            session.add(HistoricalBar(
                symbol=symbol, bar_size=bar_size, timestamp_ms=ts_ms,
                open=float(b.open), high=float(b.high), low=float(b.low),
                close=float(b.close), volume=float(b.volume or 0),
            ))
            added += 1
        if added:
            await session.commit()
    return {
        "symbol": symbol, "bar_size": bar_size, "duration": duration,
        "fetched_from_ibkr": len(bars),
        "added_to_db": added,
        "already_existed": existing,
    }


@router.post("/diag/backfill_es_vwap")
async def diag_backfill_es_vwap(
    request: Request,
    duration: str = "5 D",
) -> dict[str, Any]:
    """Compute ES session-anchored VWAP from IBKR 1-min TRADES bars and upsert
    into es_vwap_point.

    ES (not SPX cash) because VWAP needs traded volume; the index has none.
    Each bar carries `average` (its own VWAP) and `volume`; session_vwap is the
    cumulative volume-weighted average from RTH open, reset per ET session.
    Idempotent (unique on symbol+timestamp_ms). Serves both backfill and the
    on-demand refresh the live loop also performs.
    """
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from fef_sidecar.loop import refresh_es_vwap

    s = _state(request)
    if not isinstance(s.ibkr, LiveIBKRProvider):
        return {"mode": "mock", "added": 0}
    ib = getattr(s.ibkr, "_ib", None)
    if ib is None or not ib.isConnected():
        return {"mode": "disconnected"}
    try:
        added = await refresh_es_vwap(ib, duration=duration)
    except Exception as e:
        return {"error": f"ES VWAP backfill failed: {type(e).__name__}: {e}"}
    return {"symbol": "ES", "duration": duration, "added_to_db": added}


@router.get("/backtest/full_pipeline")
async def backtest_full_pipeline(
    request: Request,
    session_date_from: str,
    session_date_to: str | None = None,
    apply_be_ladder: bool = True,
    commission_per_contract: float = 0.65,
    avg_entry_slip_pct: float | None = None,
    avg_exit_slip_pct: float | None = None,
    preset_id: int | None = None,
) -> dict[str, Any]:
    """Full-pipeline backtest. For every SignalIter row in [from, to] that
    would have fired the auto-trader, simulate:
      1. Entry at limit (with optional slippage)
      2. Walk forward through subsequent iters' SPX path
      3. Check T1/T2/T3/STOP crossings against the verdict's targets
      4. Optionally simulate BE ladder ratcheting (apply_be_ladder=true)
      5. Apply commission per round trip

    Returns per-trade outcomes + equity curve + Sharpe/MDD aggregated.

    Slippage: if avg_entry_slip_pct / avg_exit_slip_pct are None, computed
    from actual IBKRExecution data for that contract. Otherwise applies the
    user-supplied flat pct.

    NOTE: this is a strategy backtester (replays auto-trader gates +
    watcher exits), distinct from optimization.py (parameter sweep at
    threshold level) and backtest.py (pattern matching). Use this for
    "if I changed the BE ladder, what would Tuesday have looked like?"
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import (
        SignalIter, FEFDirection, IBKRExecution, StrategyPreset, HistoricalBar,
    )
    from sqlmodel import select as _s
    import statistics as _stat
    import math as _math
    import traceback as _tb

    to_date = session_date_to or session_date_from

    # === Outer try/except wraps EVERYTHING so we always return JSON, never 500.
    # Inner code paths can still skip individual rows on per-row errors.
    try:
        async with _gs() as session:
            sigs = list((await session.exec(
                _s(SignalIter)
                .where(SignalIter.session_date >= session_date_from)
                .where(SignalIter.session_date <= to_date)
                .order_by(SignalIter.timestamp)  # type: ignore[attr-defined]
            )).all())
            # Historical fills for slippage estimation
            fills = list((await session.exec(
                _s(IBKRExecution)
                .where(IBKRExecution.session_date >= session_date_from)
                .where(IBKRExecution.session_date <= to_date)
            )).all())
            # Load preset thresholds if requested. When preset_id is set,
            # we instantiate a fresh FEFEngine with those thresholds and
            # re-evaluate every iter's inputs through it. Different presets
            # → different verdicts → different trades.
            preset_row = None
            if preset_id is not None:
                preset_row = (await session.exec(
                    _s(StrategyPreset).where(StrategyPreset.id == preset_id)
                )).first()
            # Pull 1-min SPX bars for finer-grained path resolution. The
            # backtester walks forward on these instead of iter snapshots —
            # catches stop/target crosses inside a 2-min iter window that
            # would otherwise be missed by snapshot-cadence checks.
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            try:
                _ts_lo = int(_dt.fromisoformat(session_date_from).replace(tzinfo=_tz.utc).timestamp() * 1000)
                _ts_hi = int((_dt.fromisoformat(to_date).replace(tzinfo=_tz.utc) + _td(days=1)).timestamp() * 1000)
            except Exception:
                _ts_lo = 0; _ts_hi = 10**13
            spx_bars = list((await session.exec(
                _s(HistoricalBar)
                .where(HistoricalBar.symbol == "SPX")
                .where(HistoricalBar.bar_size == "1 min")
                .where(HistoricalBar.timestamp_ms >= _ts_lo)
                .where(HistoricalBar.timestamp_ms < _ts_hi)
                .order_by(HistoricalBar.timestamp_ms)  # type: ignore[attr-defined]
            )).all())
    except Exception as e:
        return {
            "error": f"DB query failed: {type(e).__name__}: {e}",
            "trace": _tb.format_exc().splitlines()[-5:],
        }

    # Wrap remaining computation in a top-level try/except so a single
    # malformed historical row never produces a 500.
    try:
        return _backtest_compute(
            sigs=sigs, fills=fills, spx_bars=spx_bars,
            session_date_from=session_date_from, to_date=to_date,
            apply_be_ladder=apply_be_ladder,
            commission_per_contract=commission_per_contract,
            avg_entry_slip_pct=avg_entry_slip_pct,
            avg_exit_slip_pct=avg_exit_slip_pct,
            preset_row=preset_row,
        )
    except Exception as e:
        return {
            "error": f"backtest computation failed: {type(e).__name__}: {e}",
            "trace": _tb.format_exc().splitlines()[-10:],
            "from": session_date_from, "to": to_date,
            "iters_scanned": len(sigs),
        }


# BE ladder rungs — shared with spx_target_watcher / daily_review. Each
# tuple is (MFE trigger R, LOCKED stop R). When MFE reaches the trigger the
# stop RATCHETS up to the locked level. A stop that fires after ratcheting
# therefore banks the LOCKED R (0.0/0.5/1.0), NOT the trigger (0.5/1.0/1.5).
_BE_LADDER = [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)]
# 50/30/20 scale-out shares — same split daily_review + the auto-trader use.
_T1_SHARE, _T2_SHARE, _T3_SHARE = 0.5, 0.3, 0.2


def _walk_be_ladder_exit(*, entry_spx, t1, t2, t3, stop, is_long,
                         path_items, apply_be_ladder):
    """Walk a forward SPX path with a BE-ratcheting dynamic stop + 50/30/20
    partial scale-out and return the partial-aware realized R.

    ``path_items``: iterable of (ts_ms, open, high, low, close) tuples.

    Scoring mirrors ``daily_review._trace_outcome`` + its partial banking:

      * Each tier (T1/T2/T3) first-touched banks its share at that tier's
        ACTUAL R (``|target - entry| / risk``) — NOT a hardcoded 1.5/2.5/5.0
        magnitude. A full T3 run books ``0.5·r_t1 + 0.3·r_t2 + 0.2·r_t3``,
        so the booked R tracks the verdict's real target distances.
      * A stop settles the UNBANKED residual share at the dynamic stop's
        LOCKED R. A breakeven ratchet (locked 0.0R) therefore scores 0.0R on
        the residual, NOT the +0.5R ladder TRIGGER that used to leak in as a
        phantom win and silently flip tournament boards toward
        ``apply_be_ladder=true``.
      * A timeout leaves the residual unsettled (0R on that share), matching
        ``daily_review``.

    Returns ``dict(outcome, realized_R, be_step, be_lock_R, exit_spx)``.
    """
    risk_spx = abs(entry_spx - stop)
    risk_r = risk_spx or 1.0
    r_t1 = abs(t1 - entry_spx) / risk_r
    r_t2 = abs(t2 - entry_spx) / risk_r
    r_t3 = abs(t3 - entry_spx) / risk_r

    dyn_stop = stop
    be_step = 0.0      # highest ladder TRIGGER reached (0.5/1.0/1.5)
    be_lock_R = 0.0    # LOCKED stop level in R (0.0/0.5/1.0)
    t1_fired = t2_fired = t3_fired = False
    stop_hit_flag = False
    dyn_stop_at_hit = None
    exit_spx = path_items[-1][4] if path_items else entry_spx

    for _ts_ms, _p_open, p_high, p_low, p_close in path_items:
        favor_extreme = p_high if is_long else p_low
        adverse_extreme = p_low if is_long else p_high
        if apply_be_ladder and risk_spx > 0:
            mfe_R = ((favor_extreme - entry_spx) / risk_spx if is_long
                     else (entry_spx - favor_extreme) / risk_spx)
            for trig_R, locked_R in _BE_LADDER:
                if mfe_R >= trig_R and be_step < trig_R:
                    be_step = trig_R
                    be_lock_R = locked_R
                    if is_long:
                        new_stop = entry_spx + locked_R * risk_spx
                        if new_stop > dyn_stop:
                            dyn_stop = new_stop
                    else:
                        new_stop = entry_spx - locked_R * risk_spx
                        if new_stop < dyn_stop:
                            dyn_stop = new_stop
        # Conservative intra-bar ordering: check the stop BEFORE targets, using
        # the adverse extreme so a wick through the stop counts as a hit.
        stop_hit = ((is_long and adverse_extreme <= dyn_stop)
                    or (not is_long and adverse_extreme >= dyn_stop))
        if stop_hit:
            stop_hit_flag = True
            dyn_stop_at_hit = dyn_stop
            exit_spx = dyn_stop
            break
        # First-touch tiers (favorable extreme). Partials scale out while the
        # residual keeps running, so we do NOT break on T1/T2.
        if (is_long and favor_extreme >= t1) or (not is_long and favor_extreme <= t1):
            t1_fired = True
        if (is_long and favor_extreme >= t2) or (not is_long and favor_extreme <= t2):
            t2_fired = True
        if (is_long and favor_extreme >= t3) or (not is_long and favor_extreme <= t3):
            t3_fired = True
        if t3_fired:
            # 50/30/20 → all three tiers scale the whole position out.
            exit_spx = t3
            break

    # Partial-aware realized R (daily_review methodology).
    realized_R = 0.0
    residual_share = 1.0
    if t1_fired:
        realized_R += _T1_SHARE * r_t1
        residual_share -= _T1_SHARE
    if t2_fired:
        realized_R += _T2_SHARE * r_t2
        residual_share -= _T2_SHARE
    if t3_fired:
        realized_R += _T3_SHARE * r_t3
        residual_share -= _T3_SHARE
    if stop_hit_flag and residual_share > 0.001:
        dyn = dyn_stop_at_hit if dyn_stop_at_hit is not None else stop
        stop_r = ((dyn - entry_spx) / risk_r if is_long
                  else (entry_spx - dyn) / risk_r)
        realized_R += residual_share * stop_r

    # Outcome label: highest tier reached, else BE_STOP/STOP/TIMEOUT. The
    # BE_STOP label carries the LOCKED R so it reads honestly in trade logs.
    if t3_fired:
        outcome = "T3"
    elif t2_fired:
        outcome = "T2"
    elif t1_fired:
        outcome = "T1"
    elif stop_hit_flag:
        ratcheted = (apply_be_ladder and dyn_stop_at_hit is not None
                     and dyn_stop_at_hit != stop)
        outcome = f"BE_STOP_{be_lock_R:.1f}R" if ratcheted else "STOP"
    else:
        outcome = "TIMEOUT"

    return {
        "outcome": outcome,
        "realized_R": realized_R,
        "be_step": be_step,
        "be_lock_R": be_lock_R,
        "exit_spx": exit_spx,
    }


def _backtest_compute(*, sigs, fills, spx_bars=None, session_date_from, to_date,
                      apply_be_ladder, commission_per_contract,
                      avg_entry_slip_pct, avg_exit_slip_pct,
                      preset_row=None):
    """Synchronous backtest computation. Called from the route handler with
    pre-queried sigs + fills. Pure computation — wrapped in caller's
    try/except so any exception becomes a JSON error response.

    When preset_row is provided, every iter is re-evaluated through a fresh
    FEFEngine instantiated with that preset's thresholds. The stored
    SignalIter.direction is IGNORED — the preset's engine produces the
    verdict that drives the trade. This is how different presets produce
    different backtest results from the same historical data.
    """
    import statistics as _stat

    # If we have a preset, build a fresh engine with its thresholds and
    # re-evaluate every historical iter. Otherwise we trust the stored
    # SignalIter rows (which were produced under the live engine's config
    # at the time they were recorded).
    fresh_verdicts: dict[int, dict] | None = None
    if preset_row is not None:
        try:
            from fef_sidecar.engine import FEFEngine
            from fef_sidecar.engine.types import FEFInputs
            engine = FEFEngine(
                wall_proximity_pts=float(preset_row.wall_proximity_pts),
                # Preset replays EXPLICITLY parametrize wall_proximity_pts as
                # a strategy knob, so the proximity gate is opt-in here by
                # construction — unlike the LIVE default, which is OFF per
                # the 2026-07-02 validation (chop gate owns the harm region).
                wall_proximity_skip_enabled=True,
                wall_collapse_pct=float(preset_row.wall_collapse_pct),
                wall_collapse_floor_m=float(preset_row.wall_collapse_floor_m),
                absorption_fe_ratio_min=float(preset_row.absorption_fe_ratio_min),
            )
            fresh_verdicts = {}
            for s in sigs:
                try:
                    from fef_sidecar.engine.types import (
                        expiration_exposure_rows_from_dicts,
                        exposure_rows_from_dicts,
                    )
                    inputs = FEFInputs(
                        timestamp_ms=int(s.timestamp.timestamp() * 1000) if s.timestamp else 0,
                        spx_price=float(s.spx_price),
                        drift_cum_net_m=float(s.drift_cum_net_m or 0),
                        drift_bias=s.drift_bias or "NEUTRAL",
                        top_gex_walls=exposure_rows_from_dicts(s.top_gex_walls),
                        top_dex_walls=exposure_rows_from_dicts(s.top_dex_walls),
                        ntm_max_pain=float(s.ntm_max_pain or s.spx_price),
                        raw_max_pain=float(s.raw_max_pain or s.spx_price),
                        aggression_call_pct=s.aggression_call_pct,
                        aggression_put_pct=s.aggression_put_pct,
                        top_cex_walls=exposure_rows_from_dicts(getattr(s, "top_cex_walls", None) or []),
                        top_vex_walls=exposure_rows_from_dicts(getattr(s, "top_vex_walls", None) or []),
                        top_gex_by_expiry=expiration_exposure_rows_from_dicts(getattr(s, "top_gex_by_expiry", None) or []),
                        top_dex_by_expiry=expiration_exposure_rows_from_dicts(getattr(s, "top_dex_by_expiry", None) or []),
                        zero_dte_gex_net_m=getattr(s, "zero_dte_gex_net_m", None),
                        zero_dte_dex_net_m=getattr(s, "zero_dte_dex_net_m", None),
                        recent_whale_count_30m=int(getattr(s, "recent_whale_count_30m", 0) or 0),
                        recent_golden_sweep_count_30m=int(getattr(s, "recent_golden_sweep_count_30m", 0) or 0),
                    )
                    v = engine.push(inputs)
                    fresh_verdicts[s.id] = {
                        "direction": v.direction.value,
                        "t1": v.t1, "t2": v.t2, "t3": v.t3, "stop": v.stop,
                        "strike_rec": v.strike_rec,
                    }
                except Exception:
                    # If we can't re-evaluate this iter, fall back to skip
                    fresh_verdicts[s.id] = {"direction": "WAIT"}
        except Exception:
            logger.exception("preset re-evaluation failed; falling back to stored verdicts")
            fresh_verdicts = None

    # Compute historical avg slippage per side from IBKRExecution + SignalIter
    # extras (entry_limit on iter vs actual fill price). Only used when caller
    # didn't supply explicit slippage pct.
    if avg_entry_slip_pct is None:
        slip_samples = []
        try:
            for s_ in sigs:
                try:
                    ex = s_.extras or {}
                    opt = ex.get("option_pricing") or {}
                    limit = opt.get("entry_limit")
                    if not limit: continue
                    try: limit = float(limit)
                    except (TypeError, ValueError): continue
                    if limit <= 0: continue
                    strike_rec = ex.get("strike_rec")
                    if strike_rec is None: continue
                    try: strike_rec = float(strike_rec)
                    except (TypeError, ValueError): continue
                    for f in fills:
                        try:
                            if abs(float(f.strike) - strike_rec) > 0.5: continue
                            if f.side != "BOT": continue
                            f_ts = f.timestamp.replace(tzinfo=None) if (f.timestamp and f.timestamp.tzinfo) else f.timestamp
                            s_ts = s_.timestamp.replace(tzinfo=None) if (s_.timestamp and s_.timestamp.tzinfo) else s_.timestamp
                            if f_ts is None or s_ts is None: continue
                            dt = abs((f_ts - s_ts).total_seconds())
                            if dt > 300: continue
                            slip_samples.append((float(f.price) - limit) / limit * 100)
                            break
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            logger.exception("slip sample computation failed; defaulting to 0")
        # Manual mean — Python 3.14's statistics module has a bug where
        # _exact_ratio on certain float-like inputs raises AttributeError
        # on the .numerator attribute. Plain sum/len avoids the issue.
        avg_entry_slip_pct = round(sum(float(x) for x in slip_samples) / len(slip_samples), 2) if slip_samples else 0.0

    if avg_exit_slip_pct is None:
        avg_exit_slip_pct = avg_entry_slip_pct  # approx

    # Build SPX timeline. Prefer 1-min bars (high/low intra-bar — catches
    # stop/target crosses inside a bar that iter-snapshot misses). Fall back
    # to iter-cadence (timestamp, close) tuples when bars are missing.
    spx_path = [(s_.timestamp, s_.spx_price) for s_ in sigs]
    bars_sorted = sorted(spx_bars or [], key=lambda b: b.timestamp_ms)
    bar_starts = [b.timestamp_ms for b in bars_sorted]
    use_bars = len(bars_sorted) > 0
    import bisect as _bisect

    trades: list[dict[str, Any]] = []
    skipped = {"no_dir": 0, "no_opt": 0, "no_targets": 0, "exception": 0}
    # Track when entry_limit was synthesized (preset re-eval fires on a
    # signal that the live engine WAITed on, so no stored option_pricing).
    # The R-multiple math is SPX-point based, so a placeholder entry_limit
    # only affects commission cost (tiny relative to R).
    synth_entry_count = 0
    for idx, s_ in enumerate(sigs):
      try:
        ex = s_.extras or {}
        # If we have a preset's fresh verdicts, use those. Otherwise fall
        # back to the stored SignalIter.direction + extras.t1/t2/t3.
        fresh = fresh_verdicts.get(s_.id) if fresh_verdicts else None
        used_preset_verdict = fresh is not None
        if fresh:
            dir_val = fresh["direction"]
            t1 = fresh.get("t1"); t2 = fresh.get("t2")
            t3 = fresh.get("t3"); stop = fresh.get("stop")
        else:
            dir_val = s_.direction.value if hasattr(s_.direction, "value") else str(s_.direction)
            t1 = ex.get("t1"); t2 = ex.get("t2"); t3 = ex.get("t3"); stop = ex.get("stop")
        if dir_val not in ("LONG_CALLS", "LONG_PUTS"):
            skipped["no_dir"] += 1; continue
        # Bug #11 fix: when a PRESET re-eval fires on a signal that the live
        # engine WAITed on (e.g. tight-pin preset has tighter wall-prox so it
        # accepts more signals), the stored signal has no option_pricing.
        # Previously this caused all preset re-evals to converge to the same
        # ~220 trades (the count of LIVE-fired signals that had pricing).
        #
        # Synthesize a placeholder entry_limit from the SPX distance to t1
        # (approx atm option price) when stored option_pricing is missing.
        # The R-multiple itself is SPX-point based, so this only affects the
        # commission cost (~0.05R per trade — small enough not to dominate).
        opt = ex.get("option_pricing") or {}
        entry_limit = opt.get("entry_limit")
        if not entry_limit or entry_limit <= 0:
            if used_preset_verdict and t1 is not None and stop is not None:
                # Crude ATM option price: ~half the risk distance in SPX pts.
                # For SPX 0DTE ATM, an option premium of 5-15$ is typical for
                # 10-20pt risk distances. Use max(5.0, risk_pts * 0.5) clamped.
                try:
                    risk_pts = abs(float(s_.spx_price) - float(stop))
                    entry_limit = max(5.0, min(50.0, risk_pts * 0.5))
                    synth_entry_count += 1
                except (TypeError, ValueError):
                    skipped["no_opt"] += 1; continue
            else:
                skipped["no_opt"] += 1; continue
        if t1 is None or t2 is None or t3 is None or stop is None:
            skipped["no_targets"] += 1; continue
        # Defensive: ensure numeric (older rows might have string/Decimal)
        try:
            t1 = float(t1); t2 = float(t2); t3 = float(t3); stop = float(stop)
            entry_limit = float(entry_limit)
        except (TypeError, ValueError):
            skipped["no_targets"] += 1; continue
        entry_spx = s_.spx_price
        if entry_spx is None: continue
        entry_spx = float(entry_spx)
        is_long = dir_val == "LONG_CALLS"

        # Build forward path. Prefer 1-min bars from HistoricalBar (high+low
        # intra-bar catches stop/target crosses that 2-min iter snapshots miss).
        entry_ts_ms = (
            int(s_.timestamp.timestamp() * 1000)
            if s_.timestamp else 0
        )
        bar_path = []
        if use_bars and entry_ts_ms > 0:
            start_i = _bisect.bisect_left(bar_starts, entry_ts_ms)
            # ~3 hours of 1-min bars = 180
            bar_path = bars_sorted[start_i:start_i + 180]

        if bar_path:
            # 1-min path: each tick gives high/low for cross detection
            path_items = [(b.timestamp_ms, float(b.open), float(b.high),
                           float(b.low), float(b.close)) for b in bar_path]
            path_mode = "1min_bar"
        else:
            # Fallback: iter-cadence — use spx_price as both high/low/close
            iter_slice = spx_path[idx + 1:idx + 90]
            if not iter_slice:
                continue
            path_items = [(int(t.timestamp() * 1000) if t else 0, p, p, p, p)
                          for t, p in iter_slice]
            path_mode = "iter_snapshot"

        # BE-ratcheting dynamic stop + 50/30/20 partial scale-out, scored
        # with the partial-aware LOCKED-R methodology (see
        # _walk_be_ladder_exit). This is the fix for the BE-stop scoring bug:
        # a breakeven ratchet now banks the LOCKED 0.0R on the residual
        # instead of the +0.5R ladder TRIGGER — the leak that used to flip
        # tournament boards positive purely from apply_be_ladder=true. Full
        # T3 runs book the verdict's ACTUAL per-tier R (not a hardcoded 2.95).
        _walk = _walk_be_ladder_exit(
            entry_spx=entry_spx, t1=t1, t2=t2, t3=t3, stop=stop,
            is_long=is_long, path_items=path_items,
            apply_be_ladder=apply_be_ladder,
        )
        outcome = _walk["outcome"]
        realized_R = _walk["realized_R"]
        be_step = _walk["be_step"]
        exit_spx = _walk["exit_spx"]

        # Apply slippage + commission. All three are per-contract R fractions:
        # commission_R must NOT multiply by qty (the qty in the numerator's
        # total commission and the denominator's total premium cancel) —
        # multiplying made it ~qty× (≈5×) too large.
        qty = 5  # default
        slip_R_entry = -avg_entry_slip_pct / 100 * 0.5  # rough cost in R
        slip_R_exit = -avg_exit_slip_pct / 100 * 0.5
        commission_R = -(commission_per_contract * 2) / (entry_limit * 100)  # roundtrip / per-contract premium
        realized_R_net = realized_R + slip_R_entry + slip_R_exit + commission_R

        trades.append({
            "timestamp": s_.timestamp.isoformat(),
            "session_date": s_.session_date,
            "direction": dir_val,
            "entry_spx": entry_spx,
            "exit_spx": exit_spx,
            "outcome": outcome,
            "be_step_R": be_step,
            "realized_R_gross": round(realized_R, 3),
            "realized_R_net": round(realized_R_net, 3),
            "path_mode": path_mode,
        })
      except Exception as _e:
        skipped["exception"] += 1
        logger.exception("backtest iter %s failed", s_.id)
        continue

    # Aggregate metrics
    if not trades:
        return {
            "from": session_date_from, "to": to_date,
            "trade_count": 0,
            "iters_scanned": len(sigs),
            "skipped": skipped,
            "synthesized_entry_limit_count": synth_entry_count,
            "note": "no trades fired in this window — see 'skipped' for reasons",
        }

    # Manual aggregation — avoid statistics module (Py 3.14 .numerator bug).
    # Sanitize: drop NaN / Inf values that would poison sum-based metrics.
    import math as _math
    R_series_raw = [float(t["realized_R_net"]) for t in trades]
    R_series = [r for r in R_series_raw if _math.isfinite(r)]
    if not R_series:
        return {
            "from": session_date_from, "to": to_date,
            "trade_count": len(trades),
            "iters_scanned": len(sigs),
            "skipped": skipped,
            "note": f"all {len(trades)} trades had non-finite R — likely data issue",
        }
    wins = sum(1 for r in R_series if r > 0)
    win_rate = wins / len(R_series) * 100
    expectancy = sum(R_series) / len(R_series)
    if len(R_series) >= 2:
        variance = sum((r - expectancy) ** 2 for r in R_series) / (len(R_series) - 1)
        std_R = variance ** 0.5
        sharpe = (expectancy / std_R) if std_R > 0 else None
    else:
        sharpe = None
    cum = 0.0; peak = 0.0; max_dd = 0.0
    for r in R_series:
        cum += r
        if cum > peak: peak = cum
        if peak - cum > max_dd: max_dd = peak - cum
    gross_win = sum(r for r in R_series if r > 0)
    gross_loss = abs(sum(r for r in R_series if r < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else None

    return {
        "from": session_date_from, "to": to_date,
        "trade_count": len(trades),
        "iters_scanned": len(sigs),
        "skipped": skipped,
        "synthesized_entry_limit_count": synth_entry_count,
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate_pct": round(win_rate, 2),
        "expectancy_R": round(expectancy, 3),
        "sharpe_ratio": round(sharpe, 3) if sharpe else None,
        "max_drawdown_R": round(max_dd, 3),
        "profit_factor": round(pf, 3) if pf else None,
        "cumulative_R": round(sum(R_series), 3),
        "be_ladder_applied": apply_be_ladder,
        "avg_entry_slip_pct_used": avg_entry_slip_pct,
        "avg_exit_slip_pct_used": avg_exit_slip_pct,
        "commission_per_contract": commission_per_contract,
        "trades": trades,
    }


@router.get("/diag/signal_audit_buckets")
async def diag_signal_audit_buckets(
    request: Request, session_date: str | None = None
) -> dict[str, Any]:
    """For each parent on `session_date`, join with its firing SignalIter
    (closest timestamp before submit), then bucket by (right, status):
    C_WIN / C_LOSS / P_WIN / P_LOSS. Compute medians + means for numeric
    fields and counters for categorical, so daily pattern audits don't
    require ad-hoc Python.

    Numeric fields surfaced: delta_price, delta_drift, fe_ratio,
    drift_cum_net_m, pa_score, mp_gravity, gex_walls, dex_floor,
    nearest_wall_distance, plus HFT/microstructure when present.
    Categorical: confidence, drift_bias, aligned, vol_regime,
    book_direction, micro_direction.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import LiveOrder, SignalIter
    from sqlmodel import select as _s
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import statistics as _stat
    from collections import Counter as _Counter

    target_date = session_date or _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")

    async with _gs() as session:
        parents = list((await session.exec(
            _s(LiveOrder).where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.dry_run == False)  # noqa: E712
        )).all())
        parents = [p for p in parents
                   if p.submitted_at.strftime("%Y-%m-%d") == target_date]

        iters = list((await session.exec(
            _s(SignalIter).where(SignalIter.session_date == target_date)
            .order_by(SignalIter.timestamp)  # type: ignore[attr-defined]
        )).all())

    def _find_iter(submit_dt):
        best = None; best_diff = None
        for it in iters:
            it_ts = it.timestamp
            if it_ts.tzinfo is None:
                it_ts = it_ts.replace(tzinfo=_tz.utc)
            sub_ts = submit_dt
            if sub_ts.tzinfo is None:
                sub_ts = sub_ts.replace(tzinfo=_tz.utc)
            diff = (sub_ts - it_ts).total_seconds()
            if diff < 0 or diff > 60:
                continue
            if best_diff is None or diff < best_diff:
                best = it; best_diff = diff
        return best

    records: list[dict] = []
    for p in parents:
        it = _find_iter(p.submitted_at)
        if it is None:
            continue
        ext = dict(it.extras or {})
        sec = ext.get("secondary_scores") or {}
        hft = ext.get("hft_at_fire") or {}
        micro = ext.get("microstructure_at_fire") or {}
        records.append({
            "pid": p.id, "right": p.right, "status": p.status,
            "side_status": f"{p.right}_{'WIN' if p.status == 'CLOSED' else ('LOSS' if p.status == 'STOPPED' else 'OTHER')}",
            "direction": it.direction.value if it.direction else None,
            "confidence": it.confidence.value if it.confidence else None,
            "delta_price": ext.get("delta_price"),
            "delta_drift": ext.get("delta_drift_m"),
            "fe_ratio": ext.get("fe_ratio"),
            "aligned": ext.get("aligned"),
            "absorption": ext.get("absorption"),
            "wall_collapse": ext.get("wall_collapse_trigger"),
            "nearest_wall_distance": ext.get("nearest_wall_distance"),
            "drift_cum_net_m": it.drift_cum_net_m,
            "drift_bias": it.drift_bias,
            "pa_score": sec.get("price_action"),
            "mp_gravity": sec.get("mp_gravity"),
            "drift_score": sec.get("drift"),
            "gex_walls": sec.get("gex_walls"),
            "dex_floor": sec.get("dex_floor"),
            "agg_score": sec.get("aggression"),
            "hft_present": bool(hft),
            "vol_regime": hft.get("vol_regime"),
            "short_vol_pts": hft.get("short_vol_pts"),
            "vol_ratio": hft.get("vol_ratio"),
            "cancel_trade_ratio": hft.get("cancel_to_trade_ratio"),
            "book_imb_1": hft.get("book_imbalance"),
            "book_imb_5": hft.get("book_imbalance_5lvl"),
            "book_direction": hft.get("book_direction"),
            "book_conf": hft.get("book_confidence"),
            "composite_dir": hft.get("composite_direction"),
            "micro_present": bool(micro),
            "ofi_window": micro.get("ofi_window"),
            "avi_pct": micro.get("avi_pct"),
            "microprice_dev_bp": micro.get("microprice_deviation_bp"),
            "micro_direction": micro.get("direction"),
            "micro_conf": micro.get("confidence"),
        })

    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(r["side_status"], []).append(r)

    def _stats(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        return {
            "n": len(vals),
            "mean": round(_stat.mean(vals), 4),
            "median": round(_stat.median(vals), 4),
            "min": round(min(vals), 4),
            "max": round(max(vals), 4),
        }

    numeric_fields = [
        "delta_price", "delta_drift", "fe_ratio",
        "drift_cum_net_m", "pa_score", "mp_gravity",
        "drift_score", "gex_walls", "dex_floor", "agg_score",
        "nearest_wall_distance",
        "short_vol_pts", "vol_ratio", "cancel_trade_ratio",
        "book_imb_1", "book_imb_5",
        "ofi_window", "avi_pct", "microprice_dev_bp",
    ]
    cat_fields = [
        "confidence", "drift_bias", "aligned", "absorption", "wall_collapse",
        "vol_regime", "book_direction", "book_conf", "composite_dir",
        "micro_direction", "micro_conf",
    ]

    numeric_by_bucket: dict[str, dict[str, dict]] = {}
    for bucket, rows in groups.items():
        numeric_by_bucket[bucket] = {
            f: _stats([r[f] for r in rows]) for f in numeric_fields
        }
    categorical_by_bucket: dict[str, dict[str, dict]] = {}
    for bucket, rows in groups.items():
        categorical_by_bucket[bucket] = {
            f: dict(_Counter(r[f] for r in rows if r[f] is not None))
            for f in cat_fields
        }

    summary = {b: {"n": len(rs)} for b, rs in groups.items()}
    return {
        "session_date": target_date,
        "total_trades_audited": len(records),
        "hft_snapshot_coverage": sum(1 for r in records if r["hft_present"]),
        "micro_snapshot_coverage": sum(1 for r in records if r["micro_present"]),
        "summary_by_bucket": summary,
        "numeric_by_bucket": numeric_by_bucket,
        "categorical_by_bucket": categorical_by_bucket,
        "trades": records,  # full per-trade rows for downstream filtering
    }


@router.get("/diag/hour_of_day_stats")
async def diag_hour_of_day_stats(
    request: Request, days_back: int = 30
) -> dict[str, Any]:
    """Aggregate `by_hour_ct` JSON across recent DailyReview rows so the
    "11am-noon CT prints money" hypothesis can be checked across multiple
    sessions, not just the most recent one.

    Returns per-hour totals (signals, wins, expectancy) across the lookback
    window, plus the same data broken out per-session for trend analysis.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import DailyReview
    from sqlmodel import select as _s
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    cutoff = (_dt.now(_tz.utc) - _td(days=days_back)).strftime("%Y-%m-%d")

    async with _gs() as session:
        reviews = list((await session.exec(
            _s(DailyReview).where(DailyReview.session_date >= cutoff)
            .order_by(DailyReview.session_date)  # type: ignore[attr-defined]
        )).all())

    per_session: list[dict] = []
    rolled: dict[str, dict] = {}  # hour_ct (str) -> aggregated stats

    for r in reviews:
        hours = dict(r.by_hour_ct or {})
        per_session.append({
            "session_date": r.session_date,
            "by_hour_ct": hours,
        })
        for hr_key, stats in hours.items():
            if not isinstance(stats, dict):
                continue
            slot = rolled.setdefault(str(hr_key), {
                "n": 0, "wins": 0, "stops": 0, "timeouts": 0,
                "expectancy_sum_R": 0.0, "sessions": 0,
            })
            slot["sessions"] += 1
            n = int(stats.get("n", 0) or 0)
            wins = int(stats.get("wins", 0) or 0)
            stops = int(stats.get("stops", 0) or 0)
            timeouts = int(stats.get("timeouts", 0) or 0)
            exp_r = float(stats.get("expectancy_r", 0.0) or 0.0)
            slot["n"] += n
            slot["wins"] += wins
            slot["stops"] += stops
            slot["timeouts"] += timeouts
            slot["expectancy_sum_R"] += exp_r * n  # weight by sample size

    rolled_out = {}
    for hr, v in rolled.items():
        n = v["n"]
        rolled_out[hr] = {
            "total_signals": n,
            "total_wins": v["wins"],
            "total_stops": v["stops"],
            "total_timeouts": v["timeouts"],
            "sessions_with_data": v["sessions"],
            "win_rate_pct": round(v["wins"] * 100 / n, 2) if n else 0.0,
            "weighted_expectancy_r": round(v["expectancy_sum_R"] / n, 4) if n else 0.0,
        }

    # Hour sort
    def _hr_sort(k: str) -> int:
        try:
            return int(k.split(":")[0]) if ":" in k else int(k)
        except Exception:
            return 99

    sorted_hours = sorted(rolled_out.items(), key=lambda kv: _hr_sort(kv[0]))

    return {
        "days_back": days_back,
        "session_count": len(reviews),
        "hours_aggregated": dict(sorted_hours),
        "per_session": per_session,
    }


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


@router.get("/diag/non_fire_analysis")
async def diag_non_fire_analysis(
    session_date: str | None = None,
    lookahead_minutes: int = 30,
) -> dict[str, Any]:
    """Bucket SignalIter rows by direction × confidence and report the
    forward SPX excursion observed in the next `lookahead_minutes` (5/15/30/60).

    Surfaces "WAIT verdicts that would have hit T1" — the missed-trade
    blind spot. Read-only; freeze-safe.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import SignalIter, SignalLookaheadOutcome
    from sqlmodel import select as _s

    date = session_date or _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")
    if lookahead_minutes not in (5, 15, 30, 60):
        raise HTTPException(400, "lookahead_minutes must be one of 5/15/30/60")

    async with _gs() as session:
        outcomes = list((await session.exec(
            _s(SignalLookaheadOutcome)
            .where(SignalLookaheadOutcome.session_date == date)
            .where(SignalLookaheadOutcome.lookahead_minutes == lookahead_minutes)
        )).all())
        signal_ids = {o.signal_iter_id for o in outcomes}
        sigs = list((await session.exec(
            _s(SignalIter)
            .where(SignalIter.session_date == date)
        )).all())
    sigs_by_id = {s.id: s for s in sigs if s.id is not None}

    by_bucket: dict[str, dict[str, Any]] = {}
    for o in outcomes:
        sig = sigs_by_id.get(o.signal_iter_id)
        if sig is None:
            continue
        bucket_key = f"{o.direction_at_signal}_{o.confidence_at_signal}"
        slot = by_bucket.setdefault(bucket_key, {
            "n": 0, "would_have_hit_t1": 0, "would_have_hit_stop": 0,
            "spx_mfe_pts_sum": 0.0, "spx_mae_pts_sum": 0.0,
        })
        slot["n"] += 1
        if o.would_have_hit_t1:
            slot["would_have_hit_t1"] += 1
        if o.would_have_hit_stop:
            slot["would_have_hit_stop"] += 1
        slot["spx_mfe_pts_sum"] += o.spx_mfe_pts
        slot["spx_mae_pts_sum"] += o.spx_mae_pts
    for slot in by_bucket.values():
        n = max(slot["n"], 1)
        slot["mfe_avg"] = round(slot["spx_mfe_pts_sum"] / n, 2)
        slot["mae_avg"] = round(slot["spx_mae_pts_sum"] / n, 2)
        slot["would_hit_t1_pct"] = round(slot["would_have_hit_t1"] / n * 100, 1)
        slot["would_hit_stop_pct"] = round(slot["would_have_hit_stop"] / n * 100, 1)
        del slot["spx_mfe_pts_sum"], slot["spx_mae_pts_sum"]

    return {
        "session_date": date,
        "lookahead_minutes": lookahead_minutes,
        "total_outcomes": len(outcomes),
        "buckets": by_bucket,
        "note": "WAIT rows with high mfe_avg = missed opportunities. "
                "LOSS rows with low mfe_avg = correctly skipped chop.",
    }


@router.post("/diag/non_fire_backfill")
async def diag_non_fire_backfill(session_date: str | None = None) -> dict[str, Any]:
    """Trigger an on-demand backfill of SignalLookaheadOutcome rows for
    the given session_date (default today). Normally handled by the
    background worker every 5min; this endpoint forces immediate run."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from fef_sidecar.engine.signal_lookahead import backfill_session
    date = session_date or _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")
    return await backfill_session(date)


@router.get("/diag/counterfactual_signal_strength")
async def diag_counterfactual_signal_strength(session_date: str | None = None) -> dict[str, Any]:
    """Replay each closed trade through hypothetical B+C+D exits using
    BE-step-reached as proxy for option MFE. Returns per-parent actual
    vs counterfactual P/L and the aggregate incremental upside.

    Pure analytics — does not modify any state. Quantifies "what would
    today's P/L be with option-price + trail + time-decay exits?"
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from fef_sidecar.engine.counterfactual import run_for_session
    date = session_date or _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")
    return await run_for_session(date)


@router.get("/diag/counterfactual_polling_interval")
async def diag_counterfactual_polling_interval(
    session_date: str | None = None,
    intervals: str = "30,60,90",
) -> dict[str, Any]:
    """Replay FEF engine at faster polling cadences over today's FlowSnapshot
    data + simulate trade outcomes via 1-min SPX bars. Surfaces how many
    additional actionable signals would have fired and their estimated
    win rate / expectancy.

    Quantifies "what if we polled at 30s / 60s instead of 120s?"

    Args:
        session_date: YYYY-MM-DD (default today ET)
        intervals: comma-separated target intervals in seconds (default "30,60,90")
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from fef_sidecar.engine.counterfactual import run_polling_simulation
    date = session_date or _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")
    iv_list = [int(x.strip()) for x in intervals.split(",") if x.strip().isdigit()]
    return await run_polling_simulation(date, iv_list or [30, 60, 90])


@router.get("/diag/fire_order_analysis")
async def diag_fire_order_analysis(session_date: str | None = None) -> dict[str, Any]:
    """Bucket signal outcomes by `fire_order` (1 = first fire of cluster,
    2+ = stacks). Answers "are stacks adding value or hurting?"

    Joins SignalIter (filtered by session_date if given) with LiveOrder
    fills to compute realized R per fire_order bucket.
    """
    from collections import defaultdict
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import IBKRExecution, LiveOrder, SignalIter
    from sqlmodel import select as _s

    date = session_date or _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")

    async with _gs() as session:
        sigs = list((await session.exec(
            _s(SignalIter)
            .where(SignalIter.session_date == date)  # type: ignore[attr-defined]
            .where(SignalIter.direction != "WAIT")  # type: ignore[attr-defined]
        )).all())
        parents = list((await session.exec(
            _s(LiveOrder)
            .where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.dry_run == False)  # noqa: E712
        )).all())
        execs = list((await session.exec(_s(IBKRExecution).where(IBKRExecution.session_date == date))).all())

    parents = [p for p in parents if p.submitted_at.strftime("%Y-%m-%d") == date]

    # Per-contract realized P/L
    by_contract: dict[tuple, dict] = defaultdict(lambda: {"bot_v": 0.0, "sld_v": 0.0, "bot_q": 0.0, "sld_q": 0.0})
    for f in execs:
        key = (f.symbol, f.strike, f.right, f.expiry)
        slot = by_contract[key]
        if f.side == "BOT":
            slot["bot_v"] += f.shares * f.price
            slot["bot_q"] += f.shares
        else:
            slot["sld_v"] += f.shares * f.price
            slot["sld_q"] += f.shares
    contract_pnl: dict[tuple, float] = {}
    for key, slot in by_contract.items():
        paired = min(slot["bot_q"], slot["sld_q"])
        if paired <= 0: continue
        avg_bot = slot["bot_v"] / slot["bot_q"] if slot["bot_q"] else 0
        avg_sld = slot["sld_v"] / slot["sld_q"] if slot["sld_q"] else 0
        contract_pnl[key] = (avg_sld - avg_bot) * paired * 100.0

    # Pair signals with parents (closest submit within 60s, same direction × strike)
    def find_parent(sg):
        from datetime import timezone
        ts = sg.timestamp
        if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
        right = "C" if (sg.direction.value if hasattr(sg.direction, "value") else str(sg.direction)) == "LONG_CALLS" else "P"
        best = None; best_diff = None
        for p in parents:
            if p.right != right: continue
            pts = p.submitted_at
            if pts.tzinfo is None: pts = pts.replace(tzinfo=timezone.utc)
            diff = (pts - ts).total_seconds()
            if 0 <= diff <= 60:
                if best_diff is None or diff < best_diff:
                    best = p; best_diff = diff
        return best

    # Bucket by fire_order
    by_order: dict[int, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for sg in sigs:
        fo = (sg.extras or {}).get("fire_order")
        if fo is None or fo == 0:
            continue
        p = find_parent(sg)
        if p is None:
            continue
        key = (p.symbol, p.strike, p.right, p.expiry)
        ppnl = contract_pnl.get(key)
        if ppnl is None: continue
        # Share P/L equally among all parents that fired on this contract
        n_parents = sum(1 for x in parents if (x.symbol, x.strike, x.right, x.expiry) == key)
        share = ppnl / max(n_parents, 1)
        # Cap bucket at 4+ to avoid n=1 buckets
        bucket = fo if fo < 4 else 4
        slot = by_order[bucket]
        slot["n"] += 1
        if share > 0: slot["wins"] += 1
        elif share < 0: slot["losses"] += 1
        slot["pnl"] += share

    out_buckets = []
    for k in sorted(by_order.keys()):
        v = by_order[k]
        label = f"{k}" if k < 4 else "4+"
        out_buckets.append({
            "fire_order": label,
            "n": v["n"],
            "wins": v["wins"],
            "losses": v["losses"],
            "win_rate_pct": round(v["wins"] / max(v["n"], 1) * 100, 1),
            "total_pnl": round(v["pnl"], 2),
            "avg_pnl": round(v["pnl"] / max(v["n"], 1), 2),
        })

    return {
        "session_date": date,
        "buckets": out_buckets,
        "note": "fire_order = position in cluster of (direction × strike) within a 5-min window. "
                "1 = first fire, 2+ = stacks. Compare WR / avg_pnl across buckets to see if "
                "stacks add value over first-fires.",
    }


@router.post("/diag/fire_order_backfill")
async def diag_fire_order_backfill(session_date: str | None = None) -> dict[str, Any]:
    """One-time / retroactive: stamp fire_order into existing SignalIter.extras."""
    from fef_sidecar.engine.fire_order import backfill_fire_order
    return await backfill_fire_order(session_date=session_date)


@router.get("/diag/realistic_fill_replay")
async def diag_realistic_fill_replay(session_date: str | None = None) -> dict[str, Any]:
    """Replay paper trades under haircutted fill assumptions (spread cross +
    latency drag + commissions). Tells you "if my edge is execution +
    stacking, what's left when paper-flattering effects are removed?"
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from fef_sidecar.engine.realistic_fill import replay_session
    date = session_date or _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")
    return await replay_session(date)


@router.get("/diag/edge_decomposition")
async def diag_edge_decomposition(
    session_dates: str | None = None,
    realistic_fill_haircut_pct: float = 0.20,
) -> dict[str, Any]:
    """Shapley-style decomposition of paper edge into signal + execution +
    stacking + interaction components. Each component has a different
    live-survival rate, so the decomposition predicts what fraction of
    paper edge will survive the paper-to-live transition.

    Run ideally on the full 10-session freeze dataset post-freeze. Earlier
    runs are directionally useful but estimates are noisy.

    Args:
        session_dates: comma-separated YYYY-MM-DD list (default: today only)
        realistic_fill_haircut_pct: 0.20 = 20% haircut (moderate scenario)
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from fef_sidecar.engine.shapley_decomposition import run_decomposition
    if session_dates:
        dates = [d.strip() for d in session_dates.split(",") if d.strip()]
    else:
        dates = [_dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")]
    return await run_decomposition(dates, realistic_fill_haircut_pct=realistic_fill_haircut_pct)


@router.get("/diag/realistic_fill_history")
async def diag_realistic_fill_history(days: int = 7) -> dict[str, Any]:
    """Per-session realistic-fill replay history. Tracks haircut % over time."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from fef_sidecar.engine.realistic_fill import replay_all_sessions
    et = _tz(_td(hours=-4))
    today = _dt.now(et)
    dates = [(today - _td(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    return await replay_all_sessions(dates)


@router.get("/diag/counterfactual_cooldown")
async def diag_counterfactual_cooldown(
    session_date: str | None = None,
    cooldowns: str = "60,180,300,600",
) -> dict[str, Any]:
    """Replay today's actual signals through different per-symbol cooldown
    rules. Lets you test "what if I used 300s instead of 60s?" without
    actually changing the auto-trader.

    For each cooldown value, the simulator filters which actual signals
    would have made it through that cooldown gate, then simulates trade
    outcomes via 1-min SPX bars.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from fef_sidecar.engine.counterfactual import run_cooldown_simulation
    date = session_date or _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")
    cd_list = [int(x.strip()) for x in cooldowns.split(",") if x.strip().isdigit()]
    return await run_cooldown_simulation(date, cd_list or [60, 180, 300, 600])


@router.get("/diag/session_pnl_estimate")
async def diag_session_pnl_estimate(request: Request) -> dict[str, Any]:
    """Compute today's session P/L from persisted IBKR executions.

    Preferred source (when present): sum of (SLD value - BOT value) × $100
    across all IBKRExecution rows for today, scoped by contract so partial
    fills net correctly even when buys and sells span multiple bars. This
    matches the per-date number shown in /diag/ibkr_executions_persisted
    (the +$6,780 today). Returns it as `estimated_session_pnl_usd` so the
    TopBar pill picks it up unchanged.

    Fallback (no executions persisted yet): coarse LiveOrder R-weighted
    estimate — kept so the pill still shows *something* on a freshly
    restarted sidecar before the execution write-through fires. The
    response includes a `source` field so callers can tell which path
    produced the number.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import IBKRExecution, LiveOrder
    from sqlmodel import select as _s
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    today_et = _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")

    async with _gs() as session:
        # Primary: persisted IBKR executions for today
        exec_rows = list((await session.exec(
            _s(IBKRExecution).where(IBKRExecution.session_date == today_et)
        )).all())

    if exec_rows:
        # Net BOT vs SLD value per contract; sum × 100 = realized $ PnL.
        # Contracts where BOT qty ≠ SLD qty still have open exposure; we
        # only count the closed leg (min(bot_q, sld_q)) at avg prices so a
        # half-closed contract contributes its realized portion.
        by_contract: dict[str, dict[str, float]] = {}
        for r in exec_rows:
            key = f"{r.symbol} {r.strike}{r.right} {r.expiry}"
            slot = by_contract.setdefault(key, {"bot_v": 0.0, "sld_v": 0.0, "bot_q": 0.0, "sld_q": 0.0})
            if r.side == "BOT":
                slot["bot_v"] += r.shares * r.price
                slot["bot_q"] += r.shares
            elif r.side == "SLD":
                slot["sld_v"] += r.shares * r.price
                slot["sld_q"] += r.shares

        total_pnl = 0.0
        open_exposure_contracts = 0
        for key, slot in by_contract.items():
            bot_q = slot["bot_q"]
            sld_q = slot["sld_q"]
            if bot_q == 0 and sld_q == 0:
                continue
            if bot_q != sld_q:
                open_exposure_contracts += 1
            # Realized PnL on the closed leg: pair off min(bot_q, sld_q)
            # using avg prices on each side.
            paired = min(bot_q, sld_q)
            if paired <= 0:
                continue
            avg_bot = slot["bot_v"] / bot_q if bot_q > 0 else 0.0
            avg_sld = slot["sld_v"] / sld_q if sld_q > 0 else 0.0
            total_pnl += (avg_sld - avg_bot) * paired * 100.0

        return {
            "session_date": today_et,
            "source": "ibkr_executions",
            "fills": len(exec_rows),
            "contracts_touched": len(by_contract),
            "open_exposure_contracts": open_exposure_contracts,
            "estimated_session_pnl_usd": round(total_pnl, 0),
            "note": "realized P/L from persisted IBKR executions, paired by contract",
        }

    # Fallback path — no executions persisted yet
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
        # Risk per contract = (entry option price - stop option price) × $100.
        # We don't track stop option prices directly; approximate as 50% of
        # entry premium (mirrors the typical 50% trail-stop the engine sets).
        per_contract_risk = max((p.limit_price or 1.0) * 0.5, 0.5) * 100
        if p.status == "CLOSED":
            est_total += per_contract_risk * p.qty * 1.1
        elif p.status == "STOPPED":
            est_total -= per_contract_risk * p.qty * 1.0
        elif p.status == "PARTIAL":
            est_total += per_contract_risk * p.qty * 0.4

    return {
        "session_date": today_et,
        "source": "liveorder_fallback",
        "parents_count": len(today_parents),
        "by_status": by_status,
        "estimated_session_pnl_usd": round(est_total, 0),
        "note": "fallback estimate — no persisted IBKR executions yet for today",
    }


@router.get("/history/hft")
async def history_hft(symbol: str = "SPX", from_ts_ms: int | None = None,
                       to_ts_ms: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """Return HFTSample rows. Filter by symbol + timestamp range.
    Used by ml_patterns + analytical queries to correlate HFT state
    with trade outcomes.
    """
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import HFTSample
    from sqlmodel import select as _s
    async with _gs() as session:
        stmt = _s(HFTSample).where(HFTSample.symbol == symbol)
        if from_ts_ms is not None:
            stmt = stmt.where(HFTSample.timestamp_ms >= from_ts_ms)
        if to_ts_ms is not None:
            stmt = stmt.where(HFTSample.timestamp_ms <= to_ts_ms)
        stmt = stmt.order_by(HFTSample.timestamp_ms.desc()).limit(limit)  # type: ignore[attr-defined]
        rows = list((await session.exec(stmt)).all())
    return [
        {"id": r.id, "symbol": r.symbol, "timestamp_ms": r.timestamp_ms,
         "short_vol_pts": r.short_vol_pts, "long_vol_pts": r.long_vol_pts,
         "vol_ratio": r.vol_ratio, "vol_regime": r.vol_regime,
         "cancel_to_trade_ratio": r.cancel_to_trade_ratio,
         "quote_intensity_per_sec": r.quote_intensity_per_sec,
         "stuffing_severity": r.stuffing_severity,
         "book_imbalance": r.book_imbalance,
         "book_imbalance_5lvl": r.book_imbalance_5lvl,
         "book_direction": r.book_direction,
         "book_confidence": r.book_confidence,
         "weighted_mid_price": r.weighted_mid_price,
         "book_spread": r.book_spread,
         "composite_direction": r.composite_direction,
         "vol_source": r.vol_source,
         "stuffing_source": r.stuffing_source,
         "depth_source": r.depth_source}
        for r in rows
    ]


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

    # 3. Market-sell all open option positions. Same routing-bug fix as
    # kill_all_orders — must rebuild + qualify the contract before
    # placing, otherwise IBKR silently rejects.
    #
    # SELL paths route through safe_market_sell — structurally impossible
    # to naked-short.
    sells = []
    if isinstance(s.ibkr, LiveIBKRProvider) and s.ibkr._ib is not None:  # type: ignore[attr-defined]
        ib = s.ibkr._ib  # type: ignore[attr-defined]
        if ib.isConnected():
            try:
                from ib_async import MarketOrder
                from fef_sidecar.ibkr.live import _to_ib_contract
                from fef_sidecar.ibkr.models import Contract, ContractKind
                from fef_sidecar.ibkr.safe_sell import safe_market_sell
                for p in ib.positions():
                    if getattr(p.contract, "secType", "") != "OPT": continue
                    qty = int(p.position)
                    if abs(qty) < 1: continue
                    action = "SELL" if qty > 0 else "BUY"
                    sym = getattr(p.contract, "symbol", "")
                    strike = float(getattr(p.contract, "strike", 0))
                    right = getattr(p.contract, "right", "")
                    expiry = getattr(p.contract, "lastTradeDateOrContractMonth", "")
                    try:
                        opt_contract = Contract(
                            symbol=sym, kind=ContractKind.OPTION,
                            exchange="CBOE" if sym == "SPX" else "SMART",
                            expiry=expiry, strike=strike, right=right,
                        )
                        ic = _to_ib_contract(opt_contract)
                        await ib.qualifyContractsAsync(ic)
                        if action == "SELL":
                            res = await safe_market_sell(
                                ib, ic, abs(qty), purpose="panic_flat", state=s,
                            )
                            if res.placed:
                                sells.append({
                                    "contract": f"{sym} {strike}{right}",
                                    "qty": res.sell_qty, "action": "SELL",
                                    "ib_order_id": res.trade.order.orderId,
                                })
                            else:
                                sells.append({
                                    "contract": f"{sym} {strike}{right}",
                                    "skipped": True, "reason": res.reason,
                                })
                        else:
                            order = MarketOrder("BUY", abs(qty), transmit=True)
                            t = ib.placeOrder(ic, order)
                            sells.append({
                                "contract": f"{sym} {strike}{right}",
                                "qty": abs(qty), "action": "BUY",
                                "ib_order_id": t.order.orderId,
                            })
                    except Exception as e:
                        sells.append({"contract": f"{sym} {strike}{right}", "error": str(e)})
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

    Built via the CANONICAL engine factory (state._make_engine) so the shadow
    honors FEF_FLOW_SURGE_GATE_ENABLED + surge tuning + drift-sanity ceiling
    exactly like the production engine — previously a bare FEFEngine() left
    flow_surge_gate_enabled at its constructor default (True), so shadows
    silently ran the retired flow-surge path. The 4 body params overlay the
    env-derived config (same private-attr hot-reload style as POST /config).
    """
    from fef_sidecar.state import _make_engine
    s = _state(request)
    s.shadow_label = body.get("label", "shadow")
    engine = _make_engine()
    if "wall_proximity_pts" in body:
        engine._wall_proximity_pts = float(body["wall_proximity_pts"])  # type: ignore[attr-defined]
        # Explicitly shadowing the proximity knob = opting into the gate
        # (the live default is OFF per the 2026-07-02 wall validation).
        engine._wall_proximity_skip_enabled = True  # type: ignore[attr-defined]
    if "wall_collapse_pct" in body:
        engine._wall_collapse_pct = float(body["wall_collapse_pct"])  # type: ignore[attr-defined]
    if "wall_collapse_floor_m" in body:
        engine._wall_collapse_floor_m = float(body["wall_collapse_floor_m"])  # type: ignore[attr-defined]
    if "absorption_fe_ratio_min" in body:
        engine._absorption_fe_min = float(body["absorption_fe_ratio_min"])  # type: ignore[attr-defined]
    s.shadow_engine = engine
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


@router.post("/admin/backfill_live_order_fills")
async def admin_backfill_live_order_fills(
    body: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """Backfill live_order.filled_qty + avg_fill_price + status from
    ibkr_execution rows (matched by ib_order_id). Read-only fix for the
    long-standing gap where parent order rows never had their fill data
    populated, so UI / research scripts showed filled_qty=0 on rows
    that actually filled.

    Safe: only updates fill-tracking fields on existing rows; doesn't
    touch the live order placement / callback path. Idempotent.

    Body: {session_date?: 'YYYY-MM-DD'} — restrict to one date, or omit
    for all-time."""
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import IBKRExecution, LiveOrder
    from sqlmodel import select as _s

    session_date = body.get("session_date")
    async with _gs() as _ss:
        # Gather all execs and group by ib_order_id
        execs_q = _s(IBKRExecution)
        if session_date:
            execs_q = execs_q.where(IBKRExecution.session_date == session_date)
        execs = list((await _ss.exec(execs_q)).all())
        by_order: dict[int, list] = {}
        for e in execs:
            if e.ib_order_id is None:
                continue
            by_order.setdefault(int(e.ib_order_id), []).append(e)

        # Gather candidate live_orders. Restrict by date if requested.
        lo_q = _s(LiveOrder)
        candidates = list((await _ss.exec(lo_q)).all())
        if session_date:
            candidates = [
                p for p in candidates
                if p.submitted_at and p.submitted_at.strftime("%Y-%m-%d") == session_date
            ]

        updated = 0
        skipped_no_match = 0
        skipped_already_filled = 0
        for p in candidates:
            if p.ib_order_id is None:
                continue
            fills = by_order.get(int(p.ib_order_id))
            if not fills:
                skipped_no_match += 1
                continue
            total_shares = sum(float(f.shares or 0) for f in fills)
            if total_shares <= 0:
                skipped_no_match += 1
                continue
            weighted_px = sum(float(f.shares or 0) * float(f.price or 0) for f in fills) / total_shares
            # Idempotency: only write if anything actually changes
            new_filled = int(round(total_shares))
            new_px = round(weighted_px, 4)
            if (p.filled_qty == new_filled
                    and p.avg_fill_price is not None
                    and abs((p.avg_fill_price or 0) - new_px) < 0.005):
                skipped_already_filled += 1
                continue
            p.filled_qty = new_filled
            p.avg_fill_price = new_px
            # Update status to a sane value if it was still in pre-fill state
            if p.status in ("SUBMITTED", "ACCEPTED", "WORKING", None):
                qty_target = int(p.qty or 0)
                if qty_target > 0 and new_filled >= qty_target:
                    p.status = "FILLED"
                elif new_filled > 0:
                    p.status = "PARTIAL"
            _ss.add(p)
            updated += 1
        await _ss.commit()

    return {
        "ok": True,
        "session_date": session_date or "all",
        "live_orders_updated": updated,
        "skipped_no_matching_exec": skipped_no_match,
        "skipped_already_filled": skipped_already_filled,
        "exec_rows_scanned": len(execs),
    }


@router.post("/admin/backfill_commissions")
async def admin_backfill_commissions(
    request: Request, body: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """Backfill ibkr_execution.commission + realized_pnl from IBKR's
    CommissionReport. Calls reqExecutionsAsync (~7-day server cache) and
    UPDATEs existing rows by exec_id where commission is currently NULL.

    Idempotent. Safe to run repeatedly. Doesn't insert new rows — that's
    /diag/ibkr_executions's job. Only fills in the commission/pnl fields
    on rows that were captured before the 2026-05-21 commission-capture
    write-path fix.

    Paper accounts (DUM prefix) typically have $0.00 commissions. NULL ->
    $0 after backfill is the expected case for the existing DUM098107 data.

    Body (optional): {"days": int} — limits server-side reqExecutions
    lookback. Default 30 (IBKR caps at ~7 days regardless)."""
    from fef_sidecar.db.engine import get_session as _gs
    from fef_sidecar.db.models import IBKRExecution
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from sqlmodel import select as _s

    s = _state(request)
    if not isinstance(s.ibkr, LiveIBKRProvider):
        return {"ok": False, "error": "IBKR provider is not the live one — cannot fetch fills"}
    ib = s.ibkr._ib  # type: ignore[attr-defined]
    if ib is None:
        return {"ok": False, "error": "IBKR not connected"}

    from ib_async import ExecutionFilter
    try:
        fills = await ib.reqExecutionsAsync(ExecutionFilter())
    except Exception as e:
        return {"ok": False, "error": f"reqExecutionsAsync failed: {e}"}

    # CRITICAL: ib_async's reqExecutionsAsync resolves the future with Fill
    # objects whose commissionReport is empty — commissionReport events
    # arrive on a SEPARATE TWS event channel after execDetails. The Fill
    # objects in the returned list are the SAME objects as wrapper.fills
    # (mutated in place by dataclassUpdate when commissionReport events
    # land). So we wait a moment after reqExecutionsAsync returns to let
    # those events flow, then re-read fill.commissionReport.commission.
    # Reference: ib_async wrapper.py:824, 848-861.
    import asyncio as _aio_comm, os as _os_comm
    await _aio_comm.sleep(float(_os_comm.environ.get("FEF_COMMISSION_RACE_WAIT_SEC", "2.0")))

    # exec_id -> (commission, realized_pnl)
    pairs: dict[str, tuple[float | None, float | None]] = {}
    for f in fills:
        ex = f.execution
        if not getattr(ex, "execId", None):
            continue
        cr = getattr(f, "commissionReport", None)
        comm = None
        rpnl = None
        if cr is not None:
            _c = getattr(cr, "commission", None)
            if _c is not None and not (isinstance(_c, float) and _c != _c):
                try: comm = float(_c)
                except (TypeError, ValueError): pass
            _r = getattr(cr, "realizedPNL", None)
            if _r is not None and not (isinstance(_r, float) and _r != _r):
                try: rpnl = float(_r)
                except (TypeError, ValueError): pass
        pairs[ex.execId] = (comm, rpnl)

    updated = 0
    skipped_no_match = 0
    skipped_already_set = 0
    async with _gs() as session:
        rows = list((await session.exec(_s(IBKRExecution))).all())
        by_id = {r.exec_id: r for r in rows}
        for exec_id, (comm, rpnl) in pairs.items():
            row = by_id.get(exec_id)
            if row is None:
                skipped_no_match += 1
                continue
            changed = False
            if row.commission is None and comm is not None:
                row.commission = comm
                changed = True
            if row.realized_pnl is None and rpnl is not None:
                row.realized_pnl = rpnl
                changed = True
            if changed:
                session.add(row)
                updated += 1
            else:
                skipped_already_set += 1
        await session.commit()

    return {
        "ok": True,
        "ibkr_fills_returned": len(fills),
        "exec_ids_with_commission_data": sum(1 for c, _ in pairs.values() if c is not None),
        "db_rows_updated": updated,
        "db_rows_already_set": skipped_already_set,
        "db_rows_no_match": skipped_no_match,
        "note": "Paper accounts (DUM prefix) typically report $0.00 commission — that's still a valid backfill",
    }


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
        summary=f"Reset auto-trade session counters (was {old} fires, per-symbol cap {s.auto_trade_max_per_symbol})",
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
async def analytics_attribution(session_date: str | None = None) -> dict[str, Any]:
    """P/L attribution by direction/confidence/hour.

    Args:
        session_date: YYYY-MM-DD ET to filter to one session.
                      Default None = all-time.
    """
    from fef_sidecar.engine.perf_attribution import attribution
    result = await attribution(session_date=session_date)
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

    # AUDIT TRAIL (2026-07-08 review F4): POST /config is now the canonical
    # change path for ENFORCED policy (auto_trade_confidence_floor,
    # auto_trade_qty_override, cross_symbol_cap_enabled) — every effective
    # change writes one audit row listing key: old -> new. Generic across all
    # keys (cheap, and the other gates' knobs deserve the same trace), so the
    # persistent qty size is covered here with no key-specific code. Never
    # blocks the save. Invalid values (qty 0 / >100) fail model_validate above
    # and are rejected before persistence (same convention as the floor).
    try:
        before = current.model_dump()
        after = merged.model_dump()
        changed = {
            k: {"old": before.get(k), "new": after.get(k)}
            for k in updates
            if before.get(k) != after.get(k)
        }
        if changed:
            from fef_sidecar.db.repository import SignalIterRepository
            await SignalIterRepository.write_audit_log(
                category="config", actor="user",
                summary="CONFIG CHANGED: " + "; ".join(
                    f"{k}: {v['old']} -> {v['new']}" for k, v in sorted(changed.items())
                ),
                details={"changed": changed},
            )
    except Exception:
        logger.exception("config-change audit write failed (config saved)")

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
        status_value = ticket.status.value

    # Auto-retrain debounce — when a ticket finalizes, schedule a retrain.
    # The trigger function dedups concurrent calls so rapid-fire closes
    # don't kick off multiple trains.
    if status_value in ("CLOSED", "STOPPED", "PARTIAL"):
        try:
            from fef_sidecar.engine.ml_autoretrain import schedule_retrain
            schedule_retrain(_state(request))
        except Exception:
            logger.exception("Auto-retrain scheduling failed (ticket %s)", ticket_id)

    return {"ok": True, "ticket_id": ticket_id, "status": status_value}


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


# Chart pattern overlay — markers / measured-move targets / trendlines for
# ChartPane. Computed from PERSISTED bars (historical_bar, read-only) rather
# than a fresh IBKR fetch so polling this endpoint adds zero IBKR load on
# top of the chart's own /bars polling. Live capture persists 1-min bars for
# the active symbol; other timeframes are resampled server-side from those,
# aligned to the same wall-clock boundaries IBKR uses for intraday bars.
_PATTERN_BAR_MINUTES = {
    "1 min": 1, "2 mins": 2, "5 mins": 5, "15 mins": 15, "30 mins": 30, "1 hour": 60,
}


@router.get("/chart/pattern_overlay")
async def chart_pattern_overlay(
    request: Request,
    symbol: str = "SPX",
    bar_size: str = "1 min",
    lookback: int = 400,
    doji: bool = False,
) -> dict[str, Any]:
    """Pattern overlay payload for the chart.

    Returns {markers, target_lines, flag_zones, trendlines, bar_count, status}.
    Marker/line times are epoch SECONDS (lightweight-charts native).
    Thresholds come from UIConfig pattern_* fields — the same source of
    truth the live pattern trigger uses. `doji=true` adds doji markers
    (off by default, noisy)."""
    from fef_sidecar.engine.pattern_overlay import build_overlay, resample_bars
    from fef_sidecar.engine.pattern_trigger import overlay_config_from_ui

    s = _state(request)
    sym = symbol.upper()
    lookback = max(50, min(int(lookback), 2000))
    minutes = _PATTERN_BAR_MINUTES.get(bar_size)
    empty = {"markers": [], "target_lines": [], "flag_zones": [], "trendlines": []}
    if minutes is None:
        return {**empty, "bar_count": 0,
                "status": f"pattern overlay unsupported for bar_size {bar_size!r}"}

    if type(s.ibkr).__name__ == "MockIBKRProvider":
        # Mirror /bars' mock branch — coherent PriceTrack so markers line up
        # with the candles the chart is showing in dev mode.
        from fef_sidecar.mock import get_track
        track = get_track()
        bars = track.build_bars(bar_size_minutes=minutes)
        if sym == "QQQ":
            tf = lambda v: round(v / 14.5, 2)
        elif sym == "IWM":
            tf = lambda v: round(v / 33.0, 2)
        elif sym == "SPY":
            tf = lambda v: round(v / 10.0, 2)
        elif sym == "ES":
            tf = lambda v: round(v - 5.0, 2)
        else:
            tf = lambda v: v
        window = [
            {"timestamp_ms": b["timestamp_ms"], "open": tf(b["open"]), "high": tf(b["high"]),
             "low": tf(b["low"]), "close": tf(b["close"]), "volume": b["volume"]}
            for b in bars
        ][-lookback:]
        source = "mock_track"
    else:
        rows = await SignalIterRepository.query_recent_bars(
            sym, "1 min", limit=lookback * minutes,
        )
        base = [
            {"timestamp_ms": r.timestamp_ms, "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "volume": r.volume}
            for r in rows
        ]
        if not base and minutes > 1:
            # No live-captured 1-min rows (e.g. non-active symbol) — try bars
            # persisted directly at the requested size (backfills).
            rows = await SignalIterRepository.query_recent_bars(sym, bar_size, limit=lookback)
            base = [
                {"timestamp_ms": r.timestamp_ms, "open": r.open, "high": r.high,
                 "low": r.low, "close": r.close, "volume": r.volume}
                for r in rows
            ]
            window = base[-lookback:]
        else:
            window = resample_bars(base, minutes)[-lookback:]
        source = "historical_bar"
        if not window:
            return {**empty, "bar_count": 0,
                    "status": f"no persisted bars for {sym} — pattern overlay needs "
                              f"live 1-min capture (active symbol) or a backfill"}

    cfg = overlay_config_from_ui(load_config(), include_doji=doji)
    payload = build_overlay(window, cfg)
    return {**payload, "bar_count": len(window), "status": "ok", "source": source}


@router.get("/chart/regression_channel")
async def chart_regression_channel(
    request: Request,
    symbol: str = "SPX",
    bar_size: str = "1 day",
    duration: str = "6 M",
    dev: float = 2.0,
) -> dict[str, Any]:
    """Regression (centered-OLS StdDev) channel overlay for the chart.

    Fetches bars at (bar_size, duration) via the shared cached bar-fetch
    (the SAME helper the spread recommender uses — IBKR serves any
    bar_size/duration on demand, no new data source), runs the reused
    engine.technical.stddev_channel, and returns the center line plus band
    arrays as {time, value} points. `time` is epoch SECONDS, matching the
    /chart/pattern_overlay convention.

    Powers both chart modes: (A) chart-following — bar_size = the chart's
    timeframe, duration = the user's lookback; (B) independent overlay —
    caller-chosen (bar_size, duration) decoupled from the chart timeframe.

    Bands: fixed +/-1 sigma /+/-2 sigma /+/-3 sigma PLUS a custom +/-dev*sigma
    pair (upper_dev/lower_dev) so the frontend can draw the user's chosen
    deviation multiplier. `dev` defaults to 2.0 (Dale's 2-sigma setup).

    Fail-soft: returns empty arrays + a status string on any fetch/parse
    failure (never raises), like /chart/pattern_overlay.

    DISPLAY ONLY — read-only bar fetch; does NOT touch the auto-trade /
    scanner / watcher / exit path.
    """
    from fef_sidecar.engine.technical import stddev_channel

    s = _state(request)
    sym = symbol.upper()
    try:
        dev_mult = float(dev)
    except (TypeError, ValueError):
        dev_mult = 2.0
    if not (dev_mult > 0):
        dev_mult = 2.0

    empty = {
        "center": [], "upper_1": [], "upper_2": [], "upper_3": [],
        "lower_1": [], "lower_2": [], "lower_3": [],
        "upper_dev": [], "lower_dev": [],
    }

    def _fail(status: str) -> dict[str, Any]:
        return {**empty, "dev": dev_mult, "bar_size": bar_size,
                "duration": duration, "bar_count": 0, "status": status}

    try:
        bars = await _get_cached_bars(s, sym, bar_size, duration)
    except Exception:
        logger.exception(
            "regression_channel bar fetch failed for %s (%s / %s)", sym, bar_size, duration
        )
        return _fail("bar fetch failed")
    if not bars:
        return _fail(f"no bars for {sym} ({bar_size} / {duration})")

    # Bars are ibkr.models.Bar — need epoch-second timestamps + closes.
    # Sort ascending + dedupe on time (defense: the frontend's setData
    # requires strictly ascending, distinct times).
    dedup: dict[int, float] = {}
    for b in bars:
        try:
            t = int(b.timestamp.timestamp())
            dedup[t] = float(b.close)
        except Exception:
            continue
    if not dedup:
        return _fail("bars had no usable timestamps/closes")
    times = sorted(dedup)
    closes = [dedup[t] for t in times]

    channel = stddev_channel(closes)

    def _series(getter) -> list[dict[str, float]]:
        return [{"time": times[i], "value": round(getter(channel[i]), 4)}
                for i in range(len(times))]

    # sigma_i = center-to-1-sigma distance (stddev_channel: upper_1 = center + sigma).
    return {
        "center": _series(lambda p: p.center),
        "upper_1": _series(lambda p: p.upper_1),
        "upper_2": _series(lambda p: p.upper_2),
        "upper_3": _series(lambda p: p.upper_3),
        "lower_1": _series(lambda p: p.lower_1),
        "lower_2": _series(lambda p: p.lower_2),
        "lower_3": _series(lambda p: p.lower_3),
        "upper_dev": _series(lambda p: p.center + dev_mult * (p.upper_1 - p.center)),
        "lower_dev": _series(lambda p: p.center - dev_mult * (p.upper_1 - p.center)),
        "dev": dev_mult,
        "bar_size": bar_size,
        "duration": duration,
        "bar_count": len(times),
        "status": "ok",
    }


@router.get("/chart/tick_candles")
async def chart_tick_candles(
    request: Request,
    symbol: str = "ES",
    n: int = 1000,
) -> dict[str, Any]:
    """N-tick candles (default ES 1000T) aggregated from the tick-by-tick
    AllLast trade stream the microstructure loop already holds. Returns
    {candles, status, n}; candles carry timestamp_ms like /bars. When no
    trade stream is live for the symbol, candles=[] and status.detail says
    why (chart shows 'no tick feed')."""
    s = _state(request)
    svc = s.tick_candles
    if int(n) not in svc.tick_sizes:
        raise HTTPException(400, f"n must be one of {list(svc.tick_sizes)}")
    return {
        "candles": svc.get_candles(symbol, int(n)),
        "status": svc.status(symbol),
        "n": int(n),
    }


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
