"""High-level read/write methods used by the FEF engine + WebSocket layer.

Keep query logic out of the engine/WS code — they go through these methods
so we can swap storage later without touching call sites.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import or_, select

from fef_sidecar import et_time
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import (
    AuditLog,
    CalendarEvent,
    DailySession,
    FEFConfidence,
    FEFDirection,
    FlowSnapshot,
    HistoricalBar,
    LiveOrder,
    LivePosition,
    MLModelRun,
    MicrostructureSample,
    SignalIter,
    SignalLookaheadOutcome,
    TradeTicket,
    VixSample,
    WalkForwardRun,
)


def _today_et() -> str:
    return et_time.today_et()


def build_actionable_signals_sql(
    *,
    session_date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    paper_only: bool = True,
    price_band: tuple[float, float] | None = (1000.0, 10000.0),
    require_lookahead: bool = False,
    select_clause: str = "s.*",
) -> tuple[str, list]:
    """Build the canonical 'actionable signals' SELECT for raw sqlite3
    callers (research scripts).

    Mirrors :func:`fetch_actionable_signals` semantics: actionable = direction
    != 'WAIT', with explicit paper_only / price_band / require_lookahead /
    date-range axes. Each research script that filters on 'actionable'
    should call this and pass the args explicitly so we can SEE what
    definition is in use.

    The paper_only filter is implemented in SQL via a json_extract on
    extras.account_class (SQLite has the json_extract function; sqlmodel
    layer's helper does post-query filtering instead — same semantics).

    Args mirror :func:`fetch_actionable_signals`. Returns (sql, params)
    suitable for ``cursor.execute(sql, params)``.

    Example:
        sql, params = build_actionable_signals_sql(
            date_from='2026-05-18', date_to='2026-05-22',
            paper_only=True, price_band=(1000.0, 10000.0),
        )
        rows = cur.execute(sql, params).fetchall()
    """
    where: list[str] = ["s.direction != 'WAIT'"]
    params: list = []
    if session_date is not None:
        where.append("s.session_date = ?")
        params.append(session_date)
    elif date_from is not None and date_to is not None:
        where.append("s.session_date BETWEEN ? AND ?")
        params.extend([date_from, date_to])
    if price_band is not None:
        lo, hi = price_band
        where.append("s.spx_price BETWEEN ? AND ?")
        params.extend([lo, hi])
    if paper_only:
        # SQLite's json_extract on extras returns NULL when missing.
        # Pre-tag rows (account_class missing) are treated as paper-equivalent.
        where.append(
            "("
            "json_extract(s.extras, '$.account_class') IS NULL "
            "OR json_extract(s.extras, '$.account_class') = 'paper'"
            ")"
        )
    if require_lookahead:
        where.append(
            "EXISTS (SELECT 1 FROM signal_lookahead_outcome o "
            "WHERE o.signal_iter_id = s.id AND o.lookahead_minutes = 60)"
        )
    sql = (
        f"SELECT {select_clause} FROM signal_iter s "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY s.session_date, s.timestamp"
    )
    return sql, params


async def fetch_actionable_signals(
    session: AsyncSession,
    *,
    session_date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    paper_only: bool = True,
    price_band: tuple[float, float] | None = (1000.0, 10000.0),
    require_lookahead: bool = False,
) -> list[SignalIter]:
    """Single source of truth for fetching 'actionable' SignalIter rows.

    Canonical definition: `direction != FEFDirection.WAIT` (matches the
    DailySession.actionable_signals counter and daily_review's
    actionable_signals column).

    Chat audit Priority 10 (2026-05-22): the "actionable" filter varied
    across 10 research sites; counts (268, 307, 43) diverged due to
    inconsistent date range, account-class filter, and price-band guard.
    This helper centralises the three axes of variation so each caller
    SETS them explicitly and we can SEE what definition is being used.

    Args:
        session: an open AsyncSession (callers manage the session lifecycle).
        session_date: single YYYY-MM-DD. Mutually exclusive with the range.
        date_from / date_to: inclusive YYYY-MM-DD range. Both must be set
            to filter on range; either alone is ignored.
        paper_only: when True, filters out extras.account_class != 'paper'
            but keeps rows where account_class is missing (pre-tag rows,
            treated as paper-equivalent — 100% of pre-5/21 data is from the
            paper account DUM098107). When False, all account classes pass.
        price_band: (lo, hi) inclusive SPX sanity bounds. Drops rows where
            spx_price is outside the band (catches test/mock rows with
            spx_price like 67.20). Set to None to disable.
        require_lookahead: when True, only return signals that have a
            matching SignalLookaheadOutcome row at lookahead_minutes=60.
            Use this when downstream analysis needs forward outcomes.

    Returns: list of SignalIter rows ordered by id ascending (chronological
    within a session, monotonic across sessions).
    """
    stmt = select(SignalIter).where(SignalIter.direction != FEFDirection.WAIT)
    if session_date is not None:
        stmt = stmt.where(SignalIter.session_date == session_date)
    elif date_from is not None and date_to is not None:
        stmt = stmt.where(SignalIter.session_date >= date_from)
        stmt = stmt.where(SignalIter.session_date <= date_to)
    if price_band is not None:
        lo, hi = price_band
        stmt = stmt.where(SignalIter.spx_price >= lo)
        stmt = stmt.where(SignalIter.spx_price <= hi)
    if require_lookahead:
        # Inner join on signal_lookahead_outcome @ 60min — only signals with
        # a forward outcome row are returned. Done as a subquery so the
        # primary SignalIter row still comes back as a SignalIter object.
        lookahead_subq = (
            select(SignalLookaheadOutcome.signal_iter_id)
            .where(SignalLookaheadOutcome.lookahead_minutes == 60)
        ).subquery()
        stmt = stmt.where(SignalIter.id.in_(select(lookahead_subq)))  # type: ignore[attr-defined]
    stmt = stmt.order_by(SignalIter.id.asc())  # type: ignore[attr-defined]
    result = await session.exec(stmt)
    rows = list(result.all())
    if paper_only:
        # account_class lives in extras (JSON column). Filtering it post-
        # query is cheaper than expression-indexing the JSON, and
        # 'unknown'/missing should pass — 100% of pre-tag data is paper.
        filtered: list[SignalIter] = []
        for r in rows:
            ex = r.extras if isinstance(r.extras, dict) else {}
            cls = (ex.get("account_class") if isinstance(ex, dict) else None) or "unknown"
            cls = str(cls).lower()
            if cls in ("paper", "unknown"):
                filtered.append(r)
        rows = filtered
    return rows


class SignalIterRepository:
    """Writes/reads for SignalIter + cascading DailySession updates."""

    # Per-process iter counter — resets to 0 on each sidecar startup so the UI
    # shows iter #1, #2, ... fresh per launch instead of resuming today's
    # cumulative count from the DB (which could be in the thousands after a
    # mock-testing day).
    _process_iter_counter: int = 0

    @staticmethod
    async def next_iter_num(session_date: str | None = None) -> int:
        """Return iter_num for the next signal — fresh per sidecar launch."""
        SignalIterRepository._process_iter_counter += 1
        return SignalIterRepository._process_iter_counter

    @staticmethod
    async def write_iter(
        *,
        spx_price: float,
        direction: FEFDirection,
        confidence: FEFConfidence,
        reasoning: str,
        drift_cum_net_m: float,
        drift_bias: str,
        ntm_max_pain: float,
        raw_max_pain: float,
        top_gex_walls: list[dict[str, Any]],
        top_dex_walls: list[dict[str, Any]],
        top_cex_walls: list[dict[str, Any]] | None = None,
        top_vex_walls: list[dict[str, Any]] | None = None,
        top_gex_by_expiry: list[dict[str, Any]] | None = None,
        top_dex_by_expiry: list[dict[str, Any]] | None = None,
        zero_dte_gex_net_m: float | None = None,
        zero_dte_dex_net_m: float | None = None,
        recent_orders: list[dict[str, Any]] | None = None,
        recent_whale_count_30m: int = 0,
        recent_golden_sweep_count_30m: int = 0,
        aggression_call_pct: float | None = None,
        aggression_put_pct: float | None = None,
        iv_rank_call_pct: float | None = None,
        iv_rank_put_pct: float | None = None,
        extras: dict[str, Any] | None = None,
        session_date: str | None = None,
        provider_mode: str | None = None,
    ) -> SignalIter:
        """Persist one FEF iter and bump the DailySession counters atomically."""
        date = session_date or _today_et()
        iter_num = await SignalIterRepository.next_iter_num(date)

        signal = SignalIter(
            session_date=date,
            iter_num=iter_num,
            spx_price=spx_price,
            direction=direction,
            confidence=confidence,
            reasoning=reasoning,
            drift_cum_net_m=drift_cum_net_m,
            drift_bias=drift_bias,
            ntm_max_pain=ntm_max_pain,
            raw_max_pain=raw_max_pain,
            top_gex_walls=top_gex_walls,
            top_dex_walls=top_dex_walls,
            top_cex_walls=top_cex_walls or [],
            top_vex_walls=top_vex_walls or [],
            top_gex_by_expiry=top_gex_by_expiry or [],
            top_dex_by_expiry=top_dex_by_expiry or [],
            zero_dte_gex_net_m=zero_dte_gex_net_m,
            zero_dte_dex_net_m=zero_dte_dex_net_m,
            recent_orders=recent_orders or [],
            recent_whale_count_30m=recent_whale_count_30m,
            recent_golden_sweep_count_30m=recent_golden_sweep_count_30m,
            aggression_call_pct=aggression_call_pct,
            aggression_put_pct=aggression_put_pct,
            iv_rank_call_pct=iv_rank_call_pct,
            iv_rank_put_pct=iv_rank_put_pct,
            extras=extras or {},
            provider_mode=provider_mode,
        )

        async with get_session() as session:
            session.add(signal)
            await session.flush()

            # Upsert daily session counters
            existing = await session.exec(
                select(DailySession).where(DailySession.session_date == date)
            )
            daily = existing.first()
            if daily is None:
                daily = DailySession(session_date=date, total_iters=1)
                if direction != FEFDirection.WAIT:
                    daily.actionable_signals = 1
                session.add(daily)
            else:
                daily.total_iters += 1
                if direction != FEFDirection.WAIT:
                    daily.actionable_signals += 1
                daily.updated_at = datetime.now(UTC)
                session.add(daily)

            await session.commit()
            await session.refresh(signal)
            return signal

    @staticmethod
    async def recent(limit: int = 50, session_date: str | None = None) -> list[SignalIter]:
        """Return most recent iters for the given session (default: today).

        Order by id desc, NOT iter_num desc: iter_num is a per-process counter
        that resets each sidecar launch, so iter#28 from an earlier process
        would otherwise sort above iter#5 from the freshest one.
        """
        date = session_date or _today_et()
        async with get_session() as session:
            stmt = (
                select(SignalIter)
                .where(SignalIter.session_date == date)
                .order_by(SignalIter.id.desc())  # type: ignore[attr-defined]
                .limit(limit)
            )
            result = await session.exec(stmt)
            return list(result.all())

    @staticmethod
    async def actionable_today(session_date: str | None = None) -> list[SignalIter]:
        """Return all actionable (non-WAIT) iters from today's session.

        Thin wrapper around :func:`fetch_actionable_signals` for the common
        "today, paper, with price-band guard" case.
        """
        date = session_date or _today_et()
        async with get_session() as session:
            return await fetch_actionable_signals(
                session,
                session_date=date,
                paper_only=True,
                price_band=(1000.0, 10000.0),
            )

    # ------------------------------------------------------------------
    # Historical persistence — bars, flow snapshots, microstructure, VIX
    # ------------------------------------------------------------------

    @staticmethod
    async def write_bar(symbol: str, bar_size: str, timestamp_ms: int,
                        open_: float, high: float, low: float, close: float, volume: float) -> None:
        """Persist a single OHLCV bar. Silently no-ops on duplicate
        (same symbol+bar_size+timestamp_ms) so reconnects don't fail."""
        async with get_session() as session:
            existing = await session.exec(
                select(HistoricalBar)
                .where(HistoricalBar.symbol == symbol)
                .where(HistoricalBar.bar_size == bar_size)
                .where(HistoricalBar.timestamp_ms == timestamp_ms)
            )
            if existing.first() is not None:
                return
            session.add(HistoricalBar(
                symbol=symbol, bar_size=bar_size, timestamp_ms=timestamp_ms,
                open=open_, high=high, low=low, close=close, volume=volume,
            ))

    @staticmethod
    async def query_bars(symbol: str, bar_size: str, *,
                         from_ms: int | None = None, to_ms: int | None = None,
                         limit: int = 5000) -> list[HistoricalBar]:
        async with get_session() as session:
            stmt = (
                select(HistoricalBar)
                .where(HistoricalBar.symbol == symbol)
                .where(HistoricalBar.bar_size == bar_size)
                .order_by(HistoricalBar.timestamp_ms.asc())  # type: ignore[attr-defined]
                .limit(limit)
            )
            if from_ms is not None:
                stmt = stmt.where(HistoricalBar.timestamp_ms >= from_ms)
            if to_ms is not None:
                stmt = stmt.where(HistoricalBar.timestamp_ms <= to_ms)
            return list((await session.exec(stmt)).all())

    @staticmethod
    async def query_recent_bars(symbol: str, bar_size: str, *,
                                limit: int = 400) -> list[HistoricalBar]:
        """Most recent `limit` bars for symbol+bar_size, returned OLDEST-first
        (chronological). Used by the pattern trigger loop + overlay endpoint,
        which want 'the last N bars' rather than a time window."""
        async with get_session() as session:
            stmt = (
                select(HistoricalBar)
                .where(HistoricalBar.symbol == symbol)
                .where(HistoricalBar.bar_size == bar_size)
                .order_by(HistoricalBar.timestamp_ms.desc())  # type: ignore[attr-defined]
                .limit(limit)
            )
            rows = list((await session.exec(stmt)).all())
        rows.reverse()
        return rows

    @staticmethod
    async def write_flow_snapshot(
        symbol: str, session_date: str, timestamp_ms: int, spx_price: float,
        top_gex_walls: list, top_dex_walls: list,
        drift_cum_net_m: float, drift_bias: str,
        ntm_max_pain: float, raw_max_pain: float,
        aggression_call_pct: float | None, aggression_put_pct: float | None,
        iv_rank_call_pct: float | None, iv_rank_put_pct: float | None,
        top_cex_walls: list | None = None,
        top_vex_walls: list | None = None,
        top_gex_by_expiry: list | None = None,
        top_dex_by_expiry: list | None = None,
        zero_dte_gex_net_m: float | None = None,
        zero_dte_dex_net_m: float | None = None,
        recent_orders: list | None = None,
        recent_whale_count_30m: int = 0,
        recent_golden_sweep_count_30m: int = 0,
    ) -> None:
        async with get_session() as session:
            session.add(FlowSnapshot(
                symbol=symbol, session_date=session_date, timestamp_ms=timestamp_ms,
                spx_price=spx_price,
                top_gex_walls=top_gex_walls, top_dex_walls=top_dex_walls,
                top_cex_walls=top_cex_walls or [],
                top_vex_walls=top_vex_walls or [],
                top_gex_by_expiry=top_gex_by_expiry or [],
                top_dex_by_expiry=top_dex_by_expiry or [],
                zero_dte_gex_net_m=zero_dte_gex_net_m,
                zero_dte_dex_net_m=zero_dte_dex_net_m,
                recent_orders=recent_orders or [],
                recent_whale_count_30m=recent_whale_count_30m,
                recent_golden_sweep_count_30m=recent_golden_sweep_count_30m,
                drift_cum_net_m=drift_cum_net_m, drift_bias=drift_bias,
                ntm_max_pain=ntm_max_pain, raw_max_pain=raw_max_pain,
                aggression_call_pct=aggression_call_pct,
                aggression_put_pct=aggression_put_pct,
                iv_rank_call_pct=iv_rank_call_pct,
                iv_rank_put_pct=iv_rank_put_pct,
            ))

    @staticmethod
    async def query_flow_snapshots(
        symbol: str, session_date: str | None = None,
        *, from_ms: int | None = None, to_ms: int | None = None,
        limit: int = 5000,
    ) -> list[FlowSnapshot]:
        async with get_session() as session:
            stmt = (
                select(FlowSnapshot)
                .where(FlowSnapshot.symbol == symbol)
                .order_by(FlowSnapshot.timestamp_ms.asc())  # type: ignore[attr-defined]
                .limit(limit)
            )
            if session_date is not None:
                stmt = stmt.where(FlowSnapshot.session_date == session_date)
            if from_ms is not None:
                stmt = stmt.where(FlowSnapshot.timestamp_ms >= from_ms)
            if to_ms is not None:
                stmt = stmt.where(FlowSnapshot.timestamp_ms <= to_ms)
            return list((await session.exec(stmt)).all())

    @staticmethod
    async def write_microstructure(
        symbol: str, timestamp_ms: int,
        ofi_window: float, avi_pct: float,
        aggressive_buy_volume: float, aggressive_sell_volume: float,
        microprice: float | None, last_trade_price: float | None,
        microprice_deviation_bp: float | None,
        direction: str, confidence: str,
    ) -> None:
        async with get_session() as session:
            session.add(MicrostructureSample(
                symbol=symbol, timestamp_ms=timestamp_ms,
                ofi_window=ofi_window, avi_pct=avi_pct,
                aggressive_buy_volume=aggressive_buy_volume,
                aggressive_sell_volume=aggressive_sell_volume,
                microprice=microprice, last_trade_price=last_trade_price,
                microprice_deviation_bp=microprice_deviation_bp,
                direction=direction, confidence=confidence,
            ))

    @staticmethod
    async def query_microstructure(symbol: str, *,
                                   from_ms: int | None = None, to_ms: int | None = None,
                                   limit: int = 10000) -> list[MicrostructureSample]:
        async with get_session() as session:
            stmt = (
                select(MicrostructureSample)
                .where(MicrostructureSample.symbol == symbol)
                .order_by(MicrostructureSample.timestamp_ms.asc())  # type: ignore[attr-defined]
                .limit(limit)
            )
            if from_ms is not None:
                stmt = stmt.where(MicrostructureSample.timestamp_ms >= from_ms)
            if to_ms is not None:
                stmt = stmt.where(MicrostructureSample.timestamp_ms <= to_ms)
            return list((await session.exec(stmt)).all())

    @staticmethod
    async def write_vix(timestamp_ms: int, value: float) -> None:
        async with get_session() as session:
            session.add(VixSample(timestamp_ms=timestamp_ms, value=value))

    @staticmethod
    async def write_audit_log(category: str, summary: str,
                              actor: str = "user", details: dict | None = None,
                              block_reason_code: str | None = None) -> None:
        """Append an AuditLog row.

        block_reason_code: structured code for WAIT/block rows (matches
        auto_trader.BlockReason values). Populates the dedicated column
        so aggregation queries don't have to dig into the details JSON.
        Leave None for non-block rows (kill_switch, config, order, etc.).
        """
        d = dict(details or {})
        # Keep details["block_reason_code"] in sync if the kwarg is set —
        # historical callers stuffed it into details; downstream readers
        # (research scripts) check both locations.
        if block_reason_code is not None and "block_reason_code" not in d:
            d["block_reason_code"] = block_reason_code
        # Build payload conditionally because old DBs missed the column
        # before the migration ran (init_db tolerates the schema gap).
        kwargs: dict[str, Any] = dict(category=category, summary=summary,
                                       actor=actor, details=d)
        if block_reason_code is not None:
            kwargs["block_reason_code"] = block_reason_code
        async with get_session() as session:
            session.add(AuditLog(**kwargs))

    @staticmethod
    async def query_audit_log(*, category: str | None = None, limit: int = 200) -> list[AuditLog]:
        async with get_session() as session:
            # id tie-break: rows written within one timestamp tick (Windows
            # datetime resolution ~15ms) otherwise return in unstable order.
            stmt = select(AuditLog).order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()).limit(limit)  # type: ignore[attr-defined]
            if category is not None:
                stmt = stmt.where(AuditLog.category == category)
            return list((await session.exec(stmt)).all())

    @staticmethod
    async def upsert_calendar_event(event: CalendarEvent) -> CalendarEvent:
        async with get_session() as session:
            existing = await session.exec(
                select(CalendarEvent)
                .where(CalendarEvent.event_date == event.event_date)
                .where(CalendarEvent.category == event.category)
                .where(CalendarEvent.title == event.title)
            )
            row = existing.first()
            if row is not None:
                return row
            session.add(event)
            await session.flush()
            await session.refresh(event)
            return event

    @staticmethod
    async def query_calendar_events(*, from_date: str | None = None,
                                    to_date: str | None = None) -> list[CalendarEvent]:
        async with get_session() as session:
            stmt = select(CalendarEvent).order_by(CalendarEvent.event_date.asc())  # type: ignore[attr-defined]
            if from_date is not None:
                stmt = stmt.where(CalendarEvent.event_date >= from_date)
            if to_date is not None:
                stmt = stmt.where(CalendarEvent.event_date <= to_date)
            return list((await session.exec(stmt)).all())

    @staticmethod
    async def write_live_order(order: LiveOrder) -> LiveOrder:
        async with get_session() as session:
            session.add(order)
            await session.flush()
            await session.refresh(order)
            return order

    @staticmethod
    async def write_watcher_sell_child(
        *,
        parent: LiveOrder,
        bracket_role: str,
        qty: int,
        ib_order_id: int | None,
        ib_perm_id: int | None = None,
        avg_fill_price: float | None = None,
        notes: str = "",
    ) -> LiveOrder:
        """Persist a child LiveOrder row for a watcher-fired market SELL.

        The watcher fires market sells outside the place_bracket pipeline
        (SPX-points exits, dollar-loss cap, runner-protect, post-T1 trail).
        Those fires previously only landed in ibkr_execution, so per-parent
        prorated P&L attribution had to dedup by contract-stacking heuristic.
        This writer makes the parent_id linkage explicit on the LiveOrder
        side so attribution is unambiguous.

        bracket_role values: 't1' / 't2' / 't3' / 'stop' / 'runner_protect'
                             / 'dollar_cap' / 'trail_after_t1' / 'eod_flat'
        """
        child = LiveOrder(
            ticket_id=parent.ticket_id,
            parent_id=parent.id,
            symbol=parent.symbol,
            strike=parent.strike,
            right=parent.right,
            expiry=parent.expiry,
            # SPY-mirror ledger tag propagates parent -> sell children so the
            # mirror book's EXITS are selectable without a parent join
            # (2026-07-17 review Finding 5).
            mirror_of=getattr(parent, "mirror_of", None),
            action="SELL",
            qty=qty,
            order_type="MKT",
            limit_price=None,
            bracket_role=bracket_role,
            status="FILLED" if avg_fill_price is not None else "SUBMITTED",
            filled_qty=qty if avg_fill_price is not None else 0,
            avg_fill_price=avg_fill_price,
            ib_order_id=ib_order_id,
            ib_perm_id=ib_perm_id,
            dry_run=parent.dry_run,
            engine_version=parent.engine_version,
            notes=notes,
        )
        return await SignalIterRepository.write_live_order(child)

    @staticmethod
    async def query_live_orders(*, status: str | None = None,
                                limit: int = 200) -> list[LiveOrder]:
        async with get_session() as session:
            stmt = select(LiveOrder).order_by(LiveOrder.submitted_at.desc()).limit(limit)  # type: ignore[attr-defined]
            if status is not None:
                stmt = stmt.where(LiveOrder.status == status)
            return list((await session.exec(stmt)).all())

    # Statuses that do NOT consume the once-per-session budget, because they
    # represent an order that never reached the broker and left no position and
    # no exposure. Everything else — including DRY_RUN and any status not listed
    # here — counts, so the deny-list fails CLOSED against future status values.
    #
    # ⚠ 2026-08-12: the fully status-blind version counted parent 927, which our
    # OWN FEF_ALLOW_CREDIT_SPREADS gate had refused and marked REJECTED before
    # anything was transmitted, as "already placed today". One self-refused
    # attempt burned the entire session and produced 104 `already_today` blocks.
    # DRY_RUN must keep counting (a rehearsal has to exercise the same guard the
    # live path does — that was the 2026-08-11 finding); a refusal must not.
    SPREAD_DEDUPE_IGNORED_STATUSES = ("REJECTED", "CANCELLED", "CANCELED")

    @staticmethod
    async def spread_parents_since(since: "datetime") -> list[LiveOrder]:
        """Every credit-spread PARENT submitted at/after `since` that still
        counts against the one-per-session budget.

        Time-bounded rather than the usual `limit=N` recency window. The spread
        auto-trader's once-per-session guarantee was previously in-memory (lost
        on restart) and, when it did consult the DB, used
        `query_live_orders(limit=200)` — a recency window that an older parent
        falls out of on a busy day, plus a status filter that omitted DRY_RUN.
        Both let a second order through on a day that already had one
        (adversarial review, 2026-08-11).

        Near-status-blind: every status counts EXCEPT the ones in
        `SPREAD_DEDUPE_IGNORED_STATUSES`, and even those still count if the
        order carries a broker identifier. See that constant for the incident
        that required the change.

        ⚠ WHY THE BROKER-ID TERM EXISTS. Excluding REJECTED is only safe for
        orders that never reached IBKR. `place_credit_spread` wraps
        `ib.placeOrder` and the DB write that follows it in ONE try/except, and
        marks the parent REJECTED on any exception — so a submission that
        SUCCEEDED but whose bookkeeping write failed is recorded as REJECTED
        while the order is live at the broker. Under a plain status exclusion
        the session would reopen and a second spread could go out on top of a
        real position. Any row that got an `ib_order_id` or `ib_perm_id`
        therefore keeps consuming the budget regardless of status. (The gate
        refusal this whole change is about never reaches `placeOrder`, so it
        has neither id and is still correctly excluded.)
        """
        ignored = SignalIterRepository.SPREAD_DEDUPE_IGNORED_STATUSES
        async with get_session() as session:
            stmt = (
                select(LiveOrder)
                .where(LiveOrder.exit_strategy == "spread_premium")  # type: ignore[attr-defined]
                .where(LiveOrder.bracket_role == "parent")  # type: ignore[attr-defined]
                .where(LiveOrder.submitted_at >= since)  # type: ignore[attr-defined]
                # Count the row unless it is BOTH an ignored status AND never
                # made it to the broker. A NULL status is unknown, not refused —
                # `notin_` alone evaluates NULL and silently drops the row,
                # which would fail OPEN — so it is listed explicitly.
                .where(
                    or_(
                        LiveOrder.status.is_(None),  # type: ignore[attr-defined]
                        LiveOrder.status.notin_(ignored),  # type: ignore[attr-defined]
                        LiveOrder.ib_order_id.is_not(None),  # type: ignore[attr-defined]
                        LiveOrder.ib_perm_id.is_not(None),  # type: ignore[attr-defined]
                    )
                )
            )
            return list((await session.exec(stmt)).all())

    @staticmethod
    async def update_live_order(order_id: int, **fields: object) -> None:
        async with get_session() as session:
            row = (await session.exec(select(LiveOrder).where(LiveOrder.id == order_id))).first()
            if row is None:
                return
            for k, v in fields.items():
                setattr(row, k, v)
            row.last_status_at = datetime.now(UTC)

    @staticmethod
    async def write_live_position(pos: LivePosition) -> None:
        async with get_session() as session:
            session.add(pos)

    @staticmethod
    async def query_live_positions(*, since_ms: int | None = None,
                                   limit: int = 100) -> list[LivePosition]:
        async with get_session() as session:
            stmt = select(LivePosition).order_by(LivePosition.snapshot_ts.desc()).limit(limit)  # type: ignore[attr-defined]
            return list((await session.exec(stmt)).all())

    @staticmethod
    async def write_walk_forward_run(run: WalkForwardRun) -> None:
        async with get_session() as session:
            session.add(run)

    @staticmethod
    async def query_walk_forward_runs(limit: int = 30) -> list[WalkForwardRun]:
        async with get_session() as session:
            stmt = select(WalkForwardRun).order_by(WalkForwardRun.run_at.desc()).limit(limit)  # type: ignore[attr-defined]
            return list((await session.exec(stmt)).all())

    @staticmethod
    async def write_ml_model_run(run: MLModelRun) -> None:
        async with get_session() as session:
            session.add(run)

    @staticmethod
    async def query_ml_model_runs(limit: int = 20) -> list[MLModelRun]:
        async with get_session() as session:
            stmt = select(MLModelRun).order_by(MLModelRun.trained_at.desc()).limit(limit)  # type: ignore[attr-defined]
            return list((await session.exec(stmt)).all())

    @staticmethod
    async def list_historical_dates(symbol: str) -> list[str]:
        """Return distinct YYYY-MM-DD dates that have flow snapshots
        for this symbol, descending. Used by the chart date picker."""
        from datetime import datetime as _dt
        async with get_session() as session:
            stmt = (
                select(FlowSnapshot.session_date)
                .where(FlowSnapshot.symbol == symbol)
                .distinct()
            )
            dates = list((await session.exec(stmt)).all())
            dates.sort(reverse=True)
            return dates

    @staticmethod
    async def attach_ticket(signal_iter_id: int, ticket: TradeTicket) -> TradeTicket:
        """Persist a trade ticket linked to the given signal iter."""
        ticket.signal_iter_id = signal_iter_id
        async with get_session() as session:
            session.add(ticket)
            await session.flush()

            daily_date_stmt = select(SignalIter.session_date).where(SignalIter.id == signal_iter_id)
            date_result = await session.exec(daily_date_stmt)
            date = date_result.one()

            existing = await session.exec(
                select(DailySession).where(DailySession.session_date == date)
            )
            daily = existing.first()
            if daily is not None:
                daily.trades_taken += 1
                daily.updated_at = datetime.now(UTC)
                session.add(daily)

            await session.refresh(ticket)
            return ticket
