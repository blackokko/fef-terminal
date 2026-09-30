"""Performance attribution — P/L decomposed by pattern / regime / confidence / hour.

Pulls from BOTH:
  - TradeTicket rows (manual-ticket workflow — user copies an order to TWS)
  - LiveOrder rows (auto-trader fills, joined with IBKRExecution for real $)

For LiveOrder rows, P/L is computed from actual IBKR fills (BOT vs SLD avg
prices × qty × 100). This is the authoritative dollar P/L for auto-trade
activity.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlmodel import select

from fef_sidecar import et_time
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import (
    IBKRExecution, LiveOrder, SignalIter, TicketStatus, TradeTicket,
)


@dataclass(frozen=True)
class BucketStat:
    bucket: str
    trades: int
    wins: int
    losses: int
    total_pnl: float
    avg_pnl: float
    win_rate: float


def _utc_dt_to_ct_hour_label(dt) -> str:
    """Convert tz-naive UTC datetime → 12-hour CT label like '9 AM CT'.
    Uses America/Chicago zoneinfo so DST is handled — CT is UTC-6 in
    winter (CST) and UTC-5 in summer (CDT).
    """
    ct = et_time.to_ct(dt)
    h24 = ct.hour
    period = "AM" if h24 < 12 else "PM"
    h12 = h24 % 12
    if h12 == 0:
        h12 = 12
    return f"{h12} {period} CT"


def _hour_sort_key(label: str) -> int:
    """Sort '9 AM CT' < '1 PM CT' < '3 PM CT'. Returns 0-23 ordinal."""
    try:
        parts = label.split(" ")
        h = int(parts[0])
        period = parts[1] if len(parts) > 1 else "AM"
        if period == "AM":
            return 0 if h == 12 else h
        else:  # PM
            return 12 if h == 12 else h + 12
    except Exception:
        return 99


async def attribution(session_date: str | None = None) -> dict[str, list[BucketStat]]:
    """Compute P/L attribution across multiple dimensions in one query pass.

    Args:
        session_date: YYYY-MM-DD to filter to a single trading session in ET.
                      If None, returns all-time attribution.
    """
    by_direction: dict[str, list[float]] = defaultdict(list)
    by_confidence: dict[str, list[float]] = defaultdict(list)
    by_hour: dict[str, list[float]] = defaultdict(list)
    by_signal_id: dict[int, float] = {}  # signal_id -> realized pnl

    def _ticket_in_window(t):
        if session_date is None:
            return True
        # closed_at is the realization timestamp; fallback to filled_at
        ts = t.closed_at or t.filled_at
        if ts is None:
            return False
        from datetime import timezone
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        et = et_time.to_et(ts)
        return et.strftime("%Y-%m-%d") == session_date

    def _parent_in_window(p):
        if session_date is None:
            return True
        from datetime import timezone
        ts = p.submitted_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        et = et_time.to_et(ts)
        return et.strftime("%Y-%m-%d") == session_date

    async with get_session() as session:
        # Pull all closed tickets with their associated signal
        ticket_rows = list((await session.exec(
            select(TradeTicket).where(
                TradeTicket.status.in_([TicketStatus.CLOSED, TicketStatus.STOPPED, TicketStatus.PARTIAL])  # type: ignore[attr-defined]
            )
        )).all())
        ticket_rows = [t for t in ticket_rows if _ticket_in_window(t)]

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
            hour = _utc_dt_to_ct_hour_label(sig.timestamp)
            by_hour[hour].append(pnl)
            by_signal_id[sig.id] = pnl  # type: ignore[index]

        # === AUTO-TRADER pass — PER CONTRACT, not per parent ===
        # When multiple parents stack on the same contract (e.g. 4 parents
        # on SPX 7400C), each one looks up the same aggregated IBKR fills.
        # Crediting every parent with the full contract P/L overstates
        # results by a factor of n_parents_per_contract. We bucket per
        # contract instead, using the FIRST parent's submit time/right
        # as representative.
        live_parents = list((await session.exec(
            select(LiveOrder)
            .where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.dry_run == False)  # noqa: E712
            .where(LiveOrder.status.in_(["CLOSED", "STOPPED", "PARTIAL"]))  # type: ignore[attr-defined]
        )).all())
        live_parents = [p for p in live_parents if _parent_in_window(p)]
        if live_parents:
            all_fills = list((await session.exec(select(IBKRExecution))).all())
            fills_by_key: dict[tuple, list] = defaultdict(list)
            for f in all_fills:
                fills_by_key[(f.symbol, f.strike, f.right, f.expiry)].append(f)

        # Group parents by contract; first parent per contract is the bucketer.
        parents_by_contract: dict[tuple, list] = defaultdict(list)
        for p in live_parents:
            parents_by_contract[(p.symbol, p.strike, p.right, p.expiry)].append(p)

        for key, plist in parents_by_contract.items():
            fills = fills_by_key.get(key, [])
            if not fills:
                continue
            bought = sum(f.shares * f.price for f in fills if f.side == "BOT")
            sold = sum(f.shares * f.price for f in fills if f.side == "SLD")
            bot_q = sum(f.shares for f in fills if f.side == "BOT")
            sld_q = sum(f.shares for f in fills if f.side == "SLD")
            if bot_q != sld_q or bot_q == 0:
                continue  # only count fully-closed for clean P/L
            pnl_dollars = round((sold - bought) * 100, 2)

            # Use earliest parent's submit time + the contract's right
            p = min(plist, key=lambda x: x.submitted_at)
            hour = _utc_dt_to_ct_hour_label(p.submitted_at)
            direction = "LONG_CALLS" if p.right == "C" else "LONG_PUTS"

            by_direction[direction].append(pnl_dollars)
            by_hour[hour].append(pnl_dollars)
            by_confidence["AUTO_TRADER"].append(pnl_dollars)

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

    # By-hour wants chronological order (9am, 10am, 11am...) rather than
    # P/L ranking, since the chart reads top-down by time of day.
    by_hour_stats = stats_for(by_hour)
    by_hour_stats.sort(key=lambda s: _hour_sort_key(s.bucket))

    return {
        "by_direction": stats_for(by_direction),
        "by_confidence": stats_for(by_confidence),
        "by_hour": by_hour_stats,
    }
