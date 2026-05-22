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

Poll rate: SPX_WATCHER_INTERVAL_SEC (default 0.1s). The hot path
(stop / T1 / T2 / T3 detection) is pure arithmetic against the cached
live SPX tick and does not require any IBKR round-trip, so a tight
loop is cheap. The IBKR round-trips (qualify + place) only happen
when a level is actually crossed, and the qualified contract is
cached after the first lookup so subsequent fires skip qualify
entirely. Reduce only if event-loop CPU pressure shows up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time as _time

logger = logging.getLogger(__name__)


async def _qualified_ib_contract(state, p):
    """Cached IB option-contract lookup. qualifyContractsAsync round-trips
    to IBKR (~50-300ms) — caching it cuts that latency to ~0 for every
    market-sell after the first. Cache key = (symbol, strike, right,
    expiry). Cache lives on `state` and is hot for the sidecar lifetime."""
    from fef_sidecar.ibkr.live import _to_ib_contract
    from fef_sidecar.ibkr.models import Contract, ContractKind
    if not hasattr(state, "_qualified_ib_contracts"):
        state._qualified_ib_contracts = {}  # type: ignore[attr-defined]
    key = (p.symbol, float(p.strike), p.right, p.expiry)
    cached = state._qualified_ib_contracts.get(key)  # type: ignore[attr-defined]
    if cached is not None:
        return cached
    opt_contract = Contract(
        symbol=p.symbol, kind=ContractKind.OPTION,
        exchange="CBOE" if p.symbol == "SPX" else "SMART",
        expiry=p.expiry, strike=p.strike, right=p.right,
    )
    ic = _to_ib_contract(opt_contract)
    ib = state.ibkr._ib  # type: ignore[attr-defined]
    await ib.qualifyContractsAsync(ic)
    state._qualified_ib_contracts[key] = ic  # type: ignore[attr-defined]
    return ic


async def _check_dollar_loss(state, p) -> None:
    """Out-of-band dollar-loss cap check. Spawned as a background task so
    it never blocks the SPX target evaluation loop. If the option premium
    has dropped enough that open-qty loss exceeds the cap, market-exit
    remaining qty.
    """
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from fef_sidecar.ibkr.models import Contract, ContractKind
    try:
        opt_quote = await state.ibkr.get_option_quote(Contract(
            symbol=p.symbol, kind=ContractKind.OPTION,
            exchange="CBOE" if p.symbol == "SPX" else "SMART",
            expiry=p.expiry, strike=p.strike, right=p.right,
        ))
        mid = opt_quote.mid
        if mid is None or mid <= 0:
            return
        open_qty = 0
        if not p.spx_target_t1_fired and p.spx_target_t1_qty:
            open_qty += p.spx_target_t1_qty
        if not p.spx_target_t2_fired and p.spx_target_t2_qty:
            open_qty += p.spx_target_t2_qty
        if not p.spx_target_t3_fired and p.spx_target_t3_qty:
            open_qty += p.spx_target_t3_qty
        if open_qty <= 0:
            return
        pnl = (mid - p.limit_price) * open_qty * 100
        if pnl > p.dollar_loss_cap:
            return
        # Cap breached — fire market sell directly (mirrors the watcher's
        # _market_sell logic but inlined since we're out-of-band).
        if not isinstance(state.ibkr, LiveIBKRProvider):
            await SignalIterRepository.write_audit_log(
                category="spx_watcher", actor="system",
                summary=f"[DRY] DOLLAR_STOP(${pnl:.0f}): would market-sell {open_qty}× {p.symbol} {p.strike}{p.right}",
            )
            return
        from ib_async import MarketOrder
        ib = state.ibkr._ib  # type: ignore[attr-defined]
        ic = await _qualified_ib_contract(state, p)
        positions = ib.positions()
        long_qty = sum(
            int(pos.position) for pos in positions
            if (getattr(pos.contract, "secType", "") == "OPT"
                and getattr(pos.contract, "symbol", "") == p.symbol
                and abs(float(getattr(pos.contract, "strike", 0)) - p.strike) < 0.01
                and getattr(pos.contract, "right", "") == p.right
                and getattr(pos.contract, "lastTradeDateOrContractMonth", "") == p.expiry
                and pos.position > 0)
        )
        # PATCH 1: contract-level pending-sell reservation (see _market_sell).
        contract_key = (p.symbol, float(p.strike), p.right, p.expiry)
        pending = state.firing_contracts.get(contract_key, 0)
        available = max(0, long_qty - pending)
        sell_qty = min(open_qty, available)
        if sell_qty <= 0:
            await SignalIterRepository.write_audit_log(
                category="spx_watcher", actor="system",
                summary=(
                    f"DOLLAR_STOP SKIPPED: 0 available after reservation "
                    f"(long={long_qty}, pending={pending}, requested={open_qty}) "
                    f"for {p.symbol} {p.strike}{p.right} — refusing to short"
                ),
            )
            return

        # PATCH 2: HARD-BLOCK structural check (defense in depth).
        fresh_positions = ib.positions()
        net_long = 0
        for pos in fresh_positions:
            c = pos.contract
            if (getattr(c, "symbol", "") == p.symbol
                    and abs(float(getattr(c, "strike", 0)) - float(p.strike)) < 0.01
                    and getattr(c, "right", "") == p.right
                    and getattr(c, "lastTradeDateOrContractMonth", "") == p.expiry):
                net_long += int(pos.position)
        max_sellable = max(0, net_long)
        if sell_qty > max_sellable:
            sell_qty = max_sellable
        if sell_qty <= 0:
            await SignalIterRepository.write_audit_log(
                category="spx_watcher", actor="system",
                summary=(
                    f"HARD-BLOCK: refusing to short {p.symbol} {p.strike}{p.right} "
                    f"— net_long={net_long}, requested={open_qty} (DOLLAR_STOP)"
                ),
            )
            return

        # Reserve before placing. Spawn a 2s-delayed decrement.
        state.firing_contracts[contract_key] = pending + sell_qty

        async def _release_reservation(key, amount):
            try:
                await asyncio.sleep(2.0)
                cur = state.firing_contracts.get(key, 0)
                state.firing_contracts[key] = max(0, cur - amount)
            except Exception:
                pass

        asyncio.create_task(_release_reservation(contract_key, sell_qty))

        mkt = MarketOrder("SELL", sell_qty, transmit=True)
        trade = ib.placeOrder(ic, mkt)
        # status=STOPPED is the resolved-marker. Don't overwrite the per-tier
        # fired flags as cleanup — they record whether each tier was banked
        # as a partial, not whether the trade resolved. (See poll-loop branch
        # for the full rationale.)
        await SignalIterRepository.update_live_order(
            p.id, status="STOPPED",
        )
        await SignalIterRepository.write_audit_log(
            category="spx_watcher", actor="system",
            summary=f"DOLLAR_STOP(${pnl:.0f}): market-sold-to-close {sell_qty}× {p.symbol} {p.strike}{p.right} (ib_order_id={trade.order.orderId})",
        )
    except Exception:
        logger.exception("dollar-loss check failed for parent %s", p.id)


async def spx_target_watcher_loop(state) -> None:
    from fef_sidecar.db.models import LiveOrder
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from fef_sidecar.ibkr.models import Contract, ContractKind

    interval = float(os.environ.get("SPX_WATCHER_INTERVAL_SEC", "0.1"))
    logger.info("SPX target watcher loop starting (interval=%.2fs)", interval)
    # Tick-age threshold for stale-data warnings (audit-logged). If the
    # cached SPX hasn't refreshed in this long, the IBKR tick stream is
    # likely stalled and stop detection may be acting on stale data.
    stale_tick_warn_ms = int(os.environ.get("SPX_WATCHER_STALE_TICK_MS", "1500"))
    # Per-parent log throttle so the "spot near stop" diagnostic doesn't
    # flood the audit_log when iterating at 0.1s.
    last_near_stop_log_ms: dict[int, int] = {}
    NEAR_STOP_LOG_INTERVAL_MS = 5000  # 5s between near-stop diagnostics per parent

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

            now_ms = int(asyncio.get_running_loop().time() * 1000)
            if not hasattr(state, "_dollar_cap_last_check_ms"):
                state._dollar_cap_last_check_ms = {}  # type: ignore[attr-defined]

            # Spawn dollar-loss-cap check as a background task per parent.
            # Doesn't block SPX-target evaluation, throttled per parent to 10s.
            for p in parents:
                if (p.dollar_loss_cap is not None
                    and p.limit_price is not None
                    and p.status not in ("STOPPED",)):
                    last_check = state._dollar_cap_last_check_ms.get(p.id, 0)  # type: ignore[attr-defined]
                    if now_ms - last_check >= 10000:
                        state._dollar_cap_last_check_ms[p.id] = now_ms  # type: ignore[attr-defined]
                        asyncio.create_task(_check_dollar_loss(state, p))

            # Default BE ladder. Configurable per-deployment via env if needed.
            # Each tuple: (trigger_R, new_stop_at_R_from_entry)
            # 0.5R reached -> stop to entry (BE)
            # 1.0R reached -> stop to +0.5R (locks half)
            # 1.5R reached -> stop to +1.0R (locks 1R)
            BE_LADDER = [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)]

            for p in parents:
                is_long_calls = p.right == "C"

                # BE ladder: check current MFE in R, ratchet stop level up.
                # Pure state change — no IBKR call, no order placement, fast.
                if (p.entry_spx_price is not None and p.spx_target_stop is not None
                    and p.dynamic_stop_level is not None):
                    risk = abs(p.entry_spx_price - p.spx_target_stop)
                    if risk > 0:
                        if is_long_calls:
                            mfe_R = (spot - p.entry_spx_price) / risk
                        else:
                            mfe_R = (p.entry_spx_price - spot) / risk
                        for trig_R, new_stop_R in BE_LADDER:
                            if mfe_R >= trig_R and p.be_ladder_max_step_R < trig_R:
                                # Ratchet stop. For longs: stop moves UP. For puts: DOWN.
                                if is_long_calls:
                                    new_stop = p.entry_spx_price + new_stop_R * risk
                                    if new_stop > (p.dynamic_stop_level or -1e9):
                                        await SignalIterRepository.update_live_order(
                                            p.id, dynamic_stop_level=new_stop,
                                            be_ladder_max_step_R=trig_R,
                                        )
                                        p.dynamic_stop_level = new_stop
                                        p.be_ladder_max_step_R = trig_R
                                        await SignalIterRepository.write_audit_log(
                                            category="spx_watcher", actor="system",
                                            summary=f"BE LADDER step {trig_R}R: stop moved to {new_stop:.2f} (locks {new_stop_R}R) on {p.symbol} {p.strike}{p.right}",
                                        )
                                else:
                                    new_stop = p.entry_spx_price - new_stop_R * risk
                                    if new_stop < (p.dynamic_stop_level or 1e9):
                                        await SignalIterRepository.update_live_order(
                                            p.id, dynamic_stop_level=new_stop,
                                            be_ladder_max_step_R=trig_R,
                                        )
                                        p.dynamic_stop_level = new_stop
                                        p.be_ladder_max_step_R = trig_R
                                        await SignalIterRepository.write_audit_log(
                                            category="spx_watcher", actor="system",
                                            summary=f"BE LADDER step {trig_R}R: stop moved to {new_stop:.2f} (locks {new_stop_R}R) on {p.symbol} {p.strike}{p.right}",
                                        )

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
                        ib = state.ibkr._ib  # type: ignore[attr-defined]
                        ic = await _qualified_ib_contract(state, p)

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
                        # PATCH 1: contract-level pending-sell reservation. If
                        # another parent on the same contract has already placed
                        # a SELL whose fill hasn't propagated through
                        # ib.positions() yet, subtract that reserved qty from
                        # available_long so we don't double-sell into a short.
                        contract_key = (p.symbol, float(p.strike), p.right, p.expiry)
                        pending = state.firing_contracts.get(contract_key, 0)
                        available = max(0, long_qty - pending)
                        sell_qty = min(qty, available)
                        if sell_qty <= 0:
                            await SignalIterRepository.write_audit_log(
                                category="spx_watcher", actor="system",
                                summary=(
                                    f"{tier_label} SKIPPED: 0 available after reservation "
                                    f"(long={long_qty}, pending={pending}, requested={qty}) "
                                    f"for {p.symbol} {p.strike}{p.right} — refusing to short"
                                ),
                            )
                            return
                        if sell_qty < qty:
                            await SignalIterRepository.write_audit_log(
                                category="spx_watcher", actor="system",
                                summary=(
                                    f"{tier_label} PARTIAL: clamped to {sell_qty}/{qty} for "
                                    f"{p.symbol} {p.strike}{p.right} "
                                    f"(long={long_qty}, pending={pending})"
                                ),
                            )

                        # PATCH 2: HARD-BLOCK structural check (defense in depth).
                        # Re-read positions and clamp sell_qty to net_long.
                        # Catches any race that bypasses Patch 1's reservation.
                        fresh_positions = ib.positions()
                        net_long = 0
                        for pos in fresh_positions:
                            c = pos.contract
                            if (getattr(c, "symbol", "") == p.symbol
                                    and abs(float(getattr(c, "strike", 0)) - float(p.strike)) < 0.01
                                    and getattr(c, "right", "") == p.right
                                    and getattr(c, "lastTradeDateOrContractMonth", "") == p.expiry):
                                net_long += int(pos.position)
                        max_sellable = max(0, net_long)
                        if sell_qty > max_sellable:
                            sell_qty = max_sellable
                        if sell_qty <= 0:
                            await SignalIterRepository.write_audit_log(
                                category="spx_watcher", actor="system",
                                summary=(
                                    f"HARD-BLOCK: refusing to short {p.symbol} {p.strike}{p.right} "
                                    f"— net_long={net_long}, requested={qty} ({tier_label})"
                                ),
                            )
                            return

                        # Reserve before placing. Spawn a 2s-delayed decrement so
                        # the reservation gets released after the SELL has had
                        # time to propagate through ib.positions().
                        state.firing_contracts[contract_key] = pending + sell_qty

                        async def _release_reservation(key, amount):
                            try:
                                await asyncio.sleep(2.0)
                                cur = state.firing_contracts.get(key, 0)
                                state.firing_contracts[key] = max(0, cur - amount)
                            except Exception:
                                pass

                        asyncio.create_task(_release_reservation(contract_key, sell_qty))

                        mkt = MarketOrder("SELL", sell_qty, transmit=True)
                        trade = ib.placeOrder(ic, mkt)
                        await SignalIterRepository.write_audit_log(
                            category="spx_watcher", actor="system",
                            summary=f"{tier_label}: market-sold-to-close {sell_qty}× {p.symbol} {p.strike}{p.right} at SPX {spot:.2f} (ib_order_id={trade.order.orderId})",
                        )
                    except Exception:
                        logger.exception("spx_watcher market sell failed")

                # Stop check uses the dynamic level (BE ladder ratchets it).
                # If the tick-driven fast path is already firing this parent's
                # stop, skip — avoids double-fire.
                if p.id in state.firing_parent_ids:
                    continue
                effective_stop = p.dynamic_stop_level if p.dynamic_stop_level is not None else p.spx_target_stop
                if effective_stop is not None:
                    # Distance to stop (signed by direction — positive = safe,
                    # negative = breached). Used for the near-stop diagnostic
                    # below + for stale-tick warnings if a fire ever lags.
                    dist_to_stop = (
                        (spot - effective_stop) if is_long_calls
                        else (effective_stop - spot)
                    )
                    # Diagnostic: if SPX is within 2pt of the stop, audit-log
                    # once every 5s per parent with the live-tick freshness so
                    # we can prove watcher latency from the log timeline. Only
                    # written when actually close to firing — won't spam.
                    if dist_to_stop <= 2.0 and p.id is not None:
                        try:
                            last_log = last_near_stop_log_ms.get(p.id, 0)
                            now_ms = int(asyncio.get_running_loop().time() * 1000)
                            if now_ms - last_log >= NEAR_STOP_LOG_INTERVAL_MS:
                                last_near_stop_log_ms[p.id] = now_ms
                                tick_age_ms = (
                                    int(_time.time() * 1000) - state.last_live_tick_ms
                                    if state.last_live_tick_ms > 0 else -1
                                )
                                stale = " STALE" if tick_age_ms > stale_tick_warn_ms else ""
                                await SignalIterRepository.write_audit_log(
                                    category="spx_watcher", actor="system",
                                    summary=(f"NEAR_STOP{stale}: {p.symbol} {p.strike}{p.right} "
                                            f"spot={spot:.2f} stop={effective_stop:.2f} "
                                            f"dist={dist_to_stop:+.2f}pt tick_age={tick_age_ms}ms"),
                                )
                        except Exception:
                            pass
                    stop_hit = dist_to_stop <= 0
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
                            state.firing_parent_ids.add(p.id)
                            try:
                                await _market_sell(unfired_qty, "STOP")
                                # IMPORTANT: only set status=STOPPED. Do NOT
                                # overwrite spx_target_t{1,2,3}_fired to True
                                # as cleanup — those flags record whether each
                                # tier was actually banked as a partial during
                                # the trade. Polluting them on stop misleads
                                # every downstream consumer (research_dump,
                                # ablation, _estimate_parent_r, BE-kill stats).
                                # status=STOPPED is the sole resolved-marker.
                                await SignalIterRepository.update_live_order(
                                    p.id, status="STOPPED",
                                )
                            finally:
                                state.firing_parent_ids.discard(p.id)
                        continue  # nothing else to do on this parent

                # T1
                if not p.spx_target_t1_fired and p.spx_target_t1 is not None and p.spx_target_t1_qty:
                    t1_hit = (
                        (is_long_calls and spot >= p.spx_target_t1)
                        or (not is_long_calls and spot <= p.spx_target_t1)
                    )
                    if t1_hit:
                        await _market_sell(p.spx_target_t1_qty, "T1")
                        p.spx_target_t1_fired = True; p.spx_target_t1_at_spx = spot
                        await SignalIterRepository.update_live_order(
                            p.id, spx_target_t1_fired=True, spx_target_t1_at_spx=spot,
                            status="PARTIAL",
                        )

                # T2
                if not p.spx_target_t2_fired and p.spx_target_t2 is not None and p.spx_target_t2_qty:
                    t2_hit = (
                        (is_long_calls and spot >= p.spx_target_t2)
                        or (not is_long_calls and spot <= p.spx_target_t2)
                    )
                    if t2_hit:
                        await _market_sell(p.spx_target_t2_qty, "T2")
                        p.spx_target_t2_fired = True; p.spx_target_t2_at_spx = spot
                        await SignalIterRepository.update_live_order(
                            p.id, spx_target_t2_fired=True, spx_target_t2_at_spx=spot,
                            status="PARTIAL",
                        )

                # T3
                if not p.spx_target_t3_fired and p.spx_target_t3 is not None and p.spx_target_t3_qty:
                    t3_hit = (
                        (is_long_calls and spot >= p.spx_target_t3)
                        or (not is_long_calls and spot <= p.spx_target_t3)
                    )
                    if t3_hit:
                        await _market_sell(p.spx_target_t3_qty, "T3")
                        p.spx_target_t3_fired = True; p.spx_target_t3_at_spx = spot
                        await SignalIterRepository.update_live_order(
                            p.id, spx_target_t3_fired=True, spx_target_t3_at_spx=spot,
                            status="PARTIAL",
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
                            p.spx_target_t2_fired = True; p.spx_target_t3_fired = True
                            await SignalIterRepository.update_live_order(
                                p.id, spx_target_t2_fired=True, spx_target_t3_fired=True,
                            )

                # Sync parent status to CLOSED once all tiers have fired
                if (p.spx_target_t1_fired and p.spx_target_t2_fired
                    and p.spx_target_t3_fired and p.status not in ("STOPPED", "CLOSED")):
                    await SignalIterRepository.update_live_order(p.id, status="CLOSED")

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("spx_target_watcher iteration failed")
        await asyncio.sleep(interval)


async def spx_stop_tick_watcher_loop(state) -> None:
    """Tick-driven stop fast path.

    Subscribes to the broadcaster's tick stream and checks stop levels on
    open `spx_points` parents on EVERY new SPX tick — typically ~3-10
    ticks/sec from IBKR. Compared to the polling loop's 0.1s cadence
    this trims stop detection latency from ~100ms worst case to ~ms-
    of-the-tick-arrival.

    Coexists with `spx_target_watcher_loop`:
      - The poll loop still handles BE ladder ratchets, T1/T2/T3
        detection, dollar-loss checks, runner-protect, and the periodic
        near-stop diagnostic. Those are not as latency-critical and
        running them at tick cadence would just multiply IBKR calls.
      - This fast path ONLY checks stops. Per-parent firing is gated by
        `state.firing_parent_ids` so the two paths can never race into
        a double market-sell on the same parent.

    If the tick stream stalls or this task crashes, the poll loop
    continues to fire stops at its own cadence — defense in depth.
    """
    from fef_sidecar.db.models import LiveOrder
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.ibkr.live import LiveIBKRProvider

    logger.info("SPX stop tick-watcher starting (tick-driven fast path)")
    # Cache open parents for short windows so we don't re-query DB on
    # every single tick. Refreshed when stale.
    parents_cache: list = []
    parents_cache_at_ms: int = 0
    CACHE_TTL_MS = 500  # refresh open-parents list at most every 500ms

    async with state.broadcaster.subscribe() as queue:
        while True:
            try:
                event = await queue.get()
                # Only tick events drive stop detection. Skip everything else.
                if getattr(event, "type", None) != "tick":
                    continue
                spot = float(getattr(event, "spx_price", 0) or 0)
                if spot <= 0:
                    continue
                symbol = getattr(event, "symbol", None) or "SPX"
                # Only act on the symbol the user is actively trading. The
                # tick stream sometimes carries multi-symbol ticks when the
                # scanner has secondary subscriptions; filter to whichever
                # is the active SPX-style symbol.
                if symbol != "SPX":
                    continue

                now_ms = int(_time.time() * 1000)
                if (not parents_cache) or (now_ms - parents_cache_at_ms > CACHE_TTL_MS):
                    parents_cache = [
                        o for o in await SignalIterRepository.query_live_orders(limit=200)
                        if o.bracket_role == "parent"
                        and o.exit_strategy == "spx_points"
                        and o.status in ("SUBMITTED", "FILLED", "PARTIAL")
                    ]
                    parents_cache_at_ms = now_ms

                for p in parents_cache:
                    if p.id in state.firing_parent_ids:
                        continue
                    is_long_calls = p.right == "C"
                    effective_stop = (
                        p.dynamic_stop_level
                        if p.dynamic_stop_level is not None
                        else p.spx_target_stop
                    )
                    if effective_stop is None:
                        continue
                    stop_hit = (
                        (is_long_calls and spot <= effective_stop)
                        or (not is_long_calls and spot >= effective_stop)
                    )
                    if not stop_hit:
                        continue
                    # Compute unfired qty from the cached snapshot. The
                    # long_qty position guard in the IBKR sell call will
                    # clip if the cache is stale (e.g., T1 fired between
                    # cache refreshes).
                    unfired_qty = 0
                    if not p.spx_target_t1_fired and p.spx_target_t1_qty:
                        unfired_qty += p.spx_target_t1_qty
                    if not p.spx_target_t2_fired and p.spx_target_t2_qty:
                        unfired_qty += p.spx_target_t2_qty
                    if not p.spx_target_t3_fired and p.spx_target_t3_qty:
                        unfired_qty += p.spx_target_t3_qty
                    if unfired_qty <= 0:
                        continue
                    # Acquire the firing lock — only one path may fire this
                    # parent at a time.
                    state.firing_parent_ids.add(p.id)
                    try:
                        if not isinstance(state.ibkr, LiveIBKRProvider):
                            await SignalIterRepository.write_audit_log(
                                category="spx_watcher", actor="system",
                                summary=(
                                    f"[DRY/TICK] STOP crossed: would market-sell-to-close "
                                    f"{unfired_qty}x {p.symbol} {p.strike}{p.right} at SPX {spot:.2f}"
                                ),
                            )
                        else:
                            from ib_async import MarketOrder
                            ib = state.ibkr._ib  # type: ignore[attr-defined]
                            try:
                                ic = await _qualified_ib_contract(state, p)
                                positions = ib.positions()
                                long_qty = sum(
                                    int(pos.position) for pos in positions
                                    if (getattr(pos.contract, "secType", "") == "OPT"
                                        and getattr(pos.contract, "symbol", "") == p.symbol
                                        and abs(float(getattr(pos.contract, "strike", 0)) - p.strike) < 0.01
                                        and getattr(pos.contract, "right", "") == p.right
                                        and getattr(pos.contract, "lastTradeDateOrContractMonth", "") == p.expiry
                                        and pos.position > 0)
                                )
                                # PATCH 1: contract-level pending-sell reservation.
                                # Prevents cross-parent races on same contract —
                                # the 7-parent SPX 7520C race that placed a
                                # naked short occurred here.
                                contract_key = (p.symbol, float(p.strike), p.right, p.expiry)
                                pending = state.firing_contracts.get(contract_key, 0)
                                available = max(0, long_qty - pending)
                                sell_qty = min(unfired_qty, available)
                                if sell_qty <= 0:
                                    await SignalIterRepository.write_audit_log(
                                        category="spx_watcher", actor="system",
                                        summary=(
                                            f"STOP/TICK SKIPPED: 0 available after reservation "
                                            f"(long={long_qty}, pending={pending}, requested={unfired_qty}) "
                                            f"for {p.symbol} {p.strike}{p.right} — refusing to short"
                                        ),
                                    )
                                else:
                                    # PATCH 2: HARD-BLOCK structural check
                                    # (defense in depth).
                                    fresh_positions = ib.positions()
                                    net_long = 0
                                    for pos in fresh_positions:
                                        c = pos.contract
                                        if (getattr(c, "symbol", "") == p.symbol
                                                and abs(float(getattr(c, "strike", 0)) - float(p.strike)) < 0.01
                                                and getattr(c, "right", "") == p.right
                                                and getattr(c, "lastTradeDateOrContractMonth", "") == p.expiry):
                                            net_long += int(pos.position)
                                    max_sellable = max(0, net_long)
                                    if sell_qty > max_sellable:
                                        sell_qty = max_sellable
                                    if sell_qty <= 0:
                                        await SignalIterRepository.write_audit_log(
                                            category="spx_watcher", actor="system",
                                            summary=(
                                                f"HARD-BLOCK: refusing to short {p.symbol} {p.strike}{p.right} "
                                                f"— net_long={net_long}, requested={unfired_qty} (STOP/TICK)"
                                            ),
                                        )
                                    else:
                                        # Reserve before placing. Spawn a 2s-delayed
                                        # decrement (see _market_sell rationale).
                                        state.firing_contracts[contract_key] = pending + sell_qty

                                        async def _release_reservation(key, amount):
                                            try:
                                                await asyncio.sleep(2.0)
                                                cur = state.firing_contracts.get(key, 0)
                                                state.firing_contracts[key] = max(0, cur - amount)
                                            except Exception:
                                                pass

                                        asyncio.create_task(_release_reservation(contract_key, sell_qty))

                                        mkt = MarketOrder("SELL", sell_qty, transmit=True)
                                        trade = ib.placeOrder(ic, mkt)
                                        tick_age_ms = (
                                            now_ms - state.last_live_tick_ms
                                            if state.last_live_tick_ms > 0 else -1
                                        )
                                        await SignalIterRepository.write_audit_log(
                                            category="spx_watcher", actor="system",
                                            summary=(
                                                f"STOP/TICK: market-sold-to-close {sell_qty}x "
                                                f"{p.symbol} {p.strike}{p.right} at SPX {spot:.2f} "
                                                f"(stop={effective_stop:.2f}, tick_age={tick_age_ms}ms, "
                                                f"ib_order_id={trade.order.orderId})"
                                            ),
                                        )
                            except Exception:
                                logger.exception("spx_stop_tick_watcher market sell failed")
                        # Same as the polling-loop stop branch: status=STOPPED
                        # is the resolved-marker. Don't overwrite the per-tier
                        # fired flags as cleanup — that pollutes partial-tracking
                        # for every downstream analytic.
                        await SignalIterRepository.update_live_order(
                            p.id, status="STOPPED",
                        )
                        # Refresh cache so the next ticks don't re-evaluate
                        # this just-stopped parent.
                        parents_cache = [c for c in parents_cache if c.id != p.id]
                    finally:
                        state.firing_parent_ids.discard(p.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("spx_stop_tick_watcher iteration failed")
