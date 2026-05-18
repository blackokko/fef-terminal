"""SPX-target watcher — Option C exit strategy for long calls/puts.

When a parent order is placed with exit_strategy='spx_points', the bracket
has NO T1/T2/T3 children — instead this loop watches live SPX ticks and
fires market sells when SPX crosses each target level.

Advantages over option-price brackets:
  - No gamma / theta / IV approximation needed
  - SPX cross is exact, deterministic
  - Same logic for any DTE (no time-decay distortion)

Mechanics per parent order:
  Direction LONG_CALLS:
    T1 fires when SPX >= spx_target_t1  → market-sell t1_qty
    T2 fires when SPX >= spx_target_t2  → market-sell t2_qty
    T3 fires when SPX >= spx_target_t3  → market-sell t3_qty
    STOP fires when SPX <= spx_target_stop → market-sell ALL remaining
    After T1 fires, if SPX retraces back to spx_target_t1_at_spx → market-sell remaining (runner_protect)

  Direction LONG_PUTS: inverse comparisons.

Poll rate: SPX_WATCHER_INTERVAL_SEC (default 2s). Sub-second would be
nicer but option-quote fetches add latency.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


async def spx_target_watcher_loop(state) -> None:
    from fef_sidecar.db.models import LiveOrder
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from fef_sidecar.ibkr.models import Contract, ContractKind

    interval = float(os.environ.get("SPX_WATCHER_INTERVAL_SEC", "2"))
    logger.info("SPX target watcher loop starting (interval=%.1fs)", interval)

    while True:
        try:
            spot = state.last_live_tick_spx or state.last_spx_price
            if spot <= 0:
                await asyncio.sleep(interval)
                continue

            parents = [
                o for o in await SignalIterRepository.query_live_orders(limit=200)
                if o.bracket_role == "parent"
                and o.exit_strategy == "spx_points"
                and o.status in ("SUBMITTED", "FILLED", "PARTIAL")
            ]

            # Throttle dollar-loss premium fetches — once every ~10s per parent
            # to avoid hammering IBKR with option_quote calls.
            now_ms = int(asyncio.get_running_loop().time() * 1000)
            if not hasattr(state, "_dollar_cap_last_check_ms"):
                state._dollar_cap_last_check_ms = {}  # type: ignore[attr-defined]

            for p in parents:
                is_long_calls = p.right == "C"

                async def _market_sell(qty: int, tier_label: str) -> None:
                    if qty <= 0:
                        return
                    if not isinstance(state.ibkr, LiveIBKRProvider):
                        await SignalIterRepository.write_audit_log(
                            category="spx_watcher", actor="system",
                            summary=f"[DRY] {tier_label} crossed: would market-sell-to-close {qty}× {p.symbol} {p.strike}{p.right} at SPX {spot:.2f}",
                        )
                        return
                    try:
                        from ib_async import MarketOrder
                        from fef_sidecar.ibkr.live import _to_ib_contract
                        ib = state.ibkr._ib  # type: ignore[attr-defined]
                        opt_contract = Contract(
                            symbol=p.symbol, kind=ContractKind.OPTION,
                            exchange="CBOE" if p.symbol == "SPX" else "SMART",
                            expiry=p.expiry, strike=p.strike, right=p.right,
                        )
                        ic = _to_ib_contract(opt_contract)
                        await ib.qualifyContractsAsync(ic)

                        # SAFETY: never sell more than we own. If parent BUY
                        # didn't fill (rejection / partial / illiquid), don't
                        # accidentally open a SHORT by selling unbacked qty.
                        positions = ib.positions()
                        long_qty = 0
                        for pos in positions:
                            c = pos.contract
                            if (getattr(c, "secType", "") == "OPT"
                                and getattr(c, "symbol", "") == p.symbol
                                and abs(float(getattr(c, "strike", 0)) - p.strike) < 0.01
                                and getattr(c, "right", "") == p.right
                                and getattr(c, "lastTradeDateOrContractMonth", "") == p.expiry
                                and pos.position > 0):
                                long_qty += int(pos.position)
                        sell_qty = min(qty, long_qty)
                        if sell_qty <= 0:
                            await SignalIterRepository.write_audit_log(
                                category="spx_watcher", actor="system",
                                summary=f"{tier_label} SKIPPED: no long position to close for {p.symbol} {p.strike}{p.right} (requested {qty}, owned 0) — refusing to short",
                            )
                            return
                        if sell_qty < qty:
                            await SignalIterRepository.write_audit_log(
                                category="spx_watcher", actor="system",
                                summary=f"{tier_label} PARTIAL: only own {sell_qty}/{qty} for {p.symbol} {p.strike}{p.right} — selling owned qty only",
                            )

                        mkt = MarketOrder("SELL", sell_qty, transmit=True)
                        trade = ib.placeOrder(ic, mkt)
                        await SignalIterRepository.write_audit_log(
                            category="spx_watcher", actor="system",
                            summary=f"{tier_label}: market-sold-to-close {sell_qty}× {p.symbol} {p.strike}{p.right} at SPX {spot:.2f} (ib_order_id={trade.order.orderId})",
                        )
                    except Exception:
                        logger.exception("spx_watcher market sell failed")

                # Dollar-loss cap (CRITICAL — checked first, before SPX stop)
                if (p.dollar_loss_cap is not None
                    and p.limit_price is not None
                    and p.status not in ("STOPPED",)):
                    last_check = state._dollar_cap_last_check_ms.get(p.id, 0)  # type: ignore[attr-defined]
                    if now_ms - last_check >= 10000:  # every 10s
                        state._dollar_cap_last_check_ms[p.id] = now_ms  # type: ignore[attr-defined]
                        try:
                            opt_quote = await state.ibkr.get_option_quote(Contract(
                                symbol=p.symbol, kind=ContractKind.OPTION,
                                exchange="CBOE" if p.symbol == "SPX" else "SMART",
                                expiry=p.expiry, strike=p.strike, right=p.right,
                            ))
                            mid = opt_quote.mid
                            if mid is not None and mid > 0:
                                # Compute open-position $ loss vs entry. Sum unfired qty.
                                open_qty = 0
                                if not p.spx_target_t1_fired and p.spx_target_t1_qty:
                                    open_qty += p.spx_target_t1_qty
                                if not p.spx_target_t2_fired and p.spx_target_t2_qty:
                                    open_qty += p.spx_target_t2_qty
                                if not p.spx_target_t3_fired and p.spx_target_t3_qty:
                                    open_qty += p.spx_target_t3_qty
                                if open_qty > 0:
                                    pnl = (mid - p.limit_price) * open_qty * 100
                                    if pnl <= p.dollar_loss_cap:
                                        await _market_sell(open_qty, f"DOLLAR_STOP(${pnl:.0f})")
                                        await SignalIterRepository.update_live_order(
                                            p.id, status="STOPPED",
                                            spx_target_t1_fired=True,
                                            spx_target_t2_fired=True,
                                            spx_target_t3_fired=True,
                                        )
                                        continue
                        except Exception:
                            logger.exception("dollar_loss_cap check failed for parent %s", p.id)

                # Stop check first (most critical)
                if p.spx_target_stop is not None:
                    stop_hit = (
                        (is_long_calls and spot <= p.spx_target_stop)
                        or (not is_long_calls and spot >= p.spx_target_stop)
                    )
                    if stop_hit:
                        # Compute remaining qty across all tiers
                        unfired_qty = 0
                        if not p.spx_target_t1_fired and p.spx_target_t1_qty:
                            unfired_qty += p.spx_target_t1_qty
                        if not p.spx_target_t2_fired and p.spx_target_t2_qty:
                            unfired_qty += p.spx_target_t2_qty
                        if not p.spx_target_t3_fired and p.spx_target_t3_qty:
                            unfired_qty += p.spx_target_t3_qty
                        if unfired_qty > 0:
                            await _market_sell(unfired_qty, "STOP")
                            await SignalIterRepository.update_live_order(
                                p.id, status="STOPPED",
                                spx_target_t1_fired=True,
                                spx_target_t2_fired=True,
                                spx_target_t3_fired=True,
                            )
                        continue  # nothing else to do on this parent

                # T1
                if not p.spx_target_t1_fired and p.spx_target_t1 is not None and p.spx_target_t1_qty:
                    t1_hit = (
                        (is_long_calls and spot >= p.spx_target_t1)
                        or (not is_long_calls and spot <= p.spx_target_t1)
                    )
                    if t1_hit:
                        await _market_sell(p.spx_target_t1_qty, "T1")
                        await SignalIterRepository.update_live_order(
                            p.id, spx_target_t1_fired=True, spx_target_t1_at_spx=spot,
                        )

                # T2
                if not p.spx_target_t2_fired and p.spx_target_t2 is not None and p.spx_target_t2_qty:
                    t2_hit = (
                        (is_long_calls and spot >= p.spx_target_t2)
                        or (not is_long_calls and spot <= p.spx_target_t2)
                    )
                    if t2_hit:
                        await _market_sell(p.spx_target_t2_qty, "T2")
                        await SignalIterRepository.update_live_order(
                            p.id, spx_target_t2_fired=True,
                        )

                # T3
                if not p.spx_target_t3_fired and p.spx_target_t3 is not None and p.spx_target_t3_qty:
                    t3_hit = (
                        (is_long_calls and spot >= p.spx_target_t3)
                        or (not is_long_calls and spot <= p.spx_target_t3)
                    )
                    if t3_hit:
                        await _market_sell(p.spx_target_t3_qty, "T3")
                        await SignalIterRepository.update_live_order(
                            p.id, spx_target_t3_fired=True,
                        )

                # Runner protect: if T1 has fired but T2/T3 are still open
                # and SPX retraces back to where T1 fired → exit runners
                if (state.runner_protect_enabled and p.spx_target_t1_fired
                    and p.spx_target_t1_at_spx is not None
                    and (not p.spx_target_t2_fired or not p.spx_target_t3_fired)):
                    retrace_hit = (
                        (is_long_calls and spot <= p.spx_target_t1_at_spx)
                        or (not is_long_calls and spot >= p.spx_target_t1_at_spx)
                    )
                    if retrace_hit:
                        runner_qty = 0
                        if not p.spx_target_t2_fired and p.spx_target_t2_qty:
                            runner_qty += p.spx_target_t2_qty
                        if not p.spx_target_t3_fired and p.spx_target_t3_qty:
                            runner_qty += p.spx_target_t3_qty
                        if runner_qty > 0:
                            await _market_sell(runner_qty, "RUNNER_PROTECT")
                            await SignalIterRepository.update_live_order(
                                p.id, spx_target_t2_fired=True, spx_target_t3_fired=True,
                            )

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("spx_target_watcher iteration failed")
        await asyncio.sleep(interval)
