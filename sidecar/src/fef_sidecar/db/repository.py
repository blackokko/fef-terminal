"""High-level read/write methods used by the FEF engine + WebSocket layer.

Keep query logic out of the engine/WS code — they go through these methods
so we can swap storage later without touching call sites.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import (
    DailySession,
    FEFConfidence,
    FEFDirection,
    FlowSnapshot,
    HistoricalBar,
    MicrostructureSample,
    SignalIter,
    TradeTicket,
    VixSample,
)


def _today_et() -> str:
    et = timezone(timedelta(hours=-4))
    return datetime.now(et).strftime("%Y-%m-%d")


class SignalIterRepository:
    """Writes/reads for SignalIter + cascading DailySession updates."""

    @staticmethod
    async def next_iter_num(session_date: str | None = None) -> int:
        """Return iter_num for the next signal — 1-based per session_date."""
        date = session_date or _today_et()
        async with get_session() as session:
            stmt = select(SignalIter).where(SignalIter.session_date == date)
            result = await session.exec(stmt)
            count = len(result.all())
            return count + 1

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
        aggression_call_pct: float | None = None,
        aggression_put_pct: float | None = None,
        iv_rank_call_pct: float | None = None,
        iv_rank_put_pct: float | None = None,
        extras: dict[str, Any] | None = None,
        session_date: str | None = None,
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
            aggression_call_pct=aggression_call_pct,
            aggression_put_pct=aggression_put_pct,
            iv_rank_call_pct=iv_rank_call_pct,
            iv_rank_put_pct=iv_rank_put_pct,
            extras=extras or {},
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

            await session.refresh(signal)
            return signal

    @staticmethod
    async def recent(limit: int = 50, session_date: str | None = None) -> list[SignalIter]:
        """Return most recent iters for the given session (default: today)."""
        date = session_date or _today_et()
        async with get_session() as session:
            stmt = (
                select(SignalIter)
                .where(SignalIter.session_date == date)
                .order_by(SignalIter.iter_num.desc())  # type: ignore[attr-defined]
                .limit(limit)
            )
            result = await session.exec(stmt)
            return list(result.all())

    @staticmethod
    async def actionable_today(session_date: str | None = None) -> list[SignalIter]:
        """Return all actionable (non-WAIT) iters from today's session."""
        date = session_date or _today_et()
        async with get_session() as session:
            stmt = (
                select(SignalIter)
                .where(SignalIter.session_date == date)
                .where(SignalIter.direction != FEFDirection.WAIT)
                .order_by(SignalIter.iter_num.asc())  # type: ignore[attr-defined]
            )
            result = await session.exec(stmt)
            return list(result.all())

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
    async def write_flow_snapshot(
        symbol: str, session_date: str, timestamp_ms: int, spx_price: float,
        top_gex_walls: list, top_dex_walls: list,
        drift_cum_net_m: float, drift_bias: str,
        ntm_max_pain: float, raw_max_pain: float,
        aggression_call_pct: float | None, aggression_put_pct: float | None,
        iv_rank_call_pct: float | None, iv_rank_put_pct: float | None,
    ) -> None:
        async with get_session() as session:
            session.add(FlowSnapshot(
                symbol=symbol, session_date=session_date, timestamp_ms=timestamp_ms,
                spx_price=spx_price,
                top_gex_walls=top_gex_walls, top_dex_walls=top_dex_walls,
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
