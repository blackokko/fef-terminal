"""Performance attribution — P/L decomposed by pattern / regime / confidence / hour.

All queries run against the existing TradeTicket + SignalIter tables.
No new persistence — the data is already there, we just slice it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import SignalIter, TicketStatus, TradeTicket


@dataclass(frozen=True)
class BucketStat:
    bucket: str
    trades: int
    wins: int
    losses: int
    total_pnl: float
    avg_pnl: float
    win_rate: float


async def attribution() -> dict[str, list[BucketStat]]:
    """Compute P/L attribution across multiple dimensions in one query pass."""

    by_direction: dict[str, list[float]] = defaultdict(list)
    by_confidence: dict[str, list[float]] = defaultdict(list)
    by_hour: dict[str, list[float]] = defaultdict(list)
    by_signal_id: dict[int, float] = {}  # signal_id -> realized pnl

    async with get_session() as session:
        # Pull all closed tickets with their associated signal
        ticket_rows = list((await session.exec(
            select(TradeTicket).where(
                TradeTicket.status.in_([TicketStatus.CLOSED, TicketStatus.STOPPED, TicketStatus.PARTIAL])  # type: ignore[attr-defined]
            )
        )).all())

        signal_ids = {t.signal_iter_id for t in ticket_rows}
        signals_by_id: dict[int, SignalIter] = {}
        if signal_ids:
            sigs = list((await session.exec(
                select(SignalIter).where(SignalIter.id.in_(signal_ids))  # type: ignore[attr-defined]
            )).all())
            signals_by_id = {s.id: s for s in sigs if s.id is not None}

        for t in ticket_rows:
            pnl = t.pnl_dollars or 0.0
            sig = signals_by_id.get(t.signal_iter_id)
            if sig is None:
                continue
            by_direction[sig.direction.value].append(pnl)
            by_confidence[sig.confidence.value].append(pnl)
            hour = sig.timestamp.strftime("%H:00")
            by_hour[hour].append(pnl)
            by_signal_id[sig.id] = pnl  # type: ignore[index]

    def stats_for(d: dict[str, list[float]]) -> list[BucketStat]:
        out = []
        for k, pnls in d.items():
            wins = sum(1 for p in pnls if p > 0)
            losses = sum(1 for p in pnls if p < 0)
            total = sum(pnls)
            avg = total / len(pnls) if pnls else 0
            wr = wins / len(pnls) if pnls else 0
            out.append(BucketStat(
                bucket=k, trades=len(pnls), wins=wins, losses=losses,
                total_pnl=round(total, 2), avg_pnl=round(avg, 2),
                win_rate=round(wr, 3),
            ))
        return sorted(out, key=lambda s: -s.total_pnl)

    return {
        "by_direction": stats_for(by_direction),
        "by_confidence": stats_for(by_confidence),
        "by_hour": stats_for(by_hour),
    }
