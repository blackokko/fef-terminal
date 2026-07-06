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


def _fmt_money(x: float) -> str:
    """-290.4 -> '-$290', 290.4 -> '+$290'."""
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.0f}"


async def _premium_for_log(state, trade, p) -> tuple[float | None, str]:
    """Best premium to report on a tier-fire audit line.

    1. The actual FILL price (trade.orderStatus.avgFillPrice) — poll up to
       ~2s; market sells on SPX usually fill in well under that.
    2. Fallback: live option quote mid, labeled 'quote'.
    3. None if neither is available (audit line says premium n/a)."""
    try:
        for _ in range(20):
            status = getattr(trade, "orderStatus", None)
            avg = float(getattr(status, "avgFillPrice", 0) or 0)
            if avg > 0:
                return avg, "fill"
            await asyncio.sleep(0.1)
    except Exception:
        pass
    try:
        from fef_sidecar.ibkr.models import Contract, ContractKind
        quote = await asyncio.wait_for(
            state.ibkr.get_option_quote(Contract(
                symbol=p.symbol, kind=ContractKind.OPTION,
                exchange="CBOE" if p.symbol == "SPX" else "SMART",
                expiry=p.expiry, strike=p.strike, right=p.right,
            )),
            timeout=6.0,
        )
        mid = quote.mid
        if mid is not None and mid > 0:
            return float(mid), "quote"
    except Exception:
        pass
    return None, "unavailable"


async def _write_tier_fill_audit(
    state, p, tier_label: str, result, ref_desc: str,
    extra_summary: str = "", extra_details: dict | None = None,
) -> None:
    """Tier-fire audit row with THIS FILL's banked P&L + running cumulative.

    Previously the summary printed the whole-position unrealized mark at
    trigger time (e.g. 'at prem 15.60 / +900$' where +900 = mark x ORIGINAL
    qty), which read as the fill's realized P&L and caused triple-count
    misreads on 3-partial exits. Now:

        T1: sold 1x SPX 7490C @ 15.50 -> banked +$290 this fill
        (cum +$290; position mark +$870) at SPX 6xxx.xx (...)

    banked   = (premium - entry) * 100 * THIS fill's qty
    cum      = sum of banked across this parent's tier fires (this process)
    position mark = (premium - entry) * 100 * ORIGINAL parent qty — the old
    misleading number, kept for reference and clearly labeled.
    Premium is the actual FILL price when available at log time, else a live
    quote labeled '(quote)'. ref_desc = trigger context ('SPX 6712.50' or the
    fixed_dollar trigger premium). Runs as a spawned task so fill-price
    polling never blocks the watcher hot loop.
    """
    from fef_sidecar.db.repository import SignalIterRepository
    try:
        order_id = result.trade.order.orderId
        premium, premium_src = await _premium_for_log(state, result.trade, p)
        entry = p.avg_fill_price if (p.avg_fill_price or 0) > 0 else p.limit_price
        entry_src = "parent_fill" if (p.avg_fill_price or 0) > 0 else "parent_limit"

        if not hasattr(state, "_watcher_banked_cum"):
            state._watcher_banked_cum = {}  # type: ignore[attr-defined]

        details: dict = {
            "tier": tier_label,
            "parent_id": p.id,
            "sell_qty": int(result.sell_qty),
            "original_qty": int(p.qty),
            "premium": premium,
            "premium_source": premium_src,
            "entry_price": entry,
            "entry_price_source": entry_src,
            "ref": ref_desc,
            "ib_order_id": order_id,
            **(extra_details or {}),
        }
        if premium is not None and entry is not None:
            banked = (premium - entry) * 100 * int(result.sell_qty)
            cum = state._watcher_banked_cum.get(p.id, 0.0) + banked  # type: ignore[attr-defined]
            state._watcher_banked_cum[p.id] = cum  # type: ignore[attr-defined]
            position_mark = (premium - entry) * 100 * int(p.qty)
            details.update({
                "banked_this_fill": round(banked, 2),
                "cumulative_banked": round(cum, 2),
                "position_mark": round(position_mark, 2),
            })
            px_label = f"{premium:.2f}" + ("" if premium_src == "fill" else f" ({premium_src})")
            summary = (
                f"{tier_label}: sold {result.sell_qty}x {p.symbol} {p.strike}{p.right} "
                f"@ {px_label} -> banked {_fmt_money(banked)} this fill "
                f"(cum {_fmt_money(cum)}; position mark {_fmt_money(position_mark)}) "
                f"at {ref_desc}"
            )
        else:
            summary = (
                f"{tier_label}: sold {result.sell_qty}x {p.symbol} {p.strike}{p.right} "
                f"(premium n/a at log time) at {ref_desc}"
            )
        if extra_summary:
            summary += f" ({extra_summary}, ib_order_id={order_id})"
        else:
            summary += f" (ib_order_id={order_id})"
        await SignalIterRepository.write_audit_log(
            category="spx_watcher", actor="system",
            summary=summary, details=details,
        )
    except Exception:
        logger.exception("tier fill audit write failed for parent %s", getattr(p, "id", "?"))


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
        # Cap breached — fire market sell. Defense in depth:
        #   1. safe_market_sell is the structural guard (cannot SELL > net_long)
        #   2. Pending-sell reservation prevents cross-parent races against
        #      ib.positions() lag.
        if not isinstance(state.ibkr, LiveIBKRProvider):
            await SignalIterRepository.write_audit_log(
                category="spx_watcher", actor="system",
                summary=f"[DRY] DOLLAR_STOP(${pnl:.0f}): would market-sell {open_qty}× {p.symbol} {p.strike}{p.right}",
            )
            return
        from fef_sidecar.ibkr.safe_sell import safe_market_sell
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
        # Contract-level pending-sell reservation — prevents cross-parent
        # races on same contract where ib.positions() hasn't yet reflected
        # a prior SELL.
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

        # LIFETIME SELL CAP (2026-07-06) — absolute oversell backstop, see
        # _lifetime_cap_reserve.
        sell_qty = _lifetime_cap_reserve(state, p, sell_qty)
        if sell_qty <= 0:
            sold, cap = _lifetime_cap_state(state, p)
            await SignalIterRepository.write_audit_log(
                category="spx_watcher", actor="system",
                summary=(
                    f"DOLLAR_STOP REFUSED by lifetime sell cap: watcher already "
                    f"sold {sold}/{cap} for parent {p.id} "
                    f"({p.symbol} {p.strike}{p.right}, requested={open_qty}) "
                    f"— refusing to oversell"
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

        result = await safe_market_sell(
            ib, ic, sell_qty, purpose="dollar_cap", state=state, parent_id=p.id,
        )
        if not result.placed:
            # safe_market_sell already wrote an audit_log explaining why.
            _lifetime_cap_release(state, p, sell_qty)
            return
        if int(result.sell_qty) < sell_qty:
            _lifetime_cap_release(state, p, sell_qty - int(result.sell_qty))
        # status=STOPPED is the resolved-marker. Don't overwrite the per-tier
        # fired flags as cleanup — they record whether each tier was banked
        # as a partial, not whether the trade resolved. (See poll-loop branch
        # for the full rationale.)
        await SignalIterRepository.update_live_order(
            p.id, status="STOPPED",
        )
        await SignalIterRepository.write_audit_log(
            category="spx_watcher", actor="system",
            summary=f"DOLLAR_STOP(${pnl:.0f}): market-sold-to-close {result.sell_qty}× {p.symbol} {p.strike}{p.right} (ib_order_id={result.trade.order.orderId})",
        )
        try:
            await SignalIterRepository.write_watcher_sell_child(
                parent=p, bracket_role="dollar_cap",
                qty=int(result.sell_qty),
                ib_order_id=int(result.trade.order.orderId),
                ib_perm_id=int(getattr(result.trade.order, "permId", 0) or 0) or None,
                notes=f"dollar_cap pnl=${pnl:.0f}",
            )
        except Exception:
            logger.exception(
                "dollar_cap: write_watcher_sell_child failed for parent %s",
                p.id,
            )
    except Exception:
        logger.exception("dollar-loss check failed for parent %s", p.id)


def _fixed_dollar_actions(*, entry_px, mid, original_qty, tier_qtys, tier_fired,
                          d_targets, d_stop, semantics="whole_unrealized"):
    """Pure decision for a watcher-managed fixed_dollar exit. Given the live
    option mid, decide what to fire NOW.

    Returns ``(stop_qty, fires)``:
      * stop_qty > 0  → market-sell that many (all remaining), ignore targets.
      * fires         → list of ``(tier_label, qty)`` profit-target sells.

    ``gain_per_ct = (mid - entry) * 100`` is the $ change per contract. The
    STOP is the same in both modes: open loss on the REMAINING (unfired) qty
    hitting d_stop (a negative $).

    TARGET ``semantics`` (operator-selectable — state.exit_dollar_semantics):
      * ``whole_unrealized`` (legacy) — tier i fires when the FULL original
        position's mark-to-market profit reaches d_targets[i]. Because each tier
        sells only its slice, "hit all 3 TPs" banks far less than the top target
        (a 3-lot with 200/500/1000 banks ~$650, not $1000) — the 7510C surprise.
      * ``cumulative_realized`` — tier i's SLICE must itself earn the INCREMENT
        between consecutive cumulative targets (d_targets[i]-d_targets[i-1], with
        d_targets[-1]=0). Prior tiers each banked ~their increment at trigger, so
        cumulative banked ≈ d_targets[last fired]; "hit all 3" banks ≈ the top
        number ($1000). Stateless: needs no per-tier fill history. Fires later.
    """
    if entry_px is None or mid is None or entry_px <= 0 or mid < 0:
        return 0, []
    gain_per_ct = (mid - entry_px) * 100.0
    remaining = sum(
        q for q, f in zip(tier_qtys, tier_fired) if q and not f
    )
    if d_stop is not None and remaining > 0:
        if gain_per_ct * remaining <= d_stop:
            return remaining, []
    fires: list[tuple[str, int]] = []
    labels = ("T1", "T2", "T3")
    if semantics == "cumulative_realized":
        # Fire strictly in order: a bigger later tier can clear its (smaller)
        # increment at a lower premium than an earlier tier, so gate each tier
        # on all earlier tiers being fired-or-firing to keep the cumulative
        # invariant (never bank T3 before T1).
        prev = 0.0
        chain_ok = True
        for i in range(3):
            tgt = d_targets[i]
            if tgt is None:
                continue
            incr = tgt - prev
            prev = tgt
            q = tier_qtys[i]
            already = tier_fired[i]
            can_fire = bool(not already and q and chain_ok and gain_per_ct * q >= incr)
            if can_fire:
                fires.append((labels[i], q))
            if not (already or can_fire):
                chain_ok = False
    else:  # whole_unrealized (legacy)
        full_open_pnl = gain_per_ct * original_qty
        for i in range(3):
            q = tier_qtys[i]
            if (not tier_fired[i] and q and d_targets[i] is not None
                    and full_open_pnl >= d_targets[i]):
                fires.append((labels[i], q))
    return 0, fires


def _all_tiers_banked(tier_qtys, tier_fired, original_qty) -> bool:
    """True iff the BANKED (fired) tiers account for the full original position,
    so the parent can be marked CLOSED without orphaning an open runner.

    Guards the 2026-07-01 7520C bug: a tier that was flagged 'fired' without
    actually selling (safe-sell refused) must NOT let all-flags-true flip the
    parent to CLOSED while a contract is still open — CLOSED excludes it from
    every exit watcher. So gate CLOSED on real sold qty, not the flags alone."""
    sold = sum(q for q, f in zip(tier_qtys, tier_fired) if q and f)
    return sold >= (original_qty or 0)


def _lifetime_cap_reserve(state, p, requested: int) -> int:
    """ABSOLUTE OVERSELL CAP — defense in depth (2026-07-06 incident).

    The watcher tracks its OWN cumulative accepted sell qty per parent and
    hard-refuses any sell that would take the total past the parent's filled
    qty, REGARDLESS of what broker position snapshots claim. The pending-sell
    reservation alone has a stale-position window (pending cleared ~2s after
    placement, before ib.positions() reflects the fill) that let a stop
    double-fire into a naked short (-3 SPX 7520C) and a T3 fire after
    runner-protect had already flattened (-1 QQQ 724C).

    Check + increment happen synchronously (no await between them), so
    concurrent exit paths on the one event loop can never both pass. Returns
    the qty actually reserved (possibly clamped; 0 = refuse). Callers MUST
    _lifetime_cap_release any reserved qty that did not end up placed."""
    if p.id is None:
        return max(0, int(requested))
    if not hasattr(state, "_watcher_lifetime_sold"):
        state._watcher_lifetime_sold = {}  # type: ignore[attr-defined]
    cap = int(getattr(p, "filled_qty", 0) or 0) or int(p.qty or 0)
    sold = state._watcher_lifetime_sold.get(p.id, 0)  # type: ignore[attr-defined]
    allowed = max(0, min(int(requested), cap - sold))
    if allowed > 0:
        state._watcher_lifetime_sold[p.id] = sold + allowed  # type: ignore[attr-defined]
    return allowed


def _lifetime_cap_release(state, p, amount: int) -> None:
    """Give back a lifetime-cap reservation for a sell that was refused or
    clamped below its reserved qty."""
    if p.id is None or amount <= 0 or not hasattr(state, "_watcher_lifetime_sold"):
        return
    cur = state._watcher_lifetime_sold.get(p.id, 0)  # type: ignore[attr-defined]
    state._watcher_lifetime_sold[p.id] = max(0, cur - int(amount))  # type: ignore[attr-defined]


def _lifetime_cap_state(state, p) -> tuple[int, int]:
    """(cum_sold, cap) for audit lines."""
    sold = getattr(state, "_watcher_lifetime_sold", {}).get(p.id, 0)
    cap = int(getattr(p, "filled_qty", 0) or 0) or int(p.qty or 0)
    return sold, cap


def _try_spawn_exit_check(state, kind: str, parent_id, make_coro) -> bool:
    """Per-position in-flight latch for spawned exit checks (2026-07-06).

    asyncio.create_task(_check_...) was fire-and-forget: while one check
    awaited its quote/sell round-trips, subsequent watcher ticks spawned
    CONCURRENT checks for the SAME parent that re-evaluated the same
    stop/tier from stale flags — the root of the 7520C double-stop. If a
    check for (kind, parent_id) is already in flight, SKIP this tick (never
    queue). `make_coro` is a zero-arg factory so no coroutine is created on
    a skip. Returns True iff a new check was spawned."""
    if not hasattr(state, "_watcher_inflight_checks"):
        state._watcher_inflight_checks = set()  # type: ignore[attr-defined]
    key = (kind, parent_id)
    if key in state._watcher_inflight_checks:  # type: ignore[attr-defined]
        return False
    state._watcher_inflight_checks.add(key)  # type: ignore[attr-defined]

    async def _run() -> None:
        try:
            await make_coro()
        finally:
            try:
                state._watcher_inflight_checks.discard(key)  # type: ignore[attr-defined]
            except Exception:
                pass

    asyncio.create_task(_run())
    return True


async def _watcher_safe_sell(state, p, qty: int, tier_label: str, ref_desc: str) -> bool:
    """Safe market-sell-to-close `qty` of parent p's contract — the single shared
    exit path for BOTH spx_points and fixed_dollar watchers. Mirrors the long-qty
    guard + contract-level pending reservation + child-row write. `ref_desc` is a
    human string for the audit (e.g. 'SPX 6712.50' or 'prem 5.40 / +$520').
    Returns True iff an order was actually placed.
    """
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.ibkr.live import LiveIBKRProvider

    if qty <= 0:
        return False
    if not isinstance(state.ibkr, LiveIBKRProvider):
        await SignalIterRepository.write_audit_log(
            category="spx_watcher", actor="system",
            summary=f"[DRY] {tier_label} crossed: would market-sell-to-close {qty}× {p.symbol} {p.strike}{p.right} at {ref_desc}",
        )
        return False
    try:
        from fef_sidecar.ibkr.safe_sell import safe_market_sell
        ib = state.ibkr._ib  # type: ignore[attr-defined]
        ic = await _qualified_ib_contract(state, p)

        # SAFETY: never sell more than we own (don't open a short on a missed fill).
        long_qty = 0
        for pos in ib.positions():
            c = pos.contract
            if (getattr(c, "secType", "") == "OPT"
                and getattr(c, "symbol", "") == p.symbol
                and abs(float(getattr(c, "strike", 0)) - p.strike) < 0.01
                and getattr(c, "right", "") == p.right
                and getattr(c, "lastTradeDateOrContractMonth", "") == p.expiry
                and pos.position > 0):
                long_qty += int(pos.position)
        # Contract-level pending-sell reservation (another tier/parent on the same
        # contract may have a SELL whose fill hasn't propagated yet).
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
            return False
        if sell_qty < qty:
            await SignalIterRepository.write_audit_log(
                category="spx_watcher", actor="system",
                summary=(
                    f"{tier_label} PARTIAL: clamped to {sell_qty}/{qty} for "
                    f"{p.symbol} {p.strike}{p.right} (long={long_qty}, pending={pending})"
                ),
            )
        # LIFETIME SELL CAP (2026-07-06) — absolute, independent of broker
        # snapshots: never let the watcher's cumulative sells exceed the
        # parent's filled qty (see _lifetime_cap_reserve).
        requested_after_guards = sell_qty
        sell_qty = _lifetime_cap_reserve(state, p, sell_qty)
        if sell_qty <= 0:
            sold, cap = _lifetime_cap_state(state, p)
            await SignalIterRepository.write_audit_log(
                category="spx_watcher", actor="system",
                summary=(
                    f"{tier_label} REFUSED by lifetime sell cap: watcher already "
                    f"sold {sold}/{cap} for parent {p.id} "
                    f"({p.symbol} {p.strike}{p.right}, requested={qty}) "
                    f"— refusing to oversell"
                ),
            )
            return False
        if sell_qty < requested_after_guards:
            sold, cap = _lifetime_cap_state(state, p)
            await SignalIterRepository.write_audit_log(
                category="spx_watcher", actor="system",
                summary=(
                    f"{tier_label} CLAMPED by lifetime sell cap to "
                    f"{sell_qty}/{requested_after_guards} for parent {p.id} "
                    f"({p.symbol} {p.strike}{p.right}; cum_sold={sold}, cap={cap})"
                ),
            )
        state.firing_contracts[contract_key] = pending + sell_qty

        async def _release_reservation(key, amount):
            try:
                await asyncio.sleep(2.0)
                cur = state.firing_contracts.get(key, 0)
                state.firing_contracts[key] = max(0, cur - amount)
            except Exception:
                pass

        asyncio.create_task(_release_reservation(contract_key, sell_qty))

        result = await safe_market_sell(
            ib, ic, sell_qty, purpose="watcher_close", state=state, parent_id=p.id,
        )
        if not result.placed:
            _lifetime_cap_release(state, p, sell_qty)
            return False
        if int(result.sell_qty) < sell_qty:
            # safe_market_sell clamped further — release the unplaced slice.
            _lifetime_cap_release(state, p, sell_qty - int(result.sell_qty))
        # Tier-fire audit with THIS fill's banked $ + running cumulative +
        # clearly-labeled whole-position mark. Spawned: fill-price polling
        # must never block the watcher hot loop.
        asyncio.create_task(
            _write_tier_fill_audit(state, p, tier_label, result, ref_desc)
        )
        try:
            _role = {
                "T1": "t1", "T2": "t2", "T3": "t3",
                "STOP": "stop", "RUNNER_PROTECT": "runner_protect",
            }.get(tier_label, tier_label.lower())
            await SignalIterRepository.write_watcher_sell_child(
                parent=p, bracket_role=_role,
                qty=int(result.sell_qty),
                ib_order_id=int(result.trade.order.orderId),
                ib_perm_id=int(getattr(result.trade.order, "permId", 0) or 0) or None,
                notes=f"watcher_close@{ref_desc}",
            )
        except Exception:
            logger.exception(
                "spx_watcher: write_watcher_sell_child failed for parent %s", p.id,
            )
        return True
    except Exception:
        logger.exception("watcher market sell failed")
        return False


async def _sync_parent_fill_from_trades(state, p) -> None:
    """Continuously sync a parent's avg_fill_price + status from the live IBKR
    trade object (in-memory `ib.trades()`, no round-trip). FIX (2026-06-30):
    the fill-sync only ran nightly, so a market-entry parent sat at
    status=SUBMITTED / avg_fill_price=None for the whole session — DB P&L was
    wrong AND fixed_dollar's $ exits had no real entry price to mark against.
    Same-session fills only (cross-restart fills are handled by reconcile +
    the nightly ibkr_execution backfill). Idempotent — returns instantly once
    a parent is synced.
    """
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    if not isinstance(state.ibkr, LiveIBKRProvider) or p.ib_order_id is None:
        return
    # Already synced — nothing to do.
    if p.avg_fill_price and p.status in ("FILLED", "PARTIAL", "STOPPED", "CLOSED"):
        return
    try:
        ib = state.ibkr._ib  # type: ignore[attr-defined]
        trade = next(
            (t for t in ib.trades()
             if getattr(t.order, "orderId", None) == p.ib_order_id
             and getattr(t.order, "action", "") == "BUY"),
            None,
        )
        if trade is None:
            return
        st = trade.orderStatus
        avg = float(getattr(st, "avgFillPrice", 0) or 0)
        filled = int(float(getattr(st, "filled", 0) or 0))
        if avg <= 0 or filled <= 0:
            return
        updates: dict = {"avg_fill_price": round(avg, 4), "filled_qty": filled}
        if p.status in ("SUBMITTED", None):
            updates["status"] = "FILLED" if filled >= int(p.qty or 0) else "PARTIAL"
        await SignalIterRepository.update_live_order(p.id, **updates)
        p.avg_fill_price = updates["avg_fill_price"]
        p.filled_qty = filled
        if "status" in updates:
            p.status = updates["status"]
    except Exception:
        logger.exception("parent fill-sync failed for parent %s", p.id)


async def _check_fixed_dollar(state, p) -> None:
    """fixed_dollar exit evaluation — watcher analog of the spx_points SPX-cross
    logic, but driven by live OPTION P&L instead of SPX spot. Spawned as a
    throttled background task (it needs an IBKR option-quote round-trip, so it
    runs at a slower cadence than the 0.1s spot loop). Fires staged market sells
    as open P&L crosses the operator's $ targets, and sells all remaining at the
    $ stop.
    """
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.ibkr.models import Contract, ContractKind
    try:
        if p.id in state.firing_parent_ids:
            return
        # Only evaluate $ targets once the ENTRY has actually FILLED. The old
        # limit_price fallback let a phantom tier fire ~3s after entry — before
        # the buy propagated to ib.positions() — which safe-sell then refused,
        # yet the tier still got flagged fired → parent wrongly CLOSED and the
        # open runner orphaned (2026-07-01 7520C). No fill price ⇒ don't manage
        # yet; _sync_parent_fill_from_trades sets it within a cycle of the fill.
        entry_px = p.avg_fill_price
        if not entry_px or entry_px <= 0:
            return
        opt_quote = await state.ibkr.get_option_quote(Contract(
            symbol=p.symbol, kind=ContractKind.OPTION,
            exchange="CBOE" if p.symbol == "SPX" else "SMART",
            expiry=p.expiry, strike=p.strike, right=p.right,
        ))
        mid = opt_quote.mid
        if mid is None or mid <= 0:
            return

        tier_qtys = [p.spx_target_t1_qty or 0, p.spx_target_t2_qty or 0, p.spx_target_t3_qty or 0]
        tier_fired = [p.spx_target_t1_fired, p.spx_target_t2_fired, p.spx_target_t3_fired]
        d_targets = [p.dollar_target_t1, p.dollar_target_t2, p.dollar_target_t3]
        stop_qty, fires = _fixed_dollar_actions(
            entry_px=entry_px, mid=mid, original_qty=p.qty,
            tier_qtys=tier_qtys, tier_fired=tier_fired,
            d_targets=d_targets, d_stop=p.dollar_target_stop,
            semantics=getattr(state, "exit_dollar_semantics", "whole_unrealized"),
        )
        # Trigger context for audit lines. Clearly label BOTH the price source
        # (quote at trigger, not a fill) and that the $ figure is the WHOLE-
        # position mark — the old bare "prem X / +900$" read as the fill's
        # realized P&L and caused triple-count misreads on partial exits. The
        # authoritative banked-$ math is added per-fill by
        # _write_tier_fill_audit; this string is supplemental context.
        gain_per_ct = (mid - entry_px) * 100.0
        ref = (f"trigger prem {mid:.2f} (quote), "
               f"position mark {_fmt_money(gain_per_ct * p.qty)} on {p.qty}ct")

        # RUNNER-PROTECT (fixed_dollar): once T1 has banked profit, don't let the
        # remaining runners hand the trade back to red. If the option retraces to
        # breakeven (mid <= entry) with runners still open, market-sell them so the
        # trade keeps at least the banked T1 gain. Mirrors the spx_points
        # runner_protect (which sells if SPX retraces to the T1 trigger level) and
        # is gated by the same operator flag. Checked BEFORE the $ stop so a winner
        # is protected at breakeven rather than riding down to the -$ hard stop.
        remaining_qty = sum(q for q, f in zip(tier_qtys, tier_fired) if q and not f)
        if (state.runner_protect_enabled and p.spx_target_t1_fired
                and remaining_qty > 0 and stop_qty == 0 and not fires
                and mid <= entry_px):
            state.firing_parent_ids.add(p.id)
            try:
                # OPTIMISTIC LATCH (2026-07-06 oversell hotfix): resolve the
                # parent BEFORE the awaited sell so a re-entrant/stale check
                # can never re-fire into the placement window. Same terminal
                # status the old post-sell write produced (it was written
                # unconditionally after the sell anyway — refused included).
                p.status = "CLOSED"
                await SignalIterRepository.update_live_order(p.id, status="CLOSED")
                await _watcher_safe_sell(state, p, remaining_qty, "RUNNER_PROTECT", ref)
            finally:
                state.firing_parent_ids.discard(p.id)
            return

        if stop_qty > 0:
            state.firing_parent_ids.add(p.id)
            try:
                # OPTIMISTIC LATCH — see RUNNER_PROTECT rationale above.
                p.status = "STOPPED"
                await SignalIterRepository.update_live_order(p.id, status="STOPPED")
                await _watcher_safe_sell(state, p, stop_qty, "STOP", ref)
            finally:
                state.firing_parent_ids.discard(p.id)
            return

        flag_field = {"T1": "spx_target_t1_fired", "T2": "spx_target_t2_fired",
                      "T3": "spx_target_t3_fired"}
        any_fired = False
        for label, qty in fires:
            field = flag_field[label]
            if getattr(p, field):
                continue  # already latched by another path — never re-fire
            # OPTIMISTIC LATCH (2026-07-06 oversell hotfix): mark the tier
            # fired BEFORE the awaited sell so a re-entrant check can never
            # re-fire the same rung mid-placement (the QQQ 724C fourth sell).
            # Reverted ONLY on a definitive refusal (placed=False: safe-sell
            # refused / DRY / error) — which preserves the 7520C guarantee
            # that a refused sell never phantom-banks a tier (all-flags-true
            # → CLOSED → open runner orphaned from every watcher).
            setattr(p, field, True)
            await SignalIterRepository.update_live_order(p.id, **{field: True})
            placed = await _watcher_safe_sell(state, p, qty, label, ref)
            if not placed:
                setattr(p, field, False)
                await SignalIterRepository.update_live_order(p.id, **{field: False})
                continue
            await SignalIterRepository.update_live_order(p.id, status="PARTIAL")
            any_fired = True
        # CLOSED only when the banked tiers cover the FULL original position
        # (real remaining == 0), never on the flags alone.
        current_fired = [p.spx_target_t1_fired, p.spx_target_t2_fired, p.spx_target_t3_fired]
        if any_fired and _all_tiers_banked(tier_qtys, current_fired, p.qty):
            await SignalIterRepository.update_live_order(p.id, status="CLOSED")
    except Exception:
        logger.exception("fixed_dollar check failed for parent %s", p.id)


async def _safe_persist_execs(state) -> None:
    """Spawn-safe wrapper: persist today's IBKR executions to the ibkr_execution
    table. Previously only ran nightly / on a diag-endpoint hit (the capture
    'stopped' after 2026-05-29 when nothing hit it); now polled intraday."""
    try:
        from fef_sidecar.engine.auto_walk_forward import _persist_today_ibkr_fills
        await _persist_today_ibkr_fills(state)
    except Exception:
        logger.exception("intraday ibkr_execution persist failed")


# ---------------------------------------------------------------------------
# EXIT SHADOW (2026-07-02, reworked 2026-07-06) — live exit A/B on identical
# trades. For every watcher-managed parent this SIMULATES (never executes)
# the exit variant the position is NOT running, conditional on the parent's
# REAL exit_strategy:
#
#   real == 'fixed_dollar' -> shadow the SPX-point BE-LADDER
#       (variant_label='be_ladder_shadow'): build_plan-style T1/T2/T3/stop
#       levels, 50/30/20 partial scale-out, BE-ratchet semantics matching
#       api._walk_be_ladder_exit / the live BE_LADDER. Shadow fills are
#       recorded at the option QUOTE at touch time (bid preferred, mid
#       fallback — a market sell realistically hits the bid).
#   real == 'spx_points'  -> shadow the FIXED-DOLLAR ladder
#       (variant_label='fixed_dollar_shadow'): the operator's 300/500/1000/
#       -400 ladder with cumulative-realized semantics (the live default),
#       ETF-scaled for non-SPX via the same _price_scale/rounding the real
#       auto-trader applies. Driven by throttled option-quote mids exactly
#       like the real _check_fixed_dollar (triggers evaluate at quote mid).
#
# 2026-07-06 plumbing fixes (7/6 EOD sec. 2):
#   (a) entry_spx is sourced from the SYMBOL'S OWN CASH spot (the same spot
#       class build_plan consumes), never the active-chart tick:
#       state.last_live_tick_spx is fed by tick_stream_loop from
#       Contract.for_symbol(state.active_symbol) — charting ES all morning
#       put ES-priced 7561/7567 into the shadow rows while SPX cash sat at
#       7511/7508. A parent-recorded entry_spx_price deviating >0.5% from
#       the cash reference is overridden (provenance persisted).
#   (b) shadow dollars are NEVER defaulted to $0 on missing premiums: fills
#       missing a quote are rescued from the nearest captured option quote
#       within ±2 min (historical_option_chain_point), else the row is
#       explicitly marked unusable via the status column.
#   (c) the close-time recompute UPSERTS (replaces) the outcome row instead
#       of colliding with ux_shadow_exit_parent_variant and silently keeping
#       a stale entry-time compute (parent 747).
#   (d) coverage extends to non-SPX watcher parents — per-symbol cash spot
#       resolution + build_plan's moneyness scaling (_price_scale) give
#       SPY/QQQ shadows the same effective geometry SPX gets.
#
# ZERO effect on real orders — no code path below places, modifies, or
# cancels anything.
# ---------------------------------------------------------------------------

_SHADOW_VARIANT_LABEL = "be_ladder_shadow"
# Same rungs as the live watcher BE_LADDER + api._BE_LADDER:
# (trigger_R_of_MFE, locked_stop_R_from_entry)
_SHADOW_BE_LADDER = [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)]

# Fixed-dollar shadow spec (2026-07-06 flip): the user's live ladder —
# cumulative-realized semantics, SPX-notional dollars, ETF-scaled per symbol
# at init exactly like auto_trader's fixed_dollar path.
_FD_SHADOW_VARIANT_LABEL = "fixed_dollar_shadow"
_FD_SHADOW_TARGETS = (300.0, 500.0, 1000.0)
_FD_SHADOW_STOP = -400.0
_FD_SHADOW_SEMANTICS = "cumulative_realized"
_FD_SHADOW_QUOTE_INTERVAL_MS = int(
    float(os.environ.get("FEF_FIXED_DOLLAR_CHECK_SEC", "3.0")) * 1000
)

# Shadow row usability (persisted to shadow_exit_outcome.status):
_SHADOW_STATUS_OK = "ok"
_SHADOW_STATUS_PREMIUM_GAP = "unusable_premium_gap"
_SHADOW_STATUS_NO_ENTRY_PREMIUM = "unusable_no_entry_premium"

# entry_spx cross-check tolerance: parent-recorded entry_spx_price deviating
# more than this fraction from the cash reference is treated as polluted
# (the ES basis is ~0.66% of SPX; legitimate drift between entry and the
# first watcher poll is well under 0.1%).
_SHADOW_ENTRY_SPX_MAX_DEV = 0.005


def _shadow_split_qty(q: int) -> list[int]:
    """50/30/20 partial split with small-lot handling (3 -> 1/1/1,
    2 -> 1/1/0, 1 -> 1/0/0, 10 -> 5/3/2)."""
    q = int(q or 0)
    if q <= 0:
        return [0, 0, 0]
    t1 = max(1, q // 2)
    t2 = max(0, min(q - t1, max(1, int(q * 0.3)))) if q > 1 else 0
    t3 = max(0, q - t1 - t2)
    return [t1, t2, t3]


def _scale_fd_dollars(x: float, scale: float) -> float:
    """auto_trader's fixed_dollar ETF scaling: whole dollars, $10 minimum
    magnitude, sign preserved. scale=1.0 (SPX-class) is bit-identical."""
    if scale == 1.0:
        return float(x)
    import math as _m
    return _m.copysign(max(10.0, abs(round(x * scale))), x)


async def _shadow_cash_spot(
    state, symbol: str, poll_spot: float, cache: dict,
) -> tuple[float | None, str | None]:
    """Resolve the SYMBOL'S OWN cash spot for shadow entry/stepping/MTM.

    Provenance fix (7/6 EOD sec. 2a): the watcher's polled spot
    (state.last_live_tick_spx / last_spx_price) is fed by tick_stream_loop +
    bar_stream_loop from Contract.for_symbol(state.active_symbol) — it is
    the ACTIVE CHART symbol's price, which is SPX cash only while the user
    charts SPX (an ES morning fed ES-priced 7561 into it vs cash 7511).

    Preference order (freshest trustworthy source wins):
      1. the polled tick, ONLY when state.active_symbol == symbol (then it
         IS the symbol's own cash tick — SPX maps to the CBOE cash index),
      2. the scanner's per-symbol QuantData snapshot spot (cash, ~60s
         cadence; state.last_scanner_verdict_by_symbol[symbol]),
      3. the symbol's latest 1-min bar close from historical_bar (<=10 min
         old; DB read throttled via a 30s per-symbol cache).

    Returns (spot, provenance) or (None, None) — callers must SKIP rather
    than fall back to a cross-symbol price. States without an active_symbol
    attribute (unit-test fakes) treat the polled spot as the symbol's own."""
    if symbol in cache:
        return cache[symbol]
    result: tuple[float | None, str | None] = (None, None)
    active = getattr(state, "active_symbol", symbol)
    if active == symbol and poll_spot and poll_spot > 0:
        result = (float(poll_spot), "active_tick")
    if result[0] is None:
        try:
            v = (getattr(state, "last_scanner_verdict_by_symbol", None) or {}).get(symbol)
            if v:
                ts = int(v.get("timestamp_ms") or 0)
                px = float(v.get("spx_price") or 0.0)
                if px > 0 and (ts == 0 or int(_time.time() * 1000) - ts <= 180_000):
                    result = (px, "scanner_snapshot")
        except Exception:
            pass
    if result[0] is None:
        # Throttled 1-min-bar fallback (30s TTL per symbol).
        now_ms = int(_time.time() * 1000)
        if not hasattr(state, "_shadow_bar_spot_cache"):
            try:
                state._shadow_bar_spot_cache = {}  # type: ignore[attr-defined]
            except Exception:
                cache[symbol] = result
                return result
        entry = state._shadow_bar_spot_cache.get(symbol)  # type: ignore[attr-defined]
        if entry is not None and now_ms - entry[0] < 30_000:
            result = entry[1]
        else:
            bar_result: tuple[float | None, str | None] = (None, None)
            try:
                from fef_sidecar.db.repository import SignalIterRepository
                bars = await SignalIterRepository.query_recent_bars(
                    symbol, "1 min", limit=1,
                )
                if bars and now_ms - int(bars[-1].timestamp_ms) <= 600_000:
                    bar_result = (float(bars[-1].close), "1min_bar")
            except Exception:
                logger.debug("shadow: bar-spot fallback failed for %s", symbol)
            state._shadow_bar_spot_cache[symbol] = (now_ms, bar_result)  # type: ignore[attr-defined]
            result = bar_result
    cache[symbol] = result
    return result


def _shadow_init(state, p, spot: float | None, spot_source: str | None = None) -> dict | None:
    """Build the shadow state machine for one parent — the VARIANT is
    conditional on the parent's real exit_strategy (2026-07-06 flip):
    fixed_dollar parents shadow the BE-ladder; spx_points parents shadow the
    fixed-dollar ladder. `spot` must be the SYMBOL'S OWN cash spot (see
    _shadow_cash_spot); None defers init to a later cycle.

    entry_spx: the parent's recorded entry_spx_price is used only when it
    agrees with the cash reference within 0.5% — a deviation beyond that is
    the active-chart pollution signature (ES-priced 7561 vs cash 7511) and
    the cash reference wins, with both values persisted for audit."""
    if not p.qty:
        return None
    cash = float(spot) if spot and spot > 0 else None
    parent_entry = float(p.entry_spx_price) if (p.entry_spx_price or 0) > 0 else None
    entry_spx: float | None
    if parent_entry is not None and cash is not None:
        if abs(parent_entry - cash) / cash > _SHADOW_ENTRY_SPX_MAX_DEV:
            entry_spx = cash
            entry_spx_source = f"cash_spot_override({spot_source})"
            logger.warning(
                "exit shadow: parent %s entry_spx_price %.2f deviates >%.1f%% "
                "from %s cash %.2f — using cash (active-chart pollution guard)",
                p.id, parent_entry, _SHADOW_ENTRY_SPX_MAX_DEV * 100,
                p.symbol, cash,
            )
        else:
            entry_spx = parent_entry
            entry_spx_source = "parent_entry_spx_price"
    elif parent_entry is not None:
        # No cash reference this cycle — defer rather than trust a value we
        # cannot validate (the 7/6 rows were exactly unvalidated entries).
        return None
    else:
        entry_spx = cash
        entry_spx_source = f"cash_spot({spot_source})" if cash is not None else None
    if not entry_spx or entry_spx <= 0:
        return None
    is_long = p.right == "C"

    variant = (
        _FD_SHADOW_VARIANT_LABEL
        if getattr(p, "exit_strategy", None) == "spx_points"
        else _SHADOW_VARIANT_LABEL
    )

    parent_tier_qtys = [
        int(getattr(p, "spx_target_t1_qty", 0) or 0),
        int(getattr(p, "spx_target_t2_qty", 0) or 0),
        int(getattr(p, "spx_target_t3_qty", 0) or 0),
    ]
    tier_qtys = (
        parent_tier_qtys if sum(parent_tier_qtys) == int(p.qty)
        else _shadow_split_qty(int(p.qty))
    )

    sh: dict = {
        "variant": variant,
        "parent_id": p.id,
        "symbol": p.symbol, "strike": float(p.strike), "right": p.right,
        "expiry": p.expiry,
        "qty": int(p.qty),
        "is_long": is_long,
        "entry_spx": float(entry_spx),
        "entry_spx_source": entry_spx_source,
        "parent_entry_spx_price": parent_entry,
        "spot_source": spot_source,
        "entry_premium": float(p.avg_fill_price or p.limit_price or 0) or None,
        "tier_qtys": tier_qtys,
        "tier_fired": [False, False, False],
        "peak_mfe_R": 0.0,
        "fills": [],           # {tier, spx, qty, ts_ms, premium, premium_src}
        "premium_tasks": [],   # spawned quote captures, gathered at finalize
        "done": False,
        "exit_reason": None,
        "settled_mtm_at_real_close": False,
    }

    if variant == _FD_SHADOW_VARIANT_LABEL:
        # Fixed-dollar ladder — ETF-scaled at init from the symbol's own
        # cash level (same _price_scale + rounding as the live auto-trader).
        from fef_sidecar.engine.strikes import _price_scale
        scale = _price_scale(float(entry_spx))
        sh.update({
            "d_targets": [_scale_fd_dollars(x, scale) for x in _FD_SHADOW_TARGETS],
            "d_stop": _scale_fd_dollars(_FD_SHADOW_STOP, scale),
            "fd_semantics": _FD_SHADOW_SEMANTICS,
            "fd_scale": scale,
            "runner_protect": bool(getattr(state, "runner_protect_enabled", True)),
            "last_mid": None,
            "last_mid_ms": 0,
            "last_quote_ms": 0,
            "peak_gain_per_ct": 0.0,
            "last_spot": cash,
        })
        return sh

    # BE-ladder variant: levels from the parent's own spx_target_* when
    # present, else build_plan's default geometry anchored at entry —
    # build_plan applies _price_scale internally, so non-SPX parents get
    # the same effective moneyness geometry (2026-07-06 coverage fix).
    if all(v is not None for v in (p.spx_target_t1, p.spx_target_t2,
                                   p.spx_target_t3, p.spx_target_stop)):
        t1, t2, t3, stop = (p.spx_target_t1, p.spx_target_t2,
                            p.spx_target_t3, p.spx_target_stop)
        levels_source = "parent_spx_targets"
    else:
        try:
            from fef_sidecar.db.models import FEFDirection
            from fef_sidecar.engine.strikes import build_plan
            plan = build_plan(
                spot=float(entry_spx),
                direction=FEFDirection.LONG_CALLS if is_long else FEFDirection.LONG_PUTS,
                top_gex_walls=[], top_dex_walls=[], ntm_max_pain=None,
            )
            t1, t2, t3, stop = plan.t1, plan.t2, plan.t3, plan.stop
            levels_source = "build_plan_default"
        except Exception:
            logger.exception("be-ladder shadow: build_plan levels failed for parent %s", p.id)
            return None
    risk = abs(float(entry_spx) - float(stop))
    if risk <= 0:
        return None
    sh.update({
        "t1": float(t1), "t2": float(t2), "t3": float(t3), "stop": float(stop),
        "levels_source": levels_source,
        "dyn_stop": float(stop),
        "be_step": 0.0, "be_lock_R": 0.0,
    })
    return sh


async def _shadow_capture_premium(state, sh: dict, fill: dict) -> None:
    """Stamp the option quote price onto a shadow fill (bid preferred — a
    market sell realistically hits the bid; mid fallback). Spawned so the
    watcher hot loop never blocks on an IBKR round-trip."""
    try:
        from fef_sidecar.ibkr.models import Contract, ContractKind
        quote = await asyncio.wait_for(
            state.ibkr.get_option_quote(Contract(
                symbol=sh["symbol"], kind=ContractKind.OPTION,
                exchange="CBOE" if sh["symbol"] == "SPX" else "SMART",
                expiry=sh["expiry"], strike=sh["strike"], right=sh["right"],
            )),
            timeout=6.0,
        )
        bid = getattr(quote, "bid", None)
        mid = getattr(quote, "mid", None)
        if bid is not None and bid > 0:
            fill["premium"], fill["premium_src"] = float(bid), "bid"
        elif mid is not None and mid > 0:
            fill["premium"], fill["premium_src"] = float(mid), "mid"
    except Exception:
        logger.debug("be-ladder shadow: premium capture failed for parent %s",
                     sh.get("parent_id"))


def _shadow_record_fill(
    state, sh: dict, tier: str, qty: int, spot: float | None,
    *, premium: float | None = None, premium_src: str | None = None,
) -> None:
    """Record one shadow fill. BE-ladder fills (premium=None) spawn a live
    quote capture; fixed-dollar-shadow fills carry the triggering mid
    directly (the trigger value IS the mark, same as the real checker)."""
    fill = {
        "tier": tier, "spx": float(spot) if spot else None, "qty": int(qty),
        "ts_ms": int(_time.time() * 1000),
        "premium": float(premium) if premium is not None else None,
        "premium_src": premium_src,
    }
    sh["fills"].append(fill)
    if premium is None:
        try:
            task = asyncio.create_task(_shadow_capture_premium(state, sh, fill))
            sh["premium_tasks"].append(task)
        except RuntimeError:
            pass  # no running loop (sync test context) — premium stays None


def _shadow_remaining(sh: dict) -> int:
    return sum(q for q, f in zip(sh["tier_qtys"], sh["tier_fired"]) if q and not f)


def _shadow_fd_step(state, sh: dict, mid: float, spot: float | None = None) -> None:
    """Advance the FIXED-DOLLAR shadow one quote sample. Mirrors
    _check_fixed_dollar's live ordering exactly: decide targets/stop via the
    shared pure _fixed_dollar_actions, then runner-protect (T1 banked, mid
    back at/below entry, nothing else firing), then the $ stop, then staged
    tier fires. Fills mark at the triggering mid — the same price the real
    checker's decision evaluates."""
    if sh["done"] or mid is None or mid <= 0:
        return
    entry_prem = sh.get("entry_premium")
    if not entry_prem or entry_prem <= 0:
        return  # no real entry premium yet — don't manage $ against a guess
    sh["last_mid"] = float(mid)
    sh["last_mid_ms"] = int(_time.time() * 1000)
    if spot:
        sh["last_spot"] = float(spot)
    gain_per_ct = (float(mid) - float(entry_prem)) * 100.0
    sh["peak_gain_per_ct"] = max(sh.get("peak_gain_per_ct", 0.0), gain_per_ct)

    stop_qty, fires = _fixed_dollar_actions(
        entry_px=float(entry_prem), mid=float(mid), original_qty=sh["qty"],
        tier_qtys=sh["tier_qtys"], tier_fired=sh["tier_fired"],
        d_targets=sh["d_targets"], d_stop=sh["d_stop"],
        semantics=sh.get("fd_semantics", _FD_SHADOW_SEMANTICS),
    )
    remaining = _shadow_remaining(sh)

    # RUNNER-PROTECT mirror (same predicate as the live fixed_dollar path).
    if (sh.get("runner_protect") and sh["tier_fired"][0] and remaining > 0
            and stop_qty == 0 and not fires and float(mid) <= float(entry_prem)):
        _shadow_record_fill(state, sh, "RUNNER_PROTECT", remaining,
                            sh.get("last_spot"), premium=float(mid),
                            premium_src="mid_trigger")
        sh["tier_fired"] = [True, True, True]
        sh["done"] = True
        sh["exit_reason"] = "runner_protect"
        return

    if stop_qty > 0:
        _shadow_record_fill(state, sh, "STOP", stop_qty, sh.get("last_spot"),
                            premium=float(mid), premium_src="mid_trigger")
        sh["tier_fired"] = [True, True, True]
        sh["done"] = True
        sh["exit_reason"] = "dollar_stop"
        return

    idx = {"T1": 0, "T2": 1, "T3": 2}
    for label, q in fires:
        i = idx[label]
        if sh["tier_fired"][i]:
            continue
        _shadow_record_fill(state, sh, label, q, sh.get("last_spot"),
                            premium=float(mid), premium_src="mid_trigger")
        sh["tier_fired"][i] = True
    if _shadow_remaining(sh) <= 0:
        sh["done"] = True
        sh["exit_reason"] = "targets"


def _shadow_fd_maybe_sample(state, sh: dict) -> None:
    """Throttled + latched quote sampler for the fixed-dollar shadow — one
    in-flight quote per shadow, at the same cadence class as the real
    _check_fixed_dollar (FEF_FIXED_DOLLAR_CHECK_SEC). The decision step runs
    inside the spawned task on quote arrival, so trigger cadence == quote
    cadence, exactly like the live checker."""
    if sh["done"] or sh.get("_quote_inflight"):
        return
    now_ms = int(_time.time() * 1000)
    if now_ms - sh.get("last_quote_ms", 0) < _FD_SHADOW_QUOTE_INTERVAL_MS:
        return
    sh["_quote_inflight"] = True
    sh["last_quote_ms"] = now_ms

    async def _sample() -> None:
        try:
            from fef_sidecar.ibkr.models import Contract, ContractKind
            quote = await asyncio.wait_for(
                state.ibkr.get_option_quote(Contract(
                    symbol=sh["symbol"], kind=ContractKind.OPTION,
                    exchange="CBOE" if sh["symbol"] == "SPX" else "SMART",
                    expiry=sh["expiry"], strike=sh["strike"], right=sh["right"],
                )),
                timeout=6.0,
            )
            mid = getattr(quote, "mid", None)
            if mid is not None and mid > 0:
                _shadow_fd_step(state, sh, float(mid))
        except Exception:
            logger.debug("fixed-dollar shadow: quote sample failed for parent %s",
                         sh.get("parent_id"))
        finally:
            sh["_quote_inflight"] = False

    try:
        asyncio.create_task(_sample())
    except RuntimeError:
        sh["_quote_inflight"] = False  # no running loop (sync test context)


async def _nearest_captured_premium(
    symbol: str, expiry: str, strike: float, right: str, ts_ms: int,
    *, window_ms: int = 120_000,
) -> tuple[float | None, str | None, int | None]:
    """Nearest captured option quote within ±window_ms of ts_ms from
    historical_option_chain_point (the 2-min IBKR snapshot capture). Used to
    rescue shadow fills whose live quote capture failed, instead of letting
    the row silently contribute $0 (7/6 EOD sec. 2b). Returns
    (price, source_field, delta_ms) — bid preferred (a market sell
    realistically hits the bid), mid then last as fallbacks."""
    try:
        from sqlmodel import select
        from fef_sidecar.db.engine import get_session
        from fef_sidecar.db.models import HistoricalOptionChainPoint as _HP
        async with get_session() as session:
            rows = list((await session.exec(
                select(_HP)
                .where(_HP.symbol == symbol)
                .where(_HP.expiry == expiry)
                .where(_HP.right == right)
                .where(_HP.strike >= float(strike) - 0.01)
                .where(_HP.strike <= float(strike) + 0.01)
                .where(_HP.timestamp_ms >= ts_ms - window_ms)
                .where(_HP.timestamp_ms <= ts_ms + window_ms)
            )).all())
    except Exception:
        logger.exception("shadow: nearest-captured-premium query failed")
        return None, None, None
    best: tuple[float, str, int] | None = None
    for r in rows:
        px, src = None, None
        if r.bid is not None and r.bid > 0:
            px, src = float(r.bid), "bid"
        elif r.mid is not None and r.mid > 0:
            px, src = float(r.mid), "mid"
        elif r.last is not None and r.last > 0:
            px, src = float(r.last), "last"
        if px is None:
            continue
        dt = abs(int(r.timestamp_ms) - ts_ms)
        if best is None or dt < best[2]:
            best = (px, src, dt)
    if best is None:
        return None, None, None
    return best


def _shadow_step(state, sh: dict, spot: float) -> None:
    """Advance the shadow state machine one poll. Pure arithmetic + (on a
    fill event) one spawned quote capture. Mirrors _walk_be_ladder_exit
    semantics on tick samples: BE-ratchet from MFE first, then stop check
    (conservative ordering), then first-touch tiers in order."""
    if sh["done"] or spot <= 0:
        return
    entry = sh["entry_spx"]
    is_long = sh["is_long"]
    risk = abs(entry - sh["stop"]) or 1.0
    mfe_R = (spot - entry) / risk if is_long else (entry - spot) / risk
    sh["peak_mfe_R"] = max(sh["peak_mfe_R"], mfe_R)

    # BE ratchet — multiple rungs can trigger in one step (matches the live
    # watcher: a fast move ratchets straight to the highest cleared rung).
    for trig_R, lock_R in _SHADOW_BE_LADDER:
        if mfe_R >= trig_R and sh["be_step"] < trig_R:
            sh["be_step"], sh["be_lock_R"] = trig_R, lock_R
            new_stop = entry + lock_R * risk if is_long else entry - lock_R * risk
            if is_long:
                sh["dyn_stop"] = max(sh["dyn_stop"], new_stop)
            else:
                sh["dyn_stop"] = min(sh["dyn_stop"], new_stop)

    # Stop BEFORE targets (conservative intra-sample ordering).
    stop_hit = (spot <= sh["dyn_stop"]) if is_long else (spot >= sh["dyn_stop"])
    if stop_hit:
        remaining = _shadow_remaining(sh)
        if remaining > 0:
            _shadow_record_fill(state, sh, "STOP", remaining, spot)
        sh["tier_fired"] = [True, True, True]
        sh["done"] = True
        sh["exit_reason"] = "trail" if sh["be_step"] > 0 else "stop"
        return

    # First-touch tiers — partials scale out while the residual runs.
    for i, (lvl, label) in enumerate(
        zip((sh["t1"], sh["t2"], sh["t3"]), ("T1", "T2", "T3"))
    ):
        if sh["tier_fired"][i] or not sh["tier_qtys"][i]:
            continue
        touched = (spot >= lvl) if is_long else (spot <= lvl)
        if touched:
            _shadow_record_fill(state, sh, label, sh["tier_qtys"][i], spot)
            sh["tier_fired"][i] = True
    if _shadow_remaining(sh) <= 0:
        sh["done"] = True
        sh["exit_reason"] = "targets"


def _shadow_realized_r(sh: dict) -> float:
    """Partial-aware realized R in SPX terms — same convention as
    api._walk_be_ladder_exit (tiers bank their ACTUAL R, the stop/MTM fill
    settles the residual at its own R), weighted by real tier quantities."""
    entry, qty = sh["entry_spx"], sh["qty"]
    risk = abs(entry - sh["stop"]) or 1.0
    sign = 1.0 if sh["is_long"] else -1.0
    r = 0.0
    for f in sh["fills"]:
        fill_r = sign * (f["spx"] - entry) / risk
        r += (f["qty"] / qty) * fill_r
    return r


async def _shadow_finalize(state, sh: dict, spot: float) -> None:
    """Real position closed — settle any open shadow remainder mark-to-market
    (BE-ladder: at the symbol's cash spot; fixed-dollar: at the last sampled
    mid), rescue missing fill premiums from the nearest captured quote within
    ±2 min, then UPSERT the shadow_exit_outcome row + one compact audit line.

    2026-07-06 fixes: shadow dollars are never $0-defaulted — a row whose
    premiums cannot be established is explicitly marked unusable via the
    status column; and the close-time recompute REPLACES any existing
    (parent, variant) row instead of dying on ux_shadow_exit_parent_variant
    and silently keeping a stale entry-time compute (parent 747).
    Never raises."""
    from fef_sidecar.db.repository import SignalIterRepository
    try:
        variant = sh.get("variant", _SHADOW_VARIANT_LABEL)
        is_fd = variant == _FD_SHADOW_VARIANT_LABEL
        if not sh["done"]:
            remaining = _shadow_remaining(sh)
            if remaining > 0:
                if is_fd:
                    mid = sh.get("last_mid")
                    _shadow_record_fill(
                        state, sh, "MTM_AT_REAL_CLOSE", remaining,
                        sh.get("last_spot"),
                        premium=(float(mid) if mid else None),
                        premium_src=("last_sampled_mid" if mid else None),
                    )
                elif spot and spot > 0:
                    _shadow_record_fill(state, sh, "MTM_AT_REAL_CLOSE", remaining, spot)
            sh["tier_fired"] = [True, True, True]
            sh["done"] = True
            sh["exit_reason"] = "real_closed_mtm"
            sh["settled_mtm_at_real_close"] = True

        # Collect any in-flight premium captures (bounded).
        tasks = [t for t in sh.get("premium_tasks", []) if not t.done()]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=8.0,
                )
            except Exception:
                pass

        # PREMIUM-GAP RESCUE (7/6 EOD sec. 2b): a fill whose live quote
        # capture failed is rescued from the nearest captured chain quote
        # within ±2 min before the row is judged unusable.
        for f in sh["fills"]:
            if f.get("premium") is not None:
                continue
            px, src, dt = await _nearest_captured_premium(
                sh["symbol"], sh["expiry"], sh["strike"], sh["right"],
                int(f.get("ts_ms") or 0),
            )
            if px is not None:
                f["premium"] = px
                f["premium_src"] = f"capture_nearest({src},{dt}ms)"

        entry_prem = sh.get("entry_premium")
        premium_gaps = sum(1 for f in sh["fills"] if f.get("premium") is None)
        shadow_dollars: float | None
        if not entry_prem or entry_prem <= 0:
            # Never mark $ P&L against a guessed entry.
            status = _SHADOW_STATUS_NO_ENTRY_PREMIUM
            shadow_dollars = None
        elif premium_gaps:
            # NEVER default missing fills to $0 — a partial sum understates
            # and a zero row poisons the A/B aggregate. Explicitly unusable.
            status = _SHADOW_STATUS_PREMIUM_GAP
            shadow_dollars = None
        else:
            status = _SHADOW_STATUS_OK
            shadow_dollars = 0.0
            for f in sh["fills"]:
                shadow_dollars += (f["premium"] - entry_prem) * 100.0 * f["qty"]
        real_dollars = None
        try:
            cum = getattr(state, "_watcher_banked_cum", {}) or {}
            real_dollars = cum.get(sh["parent_id"])
        except Exception:
            pass

        if is_fd:
            # R convention for the $ ladder: risk unit = the position-level
            # $ stop (documented in variant_params.r_denominator_dollars).
            denom = abs(float(sh.get("d_stop") or 0.0)) or None
            realized_r = (
                (shadow_dollars / denom)
                if (shadow_dollars is not None and denom) else 0.0
            )
            peak_r = (
                (float(sh.get("peak_gain_per_ct", 0.0)) * sh["qty"] / denom)
                if denom else 0.0
            )
        else:
            realized_r = _shadow_realized_r(sh)
            peak_r = sh["peak_mfe_R"]
        from fef_sidecar.engine.chop_gate import session_date_et
        now_ms = int(_time.time() * 1000)

        common = {
            "tier_qtys": sh["tier_qtys"],
            "entry_premium": entry_prem,
            "entry_spx_source": sh.get("entry_spx_source"),
            "parent_entry_spx_price": sh.get("parent_entry_spx_price"),
            "spot_source": sh.get("spot_source"),
            "fills": sh["fills"],
            "premium_gaps": premium_gaps,
            "status": status,
            "shadow_dollars": (round(shadow_dollars, 2)
                               if shadow_dollars is not None else None),
            "real_dollars": (round(real_dollars, 2)
                             if real_dollars is not None else None),
            "settled_mtm_at_real_close": sh["settled_mtm_at_real_close"],
        }
        if is_fd:
            variant_params = {
                "d_targets": sh["d_targets"],
                "d_stop": sh["d_stop"],
                "semantics": sh.get("fd_semantics", _FD_SHADOW_SEMANTICS),
                "fd_scale": sh.get("fd_scale", 1.0),
                "runner_protect": sh.get("runner_protect", True),
                "r_denominator_dollars": abs(float(sh.get("d_stop") or 0.0)) or None,
                **common,
            }
        else:
            variant_params = {
                "levels": {"t1": sh["t1"], "t2": sh["t2"],
                           "t3": sh["t3"], "stop": sh["stop"]},
                "levels_source": sh["levels_source"],
                "be_ladder": _SHADOW_BE_LADDER,
                "be_step": sh["be_step"], "be_lock_R": sh["be_lock_R"],
                **common,
            }

        # UPSERT the outcome row (7/6 EOD sec. 2c): the close-time recompute
        # must REPLACE an earlier row for (parent, variant) — parent 747's
        # row froze at its entry-time compute because the final recompute
        # collided with ux_shadow_exit_parent_variant and was swallowed.
        try:
            from datetime import UTC as _UTC
            from datetime import datetime as _dt
            from sqlmodel import select as _select
            from fef_sidecar.db.engine import get_session
            from fef_sidecar.db.models import ShadowExitOutcome
            async with get_session() as session:
                existing = (await session.exec(
                    _select(ShadowExitOutcome)
                    .where(ShadowExitOutcome.parent_order_id == sh["parent_id"])
                    .where(ShadowExitOutcome.variant_label == variant)
                )).first()
                if existing is None:
                    existing = ShadowExitOutcome(
                        parent_order_id=sh["parent_id"],
                        variant_label=variant,
                    )
                existing.session_date = session_date_et(now_ms)
                existing.computed_at = _dt.now(_UTC)
                existing.entry_spx = sh["entry_spx"]
                existing.direction = "LONG_CALLS" if sh["is_long"] else "LONG_PUTS"
                existing.qty = sh["qty"]
                existing.variant_params = variant_params
                existing.realized_r = round(realized_r, 4)
                existing.exit_reason = sh["exit_reason"] or ""
                existing.peak_r = round(peak_r, 4)
                existing.exit_ms = now_ms
                existing.actual_realized_pnl = real_dollars
                existing.status = status
                session.add(existing)
        except Exception:
            logger.exception(
                "exit shadow: outcome upsert failed for parent %s", sh["parent_id"],
            )

        shadow_str = _fmt_money(shadow_dollars) if shadow_dollars is not None else "n/a"
        real_str = _fmt_money(real_dollars) if real_dollars is not None else "n/a"
        label = "fixed-$ ladder" if is_fd else "BE-ladder"
        status_note = "" if status == _SHADOW_STATUS_OK else f" [{status}]"
        await SignalIterRepository.write_audit_log(
            category="shadow_exit", actor="system",
            summary=(
                f"SHADOW {label} would have made {shadow_str} vs real {real_str} "
                f"on {sh['symbol']} {sh['strike']:.0f}{sh['right']} "
                f"(parent {sh['parent_id']}, exit {sh['exit_reason']}, "
                f"shadow_R {realized_r:+.2f}){status_note}"
            ),
            details={
                "variant_label": variant,
                "parent_order_id": sh["parent_id"],
                "shadow_dollars": shadow_dollars,
                "real_dollars": real_dollars,
                "realized_r": round(realized_r, 4),
                "exit_reason": sh["exit_reason"],
                "peak_r": round(peak_r, 4),
                "status": status,
                "premium_gaps": premium_gaps,
                "entry_spx": sh["entry_spx"],
                "entry_spx_source": sh.get("entry_spx_source"),
            },
        )
    except Exception:
        logger.exception("exit shadow finalize failed for parent %s",
                         sh.get("parent_id"))


async def _shadow_be_ladder_cycle(state, parents, spot: float) -> None:
    """One watcher-poll advance of every parent's exit shadow (both variants;
    the name is historical — state.shadow_be_ladder keys all shadows). Called
    from spx_target_watcher_loop with the parents + polled spot it already
    has; per-symbol CASH spots are resolved via _shadow_cash_spot (cached per
    cycle) so non-SPX parents step on their own price and an off-SPX active
    chart can't pollute entries. No order placement anywhere below."""
    shadows = getattr(state, "shadow_be_ladder", None)
    if shadows is None:
        shadows = {}
        try:
            state.shadow_be_ladder = shadows
        except Exception:
            return
    spot_cache: dict[str, tuple[float | None, str | None]] = {}
    live_ids = set()
    for p in parents:
        if p.id is None:
            continue
        live_ids.add(p.id)
        sh = shadows.get(p.id)
        sym_spot, sym_src = await _shadow_cash_spot(state, p.symbol, spot, spot_cache)
        if sh is None:
            sh = _shadow_init(state, p, sym_spot, spot_source=sym_src)
            if sh is not None:
                shadows[p.id] = sh
            continue
        # Keep entry premium current once the real fill syncs in.
        if not sh.get("entry_premium") and (p.avg_fill_price or 0) > 0:
            sh["entry_premium"] = float(p.avg_fill_price)
        if not sh["done"]:
            if sh.get("variant") == _FD_SHADOW_VARIANT_LABEL:
                if sym_spot:
                    sh["last_spot"] = float(sym_spot)
                _shadow_fd_maybe_sample(state, sh)
            elif sym_spot:
                _shadow_step(state, sh, sym_spot)
    # Real position closed (parent left the active set) -> finalize + emit.
    for pid in [pid for pid in list(shadows) if pid not in live_ids]:
        sh = shadows.pop(pid)
        f_spot, _f_src = await _shadow_cash_spot(state, sh["symbol"], spot, spot_cache)
        await _shadow_finalize(state, sh, f_spot or 0.0)


async def spx_target_watcher_loop(state) -> None:
    from fef_sidecar.db.models import LiveOrder
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from fef_sidecar.ibkr.models import Contract, ContractKind

    interval = float(os.environ.get("SPX_WATCHER_INTERVAL_SEC", "0.1"))
    logger.info("SPX target watcher loop starting (interval=%.2fs)", interval)
    # fixed_dollar exits poll the live option quote (an IBKR round-trip), so they
    # run at a slower per-parent cadence than the 0.1s SPX-spot loop.
    fixed_dollar_check_ms = int(
        float(os.environ.get("FEF_FIXED_DOLLAR_CHECK_SEC", "3.0")) * 1000
    )
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

            # Watcher manages BOTH spx_points (SPX-cross exits) and fixed_dollar
            # ($ P&L exits) parents — selected by the exit_strategy that was active
            # when the auto-trader placed the order.
            parents = [
                o for o in await SignalIterRepository.query_live_orders(limit=200)
                if o.bracket_role == "parent"
                and o.exit_strategy in ("spx_points", "fixed_dollar")
                and o.status in ("SUBMITTED", "FILLED", "PARTIAL")
            ]

            # EXIT SHADOW (2026-07-02, reworked 2026-07-06): simulate the
            # OTHER exit variant on every watcher-managed parent — BE-ladder
            # for fixed_dollar parents, the fixed-$ ladder for spx_points
            # parents — per-symbol cash spots, all watcher symbols. Writes a
            # shadow_exit_outcome row + audit line when the real position
            # closes. NEVER places orders (see section above).
            try:
                await _shadow_be_ladder_cycle(state, parents, spot)
            except Exception:
                logger.exception("be-ladder shadow cycle failed")

            now_ms = int(asyncio.get_running_loop().time() * 1000)
            if not hasattr(state, "_dollar_cap_last_check_ms"):
                state._dollar_cap_last_check_ms = {}  # type: ignore[attr-defined]
            if not hasattr(state, "_fixed_dollar_last_check_ms"):
                state._fixed_dollar_last_check_ms = {}  # type: ignore[attr-defined]

            # Keep ibkr_execution current intraday (spawned, never awaited, so the
            # reqExecutions round-trip + its commission-race sleep can't block stop
            # detection). Throttled to 60s.
            if not hasattr(state, "_exec_persist_last_ms"):
                state._exec_persist_last_ms = 0  # type: ignore[attr-defined]
            if now_ms - state._exec_persist_last_ms >= 60000:  # type: ignore[attr-defined]
                state._exec_persist_last_ms = now_ms  # type: ignore[attr-defined]
                asyncio.create_task(_safe_persist_execs(state))

            # Spawn dollar-loss-cap check as a background task per parent.
            # Doesn't block SPX-target evaluation, throttled per parent to 10s.
            # IN-FLIGHT LATCH (2026-07-06): if the previous check for this
            # parent is still awaiting its quote/sell round-trips, SKIP this
            # tick (don't queue) — concurrent checks on one parent re-evaluate
            # the same stop from stale flags (the oversell root cause).
            for p in parents:
                if (p.dollar_loss_cap is not None
                    and p.limit_price is not None
                    and p.status not in ("STOPPED",)):
                    last_check = state._dollar_cap_last_check_ms.get(p.id, 0)  # type: ignore[attr-defined]
                    if now_ms - last_check >= 10000:
                        if _try_spawn_exit_check(
                            state, "dollar_cap", p.id,
                            lambda state=state, p=p: _check_dollar_loss(state, p),
                        ):
                            state._dollar_cap_last_check_ms[p.id] = now_ms  # type: ignore[attr-defined]

            # Default BE ladder. Configurable per-deployment via env if needed.
            # Each tuple: (trigger_R, new_stop_at_R_from_entry)
            # 0.5R reached -> stop to entry (BE)
            # 1.0R reached -> stop to +0.5R (locks half)
            # 1.5R reached -> stop to +1.0R (locks 1R)
            BE_LADDER = [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)]

            for p in parents:
                # Keep the parent's avg_fill_price + status current from the live
                # IBKR trade (cheap, in-memory). fixed_dollar's $ exits need a real
                # entry price; spx_points benefits for accurate DB P&L.
                await _sync_parent_fill_from_trades(state, p)

                # fixed_dollar parents are managed in $ terms by a throttled
                # background check (it needs an option-quote round-trip). All the
                # logic below this branch is SPX-spot driven (spx_points only).
                if p.exit_strategy == "fixed_dollar":
                    last = state._fixed_dollar_last_check_ms.get(p.id, 0)  # type: ignore[attr-defined]
                    if now_ms - last >= fixed_dollar_check_ms:
                        # IN-FLIGHT LATCH (2026-07-06): one check per parent at
                        # a time — a still-running check means SKIP this tick,
                        # never a concurrent re-evaluation (the 7520C double-
                        # stop: attempt 1 sold 3, attempt 4 sold 3 again off a
                        # stale position snapshot).
                        if _try_spawn_exit_check(
                            state, "fixed_dollar", p.id,
                            lambda state=state, p=p: _check_fixed_dollar(state, p),
                        ):
                            state._fixed_dollar_last_check_ms[p.id] = now_ms  # type: ignore[attr-defined]
                    continue

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

                async def _market_sell(qty: int, tier_label: str) -> bool:
                    # spx_points exits delegate to the shared safe-sell helper
                    # (the same path fixed_dollar uses). ref = SPX spot at fire.
                    # Returns True iff an order actually PLACED — callers MUST gate
                    # their tier fired-flag on this, else a refused sell phantom-
                    # banks the tier and can orphan an open runner (the 7520C bug).
                    return await _watcher_safe_sell(state, p, qty, tier_label, f"SPX {spot:.2f}")

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
                                # OPTIMISTIC LATCH (2026-07-06): status=STOPPED
                                # BEFORE the awaited sell (it was already written
                                # unconditionally after it) so no other path can
                                # re-fire this parent mid-placement.
                                # IMPORTANT: only set status=STOPPED. Do NOT
                                # overwrite spx_target_t{1,2,3}_fired to True
                                # as cleanup — those flags record whether each
                                # tier was actually banked as a partial during
                                # the trade. Polluting them on stop misleads
                                # every downstream consumer (research_dump,
                                # ablation, _estimate_parent_r, BE-kill stats).
                                # status=STOPPED is the sole resolved-marker.
                                p.status = "STOPPED"
                                await SignalIterRepository.update_live_order(
                                    p.id, status="STOPPED",
                                )
                                await _market_sell(unfired_qty, "STOP")
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
                        # OPTIMISTIC LATCH (2026-07-06): flag BEFORE the awaited
                        # sell; revert ONLY on definitive refusal (placed=False)
                        # so a refused sell never phantom-banks the tier.
                        p.spx_target_t1_fired = True; p.spx_target_t1_at_spx = spot
                        await SignalIterRepository.update_live_order(
                            p.id, spx_target_t1_fired=True, spx_target_t1_at_spx=spot,
                        )
                        if await _market_sell(p.spx_target_t1_qty, "T1"):
                            await SignalIterRepository.update_live_order(
                                p.id, status="PARTIAL",
                            )
                        else:
                            p.spx_target_t1_fired = False; p.spx_target_t1_at_spx = None
                            await SignalIterRepository.update_live_order(
                                p.id, spx_target_t1_fired=False, spx_target_t1_at_spx=None,
                            )

                # T2
                if not p.spx_target_t2_fired and p.spx_target_t2 is not None and p.spx_target_t2_qty:
                    t2_hit = (
                        (is_long_calls and spot >= p.spx_target_t2)
                        or (not is_long_calls and spot <= p.spx_target_t2)
                    )
                    if t2_hit:
                        # OPTIMISTIC LATCH — see T1.
                        p.spx_target_t2_fired = True; p.spx_target_t2_at_spx = spot
                        await SignalIterRepository.update_live_order(
                            p.id, spx_target_t2_fired=True, spx_target_t2_at_spx=spot,
                        )
                        if await _market_sell(p.spx_target_t2_qty, "T2"):
                            await SignalIterRepository.update_live_order(
                                p.id, status="PARTIAL",
                            )
                        else:
                            p.spx_target_t2_fired = False; p.spx_target_t2_at_spx = None
                            await SignalIterRepository.update_live_order(
                                p.id, spx_target_t2_fired=False, spx_target_t2_at_spx=None,
                            )

                # T3
                if not p.spx_target_t3_fired and p.spx_target_t3 is not None and p.spx_target_t3_qty:
                    t3_hit = (
                        (is_long_calls and spot >= p.spx_target_t3)
                        or (not is_long_calls and spot <= p.spx_target_t3)
                    )
                    if t3_hit:
                        # OPTIMISTIC LATCH — see T1.
                        p.spx_target_t3_fired = True; p.spx_target_t3_at_spx = spot
                        await SignalIterRepository.update_live_order(
                            p.id, spx_target_t3_fired=True, spx_target_t3_at_spx=spot,
                        )
                        if await _market_sell(p.spx_target_t3_qty, "T3"):
                            await SignalIterRepository.update_live_order(
                                p.id, status="PARTIAL",
                            )
                        else:
                            p.spx_target_t3_fired = False; p.spx_target_t3_at_spx = None
                            await SignalIterRepository.update_live_order(
                                p.id, spx_target_t3_fired=False, spx_target_t3_at_spx=None,
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
                            # OPTIMISTIC LATCH — flag the runner tiers BEFORE
                            # the awaited sell; revert to their prior values on
                            # definitive refusal (a tier genuinely banked
                            # earlier must never be un-flagged).
                            prev_t2 = p.spx_target_t2_fired
                            prev_t3 = p.spx_target_t3_fired
                            p.spx_target_t2_fired = True; p.spx_target_t3_fired = True
                            await SignalIterRepository.update_live_order(
                                p.id, spx_target_t2_fired=True, spx_target_t3_fired=True,
                            )
                            if not await _market_sell(runner_qty, "RUNNER_PROTECT"):
                                p.spx_target_t2_fired = prev_t2
                                p.spx_target_t3_fired = prev_t3
                                await SignalIterRepository.update_live_order(
                                    p.id, spx_target_t2_fired=prev_t2,
                                    spx_target_t3_fired=prev_t3,
                                )

                # Sync parent status to CLOSED once the banked tiers cover the full
                # position (real remaining == 0) — not on the flags alone, so a
                # tier flagged without a completed sell can't orphan an open runner.
                if (p.spx_target_t1_fired and p.spx_target_t2_fired
                    and p.spx_target_t3_fired and p.status not in ("STOPPED", "CLOSED")
                    and _all_tiers_banked(
                        [p.spx_target_t1_qty or 0, p.spx_target_t2_qty or 0, p.spx_target_t3_qty or 0],
                        [p.spx_target_t1_fired, p.spx_target_t2_fired, p.spx_target_t3_fired],
                        p.qty,
                    )):
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
                        # OPTIMISTIC LATCH (2026-07-06): status=STOPPED BEFORE
                        # the awaited sell (previously written unconditionally
                        # after it — DRY/refused included) so the poll loop /
                        # a stale spawned check can't re-fire this parent
                        # mid-placement. Same as the polling-loop stop branch:
                        # status=STOPPED is the resolved-marker; don't overwrite
                        # the per-tier fired flags as cleanup — that pollutes
                        # partial-tracking for every downstream analytic.
                        p.status = "STOPPED"
                        await SignalIterRepository.update_live_order(
                            p.id, status="STOPPED",
                        )
                        if not isinstance(state.ibkr, LiveIBKRProvider):
                            await SignalIterRepository.write_audit_log(
                                category="spx_watcher", actor="system",
                                summary=(
                                    f"[DRY/TICK] STOP crossed: would market-sell-to-close "
                                    f"{unfired_qty}x {p.symbol} {p.strike}{p.right} at SPX {spot:.2f}"
                                ),
                            )
                        else:
                            from fef_sidecar.ibkr.safe_sell import safe_market_sell
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
                                # Contract-level pending-sell reservation.
                                # Prevents cross-parent races on same contract —
                                # the 7-parent SPX 7520C race that placed a
                                # naked short occurred here. safe_market_sell
                                # is the structural backstop if this reservation
                                # misses.
                                contract_key = (p.symbol, float(p.strike), p.right, p.expiry)
                                pending = state.firing_contracts.get(contract_key, 0)
                                available = max(0, long_qty - pending)
                                sell_qty = min(unfired_qty, available)
                                # LIFETIME SELL CAP (2026-07-06) — absolute
                                # oversell backstop, independent of the (stale-
                                # window-prone) broker position snapshot. See
                                # _lifetime_cap_reserve.
                                cap_qty = (
                                    _lifetime_cap_reserve(state, p, sell_qty)
                                    if sell_qty > 0 else 0
                                )
                                if sell_qty <= 0:
                                    await SignalIterRepository.write_audit_log(
                                        category="spx_watcher", actor="system",
                                        summary=(
                                            f"STOP/TICK SKIPPED: 0 available after reservation "
                                            f"(long={long_qty}, pending={pending}, requested={unfired_qty}) "
                                            f"for {p.symbol} {p.strike}{p.right} — refusing to short"
                                        ),
                                    )
                                elif cap_qty <= 0:
                                    sold, cap = _lifetime_cap_state(state, p)
                                    await SignalIterRepository.write_audit_log(
                                        category="spx_watcher", actor="system",
                                        summary=(
                                            f"STOP/TICK REFUSED by lifetime sell cap: watcher "
                                            f"already sold {sold}/{cap} for parent {p.id} "
                                            f"({p.symbol} {p.strike}{p.right}, requested={sell_qty}) "
                                            f"— refusing to oversell"
                                        ),
                                    )
                                else:
                                    sell_qty = cap_qty
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

                                    result = await safe_market_sell(
                                        ib, ic, sell_qty, purpose="watcher_close",
                                        state=state, parent_id=p.id,
                                    )
                                    if result.placed:
                                        tick_age_ms = (
                                            now_ms - state.last_live_tick_ms
                                            if state.last_live_tick_ms > 0 else -1
                                        )
                                        # Banked-$ audit line (spawned — see
                                        # _write_tier_fill_audit).
                                        asyncio.create_task(_write_tier_fill_audit(
                                            state, p, "STOP/TICK", result, f"SPX {spot:.2f}",
                                            extra_summary=(
                                                f"stop={effective_stop:.2f}, "
                                                f"tick_age={tick_age_ms}ms"
                                            ),
                                            extra_details={
                                                "stop_level": effective_stop,
                                                "tick_age_ms": tick_age_ms,
                                            },
                                        ))
                                        try:
                                            await SignalIterRepository.write_watcher_sell_child(
                                                parent=p, bracket_role="stop",
                                                qty=int(result.sell_qty),
                                                ib_order_id=int(result.trade.order.orderId),
                                                ib_perm_id=int(getattr(result.trade.order, "permId", 0) or 0) or None,
                                                notes=f"stop_tick@SPX={spot:.2f},age={tick_age_ms}ms",
                                            )
                                        except Exception:
                                            logger.exception(
                                                "stop_tick: write_watcher_sell_child failed for parent %s",
                                                p.id,
                                            )
                                        if int(result.sell_qty) < sell_qty:
                                            _lifetime_cap_release(
                                                state, p, sell_qty - int(result.sell_qty),
                                            )
                                    else:
                                        # safe_market_sell already wrote an
                                        # audit_log (HARD-BLOCK / no_position).
                                        _lifetime_cap_release(state, p, sell_qty)
                            except Exception:
                                logger.exception("spx_stop_tick_watcher market sell failed")
                        # (status=STOPPED already latched above, before the sell.)
                        # Refresh cache so the next ticks don't re-evaluate
                        # this just-stopped parent.
                        parents_cache = [c for c in parents_cache if c.id != p.id]
                    finally:
                        state.firing_parent_ids.discard(p.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("spx_stop_tick_watcher iteration failed")
