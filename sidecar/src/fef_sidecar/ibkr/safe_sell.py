"""Centralized safe-sell helper. The ONLY way to place a SELL order on
options in the codebase. Enforces: cannot SELL more than current net-long
position for the contract. Prevents naked shorts structurally.

Background: 2026-05-22 incident. The auto-trader placed a naked short on
SPX 7520C via a cross-parent race in spx_stop_tick_watcher_loop. Commit
f78e8c3 patched the watcher paths. This module makes the same guard
structural across all sell paths — no SELL bypasses this check.

Usage:
    from fef_sidecar.ibkr.safe_sell import safe_market_sell
    result = await safe_market_sell(
        ib, contract, qty=3, purpose="watcher_close", state=state,
        parent_id=parent.id,
    )
    if result.placed:
        trade = result.trade
    else:
        # Refused — already audit-logged
        return

For credit-spread legs (the ONLY legitimate naked-short path), pass
force_short=True. Requires FEF_ALLOW_CREDIT_SPREADS=true env var or
the call will refuse anyway.

NOT routed through this helper:
    OCA bracket children placed inside place_bracket (T1/T2/T3 + stop).
    They are submitted at parent-BUY time and tied to the parent via
    IBKR's OCA mechanism — IBKR fires them server-side, not from our
    code, so wrapping them here is impossible without rewriting the
    bracket-order pattern. If OCA integrity ever breaks (parent
    canceled, child still fires) the system could produce a naked
    short. Mitigations:
      * Prefer exit_strategy='spx_points' (watcher-driven exits) over
        'option_price' brackets — every SELL goes through this helper.
      * Long-term: replace OCA brackets with watcher-driven exits.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SafeSellResult:
    placed: bool
    sell_qty: int                # actual qty placed (clamped)
    requested_qty: int           # original request
    net_long: int                # what positions() reported
    reason: str                  # "ok", "no_position", "clamped_to_long",
                                 # "credit_spread_gate_off", "short_blocked",
                                 # "credit_spread_leg", "placement_error"
    trade: Any | None = None     # IBKR Trade object when placed


def _contract_matches(pos_contract: Any, target_contract: Any) -> bool:
    """Whether an IBKR positions() contract refers to the same option
    as `target_contract`. Handles symbol / strike / right / expiry,
    tolerating both `expiry` and `lastTradeDateOrContractMonth` field
    spellings (our Contract uses `expiry`; ib_async uses the long form).
    """
    try:
        if pos_contract.symbol != target_contract.symbol:
            return False
        if float(pos_contract.strike) != float(target_contract.strike):
            return False
        if pos_contract.right != target_contract.right:
            return False
        pos_expiry = getattr(pos_contract, "lastTradeDateOrContractMonth", None)
        tgt_expiry = (
            getattr(target_contract, "lastTradeDateOrContractMonth", None)
            or getattr(target_contract, "expiry", None)
        )
        if pos_expiry is None or tgt_expiry is None:
            return False
        return pos_expiry == tgt_expiry
    except Exception:
        return False


async def safe_market_sell(
    ib: Any,
    contract: Any,
    qty: int,
    *,
    purpose: str,
    state: Any,
    parent_id: int | None = None,
    force_short: bool = False,
) -> SafeSellResult:
    """Place a SELL market order with structural naked-short prevention.

    Args:
        ib: IBKR client (live ib_async IB instance)
        contract: IBKR contract object (must have symbol, strike, right,
                  and either `lastTradeDateOrContractMonth` or `expiry`)
        qty: requested sell qty
        purpose: caller context for audit logs ("watcher_close" |
                 "bracket_close" | "dollar_cap" | "runner_protect" |
                 "credit_spread_short_leg" | "kill_switch" |
                 "orphan_sweep" | "panic_flat")
        state: AppState — used for firing_contracts reservation +
               audit_log writes
        parent_id: optional, threaded through for audit detail
        force_short: ONLY True for credit-spread legs. Requires
                     FEF_ALLOW_CREDIT_SPREADS=true env var.

    Returns SafeSellResult. If result.placed is False, the call has
    already written an audit_log row explaining why.
    """
    from ib_async import MarketOrder
    from fef_sidecar.db.repository import SignalIterRepository

    # Credit-spread leg path — additionally gated by env var
    if force_short:
        gate = os.environ.get("FEF_ALLOW_CREDIT_SPREADS", "false").lower() in (
            "true", "1", "yes", "on",
        )
        if not gate:
            from fef_sidecar.engine.auto_trader import BlockReason as _BR
            await SignalIterRepository.write_audit_log(
                category="safe_sell_blocked", actor="system",
                summary=(
                    f"CREDIT_SPREAD_GATE: refusing to SELL {qty}x "
                    f"{contract.symbol} {contract.strike}{contract.right} — "
                    "FEF_ALLOW_CREDIT_SPREADS env var is not set. Naked-short "
                    "paths require explicit opt-in."
                ),
                details={"purpose": purpose, "qty": qty, "parent_id": parent_id},
                block_reason_code=_BR.CREDIT_SPREAD_GATE_OFF,
            )
            return SafeSellResult(
                placed=False, sell_qty=0, requested_qty=qty, net_long=0,
                reason="credit_spread_gate_off",
            )
        # force_short bypasses the net_long check below — caller has accepted
        # responsibility for the short leg via spread composition.
        try:
            mkt = MarketOrder("SELL", qty)
            trade = ib.placeOrder(contract, mkt)
            logger.info(
                "safe_market_sell placed credit-spread short leg: %s %s%s qty=%d",
                contract.symbol, contract.strike, contract.right, qty,
            )
            return SafeSellResult(
                placed=True, sell_qty=qty, requested_qty=qty, net_long=0,
                reason="credit_spread_leg", trade=trade,
            )
        except Exception as e:
            from fef_sidecar.engine.auto_trader import BlockReason as _BR
            await SignalIterRepository.write_audit_log(
                category="safe_sell_error", actor="system",
                summary=f"Credit-spread leg placeOrder failed: {e}",
                details={"purpose": purpose, "qty": qty, "parent_id": parent_id},
                block_reason_code=_BR.SAFE_SELL_ERROR,
            )
            return SafeSellResult(
                placed=False, sell_qty=0, requested_qty=qty, net_long=0,
                reason="placement_error",
            )

    # Standard closing-sell path — must have a long position to close
    positions = ib.positions()
    net_long = 0
    for pos in positions:
        if _contract_matches(pos.contract, contract):
            try:
                net_long += int(pos.position)
            except Exception:
                continue

    max_sellable = max(0, net_long)
    sell_qty = min(qty, max_sellable)

    if sell_qty <= 0:
        from fef_sidecar.engine.auto_trader import BlockReason as _BR
        _code = _BR.SAFE_SELL_NO_POSITION if net_long == 0 else _BR.SAFE_SELL_SHORT_BLOCKED
        await SignalIterRepository.write_audit_log(
            category="safe_sell_blocked", actor="system",
            summary=(
                f"STRUCTURAL_BLOCK: refusing to SELL {qty}x "
                f"{contract.symbol} {contract.strike}{contract.right} — "
                f"net_long={net_long}, would create a naked short. "
                f"purpose={purpose}"
            ),
            details={
                "purpose": purpose, "qty": qty, "net_long": net_long,
                "parent_id": parent_id,
            },
            block_reason_code=_code,
        )
        return SafeSellResult(
            placed=False, sell_qty=0, requested_qty=qty, net_long=net_long,
            reason="no_position" if net_long == 0 else "short_blocked",
        )

    # Place the clamped sell. ONLY the placeOrder call may yield
    # placement_error — once the order is transmitted the success result is
    # constructed IMMEDIATELY, and no awaited write below may flip it
    # (2026-07-07 review: a raised clamped-audit write AFTER placeOrder
    # returned placed=False, so callers rolled back their latch and re-sold
    # an already-transmitted order).
    try:
        mkt = MarketOrder("SELL", sell_qty)
        trade = ib.placeOrder(contract, mkt)
    except Exception as e:
        from fef_sidecar.engine.auto_trader import BlockReason as _BR
        await SignalIterRepository.write_audit_log(
            category="safe_sell_error", actor="system",
            summary=f"Safe-sell placeOrder failed: {e}",
            details={
                "purpose": purpose, "qty": qty, "net_long": net_long,
                "parent_id": parent_id,
            },
            block_reason_code=_BR.SAFE_SELL_ERROR,
        )
        return SafeSellResult(
            placed=False, sell_qty=0, requested_qty=qty, net_long=net_long,
            reason="placement_error",
        )
    result = SafeSellResult(
        placed=True, sell_qty=sell_qty, requested_qty=qty, net_long=net_long,
        reason="ok" if sell_qty == qty else "clamped_to_long", trade=trade,
    )
    logger.info(
        "safe_market_sell: %s %s%s sell_qty=%d (requested=%d, net_long=%d, purpose=%s)",
        contract.symbol, contract.strike, contract.right,
        sell_qty, qty, net_long, purpose,
    )
    if sell_qty < qty:
        try:
            from fef_sidecar.engine.auto_trader import BlockReason as _BR
            await SignalIterRepository.write_audit_log(
                category="safe_sell_clamped", actor="system",
                summary=(
                    f"CLAMPED: {contract.symbol} {contract.strike}{contract.right} — "
                    f"requested {qty}, sold {sell_qty} (net_long was {net_long}). "
                    f"purpose={purpose}"
                ),
                details={
                    "purpose": purpose, "requested_qty": qty,
                    "sell_qty": sell_qty, "net_long": net_long,
                    "parent_id": parent_id,
                },
                block_reason_code=_BR.SAFE_SELL_CLAMPED,
            )
        except Exception:
            logger.exception(
                "safe_market_sell: clamped-audit write failed (order already placed)",
            )
    return result
