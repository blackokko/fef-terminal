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

import logging
from typing import Any

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import LiveOrder
from fef_sidecar.db.repository import SignalIterRepository

logger = logging.getLogger(__name__)


def _position_key(symbol: str, strike: float, right: str, expiry: str) -> str:
    return f"{symbol}|{round(float(strike), 2)}|{right}|{expiry}"


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

    matched: list[dict] = []
    orphans: list[dict] = []
    phantoms: list[dict] = []

    # Matched: in both. Watcher resumes naturally.
    for key, ib_p in ib_positions.items():
        if key in parent_keys:
            db_p = parent_keys[key]
            matched.append({
                "contract": f"{ib_p['symbol']} {ib_p['strike']}{ib_p['right']}",
                "ib_qty": ib_p["qty"],
                "db_qty": db_p.qty,
                "db_status": db_p.status,
                "dynamic_stop_level": db_p.dynamic_stop_level,
                "be_ladder_max_step_R": db_p.be_ladder_max_step_R,
                "parent_id": db_p.id,
            })

    # Orphans: IBKR has, we don't
    for key, ib_p in ib_positions.items():
        if key not in parent_keys:
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
            summary=f"Resumed management of {len(matched)} existing positions",
            details={"matched": matched},
        )
    if orphans:
        await SignalIterRepository.write_audit_log(
            category="position_reconcile", actor="system",
            summary=(
                f"⚠ ORPHAN POSITIONS DETECTED: {len(orphans)} positions in IBKR "
                f"have no matching parent in our DB. The bot WILL NOT manage their "
                f"exits. Either close manually in TWS or call /admin/sweep_orphans."
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
        "Position reconcile: %d matched, %d orphans, %d phantoms",
        len(matched), len(orphans), len(phantoms),
    )
    return {
        "mode": "live",
        "matched": len(matched),
        "orphans_count": len(orphans),
        "phantoms_count": len(phantoms),
        "matched_detail": matched,
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
                    order = MarketOrder(action, abs(qty), transmit=True)
                    t = ib.placeOrder(c, order)
                    swept.append({
                        "contract": o["contract"], "qty": abs(qty),
                        "action": action, "ib_order_id": t.order.orderId,
                    })
                except Exception as e:
                    swept.append({"contract": o["contract"], "error": str(e)})
    await SignalIterRepository.write_audit_log(
        category="orphan_sweep", actor="user",
        summary=f"Orphan sweep: market-closed {len(swept)} unmanaged positions",
        details={"swept": swept},
    )
    return {"mode": "live", "swept": len(swept), "detail": swept}
