"""Entry-order timeout watchdog (2026-07-16 incident fix, part 2).

If the marketable-limit ENTRY (entry_pricing.py) is not FULLY filled within
FEF_ENTRY_TIMEOUT_SEC (default 45s) of placement, cancel the remainder and
stand the fire down. The 7/16 hang (live_order 776: MKT entry rested 4m20s,
filled $8.10 vs ~$5.50-6.00 at signal) lucked into a winner; on a reversing
tape the same hang is a catastrophic stale fill.

Outcome matrix at timeout (IBKR cancel is ASYNC — a fill can land after the
cancel is requested, so every branch re-reads the trade AFTER the bounded
cancel-confirm wait):

  filled == qty  before cancel  -> nothing to do (entry just made it).
  filled == 0,  cancel confirms -> STAND DOWN: parent (+ any child rows)
                                   marked CANCELLED, per-symbol fire budget
                                   released (cool-down deliberately KEPT so a
                                   thin tape can't hot-loop place->timeout->
                                   place), signal row counterfactual-tagged
                                   like the other gates, 'entry_timeout'
                                   audit row.
  0 < filled < qty              -> keep + manage the filled portion: row
                                   synced to PARTIAL/filled_qty exactly as
                                   _sync_parent_fill_from_trades would (the
                                   watcher scans SUBMITTED/FILLED/PARTIAL and
                                   its lifetime sell cap keys off filled_qty,
                                   so the partial is managed + can't be
                                   oversold), remainder cancelled, budget
                                   KEPT (a position exists).
  filled == qty during cancel   -> 'too late to cancel' race: keep + manage
                                   as a normal fill (fill-sync sets FILLED).
  cancel unconfirmed / broker   -> FAIL SAFE: touch nothing, audit loudly.
  state unreadable                 The parent stays SUBMITTED where the
                                   existing machinery owns it: watcher
                                   fill-sync picks up a late fill, ENTRY_
                                   FILL_GRACE (120s fail-open) keeps exits
                                   armed, periodic reconcile resolves
                                   phantoms.

'Cancelled but filled AFTER our final read' (fill event lands post-read):
the parent is already CANCELLED in our DB, so the position shows up as an
IBKR orphan — position_reconcile's re-adoption path now includes CANCELLED
in its resurrectable statuses and re-adopts it FILLED with an emergency
stop. That machinery is the designed backstop, not an accident.

Scope: watcher-managed entries (spx_points / fixed_dollar — parent-only at
IBKR, unambiguous cancel semantics). For the legacy option_price bracket,
only the ZERO-fill timeout cancels (parent + attached children die
together); a PARTIAL fill on that path is left alone — cancelling a
partially-filled parent with attached children has ambiguous exchange-side
child-quantity semantics, and auto-trade runs watcher-managed live.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)

DEFAULT_ENTRY_TIMEOUT_SEC = 45.0

# IBKR order statuses that mean the order is no longer working.
_TERMINAL_IB_STATUSES = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
_CANCELLED_IB_STATUSES = {"Cancelled", "ApiCancelled", "Inactive"}

_CANCEL_CONFIRM_WAIT_SEC = float(os.environ.get("FEF_ENTRY_CANCEL_WAIT_SEC", "10"))
_CANCEL_POLL_SEC = 0.25


def entry_timeout_sec() -> float:
    """Operator knob, env-tunable. <=0 disables the timeout entirely."""
    try:
        return float(os.environ.get("FEF_ENTRY_TIMEOUT_SEC", str(DEFAULT_ENTRY_TIMEOUT_SEC)))
    except ValueError:
        return DEFAULT_ENTRY_TIMEOUT_SEC


def spawn_entry_timeout_monitor(**kwargs) -> asyncio.Task | None:
    """create_task wrapper that can never break order placement."""
    try:
        task = asyncio.create_task(entry_timeout_monitor(**kwargs))
        # Keep a strong reference so the task isn't GC'd mid-flight.
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        return task
    except Exception:
        logger.exception("failed to spawn entry timeout monitor")
        return None


_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _filled_qty(trade) -> int:
    try:
        return int(float(getattr(trade.orderStatus, "filled", 0) or 0))
    except Exception:
        return 0


def _ib_status(trade) -> str:
    try:
        return str(getattr(trade.orderStatus, "status", "") or "")
    except Exception:
        return ""


def _avg_fill_price(trade) -> float | None:
    try:
        avg = float(getattr(trade.orderStatus, "avgFillPrice", 0) or 0)
        return round(avg, 4) if avg > 0 else None
    except Exception:
        return None


def _find_trade(ibkr, ib_order_id: int | None, held_trade=None):
    """Fresh lookup by orderId in ib.trades() (ib_async mutates Trade objects
    in place, but a reconnect can replace them); fall back to the reference
    held since placement."""
    try:
        ib = getattr(ibkr, "_ib", None)
        if ib is not None and ib_order_id is not None:
            for t in ib.trades():
                if (getattr(getattr(t, "order", None), "orderId", None) == ib_order_id
                        and getattr(t.order, "action", "") == "BUY"):
                    return t
    except Exception:
        logger.exception("entry timeout: trade lookup failed for order %s", ib_order_id)
    return held_trade


def _release_fire_budget(state, symbol: str) -> bool:
    """Give back the per-symbol session-cap slot consumed by this fire —
    mirrors the gate philosophy that counters represent OPENED positions
    ('no budget spent — counters move only on a successful fire'). The
    COOL-DOWN timestamp is deliberately NOT rolled back: an instant re-fire
    into the same tape that just refused to fill us is churn, not edge."""
    if state is None:
        return False
    try:
        by_sym = getattr(state, "auto_trade_count_by_symbol", None)
        if isinstance(by_sym, dict):
            by_sym[symbol] = max(0, int(by_sym.get(symbol, 0)) - 1)
        total = getattr(state, "auto_trade_count_session", None)
        if isinstance(total, int):
            state.auto_trade_count_session = max(0, total - 1)
        return True
    except Exception:
        logger.exception("entry timeout: budget release failed for %s", symbol)
        return False


async def _load_parent_row(parent_id: int):
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import LiveOrder
    async with get_session() as session:
        return (await session.exec(
            select(LiveOrder).where(LiveOrder.id == parent_id)
        )).first()


async def _mark_cancelled_if_unfilled(parent_id: int, tag: str) -> bool:
    """ATOMIC zero-fill stand-down write (review MINOR-3): a guarded
    UPDATE ... WHERE status='SUBMITTED' AND filled_qty=0, so the watcher's
    fill-sync stamping FILLED/PARTIAL in the read-decide-write gap can never
    be clobbered back to CANCELLED (which would drop a filled parent from
    the watcher scan until reconcile re-adopts it). Returns True iff exactly
    this transition happened."""
    from datetime import UTC, datetime
    from sqlalchemy import update as _sa_update
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import LiveOrder
    async with get_session() as session:
        row = (await session.exec(
            select(LiveOrder).where(LiveOrder.id == parent_id)
        )).first()
        if row is None or row.status != "SUBMITTED" or int(row.filled_qty or 0) > 0:
            return False
        notes_prev = (row.notes or "").strip()
        new_notes = f"{notes_prev} | {tag}" if notes_prev else tag
        result = await session.execute(
            _sa_update(LiveOrder)
            .where(LiveOrder.id == parent_id)
            .where(LiveOrder.status == "SUBMITTED")
            .where(LiveOrder.filled_qty == 0)
            .values(status="CANCELLED", notes=new_notes,
                    last_status_at=datetime.now(UTC))
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1


async def entry_timeout_monitor(
    *,
    state,
    ibkr,
    parent_id: int,
    ib_order_id: int | None,
    symbol: str,
    qty: int,
    exit_strategy: str,
    source: str,
    limit_price: float | None = None,
    signal_iter_id: int | None = None,
    child_ids: tuple[int, ...] = (),
    timeout_sec: float | None = None,
    cancel_wait_sec: float | None = None,
    parent_trade=None,
) -> dict:
    """See module docstring for the full outcome matrix. Returns a summary
    dict (for tests); every branch is also audit-logged."""
    from fef_sidecar.db.repository import SignalIterRepository

    t_out = entry_timeout_sec() if timeout_sec is None else float(timeout_sec)
    c_wait = _CANCEL_CONFIRM_WAIT_SEC if cancel_wait_sec is None else float(cancel_wait_sec)
    if t_out <= 0:
        return {"outcome": "disabled"}

    try:
        await asyncio.sleep(t_out)

        trade = _find_trade(ibkr, ib_order_id, parent_trade)
        if trade is None:
            # Can't see the order at all — FAIL SAFE (no cancel, no DB write).
            await _audit(
                SignalIterRepository, parent_id, symbol, source,
                summary=(
                    f"ENTRY TIMEOUT ({t_out:.0f}s) for parent {parent_id} "
                    f"{symbol}: could not locate ib_order_id={ib_order_id} in "
                    f"ib.trades() — NOT cancelling (fail-safe); fill-sync/"
                    f"reconcile own this parent now"
                ),
                details={"outcome": "trade_not_found", "ib_order_id": ib_order_id},
            )
            return {"outcome": "trade_not_found"}

        filled = _filled_qty(trade)
        if filled >= qty:
            return {"outcome": "filled_in_time", "filled": filled}

        if exit_strategy == "option_price" and filled > 0:
            # Legacy IBKR-bracket path: cancelling a partially-filled parent
            # with attached children has ambiguous child-qty semantics at the
            # exchange. Leave the remainder working; say so.
            await _audit(
                SignalIterRepository, parent_id, symbol, source,
                summary=(
                    f"ENTRY TIMEOUT ({t_out:.0f}s) for parent {parent_id} "
                    f"{symbol}: {filled}/{qty} filled on legacy option_price "
                    f"bracket — remainder left working (attached-children "
                    f"partial-cancel semantics ambiguous); no action taken"
                ),
                details={"outcome": "legacy_partial_left_working",
                         "filled": filled, "qty": qty},
            )
            return {"outcome": "legacy_partial_left_working", "filled": filled}

        # Not fully filled — request cancel of the remainder. IBKR cancel is
        # async; a fill can land between this call and confirmation, so the
        # authoritative read happens AFTER the bounded wait below.
        # Already-terminal trades (kill-switch or TWS-side cancel got there
        # first) skip the redundant cancelOrder — no spurious IBKR error —
        # and fall straight through to the same outcome accounting.
        try:
            if _ib_status(trade) not in _TERMINAL_IB_STATUSES:
                ib = getattr(ibkr, "_ib", None)
                if ib is None:
                    raise RuntimeError("no live ib handle")
                ib.cancelOrder(trade.order)
        except Exception as e:
            await _audit(
                SignalIterRepository, parent_id, symbol, source,
                summary=(
                    f"ENTRY TIMEOUT ({t_out:.0f}s) for parent {parent_id} "
                    f"{symbol}: cancel request FAILED ({type(e).__name__}: {e}) "
                    f"— NOT standing down (fail-safe); order may still fill; "
                    f"fill-sync/reconcile own this parent"
                ),
                details={"outcome": "cancel_request_failed", "error": str(e)},
            )
            return {"outcome": "cancel_request_failed"}

        deadline = time.monotonic() + c_wait
        while time.monotonic() < deadline and _ib_status(trade) not in _TERMINAL_IB_STATUSES:
            await asyncio.sleep(_CANCEL_POLL_SEC)

        final_status = _ib_status(trade)
        final_filled = _filled_qty(trade)

        if final_filled >= qty:
            # Fill completed while the cancel was in flight ('too late to
            # cancel'). Keep + manage as a normal fill — the watcher's
            # _sync_parent_fill_from_trades stamps FILLED within a cycle.
            await _audit(
                SignalIterRepository, parent_id, symbol, source,
                summary=(
                    f"ENTRY TIMEOUT ({t_out:.0f}s) for parent {parent_id} "
                    f"{symbol}: order FILLED {final_filled}/{qty} during the "
                    f"cancel window — kept; watcher manages it normally"
                ),
                details={"outcome": "filled_during_cancel",
                         "filled": final_filled, "qty": qty},
            )
            return {"outcome": "filled_during_cancel", "filled": final_filled}

        if final_filled > 0 and final_status in _TERMINAL_IB_STATUSES:
            # PARTIAL: remainder cancelled, keep + manage the filled lots.
            # Mirror _sync_parent_fill_from_trades' write exactly (status
            # PARTIAL only from SUBMITTED — never clobber a state the watcher
            # has already advanced).
            row = await _load_parent_row(parent_id)
            updates: dict = {"filled_qty": max(int(getattr(row, "filled_qty", 0) or 0), final_filled)}
            avg = _avg_fill_price(trade)
            if avg is not None and not (row is not None and (row.avg_fill_price or 0) > 0):
                updates["avg_fill_price"] = avg
            if row is not None and row.status == "SUBMITTED":
                updates["status"] = "PARTIAL"
            notes_prev = (getattr(row, "notes", "") or "").strip()
            tag = (f"entry_timeout: remainder {qty - final_filled}ct cancelled at "
                   f"{t_out:.0f}s; managing {final_filled}ct partial")
            updates["notes"] = f"{notes_prev} | {tag}" if notes_prev else tag
            await SignalIterRepository.update_live_order(parent_id, **updates)
            await _audit(
                SignalIterRepository, parent_id, symbol, source,
                summary=(
                    f"ENTRY TIMEOUT ({t_out:.0f}s) for parent {parent_id} "
                    f"{symbol}: PARTIAL {final_filled}/{qty} — remainder "
                    f"cancelled, keeping + managing the filled portion "
                    f"(watcher scans PARTIAL; lifetime sell cap keys off "
                    f"filled_qty). Fire budget NOT released (position exists)."
                ),
                details={"outcome": "partial_kept", "filled": final_filled,
                         "qty": qty, "ib_status": final_status},
            )
            return {"outcome": "partial_kept", "filled": final_filled}

        if final_filled <= 0 and final_status in _CANCELLED_IB_STATUSES:
            # ZERO-FILL STAND-DOWN. ATOMIC guarded write (UPDATE ... WHERE
            # status='SUBMITTED' AND filled_qty=0): if the watcher fill-sync
            # raced us to FILLED/PARTIAL, the guard misses and we fail safe.
            tag = (f"entry_timeout: unfilled after {t_out:.0f}s — cancelled, "
                   f"fire stood down (limit {limit_price})")
            if not await _mark_cancelled_if_unfilled(parent_id, tag):
                row = await _load_parent_row(parent_id)
                await _audit(
                    SignalIterRepository, parent_id, symbol, source,
                    summary=(
                        f"ENTRY TIMEOUT ({t_out:.0f}s) for parent {parent_id} "
                        f"{symbol}: broker reports cancelled/0 filled but the DB "
                        f"row is {getattr(row, 'status', 'MISSING')}/"
                        f"filled_qty={getattr(row, 'filled_qty', '?')} — NOT "
                        f"marking CANCELLED (fail-safe); reconcile owns it"
                    ),
                    details={"outcome": "db_row_diverged"},
                )
                return {"outcome": "db_row_diverged"}
            for cid in child_ids:
                try:
                    await SignalIterRepository.update_live_order(
                        cid, status="CANCELLED",
                        notes="entry_timeout: parent entry cancelled unfilled",
                    )
                except Exception:
                    logger.exception("entry timeout: child %s cancel-mark failed", cid)
            released = _release_fire_budget(state, symbol)
            # Counterfactual: tag the signal row like the other stood-down
            # gates so the lookahead worker scores the withheld fire.
            if signal_iter_id is not None:
                try:
                    from fef_sidecar.engine.auto_trader import _tag_signal_skip
                    await _tag_signal_skip(
                        signal_iter_id, "entry_timeout",
                        f"[ENTRY TIMEOUT: unfilled after {t_out:.0f}s at "
                        f"limit {limit_price} — cancelled, fire stood down]",
                    )
                except Exception:
                    logger.exception("entry timeout: counterfactual tag failed")
            await _audit(
                SignalIterRepository, parent_id, symbol, source,
                summary=(
                    f"ENTRY TIMEOUT ({t_out:.0f}s) for parent {parent_id} "
                    f"{symbol}: LMT {qty}ct @ {limit_price} unfilled — "
                    f"CANCELLED, fire stood down. Session-cap slot "
                    f"{'released' if released else 'NOT released'}; cool-down "
                    f"kept. Counterfactual tagged on signal "
                    f"{signal_iter_id} (scoreable as a withheld fire). If a "
                    f"late fill raced this cancel, reconcile re-adopts the "
                    f"CANCELLED parent with an emergency stop."
                ),
                details={"outcome": "cancelled_stand_down", "qty": qty,
                         "limit_price": limit_price, "budget_released": released,
                         "signal_iter_id": signal_iter_id,
                         "ib_status": final_status},
            )
            return {"outcome": "cancelled_stand_down", "budget_released": released}

        # Cancel not confirmed inside the wait (status still active/unknown).
        # FAIL SAFE: leave the row SUBMITTED — a late fill is picked up by the
        # watcher fill-sync / ENTRY_FILL_GRACE; a late cancel leaves a phantom
        # that reconcile marks CLOSED.
        await _audit(
            SignalIterRepository, parent_id, symbol, source,
            summary=(
                f"ENTRY TIMEOUT ({t_out:.0f}s) for parent {parent_id} "
                f"{symbol}: cancel requested but UNCONFIRMED after "
                f"{c_wait:.0f}s (status={final_status!r}, filled="
                f"{final_filled}) — leaving row SUBMITTED (fail-safe); "
                f"fill-sync/grace/reconcile own it"
            ),
            details={"outcome": "cancel_unconfirmed",
                     "ib_status": final_status, "filled": final_filled},
        )
        return {"outcome": "cancel_unconfirmed"}
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("entry timeout monitor crashed for parent %s", parent_id)
        return {"outcome": "monitor_error"}


async def _audit(repo, parent_id, symbol, source, *, summary, details) -> None:
    try:
        await repo.write_audit_log(
            category="entry_timeout", actor="system", summary=summary,
            details={"parent_id": parent_id, "symbol": symbol,
                     "source": source, **details},
        )
    except Exception:
        logger.exception("entry_timeout audit write failed for parent %s", parent_id)
