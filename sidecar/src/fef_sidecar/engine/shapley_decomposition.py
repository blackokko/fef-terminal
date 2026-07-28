"""Shapley-style edge decomposition.

Splits paper P/L into three additive components — signal, execution,
stacking — plus an interaction term. Tells you what FRACTION of paper
edge each component contributes, which determines what fraction will
survive the paper-to-live transition.

Methodology (per Claude Chat 2026-05-20 review):
  Run A: single-fire only (no stacking) + pure SPX-target exits
         (no BE ladder, no watcher) + realistic fills
  Run B: single-fire + BE ladder + watcher + realistic fills
  Run C: stacking enabled + pure SPX-target exits + realistic fills
  Run D: full system + realistic fills (actual realized)

Components:
  Signal      = Run A                            (pure direction-prediction)
  Execution   = Run B − Run A                    (BE ladder + watcher catches)
  Stacking    = Run C − Run A                    (stacking on conviction)
  Interaction = Run D − (Run A + (B−A) + (C−A))  (how components combine)

Live-survival rates (Claude Chat estimate):
  Signal:      80-90% survives transition
  Stacking:    70-85% if signal-driven, 40-60% if execution-driven
  Execution:   30-60% survives, sometimes worse
  Interaction: ~60% of the underlying mix

Projected live edge = sum(component × survival_rate)

This analysis is intended to be run ONCE post-freeze on the full
10-session dataset. Running on smaller samples produces directional
insight but estimates are noisy.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import timezone

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import HistoricalBar, IBKRExecution, LiveOrder, SignalIter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SurvivalRates:
    """Live-survival rates per component (Claude Chat estimates)."""

    signal_survival: float = 0.85
    stacking_survival: float = 0.65  # midpoint between signal-driven (0.75) and execution-driven (0.55)
    execution_survival: float = 0.45  # midpoint of 0.30-0.60 range
    interaction_survival: float = 0.60


def _utc_ms(dt) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _walk_pure_spx(direction: str, entry_spx: float, t1: float, t2: float | None,
                   t3: float | None, stop: float, fwd_bars: list) -> float:
    """Pure SPX-target outcome (no BE ladder). Returns R-multiple."""
    risk = abs(entry_spx - stop)
    if risk <= 0:
        return 0.0
    tiers_hit: list[str] = []
    stopped = False
    for b in fwd_bars:
        if direction == "LONG_CALLS":
            if b.low <= stop: stopped = True; break
            if b.high >= t1 and "T1" not in tiers_hit: tiers_hit.append("T1")
            if t2 and b.high >= t2 and "T2" not in tiers_hit: tiers_hit.append("T2")
            if t3 and b.high >= t3 and "T3" not in tiers_hit: tiers_hit.append("T3")
        else:
            if b.high >= stop: stopped = True; break
            if b.low <= t1 and "T1" not in tiers_hit: tiers_hit.append("T1")
            if t2 and b.low <= t2 and "T2" not in tiers_hit: tiers_hit.append("T2")
            if t3 and b.low <= t3 and "T3" not in tiers_hit: tiers_hit.append("T3")
        if len(tiers_hit) >= 3: break
    tier_r = {"T1": 1.5, "T2": 2.5, "T3": 5.0}
    r = sum(tier_r[t] for t in tiers_hit) / 3.0
    if stopped:
        r += -1.0 * (3 - len(tiers_hit)) / 3.0
    return r


def _walk_be_applied(direction: str, entry_spx: float, t1: float, t2: float | None,
                     t3: float | None, stop: float, fwd_bars: list) -> float:
    """Current BE ladder applied. Returns R-multiple."""
    risk = abs(entry_spx - stop)
    if risk <= 0:
        return 0.0
    from fef_sidecar.engine.spx_target_watcher import _BE_LADDER_STEPS
    BE_LADDER = _BE_LADDER_STEPS  # shared live schedule (2026-07-28 adoption)
    dyn_stop = stop
    mfe_r_max = 0.0
    tiers_hit: list[str] = []
    stopped = False
    locked_r = 0.0

    for b in fwd_bars:
        if direction == "LONG_CALLS":
            mfe_r_max = max(mfe_r_max, (b.high - entry_spx) / risk)
            for trigger_r, lock_r in BE_LADDER:
                if mfe_r_max >= trigger_r:
                    new_stop = entry_spx + lock_r * risk
                    if new_stop > dyn_stop:
                        dyn_stop = new_stop
                        locked_r = lock_r
            if b.low <= dyn_stop:
                stopped = True
                break
            if b.high >= t1 and "T1" not in tiers_hit: tiers_hit.append("T1")
            if t2 and b.high >= t2 and "T2" not in tiers_hit: tiers_hit.append("T2")
            if t3 and b.high >= t3 and "T3" not in tiers_hit: tiers_hit.append("T3")
        else:
            mfe_r_max = max(mfe_r_max, (entry_spx - b.low) / risk)
            for trigger_r, lock_r in BE_LADDER:
                if mfe_r_max >= trigger_r:
                    new_stop = entry_spx - lock_r * risk
                    if new_stop < dyn_stop:
                        dyn_stop = new_stop
                        locked_r = lock_r
            if b.high >= dyn_stop:
                stopped = True
                break
            if b.low <= t1 and "T1" not in tiers_hit: tiers_hit.append("T1")
            if t2 and b.low <= t2 and "T2" not in tiers_hit: tiers_hit.append("T2")
            if t3 and b.low <= t3 and "T3" not in tiers_hit: tiers_hit.append("T3")
        if len(tiers_hit) >= 3: break

    tier_r = {"T1": 1.5, "T2": 2.5, "T3": 5.0}
    r = sum(tier_r[t] for t in tiers_hit) / 3.0
    if stopped:
        r += locked_r * (3 - len(tiers_hit)) / 3.0
    return r


async def run_decomposition(
    session_dates: list[str],
    realistic_fill_haircut_pct: float = 0.20,
    survival: SurvivalRates | None = None,
) -> dict:
    """Run the 4-cell Shapley decomposition on the given sessions.

    realistic_fill_haircut_pct: applied uniformly to gross simulated P/L
    as a stand-in for full bid/ask + latency + commission modeling.
    20% is the moderate estimate.

    Returns paper $ per component + live-survival-adjusted projection.
    """
    survival = survival or SurvivalRates()

    async with get_session() as session:
        sigs = list((await session.exec(
            select(SignalIter)
            .where(SignalIter.session_date.in_(session_dates))  # type: ignore[attr-defined]
            .where(SignalIter.direction != "WAIT")  # type: ignore[attr-defined]
            .order_by(SignalIter.timestamp.asc())  # type: ignore[attr-defined]
        )).all())
        # Filter bars to relevant session dates only (previously loaded ALL
        # bars in the DB, which is wasteful and slow on large datasets).
        # HistoricalBar has no session_date column — compute ms bounds from
        # the date strings with 1-hour buffer on each side.
        from datetime import datetime as _dt, timedelta as _td
        _sorted_dates = sorted(session_dates)
        _bar_start_ms = int(_dt.fromisoformat(_sorted_dates[0]).replace(
            tzinfo=timezone.utc).timestamp() * 1000) - 3_600_000
        _bar_end_ms = int(_dt.fromisoformat(_sorted_dates[-1]).replace(
            tzinfo=timezone.utc).timestamp() * 1000) + 24 * 3_600_000 + 3_600_000
        bars = list((await session.exec(
            select(HistoricalBar)
            .where(HistoricalBar.symbol == "SPX")
            .where(HistoricalBar.bar_size == "1 min")
            .where(HistoricalBar.timestamp_ms >= _bar_start_ms)  # type: ignore[attr-defined]
            .where(HistoricalBar.timestamp_ms <= _bar_end_ms)  # type: ignore[attr-defined]
            .order_by(HistoricalBar.timestamp_ms.asc())  # type: ignore[attr-defined]
        )).all())
        parents = list((await session.exec(
            select(LiveOrder)
            .where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.dry_run == False)  # noqa: E712
        )).all())
        execs = list((await session.exec(
            select(IBKRExecution).where(IBKRExecution.session_date.in_(session_dates))  # type: ignore[attr-defined]
        )).all())

    parents = [p for p in parents
               if p.submitted_at.strftime("%Y-%m-%d") in session_dates]

    # Run D: actual P/L from real IBKR fills
    by_contract: dict[tuple, dict] = defaultdict(lambda: {"bv": 0.0, "sv": 0.0, "bq": 0.0, "sq": 0.0})
    for f in execs:
        key = (f.symbol, f.strike, f.right, f.expiry)
        slot = by_contract[key]
        if f.side == "BOT":
            slot["bv"] += f.shares * f.price
            slot["bq"] += f.shares
        else:
            slot["sv"] += f.shares * f.price
            slot["sq"] += f.shares
    run_d_paper = 0.0
    for slot in by_contract.values():
        paired = min(slot["bq"], slot["sq"])
        if paired <= 0: continue
        avg_b = slot["bv"] / slot["bq"] if slot["bq"] else 0
        avg_s = slot["sv"] / slot["sq"] if slot["sq"] else 0
        run_d_paper += (avg_s - avg_b) * paired * 100.0

    # Helper: get qty for a signal from matched parent (within 60s window)
    def qty_for_signal(sg) -> int:
        direction = sg.direction.value if hasattr(sg.direction, "value") else str(sg.direction)
        right = "C" if direction == "LONG_CALLS" else "P"
        ts_ms = _utc_ms(sg.timestamp)
        for p in parents:
            if p.right != right: continue
            pms = _utc_ms(p.submitted_at)
            if 0 <= pms - ts_ms <= 60_000:
                # Check strike match if available
                strike_rec = (sg.extras or {}).get("strike_rec")
                if strike_rec is None or abs(p.strike - float(strike_rec)) < 0.5:
                    return p.qty
        return 5  # default fallback

    def entry_premium_for_signal(sg) -> float:
        import math as _m
        opt = (sg.extras or {}).get("option_pricing") or {}
        raw = opt.get("entry_limit")
        try:
            v = float(raw) if raw is not None else 4.0
            if not _m.isfinite(v) or v <= 0:
                return 4.0  # fallback when paper option_pricing is missing/NaN
            return v
        except (TypeError, ValueError):
            return 4.0

    # For each signal, simulate outcome under each exit strategy
    # convert R-multiple to dollars: pnl_$ = R × risk_$ where risk_$ ≈
    # entry_premium × qty × $100 (treating "1R" as full premium-at-risk
    # since that's how the option-price translates from SPX-distance R
    # in practice for SPX 0DTE).
    run_a_paper = 0.0  # single-fire + pure SPX
    run_b_paper = 0.0  # single-fire + BE
    run_c_paper = 0.0  # all + pure SPX
    n_a = n_b = n_c = 0

    for sg in sigs:
        ext = sg.extras or {}
        t1 = ext.get("t1"); t2 = ext.get("t2"); t3 = ext.get("t3"); stop = ext.get("stop")
        if not all([t1, stop]):
            continue
        direction = sg.direction.value if hasattr(sg.direction, "value") else str(sg.direction)
        ts_ms = _utc_ms(sg.timestamp)
        fwd = [b for b in bars if b.timestamp_ms >= ts_ms]
        if not fwd:
            continue

        import math as _m
        r_pure = _walk_pure_spx(direction, sg.spx_price, t1, t2, t3, stop, fwd)
        r_be = _walk_be_applied(direction, sg.spx_price, t1, t2, t3, stop, fwd)
        if not (_m.isfinite(r_pure) and _m.isfinite(r_be)):
            continue
        qty = qty_for_signal(sg)
        entry_prem = entry_premium_for_signal(sg)
        risk_dollars = entry_prem * qty * 100.0
        if not _m.isfinite(risk_dollars) or risk_dollars <= 0:
            continue
        pnl_pure = r_pure * risk_dollars
        pnl_be = r_be * risk_dollars

        fire_order = ext.get("fire_order")
        is_first_fire = (fire_order == 1)

        # Run C: all signals, pure SPX
        run_c_paper += pnl_pure
        n_c += 1

        # Runs A + B: only first fires
        if is_first_fire:
            run_a_paper += pnl_pure
            run_b_paper += pnl_be
            n_a += 1
            n_b += 1

    # Apply realistic-fill haircut to simulated runs only (A, B, C).
    # Run D uses actual IBKR fills which already include real spread,
    # commission, and slippage — haircutting it double-counts friction.
    haircut = 1.0 - realistic_fill_haircut_pct
    run_a = run_a_paper * haircut
    run_b = run_b_paper * haircut
    run_c = run_c_paper * haircut
    run_d = run_d_paper  # no haircut — already real fills

    # Decomposition
    signal_component = run_a
    execution_component = run_b - run_a
    stacking_component = run_c - run_a
    interaction_component = run_d - (run_a + (run_b - run_a) + (run_c - run_a))

    # Live-survival adjustment
    live_signal = signal_component * survival.signal_survival
    live_execution = execution_component * survival.execution_survival
    live_stacking = stacking_component * survival.stacking_survival
    live_interaction = interaction_component * survival.interaction_survival
    live_total_projected = live_signal + live_execution + live_stacking + live_interaction

    # Component percentages
    total_paper = abs(run_d) or 1.0  # use abs to avoid div-by-zero / negative weirdness

    return {
        "session_dates": session_dates,
        "n_signals_total": n_c,
        "n_first_fires": n_a,
        "realistic_fill_haircut_pct": realistic_fill_haircut_pct,
        "runs_paper": {
            "run_a_signal_only": round(run_a, 2),
            "run_b_signal_plus_be": round(run_b, 2),
            "run_c_signal_plus_stacking": round(run_c, 2),
            "run_d_actual": round(run_d, 2),
        },
        "decomposition_paper": {
            "signal": round(signal_component, 2),
            "execution": round(execution_component, 2),
            "stacking": round(stacking_component, 2),
            "interaction": round(interaction_component, 2),
        },
        "decomposition_pct_of_actual": {
            "signal_pct": round(signal_component / total_paper * 100, 1),
            "execution_pct": round(execution_component / total_paper * 100, 1),
            "stacking_pct": round(stacking_component / total_paper * 100, 1),
            "interaction_pct": round(interaction_component / total_paper * 100, 1),
        },
        "survival_rates_used": {
            "signal": survival.signal_survival,
            "stacking": survival.stacking_survival,
            "execution": survival.execution_survival,
            "interaction": survival.interaction_survival,
        },
        "live_projection_dollars": {
            "from_signal": round(live_signal, 2),
            "from_execution": round(live_execution, 2),
            "from_stacking": round(live_stacking, 2),
            "from_interaction": round(live_interaction, 2),
            "total_projected_live": round(live_total_projected, 2),
        },
        "paper_to_live_ratio": round(live_total_projected / run_d, 3) if run_d else 0,
        "note": (
            "Approximations: realistic-fill haircut applied as flat pct (real "
            "modeling per-contract via spread + latency in realistic_fill.py); "
            "R-to-dollar conversion uses entry_premium × qty × $100 as risk_$ "
            "proxy; survival rates are Claude Chat estimates not empirical. "
            "Best interpretation is RELATIVE component shares + sign, not "
            "absolute dollar magnitudes."
        ),
    }
