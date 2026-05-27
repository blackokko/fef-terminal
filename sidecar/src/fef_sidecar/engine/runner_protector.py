"""Runner protector — after T1 fills, watch for a retrace back to T1 fill
price and market-exit the runners (T2/T3 remaining qty) to lock in T1.

Activated by the user's trading style preference:
  • 85% of position exits at T1 (close-in target)
  • 15% kept as runners for T2/T3 bigger targets
  • If those runners RETRACE back below the T1 fill price, exit them
    immediately at market — never give back gains.

Loop runs every RUNNER_PROTECT_INTERVAL_SEC (default 5s). For each
parent order whose T1 child has filled:
  1. Look up the option contract's current bid
  2. If bid <= T1 fill price → cancel T2/T3, market-sell remaining qty
  3. Update LiveOrder records + audit log

Toggle: state.runner_protect_enabled (default True — user-requested
behavior). Disable per-session if you want classic T1/T2/T3 + stop.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


async def runner_protector_loop(state) -> None:
    """Poll filled brackets; exit runners on T1-retrace."""
    from fef_sidecar.db.models import LiveOrder
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from fef_sidecar.ibkr.models import Contract, ContractKind

    interval = float(os.environ.get("RUNNER_PROTECT_INTERVAL_SEC", "5"))
    logger.info("Runner protector loop starting (interval=%.1fs)", interval)

    # Per-parent state: parent_order_id -> {t1_fill_price: float, exited: bool}
    tracked: dict[int, dict] = {}

    while True:
        try:
            if not isinstance(state.ibkr, LiveIBKRProvider):
                await asyncio.sleep(interval)
                continue
            if not getattr(state, "runner_protect_enabled", True):
                await asyncio.sleep(interval)
                continue

            ib = state.ibkr._ib  # type: ignore[attr-defined]
            if ib is None or not ib.isConnected():
                await asyncio.sleep(interval)
                continue

            # Pull all parent orders that have been submitted (not dry_run)
            all_orders = await SignalIterRepository.query_live_orders(limit=200)
            parents = [o for o in all_orders
                       if o.bracket_role == "parent" and not o.dry_run
                       and o.status not in ("CANCELLED", "REJECTED", "CLOSED")]
            for p in parents:
                if p.id in tracked and tracked[p.id].get("exited"):
                    continue

                # Find children
                children = [o for o in all_orders if o.parent_id == p.id]
                t1 = next((o for o in children if o.bracket_role == "t1"), None)
                t2 = next((o for o in children if o.bracket_role == "t2"), None)
                t3 = next((o for o in children if o.bracket_role == "t3"), None)
                if t1 is None:
                    continue

                # T1 filled? (avg_fill_price > 0)
                if not t1.avg_fill_price or t1.avg_fill_price <= 0:
                    # Sync from IBKR — maybe filled but our DB record hasn't updated
                    if t1.ib_order_id:
                        trade = next((t for t in ib.trades() if t.order.orderId == t1.ib_order_id), None)
                        if trade and trade.orderStatus.avgFillPrice > 0:
                            t1.avg_fill_price = trade.orderStatus.avgFillPrice
                            t1.filled_qty = int(trade.orderStatus.filled)
                            await SignalIterRepository.update_live_order(
                                t1.id, avg_fill_price=t1.avg_fill_price,
                                filled_qty=t1.filled_qty,
                                status="FILLED" if t1.filled_qty >= t1.qty else "PARTIAL",
                            )
                if not t1.avg_fill_price or t1.avg_fill_price <= 0:
                    continue  # T1 not filled yet

                t1_fill = t1.avg_fill_price
                tracked.setdefault(p.id, {})["t1_fill_price"] = t1_fill

                # Check if there are still runners to protect
                runners = [r for r in (t2, t3) if r and r.status not in ("FILLED", "CANCELLED", "CLOSED")]
                if not runners:
                    tracked[p.id]["exited"] = True
                    continue

                # Get current option bid
                opt_contract = Contract(
                    symbol=p.symbol, kind=ContractKind.OPTION,
                    exchange="CBOE" if p.symbol == "SPX" else "SMART",
                    expiry=p.expiry, strike=p.strike, right=p.right,
                )
                try:
                    quote = await state.ibkr.get_option_quote(opt_contract)
                except Exception:
                    logger.exception("runner_protector: option quote fetch failed")
                    continue
                current_bid = quote.bid if quote.bid and quote.bid > 0 else quote.last
                if not current_bid or current_bid <= 0:
                    continue

                # Trigger? bid <= T1 fill price
                if current_bid <= t1_fill:
                    runner_qty = sum(r.qty - r.filled_qty for r in runners)
                    if runner_qty <= 0:
                        tracked[p.id]["exited"] = True
                        continue

                    logger.warning(
                        "RUNNER PROTECT triggered: %s %s%s%.0f current bid $%.2f <= T1 fill $%.2f. "
                        "Cancelling %d runners + market-sell %d contracts.",
                        p.symbol, "" if p.symbol == "SPX" else " ", p.right, p.strike,
                        current_bid, t1_fill, len(runners), runner_qty,
                    )

                    # Cancel runners
                    for r in runners:
                        if r.ib_order_id:
                            trade = next((t for t in ib.trades() if t.order.orderId == r.ib_order_id), None)
                            if trade:
                                ib.cancelOrder(trade.order)
                        await SignalIterRepository.update_live_order(
                            r.id, status="CANCELLED",
                            notes="runner_protector: T1 retrace exit",
                        )

                    # Market-sell remaining qty via the centralized safe-sell
                    # helper — refuses to short if positions() shows we don't
                    # hold enough long for the runner_qty (defense in depth on
                    # top of the in-process LiveOrder accounting).
                    try:
                        from fef_sidecar.ibkr.live import _to_ib_contract
                        from fef_sidecar.ibkr.safe_sell import safe_market_sell
                        ib_contract = _to_ib_contract(opt_contract)
                        await ib.qualifyContractsAsync(ib_contract)
                        result = await safe_market_sell(
                            ib, ib_contract, runner_qty,
                            purpose="runner_protect", state=state, parent_id=p.id,
                        )
                        if result.placed:
                            await SignalIterRepository.write_audit_log(
                                category="runner_protect", actor="system",
                                summary=f"Runner protector exited {result.sell_qty}× {p.symbol} {p.strike}{p.right} at market (T1 fill ${t1_fill:.2f}, bid ${current_bid:.2f})",
                                details={"parent_id": p.id, "runner_qty": result.sell_qty,
                                         "t1_fill_price": t1_fill, "trigger_bid": current_bid,
                                         "market_order_id": result.trade.order.orderId},
                            )
                            try:
                                await SignalIterRepository.write_watcher_sell_child(
                                    parent=p, bracket_role="trail_after_t1",
                                    qty=int(result.sell_qty),
                                    ib_order_id=int(result.trade.order.orderId),
                                    ib_perm_id=int(getattr(result.trade.order, "permId", 0) or 0) or None,
                                    notes=f"runner_protect t1_fill=${t1_fill:.2f},bid=${current_bid:.2f}",
                                )
                            except Exception:
                                logger.exception(
                                    "runner_protect: write_watcher_sell_child failed for parent %s",
                                    p.id,
                                )
                        # else: safe_market_sell already wrote an audit_log.
                    except Exception as e:
                        logger.exception("Market exit of runners failed")
                        await SignalIterRepository.write_audit_log(
                            category="runner_protect", actor="system",
                            summary=f"FAILED to market-exit runners: {e}",
                        )
                    tracked[p.id]["exited"] = True

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("runner_protector iteration failed")
        await asyncio.sleep(interval)
