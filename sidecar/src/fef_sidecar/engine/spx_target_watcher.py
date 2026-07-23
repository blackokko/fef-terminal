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
    After T1 fires, if SPX retraces THROUGH the T1 PLAN level minus a small
    buffer (spx_target_t1 - _rp_buffer_pts) → market-sell remaining
    (runner_protect; never evaluated in the cycle that filled T1)

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


# Bound on the fire-path contract qualify (2026-07-23 exit-wedge incident):
# qualifyContractsAsync is a contract-details request — when the TWS CDS farm
# wedges, the future can pend FOREVER while order placement keeps working on
# the same connection (reqId-path vs orderId-path asymmetry). Unbounded, each
# wedged fire ate the full 30s FIRE DEADLINE (and, pre-handoff, stalled the
# whole poll loop with it). Bounded, a wedged qualify fails the attempt in
# seconds and lands on the normal refusal/backoff retry machinery.
_QUALIFY_TIMEOUT_SEC = float(os.environ.get("FEF_WATCHER_QUALIFY_TIMEOUT_SEC", "8.0"))


async def _qualified_ib_contract(state, p):
    """Cached IB option-contract lookup. qualifyContractsAsync round-trips
    to IBKR (~50-300ms) — caching it cuts that latency to ~0 for every
    market-sell after the first. Cache key = (symbol, strike, right,
    expiry). Cache lives on `state` and is hot for the sidecar lifetime.

    The qualify await is BOUNDED (_QUALIFY_TIMEOUT_SEC): a hung
    contract-details farm raises TimeoutError here instead of pending
    forever — callers treat it like any other pre-placement error
    (refusal rollback + backoff retry)."""
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
    await asyncio.wait_for(
        ib.qualifyContractsAsync(ic), timeout=_QUALIFY_TIMEOUT_SEC,
    )
    state._qualified_ib_contracts[key] = ic  # type: ignore[attr-defined]
    return ic


def _fmt_money(x: float) -> str:
    """-290.4 -> '-$290', 290.4 -> '+$290'."""
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.0f}"


def _rp_buffer_pts(level_ref: float) -> float:
    """Runner-protect retrace buffer, in the symbol's own points.

    2026-07-08 FIX (live defect, 3-for-3 on 7/8): runner-protect anchored at
    the T1 FILL-MOMENT spot (spx_target_t1_at_spx) and compared spot <=
    anchor — in the very cycle T1 filled, spot == anchor, so every T1 fill
    instantly dumped the runners at the same price ("sell all at T1"). The
    BACKTESTED semantics (the +$18-25/ct ladder evidence) anchor at the T1
    PLAN level and require a genuine retrace THROUGH it: calls fire at
    spot <= t1_level - buffer, puts at spot >= t1_level + buffer.

    Buffer = FEF_RP_BUFFER_PTS (default 0.5) SPX-calibrated points, scaled
    by the symbol's notional (_price_scale: 1.0 for SPX-class, spot/7500
    below 2000) so ETF-class parents get the same effective moneyness
    cushion. level_ref = the T1 plan level (same price scale as spot)."""
    from fef_sidecar.engine.strikes import _price_scale
    try:
        base = float(os.environ.get("FEF_RP_BUFFER_PTS", "0.5"))
    except (TypeError, ValueError):
        base = 0.5
    return base * _price_scale(float(level_ref))


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
    quote labeled '(quote)'.

    entry (2026-07-08 fix): the parent's ACTUAL average entry fill
    (avg_fill_price) — re-read from the DB when the captured `p` predates
    the fill sync (this audit task polls the SELL fill up to ~2s, plenty of
    time for _sync_parent_fill_from_trades to land the BUY fill). Only when
    no fill price exists anywhere does it fall back to the placement quote
    (limit_price), and then the summary's banked figures carry an '(est)'
    marker so a quote-based $ can never read as realized P&L.

    ref_desc = trigger context ('SPX 6712.50' or the fixed_dollar trigger
    premium). Runs as a spawned task so fill-price polling never blocks the
    watcher hot loop.
    """
    from fef_sidecar.db.repository import SignalIterRepository
    try:
        order_id = result.trade.order.orderId
        premium, premium_src = await _premium_for_log(state, result.trade, p)
        entry = p.avg_fill_price if (p.avg_fill_price or 0) > 0 else None
        entry_src = "parent_fill"
        if entry is None and p.id is not None:
            # The in-memory parent may predate the entry-fill sync — the DB
            # row is the freshest cheap source at audit time.
            try:
                from sqlmodel import select as _sm_select
                from fef_sidecar.db.engine import get_session as _gs_audit
                from fef_sidecar.db.models import LiveOrder as _LO_audit
                async with _gs_audit() as _session_audit:
                    _row = (await _session_audit.exec(
                        _sm_select(_LO_audit).where(_LO_audit.id == p.id)
                    )).first()
                if _row is not None and (_row.avg_fill_price or 0) > 0:
                    entry = _row.avg_fill_price
                    entry_src = "parent_fill_db"
            except Exception:
                logger.exception(
                    "tier fill audit: entry fill re-read failed for parent %s",
                    p.id,
                )
        if entry is None:
            entry = p.limit_price
            entry_src = "parent_limit"

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
            # '(est)' = entry side is the placement quote, not a real fill —
            # the banked $ is an estimate until the entry fill syncs.
            est = " (est)" if entry_src == "parent_limit" else ""
            summary = (
                f"{tier_label}: sold {result.sell_qty}x {p.symbol} {p.strike}{p.right} "
                f"@ {px_label} -> banked {_fmt_money(banked)}{est} this fill "
                f"(cum {_fmt_money(cum)}{est}; position mark {_fmt_money(position_mark)}{est}) "
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
        # BOUNDED (2026-07-23 exit-wedge hardening): an unbounded quote on a
        # sell-capable path + the in-flight latch would silently disable this
        # parent's dollar-cap checks for as long as the broker wedges.
        opt_quote = await asyncio.wait_for(
            state.ibkr.get_option_quote(Contract(
                symbol=p.symbol, kind=ContractKind.OPTION,
                exchange="CBOE" if p.symbol == "SPX" else "SMART",
                expiry=p.expiry, strike=p.strike, right=p.right,
            )),
            timeout=6.0,
        )
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
        # Entry reference: the REAL fill when synced (fill-sync stamps it
        # within a cycle), else limit_price. 2026-07-16: limit_price on auto
        # parents is now the marketable LMT (ask + buffer), slightly above
        # the old raw-ask value — preferring avg_fill_price keeps the -$1800
        # cap measured from what was actually paid, not the buffered cap.
        _entry_ref = p.avg_fill_price if (p.avg_fill_price or 0) > 0 else p.limit_price
        pnl = (mid - _entry_ref) * open_qty * 100
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

        placed_qty = 0
        try:
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
                return
            placed_qty = int(result.sell_qty)
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
        finally:
            # CAP-LEAK GUARD (2026-07-07 review BLOCKER-2): release whatever
            # was reserved but not confirmed placed — refusal, exception, or
            # cancellation alike.
            if placed_qty < sell_qty:
                _lifetime_cap_release(state, p, sell_qty - placed_qty)
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


# Sell-attempt outcomes that must NOT keep an optimistic terminal latch:
# no order was placed AND the refusal is expected to clear (fill propagation)
# or demands escalation (cap-vs-broker divergence) — the 2026-07-07 eviction:
# "0 available after reservation" ~40s after entry, STOPPED persisted before
# the sell, refusal path returned without reverting -> parent dropped from
# the watcher scan with contracts still open, no alert.
#   "refused_guard"          — anti-short reservation / no_position; clears
#                              once ib.positions() reflects the entry fill.
#   "refused_cap_divergence" — lifetime cap says fully sold but the broker
#                              still shows the position (a leaked reservation
#                              or counter drift); keep managed + escalate,
#                              bounded by the refusal streak -> park.
#   "error"                  — no order placed (exception before placement).
# Plain "refused_cap" (cap sold-out AND broker flat/pending-covered) stays
# terminal — reverting it would reopen a ghost. "dry" keeps DRY-mode
# resolved-marker semantics.
_RETRYABLE_SELL_OUTCOMES = ("refused_guard", "refused_cap_divergence", "error")

# Back-off between retryable refused sell attempts per parent — bounds the
# audit volume of the retry loop (0.1s poll + tick path would otherwise spam
# a SKIPPED + refusal row pair per tick until the fill propagates).
_REFUSAL_BACKOFF_SEC = float(os.environ.get("FEF_WATCHER_REFUSAL_BACKOFF_SEC", "2.0"))

# Refused-sell streak budget (2026-07-07 review MAJOR-5): a parent whose
# sells are refused indefinitely (e.g. the user manually flattened a shared
# strike) must not flap FILLED<->STOPPED at the backoff cadence forever
# (~5-7k DB writes/hour). Past the budget the parent is PARKED: broker
# net-flat -> CLOSED_EXTERNAL, else STUCK — both leave the watcher scan
# filter — with ONE loud category="watcher_stuck" row. A >60s gap between
# refusals starts a fresh streak; any placed order clears it.
_REFUSAL_MAX_ATTEMPTS = int(os.environ.get("FEF_WATCHER_REFUSAL_MAX_ATTEMPTS", "20"))
_REFUSAL_WINDOW_SEC = float(os.environ.get("FEF_WATCHER_REFUSAL_WINDOW_SEC", "90"))


def _refusal_backoff_active(state, p) -> bool:
    until = getattr(state, "_watcher_refusal_backoff_ms", {}).get(p.id, 0)
    return int(_time.time() * 1000) < until


def _set_refusal_backoff(state, p) -> None:
    if not hasattr(state, "_watcher_refusal_backoff_ms"):
        state._watcher_refusal_backoff_ms = {}  # type: ignore[attr-defined]
    state._watcher_refusal_backoff_ms[p.id] = (  # type: ignore[attr-defined]
        int(_time.time() * 1000) + int(_REFUSAL_BACKOFF_SEC * 1000)
    )


def _note_refusal_over_limit(state, p) -> bool:
    """Bump the per-parent refused-sell streak; True when the streak exceeds
    _REFUSAL_MAX_ATTEMPTS or spans _REFUSAL_WINDOW_SEC and the parent must be
    parked instead of retried again."""
    if p.id is None:
        return False
    if not hasattr(state, "_watcher_refusal_streak"):
        state._watcher_refusal_streak = {}  # type: ignore[attr-defined]
    now_ms = int(_time.time() * 1000)
    entry = state._watcher_refusal_streak.get(p.id)  # type: ignore[attr-defined]
    if entry is None or now_ms - entry[2] > 60_000:
        entry = (0, now_ms, now_ms)  # (count, first_ms, last_ms) — new streak
    count = entry[0] + 1
    state._watcher_refusal_streak[p.id] = (count, entry[1], now_ms)  # type: ignore[attr-defined]
    return (count >= _REFUSAL_MAX_ATTEMPTS
            or (now_ms - entry[1]) / 1000.0 >= _REFUSAL_WINDOW_SEC)


def _clear_refusal_streak(state, p) -> None:
    getattr(state, "_watcher_refusal_streak", {}).pop(p.id, None)


async def _broker_long_qty_fresh(state, p) -> int:
    """Net long at the broker for p's contract from a FRESH awaited
    reqPositionsAsync round-trip; -1 when unknown (non-live provider,
    disconnected, or request error).

    NOT the ib_async positions cache (re-review N4): during a TWS
    restart/reconnect the cache is legitimately EMPTY for longer than the
    refusal-streak window — classifying a live position as flat off the
    cache would park it CLOSED_EXTERNAL with a receipt claiming the user
    closed it. Flat may only be concluded from a connected session's fresh
    answer; anything less certain returns -1 (callers must fail to STUCK)."""
    try:
        from fef_sidecar.ibkr.live import LiveIBKRProvider
        if not isinstance(state.ibkr, LiveIBKRProvider):
            return -1
        ib = state.ibkr._ib  # type: ignore[attr-defined]
        if ib is None or not ib.isConnected():
            return -1
        positions = await asyncio.wait_for(ib.reqPositionsAsync(), timeout=10.0)
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
        return long_qty
    except Exception:
        return -1


async def _park_stuck_parent(
    state, p, tier_label: str, outcome_kind: str | None, ref_desc: str,
) -> None:
    """Refused-sell streak exceeded the retry budget — stop flapping. Broker
    net-flat means the position was closed outside the watcher (manual
    flatten of a shared strike) -> CLOSED_EXTERNAL; still long or unknown ->
    STUCK for manual intervention. Both statuses leave the watcher scan
    filter; the single watcher_stuck row is the loud alert.

    CLOSED_EXTERNAL requires a CONNECTED session's fresh reqPositionsAsync
    confirming flat (re-review N4) — an empty positions cache during a TWS
    restart is not evidence of anything. Unknown/-1 -> STUCK. The periodic
    position reconcile re-checks parked parents every few minutes and
    resolves/re-adopts them as broker truth firms up."""
    from fef_sidecar.db.repository import SignalIterRepository
    long_qty = await _broker_long_qty_fresh(state, p)
    new_status = "CLOSED_EXTERNAL" if long_qty == 0 else "STUCK"
    try:
        p.status = new_status
        await SignalIterRepository.update_live_order(p.id, status=new_status)
    except Exception:
        logger.exception("park: status write failed for parent %s", p.id)
    try:
        await SignalIterRepository.write_audit_log(
            category="watcher_stuck", actor="system",
            summary=(
                f"PARKED {new_status}: parent {p.id} {p.symbol} {p.strike}{p.right} — "
                f"refused-{tier_label}-sell streak exhausted the retry budget "
                f"({_REFUSAL_MAX_ATTEMPTS} attempts / {_REFUSAL_WINDOW_SEC:.0f}s; "
                f"last outcome {outcome_kind or 'unknown'} at {ref_desc}); "
                f"broker long={'unknown' if long_qty < 0 else long_qty}"
                + (" — position closed outside the watcher" if long_qty == 0
                   else " — MANUAL INTERVENTION REQUIRED")
            ),
            details={
                "parent_id": p.id, "status": new_status,
                "broker_long": long_qty, "tier": tier_label,
                "outcome": outcome_kind, "ref": ref_desc,
            },
        )
    except Exception:
        logger.exception("park: audit write failed for parent %s", p.id)
    _clear_refusal_streak(state, p)


async def drain_detached_fires(state, timeout: float = 15.0) -> int:
    """Await in-flight detached exit fires (bounded) — called by the lifespan
    teardown BEFORE loops are cancelled and the DB is disposed (re-review
    N6): a fire cancelled mid-sell at teardown would race its rollback write
    against dispose_db(). Returns how many fires were still unfinished at
    the timeout (0 = clean)."""
    fires = {
        t for t in (getattr(state, "_watcher_detached_fires", None) or set())
        if not t.done()
    }
    if not fires:
        return 0
    logger.info("draining %d in-flight exit fire(s) before teardown", len(fires))
    try:
        _done, pending = await asyncio.wait(fires, timeout=timeout)
        for t in pending:
            logger.error("exit fire still running after the %.0fs teardown drain", timeout)
        return len(pending)
    except Exception:
        logger.exception("detached-fire drain failed")
        return len(fires)


async def _run_exit_fire_shielded(state, fire_factory):
    """BLOCKER-1 (2026-07-07 review): the watcher task can be CANCELLED
    mid-sell (watchdog stale-respawn, /config/symbol respawn, shutdown) and
    CancelledError bypasses every except-Exception rollback — an optimistic
    latch would strand the parent exactly like the original eviction. Each
    latch->sell->rollback critical section runs as its OWN task behind
    asyncio.shield: outer cancellation propagates to the caller immediately
    while the fire keeps running detached to completion — its rollback and
    firing_parent_ids release included. Detached fires are strongly
    referenced on state (asyncio holds tasks weakly).

    DEADLINE (re-review N1): a wedged sell (hung TWS socket inside qualify —
    the original incident trigger) must not block the poll loop and freeze
    every OTHER parent's exits. After FEF_FIRE_DEADLINE_SEC (default 30) the
    INNER fire task is cancelled — every fire's finally-gated rollback + cap
    release make that safe (N2/N3) — and the attempt lands on the normal
    refused/error retry path via the fire's own rollback. Fire start times
    are recorded (state._watcher_fire_started_mono) so the watchdog deferral
    can age-bound itself instead of freezing on a wedge."""
    if getattr(state, "_watcher_teardown", False):
        # TEARDOWN GATE (re-review R4): fires started during the shutdown
        # drain escape its snapshot and their rollbacks race the disposed
        # DB. Refuse PRE-LATCH (the factory is never invoked, so there is
        # nothing to roll back); audited once per process.
        if not getattr(state, "_watcher_teardown_refusal_logged", False):
            try:
                state._watcher_teardown_refusal_logged = True  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                from fef_sidecar.db.repository import SignalIterRepository
                await SignalIterRepository.write_audit_log(
                    category="spx_watcher", actor="system",
                    summary=(
                        "TEARDOWN: refusing to start new exit fires — sidecar "
                        "is shutting down; open parents resume on next boot "
                        "(reconcile re-arms)"
                    ),
                )
            except Exception:
                pass
        return None
    deadline = float(os.environ.get("FEF_FIRE_DEADLINE_SEC", "30"))
    task = asyncio.ensure_future(fire_factory())
    fires = getattr(state, "_watcher_detached_fires", None)
    if fires is None:
        fires = set()
        try:
            state._watcher_detached_fires = fires  # type: ignore[attr-defined]
        except Exception:
            pass
    starts = getattr(state, "_watcher_fire_started_mono", None)
    if starts is None:
        starts = {}
        try:
            state._watcher_fire_started_mono = starts  # type: ignore[attr-defined]
        except Exception:
            pass
    fires.add(task)
    starts[task] = _time.monotonic()

    def _cleanup(t) -> None:
        fires.discard(t)
        starts.pop(t, None)

    task.add_done_callback(_cleanup)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=deadline)
    except (asyncio.TimeoutError, TimeoutError):
        logger.error(
            "exit fire exceeded the %.0fs deadline — cancelling the wedged sell "
            "(rollback/backoff handled by the fire's own finally)", deadline,
        )
        task.cancel()
        try:
            from fef_sidecar.db.repository import SignalIterRepository
            await SignalIterRepository.write_audit_log(
                category="spx_watcher", actor="system",
                summary=(
                    f"FIRE DEADLINE: an exit fire exceeded {deadline:.0f}s "
                    f"(wedged broker call?) — cancelled; the fire's rollback "
                    f"keeps the parent managed and the backoff schedules a retry"
                ),
            )
        except Exception:
            pass
        return None


# Inline budget for exit fires (2026-07-23 exit-wedge incident): the poll
# loop used to await _run_exit_fire_shielded INLINE, so ONE parent's wedged
# broker call froze stop/tier detection for EVERY parent for the full 30s
# FIRE DEADLINE — twice in a row live. Fires still run inline in the healthy
# case (sub-second; preserves the existing same-cycle ordering guarantees and
# test semantics); a fire exceeding this budget is handed off DETACHED — the
# wrapper task carries the 30s deadline + finally-gated rollback with it, so
# loop cancellation (symbol-switch respawn, the incident's second-order
# hazard) can never orphan a wedged fire without its deadline again.
_FIRE_INLINE_BUDGET_SEC = float(os.environ.get("FEF_FIRE_INLINE_BUDGET_SEC", "2.0"))
# Throttle for the handoff audit row (one per parent per interval).
_FIRE_HANDOFF_LOG_INTERVAL_MS = 60_000


def _fire_handoff_inflight(state, parent_id) -> bool:
    """True while a handed-off (detached) fire wrapper for this parent is
    still running — callers must not start another fire for the parent."""
    handoff = getattr(state, "_watcher_fire_handoff", None) or {}
    task = handoff.get(parent_id)
    return task is not None and not task.done()


async def _fire_with_inline_budget(state, p, fire_factory, tier_label: str):
    """Run one exit fire through _run_exit_fire_shielded, awaiting it inline
    only up to _FIRE_INLINE_BUDGET_SEC.

      * Healthy fire (qualify cached or fast, order placed): completes inline
        — identical semantics to the pre-2026-07-23 code.
      * Wedged fire: after the budget the WRAPPER keeps running detached
        (strongly referenced on state._watcher_fire_handoff); its own 30s
        deadline cancels the wedged sell and the fire's finally-gated
        rollback/backoff keep the parent managed. The caller returns None and
        the loop continues — other parents' exits are never frozen.
      * Single-flight per parent: while a handed-off wrapper is in flight,
        further fire attempts for the SAME parent are refused here (sync
        check, no coroutine created), on top of the firing_parent_ids latch
        inside every fire body.

    Loop-cancellation safety (the incident's silent-leak class): the deadline
    used to live in the CALLER's await — a /config/symbol respawn cancelling
    the poll loop mid-deadline left the shielded fire running with NO deadline
    and the firing latch held. The wrapper task now owns the deadline, so it
    survives caller cancellation and always resolves the fire one way or the
    other."""
    if p.id is not None and _fire_handoff_inflight(state, p.id):
        return None
    if not hasattr(state, "_watcher_fire_handoff"):
        try:
            state._watcher_fire_handoff = {}  # type: ignore[attr-defined]
        except Exception:
            # attr-frozen state (unit-test fakes) — fall back to inline await.
            return await _run_exit_fire_shielded(state, fire_factory)
    handoff: dict = state._watcher_fire_handoff  # type: ignore[attr-defined]
    task = asyncio.ensure_future(_run_exit_fire_shielded(state, fire_factory))
    if p.id is not None:
        handoff[p.id] = task

        def _cleanup(t, pid=p.id) -> None:
            if handoff.get(pid) is t:
                handoff.pop(pid, None)

        task.add_done_callback(_cleanup)
    try:
        return await asyncio.wait_for(
            asyncio.shield(task), timeout=_FIRE_INLINE_BUDGET_SEC,
        )
    except (asyncio.TimeoutError, TimeoutError):
        # Handed off — the wrapper's own deadline/rollback owns it now.
        now_ms = int(_time.time() * 1000)
        if _throttled(state, "_watcher_fire_handoff_log_ms", p.id, now_ms,
                      interval_ms=_FIRE_HANDOFF_LOG_INTERVAL_MS):
            try:
                from fef_sidecar.db.repository import SignalIterRepository
                await SignalIterRepository.write_audit_log(
                    category="spx_watcher", actor="system",
                    summary=(
                        f"FIRE HANDOFF: {tier_label} fire for parent {p.id} "
                        f"{p.symbol} {p.strike}{p.right} exceeded the "
                        f"{_FIRE_INLINE_BUDGET_SEC:.1f}s inline budget — loop "
                        f"continues; fire runs detached under its own "
                        f"deadline/rollback"
                    ),
                )
            except Exception:
                pass
        return None


async def _rollback_refused_terminal_latch(
    state, p, *, latched_status: str, prior_status: str,
    tier_label: str, outcome_kind: str | None, ref_desc: str,
) -> None:
    """REFUSAL ROLLBACK (2026-07-07 eviction incident): a terminal status may
    only survive a sell that actually PLACED an order. Reverts the optimistic
    status (in memory + persisted) so the parent stays inside the watcher's
    SUBMITTED/FILLED/PARTIAL scan filter and the sell is retried, and writes
    the loud watcher_refusal_retry row.

    Ordering (review MINOR-7): the backoff is set FIRST (sync — it must hold
    even when the writes below fail); a streak past the retry budget PARKS
    the parent instead of reverting (MAJOR-5); the status revert retries once
    — a failed revert IS the eviction hole, so the fallback is a loud
    watcher_stuck row. Best-effort — never raises into the caller's finally."""
    from fef_sidecar.db.repository import SignalIterRepository
    try:
        _set_refusal_backoff(state, p)
        if _note_refusal_over_limit(state, p):
            await _park_stuck_parent(state, p, tier_label, outcome_kind, ref_desc)
            return
        p.status = prior_status
        reverted = False
        for _attempt in (1, 2):
            try:
                await SignalIterRepository.update_live_order(p.id, status=prior_status)
                reverted = True
                break
            except Exception:
                logger.exception(
                    "refusal rollback write failed (attempt %d) for parent %s",
                    _attempt, p.id,
                )
        if not reverted:
            try:
                await SignalIterRepository.write_audit_log(
                    category="watcher_stuck", actor="system",
                    summary=(
                        f"ROLLBACK WRITE FAILED for parent {p.id} {p.symbol} "
                        f"{p.strike}{p.right} — DB may still show {latched_status} "
                        f"with contracts open; MANUAL CHECK REQUIRED"
                    ),
                    details={"parent_id": p.id, "latched_status": latched_status,
                             "intended_status": prior_status},
                )
            except Exception:
                logger.exception("rollback-failure audit failed for parent %s", p.id)
            return
        await SignalIterRepository.write_audit_log(
            category="watcher_refusal_retry", actor="system",
            summary=(
                f"{tier_label} sell REFUSED ({outcome_kind or 'unknown'}) for parent {p.id} "
                f"{p.symbol} {p.strike}{p.right} at {ref_desc} — status reverted "
                f"{latched_status}->{prior_status}; parent stays managed, retry in "
                f"{_REFUSAL_BACKOFF_SEC:.0f}s"
            ),
            details={
                "parent_id": p.id, "tier": tier_label,
                "outcome": outcome_kind, "latched_status": latched_status,
                "reverted_to": prior_status, "ref": ref_desc,
            },
        )
    except Exception:
        logger.exception(
            "refusal rollback failed for parent %s (%s)", p.id, tier_label,
        )


async def _audit_tier_refusal_retry(
    state, p, tier_label: str, outcome_kind: str | None, ref_desc: str,
) -> None:
    """watcher_refusal_retry row for a tier whose optimistic fired-flag was
    reverted after a retryable refusal (the flag revert itself is done by the
    caller — it predates this incident and stays as-is). Shares the backoff +
    streak budget with the status rollback path (MAJOR-5)."""
    from fef_sidecar.db.repository import SignalIterRepository
    try:
        _set_refusal_backoff(state, p)
        if _note_refusal_over_limit(state, p):
            await _park_stuck_parent(state, p, tier_label, outcome_kind, ref_desc)
            return
        await SignalIterRepository.write_audit_log(
            category="watcher_refusal_retry", actor="system",
            summary=(
                f"{tier_label} sell REFUSED ({outcome_kind or 'unknown'}) for parent {p.id} "
                f"{p.symbol} {p.strike}{p.right} at {ref_desc} — fired-flag reverted; "
                f"tier stays armed, retry in {_REFUSAL_BACKOFF_SEC:.0f}s"
            ),
            details={
                "parent_id": p.id, "tier": tier_label,
                "outcome": outcome_kind, "ref": ref_desc,
            },
        )
    except Exception:
        logger.exception(
            "tier refusal audit failed for parent %s (%s)", p.id, tier_label,
        )


# --- Entry-fill grace (2026-07-07) -----------------------------------------
# Stop/tier triggers must not be CONSUMED before the entry fill is confirmed:
# both incident parents crossed their (corrupt) stops ~40s after entry while
# ib.positions() had not yet reflected the buy — every sell was refused by
# the anti-short guard, and the optimistic latch then evicted the parents.
# Evaluation is skipped until filled_qty/avg_fill_price sync in or the broker
# position is visible; after FEF_ENTRY_FILL_GRACE_SEC (default 120s) it
# proceeds regardless (fail-open, audited once) so a broken fill-sync can
# never silently disable exit management.
_ENTRY_FILL_GRACE_SEC = float(os.environ.get("FEF_ENTRY_FILL_GRACE_SEC", "120"))


def _entry_fill_confirmed(state, p) -> bool:
    if int(getattr(p, "filled_qty", 0) or 0) > 0:
        return True
    if (p.avg_fill_price or 0) > 0:
        return True
    try:
        from fef_sidecar.ibkr.live import LiveIBKRProvider
        if isinstance(state.ibkr, LiveIBKRProvider):
            ib = state.ibkr._ib  # type: ignore[attr-defined]
            for pos in ib.positions():
                c = pos.contract
                if (getattr(c, "secType", "") == "OPT"
                        and getattr(c, "symbol", "") == p.symbol
                        and abs(float(getattr(c, "strike", 0)) - p.strike) < 0.01
                        and getattr(c, "right", "") == p.right
                        and getattr(c, "lastTradeDateOrContractMonth", "") == p.expiry
                        and pos.position > 0):
                    return True
    except Exception:
        pass
    return False


async def _entry_fill_grace_pending(state, p) -> bool:
    """True while stop/tier evaluation must be SKIPPED for this parent (entry
    fill unconfirmed and grace window still open). See block comment above."""
    from fef_sidecar.db.repository import SignalIterRepository
    if p.id is None or _entry_fill_confirmed(state, p):
        return False
    now_ms = int(_time.time() * 1000)
    start_ms = None
    sub = getattr(p, "submitted_at", None)
    if sub is not None:
        try:
            from datetime import UTC
            if sub.tzinfo is None:
                sub = sub.replace(tzinfo=UTC)  # DB round-trips drop tzinfo; stored UTC
            start_ms = int(sub.timestamp() * 1000)
        except Exception:
            start_ms = None
    if start_ms is None:
        # No usable submit time — anchor on first sight (process-local).
        if not hasattr(state, "_watcher_fill_grace_seen_ms"):
            state._watcher_fill_grace_seen_ms = {}  # type: ignore[attr-defined]
        start_ms = state._watcher_fill_grace_seen_ms.setdefault(p.id, now_ms)  # type: ignore[attr-defined]
    if (now_ms - start_ms) / 1000.0 < _ENTRY_FILL_GRACE_SEC:
        return True
    # Fail-open: grace expired without a confirmed fill — manage anyway
    # (the guards downstream still refuse to short) and say so once.
    if not hasattr(state, "_watcher_fill_grace_expired"):
        state._watcher_fill_grace_expired = set()  # type: ignore[attr-defined]
    if p.id not in state._watcher_fill_grace_expired:  # type: ignore[attr-defined]
        state._watcher_fill_grace_expired.add(p.id)  # type: ignore[attr-defined]
        try:
            await SignalIterRepository.write_audit_log(
                category="spx_watcher", actor="system",
                summary=(
                    f"ENTRY_FILL_GRACE expired ({_ENTRY_FILL_GRACE_SEC:.0f}s) for parent "
                    f"{p.id} {p.symbol} {p.strike}{p.right} — no confirmed fill; "
                    f"evaluating stop/tiers anyway (fail-open)"
                ),
            )
        except Exception:
            pass
    return False


# --- Arm-time plan sanity / re-anchor (2026-07-07) --------------------------
# build_plan geometry at scale 1.0: stop 10pt, T1 15pt (1.5R), T2 25pt (2.5R),
# T3 50pt (5R). Non-SPX symbols scale by _price_scale (strikes.py).
_PLAN_EXPECTED_STOP_PTS = 10.0
_PLAN_EXPECTED_T_PTS = (15.0, 25.0, 50.0)
_PLAN_MAX_REL_DEV = 0.40
# Re-anchor TRIGGER is one-sided (review MAJOR-4): only a COLLAPSED or
# wrong-side R is corrupt (the incident's 0.38pt). A wider-than-expected R
# is routine on fast tape (verdict->fill latency moves the entry ref beyond
# the scanner spot) and legitimately wall-anchored plans must survive it.
_PLAN_MIN_STOP_RATIO = 0.60
# entry ref / symbol spot disagreeing by more than this RATIO is the
# cross-symbol capture signature (SPX 7530.28 recorded on a SPY parent).
_PLAN_UNITS_RATIO = 3.0
# Spot sources trustworthy enough to REPAIR a cross-unit entry ref (review
# MINOR-8): a 1-min bar can be up to 10 minutes old — never anchor to it.
# 'ibkr_stock_quote' (2026-07-17) is a live quote TTL-cached <=5s — fresher
# than the scanner snapshot's 180s bound, so it qualifies.
_PLAN_REPAIR_SPOT_SOURCES = (
    "active_tick", "scanner_snapshot", "tick_stream", "ibkr_stock_quote",
)
# SAME-SCALE entry-pollution threshold (2026-07-23 incident B): an ES-priced
# entry ref on an SPX parent sits ~0.3-0.7% above cash (the futures basis) —
# far under the 3x units ratio, and today's live deviation (0.477%) slipped
# just UNDER the shadow guard's 0.5%. Legitimate drift between the verdict
# spot and the watcher's first evaluation is seconds-scale (<0.1% even on
# 2026-07-23's violent tape), so 0.25% cleanly separates the two.
_PLAN_ENTRY_MAX_DEV = float(os.environ.get("FEF_PLAN_ENTRY_MAX_DEV", "0.0025"))


async def _maybe_reanchor_plan(
    state, p, sym_spot: float | None, spot_src: str | None = "active_tick",
) -> bool:
    """ARM-TIME PLAN SANITY (2026-07-07 incident): the scanner builds
    T1/T2/T3/STOP from its own per-symbol QD spot while entry_spx_price is
    captured from the active-chart tick (auto_trader) — two price sources.
    Parent 751: plan ref 7520.66 vs entry 7530.28 -> |stop-entry| = 0.38pt,
    R collapsed 10 -> 0.38, the BE ladder insta-stepped to 1.5R inside 200ms
    and the ratcheted stop crossed ~40s later.

    Trigger (one-sided, review MAJOR-4): re-anchor only when the stop sits on
    the WRONG SIDE of entry or the offset COLLAPSED below
    _PLAN_MIN_STOP_RATIO x the expected 10 x _price_scale points — a
    wider-than-expected R (fast tape) is left alone. On violation, all four
    levels are re-anchored as offsets from entry_spx_price (stop -/+10,
    T1 +/-15, T2 +/-25, T3 +/-50, scaled); a target is preserved only when
    its own offset is within _PLAN_MAX_REL_DEV of expected on the profit
    side (wall-anchored levels that agree with the entry survive).
    dynamic_stop_level + be_ladder_max_step_R reset alongside.

    An entry_spx_price in the wrong UNITS entirely (ratio > 3x vs the
    symbol's own spot — cross-symbol capture) is first repaired to sym_spot;
    the repair requires fresh provenance (_PLAN_REPAIR_SPOT_SOURCES) — with
    only a stale bar spot available the parent is NOT evaluated this cycle
    (returns False; review MINOR-8).

    Checked once per parent (memoized). Re-anchored values are re-applied
    to stale in-memory copies (the tick loop's 500ms parent cache).
    Persists + audits category="plan_reanchored" with before/after.
    Returns True when the caller may evaluate the parent this cycle."""
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.engine.strikes import _price_scale
    if p.id is None:
        return True
    if not hasattr(state, "_watcher_plan_sanity"):
        state._watcher_plan_sanity = {}  # type: ignore[attr-defined]
    memo = state._watcher_plan_sanity  # type: ignore[attr-defined]
    if p.id in memo:
        fixed = memo[p.id]
        if fixed:
            # Re-apply the STATIC plan fields to a stale copy (tick cache).
            # Ladder state (dynamic_stop_level / be_ladder_max_step_R) is
            # NOT memoized — it legitimately ratchets after the re-anchor
            # and must never be rewound to its reset value.
            for k, v in fixed.items():
                setattr(p, k, v)
        return True
    entry = float(p.entry_spx_price) if (p.entry_spx_price or 0) > 0 else None
    stop = float(p.spx_target_stop) if p.spx_target_stop is not None else None
    if entry is None or stop is None or not sym_spot or sym_spot <= 0:
        return True  # nothing to validate yet — retry next cycle (no memo)
    before = {
        "entry_spx_price": entry, "spx_target_t1": p.spx_target_t1,
        "spx_target_t2": p.spx_target_t2, "spx_target_t3": p.spx_target_t3,
        "spx_target_stop": stop, "dynamic_stop_level": p.dynamic_stop_level,
        "be_ladder_max_step_R": p.be_ladder_max_step_R,
    }
    updates: dict = {}
    ratio = entry / float(sym_spot)
    entry_repaired = ratio > _PLAN_UNITS_RATIO or ratio < 1.0 / _PLAN_UNITS_RATIO
    if entry_repaired:
        if spot_src not in _PLAN_REPAIR_SPOT_SOURCES:
            # Cross-unit entry ref but no trustworthy anchor this cycle —
            # skip evaluation entirely rather than repair against a spot
            # that may be minutes old (no memo; retried when a fresh
            # tick/snapshot resolves).
            return False
        entry = float(sym_spot)
        updates["entry_spx_price"] = entry
    elif abs(entry - float(sym_spot)) / float(sym_spot) > _PLAN_ENTRY_MAX_DEV:
        # SAME-SCALE POLLUTION (2026-07-23 incident B): an ES-priced
        # entry_spx_price on an SPX parent sits ~0.3-0.7% above cash — far
        # under the 3x units ratio, so the cross-unit branch never sees it.
        # For PUTS the polluted entry put the CORRECT scanner stop "on the
        # wrong side" and this function then rewrote a healthy plan onto the
        # bad anchor (parent 799: entry 7464.50 vs cash 7429.04 -> stop
        # 7439.04 -> 7474.5, instant T1/T2 + 3-rung BE burst); for CALLS it
        # silently inflated the ladder risk ~4x and disabled every BE step
        # (parent 796 rode to the full -10pt stop, -$1,770). Threshold =
        # _PLAN_ENTRY_MAX_DEV (0.25% — the live deviation was 0.477%, and
        # legitimate verdict->first-eval drift is well under 0.1%). Repair
        # requires the same fresh-provenance gate as the cross-unit branch.
        if spot_src not in _PLAN_REPAIR_SPOT_SOURCES:
            return False
        entry = float(sym_spot)
        updates["entry_spx_price"] = entry
        entry_repaired = True
    scale = _price_scale(float(sym_spot))
    expected_stop = _PLAN_EXPECTED_STOP_PTS * scale
    sign = 1.0 if p.right == "C" else -1.0
    stop_off = sign * (entry - stop)  # >0 iff stop is on the loss side
    stop_ok = stop_off >= _PLAN_MIN_STOP_RATIO * expected_stop
    if stop_ok and not entry_repaired:
        memo[p.id] = None
        return True
    if not stop_ok:
        new_stop = entry - sign * expected_stop
        updates["spx_target_stop"] = new_stop
        for fld, pts in zip(
            ("spx_target_t1", "spx_target_t2", "spx_target_t3"),
            _PLAN_EXPECTED_T_PTS,
        ):
            cur = getattr(p, fld)
            exp_off = pts * scale
            keep = False
            if cur is not None:
                off = sign * (float(cur) - entry)  # >0 iff on the profit side
                keep = off > 0 and abs(off - exp_off) / exp_off <= _PLAN_MAX_REL_DEV
            if not keep:
                updates[fld] = entry + sign * exp_off
    # Ladder state derived from a bad entry/stop is garbage — reset it to
    # the (possibly new) plan stop in both violation modes.
    updates["dynamic_stop_level"] = updates.get("spx_target_stop", stop)
    updates["be_ladder_max_step_R"] = 0.0
    await SignalIterRepository.update_live_order(p.id, **updates)
    for k, v in updates.items():
        setattr(p, k, v)
    memo[p.id] = {
        k: v for k, v in updates.items()
        if k not in ("dynamic_stop_level", "be_ladder_max_step_R")
    }
    await SignalIterRepository.write_audit_log(
        category="plan_reanchored", actor="system",
        summary=(
            f"PLAN RE-ANCHORED for parent {p.id} {p.symbol} {p.strike}{p.right}: "
            f"stop {before['spx_target_stop']} -> {p.spx_target_stop}, "
            f"T1 {before['spx_target_t1']} -> {p.spx_target_t1}, "
            f"T2 {before['spx_target_t2']} -> {p.spx_target_t2}, "
            f"T3 {before['spx_target_t3']} -> {p.spx_target_t3} "
            f"(entry ref {entry:.2f}{' repaired' if entry_repaired else ''}, "
            f"expected R {expected_stop:.2f}, seen {stop_off:.2f})"
        ),
        details={
            "parent_id": p.id, "before": before,
            "after": {k: getattr(p, k) for k in before},
            "entry_repaired": entry_repaired, "scale": scale,
            "expected_stop_pts": expected_stop, "seen_stop_offset": stop_off,
            "spot_ref": float(sym_spot), "spot_src": spot_src,
        },
    )
    return True


# --- Per-symbol spot resolution + units guard (2026-07-07) -------------------
_WATCHER_SPOT_AUDIT_INTERVAL_MS = 300_000  # no-spot / units-mismatch: 1 row / 5 min
_WATCHER_UNITS_RATIO = 3.0

# --- Stock-quote spot fallback (2026-07-17, SPY mirror build) ----------------
# A SPY mirror parent exists WITHOUT the SPY scanner being enabled (the
# mirror is placed off the SPX signal; scanner symbol enablement is
# untouched by design). With the scanner SPX-only pinned, _shadow_cash_spot
# has NO source for SPY: active tick is SPX, there is no SPY scanner
# snapshot, and no SPY 1-min bars are being captured — the parent would sit
# unevaluated (watcher_no_spot every 5 min) until the EOD flat. Fallback:
# a throttled live IBKR STOCK quote for the parent's own symbol (STOCK-kind
# symbols only — SPY/QQQ/IWM/GLD/SMH/TLT). TTL-cached on state so the 0.1s
# poll costs at most one bounded round-trip per symbol per TTL.
_STOCK_SPOT_TTL_MS = int(float(os.environ.get("FEF_WATCHER_STOCK_SPOT_TTL_SEC", "5")) * 1000)
# Serve a previously-fetched quote up to this old while a refresh is in
# flight; past it the value is too stale to gate a stop on.
_STOCK_SPOT_SERVE_MAX_MS = 30_000
_STOCK_SPOT_TIMEOUT_SEC = 4.0


def _stock_spot_state(state) -> tuple[dict, set, set] | None:
    """(cache, refreshing, tasks) holders on state; None if unassignable."""
    try:
        if not hasattr(state, "_watcher_stock_spot_cache"):
            state._watcher_stock_spot_cache = {}  # type: ignore[attr-defined]
        if not hasattr(state, "_watcher_stock_spot_refreshing"):
            state._watcher_stock_spot_refreshing = set()  # type: ignore[attr-defined]
        if not hasattr(state, "_watcher_stock_spot_tasks"):
            state._watcher_stock_spot_tasks = set()  # type: ignore[attr-defined]
    except Exception:
        return None
    return (
        state._watcher_stock_spot_cache,        # type: ignore[attr-defined]
        state._watcher_stock_spot_refreshing,   # type: ignore[attr-defined]
        state._watcher_stock_spot_tasks,        # type: ignore[attr-defined]
    )


async def _stock_spot_refresh(state, symbol: str, contract) -> None:
    """Background refresh of one symbol's stock-quote spot. BID/ASK MID ONLY
    — the provider's `last` can silently fall back to the PRIOR SESSION
    CLOSE when live fields are NaN (adversarial review Finding 3); a stop
    must never be evaluated against yesterday's close. Failures are cached
    (as None) so a dead feed is retried at TTL cadence, not poll cadence."""
    spot: float | None = None
    try:
        try:
            import math as _m
            quote = await asyncio.wait_for(
                state.ibkr.get_last_quote(contract), timeout=_STOCK_SPOT_TIMEOUT_SEC,
            )
            bid = getattr(quote, "bid", None)
            ask = getattr(quote, "ask", None)
            if (isinstance(bid, (int, float)) and isinstance(ask, (int, float))
                    and _m.isfinite(bid) and _m.isfinite(ask) and bid > 0 and ask > 0):
                spot = (float(bid) + float(ask)) / 2.0
        except Exception:
            logger.debug("watcher: stock-quote spot refresh failed for %s", symbol)
            spot = None
    finally:
        # CancelledError bypasses `except Exception` — the latch release must
        # be unconditional or the symbol can never refresh again (review F-A).
        holders = _stock_spot_state(state)
        if holders is not None:
            cache, refreshing, _tasks = holders
            cache[symbol] = (int(_time.time() * 1000), spot)
            refreshing.discard(symbol)


async def _watcher_stock_quote_spot(state, symbol: str) -> tuple[float | None, str | None]:
    """(spot, 'ibkr_stock_quote') for a STOCK-kind symbol, NON-BLOCKING
    (adversarial review Finding 2: the provider's snapshot quote holds an
    unconditional ~1.5s sleep — awaiting it inline stalled the ENTIRE 0.1s
    poll loop ~30-40% duty-cycle while a mirror was open). Always serves
    from the TTL cache; a stale/missing entry kicks a DETACHED refresh task
    and this call returns what it has (None on the first cycles — the
    parent is simply skipped until the refresh lands, ~1.5-2s)."""
    try:
        from fef_sidecar.ibkr.models import Contract, ContractKind
        contract = Contract.for_symbol(symbol)
        if contract.kind != ContractKind.STOCK:
            return None, None
    except Exception:
        return None, None
    holders = _stock_spot_state(state)
    if holders is None:
        return None, None
    cache, refreshing, tasks = holders
    now_ms = int(_time.time() * 1000)
    entry = cache.get(symbol)
    if (entry is None or now_ms - entry[0] >= _STOCK_SPOT_TTL_MS) \
            and symbol not in refreshing:
        refreshing.add(symbol)
        try:
            task = asyncio.create_task(_stock_spot_refresh(state, symbol, contract))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        except Exception:
            refreshing.discard(symbol)
    if entry is None:
        return None, None
    spot = entry[1]
    if spot and now_ms - entry[0] < _STOCK_SPOT_SERVE_MAX_MS:
        return float(spot), "ibkr_stock_quote"
    return None, None


def _throttled(state, attr: str, key, now_ms: int,
               interval_ms: int = _WATCHER_SPOT_AUDIT_INTERVAL_MS) -> bool:
    """True iff the (attr, key) audit slot is open; claims it when open."""
    if not hasattr(state, attr):
        setattr(state, attr, {})
    slots = getattr(state, attr)
    if now_ms - slots.get(key, 0) < interval_ms:
        return False
    slots[key] = now_ms
    return True


async def _watcher_symbol_spot(
    state, p, poll_spot: float, spot_cache: dict,
) -> tuple[float | None, str | None]:
    """Resolve parent p's OWN symbol spot for spx_points stop/tier evaluation
    (2026-07-07 incident #2: the SPY 749P plan stop 751.16 was compared
    against the ACTIVE-CHART SPX tick 7533 -> dist=-6781pt -> instant stop).
    Reuses _shadow_cash_spot resolution: active tick only when active_symbol
    == p.symbol, else scanner per-symbol snapshot <=180s, else latest 1-min
    bar <=10min. Returns (spot, provenance); no symbol-correct spot ->
    (None, None) and evaluation is SKIPPED for this parent
    (category="watcher_no_spot", once per 5 min per parent)."""
    from fef_sidecar.db.repository import SignalIterRepository
    spot, src = await _shadow_cash_spot(state, p.symbol, poll_spot, spot_cache)
    if spot is not None and spot > 0 and src != "1min_bar":
        return float(spot), src
    # STOCK-QUOTE FALLBACK (2026-07-17): a mirror-placed SPY parent has no
    # scanner snapshot and no bar capture — resolve its spot from a
    # TTL-cached live IBKR stock quote (non-blocking; a stale cache kicks a
    # detached refresh). Also OUTRANKS a 1-min-bar spot for STOCK symbols
    # (review Finding 10: the bar close can be up to 10 min old — never
    # prefer it over a <=5s quote when one is available).
    q_spot, q_src = await _watcher_stock_quote_spot(state, p.symbol)
    if q_spot is not None and q_spot > 0:
        spot_cache[p.symbol] = (float(q_spot), q_src)  # per-cycle reuse
        return float(q_spot), q_src
    if spot is not None and spot > 0:
        return float(spot), src  # the bar spot — better than skipping
    now_ms = int(_time.time() * 1000)
    if _throttled(state, "_watcher_no_spot_log_ms", p.id, now_ms):
        try:
            await SignalIterRepository.write_audit_log(
                category="watcher_no_spot", actor="system",
                summary=(
                    f"NO SYMBOL-CORRECT SPOT for parent {p.id} {p.symbol} "
                    f"{p.strike}{p.right} (active={getattr(state, 'active_symbol', '?')}) "
                    f"— stop/tier evaluation SKIPPED until a {p.symbol} spot resolves"
                ),
            )
        except Exception:
            pass
    return None, None


async def _units_guard_ok(state, p, spot: float) -> bool:
    """Cross-unit comparison guard: never evaluate a stop whose price scale
    disagrees with the spot by more than 3x in either direction (an SPX-scale
    number against an ETF-scale one is ~10x). Audits
    category="watcher_units_mismatch" once per 5 min per parent."""
    from fef_sidecar.db.repository import SignalIterRepository
    stop_ref = (
        p.dynamic_stop_level if p.dynamic_stop_level is not None
        else p.spx_target_stop
    )
    if stop_ref is None or stop_ref <= 0 or spot <= 0:
        return True  # nothing comparable — no stop can fire anyway
    ratio = spot / float(stop_ref)
    if 1.0 / _WATCHER_UNITS_RATIO <= ratio <= _WATCHER_UNITS_RATIO:
        return True
    now_ms = int(_time.time() * 1000)
    if _throttled(state, "_watcher_units_log_ms", p.id, now_ms):
        try:
            await SignalIterRepository.write_audit_log(
                category="watcher_units_mismatch", actor="system",
                summary=(
                    f"UNITS MISMATCH for parent {p.id} {p.symbol} {p.strike}{p.right}: "
                    f"spot={spot:.2f} vs stop={float(stop_ref):.2f} (ratio {ratio:.2f}) "
                    f"— refusing to evaluate a cross-unit comparison"
                ),
            )
        except Exception:
            pass
    return False


async def _watcher_safe_sell(state, p, qty: int, tier_label: str, ref_desc: str,
                             outcome: dict | None = None) -> bool:
    """Safe market-sell-to-close `qty` of parent p's contract — the single shared
    exit path for BOTH spx_points and fixed_dollar watchers. Mirrors the long-qty
    guard + contract-level pending reservation + child-row write. `ref_desc` is a
    human string for the audit (e.g. 'SPX 6712.50' or 'prem 5.40 / +$520').
    Returns True iff an order was actually placed.

    When `outcome` is provided, outcome["kind"] records how the attempt
    resolved so callers can decide whether an optimistic latch must roll back:
      "placed"                 — order placed.
      "dry"                    — non-live provider ([DRY] row written).
      "refused_guard"          — anti-short reservation or safe-sell
                                 no_position/short_blocked; clears once the
                                 entry fill propagates -> RETRYABLE.
      "refused_cap"            — lifetime sell cap with the broker flat /
                                 pending-covered (genuinely sold out) -> NOT
                                 retryable. Also returned when the broker is
                                 flat AND the cap shows fully sold (the
                                 refusal-retry loop's terminal signal).
      "refused_cap_divergence" — STOP only: cap says fully sold but the
                                 broker still shows the position -> RETRYABLE
                                 + loud watcher_cap_divergence escalation.
      "error"                  — exception, no order placed -> RETRYABLE.
      "noop"                   — qty <= 0.
    """
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.ibkr.live import LiveIBKRProvider

    def _resolve(kind: str) -> None:
        if outcome is not None:
            outcome["kind"] = kind

    _resolve("error")  # pessimistic default — overwritten on a decisive path
    if qty <= 0:
        _resolve("noop")
        return False
    if not isinstance(state.ibkr, LiveIBKRProvider):
        await SignalIterRepository.write_audit_log(
            category="spx_watcher", actor="system",
            summary=f"[DRY] {tier_label} crossed: would market-sell-to-close {qty}× {p.symbol} {p.strike}{p.right} at {ref_desc}",
        )
        _resolve("dry")
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
            sold, cap = _lifetime_cap_state(state, p)
            if long_qty <= 0 and cap > 0 and sold >= cap:
                # Broker flat AND the watcher already sold the full fill —
                # the exit is complete; a rolled-back retry loop lands here
                # once positions catch up, and the terminal latch is TRUE.
                await SignalIterRepository.write_audit_log(
                    category="spx_watcher", actor="system",
                    summary=(
                        f"{tier_label} RESOLVED: broker flat and watcher already "
                        f"sold {sold}/{cap} for parent {p.id} "
                        f"({p.symbol} {p.strike}{p.right}) — exit complete"
                    ),
                )
                _resolve("refused_cap")
                return False
            await SignalIterRepository.write_audit_log(
                category="spx_watcher", actor="system",
                summary=(
                    f"{tier_label} SKIPPED: 0 available after reservation "
                    f"(long={long_qty}, pending={pending}, requested={qty}) "
                    f"for {p.symbol} {p.strike}{p.right} — refusing to short"
                ),
            )
            _resolve("refused_guard")
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
        reserved = _lifetime_cap_reserve(state, p, sell_qty)
        if reserved <= 0:
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
            if tier_label == "STOP" and available >= qty:
                # CAP-vs-BROKER DIVERGENCE (2026-07-07 review BLOCKER-2): the
                # cap claims fully sold, yet the broker still shows the whole
                # requested qty available (long minus pending). A leaked
                # reservation would strand OPEN contracts behind a terminal
                # latch — escalate loudly and keep the parent managed; the
                # refusal streak bounds the retries (park -> reconcile).
                await SignalIterRepository.write_audit_log(
                    category="watcher_cap_divergence", actor="system",
                    summary=(
                        f"LIFETIME-CAP DIVERGENCE on {tier_label}: cap shows "
                        f"sold {sold}/{cap} for parent {p.id} "
                        f"({p.symbol} {p.strike}{p.right}) but broker shows "
                        f"long={long_qty}, pending={pending} — keeping parent "
                        f"managed; VERIFY POSITION"
                    ),
                    details={"parent_id": p.id, "cum_sold": sold, "cap": cap,
                             "broker_long": long_qty, "pending": pending,
                             "requested": qty},
                )
                _resolve("refused_cap_divergence")
            else:
                _resolve("refused_cap")
            return False
        sell_qty = reserved
        placed_qty = 0
        try:
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
                # no_position/short_blocked = the same fill-lag class as the
                # reservation skip; placement_error = no order placed either.
                _resolve(
                    "refused_guard"
                    if result.reason in ("no_position", "short_blocked")
                    else "error"
                )
                return False
            placed_qty = int(result.sell_qty)
            _resolve("placed")  # set BEFORE the audit/child writes — an exception
            # past this point must never classify a placed order as retryable.
            _clear_refusal_streak(state, p)
            try:
                # Post-placement bookkeeping is BaseException-guarded: a
                # deadline-cancel landing here must not escape before the
                # placed result is returned (the order IS transmitted —
                # escaping would revert flags and re-sell; re-review N3).
                # Tier-fire audit with THIS fill's banked $ + running
                # cumulative + clearly-labeled whole-position mark. Spawned:
                # fill-price polling must never block the watcher hot loop.
                asyncio.create_task(
                    _write_tier_fill_audit(state, p, tier_label, result, ref_desc)
                )
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
            except BaseException:
                logger.exception(
                    "spx_watcher: post-placement bookkeeping interrupted for "
                    "parent %s (order already placed)", p.id,
                )
            return True
        finally:
            # CAP-LEAK GUARD (2026-07-07 review BLOCKER-2): whatever exits
            # this block — refusal, clamp, exception, cancellation — any
            # reserved qty not confirmed placed is released. A leaked
            # reservation turns every later sell into a cap refusal.
            if placed_qty < sell_qty:
                _lifetime_cap_release(state, p, sell_qty - placed_qty)
    except Exception:
        logger.exception("watcher market sell failed")
        return False


async def _sync_parent_fill_from_trades(state, p) -> None:
    """Continuously sync a parent's avg_fill_price + filled_qty + status from
    the live IBKR trade object (in-memory `ib.trades()`, no round-trip). FIX
    (2026-06-30): the fill-sync only ran nightly, so a market-entry parent sat
    at status=SUBMITTED / avg_fill_price=None for the whole session — DB P&L
    was wrong AND fixed_dollar's $ exits had no real entry price to mark against.

    UNDERSELL FIX (2026-07-20): the old guard treated PARTIAL as a *terminal*
    sync state and stopped re-reading orderStatus.filled the instant it saw ANY
    fill. A market entry that fills in pieces (e.g. a SPY 10-lot filling 9 then
    1 across two exec reports) gets caught mid-fill: filled_qty froze at 9 while
    IBKR went on to fill all 10. The lifetime sell cap is `filled_qty or qty`,
    so the exit sold only 9 and orphaned 1 naked contract (live: parent 790,
    SPY 741P). filled_qty is now kept reconciled to the AUTHORITATIVE
    orderStatus.filled until the order is genuinely done — fully filled OR the
    broker order reaches a terminal status — so by the time any exit treats the
    parent as closed, the cap reflects the TRUE held quantity. Invariants:
    filled_qty is MONOTONIC (a transient low read never lowers it) and no
    lifecycle status the exit path already advanced (STOPPED/CLOSED/FILLED) is
    ever downgraded.

    Same-session fills only (cross-restart fills are handled by reconcile +
    the nightly ibkr_execution backfill).
    """
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    if not isinstance(state.ibkr, LiveIBKRProvider) or p.ib_order_id is None:
        return
    # One-shot "done" latch: only stop re-reading once the fill can no longer
    # change. PARTIAL alone is deliberately NOT a stopping condition — that was
    # the undersell bug. The latch is set below on fully-filled or broker-
    # terminal, and cleared by _reset_watcher_runtime_state on re-adoption.
    if not hasattr(state, "_watcher_fill_synced"):
        state._watcher_fill_synced = set()  # type: ignore[attr-defined]
    if p.id in state._watcher_fill_synced:  # type: ignore[attr-defined]
        return
    # A lifecycle-terminal parent is already being/has been wound down by the
    # exit path — never touch its fill record (and stop scanning it).
    if p.status in ("STOPPED", "CLOSED", "CLOSED_EXTERNAL", "STUCK", "CANCELLED"):
        state._watcher_fill_synced.add(p.id)  # type: ignore[attr-defined]
        return
    # Fast path: already fully recorded (avg present AND the whole order filled).
    # PARTIAL is intentionally excluded here so a mid-fill snapshot keeps syncing.
    if (p.avg_fill_price and int(p.qty or 0) > 0
            and int(getattr(p, "filled_qty", 0) or 0) >= int(p.qty or 0)):
        state._watcher_fill_synced.add(p.id)  # type: ignore[attr-defined]
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
        status_str = str(getattr(st, "status", "") or "")
        avg = float(getattr(st, "avgFillPrice", 0) or 0)
        filled = int(float(getattr(st, "filled", 0) or 0))
        if avg <= 0 or filled <= 0:
            return
        qty = int(p.qty or 0)
        # MONOTONIC: a transient low/zero read can never decrease the count.
        cur_filled = int(getattr(p, "filled_qty", 0) or 0)
        new_filled = max(cur_filled, filled)
        fully_filled = qty > 0 and new_filled >= qty
        order_terminal = status_str in ("Filled", "Cancelled", "ApiCancelled", "Inactive")
        # Status without ever DOWNGRADING a state the exit path advanced: only
        # SUBMITTED/None gets a fresh stamp; a PARTIAL is promoted to FILLED
        # once the order completes; everything else is left untouched.
        new_status: str | None = None
        if p.status in ("SUBMITTED", None):
            new_status = "FILLED" if fully_filled else "PARTIAL"
        elif p.status == "PARTIAL" and fully_filled:
            new_status = "FILLED"
        # Only write when something actually changed — a stuck/cancelled partial
        # (9/10, remainder cancelled) is re-scanned cheaply from the in-memory
        # trade but never re-written, and is latched off below via order_terminal.
        updates: dict = {}
        if new_filled != cur_filled:
            updates["filled_qty"] = new_filled
        if round(avg, 4) != round(float(p.avg_fill_price or 0), 4):
            updates["avg_fill_price"] = round(avg, 4)
        if new_status is not None and new_status != p.status:
            updates["status"] = new_status
        if updates:
            await SignalIterRepository.update_live_order(p.id, **updates)
            if "filled_qty" in updates:
                p.filled_qty = updates["filled_qty"]
            if "avg_fill_price" in updates:
                p.avg_fill_price = updates["avg_fill_price"]
            if "status" in updates:
                p.status = updates["status"]
        # Latch OFF further syncing only when the order can no longer change.
        if fully_filled or order_terminal:
            state._watcher_fill_synced.add(p.id)  # type: ignore[attr-defined]
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
        # BOUNDED (2026-07-23 exit-wedge hardening) — see _check_dollar_loss.
        opt_quote = await asyncio.wait_for(
            state.ibkr.get_option_quote(Contract(
                symbol=p.symbol, kind=ContractKind.OPTION,
                exchange="CBOE" if p.symbol == "SPX" else "SMART",
                expiry=p.expiry, strike=p.strike, right=p.right,
            )),
            timeout=6.0,
        )
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

            async def _fire_fd_runner() -> None:
                # OPTIMISTIC LATCH (2026-07-06 oversell hotfix): resolve the
                # parent BEFORE the awaited sell so a re-entrant/stale check
                # can never re-fire into the placement window. REFUSAL
                # ROLLBACK (2026-07-07): the terminal status only survives a
                # sell that actually placed — a guard-refused/errored attempt
                # reverts it (finally-gated so cancellation can't skip it)
                # and the parent stays managed. Runs shielded.
                if p.id in state.firing_parent_ids:
                    return
                state.firing_parent_ids.add(p.id)
                prior_status = p.status
                outcome: dict = {}
                placed = False
                try:
                    p.status = "CLOSED"
                    await SignalIterRepository.update_live_order(p.id, status="CLOSED")
                    placed = await _watcher_safe_sell(
                        state, p, remaining_qty, "RUNNER_PROTECT", ref, outcome=outcome,
                    )
                finally:
                    try:
                        if not placed and outcome.get("kind", "error") in _RETRYABLE_SELL_OUTCOMES:
                            await _rollback_refused_terminal_latch(
                                state, p, latched_status="CLOSED",
                                prior_status=prior_status,
                                tier_label="RUNNER_PROTECT",
                                outcome_kind=outcome.get("kind", "error"),
                                ref_desc=ref,
                            )
                    finally:
                        state.firing_parent_ids.discard(p.id)

            await _run_exit_fire_shielded(state, _fire_fd_runner)
            return

        if stop_qty > 0:

            async def _fire_fd_stop() -> None:
                # OPTIMISTIC LATCH + REFUSAL ROLLBACK — see RUNNER_PROTECT above.
                if p.id in state.firing_parent_ids:
                    return
                state.firing_parent_ids.add(p.id)
                prior_status = p.status
                outcome: dict = {}
                placed = False
                try:
                    p.status = "STOPPED"
                    await SignalIterRepository.update_live_order(p.id, status="STOPPED")
                    placed = await _watcher_safe_sell(
                        state, p, stop_qty, "STOP", ref, outcome=outcome,
                    )
                finally:
                    try:
                        if not placed and outcome.get("kind", "error") in _RETRYABLE_SELL_OUTCOMES:
                            await _rollback_refused_terminal_latch(
                                state, p, latched_status="STOPPED",
                                prior_status=prior_status, tier_label="STOP",
                                outcome_kind=outcome.get("kind", "error"),
                                ref_desc=ref,
                            )
                    finally:
                        state.firing_parent_ids.discard(p.id)

            await _run_exit_fire_shielded(state, _fire_fd_stop)
            return

        flag_field = {"T1": "spx_target_t1_fired", "T2": "spx_target_t2_fired",
                      "T3": "spx_target_t3_fired"}
        any_fired = False
        for label, qty in fires:
            field = flag_field[label]
            if getattr(p, field):
                continue  # already latched by another path — never re-fire

            async def _fire_fd_tier(label=label, field=field, qty=qty) -> bool:
                # OPTIMISTIC LATCH (2026-07-06 oversell hotfix): mark the tier
                # fired BEFORE the awaited sell so a re-entrant check can never
                # re-fire the same rung mid-placement (the QQQ 724C fourth
                # sell). Reverted on any not-placed outcome (finally-gated so
                # cancellation can't skip it) — preserves the 7520C guarantee
                # that a refused sell never phantom-banks a tier. Shielded +
                # per-parent in-flight guard (2026-07-07 review MAJOR-6).
                if p.id in state.firing_parent_ids:
                    return False
                state.firing_parent_ids.add(p.id)
                outcome: dict = {}
                placed = False
                try:
                    setattr(p, field, True)
                    await SignalIterRepository.update_live_order(p.id, **{field: True})
                    placed = await _watcher_safe_sell(
                        state, p, qty, label, ref, outcome=outcome,
                    )
                    if placed:
                        await SignalIterRepository.update_live_order(p.id, status="PARTIAL")
                finally:
                    try:
                        if not placed and outcome.get("kind") != "placed":
                            setattr(p, field, False)
                            try:
                                await SignalIterRepository.update_live_order(
                                    p.id, **{field: False},
                                )
                            except Exception:
                                logger.exception(
                                    "fd tier flag revert write failed for parent %s", p.id,
                                )
                            kind = outcome.get("kind", "error")
                            if kind in _RETRYABLE_SELL_OUTCOMES:
                                await _audit_tier_refusal_retry(state, p, label, kind, ref)
                            elif kind == "refused_cap":
                                # Cap-refused tier re-fires would otherwise
                                # flood at check cadence (review BLOCKER-2):
                                # back off + count toward the park budget.
                                _set_refusal_backoff(state, p)
                                if _note_refusal_over_limit(state, p):
                                    await _park_stuck_parent(state, p, label, kind, ref)
                    finally:
                        state.firing_parent_ids.discard(p.id)
                return placed

            if await _run_exit_fire_shielded(state, _fire_fd_tier):
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
#   real == 'spx_points' on a LO-VIX day -> ALSO the LO-VIX SHRINK ladder
#       (variant_label='lowvix_shrink_shadow', 2026-07-07 registered
#       backtest, shadow-first; SPX parents only — the adopted arm was
#       SPX-only): the parent's own stored plan with all four offsets
#       scaled by m=0.6 via the exact backtest transformation
#       (_lowvix_scaled_plan), walked with be_ladder-shadow semantics PLUS
#       the live T1-retrace runner-protect (review MAJOR-2; ~13% of the
#       adopted arm's lo-fire exits resolved via rp) — a SECOND row for
#       the same parent (see _lowvix_shadow_cycle_one). KNOWN HORIZON
#       DIVERGENCE (documented, not replicated): the adopted arm walked a
#       60-min window cap (~10% of its exits); live spx_points and this
#       shadow run uncapped to the close — the adoption number assumed a
#       horizon live doesn't have (variant_params.horizon_note).
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

# Lo-VIX ladder-shrink shadow (2026-07-07 registered backtest — scratchpad
# lowvix_score.py over the strict-replay fire set: m=0.6 on lo-VIX days,
# ADOPT on both fill conventions; the operator's standing rule = SHADOW-
# FIRST). spx_points parents on lo-VIX days (engine/lo_vix_day.py) carry
# this SECOND shadow: the same entry walked on the parent's own stored plan
# with ALL FOUR offsets scaled by m=0.6 via the exact backtest plan
# transformation (_lowvix_scaled_plan). Non-lo/unknown days: no extra row.
_LOWVIX_SHADOW_VARIANT_LABEL = "lowvix_shrink_shadow"
_LOWVIX_SHRINK_M = 0.6
_LOWVIX_EPS = 1e-6  # backtest scaled_plan's EPS — default-offset detection

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


def _resolve_shadow_entry_spx(
    state, p, spot: float | None, spot_source: str | None,
) -> tuple[float, str, float | None] | None:
    """(entry_spx, entry_spx_source, parent_entry_spx_price) for a shadow —
    the 7/6 pollution guard shared by every shadow init: the parent's
    recorded entry_spx_price is used only when it agrees with the cash
    reference within _SHADOW_ENTRY_SPX_MAX_DEV; beyond that the cash
    reference wins (active-chart pollution signature, ES-priced 7561 vs
    cash 7511), with both values persisted for audit. None => DEFER init
    (no cash reference to validate against, or no usable spot at all)."""
    cash = float(spot) if spot and spot > 0 else None
    parent_entry = float(p.entry_spx_price) if (p.entry_spx_price or 0) > 0 else None
    if parent_entry is not None and cash is not None:
        if abs(parent_entry - cash) / cash > _SHADOW_ENTRY_SPX_MAX_DEV:
            logger.warning(
                "exit shadow: parent %s entry_spx_price %.2f deviates >%.1f%% "
                "from %s cash %.2f — using cash (active-chart pollution guard)",
                p.id, parent_entry, _SHADOW_ENTRY_SPX_MAX_DEV * 100,
                p.symbol, cash,
            )
            return cash, f"cash_spot_override({spot_source})", parent_entry
        return parent_entry, "parent_entry_spx_price", parent_entry
    if parent_entry is not None:
        # No cash reference this cycle — defer rather than trust a value we
        # cannot validate (the 7/6 rows were exactly unvalidated entries).
        return None
    if cash is None:
        return None
    return cash, f"cash_spot({spot_source})", None


def _shadow_tier_qtys(p) -> list[int]:
    """The parent's own tier split when it accounts for the full qty, else
    the canonical 50/30/20 shadow split."""
    parent_tier_qtys = [
        int(getattr(p, "spx_target_t1_qty", 0) or 0),
        int(getattr(p, "spx_target_t2_qty", 0) or 0),
        int(getattr(p, "spx_target_t3_qty", 0) or 0),
    ]
    if sum(parent_tier_qtys) == int(p.qty):
        return parent_tier_qtys
    return _shadow_split_qty(int(p.qty))


def _shadow_base(
    p, variant: str, entry_spx: float, entry_spx_source: str,
    parent_entry: float | None, spot_source: str | None,
) -> dict:
    """Common shadow state-machine fields — every variant layers its own
    levels/ladder config on top (see _shadow_init / _lowvix_shadow_init)."""
    return {
        "variant": variant,
        "parent_id": p.id,
        "symbol": p.symbol, "strike": float(p.strike), "right": p.right,
        "expiry": p.expiry,
        "qty": int(p.qty),
        "is_long": p.right == "C",
        "entry_spx": float(entry_spx),
        "entry_spx_source": entry_spx_source,
        "parent_entry_spx_price": parent_entry,
        "spot_source": spot_source,
        "entry_premium": float(p.avg_fill_price or p.limit_price or 0) or None,
        "tier_qtys": _shadow_tier_qtys(p),
        "tier_fired": [False, False, False],
        "peak_mfe_R": 0.0,
        "fills": [],           # {tier, spx, qty, ts_ms, premium, premium_src}
        "premium_tasks": [],   # spawned quote captures, gathered at finalize
        "done": False,
        "exit_reason": None,
        "settled_mtm_at_real_close": False,
    }


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
    resolved = _resolve_shadow_entry_spx(state, p, spot, spot_source)
    if resolved is None:
        return None
    entry_spx, entry_spx_source, parent_entry = resolved
    is_long = p.right == "C"

    variant = (
        _FD_SHADOW_VARIANT_LABEL
        if getattr(p, "exit_strategy", None) == "spx_points"
        else _SHADOW_VARIANT_LABEL
    )
    sh = _shadow_base(p, variant, entry_spx, entry_spx_source, parent_entry,
                      spot_source)

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
            "last_spot": float(spot) if spot and spot > 0 else None,
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


def _plan_anchor_from_stop(stop: float, is_long: bool) -> float | None:
    """Recover the spot a parent's plan was anchored on from its stored stop.

    build_plan NEVER wall-anchors the stop — it is always
    spot -/+ 10*_price_scale(spot) — so the inversion is exact:
    SPX-class (scale pinned to 1.0): anchor = stop +/- 10; ETF-class:
    solve spot*(1 -/+ 10/7500) = stop. The class is decided by the
    recovered value itself; both branches are exact within their class and
    nothing trades near the 2000 boundary where they could disagree.
    Callers must cross-check the anchor against the validated entry spot —
    a hand-set or non-default stop yields a bogus anchor."""
    from fef_sidecar.engine.strikes import _SPX_REF_LEVEL
    sgn = 1.0 if is_long else -1.0
    cand = stop + sgn * 10.0
    if cand >= 2000.0:
        return cand
    denom = 1.0 - sgn * (10.0 / _SPX_REF_LEVEL)
    if denom <= 0:
        return None
    cand = stop / denom
    if 0.0 < cand < 2000.0:
        return cand
    return None


def _lowvix_scaled_plan(
    spx_sig: float, t1: float, t2: float, t3: float, stop: float,
    is_long: bool, m: float, price_scale: float = 1.0,
) -> tuple[float, float, float, float]:
    """EXACT mirror of the registered backtest's plan transformation
    (scratchpad regime_exit_score.scaled_plan, reused verbatim by
    lowvix_score.py): scale the DEFAULT offsets by m around the plan's own
    reference spot; KEEP a wall-anchored tier only while it passes
    build_plan's live sanity band re-evaluated at the scaled stop distance
    R' (T1 in [0.5R', 2R']; T2 in [2R', 3R'] and >3pts beyond the new T1 —
    the raw 3-pt separation matches both build_plan and the backtest; T3
    beyond the new T2); a wall failing its band falls back to the scaled
    default. The stop is ALWAYS the scaled default — build_plan never
    wall-anchors it. At m=1.0 this reproduces the stored plan exactly.
    price_scale generalizes the backtest's SPX defaults (15/25/50/-10 x
    _price_scale; 1.0 for SPX -> bit-identical to the backtest arm)."""
    sgn = 1.0 if is_long else -1.0
    d1, d2, d3 = 15.0 * price_scale, 25.0 * price_scale, 50.0 * price_scale
    off1 = sgn * (t1 - spx_sig)
    off2 = sgn * (t2 - spx_sig)
    off3 = sgn * (t3 - spx_sig)
    R = 10.0 * price_scale * m
    ns = spx_sig - sgn * R
    # T1
    if abs(off1 - d1) < _LOWVIX_EPS:
        n1 = spx_sig + sgn * d1 * m
    elif 0.5 * R - _LOWVIX_EPS <= off1 <= 2.0 * R + _LOWVIX_EPS:
        n1 = t1
    else:
        n1 = spx_sig + sgn * d1 * m
    # T2
    if abs(off2 - d2) < _LOWVIX_EPS:
        n2 = spx_sig + sgn * d2 * m
    elif (2.0 * R - _LOWVIX_EPS <= off2 <= 3.0 * R + _LOWVIX_EPS) and abs(t2 - n1) > 3.0:
        n2 = t2
    else:
        n2 = spx_sig + sgn * d2 * m
    # T3 (anchor = wall or NTM max pain; keep iff still beyond the new T2)
    if abs(off3 - d3) < _LOWVIX_EPS:
        n3 = spx_sig + sgn * d3 * m
    elif sgn * (t3 - n2) > 0:
        n3 = t3
    else:
        n3 = spx_sig + sgn * d3 * m
    return n1, n2, n3, ns


def _lowvix_shadow_init(
    state, p, spot: float | None, spot_source: str | None = None,
) -> dict | str | None:
    """SECOND shadow for an spx_points parent on a lo-VIX day (the caller
    has already established is_lo_vix_day(state) is True): the SAME entry
    walked on the parent's own stored plan with all four offsets scaled by
    m=_LOWVIX_SHRINK_M via the exact backtest transformation
    (_lowvix_scaled_plan). Walks with the same conventions as the
    be_ladder shadow (_shadow_step: BE ratchet, stop-before-targets,
    first-touch tiers; fills at quote prices; finalize-on-close upsert;
    unusable_* statuses never $0).

    Requires the parent's stored spx_target_* plan — the registered
    variant transforms the STORED ladder, never a synthesized one — and a
    recoverable plan anchor: build_plan's stop is always its default
    offset from the plan reference spot, so the anchor is inverted from
    the stored stop and cross-checked against the validated entry. A stop
    that was hand-set (anchor >0.5% off entry) makes the transformation
    undefined -> no lo-VIX row for this parent.

    Returns the shadow dict; None to DEFER (no validated entry spot / no
    qty synced yet — retry next cycle); or a string reason to DECLINE
    (memoized by the caller against the levels it declined, so the 0.1s
    loop logs once and a later plan repair re-admits the parent)."""
    if not p.qty:
        return None
    if getattr(p, "symbol", None) != "SPX":
        # The adopted backtest arm was SPX-only; the _price_scale
        # generalization in _lowvix_scaled_plan is UNVALIDATED — do not
        # collect ETF rows that would blend into the SPX decision dataset.
        return "non-SPX parent (backtest arm was SPX-only)"
    levels = (p.spx_target_t1, p.spx_target_t2, p.spx_target_t3,
              p.spx_target_stop)
    if any(v is None for v in levels):
        return "parent has no stored spx_target_* plan"
    resolved = _resolve_shadow_entry_spx(state, p, spot, spot_source)
    if resolved is None:
        return None
    entry_spx, entry_spx_source, parent_entry = resolved
    is_long = p.right == "C"
    t1, t2, t3, stop = (float(v) for v in levels)
    anchor = _plan_anchor_from_stop(stop, is_long)
    if anchor is None or abs(anchor - entry_spx) / entry_spx > _SHADOW_ENTRY_SPX_MAX_DEV:
        return (
            f"plan anchor unrecoverable from stop {stop:.2f} "
            f"(anchor {f'{anchor:.2f}' if anchor is not None else 'n/a'} "
            f"vs entry {entry_spx:.2f})"
        )
    from fef_sidecar.engine.strikes import _price_scale
    ps = _price_scale(anchor)
    n1, n2, n3, ns = _lowvix_scaled_plan(
        anchor, t1, t2, t3, stop, is_long, _LOWVIX_SHRINK_M, ps,
    )
    risk = abs(float(entry_spx) - ns)
    if risk <= 0:
        return f"scaled stop {ns:.2f} coincides with entry {entry_spx:.2f}"
    rp_on = bool(getattr(state, "runner_protect_enabled", True))
    sh = _shadow_base(p, _LOWVIX_SHADOW_VARIANT_LABEL, entry_spx,
                      entry_spx_source, parent_entry, spot_source)
    sh.update({
        "t1": n1, "t2": n2, "t3": n3, "stop": ns,
        "levels_source": "lowvix_scaled_plan(parent_spx_targets)",
        "dyn_stop": ns,
        "be_step": 0.0, "be_lock_R": 0.0,
        # T1-retrace runner-protect (review MAJOR-2) — the adopted arm and
        # the live spx_points executor both run it; _shadow_step honors the
        # flag only on this variant.
        "rp_protect": rp_on,
        # Provenance merged into variant_params at finalize.
        "variant_extra": {
            "m_scale": _LOWVIX_SHRINK_M,
            "plan_anchor": anchor,
            "price_scale": ps,
            "original_levels": {"t1": t1, "t2": t2, "t3": t3, "stop": stop},
            "runner_protect": rp_on,
            # Horizon divergence (review MAJOR-2, documented not "fixed"):
            # the ADOPTED arm walked a 60-min window cap; live spx_points
            # (and this shadow) run uncapped to the real close.
            "horizon_note": "backtest arm capped at 60min; live+shadow uncapped",
        },
    })
    return sh


def _log_shadow_parent_error_once(state, pid) -> None:
    """Traceback for one parent's failed shadow advance — once per parent
    per session (the cycle retries it every 0.1s poll regardless)."""
    from fef_sidecar.engine.chop_gate import session_date_et
    sess = session_date_et(int(_time.time() * 1000))
    logged = getattr(state, "_shadow_cycle_err_logged", None)
    if not isinstance(logged, dict):
        logged = {}
        try:
            state._shadow_cycle_err_logged = logged  # type: ignore[attr-defined]
        except Exception:
            pass
    if logged.get(pid) == sess:
        return
    logged[pid] = sess
    logger.exception(
        "exit shadow: cycle advance failed for parent %s "
        "(logged once per session; retried every poll)", pid,
    )


def _lowvix_declined(state) -> dict:
    """Session-scoped {parent_id: stored-levels-at-decline}. A parent whose
    plan changed since its decline is re-admitted (review MAJOR-1 mirror);
    the map resets on session roll so it cannot grow unbounded."""
    from fef_sidecar.engine.chop_gate import session_date_et
    sess = session_date_et(int(_time.time() * 1000))
    memo = getattr(state, "_lowvix_shadow_declined", None)
    if not isinstance(memo, dict) or memo.get("session") != sess:
        memo = {"session": sess, "ids": {}}
        try:
            state._lowvix_shadow_declined = memo  # type: ignore[attr-defined]
        except Exception:
            pass
    return memo["ids"]


async def _lowvix_shadow_cycle_one(
    state, p, shadows: dict | None, sym_spot: float | None,
    sym_src: str | None,
) -> None:
    """Init/advance one parent's lo-VIX shrink shadow. No-op unless the
    parent is an SPX spx_points parent on a lo-VIX day (None/unknown =>
    NOT lo — fail-open; non-SPX excluded: the adopted arm was SPX-only).
    Kept apart from the primary shadow's init/step branching so neither
    variant's deferral stalls the other. NEVER places orders.

    RE-ANCHOR RACE (review MAJOR-1): this cycle runs BEFORE the eval
    loop's _maybe_reanchor_plan in each watcher iteration, so init WAITS
    until plan sanity has vetted the parent (p.id in
    state._watcher_plan_sanity) — a 751-class corrupt plan (collapsed
    stop; its recovered anchor sits ~0.13% from entry and PASSES the 0.5%
    cross-check) would otherwise walk into the shadow one cycle before
    the repair rewrites it and finalize as a plausible status='ok' row.
    Post-init plan mutations drop the stale shadow WITHOUT a row and
    re-init from the current plan; declines are memoized against the
    levels they declined, so a later repair re-admits the parent."""
    if shadows is None or p.id is None:
        return
    if getattr(p, "exit_strategy", None) != "spx_points":
        return
    if getattr(p, "symbol", None) != "SPX":
        return  # backtest arm was SPX-only (see _lowvix_shadow_init)
    cur_levels = (p.spx_target_t1, p.spx_target_t2, p.spx_target_t3,
                  p.spx_target_stop)
    sh = shadows.get(p.id)
    if sh is not None:
        orig = (sh.get("variant_extra") or {}).get("original_levels") or {}
        if cur_levels != (orig.get("t1"), orig.get("t2"),
                          orig.get("t3"), orig.get("stop")):
            # Stored plan mutated after init (re-anchor repair or any
            # future rewrite): the walked levels were scaled from a
            # superseded plan — drop WITHOUT finalizing (a plausible
            # corrupt row is worse than no row) and re-init below.
            shadows.pop(p.id, None)
            logger.warning(
                "lowvix shadow: parent %s stored plan changed after init — "
                "dropped the stale shadow (no row), re-initializing", p.id,
            )
            sh = None
        else:
            # Keep entry premium current once the real fill syncs in.
            if not sh.get("entry_premium") and (p.avg_fill_price or 0) > 0:
                sh["entry_premium"] = float(p.avg_fill_price)
            if not sh["done"] and sym_spot:
                _shadow_step(state, sh, sym_spot)
            return
    declined = _lowvix_declined(state)
    if p.id in declined:
        if declined[p.id] == cur_levels:
            return
        declined.pop(p.id, None)  # plan changed since the decline — re-admit
        logger.info(
            "lowvix shadow: parent %s stored plan changed since decline — "
            "re-admitting", p.id,
        )
    sanity = getattr(state, "_watcher_plan_sanity", None)
    if not isinstance(sanity, dict) or p.id not in sanity:
        return  # plan sanity hasn't vetted this parent yet — wait
    from fef_sidecar.engine.lo_vix_day import is_lo_vix_day
    if await is_lo_vix_day(state) is not True:  # session-cached
        return
    made = _lowvix_shadow_init(state, p, sym_spot, spot_source=sym_src)
    if isinstance(made, dict):
        shadows[p.id] = made
    elif isinstance(made, str):
        # Decline memoized against these levels — logs ONCE, re-admitted
        # if the plan is later repaired/rewritten.
        declined[p.id] = cur_levels
        logger.warning(
            "lowvix shadow: parent %s gets no lo-VIX shrink row — %s",
            p.id, made,
        )


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

    # RUNNER-PROTECT (lo-VIX shrink variant only — review MAJOR-2), aligned
    # 2026-07-08 with the FIXED live semantics so the A/B stays apples-to-
    # apples: anchor at the T1 PLAN level (sh["t1"]) — never the T1 fill
    # spot, whose fill-print equality was the live "sell all at T1" defect —
    # and trigger only on a genuine retrace THROUGH it with the shared
    # buffer (_rp_buffer_pts). Evaluated after the stop but BEFORE this
    # sample's tier fires (the backtest walker's ordering), so the T1 fill
    # sample itself can never insta-trigger. Gated on the flag only
    # _lowvix_shadow_init sets — be_ladder_shadow has no rp to align
    # (shipped no-rp semantics preserved).
    if sh.get("rp_protect") and sh["tier_fired"][0]:
        t1_level = sh.get("t1")
        remaining = _shadow_remaining(sh)
        if t1_level is not None and remaining > 0:
            buf = _rp_buffer_pts(t1_level)
            retraced = (
                (spot <= t1_level - buf) if is_long
                else (spot >= t1_level + buf)
            )
            if retraced:
                _shadow_record_fill(state, sh, "RUNNER_PROTECT", remaining, spot)
                sh["tier_fired"] = [True, True, True]
                sh["done"] = True
                sh["exit_reason"] = "runner_protect"
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
            if i == 0:
                # Provenance only (2026-07-08): rp anchors at the T1 PLAN
                # level now, never this fill spot.
                sh["t1_at_spx"] = float(spot)
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
        # Variant-specific provenance stamped at init (lo-VIX shrink: m,
        # recovered plan anchor, price scale, the unscaled stored plan).
        variant_params.update(sh.get("variant_extra") or {})

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
        label = (
            "fixed-$ ladder" if is_fd
            else "lo-VIX m=0.6 ladder" if variant == _LOWVIX_SHADOW_VARIANT_LABEL
            else "BE-ladder"
        )
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
    """One watcher-poll advance of every parent's exit shadow (all variants;
    the name is historical — state.shadow_be_ladder keys the primary
    shadows, state.shadow_lowvix the lo-VIX shrink ones, so an spx_points
    parent on a lo-VIX day carries TWO (parent, variant) rows — the DB
    upsert key ux_shadow_exit_parent_variant supports that). Called from
    spx_target_watcher_loop with the parents + polled spot it already has;
    per-symbol CASH spots are resolved via _shadow_cash_spot (cached per
    cycle) so non-SPX parents step on their own price and an off-SPX active
    chart can't pollute entries. No order placement anywhere below."""
    shadows = getattr(state, "shadow_be_ladder", None)
    if shadows is None:
        shadows = {}
        try:
            state.shadow_be_ladder = shadows
        except Exception:
            return
    lowvix_shadows = getattr(state, "shadow_lowvix", None)
    if lowvix_shadows is None:
        lowvix_shadows = {}
        try:
            state.shadow_lowvix = lowvix_shadows
        except Exception:
            lowvix_shadows = None  # attr-frozen state: no lo-VIX shadows
    spot_cache: dict[str, tuple[float | None, str | None]] = {}
    live_ids = set()
    for p in parents:
        if p.id is None:
            continue
        live_ids.add(p.id)
        # PER-PARENT GUARD (review minor): one raising parent must not
        # stall every other parent's A/B this cycle. Logged once per
        # parent per session (retried every cycle regardless).
        try:
            sh = shadows.get(p.id)
            sym_spot, sym_src = await _shadow_cash_spot(state, p.symbol, spot, spot_cache)
            # LO-VIX SHRINK SHADOW (2026-07-07 registered backtest, shadow-
            # first): SPX spx_points parents on lo-VIX days get a SECOND
            # shadow — same plan, all four offsets scaled by m=0.6.
            await _lowvix_shadow_cycle_one(state, p, lowvix_shadows, sym_spot, sym_src)
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
        except Exception:
            _log_shadow_parent_error_once(state, p.id)
    # Real position closed (parent left the active set) -> finalize + emit.
    for pid in [pid for pid in list(shadows) if pid not in live_ids]:
        sh = shadows.pop(pid)
        f_spot, _f_src = await _shadow_cash_spot(state, sh["symbol"], spot, spot_cache)
        await _shadow_finalize(state, sh, f_spot or 0.0)
    if lowvix_shadows:
        for pid in [pid for pid in list(lowvix_shadows) if pid not in live_ids]:
            lsh = lowvix_shadows.pop(pid)
            f_spot, _f_src = await _shadow_cash_spot(state, lsh["symbol"], spot, spot_cache)
            await _shadow_finalize(state, lsh, f_spot or 0.0)


# Default BE ladder. Configurable per-deployment via env if needed.
# Each tuple: (trigger_R, new_stop_at_R_from_entry)
# 0.5R reached -> stop to entry (BE)
# 1.0R reached -> stop to +0.5R (locks half)
# 1.5R reached -> stop to +1.0R (locks 1R)
_BE_LADDER_STEPS = [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)]

# Tick-age threshold for stale-data warnings (audit-logged). If the cached
# tick hasn't refreshed in this long, the IBKR tick stream is likely stalled
# and stop detection may be acting on stale data.
_STALE_TICK_WARN_MS = int(os.environ.get("SPX_WATCHER_STALE_TICK_MS", "1500"))
# Per-parent throttle so the "spot near stop" diagnostic doesn't flood the
# audit_log when iterating at 0.1s.
_NEAR_STOP_LOG_INTERVAL_MS = 5000


async def _eval_spx_points_parent(
    state, p, spot: float, last_near_stop_log_ms: dict | None = None,
    spot_src: str | None = "active_tick",
) -> None:
    """One poll-cycle evaluation of a single spx_points parent against ITS
    OWN symbol spot (extracted from spx_target_watcher_loop 2026-07-07 so the
    stop/tier/rollback state machine is unit-testable). `spot` must already
    be symbol-correct (_watcher_symbol_spot); `spot_src` is its provenance
    (gates the cross-unit entry repair — see _maybe_reanchor_plan). Guard
    order: units guard -> arm-time plan sanity -> refusal backoff ->
    entry-fill grace -> BE ladder -> stop -> runner protect -> T1/T2/T3 ->
    CLOSED sync (RP before the tier fires = the backtest walker's ordering,
    so the cycle that fills T1 can never also fire RP). Every latch->sell
    block runs shielded
    (_run_exit_fire_shielded) so task cancellation can never strand an
    optimistic latch."""
    from fef_sidecar.db.repository import SignalIterRepository

    if last_near_stop_log_ms is None:
        last_near_stop_log_ms = {}

    # UNITS GUARD (2026-07-07): never compare a stop against a spot in a
    # different price scale. Runs BEFORE re-anchor — the plan stop was built
    # from the scanner's per-symbol spot, so spot-vs-stop agreement is what
    # vouches for `spot` before it may repair a polluted entry ref.
    if not await _units_guard_ok(state, p, spot):
        return

    # ARM-TIME PLAN SANITY (2026-07-07): two-price-sources kill switch —
    # re-anchors collapsed/wrong-side levels as offsets from entry_spx_price
    # (parent 751: R collapsed 10 -> 0.38 and the BE ladder insta-stepped).
    # False = a cross-unit entry needs repairing but the spot provenance is
    # too weak to anchor on this cycle.
    if not await _maybe_reanchor_plan(state, p, spot, spot_src):
        return

    # Retryable-refusal backoff: a just-refused sell retries on a 2s clock,
    # not per 0.1s tick (bounds audit volume while positions() catches up).
    if _refusal_backoff_active(state, p):
        return

    # ENTRY-FILL GRACE (2026-07-07): don't consume stop/tier triggers before
    # the entry fill is confirmed (fail-open after the grace window).
    if await _entry_fill_grace_pending(state, p):
        return

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
            for trig_R, new_stop_R in _BE_LADDER_STEPS:
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

    async def _market_sell(qty: int, tier_label: str, outcome: dict | None = None) -> bool:
        # spx_points exits delegate to the shared safe-sell helper
        # (the same path fixed_dollar uses). ref = the symbol's spot at fire.
        # Returns True iff an order actually PLACED — callers MUST gate
        # their tier fired-flag on this, else a refused sell phantom-
        # banks the tier and can orphan an open runner (the 7520C bug).
        return await _watcher_safe_sell(
            state, p, qty, tier_label, f"{p.symbol} {spot:.2f}", outcome=outcome,
        )

    # Stop check uses the dynamic level (BE ladder ratchets it).
    # If the tick-driven fast path is already firing this parent's
    # stop, skip — avoids double-fire.
    if p.id in state.firing_parent_ids:
        return
    effective_stop = p.dynamic_stop_level if p.dynamic_stop_level is not None else p.spx_target_stop
    if effective_stop is not None:
        # Distance to stop (signed by direction — positive = safe,
        # negative = breached). Used for the near-stop diagnostic
        # below + for stale-tick warnings if a fire ever lags.
        dist_to_stop = (
            (spot - effective_stop) if is_long_calls
            else (effective_stop - spot)
        )
        # Diagnostic: if the spot is within 2pt of the stop, audit-log
        # once every 5s per parent with the live-tick freshness so
        # we can prove watcher latency from the log timeline. Only
        # written when actually close to firing — won't spam.
        if dist_to_stop <= 2.0 and p.id is not None:
            try:
                last_log = last_near_stop_log_ms.get(p.id, 0)
                now_ms = int(asyncio.get_running_loop().time() * 1000)
                if now_ms - last_log >= _NEAR_STOP_LOG_INTERVAL_MS:
                    last_near_stop_log_ms[p.id] = now_ms
                    tick_age_ms = (
                        int(_time.time() * 1000) - state.last_live_tick_ms
                        if state.last_live_tick_ms > 0 else -1
                    )
                    stale = " STALE" if tick_age_ms > _STALE_TICK_WARN_MS else ""
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

                async def _fire_stop() -> None:
                    # OPTIMISTIC LATCH (2026-07-06): status=STOPPED
                    # BEFORE the awaited sell so no other path can
                    # re-fire this parent mid-placement.
                    # IMPORTANT: only set status=STOPPED. Do NOT
                    # overwrite spx_target_t{1,2,3}_fired to True
                    # as cleanup — those flags record whether each
                    # tier was actually banked as a partial during
                    # the trade. Polluting them on stop misleads
                    # every downstream consumer (research_dump,
                    # ablation, _estimate_parent_r, BE-kill stats).
                    # status=STOPPED is the sole resolved-marker.
                    # REFUSAL ROLLBACK (2026-07-07): the terminal
                    # status only survives an actually-placed order —
                    # a guard-refused/errored sell reverts it so the
                    # parent stays managed (the eviction hole). The
                    # rollback is finally-gated + the fire is shielded
                    # so cancellation can never skip it (review B-1).
                    if p.id in state.firing_parent_ids:
                        return
                    state.firing_parent_ids.add(p.id)
                    prior_status = p.status
                    outcome: dict = {}
                    placed = False
                    try:
                        p.status = "STOPPED"
                        await SignalIterRepository.update_live_order(
                            p.id, status="STOPPED",
                        )
                        placed = await _market_sell(unfired_qty, "STOP", outcome)
                    finally:
                        try:
                            if (not placed and outcome.get("kind", "error")
                                    in _RETRYABLE_SELL_OUTCOMES):
                                await _rollback_refused_terminal_latch(
                                    state, p, latched_status="STOPPED",
                                    prior_status=prior_status, tier_label="STOP",
                                    outcome_kind=outcome.get("kind", "error"),
                                    ref_desc=f"{p.symbol} {spot:.2f}",
                                )
                        finally:
                            state.firing_parent_ids.discard(p.id)

                await _fire_with_inline_budget(state, p, _fire_stop, "STOP")
            return  # nothing else to do on this parent

    # Runner protect — BACKTESTED semantics (2026-07-08 fix). The live
    # defect: the trigger compared spot against the T1 FILL-MOMENT spot
    # (spx_target_t1_at_spx) in the SAME cycle _fire_tier had just set it to
    # the current spot — spot <= spot fired instantly, 3-for-3 on 7/8
    # ("sell all at T1"). The backtested rule (+$18-25/ct ladder evidence):
    #   anchor  = the T1 PLAN level (spx_target_t1), never the fill spot;
    #   trigger = a genuine retrace THROUGH the anchor with a small buffer
    #             (calls: spot <= t1 - buf; puts: spot >= t1 + buf);
    #   arming  = never in the cycle that fills T1 — evaluated BEFORE this
    #             cycle's tier fires (the backtest walker's ordering, same
    #             as _shadow_step), so the T1-fill cycle sees
    #             spx_target_t1_fired=False here and RP first becomes
    #             armable on the NEXT cycle.
    if (state.runner_protect_enabled and p.spx_target_t1_fired
        and p.spx_target_t1 is not None
        and (not p.spx_target_t2_fired or not p.spx_target_t3_fired)):
        rp_buffer = _rp_buffer_pts(p.spx_target_t1)
        retrace_hit = (
            (is_long_calls and spot <= p.spx_target_t1 - rp_buffer)
            or (not is_long_calls and spot >= p.spx_target_t1 + rp_buffer)
        )
        if retrace_hit:
            runner_qty = 0
            if not p.spx_target_t2_fired and p.spx_target_t2_qty:
                runner_qty += p.spx_target_t2_qty
            if not p.spx_target_t3_fired and p.spx_target_t3_qty:
                runner_qty += p.spx_target_t3_qty
            if runner_qty > 0:

                async def _fire_runner(runner_qty=runner_qty) -> None:
                    # OPTIMISTIC LATCH — flag the runner tiers BEFORE the
                    # awaited sell; revert to their prior values on any
                    # not-placed outcome (finally-gated; a tier genuinely
                    # banked earlier must never be un-flagged). Shielded +
                    # per-parent in-flight guard.
                    if p.id in state.firing_parent_ids:
                        return
                    state.firing_parent_ids.add(p.id)
                    prev_t2 = p.spx_target_t2_fired
                    prev_t3 = p.spx_target_t3_fired
                    outcome: dict = {}
                    placed = False
                    try:
                        p.spx_target_t2_fired = True; p.spx_target_t3_fired = True
                        await SignalIterRepository.update_live_order(
                            p.id, spx_target_t2_fired=True, spx_target_t3_fired=True,
                        )
                        placed = await _market_sell(runner_qty, "RUNNER_PROTECT", outcome)
                    finally:
                        try:
                            if not placed and outcome.get("kind") != "placed":
                                p.spx_target_t2_fired = prev_t2
                                p.spx_target_t3_fired = prev_t3
                                try:
                                    await SignalIterRepository.update_live_order(
                                        p.id, spx_target_t2_fired=prev_t2,
                                        spx_target_t3_fired=prev_t3,
                                    )
                                except Exception:
                                    logger.exception(
                                        "runner flag revert write failed for parent %s",
                                        p.id,
                                    )
                                kind = outcome.get("kind", "error")
                                if kind in _RETRYABLE_SELL_OUTCOMES:
                                    await _audit_tier_refusal_retry(
                                        state, p, "RUNNER_PROTECT", kind,
                                        f"{p.symbol} {spot:.2f}",
                                    )
                                elif kind == "refused_cap":
                                    _set_refusal_backoff(state, p)
                                    if _note_refusal_over_limit(state, p):
                                        await _park_stuck_parent(
                                            state, p, "RUNNER_PROTECT", kind,
                                            f"{p.symbol} {spot:.2f}",
                                        )
                        finally:
                            state.firing_parent_ids.discard(p.id)

                await _fire_with_inline_budget(
                    state, p, _fire_runner, "RUNNER_PROTECT",
                )

    async def _fire_tier(label: str, flag_field: str, at_field: str, qty: int) -> None:
        # OPTIMISTIC LATCH (2026-07-06): flag BEFORE the awaited sell; revert
        # on any not-placed outcome (finally-gated so cancellation can't skip
        # it) — a refused sell never phantom-banks the tier (7520C
        # guarantee). Shielded + per-parent in-flight guard so overlapping
        # watcher instances can never both sell one tier (review MAJOR-6).
        if p.id in state.firing_parent_ids:
            return
        state.firing_parent_ids.add(p.id)
        outcome: dict = {}
        placed = False
        try:
            setattr(p, flag_field, True)
            setattr(p, at_field, spot)
            await SignalIterRepository.update_live_order(
                p.id, **{flag_field: True, at_field: spot},
            )
            placed = await _market_sell(qty, label, outcome)
            if placed:
                await SignalIterRepository.update_live_order(
                    p.id, status="PARTIAL",
                )
        finally:
            try:
                if not placed and outcome.get("kind") != "placed":
                    setattr(p, flag_field, False)
                    setattr(p, at_field, None)
                    try:
                        await SignalIterRepository.update_live_order(
                            p.id, **{flag_field: False, at_field: None},
                        )
                    except Exception:
                        logger.exception(
                            "tier flag revert write failed for parent %s", p.id,
                        )
                    kind = outcome.get("kind", "error")
                    if kind in _RETRYABLE_SELL_OUTCOMES:
                        await _audit_tier_refusal_retry(
                            state, p, label, kind, f"{p.symbol} {spot:.2f}",
                        )
                    elif kind == "refused_cap":
                        # Cap-refused re-fires would otherwise flood at the
                        # 0.1s poll cadence (review BLOCKER-2): back off +
                        # count toward the park budget.
                        _set_refusal_backoff(state, p)
                        if _note_refusal_over_limit(state, p):
                            await _park_stuck_parent(
                                state, p, label, kind, f"{p.symbol} {spot:.2f}",
                            )
            finally:
                state.firing_parent_ids.discard(p.id)

    # T1
    if not p.spx_target_t1_fired and p.spx_target_t1 is not None and p.spx_target_t1_qty:
        t1_hit = (
            (is_long_calls and spot >= p.spx_target_t1)
            or (not is_long_calls and spot <= p.spx_target_t1)
        )
        if t1_hit:
            await _fire_with_inline_budget(state, p, lambda: _fire_tier(
                "T1", "spx_target_t1_fired", "spx_target_t1_at_spx",
                p.spx_target_t1_qty,
            ), "T1")

    # T2
    if not p.spx_target_t2_fired and p.spx_target_t2 is not None and p.spx_target_t2_qty:
        t2_hit = (
            (is_long_calls and spot >= p.spx_target_t2)
            or (not is_long_calls and spot <= p.spx_target_t2)
        )
        if t2_hit:
            await _fire_with_inline_budget(state, p, lambda: _fire_tier(
                "T2", "spx_target_t2_fired", "spx_target_t2_at_spx",
                p.spx_target_t2_qty,
            ), "T2")

    # T3
    if not p.spx_target_t3_fired and p.spx_target_t3 is not None and p.spx_target_t3_qty:
        t3_hit = (
            (is_long_calls and spot >= p.spx_target_t3)
            or (not is_long_calls and spot <= p.spx_target_t3)
        )
        if t3_hit:
            await _fire_with_inline_budget(state, p, lambda: _fire_tier(
                "T3", "spx_target_t3_fired", "spx_target_t3_at_spx",
                p.spx_target_t3_qty,
            ), "T3")

    # (Runner protect moved ABOVE the tier checks — 2026-07-08 fix. See the
    # block after the stop check: plan-level anchor + retrace buffer, never
    # evaluated in the cycle that filled T1.)

    # Sync parent status to CLOSED once the banked tiers cover the full
    # position (real remaining == 0) — not on the flags alone, so a tier
    # flagged without a completed sell can't orphan an open runner.
    #
    # ALL NON-ZERO TIERS FIRED, not the three flags literally (2026-07-09): a
    # small-qty split leaves 0-qty tiers (qty=2 -> [1,1,0]) that never evaluate,
    # so requiring spx_target_t3_fired would DEADLOCK CLOSED for a 1/2-lot that
    # has fully exited. _all_tiers_banked (sold >= original) stays the real gate.
    _tier_qtys_now = [p.spx_target_t1_qty or 0, p.spx_target_t2_qty or 0, p.spx_target_t3_qty or 0]
    _tier_fired_now = [p.spx_target_t1_fired, p.spx_target_t2_fired, p.spx_target_t3_fired]
    _nonzero_all_fired = all(f for q, f in zip(_tier_qtys_now, _tier_fired_now) if q > 0)
    if (_nonzero_all_fired and p.status not in ("STOPPED", "CLOSED")
            and _all_tiers_banked(_tier_qtys_now, _tier_fired_now, p.qty)):
        await SignalIterRepository.update_live_order(p.id, status="CLOSED")


# --- EOD assignment guard (2026-07-17, SPY mirror build) ---------------------
# SPY (and every equity-class option the bot may manage) is AMERICAN-style,
# PHYSICALLY settled: a 0DTE long carried into the close is auto-exercised
# into shares (or expires against an assignment). SPX is European/cash-
# settled and is NEVER touched by this guard. HARD RULE (never cut): flatten
# ALL bot-managed equity-option parents expiring today from 15:40 ET. Window
# end 16:15 ET — SPY/QQQ/IWM options trade until 16:15; past that a retry is
# pure churn (the refusal budget parks anything still open, loudly).
_EOD_FLAT_END_ET = "16:15"
_EOD_FLAT_CHECK_INTERVAL_MS = 5_000

# NYSE EARLY-CLOSE sessions (equities close 13:00 ET, equity options 13:15):
# the normal 15:40 window would never arrive before the close, silently
# skipping the assignment guard. On these dates the flat runs 12:40-13:15 ET
# instead. Official NYSE 2026 holiday calendar (hardcoded through 2026, same
# convention as event_calendar.FOMC_DATES — UPDATE for 2027+):
#   2026-11-27 (day after Thanksgiving), 2026-12-24 (Christmas Eve).
_EARLY_CLOSE_DATES = frozenset({"20261127", "20261224"})
_EOD_FLAT_START_EARLY_ET = "12:40"
_EOD_FLAT_END_EARLY_ET = "13:15"


def _eod_flat_window(today_ymd: str) -> tuple[str, str]:
    """[start, end) ET window for the assignment flat on this session."""
    if today_ymd in _EARLY_CLOSE_DATES:
        return _EOD_FLAT_START_EARLY_ET, _EOD_FLAT_END_EARLY_ET
    return _eod_flat_start_et(), _EOD_FLAT_END_ET


def _eod_flat_start_et() -> str:
    """Operator knob (FEF_EOD_FLAT_START_ET), validated 'HH:MM' — anything
    malformed falls back to the 15:40 default (fail-safe: the guard must
    never be silently disabled by a typo)."""
    raw = os.environ.get("FEF_EOD_FLAT_START_ET", "15:40")
    if (len(raw) == 5 and raw[2] == ":"
            and raw[:2].isdigit() and raw[3:].isdigit()
            and raw < _EOD_FLAT_END_ET):
        return raw
    return "15:40"


def _et_now_hhmm_ymd() -> tuple[str, str]:
    """('HH:MM', 'YYYYMMDD') in America/New_York (DST-correct); falls back
    to fixed UTC-4 if zoneinfo is unavailable."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        from datetime import timedelta, timezone
        now = datetime.now(timezone(timedelta(hours=-4)))
    return now.strftime("%H:%M"), now.strftime("%Y%m%d")


def _cancel_working_entry_order(state, p) -> None:
    """Best-effort cancel of the parent's WORKING entry BUY as part of the
    EOD flat (adversarial review Finding 4b): an unfilled/partial entry left
    working can fill AFTER the flat has sold/parked — an unmanaged equity
    0DTE long riding into the close, the exact disaster this guard exists to
    prevent. Never raises; a failed cancel leaves the entry to the 45s
    entry-timeout monitor / reconcile as before."""
    try:
        from fef_sidecar.ibkr.live import LiveIBKRProvider
        if not isinstance(state.ibkr, LiveIBKRProvider) or p.ib_order_id is None:
            return
        if int(getattr(p, "filled_qty", 0) or 0) >= int(p.qty or 0):
            return  # fully filled — nothing can be working
        ib = state.ibkr._ib  # type: ignore[attr-defined]
        for t in ib.trades():
            if (getattr(getattr(t, "order", None), "orderId", None) == p.ib_order_id
                    and getattr(t.order, "action", "") == "BUY"):
                status = str(getattr(getattr(t, "orderStatus", None), "status", "") or "")
                if status not in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                    ib.cancelOrder(t.order)
                    logger.warning(
                        "EOD flat: cancelled WORKING entry BUY for parent %s "
                        "(%s %s%s, ib_order_id=%s, status=%s)",
                        p.id, p.symbol, p.strike, p.right, p.ib_order_id, status,
                    )
                break
    except Exception:
        logger.exception("EOD flat: entry-cancel failed for parent %s", p.id)


def _eod_flat_due(p, now_hhmm: str, today_ymd: str) -> bool:
    """True iff parent p must be flattened by the assignment guard now:
    equity-class (non-SPX) option, expiring today or earlier, inside the
    [start, 16:15) ET window."""
    if str(getattr(p, "symbol", "") or "").upper() == "SPX":
        return False  # European/cash-settled — no assignment risk
    expiry = str(getattr(p, "expiry", "") or "")
    if len(expiry) != 8 or not expiry.isdigit() or expiry > today_ymd:
        return False
    start, end = _eod_flat_window(today_ymd)
    return start <= now_hhmm < end


async def _eod_assignment_flat_cycle(state, parents) -> set:
    """Flatten every due equity-option parent (throttled to one sweep per
    _EOD_FLAT_CHECK_INTERVAL_MS). Returns the parent ids flattened THIS
    cycle so the caller excludes them from tier/stop evaluation (their
    in-memory tier flags are stale-open until the next DB re-query).

    Race discipline mirrors the stop path exactly: per-parent
    firing_parent_ids latch (an in-flight stop/tier fire wins; we retry in
    5s), optimistic terminal latch (CLOSED) persisted BEFORE the awaited
    sell, finally-gated refusal rollback via the shared machinery, the whole
    fire shielded (_run_exit_fire_shielded) so cancellation can never strand
    the latch. Sells route through _watcher_safe_sell -> safe_market_sell —
    the 7/6 anti-short + lifetime-cap guards apply to SPY exactly as SPX."""
    from fef_sidecar.db.repository import SignalIterRepository

    handled: set = set()
    now_ms = int(_time.time() * 1000)
    last = getattr(state, "_eod_flat_last_check_ms", 0)
    if now_ms - last < _EOD_FLAT_CHECK_INTERVAL_MS:
        return handled
    try:
        state._eod_flat_last_check_ms = now_ms  # type: ignore[attr-defined]
    except Exception:
        pass
    now_hhmm, today_ymd = _et_now_hhmm_ymd()

    # Calendar-coverage tripwire (review Finding 9): the early-close list is
    # hardcoded through 2026 — past that the window silently reverts to
    # 15:40 on half days and the guard never fires before a 13:00 close.
    # One loud row per process once the coverage year is exceeded.
    try:
        if (int(today_ymd[:4]) > 2026
                and not getattr(state, "_early_close_coverage_warned", False)):
            state._early_close_coverage_warned = True  # type: ignore[attr-defined]
            await SignalIterRepository.write_audit_log(
                category="spy_eod_flat", actor="system",
                summary=(
                    "EARLY-CLOSE CALENDAR COVERAGE EXPIRED: _EARLY_CLOSE_DATES "
                    "in spx_target_watcher.py is hardcoded through 2026 — on a "
                    "half day the assignment flat would run at 15:40, AFTER a "
                    "13:00 close. Update the list before trading equity 0DTE "
                    "mirrors this year."
                ),
            )
    except Exception:
        pass

    for p in parents:
        if p.id is None or not _eod_flat_due(p, now_hhmm, today_ymd):
            continue
        if p.id in state.firing_parent_ids:
            continue  # stop/tier/dollar fire in flight — retry next sweep
        if _refusal_backoff_active(state, p):
            continue
        # Remaining = unfired tier qty — the same computation the stop path
        # uses. Tier fired-flags are NOT touched (they record banked
        # partials; status is the sole resolved-marker).
        unfired_qty = 0
        if not p.spx_target_t1_fired and p.spx_target_t1_qty:
            unfired_qty += p.spx_target_t1_qty
        if not p.spx_target_t2_fired and p.spx_target_t2_qty:
            unfired_qty += p.spx_target_t2_qty
        if not p.spx_target_t3_fired and p.spx_target_t3_qty:
            unfired_qty += p.spx_target_t3_qty
        if unfired_qty <= 0:
            continue  # fully banked — the CLOSED-sync owns it

        async def _fire_eod_flat(p=p, unfired_qty=unfired_qty):
            if p.id in state.firing_parent_ids:
                return False
            state.firing_parent_ids.add(p.id)
            prior_status = p.status
            outcome: dict = {}
            placed = False
            try:
                p.status = "CLOSED"
                await SignalIterRepository.update_live_order(p.id, status="CLOSED")
                # Cancel a still-working entry BUY first (Finding 4b): the
                # sell below only covers what FILLED; the working remainder
                # must not fill after the flat and ride into the close.
                _cancel_working_entry_order(state, p)
                placed = await _watcher_safe_sell(
                    state, p, unfired_qty, "EOD_FLAT",
                    f"{now_hhmm} ET assignment guard", outcome=outcome,
                )
                if placed:
                    try:
                        await SignalIterRepository.write_audit_log(
                            category="spy_eod_flat", actor="system",
                            summary=(
                                f"EOD FLAT: market-sold {unfired_qty}x {p.symbol} "
                                f"{p.strike}{p.right} exp {p.expiry} at {now_hhmm} ET "
                                f"— assignment guard (American/physical settlement; "
                                f"never hold equity 0DTE to the close); parent "
                                f"{p.id} closed before expiry"
                            ),
                            details={
                                "parent_id": p.id, "symbol": p.symbol,
                                "strike": p.strike, "right": p.right,
                                "expiry": p.expiry, "qty": unfired_qty,
                                "time_et": now_hhmm,
                                "mirror_of": getattr(p, "mirror_of", None),
                            },
                        )
                    except Exception:
                        logger.exception(
                            "spy_eod_flat audit write failed for parent %s", p.id,
                        )
            finally:
                try:
                    if (not placed and outcome.get("kind", "error")
                            in _RETRYABLE_SELL_OUTCOMES):
                        await _rollback_refused_terminal_latch(
                            state, p, latched_status="CLOSED",
                            prior_status=prior_status, tier_label="EOD_FLAT",
                            outcome_kind=outcome.get("kind", "error"),
                            ref_desc=f"{now_hhmm} ET assignment guard",
                        )
                finally:
                    state.firing_parent_ids.discard(p.id)
            return placed

        try:
            placed = await _fire_with_inline_budget(
                state, p, _fire_eod_flat, "EOD_FLAT",
            )
        except Exception:
            logger.exception("EOD flat fire failed for parent %s", p.id)
            placed = False
        if placed:
            handled.add(p.id)
    return handled


async def spx_target_watcher_loop(state) -> None:
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.engine.loop_watchdog import heartbeat as _wd_beat

    interval = float(os.environ.get("SPX_WATCHER_INTERVAL_SEC", "0.1"))
    logger.info("SPX target watcher loop starting (interval=%.2fs)", interval)
    # fixed_dollar exits poll the live option quote (an IBKR round-trip), so they
    # run at a slower per-parent cadence than the 0.1s SPX-spot loop.
    fixed_dollar_check_ms = int(
        float(os.environ.get("FEF_FIXED_DOLLAR_CHECK_SEC", "3.0")) * 1000
    )
    # Per-parent log throttle so the "spot near stop" diagnostic doesn't
    # flood the audit_log when iterating at 0.1s (see _NEAR_STOP_LOG_INTERVAL_MS).
    last_near_stop_log_ms: dict[int, int] = {}

    while True:
        try:
            _wd_beat(state, "spx-target-watcher")
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

            # EOD ASSIGNMENT GUARD (2026-07-17, SPY mirror build): flatten
            # bot-managed EQUITY-option parents (SPY mirrors and any other
            # non-SPX watcher position) expiring today, from 15:40 ET —
            # American/physical settlement must never ride into the close.
            # Runs FIRST so a flattened parent is excluded from this cycle's
            # tier/stop/dollar evaluation (its in-memory flags are stale).
            try:
                _eod_handled = await _eod_assignment_flat_cycle(state, parents)
                if _eod_handled:
                    parents = [p for p in parents if p.id not in _eod_handled]
            except Exception:
                logger.exception("EOD assignment flat cycle failed")

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

            # Per-cycle cache for per-symbol spot resolution (2026-07-07):
            # each spx_points parent is evaluated against ITS OWN symbol's
            # spot, never the active-chart price of a different symbol.
            spot_cache: dict[str, tuple[float | None, str | None]] = {}

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

                # PER-SYMBOL SPOT (2026-07-07): resolve the parent's OWN
                # symbol spot (active tick only when active_symbol matches,
                # else scanner snapshot <=180s, else 1-min bar <=10min). No
                # symbol-correct spot -> SKIP this parent (audited 1/5min).
                sym_spot, sym_src = await _watcher_symbol_spot(
                    state, p, spot, spot_cache,
                )
                if sym_spot is None:
                    continue
                await _eval_spx_points_parent(
                    state, p, sym_spot, last_near_stop_log_ms, spot_src=sym_src,
                )

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
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.engine.loop_watchdog import heartbeat as _wd_beat

    logger.info("SPX stop tick-watcher starting (tick-driven fast path)")
    # Cache open parents for short windows so we don't re-query DB on
    # every single tick. Refreshed when stale.
    parents_cache: list = []
    parents_cache_at_ms: int = 0
    CACHE_TTL_MS = 500  # refresh open-parents list at most every 500ms

    async with state.broadcaster.subscribe() as queue:
        while True:
            try:
                _wd_beat(state, "spx-stop-tick-watcher")
                try:
                    # Bounded wait so the watchdog heartbeat keeps stamping
                    # even when no ticks arrive (closed market / stalled feed).
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                except (asyncio.TimeoutError, TimeoutError):
                    continue
                # Only tick events drive stop detection. Skip everything else.
                if getattr(event, "type", None) != "tick":
                    continue
                spot = float(getattr(event, "spx_price", 0) or 0)
                if spot <= 0:
                    continue
                # SYMBOL TRUST (2026-07-23 incident B): an event WITHOUT an
                # explicit symbol is UNTRUSTED, never assumed SPX — the bar
                # stream published the ACTIVE chart's (ES) bar closes as
                # unlabeled TickEvents and the old `or "SPX"` default walked
                # an ES-priced 7464.25 into parent 799's stop evaluation
                # (STOP/TICK at "SPX 7464.25" while cash sat at ~7429).
                symbol = getattr(event, "symbol", None)
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

                for p in list(parents_cache):
                    resolved = await _tick_check_parent_stop(state, p, spot, symbol)
                    if resolved:
                        # Refresh cache so the next ticks don't re-evaluate
                        # this just-stopped parent. A rolled-back refusal
                        # stays cached so the retry clock applies to it.
                        parents_cache = [c for c in parents_cache if c.id != p.id]
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("spx_stop_tick_watcher iteration failed")


async def _tick_check_parent_stop(state, p, spot: float, symbol: str) -> bool:
    """Tick fast-path stop check for ONE cached spx_points parent. Returns
    True iff the parent resolved terminally (caller drops it from the tick
    cache); False = keep managing (no trigger, guard skip, or a refusal that
    rolled the optimistic latch back).

    Guards (2026-07-07 incident): the tick is only comparable to parents of
    ITS OWN symbol — the SPY 749P stop 751.16 was evaluated against the SPX
    tick 7533 (dist=-6781pt) and instantly "triggered". Cross-symbol parents
    are skipped here entirely; the poll loop manages them on their own
    resolved spot. Units guard + arm-time plan sanity + entry-fill grace
    mirror the poll path.
    """
    from fef_sidecar.db.repository import SignalIterRepository
    from fef_sidecar.ibkr.live import LiveIBKRProvider

    if p.symbol != symbol:
        return False
    if p.id in state.firing_parent_ids:
        return False
    if not await _units_guard_ok(state, p, spot):
        return False
    if not await _maybe_reanchor_plan(state, p, spot, "tick_stream"):
        return False
    if _refusal_backoff_active(state, p):
        return False
    if await _entry_fill_grace_pending(state, p):
        return False
    is_long_calls = p.right == "C"
    effective_stop = (
        p.dynamic_stop_level
        if p.dynamic_stop_level is not None
        else p.spx_target_stop
    )
    if effective_stop is None:
        return False
    stop_hit = (
        (is_long_calls and spot <= effective_stop)
        or (not is_long_calls and spot >= effective_stop)
    )
    if not stop_hit:
        return False
    # Compute unfired qty from the cached snapshot. The long_qty position
    # guard in the IBKR sell call will clip if the cache is stale (e.g., T1
    # fired between cache refreshes).
    unfired_qty = 0
    if not p.spx_target_t1_fired and p.spx_target_t1_qty:
        unfired_qty += p.spx_target_t1_qty
    if not p.spx_target_t2_fired and p.spx_target_t2_qty:
        unfired_qty += p.spx_target_t2_qty
    if not p.spx_target_t3_fired and p.spx_target_t3_qty:
        unfired_qty += p.spx_target_t3_qty
    if unfired_qty <= 0:
        return False

    async def _fire_tick_stop() -> bool:
        """Latch -> sell -> (terminal | finally-gated rollback). Shielded so
        tick-watcher cancellation mid-sell can never strand the latch
        (review BLOCKER-1); the cap reservation is released on every path
        that doesn't confirm placement (review BLOCKER-2)."""
        if p.id in state.firing_parent_ids:
            return False
        # Acquire the firing lock — only one path may fire this parent at a time.
        state.firing_parent_ids.add(p.id)
        prior_status = p.status
        # Fail-safe default (re-review N2): a CancelledError anywhere in the
        # latch->sell region skips except-Exception handlers — the finally
        # must still roll back, so "error" is the default and each decisive
        # TERMINAL path overwrites it explicitly (mirrors the outcome-dict
        # pattern every other fire uses).
        rollback_kind: str | None = "error"
        ref = f"{symbol} {spot:.2f}"
        now_ms = int(_time.time() * 1000)
        try:
            # OPTIMISTIC LATCH (2026-07-06): status=STOPPED BEFORE the awaited
            # sell so the poll loop / a stale spawned check can't re-fire this
            # parent mid-placement. status=STOPPED is the resolved-marker; don't
            # overwrite the per-tier fired flags as cleanup — that pollutes
            # partial-tracking for every downstream analytic.
            # REFUSAL ROLLBACK (2026-07-07): the terminal status only survives an
            # actually-placed order — a guard-refused/errored sell reverts it so
            # the parent stays managed (the eviction hole: "STOP/TICK SKIPPED: 0
            # available after reservation" returned with STOPPED persisted).
            p.status = "STOPPED"
            await SignalIterRepository.update_live_order(
                p.id, status="STOPPED",
            )
            if not isinstance(state.ibkr, LiveIBKRProvider):
                rollback_kind = "dry"  # terminal — DRY keeps the resolved-marker
                await SignalIterRepository.write_audit_log(
                    category="spx_watcher", actor="system",
                    summary=(
                        f"[DRY/TICK] STOP crossed: would market-sell-to-close "
                        f"{unfired_qty}x {p.symbol} {p.strike}{p.right} at {ref}"
                    ),
                )
                return True
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
                # Contract-level pending-sell reservation. Prevents
                # cross-parent races on same contract — the 7-parent SPX
                # 7520C race that placed a naked short occurred here.
                # safe_market_sell is the structural backstop if this
                # reservation misses.
                contract_key = (p.symbol, float(p.strike), p.right, p.expiry)
                pending = state.firing_contracts.get(contract_key, 0)
                available = max(0, long_qty - pending)
                sell_qty = min(unfired_qty, available)
                if sell_qty <= 0:
                    sold, cap = _lifetime_cap_state(state, p)
                    if long_qty <= 0 and cap > 0 and sold >= cap:
                        # Broker flat AND the watcher already sold the full
                        # fill — the exit is complete; keep the terminal latch.
                        rollback_kind = "refused_cap"  # terminal
                        await SignalIterRepository.write_audit_log(
                            category="spx_watcher", actor="system",
                            summary=(
                                f"STOP/TICK RESOLVED: broker flat and watcher already "
                                f"sold {sold}/{cap} for parent {p.id} "
                                f"({p.symbol} {p.strike}{p.right}) — exit complete"
                            ),
                        )
                        return True
                    await SignalIterRepository.write_audit_log(
                        category="spx_watcher", actor="system",
                        summary=(
                            f"STOP/TICK SKIPPED: 0 available after reservation "
                            f"(long={long_qty}, pending={pending}, requested={unfired_qty}) "
                            f"for {p.symbol} {p.strike}{p.right} — refusing to short"
                        ),
                    )
                    rollback_kind = "refused_guard"
                    return False
                # LIFETIME SELL CAP (2026-07-06) — absolute oversell backstop,
                # independent of the (stale-window-prone) broker position
                # snapshot. See _lifetime_cap_reserve.
                reserved = _lifetime_cap_reserve(state, p, sell_qty)
                if reserved <= 0:
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
                    if available >= unfired_qty:
                        # CAP-vs-BROKER DIVERGENCE (review BLOCKER-2): the cap
                        # claims fully sold, yet the broker still shows the
                        # whole remaining qty available — a leaked reservation
                        # would strand OPEN contracts behind a terminal latch.
                        # Escalate + keep managed; the refusal streak bounds
                        # the retries (park -> reconcile).
                        await SignalIterRepository.write_audit_log(
                            category="watcher_cap_divergence", actor="system",
                            summary=(
                                f"LIFETIME-CAP DIVERGENCE on STOP/TICK: cap shows "
                                f"sold {sold}/{cap} for parent {p.id} "
                                f"({p.symbol} {p.strike}{p.right}) but broker shows "
                                f"long={long_qty}, pending={pending} — keeping parent "
                                f"managed; VERIFY POSITION"
                            ),
                            details={"parent_id": p.id, "cum_sold": sold, "cap": cap,
                                     "broker_long": long_qty, "pending": pending,
                                     "requested": unfired_qty},
                        )
                        rollback_kind = "refused_cap_divergence"
                        return False
                    rollback_kind = "refused_cap"  # terminal — genuinely sold out
                    return True
                sell_qty = reserved
                placed_qty = 0
                try:
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
                        placed_qty = int(result.sell_qty)
                        rollback_kind = "placed"  # terminal — order transmitted
                        _clear_refusal_streak(state, p)
                        try:
                            # Post-placement bookkeeping is BaseException-
                            # guarded (re-review N3): a deadline-cancel here
                            # must not skip the terminal return.
                            tick_age_ms = (
                                now_ms - state.last_live_tick_ms
                                if state.last_live_tick_ms > 0 else -1
                            )
                            # Banked-$ audit line (spawned — see
                            # _write_tier_fill_audit).
                            asyncio.create_task(_write_tier_fill_audit(
                                state, p, "STOP/TICK", result, ref,
                                extra_summary=(
                                    f"stop={effective_stop:.2f}, "
                                    f"tick_age={tick_age_ms}ms"
                                ),
                                extra_details={
                                    "stop_level": effective_stop,
                                    "tick_age_ms": tick_age_ms,
                                },
                            ))
                            await SignalIterRepository.write_watcher_sell_child(
                                parent=p, bracket_role="stop",
                                qty=int(result.sell_qty),
                                ib_order_id=int(result.trade.order.orderId),
                                ib_perm_id=int(getattr(result.trade.order, "permId", 0) or 0) or None,
                                notes=f"stop_tick@{symbol}={spot:.2f},age={tick_age_ms}ms",
                            )
                        except BaseException:
                            logger.exception(
                                "stop_tick: post-placement bookkeeping interrupted "
                                "for parent %s (order already placed)", p.id,
                            )
                        return True
                    # safe_market_sell wrote its own audit (HARD-BLOCK /
                    # no_position / placement_error) — no order placed, so
                    # the latch must roll back.
                    rollback_kind = (
                        "refused_guard"
                        if result.reason in ("no_position", "short_blocked")
                        else "error"
                    )
                    return False
                finally:
                    # CAP-LEAK GUARD (review BLOCKER-2): release whatever was
                    # reserved but not confirmed placed — refusal, exception,
                    # or cancellation alike.
                    if placed_qty < sell_qty:
                        _lifetime_cap_release(state, p, sell_qty - placed_qty)
            except Exception:
                logger.exception("spx_stop_tick_watcher market sell failed")
                # Exception before placement (qualify/positions/reserve) —
                # revert the latch so the parent stays managed and retries.
                rollback_kind = rollback_kind or "error"
                return False
        finally:
            try:
                if rollback_kind in _RETRYABLE_SELL_OUTCOMES:
                    await _rollback_refused_terminal_latch(
                        state, p, latched_status="STOPPED",
                        prior_status=prior_status, tier_label="STOP/TICK",
                        outcome_kind=rollback_kind, ref_desc=ref,
                    )
            finally:
                state.firing_parent_ids.discard(p.id)

    return bool(await _fire_with_inline_budget(
        state, p, _fire_tick_stop, "STOP/TICK",
    ))
