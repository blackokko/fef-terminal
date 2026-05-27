"""FIFO pair-off of ``ibkr_execution`` rows attributed back to parents.

Background
----------
The pre-5/21 ``daily_review.actual_wins`` / ``actual_losses`` counts came
from ``_estimate_parent_r``, which read ``LiveOrder.spx_target_t{1,2,3}_fired``
boolean flags. Those flags are "did we INTEND to fire" signals, not
"did the broker confirm a fill" signals. On legacy parents where the
flags weren't bumped (or where watcher sells skipped the flag-write
path), the estimator returns garbage and the daily_review row inflated
to all-wins.

The right source of truth is broker-confirmed fills from
``ibkr_execution``, paired off FIFO per ``(symbol, strike, right,
expiry)`` and attributed back to the parent ``LiveOrder.id`` that
opened each lot.

This module exposes ``compute_parent_wl_from_fills`` as the canonical
helper. ``daily_review.compute_daily_review`` and the recompute
maintenance scripts should both use it so the numbers agree on every
session.

The algorithm mirrors ``scripts/maintenance/recompute_daily_session_wl.py``
exactly so the two paths produce the same answer.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import IBKRExecution, LiveOrder

logger = logging.getLogger(__name__)


@dataclass
class ParentFillSummary:
    """Per-parent realized totals from FIFO pair-off of ibkr_execution.

    ``classification`` is the W/L bucket the parent belongs to:

    * ``'win'`` — realized > 1 cent
    * ``'loss'`` — realized < -1 cent
    * ``'flat'`` — paired off to within 1 cent of zero
    * ``'open'`` — no offsetting SLD seen for the BOT lot(s) at session EOD
    """

    parent_id: int
    realized_dollars: float = 0.0
    open_shares: float = 0.0  # shares still long at EOD
    classification: str = "flat"  # 'win' | 'loss' | 'flat' | 'open'


@dataclass
class SessionFillSummary:
    """Session-level FIFO totals plus per-parent breakdown.

    ``unattributed_realized`` captures realized $ from execs that
    couldn't be tied back to a parent (orphan SLD with no matching BOT
    lot, or BOT exec with no matching ``live_order`` row). It still
    contributes to ``total_realized_dollars`` so the session sum equals
    the contract-level FIFO sum used by daily_session.
    """

    session_date: str
    total_realized_dollars: float = 0.0
    unattributed_realized: float = 0.0
    unattributed_sell_shares: float = 0.0
    best_pnl: float = 0.0
    worst_pnl: float = 0.0
    n_contracts: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    open_at_eod: int = 0
    parents_attributed: int = 0
    n_execs: int = 0
    by_parent: dict[int, ParentFillSummary] = field(default_factory=dict)


async def compute_parent_wl_from_fills(
    session_date: str,
) -> SessionFillSummary:
    """Walk ``ibkr_execution`` for the session and FIFO-pair-off per contract.

    Returns a ``SessionFillSummary`` carrying:

    * Per-parent realized $ + classification (win/loss/flat/open)
    * Session totals: realized $, best/worst per-contract, win/loss/flat counts
    * Unattributed $ from orphan execs (no matching parent)

    The contract-level FIFO sum equals
    ``daily_session.gross_pnl`` to the penny on 5/19, 5/20, 5/21.

    This is the canonical W/L helper. ``daily_review.compute_daily_review``
    delegates to this for ``actual_wins`` / ``actual_losses`` /
    ``actual_total_R`` to keep daily_review + daily_session on the same
    source of truth.
    """
    async with get_session() as session:
        execs = list((await session.exec(
            select(IBKRExecution)
            .where(IBKRExecution.session_date == session_date)
            .order_by(IBKRExecution.timestamp, IBKRExecution.id)
        )).all())

        summary = SessionFillSummary(session_date=session_date, n_execs=len(execs))
        if not execs:
            return summary

        # Group by contract for per-contract FIFO
        by_contract: dict[tuple, list] = defaultdict(list)
        for e in execs:
            key = (e.symbol, float(e.strike or 0.0), e.right, e.expiry)
            by_contract[key].append(e)

        summary.n_contracts = len(by_contract)

        # Pre-resolve all BOT execs to parents in one pass so we don't
        # re-query the same (ib_order_id, contract) tuple multiple times.
        # The recompute script does it inline per BOT; we batch for perf.
        # Pull every parent for the session into memory.
        all_parents = list((await session.exec(
            select(LiveOrder).where(LiveOrder.bracket_role == "parent")
        )).all())
        parents_by_oid: dict[int, LiveOrder] = {}
        parents_for_contract: dict[tuple, list[LiveOrder]] = defaultdict(list)
        for p in all_parents:
            if p.submitted_at.strftime("%Y-%m-%d") != session_date:
                continue
            if p.ib_order_id is not None:
                parents_by_oid[p.ib_order_id] = p
            key = (p.symbol, float(p.strike or 0.0), p.right, p.expiry)
            parents_for_contract[key].append(p)
        # Sort each per-contract list by submitted_at desc so the
        # fallback "nearest at-or-before" lookup is O(N) walk.
        for key in parents_for_contract:
            parents_for_contract[key].sort(
                key=lambda p: p.submitted_at, reverse=True
            )

        def _resolve_local(e: IBKRExecution) -> int | None:
            if e.ib_order_id is not None:
                p = parents_by_oid.get(e.ib_order_id)
                if p is not None:
                    return p.id
            key = (e.symbol, float(e.strike or 0.0), e.right, e.expiry)
            for p in parents_for_contract.get(key, []):
                if p.submitted_at <= e.timestamp:
                    return p.id
            return None

        realized_per_parent: dict[int, float] = defaultdict(float)
        open_per_parent: dict[int, float] = defaultdict(float)
        contract_pnl: dict[tuple, float] = defaultdict(float)
        unattributed_realized = 0.0
        unattributed_sell_shares = 0.0

        for key, ex_list in by_contract.items():
            # FIFO queue of open lots: [parent_id_or_None, qty_remaining, buy_px]
            lots: deque = deque()
            for e in ex_list:
                side_u = (e.side or "").upper()
                shares = float(e.shares or 0.0)
                price = float(e.price or 0.0)
                if side_u in ("BOT", "BUY"):
                    parent_id = _resolve_local(e)
                    lots.append([parent_id, shares, price])
                    if parent_id is not None:
                        open_per_parent[parent_id] += shares
                elif side_u in ("SLD", "SELL"):
                    remaining = shares
                    while remaining > 0 and lots:
                        lot = lots[0]
                        take = min(remaining, lot[1])
                        pnl = (price - lot[2]) * take * 100.0
                        pid = lot[0]
                        if pid is not None:
                            realized_per_parent[pid] += pnl
                            open_per_parent[pid] -= take
                        else:
                            unattributed_realized += pnl
                        contract_pnl[key] += pnl
                        lot[1] -= take
                        remaining -= take
                        if lot[1] <= 1e-9:
                            lots.popleft()
                    if remaining > 0:
                        # No open lots to pair against — orphan sell (shouldn't
                        # happen in normal long-only operation, but record it)
                        unattributed_realized += -price * remaining * 100.0
                        unattributed_sell_shares += remaining

        # Build per-parent summaries.
        # Classification is by REALIZED $: a partially-scaled parent with
        # some realized profit and some residual still long counts as a
        # winner on the realized portion (matches
        # recompute_daily_session_wl semantics). The `open` label is
        # reserved for parents with NO realized fill — those genuinely
        # belong to the next session.
        all_pids = set(realized_per_parent.keys()) | set(open_per_parent.keys())
        for pid in all_pids:
            realized = realized_per_parent.get(pid, 0.0)
            open_qty = open_per_parent.get(pid, 0.0)
            if abs(realized) > 0.01:
                classification = "win" if realized > 0 else "loss"
            elif open_qty > 1e-6:
                classification = "open"
            else:
                classification = "flat"
            summary.by_parent[pid] = ParentFillSummary(
                parent_id=pid,
                realized_dollars=realized,
                open_shares=open_qty,
                classification=classification,
            )

        # Session-level tallies
        summary.wins = sum(1 for s in summary.by_parent.values()
                           if s.classification == "win")
        summary.losses = sum(1 for s in summary.by_parent.values()
                             if s.classification == "loss")
        summary.flats = sum(1 for s in summary.by_parent.values()
                            if s.classification == "flat")
        # open_at_eod counts ANY parent with residual shares (even
        # winners that scaled out partials). This is a diagnostic, not
        # a primary classification bucket — sum(wins+losses+flats) is the
        # canonical "parents resolved this session" total.
        summary.open_at_eod = sum(1 for q in open_per_parent.values()
                                  if q > 1e-6)
        summary.parents_attributed = len(realized_per_parent)
        summary.unattributed_realized = unattributed_realized
        summary.unattributed_sell_shares = unattributed_sell_shares
        # Session realized $ = sum of all per-parent realized + unattributed
        # (matches contract-level FIFO sum which equals daily_session.gross_pnl)
        summary.total_realized_dollars = (
            sum(realized_per_parent.values()) + unattributed_realized
        )
        # Best/worst per parent (matches recompute_daily_session_wl semantics)
        per_parent_vals = list(realized_per_parent.values())
        summary.best_pnl = max(per_parent_vals, default=0.0)
        summary.worst_pnl = min(per_parent_vals, default=0.0)

    return summary
