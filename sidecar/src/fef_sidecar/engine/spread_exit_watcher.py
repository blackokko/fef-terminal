"""Spread exit watcher — handles TP + stop for credit spreads (CCS/PCS).

For each open spread (exit_strategy='spread_premium', status SUBMITTED/FILLED):
  - Fetch current bid/ask of both legs
  - Compute net spread debit/credit at current prices
  - TP rule: if current debit-to-close <= tp_close_debit ($0.05) → close
  - Stop rule: if current debit-to-close >= stop_multiplier × initial_credit → close

Close = BUY the same BAG at current debit (covers both legs).
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


async def spread_exit_watcher_loop(state) -> None:
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from fef_sidecar.ibkr.models import Contract, ContractKind

    interval = float(os.environ.get("SPREAD_WATCHER_INTERVAL_SEC", "5"))
    logger.info("Spread exit watcher loop starting (interval=%.1fs)", interval)

    while True:
        try:
            spreads = [
                o for o in await SignalIterRepository.query_live_orders(limit=200)
                if o.exit_strategy == "spread_premium"
                and o.bracket_role == "parent"
                and o.status in ("SUBMITTED", "FILLED", "PARTIAL")
            ]
            for sp in spreads:
                if not (sp.spread_short_strike and sp.spread_long_strike and sp.spread_initial_credit):
                    continue

                # Get current bid/ask of each leg
                short_contract = Contract(
                    symbol=sp.symbol, kind=ContractKind.OPTION,
                    exchange="CBOE" if sp.symbol == "SPX" else "SMART",
                    expiry=sp.expiry, strike=sp.spread_short_strike, right=sp.right,
                )
                long_contract = Contract(
                    symbol=sp.symbol, kind=ContractKind.OPTION,
                    exchange="CBOE" if sp.symbol == "SPX" else "SMART",
                    expiry=sp.expiry, strike=sp.spread_long_strike, right=sp.right,
                )
                try:
                    short_q = await state.ibkr.get_option_quote(short_contract)
                    long_q = await state.ibkr.get_option_quote(long_contract)
                except Exception:
                    continue
                if not short_q.ask or not long_q.bid:
                    continue

                # To close the spread: BUY the short leg + SELL the long leg
                # Net debit = short_ask - long_bid (conservative — fillable)
                current_debit = short_q.ask - long_q.bid
                tp_threshold = state.spread_tp_close_debit
                stop_threshold = state.spread_stop_multiplier * sp.spread_initial_credit

                fired = None
                if current_debit <= tp_threshold:
                    fired = "TP"
                elif current_debit >= stop_threshold:
                    fired = "STOP"

                if fired is None:
                    continue

                logger.warning(
                    "SPREAD EXIT %s: %s short=%.0f long=%.0f current_debit=$%.2f (entry credit $%.2f)",
                    fired, sp.spread_kind, sp.spread_short_strike, sp.spread_long_strike,
                    current_debit, sp.spread_initial_credit,
                )

                if not isinstance(state.ibkr, LiveIBKRProvider):
                    await SignalIterRepository.write_audit_log(
                        category="spread_exit", actor="system",
                        summary=f"[DRY] {fired} on {sp.spread_kind} {sp.spread_short_strike}/{sp.spread_long_strike}: current debit ${current_debit:.2f}",
                    )
                    await SignalIterRepository.update_live_order(sp.id, status="CLOSED")
                    continue

                # Live close: BUY the bag at current debit
                try:
                    from ib_async import Bag, ComboLeg, LimitOrder, Option
                    from fef_sidecar.ibkr.live import _to_ib_contract
                    ib = state.ibkr._ib  # type: ignore[attr-defined]
                    short_opt = Option(sp.symbol, sp.expiry, sp.spread_short_strike, sp.right,
                                       "CBOE" if sp.symbol == "SPX" else "SMART", currency="USD")
                    long_opt = Option(sp.symbol, sp.expiry, sp.spread_long_strike, sp.right,
                                      "CBOE" if sp.symbol == "SPX" else "SMART", currency="USD")
                    await ib.qualifyContractsAsync(short_opt, long_opt)
                    bag = Bag(symbol=sp.symbol, exchange="CBOE" if sp.symbol == "SPX" else "SMART", currency="USD")
                    bag.comboLegs = [
                        ComboLeg(conId=short_opt.conId, ratio=1, action="BUY",
                                 exchange="CBOE" if sp.symbol == "SPX" else "SMART"),
                        ComboLeg(conId=long_opt.conId, ratio=1, action="SELL",
                                 exchange="CBOE" if sp.symbol == "SPX" else "SMART"),
                    ]
                    close_order = LimitOrder("BUY", sp.qty, current_debit + 0.05, transmit=True)
                    trade = ib.placeOrder(bag, close_order)
                    await SignalIterRepository.update_live_order(
                        sp.id, status="CLOSED",
                        notes=f"{fired} fired at debit ${current_debit:.2f}",
                    )
                    await SignalIterRepository.write_audit_log(
                        category="spread_exit", actor="system",
                        summary=f"{fired}: closed {sp.spread_kind} {sp.spread_short_strike}/{sp.spread_long_strike} × {sp.qty} at debit ${current_debit:.2f} (entry credit ${sp.spread_initial_credit:.2f})",
                        details={"close_order_id": trade.order.orderId},
                    )
                except Exception:
                    logger.exception("Spread close failed")

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("spread_exit_watcher iteration failed")
        await asyncio.sleep(interval)
