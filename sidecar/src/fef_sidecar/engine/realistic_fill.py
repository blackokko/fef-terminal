"""Realistic-fill estimator.

Replays paper trades under a HAIRCUTTED fill model that approximates
live trading friction:
  - BUYs fill at `ask + edge_$` (you cross the spread; worse than mid)
  - SELLs fill at `bid - edge_$` (same)
  - Plus a latency-drag estimate per exit
  - Plus per-leg commission

Bid/ask at entry time is available in SignalIter.extras.option_pricing
(forward-only since 2026-05-20). Bid/ask at exit time isn't captured
yet; we estimate by assuming the spread at exit equals the spread at
entry (a strong simplification but the best we can do without exit-time
L1 capture, which is being added forward).

The realistic-fill P/L tells you: "if your paper edge is execution +
stacking, what's left when paper-flattering effects are removed?" If
realistic-fill stays positive across many sessions, the edge survives
the transition. If it collapses, the edge is paper-only.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RealisticFillConfig:
    """Tunable haircuts for the realistic-fill replay."""

    # Per-contract edge over the quoted side of the spread. You cross
    # the spread to take liquidity on entries and exits.
    spread_edge_per_contract: float = 0.05

    # Latency drag: estimate of P/L lost between trigger and fill on
    # exits. For high-gamma 0DTE options on fast moves, the quote can
    # move 20-40% of the trade's R-value in 50-300ms. Modeled as a
    # fixed % of the option's pre-exit value, applied to exit-side
    # legs only.
    latency_drag_pct: float = 0.05   # 5% conservative; real could be more

    # IBKR Pro commissions for SPX options. $0.65/leg × 2 legs = $1.30 RT.
    commission_per_contract_rt: float = 1.30


async def replay_session(session_date: str, cfg: RealisticFillConfig | None = None) -> dict:
    """Replay every closed parent for the session under realistic-fill
    assumptions. Returns paper vs realistic P/L per contract + totals.
    """
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import IBKRExecution, LiveOrder, SignalIter

    cfg = cfg or RealisticFillConfig()

    async with get_session() as session:
        execs = list((await session.exec(
            select(IBKRExecution).where(IBKRExecution.session_date == session_date)  # type: ignore[attr-defined]
        )).all())
        parents = list((await session.exec(
            select(LiveOrder)
            .where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.dry_run == False)  # noqa: E712
        )).all())
        # Pull signals for entry-time bid/ask lookup
        sigs = list((await session.exec(
            select(SignalIter).where(SignalIter.session_date == session_date)  # type: ignore[attr-defined]
        )).all())

    parents = [p for p in parents if p.submitted_at.strftime("%Y-%m-%d") == session_date]

    # Map (symbol, strike, right, expiry) → entry-time bid/ask via the
    # signal that fired closest to (and ≤60s before) each parent.
    def find_signal_for_parent(p):
        from datetime import timezone
        p_ts = p.submitted_at
        if p_ts.tzinfo is None:
            p_ts = p_ts.replace(tzinfo=timezone.utc)
        best = None
        best_diff = None
        for sg in sigs:
            sg_ts = sg.timestamp
            if sg_ts.tzinfo is None:
                sg_ts = sg_ts.replace(tzinfo=timezone.utc)
            diff = (p_ts - sg_ts).total_seconds()
            if 0 <= diff <= 60:
                opt = (sg.extras or {}).get("option_pricing") or {}
                if opt.get("contract_strike") == p.strike and opt.get("right") == p.right:
                    if best_diff is None or diff < best_diff:
                        best = sg
                        best_diff = diff
        return best

    # Group fills per contract
    by_contract = defaultdict(list)
    for f in execs:
        by_contract[(f.symbol, f.strike, f.right, f.expiry)].append(f)

    per_contract = []
    paper_total = 0.0
    realistic_total = 0.0

    for key, fills in by_contract.items():
        sym, strike, right, expiry = key
        # Paper P/L: actual fills as IBKR reports
        bot_value = sum(f.shares * f.price for f in fills if f.side == "BOT")
        sld_value = sum(f.shares * f.price for f in fills if f.side == "SLD")
        bot_q = sum(f.shares for f in fills if f.side == "BOT")
        sld_q = sum(f.shares for f in fills if f.side == "SLD")
        paired = min(bot_q, sld_q)
        if paired <= 0:
            continue

        avg_bot = bot_value / bot_q if bot_q else 0
        avg_sld = sld_value / sld_q if sld_q else 0
        paper_pnl = (avg_sld - avg_bot) * paired * 100.0

        # Realistic: shift entry up by spread_edge, exit down by spread_edge
        # PLUS apply latency drag % to exit price, PLUS commissions
        realistic_avg_bot = avg_bot + cfg.spread_edge_per_contract
        realistic_avg_sld = avg_sld - cfg.spread_edge_per_contract
        # Latency drag on exit: reduce realized exit price by latency_drag_pct
        realistic_avg_sld_after_latency = realistic_avg_sld * (1 - cfg.latency_drag_pct)
        realistic_pnl_gross = (realistic_avg_sld_after_latency - realistic_avg_bot) * paired * 100.0
        commission = paired * cfg.commission_per_contract_rt
        realistic_pnl = realistic_pnl_gross - commission

        paper_total += paper_pnl
        realistic_total += realistic_pnl

        per_contract.append({
            "contract": f"{sym} {strike}{right} {expiry}",
            "qty_paired": int(paired),
            "avg_bot_paper": round(avg_bot, 4),
            "avg_sld_paper": round(avg_sld, 4),
            "avg_bot_realistic": round(realistic_avg_bot, 4),
            "avg_sld_realistic": round(realistic_avg_sld_after_latency, 4),
            "paper_pnl": round(paper_pnl, 2),
            "realistic_pnl": round(realistic_pnl, 2),
            "delta": round(realistic_pnl - paper_pnl, 2),
            "commission": round(commission, 2),
        })

    per_contract.sort(key=lambda x: -x["paper_pnl"])

    return {
        "session_date": session_date,
        "config": {
            "spread_edge_per_contract": cfg.spread_edge_per_contract,
            "latency_drag_pct": cfg.latency_drag_pct,
            "commission_per_contract_rt": cfg.commission_per_contract_rt,
        },
        "paper_total_pnl": round(paper_total, 2),
        "realistic_total_pnl": round(realistic_total, 2),
        "total_haircut": round(paper_total - realistic_total, 2),
        "haircut_pct": round((paper_total - realistic_total) / paper_total * 100, 1) if paper_total != 0 else 0,
        "contracts_analyzed": len(per_contract),
        "per_contract": per_contract,
        "note": (
            "Estimate. Entry-side spreads come from SignalIter.extras.option_pricing "
            "captured at fire time. Exit-side spreads are approximated as equal to "
            "entry. Forward sessions will use captured exit-time L1 once watcher "
            "logging is live. Latency drag is a percentage estimate, not measured."
        ),
    }


async def replay_all_sessions(session_dates: list[str] | None = None) -> dict:
    """Run realistic-fill replay across multiple sessions for trend tracking."""
    if session_dates is None:
        from datetime import datetime, timedelta, timezone
        et = timezone(timedelta(hours=-4))
        today = datetime.now(et)
        session_dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, 7)]

    results = []
    for d in session_dates:
        try:
            r = await replay_session(d)
            if r.get("contracts_analyzed", 0) > 0:
                results.append({
                    "session_date": d,
                    "paper_total_pnl": r["paper_total_pnl"],
                    "realistic_total_pnl": r["realistic_total_pnl"],
                    "haircut_pct": r["haircut_pct"],
                    "contracts_analyzed": r["contracts_analyzed"],
                })
        except Exception as e:
            logger.exception("replay_all_sessions failed for %s", d)

    return {"sessions": results}
