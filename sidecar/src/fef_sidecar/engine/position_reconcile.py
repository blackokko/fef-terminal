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


def _resurrectable_for_orphan(key, parent_keys, resurrectable, spot):
    """Which resolved parent (if any) to RE-ADOPT for an orphaned IBKR position.

    Safety policy — re-adopt ONLY when all hold, else leave it a warned orphan:
      * the position has NO open parent (genuinely orphaned), AND
      * a recently-resolved (CLOSED/STOPPED) parent for the SAME contract exists
        — i.e. it's our own wrongly-closed bracket, not an untracked manual trade
        we shouldn't auto-manage, AND
      * we have a live spot to anchor the emergency stop.
    Returns the parent to reopen, or None.
    """
    if key in parent_keys or not spot:
        return None
    return resurrectable.get(key)


async def reconcile_positions(state) -> dict[str, Any]:
    """Walk IBKR positions ↔ LiveOrder parents. Return counts + lists."""
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
    async with get_session() as session:
        resolved_parents = list((await session.exec(
            select(LiveOrder)
            .where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.dry_run == False)  # noqa: E712
            .where(LiveOrder.status.in_(["CLOSED", "STOPPED"]))  # type: ignore[attr-defined]
            .order_by(LiveOrder.id.desc())  # type: ignore[attr-defined]
            .limit(100)
        )).all())
    resurrectable: dict[tuple, LiveOrder] = {}
    for p in resolved_parents:
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
        resur = _resurrectable_for_orphan(key, parent_keys, resurrectable, spot)
        if resur is not None:
            # Reopen FILLED with a fresh emergency spx_points stop + targets and
            # CLEAR the (possibly corrupted) tier-fired flags so the watcher
            # manages the still-open qty. safe_market_sell clamps to the live
            # position, so fresh targets can never over-sell. Original strategy
            # preserved in notes.
            updates = _emergency_backstop_updates(resur, spot)
            updates["status"] = "FILLED"
            updates["spx_target_t1_fired"] = False
            updates["spx_target_t2_fired"] = False
            updates["spx_target_t3_fired"] = False
            prev_notes = (resur.notes or "").strip()
            tag = (f"readopted_orphan: was {resur.status}, {ib_p['qty']}ct still open "
                   f"at IBKR — reopened + emergency stop (orig_exit={resur.exit_strategy})")
            updates["notes"] = f"{prev_notes} | {tag}" if prev_notes else tag
            try:
                await SignalIterRepository.update_live_order(resur.id, **updates)
                rearmed_count += 1
                readopted.append({
                    "contract": f"{ib_p['symbol']} {ib_p['strike']}{ib_p['right']}",
                    "qty": ib_p["qty"],
                    "parent_id": resur.id,
                    "was_status": resur.status,
                    "emergency_stop": updates.get("dynamic_stop_level"),
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

    # Phantoms: we have, IBKR doesn't
    for key, db_p in parent_keys.items():
        if key not in ib_positions:
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
        "matched_detail": matched,
        "readopted_detail": readopted,
        "orphans_detail": orphans,
        "phantoms_detail": phantoms,
    }


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
