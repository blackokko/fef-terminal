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

import asyncio
import logging
import math
import os
import time
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
    # Dollar P&L targets (when exit_strategy='fixed_dollar'). The watcher fires
    # staged market sells as live open P&L crosses each target, and sells all
    # remaining when open loss reaches d_stop (negative $). No IBKR option-price
    # children are placed for fixed_dollar — the watcher manages it in $ terms,
    # exactly as it does spx_points in point terms.
    d_t1: float | None = None
    d_t2: float | None = None
    d_t3: float | None = None
    d_stop: float | None = None
    # Spot SPX at the moment auto-trade fired — needed for MFE / R calculations
    # in the spx_target_watcher's BE ladder. None disables the ladder.
    entry_spx_price: float | None = None
    # When set, spx_target_watcher market-exits remaining qty if the option
    # premium drops enough that the open position is down by this many
    # dollars. Auto-trader sets this to -3000 by default. None = no $ cap.
    dollar_loss_cap: float | None = None
    # When true, submit a MarketOrder for the parent BUY instead of a
    # LimitOrder. RETIRED for auto-trade 2026-07-16: IBKR's price cap on
    # market option orders rested live_order 776 for 4m20s on a running ask
    # ($8.10 fill vs ~$5.50-6.00 at signal). Auto-trade now sends a
    # marketable LMT (fresh ask + buffer, entry_pricing.py) instead; the
    # flag remains for manual/legacy callers.
    use_market_entry: bool = False
    # If set (>0), place_bracket spawns entry_timeout.entry_timeout_monitor:
    # an entry not FULLY filled within this many seconds is cancelled and the
    # fire stood down (partials: remainder cancelled, filled portion kept +
    # watcher-managed). None/0 = no timeout (manual tickets).
    entry_timeout_sec: float | None = None


_UNSET: object = object()

_mkt_downgrade_warned = False


def _warn_mkt_fallback_downgraded(entry_timeout_sec) -> None:
    """Log ONCE that the chase MKT fallback was downgraded to chase-only
    because the entry-timeout watchdog is disabled (entry_timeout_sec <= 0).
    A market order with no watchdog can hang on IBKR's price cap (2026-07-16),
    so with the timeout off we refuse to go naked-MKT and rest a LIMIT
    instead. Rare (operator explicitly disabled the timeout)."""
    global _mkt_downgrade_warned
    if _mkt_downgrade_warned:
        return
    _mkt_downgrade_warned = True
    logger.warning(
        "entry chase: MKT fallback DOWNGRADED to chase-only because the entry "
        "timeout is disabled (entry_timeout_sec=%s) — a market order with no "
        "watchdog can hang on IBKR's price cap (2026-07-16). Re-enable "
        "FEF_ENTRY_TIMEOUT_SEC>0 to restore the MKT fallback.",
        entry_timeout_sec,
    )


def _spawn_entry_timeout(
    *, state, ibkr, spec: BracketSpec, parent_id: int,
    ib_order_id: int | None, parent_trade, source: str,
    signal_ref: dict | None, child_ids: tuple[int, ...],
    timeout_override: float | None = None,
    limit_override: object = _UNSET,
) -> None:
    """Arm the 45s entry-timeout watchdog on a just-placed LIVE parent BUY
    (2026-07-16 hang incident). No-op when the spec carries no timeout
    (manual tickets). Spawn failure can never break order placement.

    timeout_override: when the bounded chase (entry_chase.py) already spent
    part of the 45s window, arm the timeout for the REMAINING budget so the
    whole entry (chase + rest, including any MKT fallback) stays under the
    SAME ceiling. None => use spec.entry_timeout_sec.
    limit_override: the FINAL working order's limit ($), or None when the
    chase's MKT fallback fired. _UNSET => derive from the spec (non-chase
    callers) — preserves the legacy behaviour exactly."""
    t_out = spec.entry_timeout_sec if timeout_override is None else timeout_override
    if not t_out or t_out <= 0:
        return
    if limit_override is _UNSET:
        limit_price = None if spec.use_market_entry else spec.entry_limit
    else:
        limit_price = limit_override
    try:
        from fef_sidecar.engine.entry_timeout import spawn_entry_timeout_monitor
        spawn_entry_timeout_monitor(
            state=state, ibkr=ibkr, parent_id=parent_id,
            ib_order_id=ib_order_id, symbol=spec.symbol, qty=spec.qty,
            exit_strategy=spec.exit_strategy, source=source,
            limit_price=limit_price,
            signal_iter_id=(signal_ref or {}).get("signal_iter_id"),
            child_ids=child_ids, timeout_sec=t_out,
            parent_trade=parent_trade,
        )
    except Exception:
        logger.exception("entry-timeout monitor spawn failed for parent %s", parent_id)


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
    source: str = "manual_ticket",
    signal_ref: dict | None = None,
    state: object | None = None,
    mirror_of: int | None = None,
) -> dict:
    """Submit a bracket order: parent BUY + 3 limit SELL children + stop SELL.

    Returns dict with order IDs + status. If FEF_LIVE_TRADING is false OR
    confirm is false, returns dry-run records without hitting IBKR.

    source: who initiated this order — 'manual_ticket' (UI Send-to-IBKR),
    'auto_main' (fef_tick_loop), 'auto_scanner' (multi-symbol scanner) or
    'auto_pattern' (pattern trigger). Drives audit attribution: auto orders
    log with actor='system' + an 'AUTO-TRADE[<source>]' summary prefix and
    details.source, and the LiveOrder parent notes carry 'source=<source>;'
    — so auto-vs-manual AND scanner-vs-pattern are unambiguous from the
    audit trail (previously everything logged actor='user').
    signal_ref: optional verdict/trigger context stamped into audit details.
    state: AppState (auto-trade callers only) — handed to the entry-timeout
    monitor so a zero-fill stand-down can release the per-symbol session-cap
    slot. None (manual path) simply skips budget release.
    mirror_of: SPY-mirror ledger tag (2026-07-17) — the SPX parent
    LiveOrder.id when this bracket is the SPY mirror of an SPX auto-trade
    fire. Stamped into LiveOrder.mirror_of AND appended to notes
    ('mirror_of=<id>;') so EOD attribution can split SPX-book (verdict
    window) from SPY-mirror (its own live-confirmation ledger). None for
    every non-mirror order.

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
    # Any non-manual source is an auto fire. Known sources: 'manual_ticket',
    # 'auto_main' (fef_tick_loop), 'auto_scanner' (multi-symbol scanner),
    # 'auto_pattern' (pattern trigger). The prefix carries the exact source
    # so per-source P&L attribution is a plain LIKE query on audit/order rows.
    is_auto = source != "manual_ticket"
    audit_actor = "system" if is_auto else "user"
    audit_prefix = f"AUTO-TRADE[{source}] " if is_auto else ""

    # SAFETY GATE: when FEF_REQUIRE_PAPER_ACCOUNT=true (default), refuse to
    # submit live orders to a non-paper account. Catches the case where TWS
    # is logged into the live account but exposing port 7497.
    if live:
        try:
            await assert_paper_if_required(ibkr)
        except RuntimeError as e:
            from fef_sidecar.engine.auto_trader import BlockReason as _BR_or
            await SignalIterRepository.write_audit_log(
                category="order_refused", actor="system",
                summary=f"REFUSED: {e}",
                details={"reason": str(e), "spec_qty": spec.qty,
                         "spec_strike": spec.strike, "spec_right": spec.right},
                block_reason_code=_BR_or.ORDER_REFUSED,
            )
            return {"ok": False, "error": str(e), "refused_safety_gate": True}

    contract = Contract(
        symbol=spec.symbol, kind=ContractKind.OPTION,
        exchange="CBOE" if spec.symbol == "SPX" else "SMART",
        expiry=spec.expiry, strike=spec.strike, right=spec.right,
    )

    # Capture account class + ID at order time so research scripts can
    # filter paper from live data later. We stamp this into notes since
    # the LiveOrder schema doesn't have dedicated columns; a schema
    # migration to add proper fields is in DEFERRED_CHANGES.
    _acct_class = getattr(ibkr, "account_class", "unknown") or "unknown"
    _acct_id = getattr(ibkr, "account_id", "") or ""
    _acct_tag = f"account_class={_acct_class}; account_id={_acct_id}; source={source};"
    if mirror_of is not None:
        _acct_tag += f" mirror_of={int(mirror_of)};"

    # Watcher-managed strategies (spx_points + fixed_dollar) place ONLY the
    # parent BUY; the watcher fires staged market sells from the targets stored
    # on this row (SPX-cross for spx_points, live $ P&L for fixed_dollar). Tier
    # quantities + fired flags reuse the spx_target_*_qty / *_fired columns for
    # both. fixed_dollar deliberately stores NO option-price children — that
    # removes the fragile $→option-price translation (the -$500→$1.35-stop bug).
    _watcher_managed = spec.exit_strategy in ("spx_points", "fixed_dollar")
    parent = LiveOrder(
        ticket_id=ticket_id, symbol=spec.symbol, strike=spec.strike,
        right=spec.right, expiry=spec.expiry, action="BUY", qty=spec.qty,
        order_type="MKT" if spec.use_market_entry else "LMT",
        limit_price=spec.entry_limit,  # informational when market order
        bracket_role="parent", dry_run=dry_run, status="DRY_RUN" if dry_run else "SUBMITTED",
        exit_strategy=spec.exit_strategy,
        spx_target_t1=spec.spx_t1, spx_target_t2=spec.spx_t2,
        spx_target_t3=spec.spx_t3, spx_target_stop=spec.spx_stop,
        spx_target_t1_qty=spec.t1_qty if _watcher_managed else None,
        spx_target_t2_qty=spec.t2_qty if _watcher_managed else None,
        spx_target_t3_qty=spec.t3_qty if _watcher_managed else None,
        dollar_target_t1=spec.d_t1, dollar_target_t2=spec.d_t2,
        dollar_target_t3=spec.d_t3, dollar_target_stop=spec.d_stop,
        dollar_loss_cap=spec.dollar_loss_cap,
        # BE ladder (spx_points only): starts at the original stop, ratchets up
        # as MFE grows. fixed_dollar manages its stop in $ terms, not via the
        # SPX-point dynamic_stop_level, so leave it None there.
        dynamic_stop_level=spec.spx_stop if spec.exit_strategy == "spx_points" else None,
        entry_spx_price=spec.entry_spx_price,
        notes=_acct_tag,
        mirror_of=mirror_of,
    )
    parent_saved = await SignalIterRepository.write_live_order(parent)
    children_saved = []
    # Watcher-managed: no option-price children, only the parent. The
    # spx_target_watcher_loop fires market sells (SPX cross or $ P&L cross).
    if _watcher_managed:
        # Exit-ladder description shared by the dry-run + live audit lines —
        # prints the ACTUAL spec targets (post any per-symbol $ scaling) so
        # the ladder each trade will run under is visible in the audit trail.
        if spec.exit_strategy == "fixed_dollar":
            _exits_desc = (f"Watcher fires at $ T1/T2/T3/STOP="
                           f"{spec.d_t1}/{spec.d_t2}/{spec.d_t3}/{spec.d_stop}")
        else:
            _exits_desc = (f"Watcher fires at SPX T1/T2/T3/STOP="
                           f"{spec.spx_t1}/{spec.spx_t2}/{spec.spx_t3}/{spec.spx_stop}")
        if dry_run:
            # Audit even in dry-run — attribution parity with the live path.
            # (Previously this early return skipped the audit entirely, so
            # dry-run watcher-managed orders left no 'order' trail at all.)
            entry_desc = "MARKET" if spec.use_market_entry else f"LMT @ ${spec.entry_limit:.2f}"
            await SignalIterRepository.write_audit_log(
                category="order", actor="system",
                summary=f"{audit_prefix}DRY_RUN {spec.exit_strategy} entry: BUY {spec.qty} {spec.symbol} {spec.strike}{spec.right} {entry_desc}. {_exits_desc}",
                details={"parent_order_id": parent_saved.id,
                         "exit_strategy": spec.exit_strategy,
                         "order_type": "MKT" if spec.use_market_entry else "LMT",
                         "source": source, "signal_ref": signal_ref},
            )
            return {
                "ok": True, "dry_run": True,
                "parent_order_id": parent_saved.id,
                "children": [],
                "exit_strategy": spec.exit_strategy,
                "spx_targets": {"t1": spec.spx_t1, "t2": spec.spx_t2,
                                "t3": spec.spx_t3, "stop": spec.spx_stop},
                "dollar_targets": {"t1": spec.d_t1, "t2": spec.d_t2,
                                   "t3": spec.d_t3, "stop": spec.d_stop},
                "reason": f"DRY_RUN — watcher will simulate {spec.exit_strategy} exits",
            }
        # Live watcher-managed: place ONLY the parent buy. Watcher handles
        # exits. The parent BUY is a marketable LMT that BOUNDED-CHASES the
        # moving ask (entry_chase.py): cancel/re-post at the CURRENT ask up
        # to N times, then ONE MKT fallback — so the fast momentum entries this
        # strategy generates fill at a MKT-like rate WITHOUT the 2026-07-16
        # naked-MKT hang. The current ask comes from a STREAMING reqMktData
        # subscription opened at fire-commit (2026-07-21 fix — memory reads,
        # no fire-time snapshot round-trips), with the legacy snapshot
        # re-fetch as the fail-soft fallback. use_market_entry (manual/legacy)
        # and the chase master-switch (FEF_ENTRY_CHASE_ENABLED=false) both
        # bypass the chase and fall back to the original single static post.
        try:
            from ib_async import LimitOrder, MarketOrder
            ib = ibkr._ib  # type: ignore[attr-defined]
            from fef_sidecar.ibkr.live import _to_ib_contract
            ib_contract = _to_ib_contract(contract)
            await ib.qualifyContractsAsync(ib_contract)

            def _mk_limit(q: int, px: float):
                o = LimitOrder("BUY", q, px, transmit=True)
                # Force auto-submit even when TWS API precautions are on.
                o.tif = "DAY"
                o.outsideRth = False
                o.transmit = True
                return o

            def _mk_market(q: int):
                o = MarketOrder("BUY", q, transmit=True)
                o.tif = "DAY"
                o.outsideRth = False
                o.transmit = True
                return o

            async def _sync_order(trade) -> None:
                # Keep the DB ib_order_id on the CURRENTLY-working order across
                # every chase re-post so a mid-chase crash + reconcile can find
                # it, and so fill-sync attributes fills to the right order.
                await SignalIterRepository.update_live_order(
                    parent_saved.id, ib_order_id=trade.order.orderId,
                    ib_perm_id=trade.order.permId,
                )

            from fef_sidecar.engine import entry_chase

            # A MarketOrder (the chase fallback) can hang on IBKR's price cap
            # (2026-07-16) unless the entry-timeout can cancel it. So the chase
            # MKT fallback is allowed ONLY when the timeout is enabled
            # (entry_timeout_sec > 0); with the timeout disabled we DOWNGRADE to
            # chase-only — a resting LIMIT never hangs. Net invariant: the chase
            # never places a MarketOrder without a live, positive-budget
            # entry-timeout watchdog on it.
            entry_tmo = spec.entry_timeout_sec
            mkt_watchdog_ok = bool(entry_tmo and entry_tmo > 0)
            did_chase = not spec.use_market_entry and entry_chase.chase_enabled()
            chase_mkt = (did_chase and mkt_watchdog_ok
                         and entry_chase.chase_mkt_fallback())
            if did_chase and entry_chase.chase_mkt_fallback() and not mkt_watchdog_ok:
                _warn_mkt_fallback_downgraded(entry_tmo)
            chase_outcome = "static_market" if spec.use_market_entry else "static_limit"
            reposts = 0
            final_limit: float | None = None if spec.use_market_entry else spec.entry_limit
            used_market = bool(spec.use_market_entry)
            timeout_budget: float | None = spec.entry_timeout_sec

            # STREAMING fire-time quote (2026-07-21 starvation fix): open a
            # push-style reqMktData subscription for the entry contract NOW —
            # BEFORE placing the order and before the shared connection gets
            # busy with placement/confirms — so the chase's re-post asks are
            # MEMORY reads off the live ticker, not request/response snapshots
            # (live 7/21: the 1.5s chase re-fetch starved at fire time while
            # TWS quotes were healthy on a free connection). NEVER blocks the
            # SPX placement: the contract is already qualified so the live
            # subscribe does zero awaited round-trips, and a hard wait_for
            # bound + fail-soft catch guarantee that ANY subscribe problem
            # just leaves entry_stream=None -> the chase uses the legacy
            # re-fetch path exactly as before this fix.
            entry_stream = None
            if did_chase:
                try:
                    _opener = getattr(ibkr, "open_option_quote_stream", None)
                    if _opener is not None:
                        entry_stream = await asyncio.wait_for(
                            _opener(contract, ib_contract=ib_contract),
                            timeout=2.0,
                        )
                except Exception:
                    entry_stream = None
                    logger.exception(
                        "entry quote stream subscribe failed for %s — chase "
                        "will use the legacy re-fetch path", spec.symbol)

            try:
                if not did_chase:
                    # Manual/legacy MKT entry, or chase disabled -> single static
                    # post (behaviour identical to before the chase).
                    parent_order = (_mk_market(spec.qty) if spec.use_market_entry
                                    else _mk_limit(spec.qty, spec.entry_limit))
                    parent_trade = ib.placeOrder(ib_contract, parent_order)
                    await _sync_order(parent_trade)
                else:
                    async def _refetch() -> float | None:
                        # A FRESH ask -> tick-rounded marketable limit, or None
                        # (no usable/fresh ask) -> the chase must NOT re-post
                        # stale. STREAM-FIRST (2026-07-21): a fresh streamed
                        # tick is a memory read (zero round-trip). A stream
                        # that TICKED but went stale returns None — the same
                        # no_fresh_quote refusal a failed re-fetch produces
                        # (never chase stale, never blind-MKT). A stream that
                        # NEVER ticked is a dead/slow subscription: close it
                        # and fall back to the legacy snapshot re-fetch below
                        # for the remainder of the chase.
                        nonlocal entry_stream
                        from fef_sidecar.engine.entry_pricing import marketable_entry_limit
                        if entry_stream is not None:
                            a = entry_stream.fresh_ask()
                            if a is not None:
                                return marketable_entry_limit(spec.symbol, float(a))
                            if entry_stream.ever_ticked:
                                return None  # stale stream -> refuse (no stale chase)
                            try:
                                entry_stream.close()
                            except Exception:
                                logger.exception("dead entry quote stream close failed")
                            entry_stream = None
                            logger.warning(
                                "entry quote stream never ticked for %s — "
                                "falling back to legacy re-fetch", spec.symbol)
                        try:
                            q = await asyncio.wait_for(
                                ibkr.get_option_quote(contract),
                                timeout=entry_chase.chase_refetch_timeout_sec(),
                            )
                        except Exception:
                            return None
                        a = getattr(q, "ask", None)
                        if isinstance(a, (int, float)) and math.isfinite(a) and a > 0:
                            return marketable_entry_limit(spec.symbol, float(a))
                        return None

                    t0 = time.monotonic()
                    monitor_floor = entry_chase.chase_monitor_floor_sec()
                    if mkt_watchdog_ok:
                        # RESERVE a monitor floor: cap the chase at
                        # (timeout - floor) so the budget handed to the entry-timeout
                        # (and therefore any fired MKT fallback) is ALWAYS >= floor.
                        chase_cap = max(0.5, min(entry_chase.chase_hard_budget_sec(),
                                                 float(entry_tmo) - monitor_floor))
                    else:
                        # Timeout disabled -> chase-only (no MKT); just bound the
                        # chase wall-time so it can't spin.
                        chase_cap = entry_chase.chase_hard_budget_sec()
                    deadline = t0 + chase_cap
                    cr = await entry_chase.place_entry_with_chase(
                        ib=ib, ib_contract=ib_contract, symbol=spec.symbol,
                        qty=spec.qty, initial_limit=spec.entry_limit,
                        make_limit_order=_mk_limit, make_market_order=_mk_market,
                        refetch_limit=_refetch, on_order_placed=_sync_order,
                        mkt_fallback=chase_mkt,
                        deadline_monotonic=deadline,
                    )
                    parent_trade = cr.trade
                    final_limit = cr.limit_price
                    used_market = cr.used_market
                    chase_outcome = cr.outcome
                    reposts = cr.reposts
                    # Remaining budget for the entry-timeout (chase counts against
                    # the SAME ceiling). If a MKT fallback fired, FLOOR the budget to
                    # the monitor floor so the market order can NEVER rest
                    # unmonitored — the structural guard against the 7/16 hang.
                    # (used_market here implies chase_mkt, which implies
                    # mkt_watchdog_ok, so entry_tmo > 0.)
                    elapsed = time.monotonic() - t0
                    if used_market:
                        timeout_budget = max(monitor_floor, float(entry_tmo) - elapsed)
                    elif mkt_watchdog_ok:
                        timeout_budget = max(0.0, float(entry_tmo) - elapsed)
                    else:
                        timeout_budget = spec.entry_timeout_sec  # <= 0 -> no-arm (chase-only)
                    # Reconcile the DB row with the order that ended up working (a
                    # re-post moved the limit; the MKT fallback changed the type).
                    # Re-stamp ib_order_id/perm here too so a silently-failed final
                    # _sync_order hook can't leave the DB on a superseded order.
                    await SignalIterRepository.update_live_order(
                        parent_saved.id,
                        ib_order_id=parent_trade.order.orderId,
                        ib_perm_id=parent_trade.order.permId,
                        order_type="MKT" if used_market else "LMT",
                        limit_price=final_limit,
                    )

                entry_desc = ("MARKET (chase fallback)" if (used_market and did_chase)
                              else "MARKET" if used_market
                              else f"LMT @ ${final_limit:.2f}")
                _chase_meta = {"outcome": chase_outcome, "reposts": reposts,
                               "used_market": used_market, "final_limit": final_limit}
                await SignalIterRepository.write_audit_log(
                    category="order", actor=audit_actor,
                    summary=f"{audit_prefix}PLACED {spec.exit_strategy} entry: BUY {spec.qty} {spec.symbol} {spec.strike}{spec.right} {entry_desc}. {_exits_desc}",
                    details={"parent_order_id": parent_saved.id, "exit_strategy": spec.exit_strategy,
                             "order_type": "MKT" if used_market else "LMT",
                             "entry_timeout_sec": spec.entry_timeout_sec,
                             "entry_chase": _chase_meta,
                             "source": source, "signal_ref": signal_ref},
                )
                _spawn_entry_timeout(
                    state=state, ibkr=ibkr, spec=spec,
                    parent_id=parent_saved.id,
                    ib_order_id=parent_trade.order.orderId,
                    parent_trade=parent_trade, source=source,
                    signal_ref=signal_ref, child_ids=(),
                    timeout_override=timeout_budget,
                    limit_override=(None if used_market else final_limit),
                )
                return {"ok": True, "dry_run": False, "parent_order_id": parent_saved.id,
                        "exit_strategy": spec.exit_strategy, "entry_chase": _chase_meta}
            finally:
                # UNSUBSCRIBE on EVERY resolution path — fill, cancel,
                # timeout/deadline, no_fresh_quote rest, MKT fallback AND any
                # exception (the outer except below still sees the original
                # error). close() is idempotent and never raises; the belt
                # try/except keeps a hostile duck-typed handle from masking
                # the placement result. Subscriptions are therefore strictly
                # chase-lived (seconds), 1-2 per fire.
                if entry_stream is not None:
                    try:
                        entry_stream.close()
                    except Exception:
                        logger.exception("entry quote stream close failed")
        except Exception as e:
            logger.exception("watcher-managed parent submission failed")
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

    entry_desc = "MARKET" if spec.use_market_entry else f"LMT @ ${spec.entry_limit:.2f}"
    await SignalIterRepository.write_audit_log(
        category="order", actor=audit_actor if confirm else "system",
        summary=f"{audit_prefix}{'DRY_RUN' if dry_run else 'PLACED'} bracket: BUY {spec.qty} {spec.symbol} {spec.strike}{spec.right} {entry_desc}",
        details={
            "parent_order_id": parent_saved.id, "children": [c.id for c in children_saved],
            "spec": {"qty": spec.qty, "t1": spec.t1_limit, "t2": spec.t2_limit,
                     "t3": spec.t3_limit, "stop": spec.stop_limit},
            "order_type": "MKT" if spec.use_market_entry else "LMT",
            "source": source, "signal_ref": signal_ref,
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
        from ib_async import LimitOrder, MarketOrder, StopOrder

        ib = ibkr._ib  # type: ignore[attr-defined]
        ib_contract = ibkr._to_ib_contract(contract) if hasattr(ibkr, "_to_ib_contract") else None
        if ib_contract is None:
            from fef_sidecar.ibkr.live import _to_ib_contract
            ib_contract = _to_ib_contract(contract)
        await ib.qualifyContractsAsync(ib_contract)

        # Honor use_market_entry on EVERY exit strategy path. Without this,
        # switching exit_strategy off spx_points reverted entries back to
        # LimitOrder and premium would walk away from the limit.
        if spec.use_market_entry:
            parent_order = MarketOrder("BUY", spec.qty, transmit=False)
        else:
            parent_order = LimitOrder("BUY", spec.qty, spec.entry_limit, transmit=False)
        parent_order.tif = "DAY"
        parent_order.outsideRth = False
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

        _spawn_entry_timeout(
            state=state, ibkr=ibkr, spec=spec,
            parent_id=parent_saved.id,
            ib_order_id=parent_trade.order.orderId,
            parent_trade=parent_trade, source=source,
            signal_ref=signal_ref,
            child_ids=tuple(c.id for c in children_saved if c.id is not None),
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
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    live = is_live_trading_enabled() and confirm and isinstance(ibkr, LiveIBKRProvider)
    dry_run = not live

    # SAFETY GATE: same paper-account check as place_bracket.
    if live:
        try:
            await assert_paper_if_required(ibkr)
        except RuntimeError as e:
            from fef_sidecar.engine.auto_trader import BlockReason as _BR_ors
            await SignalIterRepository.write_audit_log(
                category="order_refused", actor="system",
                summary=f"REFUSED SPREAD: {e}",
                details={"reason": str(e), "kind": spec.kind, "qty": spec.qty,
                         "short": spec.short_strike, "long": spec.long_strike},
                block_reason_code=_BR_ors.ORDER_REFUSED,
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

    # Live: build BAG with ComboLegs.
    #
    # CREDIT-SPREAD GATE: this is the ONE legitimate naked-short path in the
    # codebase. The short leg is uncovered until the long leg fills (typically
    # a few ms later via the combo's atomic-fill semantics, but still naked
    # in the worst case if a partial happens). We gate this behind an explicit
    # env var so it can't fire without user opt-in.
    gate = os.environ.get("FEF_ALLOW_CREDIT_SPREADS", "false").lower() in (
        "true", "1", "yes", "on",
    )
    if not gate:
        from fef_sidecar.engine.auto_trader import BlockReason as _BR_csg
        await SignalIterRepository.write_audit_log(
            category="safe_sell_blocked", actor="system",
            summary=(
                f"CREDIT_SPREAD_GATE: refusing to submit {spec.kind} "
                f"{spec.short_strike}/{spec.long_strike} × {spec.qty} — "
                "FEF_ALLOW_CREDIT_SPREADS env var is not set. Naked-short "
                "paths require explicit opt-in."
            ),
            details={
                "purpose": "credit_spread_short_leg", "qty": spec.qty,
                "parent_id": saved.id, "kind": spec.kind,
                "short": spec.short_strike, "long": spec.long_strike,
            },
            block_reason_code=_BR_csg.CREDIT_SPREAD_GATE_OFF,
        )
        await SignalIterRepository.update_live_order(
            saved.id, status="REJECTED",
            notes="FEF_ALLOW_CREDIT_SPREADS gate off",
        )
        return {
            "ok": False,
            "error": "FEF_ALLOW_CREDIT_SPREADS env var is not set",
            "refused_credit_spread_gate": True,
            "parent_order_id": saved.id,
        }

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

        # SELL the bag for net credit. This is the gated naked-short path —
        # the env-var check above guarantees the user has opted in. The bag
        # itself is a combo, not a single option, so safe_market_sell's
        # net_long check doesn't apply; the gate is the sole structural
        # protection on this path.
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
    """Cancel every working IBKR order AND market-close every open option
    position for this account.

    With market-entry auto-trade, there are rarely "working orders" sitting
    at the exchange — the parent BUY fills immediately, and watcher exits
    are computed on-the-fly. So "Kill All Orders" without the position-close
    leg is mostly a no-op. Most users hit this expecting their exposure to
    go to zero. So this function does BOTH:
      1. Cancel all working orders (reqGlobalCancel + per-trade cancel)
      2. Market-sell every open OPT position to flatten exposure

    Audit-logged. Idempotent. Auto-trade + scanner are NOT disabled —
    use /panic/flat for the full kill-switch (cancel + close + disable).
    """
    from fef_sidecar.ibkr.live import LiveIBKRProvider

    if not isinstance(ibkr, LiveIBKRProvider):
        await SignalIterRepository.write_audit_log(
            category="kill_switch", actor="user",
            summary="Kill switch invoked in MOCK mode (no-op)",
        )
        return {"ok": True, "mock": True, "cancelled": 0}

    try:
        ib = ibkr._ib  # type: ignore[attr-defined]
        if ib is None or not ib.isConnected():
            return {"ok": False, "error": "Not connected"}

        # Refresh from IBKR — pulls orders submitted by ANY client_id under
        # this account so we can cancel them even after a sidecar restart.
        try:
            await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), timeout=5.0)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("reqAllOpenOrdersAsync timed out; using cached openTrades")
        except Exception:
            logger.exception("reqAllOpenOrdersAsync failed; using cached openTrades")

        cancelled = 0
        attempted = 0
        for trade in ib.openTrades():
            attempted += 1
            try:
                ib.cancelOrder(trade.order)
                cancelled += 1
            except Exception:
                logger.exception("Failed to cancel order %s", trade.order.orderId)

        # Backstop: ask IBKR to globally cancel everything bound to this
        # client_id. Idempotent with the per-trade cancels above.
        try:
            ib.reqGlobalCancel()
        except Exception:
            logger.exception("reqGlobalCancel failed")

    except Exception as e:
        await SignalIterRepository.write_audit_log(
            category="kill_switch", actor="user",
            summary=f"Kill switch ERROR: {e}",
        )
        return {"ok": False, "error": str(e)}

    # Now also flatten any open OPT positions (the part most users actually
    # want when they hit "Kill All Orders").
    #
    # CRITICAL: `pos.contract` from `ib.positions()` is missing routing
    # fields (no exchange, no conId-driven SMART routing). Passing it
    # directly to placeOrder makes IBKR silently reject the order — the
    # client assigns an ib_order_id but the order NEVER reaches the
    # matching engine. We must rebuild a proper Contract object and
    # qualify it before placing. Mirror what spx_target_watcher's working
    # market-sell path does.
    closed_positions: list[dict] = []
    try:
        from ib_async import MarketOrder
        from fef_sidecar.ibkr.live import _to_ib_contract
        from fef_sidecar.ibkr.models import Contract, ContractKind

        from fef_sidecar.ibkr.safe_sell import safe_market_sell
        # kill_all_orders is invoked from /panic/flat and /live/kill which
        # have access to state via the request; but kill_all_orders itself
        # is called with just `ibkr`. Synthesize a minimal stand-in object
        # that carries the firing_contracts dict so safe_market_sell can
        # write its audit_log. (audit_log writes don't actually need state.)
        _state_stub = type("KillStateStub", (), {"firing_contracts": {}})()

        for pos in ib.positions():
            c = pos.contract
            if getattr(c, "secType", "") != "OPT":
                continue
            qty = int(pos.position)
            if abs(qty) < 1:
                continue
            action = "SELL" if qty > 0 else "BUY"
            sym = getattr(c, "symbol", "")
            strike = float(getattr(c, "strike", 0))
            right = getattr(c, "right", "")
            expiry = getattr(c, "lastTradeDateOrContractMonth", "")
            try:
                # Rebuild + qualify so IBKR actually accepts the order
                opt_contract = Contract(
                    symbol=sym, kind=ContractKind.OPTION,
                    exchange="CBOE" if sym == "SPX" else "SMART",
                    expiry=expiry, strike=strike, right=right,
                )
                ic = _to_ib_contract(opt_contract)
                await ib.qualifyContractsAsync(ic)
                if action == "SELL":
                    result = await safe_market_sell(
                        ib, ic, abs(qty),
                        purpose="kill_switch", state=_state_stub,
                    )
                    if result.placed:
                        closed_positions.append({
                            "contract": f"{sym} {strike}{right}",
                            "qty": result.sell_qty, "action": "SELL",
                            "ib_order_id": result.trade.order.orderId,
                        })
                    else:
                        closed_positions.append({
                            "contract": f"{sym} {strike}{right}",
                            "skipped": True, "reason": result.reason,
                        })
                else:
                    # BUY-to-close (short → flat) is not a naked-short path.
                    mkt = MarketOrder("BUY", abs(qty), transmit=True)
                    trade = ib.placeOrder(ic, mkt)
                    closed_positions.append({
                        "contract": f"{sym} {strike}{right}",
                        "qty": abs(qty), "action": "BUY",
                        "ib_order_id": trade.order.orderId,
                    })
            except Exception as e:
                closed_positions.append({
                    "contract": f"{sym} {strike}{right}",
                    "error": str(e),
                })
    except Exception:
        logger.exception("kill_all_orders position-close leg failed")

    await SignalIterRepository.write_audit_log(
        category="kill_switch", actor="user",
        summary=(
            f"Kill switch — cancelled {cancelled}/{attempted} working orders + "
            f"market-closed {len(closed_positions)} open positions"
        ),
        details={
            "cancelled_count": cancelled, "attempted_count": attempted,
            "closed_positions": closed_positions,
        },
    )
    return {
        "ok": True,
        "cancelled": cancelled,
        "attempted": attempted,
        "positions_closed": len(closed_positions),
        "positions_closed_detail": closed_positions,
    }
