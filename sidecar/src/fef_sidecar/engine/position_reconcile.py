"""Position reconciliation — cross-references IBKR open positions against
our LiveOrder parent rows. Detects:

  ORPHAN: IBKR has a position with no matching parent in our DB.
          Watcher cannot manage it (no targets, no BE ladder, no stop).
          Audit-logged with severity HIGH so the user can sweep manually.

  PHANTOM: Our DB has an open parent (SUBMITTED/PARTIAL) but IBKR no
           longer shows the position. Could mean: user manually closed
           in TWS, IBKR rejected it, position expired worthless.
           Watcher would be polling for a position that no longer
           exists. We mark the parent CLOSED + audit so it stops
           being checked.

  MATCHED: Both sides agree — normal resumption. Watcher picks up on
           next iter (within 0.5s).

Idempotent. Runs on sidecar startup after IBKR connects. Also exposed
via POST /admin/reconcile_positions for manual rerun.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from datetime import UTC, datetime
from typing import Any

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import LiveOrder
from fef_sidecar.db.repository import SignalIterRepository

logger = logging.getLogger(__name__)

# IBKR order statuses that mean an order is NO LONGER working at the exchange.
_TERMINAL_IB_STATUSES = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


def _position_key(symbol: str, strike: float, right: str, expiry: str) -> str:
    return f"{symbol}|{round(float(strike), 2)}|{right}|{expiry}"


# Mid-session rearm/re-adoption safety window (re-review R2): a parent whose
# row changed within this window may be inside a live sell's fill-propagation
# window — a re-adopt/re-arm there re-sells the full qty against a stale
# positions cache. Boot reconcile is unaffected (rows from a previous session
# are old). Env-tunable; 0 disables the guard.
_REARM_RECENCY_SEC = float(os.environ.get("FEF_RECONCILE_REARM_RECENCY_SEC", "120"))


def _rearm_blocked(state, db_p) -> str | None:
    """Reason to skip MUTATING this parent on this reconcile pass, or None.
    Two gates (re-review R2): an in-flight exit fire owns the parent, or its
    row was updated too recently to trust the broker snapshot against it
    (a just-placed stop's fill takes seconds to reach ib.positions(); the
    2s pending reservation and the firing lock have both released by the
    time a reconcile pass can observe the stale long)."""
    if db_p.id in (getattr(state, "firing_parent_ids", None) or set()):
        return "exit fire in flight"
    ts = getattr(db_p, "last_status_at", None)
    if ts is not None and _REARM_RECENCY_SEC > 0:
        try:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)  # DB round-trips drop tzinfo; stored UTC
            age = (datetime.now(UTC) - ts).total_seconds()
            if age < _REARM_RECENCY_SEC:
                return f"row updated {age:.0f}s ago"
        except Exception:
            pass
    return None


async def _active_exit_order_keys(ib) -> set[str] | None:
    """Contract keys that currently have a WORKING SELL (exit) order at IBKR.

    Returns ``None`` when we can't determine it (timeout / error) so callers can
    fail SAFE — assume the position IS protected and skip the destructive re-arm.

    A position whose key is in this set already has live downside protection (a
    bracket stop and/or profit targets working at the exchange), so reconcile must
    NOT re-arm it. Re-arming protected positions was clobbering healthy fixed_dollar
    IBKR brackets into a generic spx_points 15pt stop — the 2026-06-30 7490P
    incident this fix addresses.
    """
    try:
        await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), timeout=5.0)
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "reqAllOpenOrdersAsync timed out during reconcile — failing SAFE "
            "(treat all positions as protected, skip re-arm this cycle)"
        )
        return None
    except Exception:
        logger.exception("reqAllOpenOrdersAsync failed during reconcile")
        return None
    try:
        keys: set[str] = set()
        for trade in ib.openTrades():
            order = getattr(trade, "order", None)
            status = getattr(getattr(trade, "orderStatus", None), "status", "") or ""
            if order is None or getattr(order, "action", "") != "SELL":
                continue
            if status in _TERMINAL_IB_STATUSES:
                continue
            c = getattr(trade, "contract", None)
            if c is None or getattr(c, "secType", "") != "OPT":
                continue
            keys.add(_position_key(
                getattr(c, "symbol", ""), float(getattr(c, "strike", 0)),
                getattr(c, "right", ""), getattr(c, "lastTradeDateOrContractMonth", ""),
            ))
        return keys
    except Exception:
        logger.exception("Failed to enumerate open exit orders during reconcile")
        return None


# How long after an entry-timeout CANCELLED mark a same-contract IBKR orphan
# is still attributed to the 'cancelled but filled' race (and re-adopted)
# rather than treated as an untracked manual trade. The race fill surfaces
# within one reconcile interval (300s); 900s is generous.
_CANCELLED_RESURRECT_WINDOW_SEC = float(
    os.environ.get("FEF_CANCELLED_RESURRECT_WINDOW_SEC", "900")
)


# A SUBMITTED zero-fill parent whose entry BUY is still WORKING at IBKR and
# older than this is a restart-orphaned entry (the in-process 45s timeout
# monitor died with the process) — reconcile cancels it and stands it down.
# Younger working entries are inside the monitor's window and are SKIPPED
# (previously the phantom branch would have marked them CLOSED while the
# order still worked — harmless with instant MKT fills, a real hazard with
# resting LMT entries).
_ENTRY_SWEEP_AGE_SEC = float(os.environ.get("FEF_ENTRY_SWEEP_AGE_SEC", "180"))


def _working_buy_trades(ib) -> dict[int, Any]:
    """orderId -> Trade for every non-terminal BUY order in the (already
    refreshed) openTrades cache. Relies on _active_exit_order_keys' earlier
    reqAllOpenOrdersAsync; on any error returns {} (callers then fall through
    to today's phantom behavior — no worse than pre-2026-07-16)."""
    out: dict[int, Any] = {}
    try:
        for trade in ib.openTrades():
            order = getattr(trade, "order", None)
            status = getattr(getattr(trade, "orderStatus", None), "status", "") or ""
            if order is None or getattr(order, "action", "") != "BUY":
                continue
            if status in _TERMINAL_IB_STATUSES:
                continue
            oid = getattr(order, "orderId", None)
            if oid is not None:
                out[int(oid)] = trade
    except Exception:
        logger.exception("Failed to enumerate working BUY orders during reconcile")
    return out


def _row_age_sec(dt) -> float | None:
    if dt is None:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)  # DB round-trips drop tzinfo; stored UTC
        return (datetime.now(UTC) - dt).total_seconds()
    except Exception:
        return None


def _cancelled_entry_race_candidate(p) -> bool:
    """True iff a CANCELLED parent is the entry-timeout kind (tagged notes)
    AND recent enough that an orphan on its contract is plausibly the late
    fill that raced the cancel — not the user's own later manual trade."""
    if "entry_timeout" not in (p.notes or ""):
        return False
    ts = getattr(p, "last_status_at", None)
    if ts is None:
        return False
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)  # DB round-trips drop tzinfo; stored UTC
        return (datetime.now(UTC) - ts).total_seconds() <= _CANCELLED_RESURRECT_WINDOW_SEC
    except Exception:
        return False


def _resurrectable_for_orphan(key, parent_keys, resurrectable, spot,
                              orphan_qty: int | None = None):
    """Which resolved parent (if any) to RE-ADOPT for an orphaned IBKR position.

    Safety policy — re-adopt ONLY when all hold, else leave it a warned orphan:
      * the position has NO open parent (genuinely orphaned), AND
      * a recently-resolved (CLOSED/STOPPED) parent for the SAME contract exists
        — i.e. it's our own wrongly-closed bracket, not an untracked manual trade
        we shouldn't auto-manage, AND
      * we have a live spot to anchor the emergency stop, AND
      * for entry-timeout CANCELLED parents only: the orphan is no LARGER than
        the bot's own cancelled order (review MAJOR-2). A 'cancelled but
        filled' race fill can never exceed the bot's order size — an orphan
        bigger than that is the USER'S manual position on a shared strike
        (their documented 100-200-lot habit), which the bot must never adopt
        into its sell ladder.
    Returns the parent to reopen, or None.
    """
    if key in parent_keys or not spot:
        return None
    resur = resurrectable.get(key)
    if (resur is not None
            and getattr(resur, "status", None) == "CANCELLED"
            and orphan_qty is not None
            and int(orphan_qty) > int(getattr(resur, "qty", 0) or 0)):
        return None
    return resur


# Py3.12 nuance: an UNCONTENDED acquire never binds an event loop, which is
# why per-test event loops tolerate this module-level lock; the first
# CONTENDED acquire binds one permanently (cross-loop contention then raises
# RuntimeError — loud, not corrupting). Production is single-loop; keep it
# module-level so all four entrants (boot, periodic, manual POST, watcher
# fill-grace kick) share one serialization point.
_RECONCILE_LOCK = asyncio.Lock()


async def reconcile_positions(state) -> dict[str, Any]:
    """Walk IBKR positions ↔ LiveOrder parents. Return counts + lists.

    Serialized under a module lock (2026-07-27 review MAJOR-2): four entrants
    exist — boot pass, 300s periodic loop, manual POST /admin, and the
    watcher's fill-grace kick — and an overlapped double-run's re-adoption
    interleave could re-clear tier latches / pop the lifetime-sold oversell
    cap for a parent the watcher just acted on. The lock costs nothing in the
    common case and removes the class."""
    async with _RECONCILE_LOCK:
        return await _reconcile_positions_locked(state)


async def _reconcile_positions_locked(state) -> dict[str, Any]:
    from fef_sidecar.ibkr.live import LiveIBKRProvider

    if not isinstance(state.ibkr, LiveIBKRProvider):
        return {"mode": "mock", "matched": 0, "orphans": [], "phantoms": []}

    ib = getattr(state.ibkr, "_ib", None)
    if ib is None or not ib.isConnected():
        return {"mode": "disconnected", "matched": 0, "orphans": [], "phantoms": []}

    # Pull IBKR positions
    ib_positions: dict[str, dict] = {}
    for p in ib.positions():
        c = p.contract
        if getattr(c, "secType", "") != "OPT":
            continue
        qty = int(p.position)
        if abs(qty) < 1:
            continue
        key = _position_key(
            getattr(c, "symbol", ""), float(getattr(c, "strike", 0)),
            getattr(c, "right", ""), getattr(c, "lastTradeDateOrContractMonth", ""),
        )
        ib_positions[key] = {
            "symbol": c.symbol, "strike": float(c.strike), "right": c.right,
            "expiry": c.lastTradeDateOrContractMonth, "qty": qty,
            "avg_cost": float(p.avgCost) / 100.0 if p.avgCost else None,
        }

    # Pull our DB's open parent orders
    async with get_session() as session:
        open_parents = list((await session.exec(
            select(LiveOrder)
            .where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.status.in_(["SUBMITTED", "PARTIAL", "FILLED"]))  # type: ignore[attr-defined]
            .where(LiveOrder.dry_run == False)  # noqa: E712
        )).all())

    parent_keys = {
        _position_key(p.symbol, p.strike, p.right, p.expiry): p
        for p in open_parents
    }

    # Also pull recently-RESOLVED parents (CLOSED / STOPPED) so an orphaned IBKR
    # position whose parent was wrongly marked resolved can be RE-ADOPTED into
    # management rather than left naked — the safety net for orphaning bugs like
    # the 2026-07-01 7520C phantom-fill (a tier flagged fired without selling →
    # parent CLOSED → runner dropped by every watcher). Most-recent per contract.
    # CANCELLED added 2026-07-16: the entry-timeout watchdog marks a zero-fill
    # entry CANCELLED after its post-cancel read — if a fill RACED that cancel
    # and landed after the read, the position surfaces here as an orphan and
    # this path re-adopts the CANCELLED parent (the designed backstop for the
    # 'cancelled but filled' race; see engine/entry_timeout.py).
    async with get_session() as session:
        resolved_parents = list((await session.exec(
            select(LiveOrder)
            .where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.dry_run == False)  # noqa: E712
            .where(LiveOrder.status.in_(  # type: ignore[attr-defined]
                ["CLOSED", "STOPPED", "STUCK", "CLOSED_EXTERNAL", "CANCELLED"]
            ))
            .order_by(LiveOrder.id.desc())  # type: ignore[attr-defined]
            .limit(100)
        )).all())
    resurrectable: dict[tuple, LiveOrder] = {}
    for p in resolved_parents:
        if p.status == "CANCELLED" and not _cancelled_entry_race_candidate(p):
            # CANCELLED is resurrectable ONLY for the entry-timeout race
            # (tagged notes, recent) — a stale CANCELLED entry must never
            # let reconcile adopt the USER'S later manual trade on the same
            # strike (shared strikes are common; see CLAUDE.md attribution).
            continue
        k = _position_key(p.symbol, p.strike, p.right, p.expiry)
        if k not in parent_keys and k not in resurrectable:
            resurrectable[k] = p

    matched: list[dict] = []
    orphans: list[dict] = []
    readopted: list[dict] = []
    phantoms: list[dict] = []

    # Matched: in both. Re-arm is now STRATEGY-AWARE — the old code re-armed any
    # matched parent whose dynamic_stop_level was None, which clobbered HEALTHY
    # fixed_dollar/IBKR-bracket positions into a generic spx_points 15pt stop (the
    # 2026-06-30 7490P incident). Correct behavior:
    #   * watcher-managed (spx_points / fixed_dollar): the watcher resumes from this
    #     DB row automatically, so the position is already protected as long as it
    #     carries the fields the watcher needs (a stop and/or targets). Only set an
    #     EMERGENCY backstop if it is truly unconfigured (no stop AND no targets).
    #   * option_price (IBKR-bracket-managed): protected iff a working SELL exit order
    #     exists at IBKR. Only if the bracket is GONE do we add an emergency backstop —
    #     and we PRESERVE the original exit_strategy in notes (not silently lost).
    #   * spread_premium: left entirely to the spread_exit_watcher.
    # active_exit_keys is None when we couldn't query IBKR → fail SAFE (treat as
    # protected, never re-arm). safe_market_sell clamps to the live position, so an
    # emergency backstop can never double-sell even if a stale order is still live.
    rearmed_count = 0
    protected_count = 0
    active_exit_keys = await _active_exit_order_keys(ib)
    spot = getattr(state, "last_live_tick_spx", None) or getattr(state, "last_spx_price", None)

    def _emergency_backstop_updates(db_p, spot) -> dict[str, Any]:
        """A WIDE last-resort spx_points stop + staged targets from the current
        spot. Only used when a position has no other protection."""
        is_call = db_p.right == "C"
        R = 15.0
        entry_ref = db_p.entry_spx_price or float(spot)
        sgn = 1.0 if is_call else -1.0
        stop = entry_ref - sgn * R
        return dict(
            exit_strategy="spx_points",
            entry_spx_price=entry_ref,
            spx_target_stop=stop,
            dynamic_stop_level=stop,
            spx_target_t1=entry_ref + sgn * 1.5 * R,
            spx_target_t2=entry_ref + sgn * 2.5 * R,
            spx_target_t3=entry_ref + sgn * 5.0 * R,
        )

    for key, ib_p in ib_positions.items():
        if key not in parent_keys:
            continue
        db_p = parent_keys[key]
        blocked = _rearm_blocked(state, db_p)
        if blocked:
            # Mid-session pass only — never mutate a parent an exit fire owns
            # or whose row just changed; the next pass (~5 min) retries.
            logger.info("reconcile: skipping matched parent %s this pass (%s)",
                        db_p.id, blocked)
            continue
        strat = db_p.exit_strategy or "option_price"
        updates: dict[str, Any] = {}
        if db_p.status != "FILLED":
            updates["status"] = "FILLED"  # IBKR shows the position; sync stale fill
        rearmed = False
        protected = False

        if strat == "spread_premium":
            # Managed by the spread_exit_watcher — never re-arm here.
            protected = True
        elif strat in ("spx_points", "fixed_dollar"):
            # Watcher-managed: it resumes from this row. Armed iff it has a stop
            # and/or any target to act on.
            watcher_armed = (
                db_p.dynamic_stop_level is not None
                or db_p.spx_target_stop is not None
                or db_p.dollar_target_stop is not None
                or any(t is not None for t in (
                    db_p.spx_target_t1, db_p.spx_target_t2, db_p.spx_target_t3,
                    db_p.dollar_target_t1, db_p.dollar_target_t2, db_p.dollar_target_t3,
                ))
            )
            if watcher_armed:
                protected = True
            elif spot:
                updates.update(_emergency_backstop_updates(db_p, spot))
                rearmed = True
                rearmed_count += 1
        else:  # option_price — IBKR-bracket-managed
            has_active_exit = (active_exit_keys is None) or (key in active_exit_keys)
            if has_active_exit:
                protected = True
            elif spot:
                # Bracket is gone — add an emergency backstop, preserving the
                # original strategy in notes so the conversion is recoverable.
                updates.update(_emergency_backstop_updates(db_p, spot))
                prev_notes = (db_p.notes or "").strip()
                tag = f"emergency_rearm: orig_exit_strategy={strat} (IBKR bracket was gone)"
                updates["notes"] = f"{prev_notes} | {tag}" if prev_notes else tag
                rearmed = True
                rearmed_count += 1

        if protected:
            protected_count += 1
        if updates:
            try:
                await SignalIterRepository.update_live_order(db_p.id, **updates)
            except Exception:
                logger.exception("Failed to re-arm matched parent %s", db_p.id)
        matched.append({
            "contract": f"{ib_p['symbol']} {ib_p['strike']}{ib_p['right']}",
            "ib_qty": ib_p["qty"],
            "db_qty": db_p.qty,
            "db_status": db_p.status,
            "exit_strategy": strat,
            "dynamic_stop_level": updates.get("dynamic_stop_level", db_p.dynamic_stop_level),
            "be_ladder_max_step_R": db_p.be_ladder_max_step_R,
            "parent_id": db_p.id,
            "rearmed": rearmed,
            "protected": protected,
        })

    # Orphans: IBKR has an open position, no matching OPEN parent. First TRY to
    # RE-ADOPT it (reopen our own wrongly-resolved parent with an emergency stop),
    # so a novel orphaning bug can never leave a position naked. Only fall back to
    # the warn-only path for positions we can't safely resurrect (e.g. untracked
    # manual trades, or no spot to anchor a stop).
    for key, ib_p in ib_positions.items():
        if key in parent_keys:
            continue
        resur = _resurrectable_for_orphan(
            key, parent_keys, resurrectable, spot, orphan_qty=int(ib_p["qty"]),
        )
        if resur is not None and int(ib_p["qty"]) <= 0:
            resur = None  # short remnant — never re-adopt into a long-exit ladder
        if resur is not None and (blocked := _rearm_blocked(state, resur)):
            # Re-review R2: the resurrectable parent may be mid-fire or just
            # resolved by a sell whose fill hasn't reached the positions
            # cache — re-adopting inside that window re-sells the full qty.
            # Defer the contract entirely this pass (no orphan warning yet).
            logger.info("reconcile: deferring re-adoption of parent %s (%s)",
                        resur.id, blocked)
            continue
        if resur is not None:
            # Reopen FILLED with a fresh emergency spx_points stop + targets and
            # CLEAR the (possibly corrupted) tier-fired flags so the watcher
            # manages the still-open qty. Original strategy preserved in notes.
            updates = _emergency_backstop_updates(resur, spot)
            updates["status"] = "FILLED"
            # Re-review R1: rebase the managed quantity to the BROKER REMNANT —
            # the lifetime sell cap derives from filled_qty/qty, and re-arming
            # the ORIGINAL size on a partial remnant lets a fresh tier ladder
            # sell more than exists once the positions cache goes stale
            # (banked 2 of 3, remnant 1, cap 3 -> T1 sells 1, T2 sells 1 more
            # = naked short). Parking guarantees no placed sell for >=90s, so
            # this pass's broker read is settled.
            updates["qty"] = int(ib_p["qty"])
            updates["filled_qty"] = int(ib_p["qty"])
            updates["spx_target_t1_fired"] = False
            updates["spx_target_t2_fired"] = False
            updates["spx_target_t3_fired"] = False
            prev_notes = (resur.notes or "").strip()
            tag = (f"readopted_orphan: was {resur.status}, {ib_p['qty']}ct still open "
                   f"at IBKR — reopened + emergency stop (orig_exit={resur.exit_strategy})")
            updates["notes"] = f"{prev_notes} | {tag}" if prev_notes else tag
            try:
                await SignalIterRepository.update_live_order(resur.id, **updates)
                # A re-adopted parent starts a NEW management life — stale
                # in-memory bookkeeping from its previous one (lifetime-sold
                # counter, refusal streak/backoff, re-anchored-plan memo that
                # would clobber the fresh emergency levels on stale-copy
                # re-apply) must not carry over.
                _reset_watcher_runtime_state(state, resur.id)
                # Review MINOR-4: an entry-timeout CANCELLED parent released
                # its per-symbol session-cap slot at stand-down. If the fill
                # raced the cancel and we are re-adopting it, a position DOES
                # exist — re-consume the slot so the symbol can't run
                # max_per_symbol + 1 open positions.
                budget_reconsumed = False
                if resur.status == "CANCELLED" and "entry_timeout" in (resur.notes or ""):
                    try:
                        by_sym = getattr(state, "auto_trade_count_by_symbol", None)
                        if isinstance(by_sym, dict):
                            by_sym[resur.symbol] = int(by_sym.get(resur.symbol, 0)) + 1
                        total = getattr(state, "auto_trade_count_session", None)
                        if isinstance(total, int):
                            state.auto_trade_count_session = total + 1
                        budget_reconsumed = True
                    except Exception:
                        logger.exception(
                            "budget re-consume failed for re-adopted parent %s",
                            resur.id,
                        )
                rearmed_count += 1
                readopted.append({
                    "contract": f"{ib_p['symbol']} {ib_p['strike']}{ib_p['right']}",
                    "qty": ib_p["qty"],
                    "remnant_rebased": True,
                    "parent_id": resur.id,
                    "was_status": resur.status,
                    "emergency_stop": updates.get("dynamic_stop_level"),
                    "budget_reconsumed": budget_reconsumed,
                })
                continue
            except Exception:
                logger.exception("Failed to re-adopt orphan parent %s", resur.id)
        orphans.append({
            "contract": f"{ib_p['symbol']} {ib_p['strike']}{ib_p['right']}",
            "qty": ib_p["qty"],
            "avg_cost": ib_p["avg_cost"],
            "key": key,
        })

    # Parked parents (STUCK — re-review N4): a STUCK parent whose position
    # is confirmed GONE by a fresh broker read resolves to CLOSED_EXTERNAL,
    # so the manual-intervention flag clears itself once broker truth firms
    # up (TWS back, positions repopulated). STUCK parents whose contract is
    # still open are handled by the re-adoption pass above.
    stuck_resolved: list[dict] = []
    async with get_session() as session:
        stuck_parents = list((await session.exec(
            select(LiveOrder)
            .where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.dry_run == False)  # noqa: E712
            .where(LiveOrder.status == "STUCK")
        )).all())
    for db_p in stuck_parents:
        key = _position_key(db_p.symbol, db_p.strike, db_p.right, db_p.expiry)
        if key in ib_positions:
            continue  # still open — re-adoption above owns it
        from fef_sidecar.engine.spx_target_watcher import _broker_long_qty_fresh
        if await _broker_long_qty_fresh(state, db_p) != 0:
            continue  # not CONFIRMED flat — stay STUCK
        try:
            await SignalIterRepository.update_live_order(
                db_p.id, status="CLOSED_EXTERNAL",
                notes=(f"{(db_p.notes or '').strip()} | STUCK resolved on reconcile: "
                       f"fresh broker read confirms flat").strip(" |"),
            )
            stuck_resolved.append({
                "contract": f"{db_p.symbol} {db_p.strike}{db_p.right}",
                "parent_id": db_p.id,
            })
        except Exception:
            logger.exception("Failed to resolve STUCK parent %s", db_p.id)

    # Phantoms: we have, IBKR doesn't. A parent whose entry BUY is still
    # WORKING at the broker is NOT a phantom (2026-07-16): with resting LMT
    # entries, an unfilled SUBMITTED parent legitimately has no position yet.
    # Fresh ones are the in-process entry-timeout monitor's job (skip);
    # stale ones are restart-orphaned working orders (the monitor died with
    # the process) — cancel at the broker + stand the row down CANCELLED,
    # mirroring the monitor's own zero-fill path (no budget release: the
    # in-memory counters did not survive the restart either).
    working_buys = _working_buy_trades(ib)
    swept_entries: list[dict] = []
    for key, db_p in parent_keys.items():
        if key in ib_positions:
            continue
        wt = working_buys.get(int(db_p.ib_order_id)) if db_p.ib_order_id is not None else None
        if wt is not None:
            age = _row_age_sec(getattr(db_p, "submitted_at", None))
            if age is not None and age <= _ENTRY_SWEEP_AGE_SEC:
                continue  # inside the entry window — monitor owns it
            try:
                ib.cancelOrder(wt.order)
                await SignalIterRepository.update_live_order(
                    db_p.id, status="CANCELLED",
                    notes=(f"{(db_p.notes or '').strip()} | entry_timeout: working "
                           f"entry swept by reconcile (age {age if age is not None else '?'}s "
                           f"> {_ENTRY_SWEEP_AGE_SEC:.0f}s; monitor lost, likely restart)"
                           ).strip(" |"),
                )
                swept_entries.append({
                    "contract": f"{db_p.symbol} {db_p.strike}{db_p.right}",
                    "parent_id": db_p.id,
                    "ib_order_id": db_p.ib_order_id,
                    "age_sec": age,
                })
            except Exception:
                logger.exception("Failed to sweep working entry for parent %s", db_p.id)
            continue
        phantoms.append({
            "contract": f"{db_p.symbol} {db_p.strike}{db_p.right}",
            "parent_id": db_p.id,
            "db_status": db_p.status,
            "submitted_at": db_p.submitted_at.isoformat(),
        })
        # Mark phantom as CLOSED so the watcher stops polling it
        try:
            await SignalIterRepository.update_live_order(
                db_p.id, status="CLOSED",
                notes=f"AUTO-CLOSED on reconciliation — IBKR no longer shows this position",
            )
        except Exception:
            logger.exception("Failed to update phantom %s", db_p.id)

    # Audit each finding so the user has a permanent record
    if matched:
        await SignalIterRepository.write_audit_log(
            category="position_reconcile", actor="system",
            summary=(
                f"Resumed management of {len(matched)} existing positions "
                f"({protected_count} already protected, left untouched)"
                + (f" — RE-ARMED an emergency stop on {rearmed_count} that had no "
                   f"active protection (orphaned bracket / unconfigured watcher)"
                   if rearmed_count else "")
                + ("" if active_exit_keys is not None else
                   " — NOTE: could not query IBKR open orders, treated all as "
                   "protected (failed safe)")
            ),
            details={"matched": matched, "exit_order_query_ok": active_exit_keys is not None},
        )
    if readopted:
        await SignalIterRepository.write_audit_log(
            category="position_reconcile", actor="system",
            summary=(
                f"RE-ADOPTED {len(readopted)} orphaned position(s) whose parent was "
                f"wrongly resolved — reopened FILLED with an emergency stop so the "
                f"watcher manages them again instead of leaving them naked."
            ),
            details={"readopted": readopted},
        )
    if orphans:
        await SignalIterRepository.write_audit_log(
            category="position_reconcile", actor="system",
            summary=(
                f"⚠ ORPHAN POSITIONS DETECTED: {len(orphans)} positions in IBKR "
                f"have no matching parent in our DB (and no resolved parent to "
                f"re-adopt — likely untracked/manual). The bot WILL NOT manage "
                f"their exits. Close manually in TWS or call /admin/sweep_orphans."
            ),
            details={"orphans": orphans},
        )
    if phantoms:
        await SignalIterRepository.write_audit_log(
            category="position_reconcile", actor="system",
            summary=(
                f"{len(phantoms)} phantom parents reconciled — DB said open, "
                f"IBKR says no position. Marked CLOSED."
            ),
            details={"phantoms": phantoms},
        )
    if swept_entries:
        await SignalIterRepository.write_audit_log(
            category="entry_timeout", actor="system",
            summary=(
                f"Swept {len(swept_entries)} restart-orphaned working entry "
                f"order(s): unfilled BUY LMT older than "
                f"{_ENTRY_SWEEP_AGE_SEC:.0f}s with no in-process timeout "
                f"monitor — cancelled at the broker, parent marked CANCELLED "
                f"(fire stood down). A fill racing this cancel is re-adopted "
                f"on the next reconcile pass."
            ),
            details={"swept_entries": swept_entries},
        )
    if stuck_resolved:
        await SignalIterRepository.write_audit_log(
            category="position_reconcile", actor="system",
            summary=(
                f"Resolved {len(stuck_resolved)} STUCK parent(s) to "
                f"CLOSED_EXTERNAL — a fresh broker read confirms the "
                f"position is flat."
            ),
            details={"stuck_resolved": stuck_resolved},
        )

    logger.info(
        "Position reconcile: %d matched, %d re-adopted, %d orphans, %d phantoms",
        len(matched), len(readopted), len(orphans), len(phantoms),
    )
    return {
        "mode": "live",
        "matched": len(matched),
        "readopted_count": len(readopted),
        "orphans_count": len(orphans),
        "phantoms_count": len(phantoms),
        "swept_entries_count": len(swept_entries),
        "matched_detail": matched,
        "readopted_detail": readopted,
        "orphans_detail": orphans,
        "phantoms_detail": phantoms,
        "swept_entries_detail": swept_entries,
    }


async def _sweep_stale_firing_locks(
    state, seen: dict, *, max_age_sec: float | None = None,
) -> list[int]:
    """Force-clear firing_parent_ids entries held continuously for ~10x the
    fire deadline (re-review R3): a fire whose ROLLBACK wedges forever (the
    park broker call is bounded, DB writes are not) keeps its lock held —
    the parent is silently skipped by every watcher path with no alert.
    `seen` (loop-local, pid -> first-seen monotonic) persists across passes;
    ids that leave the set are forgotten. Loud watcher_stuck row per clear."""
    firing = getattr(state, "firing_parent_ids", None)
    if firing is None:
        return []
    if max_age_sec is None:
        max_age_sec = 10.0 * float(os.environ.get("FEF_FIRE_DEADLINE_SEC", "30"))
    now = _time.monotonic()
    for pid in set(seen) - set(firing):
        seen.pop(pid, None)
    cleared: list[int] = []
    for pid in list(firing):
        first = seen.setdefault(pid, now)
        if now - first < max_age_sec:
            continue
        firing.discard(pid)
        seen.pop(pid, None)
        cleared.append(pid)
        try:
            await SignalIterRepository.write_audit_log(
                category="watcher_stuck", actor="system",
                summary=(
                    f"FIRING LOCK FORCE-CLEARED: parent {pid} held the firing "
                    f"lock >{max_age_sec:.0f}s — wedged fire suspected; the "
                    f"parent returns to watcher management (VERIFY its state)"
                ),
                details={"parent_id": pid, "max_age_sec": max_age_sec},
            )
        except Exception:
            logger.exception("firing-lock sweep audit failed for parent %s", pid)
    return cleared


def _reset_watcher_runtime_state(state, parent_id) -> None:
    """Clear per-parent in-memory watcher bookkeeping when a parent is
    re-adopted with fresh levels — the lifetime-sold counter, refusal
    streak/backoff, and re-anchored-plan memo belong to its previous life
    (the memo would re-apply OLD levels over the emergency stop)."""
    for attr in ("_watcher_lifetime_sold", "_watcher_refusal_streak",
                 "_watcher_refusal_backoff_ms", "_watcher_plan_sanity"):
        try:
            getattr(state, attr, {}).pop(parent_id, None)
        except Exception:
            pass
    # Fill-sync "done" latch + grace flag belong to the previous life too: a
    # re-adopted parent has a fresh (broker-rebased) filled_qty and must be
    # allowed to re-sync from its new trade rather than early-returning on a
    # stale latch (2026-07-20 undersell fix).
    for attr in ("_watcher_fill_grace_expired", "_watcher_fill_synced"):
        try:
            getattr(state, attr, set()).discard(parent_id)
        except Exception:
            pass


async def position_reconcile_loop(state) -> None:
    """Periodic reconcile (re-review N4): boot-only reconciliation left
    parked (STUCK/CLOSED_EXTERNAL) and orphaned parents uncorrected for the
    rest of the session. Re-runs reconcile_positions every
    FEF_RECONCILE_INTERVAL_SEC (default 300s) — idempotent; fails safe on
    every broker-query path inside."""
    from fef_sidecar.engine.loop_watchdog import heartbeat as _wd_beat
    interval = float(os.environ.get("FEF_RECONCILE_INTERVAL_SEC", "300"))
    logger.info("position reconcile loop starting (interval=%.0fs)", interval)
    await asyncio.sleep(30)  # let boot / IBKR connect settle first
    stale_firing_seen: dict = {}
    while True:
        try:
            _wd_beat(state, "position-reconcile")
            await _sweep_stale_firing_locks(state, stale_firing_seen)
            await reconcile_positions(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("periodic position reconcile failed")
        await asyncio.sleep(interval)


async def sweep_orphan_positions(state) -> dict[str, Any]:
    """Aggressive: market-sell-to-close every OPT position that isn't tracked
    by a managed parent. Use when you see orphans in the reconcile report
    and you don't want them riding without a stop.
    """
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from ib_async import MarketOrder

    if not isinstance(state.ibkr, LiveIBKRProvider):
        return {"mode": "mock", "swept": 0}
    ib = getattr(state.ibkr, "_ib", None)
    if ib is None or not ib.isConnected():
        return {"mode": "disconnected", "swept": 0}

    from fef_sidecar.ibkr.safe_sell import safe_market_sell

    recon = await reconcile_positions(state)
    swept: list[dict] = []
    for o in recon.get("orphans_detail", []):
        # Find the contract again
        for p in ib.positions():
            c = p.contract
            if (getattr(c, "secType", "") == "OPT"
                and _position_key(c.symbol, float(c.strike), c.right,
                                  c.lastTradeDateOrContractMonth) == o["key"]):
                qty = int(p.position)
                if abs(qty) < 1: continue
                action = "SELL" if qty > 0 else "BUY"
                try:
                    if action == "SELL":
                        res = await safe_market_sell(
                            ib, c, abs(qty),
                            purpose="orphan_sweep", state=state,
                        )
                        if res.placed:
                            swept.append({
                                "contract": o["contract"], "qty": res.sell_qty,
                                "action": "SELL", "ib_order_id": res.trade.order.orderId,
                            })
                        else:
                            swept.append({
                                "contract": o["contract"], "skipped": True,
                                "reason": res.reason,
                            })
                    else:
                        order = MarketOrder("BUY", abs(qty), transmit=True)
                        t = ib.placeOrder(c, order)
                        swept.append({
                            "contract": o["contract"], "qty": abs(qty),
                            "action": "BUY", "ib_order_id": t.order.orderId,
                        })
                except Exception as e:
                    swept.append({"contract": o["contract"], "error": str(e)})
    await SignalIterRepository.write_audit_log(
        category="orphan_sweep", actor="user",
        summary=f"Orphan sweep: market-closed {len(swept)} unmanaged positions",
        details={"swept": swept},
    )
    return {"mode": "live", "swept": len(swept), "detail": swept}
