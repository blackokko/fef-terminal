"""MCP server — exposes FEF historical data + pre-baked analyses as tools
callable from Claude Code (or any MCP client).

Run: `fef-mcp` (after pip install -e .)
Wire to Claude Code: add to mcp.json — see scripts/claude-mcp-config.json

All tools are read-only against the same SQLite DB the sidecar writes to.
The MCP server doesn't need the sidecar to be running — analyses work
on whatever historical data is already persisted.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlmodel import Session, create_engine, select

logger = logging.getLogger(__name__)

mcp = FastMCP("fef-quant")


# ---------------------------------------------------------------------------
# DB connection — sync sqlmodel session against the sidecar's SQLite file
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    """Locate the sidecar's SQLite file.

    Order: FEF_DB_PATH env var → sidecar's default at ~/.fef-terminal/signal_log.db
    (mirrors fef_sidecar.db.engine.DEFAULT_DB_PATH).
    """
    env = os.environ.get("FEF_DB_PATH")
    if env:
        return Path(env)
    return Path.home() / ".fef-terminal" / "signal_log.db"


_engine = None


def _get_session() -> Session:
    global _engine
    if _engine is None:
        path = _db_path()
        if not path.exists():
            raise FileNotFoundError(
                f"Sidecar DB not found at {path}. Set FEF_DB_PATH env or run the "
                "sidecar at least once to create it."
            )
        _engine = create_engine(f"sqlite:///{path}", echo=False)
    return Session(_engine)


# ---------------------------------------------------------------------------
# Data access tools (5)
# ---------------------------------------------------------------------------


@mcp.tool()
def get_signals(
    from_date: str | None = None,
    to_date: str | None = None,
    direction: str | None = None,
    confidence: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Query historical FEF signal iterations.

    Args:
        from_date: YYYY-MM-DD, inclusive lower bound on session_date
        to_date: YYYY-MM-DD, inclusive upper bound on session_date
        direction: 'LONG_CALLS' | 'LONG_PUTS' | 'WAIT'
        confidence: 'HIGH' | 'MEDIUM' | 'LOW' | 'NONE'
        limit: max rows (default 200)

    Returns each signal with full FEF context: drift, walls (top 5 GEX +
    DEX), max pain, aggression, IVR, verdict reasoning, and the 'extras'
    JSON dict (delta_price, delta_drift_m, fe_ratio, etc.).
    """
    from fef_sidecar.db.models import SignalIter

    with _get_session() as session:
        stmt = select(SignalIter).order_by(SignalIter.timestamp.desc()).limit(limit)  # type: ignore[attr-defined]
        if from_date:
            stmt = stmt.where(SignalIter.session_date >= from_date)
        if to_date:
            stmt = stmt.where(SignalIter.session_date <= to_date)
        if direction:
            stmt = stmt.where(SignalIter.direction == direction)
        if confidence:
            stmt = stmt.where(SignalIter.confidence == confidence)
        rows = session.exec(stmt).all()
    return [
        {
            "id": r.id, "iter_num": r.iter_num, "session_date": r.session_date,
            "timestamp": r.timestamp.isoformat(), "spx_price": r.spx_price,
            "direction": r.direction.value, "confidence": r.confidence.value,
            "reasoning": r.reasoning,
            "drift_cum_net_m": r.drift_cum_net_m, "drift_bias": r.drift_bias,
            "ntm_max_pain": r.ntm_max_pain, "raw_max_pain": r.raw_max_pain,
            "top_gex_walls": r.top_gex_walls, "top_dex_walls": r.top_dex_walls,
            "aggression_call_pct": r.aggression_call_pct,
            "aggression_put_pct": r.aggression_put_pct,
            "iv_rank_call_pct": r.iv_rank_call_pct,
            "iv_rank_put_pct": r.iv_rank_put_pct,
            "extras": r.extras,
        }
        for r in rows
    ]


@mcp.tool()
def get_trades(
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    won: bool | None = None,
    signal_iter_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Query historical TradeTicket records (orders the user has copied/closed).

    Args:
        status: 'DRAFT' | 'COPIED' | 'FILLED' | 'PARTIAL' | 'CLOSED' | 'STOPPED' | 'SKIPPED'
        from_date / to_date: filter by created_at date (YYYY-MM-DD)
        won: True = pnl > 0 only, False = pnl <= 0 only
        signal_iter_id: only trades attached to this signal
        limit: max rows (default 200)
    """
    from fef_sidecar.db.models import TradeTicket

    with _get_session() as session:
        stmt = select(TradeTicket).order_by(TradeTicket.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
        if status:
            stmt = stmt.where(TradeTicket.status == status)
        if signal_iter_id is not None:
            stmt = stmt.where(TradeTicket.signal_iter_id == signal_iter_id)
        rows = list(session.exec(stmt).all())
        if from_date:
            rows = [t for t in rows if t.created_at.strftime("%Y-%m-%d") >= from_date]
        if to_date:
            rows = [t for t in rows if t.created_at.strftime("%Y-%m-%d") <= to_date]
        if won is True:
            rows = [t for t in rows if (t.pnl_dollars or 0) > 0]
        elif won is False:
            rows = [t for t in rows if (t.pnl_dollars or 0) <= 0]
    return [
        {
            "id": t.id, "signal_iter_id": t.signal_iter_id,
            "created_at": t.created_at.isoformat(), "direction": t.direction.value,
            "contract_strike": t.contract_strike, "contract_expiry": t.contract_expiry,
            "qty": t.qty, "entry_limit": t.entry_limit,
            "t1": t.t1, "t2": t.t2, "t3": t.t3, "stop": t.stop,
            "status": t.status.value,
            "filled_at": t.filled_at.isoformat() if t.filled_at else None,
            "fill_price": t.fill_price,
            "exit_t1_price": t.exit_t1_price, "exit_t2_price": t.exit_t2_price,
            "exit_t3_price": t.exit_t3_price, "exit_stop_price": t.exit_stop_price,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            "pnl_dollars": t.pnl_dollars, "notes": t.notes,
        }
        for t in rows
    ]


@mcp.tool()
def get_bars(
    symbol: str = "SPX",
    bar_size: str = "1 min",
    from_ts_ms: int | None = None,
    to_ts_ms: int | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Query persisted OHLCV bars.

    Args:
        symbol: SPX / SPY / ES / NQ / etc.
        bar_size: '1 min' / '5 mins' / etc.
        from_ts_ms / to_ts_ms: UTC millisecond bounds
        limit: max rows (default 1000)
    """
    from fef_sidecar.db.models import HistoricalBar

    with _get_session() as session:
        stmt = (
            select(HistoricalBar)
            .where(HistoricalBar.symbol == symbol.upper())
            .where(HistoricalBar.bar_size == bar_size)
            .order_by(HistoricalBar.timestamp_ms.asc())  # type: ignore[attr-defined]
            .limit(limit)
        )
        if from_ts_ms is not None:
            stmt = stmt.where(HistoricalBar.timestamp_ms >= from_ts_ms)
        if to_ts_ms is not None:
            stmt = stmt.where(HistoricalBar.timestamp_ms <= to_ts_ms)
        rows = session.exec(stmt).all()
    return [
        {"timestamp_ms": r.timestamp_ms, "open": r.open, "high": r.high,
         "low": r.low, "close": r.close, "volume": r.volume}
        for r in rows
    ]


@mcp.tool()
def get_flow_snapshot_at(
    timestamp_ms: int,
    symbol: str = "SPX",
    tolerance_seconds: int = 30,
) -> dict[str, Any] | None:
    """Get the QuantData flow snapshot closest to a given timestamp.

    Useful for: 'what did the order flow look like at the moment of this
    trade entry?' — find the closest snapshot within tolerance_seconds.
    Returns top GEX/DEX walls, drift, max pain, aggression, IVR.
    """
    from fef_sidecar.db.models import FlowSnapshot

    lo = timestamp_ms - tolerance_seconds * 1000
    hi = timestamp_ms + tolerance_seconds * 1000
    with _get_session() as session:
        stmt = (
            select(FlowSnapshot)
            .where(FlowSnapshot.symbol == symbol.upper())
            .where(FlowSnapshot.timestamp_ms >= lo)
            .where(FlowSnapshot.timestamp_ms <= hi)
            .order_by(FlowSnapshot.timestamp_ms.asc())  # type: ignore[attr-defined]
        )
        rows = list(session.exec(stmt).all())
    if not rows:
        return None
    # Pick the one closest in time
    best = min(rows, key=lambda r: abs(r.timestamp_ms - timestamp_ms))
    return {
        "timestamp_ms": best.timestamp_ms,
        "delta_ms_from_request": best.timestamp_ms - timestamp_ms,
        "session_date": best.session_date, "spx_price": best.spx_price,
        "top_gex_walls": best.top_gex_walls, "top_dex_walls": best.top_dex_walls,
        "drift_cum_net_m": best.drift_cum_net_m, "drift_bias": best.drift_bias,
        "ntm_max_pain": best.ntm_max_pain, "raw_max_pain": best.raw_max_pain,
        "aggression_call_pct": best.aggression_call_pct,
        "aggression_put_pct": best.aggression_put_pct,
        "iv_rank_call_pct": best.iv_rank_call_pct,
        "iv_rank_put_pct": best.iv_rank_put_pct,
    }


@mcp.tool()
def get_microstructure(
    symbol: str = "ES",
    from_ts_ms: int | None = None,
    to_ts_ms: int | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Query persisted microstructure samples (OFI / AVI / microprice).

    Args:
        symbol: the PROXY symbol the engine actually subscribed to
            (SPX → ES, QQQ → NQ, IWM → RTY in standard config)
        from_ts_ms / to_ts_ms: UTC millisecond bounds
        limit: max rows (default 1000)
    """
    from fef_sidecar.db.models import MicrostructureSample

    with _get_session() as session:
        stmt = (
            select(MicrostructureSample)
            .where(MicrostructureSample.symbol == symbol.upper())
            .order_by(MicrostructureSample.timestamp_ms.asc())  # type: ignore[attr-defined]
            .limit(limit)
        )
        if from_ts_ms is not None:
            stmt = stmt.where(MicrostructureSample.timestamp_ms >= from_ts_ms)
        if to_ts_ms is not None:
            stmt = stmt.where(MicrostructureSample.timestamp_ms <= to_ts_ms)
        rows = session.exec(stmt).all()
    return [
        {"timestamp_ms": r.timestamp_ms,
         "ofi_window": r.ofi_window, "avi_pct": r.avi_pct,
         "aggressive_buy_volume": r.aggressive_buy_volume,
         "aggressive_sell_volume": r.aggressive_sell_volume,
         "microprice": r.microprice, "last_trade_price": r.last_trade_price,
         "microprice_deviation_bp": r.microprice_deviation_bp,
         "direction": r.direction, "confidence": r.confidence}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Pre-baked analysis tools (5)
# ---------------------------------------------------------------------------


@mcp.tool()
def analyze_trade(ticket_id: int) -> dict[str, Any]:
    """Full autopsy of a single trade: setup at entry, flow context,
    exit timing, P/L, and a structured summary the caller (Claude) can
    extend into a narrative."""
    from fef_sidecar.db.models import SignalIter, TradeTicket

    with _get_session() as session:
        t = session.exec(select(TradeTicket).where(TradeTicket.id == ticket_id)).first()
        if t is None:
            return {"error": f"Ticket {ticket_id} not found"}
        s = session.exec(select(SignalIter).where(SignalIter.id == t.signal_iter_id)).first()

    flow = get_flow_snapshot_at(int(t.created_at.timestamp() * 1000), tolerance_seconds=60) if t else None

    return {
        "ticket": {
            "id": t.id, "direction": t.direction.value,
            "contract_strike": t.contract_strike, "contract_expiry": t.contract_expiry,
            "qty": t.qty, "entry_limit": t.entry_limit,
            "status": t.status.value,
            "fill_price": t.fill_price,
            "exit_prices": {
                "t1": t.exit_t1_price, "t2": t.exit_t2_price,
                "t3": t.exit_t3_price, "stop": t.exit_stop_price,
            },
            "pnl_dollars": t.pnl_dollars,
            "duration_seconds": (
                (t.closed_at - t.created_at).total_seconds()
                if t.closed_at else None
            ),
        },
        "signal_at_entry": {
            "iter_num": s.iter_num if s else None,
            "session_date": s.session_date if s else None,
            "spx_price": s.spx_price if s else None,
            "direction": s.direction.value if s else None,
            "confidence": s.confidence.value if s else None,
            "reasoning": s.reasoning if s else None,
            "extras": s.extras if s else None,
        } if s else None,
        "flow_at_entry": flow,
        "outcome_classification": (
            "WIN" if (t.pnl_dollars or 0) > 0
            else "LOSS" if (t.pnl_dollars or 0) < 0
            else "BREAKEVEN"
        ),
    }


@mcp.tool()
def daily_summary(session_date: str) -> dict[str, Any]:
    """Assembled daily debrief: signal count by direction/confidence,
    trade outcomes, regime distribution, calendar events, attribution.

    Pass session_date as YYYY-MM-DD. Returns structured data Claude can
    weave into a narrative."""
    from fef_sidecar.db.models import (CalendarEvent, FlowSnapshot,
                                       SignalIter, TradeTicket)

    with _get_session() as session:
        signals = list(session.exec(
            select(SignalIter).where(SignalIter.session_date == session_date)
        ).all())
        tickets = list(session.exec(
            select(TradeTicket)
        ).all())
        tickets = [t for t in tickets if t.created_at.strftime("%Y-%m-%d") == session_date]
        events = list(session.exec(
            select(CalendarEvent).where(CalendarEvent.event_date == session_date)
        ).all())
        flow_count = len(list(session.exec(
            select(FlowSnapshot).where(FlowSnapshot.session_date == session_date)
        ).all()))

    by_direction: dict[str, int] = {}
    by_confidence: dict[str, int] = {}
    actionable = 0
    high_conviction = 0
    for s in signals:
        by_direction[s.direction.value] = by_direction.get(s.direction.value, 0) + 1
        by_confidence[s.confidence.value] = by_confidence.get(s.confidence.value, 0) + 1
        if s.direction.value != "WAIT":
            actionable += 1
        if s.confidence.value == "HIGH" and s.direction.value != "WAIT":
            high_conviction += 1

    closed_tickets = [t for t in tickets if t.status.value in ("CLOSED", "STOPPED", "PARTIAL")]
    wins = [t for t in closed_tickets if (t.pnl_dollars or 0) > 0]
    losses = [t for t in closed_tickets if (t.pnl_dollars or 0) < 0]
    total_pnl = sum(t.pnl_dollars or 0 for t in closed_tickets)

    if signals:
        first_spx = signals[0].spx_price
        last_spx = signals[-1].spx_price
        spx_range = max(s.spx_price for s in signals) - min(s.spx_price for s in signals)
    else:
        first_spx = last_spx = spx_range = None

    return {
        "session_date": session_date,
        "signals": {
            "total": len(signals),
            "actionable": actionable,
            "high_conviction": high_conviction,
            "by_direction": by_direction,
            "by_confidence": by_confidence,
        },
        "trades": {
            "total_tickets": len(tickets),
            "closed": len(closed_tickets),
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(closed_tickets), 3) if closed_tickets else None,
            "total_pnl": round(total_pnl, 2),
            "best": round(max((t.pnl_dollars or 0 for t in closed_tickets), default=0), 2),
            "worst": round(min((t.pnl_dollars or 0 for t in closed_tickets), default=0), 2),
        },
        "spx_session": {
            "open_price": first_spx, "close_price": last_spx,
            "range_pts": round(spx_range, 2) if spx_range else None,
        },
        "calendar_events": [
            {"category": e.category, "title": e.title,
             "time_et": e.event_time_et, "impact": e.impact}
            for e in events
        ],
        "flow_snapshots_captured": flow_count,
    }


@mcp.tool()
def find_similar_signals(
    reference_signal_id: int,
    k: int = 10,
) -> list[dict[str, Any]]:
    """k-Nearest-Neighbors over the FEF signal feature space.

    Given one signal, find the K most similar historical signals
    (same direction/confidence, similar drift / max-pain-distance /
    wall structure / aggression). Useful for: 'what other trades looked
    like this one, and how did they play out?'

    Returns each match with its outcome (if a TradeTicket is attached)."""
    from fef_sidecar.db.models import SignalIter, TradeTicket

    with _get_session() as session:
        ref = session.exec(
            select(SignalIter).where(SignalIter.id == reference_signal_id)
        ).first()
        if ref is None:
            return [{"error": f"Signal {reference_signal_id} not found"}]
        candidates = list(session.exec(
            select(SignalIter)
            .where(SignalIter.id != reference_signal_id)
            .where(SignalIter.direction == ref.direction)
            .where(SignalIter.confidence == ref.confidence)
        ).all())
        # Pull all tickets so we can join outcomes
        all_tickets = list(session.exec(select(TradeTicket)).all())
        tickets_by_signal = {t.signal_iter_id: t for t in all_tickets}

    def _vec(s: SignalIter) -> list[float]:
        return [
            s.drift_cum_net_m,
            (s.spx_price - s.ntm_max_pain),
            s.aggression_call_pct or 50,
            s.aggression_put_pct or 50,
            s.iv_rank_call_pct or 50,
            s.iv_rank_put_pct or 50,
        ]

    ref_v = _vec(ref)

    def _dist(s: SignalIter) -> float:
        v = _vec(s)
        return sum((a - b) ** 2 for a, b in zip(ref_v, v)) ** 0.5

    scored = sorted(candidates, key=_dist)[:k]

    return [
        {
            "id": s.id, "iter_num": s.iter_num, "session_date": s.session_date,
            "timestamp": s.timestamp.isoformat(), "spx_price": s.spx_price,
            "distance": round(_dist(s), 2),
            "reasoning": s.reasoning,
            "outcome": (
                {
                    "ticket_id": tickets_by_signal[s.id].id,
                    "status": tickets_by_signal[s.id].status.value,
                    "pnl_dollars": tickets_by_signal[s.id].pnl_dollars,
                } if s.id in tickets_by_signal else None
            ),
        }
        for s in scored
    ]


@mcp.tool()
def attribution_breakdown(
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """P/L decomposed by direction / confidence / hour-of-day / pattern_match
    across the given date range. Pre-aggregated, ready for Claude to compare."""
    from collections import defaultdict
    from fef_sidecar.db.models import SignalIter, TradeTicket

    with _get_session() as session:
        tickets = list(session.exec(select(TradeTicket)).all())
        if from_date:
            tickets = [t for t in tickets if t.created_at.strftime("%Y-%m-%d") >= from_date]
        if to_date:
            tickets = [t for t in tickets if t.created_at.strftime("%Y-%m-%d") <= to_date]
        closed = [t for t in tickets if t.status.value in ("CLOSED", "STOPPED", "PARTIAL")]
        sig_ids = {t.signal_iter_id for t in closed}
        sigs = list(session.exec(
            select(SignalIter).where(SignalIter.id.in_(sig_ids))  # type: ignore[attr-defined]
        ).all()) if sig_ids else []
        sigs_by_id = {s.id: s for s in sigs if s.id is not None}

    def _bucket(items: list[tuple[str, float]]) -> list[dict]:
        groups: dict[str, list[float]] = defaultdict(list)
        for k, v in items:
            groups[k].append(v)
        out = []
        for k, pnls in groups.items():
            wins = sum(1 for p in pnls if p > 0)
            total = sum(pnls)
            out.append({
                "bucket": k, "trades": len(pnls),
                "wins": wins, "losses": len(pnls) - wins,
                "win_rate": round(wins / len(pnls), 3) if pnls else 0,
                "total_pnl": round(total, 2),
                "avg_pnl": round(total / len(pnls), 2) if pnls else 0,
            })
        return sorted(out, key=lambda d: -d["total_pnl"])

    direction_items = []
    confidence_items = []
    hour_items = []
    weekday_items = []
    for t in closed:
        sig = sigs_by_id.get(t.signal_iter_id)
        if sig is None:
            continue
        pnl = t.pnl_dollars or 0
        direction_items.append((sig.direction.value, pnl))
        confidence_items.append((sig.confidence.value, pnl))
        hour_items.append((sig.timestamp.strftime("%H:00"), pnl))
        weekday_items.append((sig.timestamp.strftime("%A"), pnl))

    return {
        "closed_trades_in_range": len(closed),
        "total_pnl": round(sum((t.pnl_dollars or 0) for t in closed), 2),
        "by_direction": _bucket(direction_items),
        "by_confidence": _bucket(confidence_items),
        "by_hour": _bucket(hour_items),
        "by_weekday": _bucket(weekday_items),
    }


@mcp.tool()
def propose_pattern(
    based_on_signal_ids: list[int] | None = None,
    based_on_won_trades_last_n: int | None = None,
) -> dict[str, Any]:
    """Analyze a set of signals (provided IDs or last N winning trades) +
    return a proposed pattern definition that captures their common features.

    Output format matches the pattern.conditions JSON schema, ready to be
    saved via /patterns POST. Claude can review the proposal before saving."""
    from collections import Counter
    from fef_sidecar.db.models import SignalIter, TradeTicket

    with _get_session() as session:
        if based_on_signal_ids:
            signals = list(session.exec(
                select(SignalIter).where(SignalIter.id.in_(based_on_signal_ids))  # type: ignore[attr-defined]
            ).all())
        elif based_on_won_trades_last_n:
            tickets = list(session.exec(select(TradeTicket)).all())
            won = [t for t in tickets if (t.pnl_dollars or 0) > 0]
            won = sorted(won, key=lambda t: t.created_at, reverse=True)[:based_on_won_trades_last_n]
            sig_ids = [t.signal_iter_id for t in won]
            signals = list(session.exec(
                select(SignalIter).where(SignalIter.id.in_(sig_ids))  # type: ignore[attr-defined]
            ).all())
        else:
            return {"error": "Pass either based_on_signal_ids or based_on_won_trades_last_n"}

    if not signals:
        return {"error": "No signals to analyze"}

    # Find common features
    dirs = Counter(s.direction.value for s in signals)
    confs = Counter(s.confidence.value for s in signals)

    drifts = [s.drift_cum_net_m for s in signals]
    drift_min, drift_max = min(drifts), max(drifts)
    drift_mean = sum(drifts) / len(drifts)

    mp_dists = [s.spx_price - s.ntm_max_pain for s in signals]
    mp_min, mp_max = min(mp_dists), max(mp_dists)

    ivr_calls = [s.iv_rank_call_pct for s in signals if s.iv_rank_call_pct is not None]
    ivr_puts = [s.iv_rank_put_pct for s in signals if s.iv_rank_put_pct is not None]

    aggs_call = [s.aggression_call_pct for s in signals if s.aggression_call_pct is not None]
    aggs_put = [s.aggression_put_pct for s in signals if s.aggression_put_pct is not None]

    # Build conditions list — only include filters where the range is meaningful
    conditions = []

    # Direction filter only if 80%+ of signals agree
    dominant_dir, dir_count = dirs.most_common(1)[0]
    if dir_count / len(signals) >= 0.8:
        conditions.append({"field": "direction", "op": "==", "value": dominant_dir})

    dominant_conf, conf_count = confs.most_common(1)[0]
    if conf_count / len(signals) >= 0.8:
        conditions.append({"field": "confidence", "op": "==", "value": dominant_conf})

    if drift_max - drift_min < 80:  # tight range
        conditions.append({"field": "drift_cum_net_m", "op": "between",
                           "value": [round(drift_min - 5, 1), round(drift_max + 5, 1)]})

    if mp_max - mp_min < 20:
        conditions.append({"field": "mp_distance", "op": "between",
                           "value": [round(mp_min - 2, 1), round(mp_max + 2, 1)]})

    if ivr_calls and max(ivr_calls) - min(ivr_calls) < 30:
        conditions.append({"field": "iv_rank_call_pct", "op": ">=",
                           "value": round(min(ivr_calls), 1)})

    return {
        "proposed_name": f"AI Pattern ({dominant_dir} {dominant_conf}, n={len(signals)})",
        "proposed_description": (
            f"Auto-generated from {len(signals)} winning signals. "
            f"Average drift {drift_mean:+.1f}M, MP distance range "
            f"[{mp_min:+.1f}, {mp_max:+.1f}]."
        ),
        "based_on_n_signals": len(signals),
        "conditions": conditions,
        "summary_stats": {
            "direction_distribution": dict(dirs),
            "confidence_distribution": dict(confs),
            "drift_range": [round(drift_min, 2), round(drift_max, 2)],
            "drift_mean": round(drift_mean, 2),
            "mp_distance_range": [round(mp_min, 2), round(mp_max, 2)],
            "ivr_call_range": [round(min(ivr_calls), 1), round(max(ivr_calls), 1)] if ivr_calls else None,
            "ivr_put_range": [round(min(ivr_puts), 1), round(max(ivr_puts), 1)] if ivr_puts else None,
            "aggression_call_range": [round(min(aggs_call), 1), round(max(aggs_call), 1)] if aggs_call else None,
            "aggression_put_range": [round(min(aggs_put), 1), round(max(aggs_put), 1)] if aggs_put else None,
        },
        "next_step_hint": (
            "Review the conditions and call save_pattern(name, description, conditions) "
            "to add it to your live pattern library — or call test_pattern(conditions, ...) "
            "to backtest it first."
        ),
    }


# ---------------------------------------------------------------------------
# Helpers + entry point
# ---------------------------------------------------------------------------


@mcp.tool()
def list_available_data() -> dict[str, Any]:
    """Inventory: how much data of each kind is in the DB.

    Useful as a first call so Claude knows what historical windows are
    available to query."""
    from fef_sidecar.db.models import (CalendarEvent, FlowSnapshot,
                                       HistoricalBar, MicrostructureSample,
                                       SignalIter, TradeTicket)

    with _get_session() as session:
        sig_count = len(list(session.exec(select(SignalIter)).all()))
        tick_count = len(list(session.exec(select(TradeTicket)).all()))
        bar_rows = list(session.exec(select(HistoricalBar)).all())
        flow_rows = list(session.exec(select(FlowSnapshot)).all())
        micro_count = len(list(session.exec(select(MicrostructureSample)).all()))
        cal_count = len(list(session.exec(select(CalendarEvent)).all()))

        sig_dates = sorted({s.session_date for s in session.exec(select(SignalIter)).all()})
        bar_symbols = sorted({b.symbol for b in bar_rows})
    return {
        "signals_total": sig_count,
        "signal_dates": sig_dates[-10:],  # most recent 10
        "tickets_total": tick_count,
        "bars_total": len(bar_rows),
        "bar_symbols": bar_symbols,
        "flow_snapshots_total": len(flow_rows),
        "microstructure_samples_total": micro_count,
        "calendar_events_total": cal_count,
        "db_path": str(_db_path()),
    }


@mcp.tool()
def get_daily_reviews(limit: int = 30) -> list[dict[str, Any]]:
    """Return the most recent DailyReview rows (one per session).

    Each row has: session_date, total_signals, actionable_signals, wins/stops/
    timeouts, win_rate_pct, expectancy_r, t1/t2/t3 hit counts, plus by_X
    breakdowns (by_trigger, by_confidence, by_hour_ct, by_direction,
    by_regime, by_wall_distance, by_fe_ratio, by_block_reason), and
    actual_trades_taken / actual_wins / actual_losses / actual_total_R.

    Use this as the entry point for any cross-session analysis.
    """
    from fef_sidecar.db.models import DailyReview
    with _get_session() as session:
        rows = session.exec(
            select(DailyReview).order_by(DailyReview.session_date.desc()).limit(limit)  # type: ignore[attr-defined]
        ).all()
    return [
        {"id": r.id, "session_date": r.session_date,
         "computed_at": r.computed_at.isoformat(),
         "total_signals": r.total_signals,
         "actionable_signals": r.actionable_signals,
         "wins": r.wins, "stops": r.stops, "timeouts": r.timeouts,
         "win_rate_pct": r.win_rate_pct, "expectancy_r": r.expectancy_r,
         "t1_hits": r.t1_hits, "t2_hits": r.t2_hits, "t3_hits": r.t3_hits,
         "by_trigger": r.by_trigger, "by_confidence": r.by_confidence,
         "by_hour_ct": r.by_hour_ct, "by_direction": r.by_direction,
         "by_regime": r.by_regime, "by_wall_distance": r.by_wall_distance,
         "by_fe_ratio": r.by_fe_ratio, "by_block_reason": r.by_block_reason,
         "actual_trades_taken": r.actual_trades_taken,
         "actual_wins": r.actual_wins, "actual_losses": r.actual_losses,
         "actual_total_R": r.actual_total_R}
        for r in rows
    ]


@mcp.tool()
def get_engine_changes(limit: int = 100) -> list[dict[str, Any]]:
    """Return EngineChange rows ordered by applied_at desc.

    Each row documents a semantic engine change with: name, category
    (gate/threshold/exit/safety/scoring/data), description, expected_impact,
    measured_impact_r (filled in post-hoc), and git_commit_sha. Cross-reference
    with DailyReview rows to compute the pre/post impact of each change.
    """
    from fef_sidecar.db.models import EngineChange
    with _get_session() as session:
        rows = session.exec(
            select(EngineChange).order_by(EngineChange.applied_at.desc()).limit(limit)  # type: ignore[attr-defined]
        ).all()
    return [
        {"id": r.id, "applied_at": r.applied_at.isoformat(),
         "name": r.name, "description": r.description, "category": r.category,
         "expected_impact": r.expected_impact,
         "measured_impact_r": r.measured_impact_r,
         "git_commit_sha": r.git_commit_sha}
        for r in rows
    ]


@mcp.tool()
def get_tuning_recommendations(status: str = "OPEN",
                                limit: int = 50) -> list[dict[str, Any]]:
    """Return TuningRecommendation rows.

    status: 'OPEN' (active recommendations from the latest auto-eval),
            'APPLIED' (user shipped), 'DISMISSED' (user rejected),
            'SUPERSEDED' (replaced by a newer eval), or 'all'.

    kind field is one of: STATE_OF_MODEL (always emitted, has backing_data.state
    of GREEN/AMBER/RED), FREEZE (3+ sessions of -0.5R), ELIGIBLE_FOR_CHANGE
    (confluence stable + 5+ days since last change), PROPOSE_GATE (feature
    bucket has 25+pp separation), ROLLBACK_CANDIDATE (post-change expectancy
    dropped >0.2R).
    """
    from fef_sidecar.db.models import TuningRecommendation
    with _get_session() as session:
        stmt = select(TuningRecommendation).order_by(TuningRecommendation.generated_at.desc()).limit(limit)  # type: ignore[attr-defined]
        if status != "all":
            stmt = stmt.where(TuningRecommendation.status == status)
        rows = session.exec(stmt).all()
    return [
        {"id": r.id, "generated_at": r.generated_at.isoformat(),
         "based_on_sessions": r.based_on_sessions,
         "based_on_window_start": r.based_on_window_start,
         "based_on_window_end": r.based_on_window_end,
         "kind": r.kind, "priority": r.priority,
         "title": r.title, "body": r.body, "backing_data": r.backing_data,
         "status": r.status}
        for r in rows
    ]


@mcp.tool()
def get_shadow_comparisons(session_date: str | None = None,
                            limit: int = 200) -> list[dict[str, Any]]:
    """Return ShadowComparison rows — A/B test of production vs shadow engine.

    Use this when shadow mode is enabled (POST /shadow/enable on the sidecar)
    to analyze whether a proposed engine config would produce different signals.
    Each row records production direction/confidence + shadow direction/confidence
    + agreement flags.
    """
    from fef_sidecar.db.models import ShadowComparison
    with _get_session() as session:
        stmt = select(ShadowComparison).order_by(ShadowComparison.iter_timestamp.desc()).limit(limit)  # type: ignore[attr-defined]
        if session_date:
            stmt = stmt.where(ShadowComparison.session_date == session_date)
        rows = session.exec(stmt).all()
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


@mcp.tool()
def get_hft_samples(symbol: str = "SPX", from_ts_ms: int | None = None,
                     to_ts_ms: int | None = None,
                     limit: int = 500) -> list[dict[str, Any]]:
    """Return HFT signal samples — realized vol + quote stuffing + L2 depth.

    HFTSample rows are written by the sidecar's hft_loop every ~1s. Each
    row has: short_vol_pts, long_vol_pts, vol_ratio, vol_regime,
    cancel_to_trade_ratio, quote_intensity_per_sec, stuffing_severity,
    book_imbalance, book_imbalance_5lvl, book_direction, book_confidence,
    weighted_mid_price, book_spread, composite_direction, vol_source,
    stuffing_source, depth_source.

    For correlating HFT state with signal outcomes: query SignalIter
    rows + HFTSample rows by timestamp_ms range, or read the
    signal_iter.extras.hft_at_fire field which has the HFT snapshot
    captured at fire time.
    """
    from fef_sidecar.db.models import HFTSample
    with _get_session() as session:
        stmt = select(HFTSample).where(HFTSample.symbol == symbol)
        if from_ts_ms is not None:
            stmt = stmt.where(HFTSample.timestamp_ms >= from_ts_ms)
        if to_ts_ms is not None:
            stmt = stmt.where(HFTSample.timestamp_ms <= to_ts_ms)
        stmt = stmt.order_by(HFTSample.timestamp_ms.desc()).limit(limit)  # type: ignore[attr-defined]
        rows = session.exec(stmt).all()
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
         "weighted_mid_price": r.weighted_mid_price,
         "book_spread": r.book_spread,
         "composite_direction": r.composite_direction}
        for r in rows
    ]


def main() -> None:
    """Entry point for the `fef-mcp` console script."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    logger.info("fef-mcp starting — DB at %s", _db_path())
    mcp.run()


if __name__ == "__main__":
    main()
