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


def require_paper_account() -> bool:
    """When true, refuse to submit any order to an account whose ID doesn't
    start with 'DU' (IBKR paper account prefix). Default ON — protects
    against the surprisingly common case where TWS is logged into the live
    account but exposing the API on port 7497 (paper's default port).
    """
    return os.environ.get("FEF_REQUIRE_PAPER_ACCOUNT", "true").lower() in (
        "true", "1", "yes", "on"
    )


def is_paper_account(account_id: str) -> bool:
    """IBKR paper accounts are prefixed 'DU' (Demo User). Live accounts
    start with 'U' (no D)."""
    return account_id.startswith("DU")


async def assert_paper_if_required(ibkr) -> None:
    """Hard-stop guard before live submission when FEF_REQUIRE_PAPER_ACCOUNT
    is on (default). Raises if the connected account looks live."""
    if not require_paper_account():
        return
    try:
        summary = await ibkr.get_account_summary()
    except Exception:
        summary = None
    if summary is None:
        raise RuntimeError(
            "Could not read IBKR account summary — refusing to submit order "
            "while FEF_REQUIRE_PAPER_ACCOUNT=true (safety default). Set "
            "FEF_REQUIRE_PAPER_ACCOUNT=false to override (live-money mode)."
        )
    account_id = summary.account or ""
    if not is_paper_account(account_id):
        raise RuntimeError(
            f"REFUSING TO SUBMIT — connected account '{account_id}' does not "
            f"look like a paper account (paper IDs start with 'DU'). Either "
            f"(a) connect TWS to the paper account on port 7497, or "
            f"(b) set FEF_REQUIRE_PAPER_ACCOUNT=false to allow live trading."
        )


@dataclass(frozen=True)
class BracketSpec:
    symbol: str
    strike: float
    right: str           # C / P
    expiry: str          # YYYYMMDD
    qty: int
    entry_limit: float
    # Children — option prices for each tier (used when exit_strategy='option_price'
    # or 'fixed_dollar'). Ignored when exit_strategy='spx_points'.
    t1_limit: float
    t2_limit: float
    t3_limit: float
    stop_limit: float
    # Partial scale-out ratios (must sum to qty)
    t1_qty: int
    t2_qty: int
    t3_qty: int
    # Exit strategy + SPX-point targets (when applicable)
    exit_strategy: str = "option_price"  # 'option_price' / 'spx_points' / 'fixed_dollar'
    spx_t1: float | None = None
    spx_t2: float | None = None
    spx_t3: float | None = None
    spx_stop: float | None = None


@dataclass(frozen=True)
class SpreadSpec:
    """Credit-spread bracket spec (CCS or PCS) — sells the short leg + buys
    the long leg for net credit. Exit rule: close at $0.05 (full profit) or
    when current spread debit hits stop_multiplier × initial_credit."""
    symbol: str
    expiry: str
    kind: str            # 'CCS' (call credit) or 'PCS' (put credit)
    short_strike: float
    long_strike: float
    qty: int
    entry_credit: float  # net credit per spread (positive)
    stop_multiplier: float = 4.0
    tp_close_debit: float = 0.05


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

    # SAFETY GATE: when FEF_REQUIRE_PAPER_ACCOUNT=true (default), refuse to
    # submit live orders to a non-paper account. Catches the case where TWS
    # is logged into the live account but exposing port 7497.
    if live:
        try:
            await assert_paper_if_required(ibkr)
        except RuntimeError as e:
            await SignalIterRepository.write_audit_log(
                category="order_refused", actor="system",
                summary=f"REFUSED: {e}",
                details={"reason": str(e), "spec_qty": spec.qty,
                         "spec_strike": spec.strike, "spec_right": spec.right},
            )
            return {"ok": False, "error": str(e), "refused_safety_gate": True}

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
        exit_strategy=spec.exit_strategy,
        spx_target_t1=spec.spx_t1, spx_target_t2=spec.spx_t2,
        spx_target_t3=spec.spx_t3, spx_target_stop=spec.spx_stop,
        spx_target_t1_qty=spec.t1_qty if spec.exit_strategy == "spx_points" else None,
        spx_target_t2_qty=spec.t2_qty if spec.exit_strategy == "spx_points" else None,
        spx_target_t3_qty=spec.t3_qty if spec.exit_strategy == "spx_points" else None,
    )
    parent_saved = await SignalIterRepository.write_live_order(parent)
    children_saved = []
    # SPX-points mode: no option-price children, only the parent.
    # The spx_target_watcher_loop fires market sells as SPX crosses each level.
    if spec.exit_strategy == "spx_points":
        if dry_run:
            return {
                "ok": True, "dry_run": True,
                "parent_order_id": parent_saved.id,
                "children": [],
                "exit_strategy": "spx_points",
                "spx_targets": {"t1": spec.spx_t1, "t2": spec.spx_t2,
                                "t3": spec.spx_t3, "stop": spec.spx_stop},
                "reason": "DRY_RUN — SPX watcher will simulate exits on cross",
            }
        # Live SPX-points: place ONLY the parent buy. Watcher handles exits.
        try:
            from ib_async import LimitOrder
            ib = ibkr._ib  # type: ignore[attr-defined]
            from fef_sidecar.ibkr.live import _to_ib_contract
            ib_contract = _to_ib_contract(contract)
            await ib.qualifyContractsAsync(ib_contract)
            parent_order = LimitOrder("BUY", spec.qty, spec.entry_limit, transmit=True)
            parent_trade = ib.placeOrder(ib_contract, parent_order)
            await SignalIterRepository.update_live_order(
                parent_saved.id, ib_order_id=parent_trade.order.orderId,
                ib_perm_id=parent_trade.order.permId,
            )
            await SignalIterRepository.write_audit_log(
                category="order", actor="user",
                summary=f"PLACED spx_points entry: BUY {spec.qty} {spec.symbol} {spec.strike}{spec.right} @ ${spec.entry_limit:.2f}. Watcher fires at SPX T1/T2/T3/STOP={spec.spx_t1}/{spec.spx_t2}/{spec.spx_t3}/{spec.spx_stop}",
                details={"parent_order_id": parent_saved.id, "exit_strategy": "spx_points"},
            )
            return {"ok": True, "dry_run": False, "parent_order_id": parent_saved.id,
                    "exit_strategy": "spx_points"}
        except Exception as e:
            logger.exception("spx_points parent submission failed")
            await SignalIterRepository.update_live_order(parent_saved.id, status="REJECTED", notes=str(e))
            return {"ok": False, "error": str(e)}

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


async def place_credit_spread(
    ibkr,
    spec: SpreadSpec,
    *,
    confirm: bool = False,
    ticket_id: int | None = None,
) -> dict:
    """Submit a credit spread (CCS or PCS) as a combo (BAG) order.

    Structure:
      CCS (bearish): SELL short_call (lower strike) + BUY long_call (higher) for net credit
      PCS (bullish): SELL short_put (higher strike) + BUY long_put (lower) for net credit

    Exit rule (handled by spread_exit_watcher_loop):
      TP: close BAG at -$0.05 net debit (basically max profit, expire worthless)
      STOP: close BAG when current debit reaches stop_multiplier × entry_credit
    """
    live = is_live_trading_enabled() and confirm and isinstance(ibkr, LiveIBKRProvider)  # noqa: F821
    dry_run = not live

    # SAFETY GATE: same paper-account check as place_bracket.
    if live:
        try:
            await assert_paper_if_required(ibkr)
        except RuntimeError as e:
            await SignalIterRepository.write_audit_log(
                category="order_refused", actor="system",
                summary=f"REFUSED SPREAD: {e}",
                details={"reason": str(e), "kind": spec.kind, "qty": spec.qty,
                         "short": spec.short_strike, "long": spec.long_strike},
            )
            return {"ok": False, "error": str(e), "refused_safety_gate": True}

    # Persist the parent (spread bracket) record
    spread_order = LiveOrder(
        ticket_id=ticket_id, symbol=spec.symbol,
        strike=spec.short_strike,  # use short strike as primary
        right="C" if spec.kind == "CCS" else "P",
        expiry=spec.expiry, action="SELL", qty=spec.qty,
        order_type="LMT", limit_price=spec.entry_credit,
        bracket_role="parent", dry_run=dry_run,
        status="DRY_RUN" if dry_run else "SUBMITTED",
        exit_strategy="spread_premium",
        spread_kind=spec.kind,
        spread_short_strike=spec.short_strike,
        spread_long_strike=spec.long_strike,
        spread_initial_credit=spec.entry_credit,
    )
    saved = await SignalIterRepository.write_live_order(spread_order)

    await SignalIterRepository.write_audit_log(
        category="order", actor="user" if confirm else "system",
        summary=f"{'DRY_RUN' if dry_run else 'PLACED'} {spec.kind}: SELL {spec.short_strike} / BUY {spec.long_strike} × {spec.qty} for net credit ${spec.entry_credit:.2f}. TP@${spec.tp_close_debit:.2f}, STOP@{spec.stop_multiplier}× credit",
        details={"parent_order_id": saved.id, "kind": spec.kind,
                 "short": spec.short_strike, "long": spec.long_strike,
                 "credit": spec.entry_credit},
    )

    if dry_run:
        return {
            "ok": True, "dry_run": True, "parent_order_id": saved.id,
            "exit_strategy": "spread_premium",
            "reason": "DRY_RUN — spread exit watcher will simulate exits",
        }

    # Live: build BAG with ComboLegs
    try:
        from ib_async import Bag, ComboLeg, LimitOrder, Option
        ib = ibkr._ib  # type: ignore[attr-defined]
        # Qualify each leg to get conId
        right = "C" if spec.kind == "CCS" else "P"
        short_opt = Option(spec.symbol, spec.expiry, spec.short_strike, right,
                           "CBOE" if spec.symbol == "SPX" else "SMART", currency="USD")
        long_opt = Option(spec.symbol, spec.expiry, spec.long_strike, right,
                          "CBOE" if spec.symbol == "SPX" else "SMART", currency="USD")
        await ib.qualifyContractsAsync(short_opt, long_opt)

        bag = Bag(spec.symbol, exchange="CBOE" if spec.symbol == "SPX" else "SMART", currency="USD")
        bag.comboLegs = [
            ComboLeg(conId=short_opt.conId, ratio=1, action="SELL",
                     exchange="CBOE" if spec.symbol == "SPX" else "SMART"),
            ComboLeg(conId=long_opt.conId, ratio=1, action="BUY",
                     exchange="CBOE" if spec.symbol == "SPX" else "SMART"),
        ]

        # SELL the bag for net credit
        entry_order = LimitOrder("SELL", spec.qty, spec.entry_credit, transmit=True)
        trade = ib.placeOrder(bag, entry_order)
        await SignalIterRepository.update_live_order(
            saved.id, ib_order_id=trade.order.orderId,
            ib_perm_id=trade.order.permId,
        )
        return {"ok": True, "dry_run": False, "parent_order_id": saved.id,
                "exit_strategy": "spread_premium",
                "ib_order_id": trade.order.orderId}
    except Exception as e:
        logger.exception("Spread order submission failed")
        await SignalIterRepository.update_live_order(saved.id, status="REJECTED", notes=str(e))
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
