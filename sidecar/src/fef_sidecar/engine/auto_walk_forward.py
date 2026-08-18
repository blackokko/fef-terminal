"""Nightly auto walk-forward — runs the existing optimization sweep,
compares optimal to current engine config, writes a WalkForwardRun row
with the drift score.

Scheduled via FEF_AUTO_WALKFORWARD_HOUR (default 22 = 10pm). Manual
trigger via POST /walk_forward/run_now.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from fef_sidecar.db.models import WalkForwardRun
from fef_sidecar.db.repository import SignalIterRepository

logger = logging.getLogger(__name__)


async def run_auto_walk_forward(state, days: int = 7) -> WalkForwardRun:
    """Run an optimization sweep + compare to current engine config."""
    from fef_sidecar.engine.optimization import (
        MIN_CONFIDENT_TRADES,
        SweepConfig,
        run_sweep,
    )

    cfg = SweepConfig(
        proximity_values=[3.0, 5.0, 8.0],
        collapse_pct_values=[0.15, 0.20, 0.30],
        collapse_floor_values=[3.0, 5.0, 10.0],
        absorption_fe_values=[3.0, 5.0, 8.0],
    )
    # Pull historical iters from DB to feed the sweep (run_sweep is sync
    # and expects pre-fetched data)
    from datetime import timedelta, timezone
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import SignalIter

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with get_session() as session:
        rows = list((await session.exec(
            select(SignalIter)
            .where(SignalIter.timestamp >= cutoff)
            .order_by(SignalIter.timestamp.asc())  # type: ignore[attr-defined]
        )).all())

    iters_data = [
        {"spx_price": r.spx_price, "drift_cum_net_m": r.drift_cum_net_m,
         "drift_bias": r.drift_bias, "ntm_max_pain": r.ntm_max_pain,
         "raw_max_pain": r.raw_max_pain,
         # session_date lets run_sweep purge the forward-walk at the overnight
         # boundary (0DTE trades can't be held across sessions).
         "session_date": r.session_date,
         "top_gex_walls": r.top_gex_walls or [],
         "top_dex_walls": r.top_dex_walls or [],
         "top_cex_walls": getattr(r, "top_cex_walls", None) or [],
         "top_vex_walls": getattr(r, "top_vex_walls", None) or [],
         "top_gex_by_expiry": getattr(r, "top_gex_by_expiry", None) or [],
         "top_dex_by_expiry": getattr(r, "top_dex_by_expiry", None) or [],
         "zero_dte_gex_net_m": getattr(r, "zero_dte_gex_net_m", None),
         "zero_dte_dex_net_m": getattr(r, "zero_dte_dex_net_m", None),
         "recent_whale_count_30m": int(getattr(r, "recent_whale_count_30m", 0) or 0),
         "recent_golden_sweep_count_30m": int(getattr(r, "recent_golden_sweep_count_30m", 0) or 0),
         "aggression_call_pct": r.aggression_call_pct or 50,
         "aggression_put_pct": r.aggression_put_pct or 50,
         "iv_rank_call_pct": r.iv_rank_call_pct or 50,
         "iv_rank_put_pct": r.iv_rank_put_pct or 50}
        for r in rows
    ]
    results = run_sweep(iters=iters_data, cfg=cfg) if iters_data else []

    # Pick best by test_sharpe (or test_total_pnl if no sharpe)
    best = None
    ci_overlap_warning: str | None = None
    if results:
        ranked = sorted(results, key=lambda r: (
            r.test_sharpe if r.test_sharpe is not None else -999,
            r.test_total_pnl,
        ), reverse=True)
        best = ranked[0]
        # Check if top-2 Sharpe CIs overlap (difference not significant)
        if len(ranked) >= 2:
            r1, r2 = ranked[0], ranked[1]
            if (r1.sharpe_ci_lo is not None and r2.sharpe_ci_hi is not None
                    and r1.sharpe_ci_lo <= r2.sharpe_ci_hi):
                ci_overlap_warning = (
                    f"Top-2 Sharpe CIs overlap: best=[{r1.sharpe_ci_lo}, {r1.sharpe_ci_hi}] "
                    f"vs runner-up=[{r2.sharpe_ci_lo}, {r2.sharpe_ci_hi}] — "
                    f"difference is NOT statistically significant"
                )
                logger.warning(ci_overlap_warning)

    eng = state.engine
    current = {
        "wall_proximity_pts": eng._wall_proximity_pts,  # type: ignore[attr-defined]
        "wall_collapse_pct": eng._wall_collapse_pct,  # type: ignore[attr-defined]
        "wall_collapse_floor_m": eng._wall_collapse_floor_m,  # type: ignore[attr-defined]
        "absorption_fe_ratio_min": eng._absorption_fe_min,  # type: ignore[attr-defined]
    }

    if best is None:
        run = WalkForwardRun(
            window_days=days,
            current_wall_proximity_pts=current["wall_proximity_pts"],
            current_wall_collapse_pct=current["wall_collapse_pct"],
            current_wall_collapse_floor_m=current["wall_collapse_floor_m"],
            current_absorption_fe_ratio_min=current["absorption_fe_ratio_min"],
            notes="No backtest data for this window",
        )
        await SignalIterRepository.write_walk_forward_run(run)
        return run

    # Drift score: normalized euclidean distance between optimal and current
    drift = 0.0
    drift += abs(best.wall_proximity_pts - current["wall_proximity_pts"]) / max(current["wall_proximity_pts"], 1)
    drift += abs(best.wall_collapse_pct - current["wall_collapse_pct"]) / max(current["wall_collapse_pct"], 0.01)
    drift += abs(best.wall_collapse_floor_m - current["wall_collapse_floor_m"]) / max(current["wall_collapse_floor_m"], 1)
    drift += abs(best.absorption_fe_ratio_min - current["absorption_fe_ratio_min"]) / max(current["absorption_fe_ratio_min"], 1)
    drift /= 4

    notes_parts = []
    if ci_overlap_warning:
        notes_parts.append(ci_overlap_warning)
    if getattr(best, "low_confidence", False):
        notes_parts.append(
            f"LOW CONFIDENCE: best window fired < {MIN_CONFIDENT_TRADES} trades "
            f"(train={best.train_trades}, test={best.test_trades}) — treat "
            f"'optimal' params as noise, do NOT retune on this window."
        )
        logger.warning(
            "Auto walk-forward: best result is low-confidence "
            "(train_trades=%d, test_trades=%d)",
            best.train_trades, best.test_trades,
        )

    run = WalkForwardRun(
        window_days=days,
        current_wall_proximity_pts=current["wall_proximity_pts"],
        current_wall_collapse_pct=current["wall_collapse_pct"],
        current_wall_collapse_floor_m=current["wall_collapse_floor_m"],
        current_absorption_fe_ratio_min=current["absorption_fe_ratio_min"],
        optimal_wall_proximity_pts=best.wall_proximity_pts,
        optimal_wall_collapse_pct=best.wall_collapse_pct,
        optimal_wall_collapse_floor_m=best.wall_collapse_floor_m,
        optimal_absorption_fe_ratio_min=best.absorption_fe_ratio_min,
        optimal_sharpe=best.test_sharpe,
        optimal_total_pnl=best.test_total_pnl,
        optimal_win_rate=best.test_win_rate_pct,
        drift_score=round(drift, 3),
        notes="; ".join(notes_parts) if notes_parts else None,
    )
    await SignalIterRepository.write_walk_forward_run(run)

    await SignalIterRepository.write_audit_log(
        category="auto_walk_forward", actor="system",
        summary=f"Auto walk-forward complete. Drift {drift:.2f} (optimal Sharpe {best.test_sharpe})",
        details={"current": current, "optimal": {
            "wall_proximity_pts": best.wall_proximity_pts,
            "wall_collapse_pct": best.wall_collapse_pct,
            "wall_collapse_floor_m": best.wall_collapse_floor_m,
            "absorption_fe_ratio_min": best.absorption_fe_ratio_min,
            "test_sharpe": best.test_sharpe, "test_total_pnl": best.test_total_pnl,
            "sharpe_ci": [best.sharpe_ci_lo, best.sharpe_ci_hi],
            "ci_overlap_warning": ci_overlap_warning,
        }},
    )
    return run


async def _persist_today_ibkr_fills(state) -> int:
    """Persist today's IBKR executions to DB. Without this, ibkr_execution
    rows only get written when /diag/ibkr_executions is queried — so if no
    UI hits it, today's fills are missing at nightly compute time. Returns
    the count of NEW rows persisted (idempotent on exec_id).

    Also captures CommissionReport.commission + realizedPNL — but ib_async
    delivers those on a SEPARATE event channel that arrives AFTER
    reqExecutionsAsync resolves. The Fill objects in the returned list are
    mutated in place when commissionReport events land, so a brief sleep
    between the request and the read picks up the commissions correctly.
    Reference: ib_async wrapper.py execDetails + commissionReport handlers."""
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import IBKRExecution
    if not isinstance(state.ibkr, LiveIBKRProvider):
        return 0
    try:
        ib = state.ibkr._ib  # type: ignore[attr-defined]
        # Pull recent fills from IBKR (server-side cache, ~7 days max)
        from ib_async import ExecutionFilter
        execs = await ib.reqExecutionsAsync(ExecutionFilter())
        if not execs:
            return 0
        # Wait for CommissionReport events to land (they arrive AFTER the
        # execDetails future resolves — see ib_async wrapper.py:824, 856).
        wait_sec = float(os.environ.get("FEF_COMMISSION_RACE_WAIT_SEC", "2.0"))
        await asyncio.sleep(wait_sec)
        from datetime import timezone as _tz, timedelta as _td
        et_tz = _tz(_td(hours=-4))
        new_count = 0
        async with get_session() as session:
            for ex in execs:
                # Read commission from the Fill's now-updated CommissionReport
                cr = getattr(ex, "commissionReport", None)
                comm_val: float | None = None
                rpnl_val: float | None = None
                if cr is not None:
                    _c = getattr(cr, "commission", None)
                    if _c is not None and not (isinstance(_c, float) and _c != _c):
                        try: comm_val = float(_c)
                        except (TypeError, ValueError): pass
                    _r = getattr(cr, "realizedPNL", None)
                    if _r is not None and not (isinstance(_r, float) and _r != _r):
                        try: rpnl_val = float(_r)
                        except (TypeError, ValueError): pass

                # Skip if already persisted (but try to update commission
                # if previously NULL — handles the case where this exec was
                # captured before the commission-race fix).
                existing = (await session.exec(
                    select(IBKRExecution).where(IBKRExecution.exec_id == ex.execution.execId)
                )).first()
                if existing is not None:
                    changed = False
                    # Retry when commission is None OR 0 — paper accounts
                    # (DUM prefix) report $0 which is correct for them, but
                    # live accounts may later provide the real fee. Accept
                    # the update if the new value is non-None and non-zero.
                    if (existing.commission is None or existing.commission == 0) and comm_val is not None:
                        if existing.commission is None or comm_val != 0:
                            existing.commission = comm_val
                            changed = True
                    if (existing.realized_pnl is None or existing.realized_pnl == 0) and rpnl_val is not None:
                        if existing.realized_pnl is None or rpnl_val != 0:
                            existing.realized_pnl = rpnl_val
                            changed = True
                    if changed:
                        session.add(existing)
                    continue
                ts = ex.time
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_tz.utc)
                sess_date = ts.astimezone(et_tz).strftime("%Y-%m-%d")
                row = IBKRExecution(
                    exec_id=ex.execution.execId,
                    timestamp=ts,
                    session_date=sess_date,
                    symbol=getattr(ex.contract, "symbol", ""),
                    strike=float(getattr(ex.contract, "strike", 0) or 0),
                    right=getattr(ex.contract, "right", "") or "",
                    expiry=getattr(ex.contract, "lastTradeDateOrContractMonth", "") or "",
                    side=ex.execution.side,
                    shares=float(ex.execution.shares),
                    price=float(ex.execution.price),
                    cum_qty=float(ex.execution.cumQty),
                    avg_price=float(ex.execution.avgPrice),
                    ib_order_id=int(getattr(ex.execution, "orderId", 0) or 0) or None,
                    perm_id=int(getattr(ex.execution, "permId", 0) or 0) or None,
                    account=getattr(ex.execution, "acctNumber", "") or "",
                    commission=comm_val,
                    realized_pnl=rpnl_val,
                )
                session.add(row)
                new_count += 1
            await session.commit()
        return new_count
    except Exception:
        logger.exception("_persist_today_ibkr_fills failed")
        return 0


async def _backfill_live_order_fills_for(session_date: str) -> dict:
    """Update live_order.filled_qty / avg_fill_price by ib_order_id match
    against ibkr_execution rows for the given session_date. Same logic as
    the /admin/backfill_live_order_fills endpoint but inlined for the
    nightly chain."""
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import IBKRExecution, LiveOrder

    async with get_session() as ss:
        execs = list((await ss.exec(
            select(IBKRExecution).where(IBKRExecution.session_date == session_date)
        )).all())
        by_order: dict[int, list] = {}
        for e in execs:
            if e.ib_order_id is None:
                continue
            by_order.setdefault(int(e.ib_order_id), []).append(e)
        candidates = list((await ss.exec(select(LiveOrder))).all())
        candidates = [p for p in candidates
                      if p.submitted_at
                      and p.submitted_at.strftime("%Y-%m-%d") == session_date]
        updated = 0
        for p in candidates:
            if p.ib_order_id is None:
                continue
            fills = by_order.get(int(p.ib_order_id))
            if not fills:
                continue
            total = sum(float(f.shares or 0) for f in fills)
            if total <= 0:
                continue
            wpx = sum(float(f.shares or 0) * float(f.price or 0) for f in fills) / total
            new_filled = int(round(total))
            new_px = round(wpx, 4)
            if (p.filled_qty == new_filled and p.avg_fill_price is not None
                    and abs((p.avg_fill_price or 0) - new_px) < 0.005):
                continue
            p.filled_qty = new_filled
            p.avg_fill_price = new_px
            if p.status in ("SUBMITTED", "ACCEPTED", "WORKING", None):
                qt = int(p.qty or 0)
                if qt > 0 and new_filled >= qt:
                    p.status = "FILLED"
                elif new_filled > 0:
                    p.status = "PARTIAL"
            ss.add(p)
            updated += 1
        await ss.commit()
    return {"execs": len(execs), "updated": updated}


async def _run_shadow_exit_replay(session_date: str) -> int:
    """Invoke the shadow exit replay over today's session. Pure data
    observation — never touches live engine code paths. Imports the
    replay script lazily so a syntax break in the research code can't
    crash the nightly chain."""
    import sys as _sys
    from pathlib import Path as _Path
    research_dir = _Path(__file__).resolve().parents[3] / "scripts" / "research"
    if str(research_dir) not in _sys.path:
        _sys.path.insert(0, str(research_dir))
    try:
        from shadow_exit_replay import run_replay  # type: ignore
    except ImportError:
        logger.warning("shadow_exit_replay.py not importable from %s", research_dir)
        return 0
    # Run synchronously in a thread — the replay is a sqlite-bound CPU
    # loop, not I/O bound, so no async wins available.
    result = await asyncio.to_thread(
        run_replay, session_date, session_date, persist=True
    )
    return int(result.get("n_outcome_rows", 0))


async def _audit_data_integrity(session_date: str) -> dict:
    """Lightweight data-integrity audit for the given session. Logs any
    anomalies to audit_log so the user sees them in the morning before
    drawing conclusions from the data. Read-only."""
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import SignalIter, IBKRExecution
    from fef_sidecar.db.repository import SignalIterRepository

    findings: list[str] = []
    async with get_session() as ss:
        # 1. SignalIter spx_price out of plausible range?
        bad_spx = list((await ss.exec(
            select(SignalIter).where(SignalIter.session_date == session_date)
            .where((SignalIter.spx_price < 1000) | (SignalIter.spx_price > 20000))  # type: ignore[operator]
        )).all())
        if bad_spx:
            findings.append(f"{len(bad_spx)} signal_iter rows have implausible spx_price")
        # 2. IBKR execs without ib_order_id (rare, means we can't backfill)
        execs = list((await ss.exec(
            select(IBKRExecution).where(IBKRExecution.session_date == session_date)
        )).all())
        execs_no_order = [e for e in execs if e.ib_order_id is None]
        if execs_no_order:
            findings.append(f"{len(execs_no_order)}/{len(execs)} IBKR fills missing ib_order_id")
        # 3. IBKR fills present at all?
        if not execs:
            findings.append("NO IBKR fills persisted for this session — check connection / write-through")
        # 4. ORPHAN BUYs — BOT execs with no matching parent live_order on
        # same contract within ±60s of fill. Discovered 2026-05-22: 2 untracked
        # BUYs (ib_order_id IS NULL) added 3 SPX 7520C contracts at 16:17 with
        # no parent — likely residue from a pre-safe_market_sell naked-short
        # cover. The structural patch prevents new naked-shorts, but this
        # detector surfaces any future occurrence (covers, manual flattens,
        # external orders) before it pollutes attribution.
        from fef_sidecar.db.models import LiveOrder
        bot_execs = [e for e in execs if (e.side or "").upper() == "BOT"]
        if bot_execs:
            # LiveOrder has no session_date column — filter by submitted_at ET-day.
            from datetime import timedelta as _td2
            day_start = datetime.fromisoformat(session_date).replace(hour=13, minute=0, tzinfo=UTC)
            day_end = day_start + _td2(hours=12)
            parents = list((await ss.exec(
                select(LiveOrder).where(LiveOrder.action == "BUY")  # type: ignore[arg-type]
                .where(LiveOrder.submitted_at >= day_start)  # type: ignore[arg-type]
                .where(LiveOrder.submitted_at <= day_end)  # type: ignore[arg-type]
            )).all())
            orphan_buys: list[dict] = []
            for e in bot_execs:
                # Candidate parent: same contract + within 60s of exec timestamp
                e_ts = e.timestamp
                candidates = [
                    p for p in parents
                    if p.symbol == e.symbol
                    and (p.strike or 0) == (e.strike or 0)
                    and (p.right or "") == (e.right or "")
                    and (p.expiry or "") == (e.expiry or "")
                    and abs((p.submitted_at - e_ts).total_seconds()) <= 60.0
                ]
                if not candidates:
                    orphan_buys.append({
                        "ibkr_execution_id": e.id,
                        "ib_order_id": e.ib_order_id,
                        "contract": f"{e.symbol} {e.strike}{e.right} {e.expiry}",
                        "qty": e.shares,
                        "timestamp": e_ts.isoformat(),
                    })
            if orphan_buys:
                findings.append(
                    f"{len(orphan_buys)} ORPHAN BUY exec(s) (no parent live_order within ±60s) — "
                    f"possible cover/flatten/external residue; first: {orphan_buys[0]['contract']}"
                )

    summary = (
        "data_integrity OK" if not findings
        else "data_integrity: " + " | ".join(findings)
    )
    details: dict = {"findings": findings, "session_date": session_date}
    if 'orphan_buys' in locals() and orphan_buys:
        details["orphan_buys"] = orphan_buys
    await SignalIterRepository.write_audit_log(
        category="data_integrity", actor="system",
        summary=summary, details=details,
    )
    return {"findings": findings, "ok": not findings}


async def nightly_scheduler(state) -> None:
    """Nightly DATA HYGIENE + COLLECTOR chain. Despite living in a module
    named auto_walk_forward, this task no longer runs any optimisation —
    see the removal note at steps 4/6 below. It is misnamed for historical
    reasons; the module keeps its name only because renaming it would
    churn imports for no behavioural gain.

    Runs, in order:
      1. Persist today's IBKR fills (no UI hit required)
      2. Backfill live_order.filled_qty / avg_fill_price
      3. Data integrity audit (logs anomalies)
      4. [REMOVED 2026-07-30 — was the auto walk-forward sweep]
      5. compute_daily_review (consumes the fresh IBKR + live_order data)
      6. [REMOVED 2026-07-30 — was the tuning recommender]
      7. Shadow exit replay
      8. Option-chain backfill from QuantData
      9+ forward-shadow scorers

    Steps 1-3 are what make everything after them trustworthy: they ensure
    the later steps operate on up-to-date data even when no UI session
    triggered the write-throughs during the trading day. That is why this
    task must never be disabled wholesale to stop the retired optimiser —
    doing so would silently take IBKR fill persistence and daily_review
    with it.

    SCHEDULE: target_hour is interpreted in UTC. The default 22 therefore
    fires at 18:00 ET during EDT and 17:00 ET during EST — NOT "10pm" as
    the retired Research panel's help text used to claim."""
    # FEF_AUTO_WALKFORWARD_HOUR is a legacy name (no walk-forward remains).
    # The new name wins; the old one is still honoured so an existing
    # environment does not silently change schedule under the operator.
    _hour_env = os.environ.get("FEF_NIGHTLY_HOUR_UTC")
    if _hour_env is None:
        _hour_env = os.environ.get("FEF_AUTO_WALKFORWARD_HOUR", "22")
    target_hour = int(_hour_env)
    logger.info(
        "Nightly scheduler started (fires %02d:00 UTC daily = %02d:00 ET in EDT)",
        target_hour, (target_hour - 4) % 24,
    )
    while True:
        now = datetime.now(UTC)
        target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target = target + timedelta(days=1)
        sleep_seconds = (target - now).total_seconds()
        logger.info("Nightly scheduler sleeping %.0f sec until next run", sleep_seconds)
        try:
            await asyncio.sleep(sleep_seconds)

            # DST-AWARE (2026-07-30). Was a hardcoded UTC-4, i.e. EDT
            # year-round, which mislabels the session date in winter. Same
            # defect class the clockflow study found in historical_bar.
            from zoneinfo import ZoneInfo
            session_date = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

            # 1. Persist today's IBKR fills (no-op if already current)
            try:
                n = await _persist_today_ibkr_fills(state)
                logger.info("Nightly: persisted %d new IBKR fills", n)
            except Exception:
                logger.exception("Nightly: persist IBKR fills failed")
            # 2. Backfill live_order.filled_qty / avg_fill_price
            try:
                r = await _backfill_live_order_fills_for(session_date)
                logger.info("Nightly: live_order fills backfill: %s", r)
            except Exception:
                logger.exception("Nightly: live_order fills backfill failed")
            # 3. Data integrity audit
            try:
                r = await _audit_data_integrity(session_date)
                logger.info("Nightly: data integrity: %s", r)
            except Exception:
                logger.exception("Nightly: data integrity audit failed")

            # 4. [REMOVED 2026-07-30] Walk-forward sweep.
            # 6. [REMOVED 2026-07-30] Tuning recommender.
            #
            # Both were retired with the Research-tab teardown. They were not
            # merely unused-by-the-UI; they were actively misleading. The
            # sweep's "optimal" wall_proximity came back 3, then 5, then 8 on
            # consecutive sessions searching the same four knobs — noise
            # rendered as a recommendation. Each run also wrote an
            # engine_change row (category="auto_walk_forward", actor="system"),
            # polluting the one audit trail we use to answer "did something
            # change underneath this session?".
            #
            # The deeper reason they cannot be repaired in place: a nightly
            # job cannot carry the discipline that makes a parameter search
            # trustworthy — no pre-registered lock, no control, no train/test
            # split, no multiple-comparison correction, no record of how many
            # cells were searched. See fef-research-archive/RESEARCH_REGISTRY.md
            # section K0 and RESEARCH_TAB_TEARDOWN.md.
            #
            # DO NOT "restore the nightly optimizer" without reading those.
            # Steps 1/2/3/5 below are data hygiene and stay — daily_review in
            # particular is the one figure the 2026-07-01 audit still trusts,
            # and killing this task wholesale would have taken IBKR fill
            # persistence down with it.

            # 5. Daily review (uses the fresh IBKR + live_order data above)
            try:
                from fef_sidecar.engine.daily_review import compute_daily_review
                await compute_daily_review(session_date)
            except Exception:
                logger.exception("Nightly: daily_review compute failed")

            # 7. Shadow exit replay — compute counterfactual outcomes for
            # every registered exit variant against today's live entries.
            # Pure read-only-on-engine; writes to shadow_exit_outcome only.
            # Lets us track what B+C+D / alternative exit logic WOULD have
            # done on real entries without touching the live exit path.
            try:
                n = await _run_shadow_exit_replay(session_date)
                logger.info("Nightly: shadow_exit_replay produced %d rows", n)
            except Exception:
                logger.exception("Nightly: shadow_exit_replay failed")

            # 8. Option chain backfill from QuantData — fills in price-only
            # rows for ATM±25pt strike grid so alternate-strike shadow
            # variants have data even when the live IBKR capture loop
            # didn't run (e.g., sidecar started post-session, or a strike
            # outside the live ±25 range becomes interesting later).
            # Idempotent against the live capture's IBKR rows via UNIQUE
            # index — no duplicates even if both ran on the same minute.
            try:
                from fef_sidecar.engine.option_chain_backfill import (
                    backfill_session_chains_quantdata,
                )
                n = await backfill_session_chains_quantdata(state, session_date)
                logger.info("Nightly: backfilled %d chain points via QuantData", n)
            except Exception:
                logger.exception("Nightly: chain backfill failed")

            # 9. vwap_loss_short forward-shadow scoring (Phase 1, paper). Scores
            # the FROZEN candidate on today's ES RTH bars + persists to
            # shadow_forward_signal; registers in variant_ledger for honest
            # N_trials. Read-only on the engine. See engine/signals/
            # shadow_vwap_loss_short.py.
            try:
                from fef_sidecar.engine.signals import shadow_vwap_loss_short as _svls
                _bars = await _svls.load_es_rth_bars()
                _added = await _svls.persist_outcomes(_svls.score_session(_bars))
                await _svls.register_variant()
                logger.info("Nightly: vwap_loss_short shadow +%d rows", _added)
            except Exception:
                logger.exception("Nightly: vwap_loss_short shadow failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Nightly scheduler iteration failed")
            await asyncio.sleep(3600)  # retry in 1h on failure
