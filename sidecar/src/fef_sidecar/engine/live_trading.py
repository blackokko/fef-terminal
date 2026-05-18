"""Live trading scaffold — IBKR order placement + bracket orders + kill switch.

SAFETY: Defaults to DRY_RUN (writes LiveOrder rows tagged dry_run=True but
never submits to IBKR). To enable real order placement:

    set FEF_LIVE_TRADING=true

Even then, individual /live/place_order calls must include `confirm=true`
in the request body. Two gates so this can't accidentally fire.

Kill switch: POST /live/kill cancels every working IBKR order tagged to this
sidecar's client_id + writes an audit log entry.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from fef_sidecar.db.models import LiveOrder
from fef_sidecar.db.repository import SignalIterRepository

logger = logging.getLogger(__name__)


def is_live_trading_enabled() -> bool:
    return os.environ.get("FEF_LIVE_TRADING", "false").lower() in ("true", "1", "yes", "on")


@dataclass(frozen=True)
class BracketSpec:
    symbol: str
    strike: float
    right: str           # C / P
    expiry: str          # YYYYMMDD
    qty: int
    entry_limit: float
    # Children — option prices for each tier (NOT spx prices)
    t1_limit: float
    t2_limit: float
    t3_limit: float
    stop_limit: float
    # Partial scale-out ratios (must sum to qty)
    t1_qty: int
    t2_qty: int
    t3_qty: int


async def place_bracket(
    ibkr,
    spec: BracketSpec,
    *,
    confirm: bool = False,
    ticket_id: int | None = None,
) -> dict:
    """Submit a bracket order: parent BUY + 3 limit SELL children + stop SELL.

    Returns dict with order IDs + status. If FEF_LIVE_TRADING is false OR
    confirm is false, returns dry-run records without hitting IBKR.

    Bracket structure used (IBKR-compatible):
      parent     LMT BUY  qty @ entry_limit
        ├ child  LMT SELL t1_qty @ t1_limit   (OCA group)
        ├ child  LMT SELL t2_qty @ t2_limit   (OCA group)
        ├ child  LMT SELL t3_qty @ t3_limit   (OCA group)
        └ child  STP SELL qty   @ stop_limit  (OCA group)
    """
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from fef_sidecar.ibkr.models import Contract, ContractKind

    live = is_live_trading_enabled() and confirm and isinstance(ibkr, LiveIBKRProvider)
    dry_run = not live

    contract = Contract(
        symbol=spec.symbol, kind=ContractKind.OPTION,
        exchange="CBOE" if spec.symbol == "SPX" else "SMART",
        expiry=spec.expiry, strike=spec.strike, right=spec.right,
    )

    parent = LiveOrder(
        ticket_id=ticket_id, symbol=spec.symbol, strike=spec.strike,
        right=spec.right, expiry=spec.expiry, action="BUY", qty=spec.qty,
        order_type="LMT", limit_price=spec.entry_limit,
        bracket_role="parent", dry_run=dry_run, status="DRY_RUN" if dry_run else "SUBMITTED",
    )
    parent_saved = await SignalIterRepository.write_live_order(parent)
    children_saved = []
    for tier_qty, tier_price, role in (
        (spec.t1_qty, spec.t1_limit, "t1"),
        (spec.t2_qty, spec.t2_limit, "t2"),
        (spec.t3_qty, spec.t3_limit, "t3"),
        (spec.qty,     spec.stop_limit, "stop"),
    ):
        child = LiveOrder(
            ticket_id=ticket_id, parent_id=parent_saved.id,
            symbol=spec.symbol, strike=spec.strike, right=spec.right,
            expiry=spec.expiry, action="SELL", qty=tier_qty,
            order_type="STP" if role == "stop" else "LMT",
            limit_price=tier_price, bracket_role=role, dry_run=dry_run,
            status="DRY_RUN" if dry_run else "SUBMITTED",
        )
        child_saved = await SignalIterRepository.write_live_order(child)
        children_saved.append(child_saved)

    await SignalIterRepository.write_audit_log(
        category="order", actor="user" if confirm else "system",
        summary=f"{'DRY_RUN' if dry_run else 'PLACED'} bracket: BUY {spec.qty} {spec.symbol} {spec.strike}{spec.right} @ ${spec.entry_limit:.2f}",
        details={
            "parent_order_id": parent_saved.id, "children": [c.id for c in children_saved],
            "spec": {"qty": spec.qty, "t1": spec.t1_limit, "t2": spec.t2_limit,
                     "t3": spec.t3_limit, "stop": spec.stop_limit},
        },
    )

    if dry_run:
        return {
            "ok": True, "dry_run": True,
            "parent_order_id": parent_saved.id,
            "children": [c.id for c in children_saved],
            "reason": "DRY_RUN (FEF_LIVE_TRADING not enabled or confirm=false)",
        }

    # LIVE path — wire to actual IBKR placeOrder.
    #
    # Order structure (no OCA on T1):
    #   parent  LMT BUY qty
    #   T1      LMT SELL t1_qty  — NOT in an OCA group, so T1 fill doesn't
    #                              cancel T2/T3/STOP. Runners stay alive.
    #   T2      LMT SELL t2_qty  — in OCA{stop+T2+T3} so any of them cancels
    #                              the others (T2 fill cancels T3 + STOP, etc.)
    #   T3      LMT SELL t3_qty  — same OCA as T2
    #   STOP    STP SELL qty     — same OCA as T2/T3 (one stop covers all
    #                              remaining qty; the runner_protector loop
    #                              will market-exit T2/T3 separately if a
    #                              retrace below T1 fill is detected)
    try:
        from ib_async import LimitOrder, StopOrder

        ib = ibkr._ib  # type: ignore[attr-defined]
        ib_contract = ibkr._to_ib_contract(contract) if hasattr(ibkr, "_to_ib_contract") else None
        if ib_contract is None:
            from fef_sidecar.ibkr.live import _to_ib_contract
            ib_contract = _to_ib_contract(contract)
        await ib.qualifyContractsAsync(ib_contract)

        parent_order = LimitOrder("BUY", spec.qty, spec.entry_limit, transmit=False)
        parent_trade = ib.placeOrder(ib_contract, parent_order)
        await SignalIterRepository.update_live_order(
            parent_saved.id, ib_order_id=parent_trade.order.orderId,
            ib_perm_id=parent_trade.order.permId,
        )

        # OCA only for runners + stop (T1 stays free-standing)
        runner_oca = f"FEF_RUNNERS_{parent_trade.order.orderId}"
        children_pairs = list(zip(children_saved, (
            (spec.t1_qty, spec.t1_limit, "t1"),
            (spec.t2_qty, spec.t2_limit, "t2"),
            (spec.t3_qty, spec.t3_limit, "t3"),
            (spec.qty,    spec.stop_limit, "stop"),
        )))
        for child_record, (tier_qty, tier_price, role) in children_pairs:
            if role == "stop":
                co = StopOrder("SELL", spec.qty, spec.stop_limit,
                               parentId=parent_trade.order.orderId,
                               ocaGroup=runner_oca, ocaType=1, transmit=True)
            elif role == "t1":
                # T1 standalone — no OCA, so T1 fill doesn't kill runners
                co = LimitOrder("SELL", tier_qty, tier_price,
                                parentId=parent_trade.order.orderId,
                                transmit=False)
            else:  # t2 or t3
                co = LimitOrder("SELL", tier_qty, tier_price,
                                parentId=parent_trade.order.orderId,
                                ocaGroup=runner_oca, ocaType=1, transmit=False)
            child_trade = ib.placeOrder(ib_contract, co)
            await SignalIterRepository.update_live_order(
                child_record.id, ib_order_id=child_trade.order.orderId,
                ib_perm_id=child_trade.order.permId,
            )

        return {"ok": True, "dry_run": False, "parent_order_id": parent_saved.id,
                "children": [c.id for c in children_saved],
                "runner_oca_group": runner_oca}
    except Exception as e:
        logger.exception("Live order submission failed")
        await SignalIterRepository.update_live_order(parent_saved.id, status="REJECTED", notes=str(e))
        return {"ok": False, "error": str(e)}


async def kill_all_orders(ibkr) -> dict:
    """Cancel every working IBKR order under this client_id.
    Always logs to audit_log."""
    from fef_sidecar.ibkr.live import LiveIBKRProvider

    if not isinstance(ibkr, LiveIBKRProvider):
        await SignalIterRepository.write_audit_log(
            category="kill_switch", actor="user",
            summary="Kill switch invoked in MOCK mode (no-op)",
        )
        return {"ok": True, "mock": True, "cancelled": 0}

    cancelled = 0
    try:
        ib = ibkr._ib  # type: ignore[attr-defined]
        if ib is None:
            return {"ok": False, "error": "Not connected"}
        for trade in ib.openTrades():
            try:
                ib.cancelOrder(trade.order)
                cancelled += 1
            except Exception:
                logger.exception("Failed to cancel order %s", trade.order.orderId)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    await SignalIterRepository.write_audit_log(
        category="kill_switch", actor="user",
        summary=f"Kill switch — cancelled {cancelled} working orders",
        details={"cancelled_count": cancelled},
    )
    return {"ok": True, "cancelled": cancelled}
