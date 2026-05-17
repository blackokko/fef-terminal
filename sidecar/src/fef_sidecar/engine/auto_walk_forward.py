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
    from fef_sidecar.engine.optimization import SweepConfig, run_sweep

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
         "top_gex_walls": r.top_gex_walls or [],
         "top_dex_walls": r.top_dex_walls or [],
         "aggression_call_pct": r.aggression_call_pct or 50,
         "aggression_put_pct": r.aggression_put_pct or 50,
         "iv_rank_call_pct": r.iv_rank_call_pct or 50,
         "iv_rank_put_pct": r.iv_rank_put_pct or 50}
        for r in rows
    ]
    results = run_sweep(iters=iters_data, cfg=cfg) if iters_data else []

    # Pick best by test_sharpe (or test_total_pnl if no sharpe)
    best = None
    if results:
        best = max(results, key=lambda r: (
            r.test_sharpe if r.test_sharpe is not None else -999,
            r.test_total_pnl,
        ))

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
        }},
    )
    return run


async def nightly_scheduler(state) -> None:
    """Sleeps until the configured hour each day, then runs auto walk-forward."""
    target_hour = int(os.environ.get("FEF_AUTO_WALKFORWARD_HOUR", "22"))
    logger.info("Auto walk-forward scheduler started (will run at %02d:00 UTC daily)", target_hour)
    while True:
        now = datetime.now(UTC)
        target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target = target + timedelta(days=1)
        sleep_seconds = (target - now).total_seconds()
        logger.info("Auto walk-forward sleeping %.0f sec until next run", sleep_seconds)
        try:
            await asyncio.sleep(sleep_seconds)
            await run_auto_walk_forward(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auto walk-forward iteration failed")
            await asyncio.sleep(3600)  # retry in 1h on failure
