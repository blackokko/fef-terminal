"""Mock auto-trader — synthesizes ticket fills + closes so backtest/analytics
have data to display in mock mode.

Opt-in via FEF_MOCK_AUTO_TRADE=true (default on when FEF_MOCK_SCRIPTED_DAY=true).
On each actionable verdict, creates a TradeTicket, simulates a fill ~3 iters
later, then closes after another ~5 iters with proxy P/L computed from
where SPX moved relative to the recommended direction.

In live mode this is replaced by real IBKR execution reconciliation.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class _PendingTrade:
    ticket_id: int
    signal_iter_id: int
    direction: str
    contract_strike: float
    entry_spx: float
    entry_iter_num: int
    opened_at: datetime
    state: str = "OPEN"  # OPEN -> FILLED -> CLOSED


@dataclass
class MockAutoTraderState:
    pending: list[_PendingTrade] = field(default_factory=list)
    rng: random.Random = field(default_factory=lambda: random.Random())


async def maybe_create_ticket(
    state_holder: MockAutoTraderState,
    verdict: Any,
    inputs: Any,
    signal_iter_id: int,
    iter_num: int,
) -> int | None:
    """If verdict is actionable, auto-create a TradeTicket in the DB.

    Returns the new ticket id, or None if skipped.
    """
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import FEFDirection, TicketStatus, TradeTicket

    if verdict.direction.value == "WAIT" or verdict.strike_rec is None:
        return None

    async with get_session() as session:
        ticket = TradeTicket(
            signal_iter_id=signal_iter_id,
            direction=FEFDirection(verdict.direction.value),
            contract_strike=verdict.strike_rec,
            contract_expiry=datetime.now(UTC).strftime("%Y-%m-%d"),
            qty=verdict.suggested_qty or 15,
            entry_limit=verdict.t1 or 0.0,  # filled below
            t1=verdict.t1 or 0.0,
            t2=verdict.t2 or 0.0,
            t3=verdict.t3 or 0.0,
            stop=verdict.stop or 0.0,
            status=TicketStatus.DRAFT,
            notes="AUTO mock-autotrader",
        )
        session.add(ticket)
        await session.flush()
        state_holder.pending.append(_PendingTrade(
            ticket_id=ticket.id or 0,
            signal_iter_id=signal_iter_id,
            direction=verdict.direction.value,
            contract_strike=verdict.strike_rec,
            entry_spx=inputs.spx_price,
            entry_iter_num=iter_num,
            opened_at=datetime.now(UTC),
        ))
        return ticket.id


async def advance_pending(
    state_holder: MockAutoTraderState,
    current_iter_num: int,
    current_spx: float,
) -> None:
    """Move OPEN tickets to FILLED after 1+ iter, FILLED to CLOSED after 3-6 more.

    Closing P/L is a proxy: how much SPX moved in our direction times $5/pt
    per contract (roughly delta=0.5 × $100 × 1 contract = $50 per point per
    15-contract position).
    """
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import TicketStatus, TradeTicket
    from sqlmodel import select

    still_pending: list[_PendingTrade] = []

    async with get_session() as session:
        for p in state_holder.pending:
            iters_held = current_iter_num - p.entry_iter_num
            ticket = (await session.exec(select(TradeTicket).where(TradeTicket.id == p.ticket_id))).first()
            if ticket is None:
                continue

            if p.state == "OPEN" and iters_held >= 1:
                # Simulate fill at entry price + tiny slippage
                fill = round(ticket.entry_limit + state_holder.rng.uniform(-0.05, 0.05), 2)
                ticket.fill_price = max(0.05, fill)
                ticket.filled_at = datetime.now(UTC)
                ticket.status = TicketStatus.FILLED
                p.state = "FILLED"
                session.add(ticket)
                still_pending.append(p)
                continue

            close_after = state_holder.rng.randint(3, 6)
            if p.state == "FILLED" and iters_held >= close_after:
                # Proxy P/L: SPX move × delta × qty × $100
                spx_move = current_spx - p.entry_spx
                directional_move = spx_move if p.direction == "LONG_CALLS" else -spx_move
                # Per contract: roughly $50 per point (delta 0.5 × $100)
                # Plus noise to simulate variability
                proxy_pnl = directional_move * 50 * ticket.qty + state_holder.rng.uniform(-100, 100)
                # Cap at "stopped out" if move was very adverse
                stop_threshold = -10 * 50 * ticket.qty  # -10 SPX pts = stop
                if proxy_pnl < stop_threshold:
                    proxy_pnl = stop_threshold
                    ticket.status = TicketStatus.STOPPED
                else:
                    ticket.status = TicketStatus.CLOSED

                ticket.pnl_dollars = round(proxy_pnl, 2)
                ticket.closed_at = datetime.now(UTC)
                # Synthesize an exit price reflecting P/L
                pnl_per_contract = proxy_pnl / ticket.qty / 100
                ticket.exit_t3_price = round((ticket.fill_price or ticket.entry_limit) + pnl_per_contract, 2)
                session.add(ticket)
                # Don't add to still_pending — trade is closed
                continue

            # Still in progress
            still_pending.append(p)

    state_holder.pending = still_pending
