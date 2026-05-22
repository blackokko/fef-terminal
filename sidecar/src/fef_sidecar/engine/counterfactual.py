"""Counterfactual analytics: two distinct simulators.

1. EXIT-STRATEGY counterfactual (`simulate_one`, `run_for_session`):
   Replay each closed/stopped trade through hypothetical B+C+D exits.
   Answers "what would today's P/L be with option-price take-profits +
   trailing peak + time-decay scaling?"

2. POLLING-INTERVAL counterfactual (`simulate_polling_interval`,
   `run_polling_simulation`): Replay the FEF engine over today's
   FlowSnapshots at a finer cadence (e.g., 30s or 60s instead of 120s)
   and simulate trade outcomes via 1-min SPX bars. Answers "if we
   polled twice as often, how many MORE actionable signals would have
   fired, and what would the WR%/expectancy look like?"

Both are pure analytics — no DB writes, no state mutation, no engine
impact. Freeze-safe.

Lets us measure "what would today's PnL be with option-price take-profits
+ trailing peak + time-decay scaling, vs the current SPX-target + BE
ladder?" The result quantifies how much exit-strategy improvement would
add to realized PnL, independent of any signal-strength change.

Approximation method: we don't have continuous option-price paths
persisted, so we use BE step reached as a proxy for option MFE:

    BE step 0.0R     → option never went positive
    BE step 0.5R     → option likely peaked ~+30% (conservative)
    BE step 1.0R     → option likely peaked ~+60%
    BE step 1.5R     → option likely peaked ~+100%
    BE step ≥2.0R    → option likely peaked ~+150%

The relationship between SPX-points-R and option-price-percent depends
on delta, gamma, theta, vol — which we DO capture in
SignalIter.extras.option_pricing for trades placed after 2026-05-20.
TODO: when option Greeks at fire become available across the freeze
window, replace the lookup table with a delta-driven projection.

Variant simulated: B + C + D combined (the user's chosen target):
  - 1/3 qty exits at +30% option gain (T1) — or +20%/+15% if late session
  - 1/3 qty exits at +75% option gain (T2)
  - 1/3 qty exits at "peak - 15%" via trailing lock
  - If T2 doesn't hit, that third joins the trail
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CounterfactualConfig:
    """Tunable parameters for the simulated B+C+D exit strategy."""

    t1_option_gain: float = 0.30      # +30% option gain
    t2_option_gain: float = 0.75      # +75% option gain
    peak_drawdown_pct: float = 0.15   # exit at peak × 0.85

    # Time-decay scaling (variant D): T1 shrinks late in the session.
    # Buckets are ET hour ranges; T1 gain reduces accordingly.
    t1_late_session_gain: float = 0.20   # post 1pm CT (2pm ET)
    t1_power_hour_gain: float = 0.15     # post 2pm CT (3pm ET)


@dataclass(frozen=True)
class FrictionModel:
    """Estimated paper-to-live haircuts. Tunable; replace estimates with
    measured live data once available.

    All slippage values are dollars-per-contract for the *additional*
    cost above what paper trading already reflects. Per-side: entry +
    exit are independent. Commission is per-contract round-trip.

    The scenarios:
      - BEST: assume paper-accurate fills + commissions only
      - REALISTIC: modest extra slippage above paper (most common case)
      - WORST: heavy slippage + adverse selection + latency drag
    """

    commission_per_contract_rt: float = 1.30   # $0.65 × 2 (entry + exit)

    # Per-contract extra slippage on top of what paper already shows.
    realistic_entry_slip: float = 0.10  # $0.10/contract worse than paper
    realistic_exit_slip: float = 0.10
    worst_entry_slip: float = 0.20
    worst_exit_slip: float = 0.20

    # Worst-case extras (only applied in worst scenario)
    worst_adverse_selection_per_session: float = 2000.0
    worst_latency_drag_per_session: float = 1000.0

    # For B+C+D the scale-out fires up to 3 exits per position instead
    # of ~1, so exit-side friction multiplies accordingly. The
    # `exit_event_multiplier_bcd` captures this — current BE = 1.0,
    # B+C+D = 1.5 (1 trail + 1 T1 partial + occasional T2).
    exit_event_multiplier_bcd: float = 1.5


def _apply_friction(
    paper_pnl: float,
    n_trades: int,
    avg_qty_per_trade: float,
    *,
    is_bcd: bool,
    fm: FrictionModel,
) -> dict[str, float]:
    """Return {best_live, realistic_live, worst_live} P/L estimates given
    a paper P/L and trade-volume context.

    n_trades = total trades (entries)
    avg_qty_per_trade = avg contracts per trade
    is_bcd = True applies the exit-event multiplier for tiered exits
    """
    total_contracts = n_trades * avg_qty_per_trade
    commission_total = total_contracts * fm.commission_per_contract_rt

    # Exit-event multiplier reflects how many exit-side fills occur
    # per trade (current BE = ~1, B+C+D = ~1.5 due to scale-outs)
    exit_mult = fm.exit_event_multiplier_bcd if is_bcd else 1.0

    # Best case: paper minus commissions only
    best = paper_pnl - commission_total

    # Realistic: modest extra slippage on entry and exit
    realistic_entry = total_contracts * fm.realistic_entry_slip * 100
    realistic_exit = total_contracts * fm.realistic_exit_slip * 100 * exit_mult
    realistic = paper_pnl - commission_total - realistic_entry - realistic_exit

    # Worst: heavy slippage plus adverse selection + latency
    worst_entry = total_contracts * fm.worst_entry_slip * 100
    worst_exit = total_contracts * fm.worst_exit_slip * 100 * exit_mult
    worst = (paper_pnl - commission_total - worst_entry - worst_exit
             - fm.worst_adverse_selection_per_session
             - fm.worst_latency_drag_per_session)

    return {
        "best_live": round(best, 2),
        "realistic_live": round(realistic, 2),
        "worst_live": round(worst, 2),
        "friction_breakdown": {
            "commission_total": round(commission_total, 2),
            "realistic_entry_friction": round(realistic_entry, 2),
            "realistic_exit_friction": round(realistic_exit, 2),
            "worst_entry_friction": round(worst_entry, 2),
            "worst_exit_friction": round(worst_exit, 2),
            "worst_adverse_selection": fm.worst_adverse_selection_per_session,
            "worst_latency_drag": fm.worst_latency_drag_per_session,
        },
    }


def _be_step_to_option_mfe(be_step: float | None) -> float:
    """Map BE step reached (SPX R-multiple) to estimated option-price MFE."""
    if be_step is None or be_step <= 0:
        return 0.0
    if be_step <= 0.5:
        return 0.30
    if be_step <= 1.0:
        return 0.60
    if be_step <= 1.5:
        return 1.00
    return 1.50


def _t1_gain_for_time(hour_ct: int, cfg: CounterfactualConfig) -> float:
    """Variant D — T1 target percentage shrinks as session progresses."""
    if hour_ct >= 14:  # 2pm CT or later
        return cfg.t1_power_hour_gain
    if hour_ct >= 13:  # 1pm CT or later
        return cfg.t1_late_session_gain
    return cfg.t1_option_gain


def simulate_one(parent: dict, cfg: CounterfactualConfig | None = None) -> dict:
    """Return counterfactual P/L estimate for one audit parent record.

    Expected parent shape matches /diag/signal_execution_audit output:
      parent = {
        "parent_id": int,
        "submitted_at": ISO timestamp,
        "qty_filled": int,
        "actual": {"avg_buy_price": float},
        "signal": {"be_step_reached_R": float},
        ...
      }
    """
    if cfg is None:
        cfg = CounterfactualConfig()

    sig = parent.get("signal", {})
    actual = parent.get("actual", {})

    qty = int(parent.get("qty_filled", 0) or 0)
    entry_px = float(actual.get("avg_buy_price", 0) or 0)
    be_step = float(sig.get("be_step_reached_R", 0) or 0)
    if qty == 0 or entry_px == 0:
        return {"counterfactual_pnl_dollars": 0.0, "trigger_breakdown": {},
                "option_mfe_estimate": 0.0, "unchanged": True}

    mfe = _be_step_to_option_mfe(be_step)
    if mfe == 0.0:
        # Never went positive — current stop and counterfactual stop both
        # take the full loss on entry × stop_distance.
        return {"counterfactual_pnl_dollars": None, "trigger_breakdown": {},
                "option_mfe_estimate": 0.0, "unchanged": True,
                "note": "clean stop, no profit ever — counterfactual identical to actual"}

    # Variant D: pull T1 threshold based on time-of-day
    hour_ct = 12  # default if no timestamp parseable
    submitted = parent.get("submitted_at", "")
    if submitted:
        try:
            # submitted_at is UTC ISO; CT is UTC-5 (or -6 in winter — both fine for our purposes)
            from datetime import datetime
            dt = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
            hour_ct = (dt.hour - 5) % 24
        except Exception:
            pass
    t1_threshold = _t1_gain_for_time(hour_ct, cfg)

    third = qty // 3
    rest = qty - 2 * third  # remainder for last tier

    pnl_t1 = 0.0
    pnl_t2 = 0.0
    pnl_trail = 0.0
    triggers: dict[str, dict] = {}

    # T1: did the option reach the threshold?
    if mfe >= t1_threshold:
        pnl_t1 = third * entry_px * t1_threshold * 100
        triggers["t1"] = {"qty": third, "gain_pct": t1_threshold, "dollars": pnl_t1}

    # T2: option-price target at +75% (variant B)
    if mfe >= cfg.t2_option_gain:
        pnl_t2 = third * entry_px * cfg.t2_option_gain * 100
        triggers["t2"] = {"qty": third, "gain_pct": cfg.t2_option_gain, "dollars": pnl_t2}
    else:
        # T2 didn't hit; that third joins the trail
        rest = qty - third if mfe >= t1_threshold else qty

    # Trailing peak lock (variant C): lock at peak × (1 - drawdown_pct)
    if rest > 0 and mfe > 0:
        trail_gain = mfe * (1 - cfg.peak_drawdown_pct)
        pnl_trail = rest * entry_px * trail_gain * 100
        triggers["trail"] = {"qty": rest, "gain_pct": trail_gain, "dollars": pnl_trail}

    total = pnl_t1 + pnl_t2 + pnl_trail
    return {
        "counterfactual_pnl_dollars": round(total, 2),
        "trigger_breakdown": triggers,
        "option_mfe_estimate": mfe,
        "unchanged": False,
        "t1_threshold_used": t1_threshold,
        "hour_ct": hour_ct,
    }


async def run_for_session(session_date: str) -> dict:
    """Run counterfactual on all today's parents. Compares to actual.

    Pulls audit data via the existing endpoint logic so this stays in
    sync with how actual realized P/L is computed.
    """
    from fef_sidecar.api import diag_signal_execution_audit  # circular-safe (lazy)
    # Not ideal to import the route handler; refactor later. For now,
    # duplicate the small bit we need.
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import IBKRExecution, LiveOrder

    # Actual per-contract realized PnL (mirrors /diag/ibkr_executions_persisted)
    async with get_session() as session:
        exec_rows = list((await session.exec(
            select(IBKRExecution).where(IBKRExecution.session_date == session_date)  # type: ignore[attr-defined]
        )).all())
    by_contract: dict[str, dict[str, float]] = {}
    for r in exec_rows:
        key = f"{r.symbol} {r.strike}{r.right} {r.expiry}"
        slot = by_contract.setdefault(key, {"bot_v": 0.0, "sld_v": 0.0, "bot_q": 0.0, "sld_q": 0.0})
        if r.side == "BOT":
            slot["bot_v"] += r.shares * r.price
            slot["bot_q"] += r.shares
        else:
            slot["sld_v"] += r.shares * r.price
            slot["sld_q"] += r.shares
    contract_realized: dict[str, float] = {}
    for key, slot in by_contract.items():
        paired = min(slot["bot_q"], slot["sld_q"])
        if paired <= 0:
            continue
        avg_bot = slot["bot_v"] / slot["bot_q"]
        avg_sld = slot["sld_v"] / slot["sld_q"]
        contract_realized[key] = (avg_sld - avg_bot) * paired * 100.0

    # Get parents via direct DB query (avoid the auth-protected route)
    async with get_session() as session:
        parents = list((await session.exec(
            select(LiveOrder)
            .where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.dry_run == False)  # noqa: E712
        )).all())
    parents = [p for p in parents
               if p.submitted_at.strftime("%Y-%m-%d") == session_date]

    cfg = CounterfactualConfig()
    parent_rows = []
    total_actual = 0.0
    total_cf = 0.0
    unchanged_count = 0
    improved_count = 0
    for p in parents:
        # LiveOrder uses `symbol`/`strike`/`right`/`expiry` (NOT
        # contract_strike/contract_expiry — those are TradeTicket fields).
        contract = f"{p.symbol} {p.strike}{p.right} {p.expiry}"
        parents_on_contract = sum(1 for x in parents if x.symbol == p.symbol
                                  and x.strike == p.strike
                                  and x.right == p.right
                                  and x.expiry == p.expiry)
        contract_pnl = contract_realized.get(contract, 0.0)
        actual_share = contract_pnl / max(parents_on_contract, 1)

        be_step = float(getattr(p, "be_ladder_max_step_R", 0) or 0)
        parent_payload = {
            "parent_id": p.id,
            "submitted_at": p.submitted_at.isoformat(),
            "qty_filled": p.qty,
            "actual": {"avg_buy_price": float(p.limit_price or 0)},
            "signal": {"be_step_reached_R": be_step},
        }
        cf_result = simulate_one(parent_payload, cfg)
        cf_pnl = cf_result.get("counterfactual_pnl_dollars")

        if cf_pnl is None or cf_result.get("unchanged"):
            cf_final = actual_share
            unchanged_count += 1
        else:
            cf_final = cf_pnl
            if cf_pnl > actual_share:
                improved_count += 1

        total_actual += actual_share
        total_cf += cf_final

        parent_rows.append({
            "parent_id": p.id,
            "contract": f"{p.symbol} {p.strike}{p.right}",
            "qty": p.qty,
            "entry_option_price": float(p.limit_price or 0),
            "be_step_reached_R": be_step,
            "status": p.status,
            "actual_pnl_dollars": round(actual_share, 2),
            "counterfactual_pnl_dollars": round(cf_final, 2),
            "delta_dollars": round(cf_final - actual_share, 2),
            "trigger_breakdown": cf_result.get("trigger_breakdown", {}),
            "option_mfe_estimate": cf_result.get("option_mfe_estimate", 0.0),
        })

    # Friction-adjusted scenarios for both actual (current BE ladder) and
    # counterfactual (B+C+D). The "actual" is paper too — it's IBKR paper
    # data — so friction estimates apply to both. Three scenarios each:
    # best (commissions only), realistic (modest slippage), worst (heavy).
    fm = FrictionModel()
    n_trades = len(parent_rows)
    avg_qty = (sum(r["qty"] for r in parent_rows) / max(n_trades, 1)) if n_trades else 0
    actual_scenarios = _apply_friction(
        total_actual, n_trades, avg_qty, is_bcd=False, fm=fm,
    )
    cf_scenarios = _apply_friction(
        total_cf, n_trades, avg_qty, is_bcd=True, fm=fm,
    )

    # Unified dedup: collapse stacked entries to the first fire per
    # (direction|strike) within a 5-min window. Identical methodology to
    # daily_review.compute_daily_review so the dedup numbers stay
    # comparable across analytics surfaces.
    from fef_sidecar.engine.dedup import deduplicate_parents
    dedup_parents, dedup_stats = deduplicate_parents(parents)
    dedup_actual_pnl = 0.0
    dedup_cf_pnl = 0.0
    dedup_wins_actual = 0
    dedup_wins_cf = 0
    for dp in dedup_parents:
        contract = f"{dp.symbol} {dp.strike}{dp.right} {dp.expiry}"
        parents_on_contract = sum(
            1 for x in parents
            if x.symbol == dp.symbol and x.strike == dp.strike
            and x.right == dp.right and x.expiry == dp.expiry
        )
        contract_pnl = contract_realized.get(contract, 0.0)
        actual_share = contract_pnl / max(parents_on_contract, 1)
        be_step = float(getattr(dp, "be_ladder_max_step_R", 0) or 0)
        cf_result = simulate_one({
            "parent_id": dp.id,
            "submitted_at": dp.submitted_at.isoformat(),
            "qty_filled": dp.qty,
            "actual": {"avg_buy_price": float(dp.limit_price or 0)},
            "signal": {"be_step_reached_R": be_step},
        }, cfg)
        cf_pnl = cf_result.get("counterfactual_pnl_dollars")
        if cf_pnl is None or cf_result.get("unchanged"):
            cf_final = actual_share
        else:
            cf_final = cf_pnl
        dedup_actual_pnl += actual_share
        dedup_cf_pnl += cf_final
        if actual_share > 0:
            dedup_wins_actual += 1
        if cf_final > 0:
            dedup_wins_cf += 1
    dedup_n = dedup_stats.dedup_count
    dedup_actual_scenarios = _apply_friction(
        dedup_actual_pnl, dedup_n, avg_qty, is_bcd=False, fm=fm,
    )
    dedup_cf_scenarios = _apply_friction(
        dedup_cf_pnl, dedup_n, avg_qty, is_bcd=True, fm=fm,
    )

    return {
        "session_date": session_date,
        "method": "BE-step proxy for option MFE; B+C+D simulated exits",
        "parents_analyzed": len(parent_rows),
        "trades_improved_by_cf": improved_count,
        "trades_unchanged": unchanged_count,
        "total_actual_pnl": round(total_actual, 2),
        "total_counterfactual_pnl": round(total_cf, 2),
        "incremental_upside_dollars": round(total_cf - total_actual, 2),
        # Friction-adjusted P/L estimates (best / realistic / worst).
        # Use these for honest comparison; use total_actual_pnl /
        # total_counterfactual_pnl for the paper-accurate ceiling.
        "actual_scenarios": actual_scenarios,
        "counterfactual_scenarios": cf_scenarios,
        "n_trades": n_trades,
        "avg_qty_per_trade": round(avg_qty, 1),
        # Dedup-aware companion view: collapse stacked entries to the
        # first fire per (direction|strike) within 5 min. Tells you
        # "what if I'd only fired ONCE per setup?" — fewer trades,
        # smaller friction surface, signal quality alone.
        "dedup": {
            "window_min": dedup_stats.window_min,
            "total_stacked": dedup_stats.total_signals,
            "unique_setups": dedup_stats.dedup_count,
            "dropped_dupes": dedup_stats.dropped_dupes,
            "actual_paper_pnl": round(dedup_actual_pnl, 2),
            "counterfactual_paper_pnl": round(dedup_cf_pnl, 2),
            "actual_wins": dedup_wins_actual,
            "counterfactual_wins": dedup_wins_cf,
            "actual_win_rate_pct": round(dedup_wins_actual / max(dedup_n, 1) * 100, 1),
            "counterfactual_win_rate_pct": round(dedup_wins_cf / max(dedup_n, 1) * 100, 1),
            "actual_scenarios": dedup_actual_scenarios,
            "counterfactual_scenarios": dedup_cf_scenarios,
        },
        "config": {
            "t1_option_gain_normal": cfg.t1_option_gain,
            "t1_late_session_gain": cfg.t1_late_session_gain,
            "t1_power_hour_gain": cfg.t1_power_hour_gain,
            "t2_option_gain": cfg.t2_option_gain,
            "peak_drawdown_pct": cfg.peak_drawdown_pct,
        },
        "parents": parent_rows,
    }


# =============================================================================
# POLLING-INTERVAL COUNTERFACTUAL
# =============================================================================
#
# Replays the FEF engine over today's persisted FlowSnapshots at a finer
# cadence than the actual 120s production loop. For each actionable
# verdict produced (whether it matches an actual SignalIter or is NEW),
# simulates the trade outcome by walking 1-min SPX bars forward and
# checking T1/T2/T3 vs stop hit order.
#
# Critical assumption: FlowSnapshot fields contain everything needed to
# build FEFInputs directly (we don't have full MarketSnapshot rows
# persisted; we have the aggregates). The verdict result is the same as
# what would have fired live if the engine had been polling at the
# target cadence.


def _flow_snapshot_to_inputs(row):
    """Build FEFInputs from a FlowSnapshot row.

    Mirror of `snapshot_to_inputs` but consumes the persisted aggregate
    fields. FlowSnapshot stores walls as raw JSON dicts; FEFInputs needs
    ExposureRow pydantic objects, so we convert here. Without this
    conversion the engine sees empty wall lists and never produces
    actionable verdicts.
    """
    from fef_sidecar.engine.types import FEFInputs
    from fef_sidecar.quantdata.models import ExposureRow

    def _to_rows(wall_dicts):
        out = []
        for d in (wall_dicts or []):
            if isinstance(d, ExposureRow):
                out.append(d)
                continue
            if not isinstance(d, dict):
                continue
            try:
                out.append(ExposureRow(
                    strike=float(d.get("strike", 0)),
                    call_m=float(d.get("call_m", 0) or 0),
                    put_m=float(d.get("put_m", 0) or 0),
                    net_m=float(d.get("net_m", 0) or 0),
                ))
            except Exception:
                pass
        return out

    return FEFInputs(
        timestamp_ms=row.timestamp_ms,
        spx_price=row.spx_price,
        drift_cum_net_m=row.drift_cum_net_m or 0.0,
        drift_bias=row.drift_bias or "NEUTRAL",
        top_gex_walls=_to_rows(row.top_gex_walls),
        top_dex_walls=_to_rows(row.top_dex_walls),
        ntm_max_pain=row.ntm_max_pain or row.spx_price,
        raw_max_pain=row.raw_max_pain or row.spx_price,
        aggression_call_pct=row.aggression_call_pct,
        aggression_put_pct=row.aggression_put_pct,
        iv_rank_call_pct=row.iv_rank_call_pct,
        iv_rank_put_pct=row.iv_rank_put_pct,
        top_cex_walls=_to_rows(getattr(row, "top_cex_walls", None) or []),
        top_vex_walls=_to_rows(getattr(row, "top_vex_walls", None) or []),
    )


def _simulate_trade_outcome(verdict, entry_ts_ms: int, bars: list,
                             exit_strategy: str = "current_be") -> dict:
    """Walk forward through 1-min SPX bars under a chosen exit strategy.

    exit_strategy: "current_be" or "b_c_d" — picks the appropriate walker.
    """
    direction = verdict.direction.value if hasattr(verdict.direction, "value") else str(verdict.direction)
    if direction == "WAIT":
        return {"outcome": "WAIT", "r_multiple": 0.0, "seconds_to_outcome": 0}

    t1 = verdict.t1
    t2 = verdict.t2
    t3 = verdict.t3
    stop = verdict.stop
    if t1 is None or stop is None:
        return {"outcome": "NO_TARGETS", "r_multiple": 0.0, "seconds_to_outcome": 0}

    entry_spx = verdict.spx_at_verdict if hasattr(verdict, "spx_at_verdict") else None
    if entry_spx is None:
        return {"outcome": "NO_ENTRY", "r_multiple": 0.0, "seconds_to_outcome": 0}

    forward_bars = [b for b in bars if b.timestamp_ms >= entry_ts_ms]
    if not forward_bars:
        return {"outcome": "NO_BARS", "r_multiple": 0.0, "seconds_to_outcome": 0}

    walker = _walk_outcome_bcd if exit_strategy == "b_c_d" else _walk_outcome_current_be
    tiers_hit, r_multiple = walker(direction, entry_spx, t1, t2, t3, stop, forward_bars)

    seconds_to_outcome = (forward_bars[-1].timestamp_ms - entry_ts_ms) // 1000
    outcome_label = (
        "T3+" if "T3" in tiers_hit else
        "T2+" if "T2" in tiers_hit else
        "T1+" if "T1" in tiers_hit else
        ("STOP" if r_multiple < 0 else "EOD")
    )
    return {
        "outcome": outcome_label,
        "r_multiple": round(r_multiple, 3),
        "seconds_to_outcome": seconds_to_outcome,
        "tiers_hit": tiers_hit,
        "stopped": r_multiple < 0 and "T3" not in tiers_hit,
        "exit_strategy": exit_strategy,
    }


async def simulate_polling_interval(
    session_date: str,
    target_interval_sec: int,
) -> dict:
    """Replay the engine at target_interval_sec over FlowSnapshots for
    session_date. Returns counterfactual statistics vs actual run.

    Args:
        session_date: YYYY-MM-DD.
        target_interval_sec: 30, 60, 90 — must be ≥15 (FlowSnapshot poll
            cadence). Lower cadences need finer source data than we have.
    """
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import FlowSnapshot, HistoricalBar, SignalIter
    from fef_sidecar.engine.fef import FEFEngine

    # Empty-data response shape — must match the populated branch below
    # so the UI can render uniformly (no separate crash path).
    def _empty_polling_response(reason: str) -> dict:
        empty_outcome = {
            "n_simulated": 0, "wins": 0, "losses": 0,
            "win_rate_pct": 0.0, "expectancy_r": 0.0, "total_R": 0.0,
        }
        return {
            "session_date": session_date,
            "target_interval_sec": target_interval_sec,
            "actual_loop_interval_sec": 120,
            "method": "(no data)",
            "flow_snapshots_total": 0,
            "flow_snapshots_sampled": 0,
            "actual_actionable_signals": 0,
            "counterfactual_actionable_signals": 0,
            "incremental_signals": 0,
            "unique_setups_after_dedup": 0,
            "stacked_dupes_within_kept": 0,
            "outcomes_current_be": dict(empty_outcome),
            "outcomes_bcd": dict(empty_outcome),
            "note": reason,
        }

    if target_interval_sec < 15:
        return _empty_polling_response("target_interval_sec must be >=15 (FlowSnapshot poll cadence)")

    async with get_session() as session:
        flows = list((await session.exec(
            select(FlowSnapshot)
            .where(FlowSnapshot.session_date == session_date)
            .order_by(FlowSnapshot.timestamp_ms.asc())  # type: ignore[attr-defined]
        )).all())
        actuals = list((await session.exec(
            select(SignalIter)
            .where(SignalIter.session_date == session_date)
        )).all())
        # SPX 1-min bars across the whole session
        if flows:
            session_start_ms = flows[0].timestamp_ms - 60_000
            session_end_ms = flows[-1].timestamp_ms + 3_600_000
        else:
            session_start_ms = session_end_ms = 0
        bars = list((await session.exec(
            select(HistoricalBar)
            .where(HistoricalBar.symbol == "SPX")
            .where(HistoricalBar.bar_size == "1 min")
            .where(HistoricalBar.timestamp_ms >= session_start_ms)
            .where(HistoricalBar.timestamp_ms <= session_end_ms)
            .order_by(HistoricalBar.timestamp_ms.asc())  # type: ignore[attr-defined]
        )).all())

    if not flows:
        return _empty_polling_response("no FlowSnapshot data for this date")

    # Sample flows at target cadence
    sampled = [flows[0]]
    last_ts = flows[0].timestamp_ms
    target_ms = target_interval_sec * 1000
    for f in flows[1:]:
        if f.timestamp_ms - last_ts >= target_ms:
            sampled.append(f)
            last_ts = f.timestamp_ms

    # Replay engine over sampled flows
    engine = FEFEngine()  # default params
    sim_verdicts = []
    for f in sampled:
        try:
            inputs = _flow_snapshot_to_inputs(f)
            verdict = engine.push(inputs)
            # Attach SPX-at-verdict for downstream simulation
            verdict.spx_at_verdict = inputs.spx_price  # type: ignore[attr-defined]
            sim_verdicts.append((f.timestamp_ms, verdict))
        except Exception as e:
            logger.debug("Engine push failed at ts=%s: %s", f.timestamp_ms, e)

    # Dedup the sim'd verdicts so stacking doesn't inflate counts
    from fef_sidecar.engine.dedup import deduplicate_signals
    actionable_verdicts = [
        (ts_ms, v) for ts_ms, v in sim_verdicts
        if (v.direction.value if hasattr(v.direction, "value") else str(v.direction)) != "WAIT"
    ]
    for_dedup = [
        {
            "timestamp_ms": ts_ms,
            "direction": v.direction.value if hasattr(v.direction, "value") else str(v.direction),
            "strike": getattr(v, "strike_rec", None),
            "_v": v,
            "_ts": ts_ms,
        }
        for ts_ms, v in actionable_verdicts
    ]
    deduped, dedup_stats = deduplicate_signals(for_dedup)

    def _stats_for(strategy: str) -> dict:
        wins = losses = 0
        total_r = 0.0
        n_sim = 0
        for d in deduped:
            outcome = _simulate_trade_outcome(d["_v"], d["_ts"], bars, exit_strategy=strategy)
            r = outcome["r_multiple"]
            n_sim += 1
            total_r += r
            if r > 0: wins += 1
            elif r < 0: losses += 1
        return {
            "n_simulated": n_sim,
            "wins": wins, "losses": losses,
            "win_rate_pct": round(wins / max(n_sim, 1) * 100, 1) if n_sim else 0.0,
            "expectancy_r": round(total_r / max(n_sim, 1), 3) if n_sim else 0.0,
            "total_R": round(total_r, 2),
        }

    current_be_stats = _stats_for("current_be")
    bcd_stats = _stats_for("b_c_d")

    # Actual cadence stats — what really fired
    actual_actionable = sum(1 for s in actuals
                            if (s.direction.value if hasattr(s.direction, "value") else str(s.direction)) != "WAIT")

    return {
        "session_date": session_date,
        "target_interval_sec": target_interval_sec,
        "actual_loop_interval_sec": 120,
        "method": "Replay FEFEngine over FlowSnapshot at finer cadence; dedup per setup; simulate outcomes via 1-min SPX bars under both exit strategies",
        "flow_snapshots_total": len(flows),
        "flow_snapshots_sampled": len(sampled),
        "actual_actionable_signals": actual_actionable,
        "counterfactual_actionable_signals": len(actionable_verdicts),
        "incremental_signals": len(actionable_verdicts) - actual_actionable,
        "unique_setups_after_dedup": dedup_stats.dedup_count,
        "stacked_dupes_within_kept": dedup_stats.dropped_dupes,
        "outcomes_current_be": current_be_stats,
        "outcomes_bcd": bcd_stats,
        "note": (
            "Engine replayed at finer cadence; deduped per setup; outcomes simulated "
            "under both CURRENT BE ladder (models +0.5R MFE → stop-to-entry) AND B+C+D "
            "(option-T1 at ~0.5R MFE + trailing peak 15%). Total_R is the headline."
        ),
    }


async def run_polling_simulation(session_date: str, intervals: list[int] | None = None) -> dict:
    """Run polling counterfactual at multiple intervals and return a side-by-side comparison."""
    if intervals is None:
        intervals = [30, 60, 90]
    results = []
    for iv in intervals:
        results.append(await simulate_polling_interval(session_date, iv))
    return {
        "session_date": session_date,
        "comparisons": results,
    }


# =============================================================================
# COOLDOWN COUNTERFACTUAL
# =============================================================================
#
# Filters today's actual SignalIter timestamps through alternative cooldown
# rules and simulates trade outcomes via 1-min SPX bars. Answers "what if
# I used a 180s or 300s cooldown instead of the current 60s?"
#
# Unlike the polling CF (which replays the engine to produce new signals),
# the cooldown CF works on signals that ALREADY fired — it just filters
# which ones would have made it through a stricter cooldown gate.


def _walk_outcome_current_be(direction: str, entry_spx: float,
                              t1: float, t2: float | None, t3: float | None,
                              stop: float, fwd_bars: list) -> tuple[list[str], float]:
    """Simulate trade outcome under the CURRENT (broken) BE ladder.

    Models: at +0.5R MFE, the SPX stop moves to entry. At +1.0R MFE,
    stop moves to entry + 0.5R. Tier hits at T1/T2/T3 fire partial
    exits (1/3 each). Stop hit closes remaining.

    Returns (tiers_hit, r_multiple).
    """
    risk_pts = abs(entry_spx - stop)
    if risk_pts <= 0:
        return [], 0.0

    # BE ladder thresholds: at this MFE in R, move dyn stop to this point in R.
    # Matches engine.spx_target_watcher's actual ladder.
    BE_LADDER = [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)]
    tiers_hit: list[str] = []
    dyn_stop = stop
    mfe_r_max = 0.0  # signed: positive = favorable
    stopped = False

    for b in fwd_bars:
        if direction == "LONG_CALLS":
            mfe_r_now = (b.high - entry_spx) / risk_pts
            mfe_r_max = max(mfe_r_max, mfe_r_now)
            # Update BE ladder
            for trigger_r, lock_r in BE_LADDER:
                if mfe_r_max >= trigger_r:
                    new_stop = entry_spx + lock_r * risk_pts
                    if new_stop > dyn_stop:
                        dyn_stop = new_stop
            # Check stop FIRST (after BE update)
            if b.low <= dyn_stop:
                stopped = True
                break
            # Check tier hits
            if b.high >= t1 and "T1" not in tiers_hit: tiers_hit.append("T1")
            if t2 and b.high >= t2 and "T2" not in tiers_hit: tiers_hit.append("T2")
            if t3 and b.high >= t3 and "T3" not in tiers_hit: tiers_hit.append("T3")
        elif direction == "LONG_PUTS":
            mfe_r_now = (entry_spx - b.low) / risk_pts
            mfe_r_max = max(mfe_r_max, mfe_r_now)
            for trigger_r, lock_r in BE_LADDER:
                if mfe_r_max >= trigger_r:
                    new_stop = entry_spx - lock_r * risk_pts
                    if new_stop < dyn_stop:
                        dyn_stop = new_stop
            if b.high >= dyn_stop:
                stopped = True
                break
            if b.low <= t1 and "T1" not in tiers_hit: tiers_hit.append("T1")
            if t2 and b.low <= t2 and "T2" not in tiers_hit: tiers_hit.append("T2")
            if t3 and b.low <= t3 and "T3" not in tiers_hit: tiers_hit.append("T3")
        if len(tiers_hit) >= 3:
            break

    # R-multiple: each tier hits 1/3 of position
    tier_r = {"T1": 1.5, "T2": 2.5, "T3": 5.0}
    r = sum(tier_r[t] for t in tiers_hit) / 3.0
    if stopped:
        # Remaining qty stopped at the dyn_stop level.
        # Locked R = (dyn_stop - entry) / risk (signed by direction)
        if direction == "LONG_CALLS":
            locked_r = (dyn_stop - entry_spx) / risk_pts
        else:
            locked_r = (entry_spx - dyn_stop) / risk_pts
        r += locked_r * (3 - len(tiers_hit)) / 3.0
    return tiers_hit, r


def _walk_outcome_bcd(direction: str, entry_spx: float,
                      t1: float, t2: float | None, t3: float | None,
                      stop: float, fwd_bars: list) -> tuple[list[str], float]:
    """Simulate trade outcome under B+C+D exits (option-price ladder +
    trailing peak + time-decay). Uses MFE in R as proxy for option MFE:
      0.5R MFE → option ~+30%
      1.0R MFE → option ~+60%
      1.5R MFE → option ~+100%

    Tiers fire at option +30% / +75%, with trailing peak at 15% from peak.
    Returns (tiers_hit, r_multiple).

    The R units here are different from current_be — option-T1 at +30%
    isn't equivalent to SPX-T1 at +1.5R. We model conservatively:
      - Option-T1 fires → 1/3 of position locks "1.0R" equivalent
      - Option-T2 fires → 1/3 locks "2.5R"
      - Trail fires → remaining 1/3 locks (peak_option_gain * 0.85) × leverage
    Leverage = 1R per +25% option gain approximately (option delta ≈ 0.4).
    """
    risk_pts = abs(entry_spx - stop)
    if risk_pts <= 0:
        return [], 0.0
    tiers_hit: list[str] = []
    mfe_r_max = 0.0
    stopped = False

    for b in fwd_bars:
        if direction == "LONG_CALLS":
            mfe_r_now = (b.high - entry_spx) / risk_pts
            mfe_r_max = max(mfe_r_max, mfe_r_now)
            if b.low <= stop:
                stopped = True
                break
            if mfe_r_max >= 0.5 and "T1" not in tiers_hit: tiers_hit.append("T1")
            if mfe_r_max >= 1.0 and "T2" not in tiers_hit: tiers_hit.append("T2")
            if mfe_r_max >= 1.5 and "T3" not in tiers_hit: tiers_hit.append("T3")
        elif direction == "LONG_PUTS":
            mfe_r_now = (entry_spx - b.low) / risk_pts
            mfe_r_max = max(mfe_r_max, mfe_r_now)
            if b.high >= stop:
                stopped = True
                break
            if mfe_r_max >= 0.5 and "T1" not in tiers_hit: tiers_hit.append("T1")
            if mfe_r_max >= 1.0 and "T2" not in tiers_hit: tiers_hit.append("T2")
            if mfe_r_max >= 1.5 and "T3" not in tiers_hit: tiers_hit.append("T3")
        if len(tiers_hit) >= 3:
            break

    # R contributions:
    #  T1 (option +30% ≈ 0.5R SPX MFE) — equivalent to ~1R per third → 1/3 of position
    #  T2 (option +75% ≈ 1.0R SPX MFE) — equivalent to ~2.5R per third → 1/3 of position
    #  Trail captures the rest at peak × 0.85 (15% drawdown from peak)
    third = 1.0 / 3.0
    r = 0.0
    if "T1" in tiers_hit:
        r += 1.0 * third  # locked +1R on first third
    if "T2" in tiers_hit:
        r += 2.5 * third  # locked +2.5R on second third
    remaining = 1.0 - (third * len(tiers_hit))
    if remaining > 0 and mfe_r_max > 0:
        # Trail locks 0.85 × max_MFE on remaining
        r += mfe_r_max * 0.85 * remaining
    return tiers_hit, r


def _ts_to_utc_ms(dt) -> int:
    """SignalIter.timestamp / LiveOrder.submitted_at are tz-naive but
    represent UTC. Naive .timestamp() interprets as LOCAL time, which
    silently shifts by the system tz offset (5+ hours on America/Chicago).
    Explicit UTC tagging is required.
    """
    from datetime import timezone as _tz
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return int(dt.timestamp() * 1000)


async def simulate_cooldown_interval(
    session_date: str,
    cooldown_sec: int,
) -> dict:
    """Replay today's signals under a hypothetical per-symbol cooldown."""
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import HistoricalBar, SignalIter

    async with get_session() as session:
        sigs = list((await session.exec(
            select(SignalIter)
            .where(SignalIter.session_date == session_date)
            .order_by(SignalIter.timestamp.asc())  # type: ignore[attr-defined]
        )).all())
        if sigs:
            session_start_ms = _ts_to_utc_ms(sigs[0].timestamp) - 60_000
            session_end_ms = _ts_to_utc_ms(sigs[-1].timestamp) + 3_600_000
        else:
            session_start_ms = session_end_ms = 0
        bars = list((await session.exec(
            select(HistoricalBar)
            .where(HistoricalBar.symbol == "SPX")
            .where(HistoricalBar.bar_size == "1 min")
            .where(HistoricalBar.timestamp_ms >= session_start_ms)
            .where(HistoricalBar.timestamp_ms <= session_end_ms)
            .order_by(HistoricalBar.timestamp_ms.asc())  # type: ignore[attr-defined]
        )).all())

    actionable = [s for s in sigs
                  if (s.direction.value if hasattr(s.direction, "value") else str(s.direction)) != "WAIT"]

    if not actionable:
        # Empty-data response shape MUST match the populated branch below
        # so the UI doesn't crash trying to read outcomes_current_be /
        # outcomes_bcd. (UI broke on pre-market days when actionable=0.)
        empty_outcome = {
            "n_simulated": 0, "wins": 0, "losses": 0,
            "win_rate_pct": 0.0, "expectancy_r": 0.0, "total_R": 0.0,
        }
        return {
            "session_date": session_date,
            "cooldown_sec": cooldown_sec,
            "actual_signals_fired": 0,
            "counterfactual_signals_kept": 0,
            "signals_blocked_by_cooldown": 0,
            "unique_setups_after_dedup": 0,
            "stacked_dupes_within_kept": 0,
            "outcomes_current_be": dict(empty_outcome),
            "outcomes_bcd": dict(empty_outcome),
            "note": "no actionable signals yet for this session",
        }

    # Per-symbol cooldown enforcement (matches the live auto-trader's gate)
    cooldown_ms = cooldown_sec * 1000
    last_fire_ms_by_symbol: dict[str, int] = {}
    kept: list[SignalIter] = []
    for s in actionable:
        sym = "SPX"  # SignalIter is currently SPX-centric. Multi-symbol scanner stores symbol in extras.
        ext = s.extras or {}
        scanner_sym = ext.get("scanner_symbol")
        if scanner_sym:
            sym = scanner_sym
        ts_ms = _ts_to_utc_ms(s.timestamp)
        prev = last_fire_ms_by_symbol.get(sym, 0)
        if prev > 0 and (ts_ms - prev) < cooldown_ms:
            continue
        last_fire_ms_by_symbol[sym] = ts_ms
        kept.append(s)

    # Apply unified dedup so outcome metrics are reported per-setup, not
    # per-stacked-entry. Without this, two stacks on SPX 7425C produce
    # identical outcomes counted twice → inflated counts + skewed WR.
    from fef_sidecar.engine.dedup import deduplicate_signals
    kept_for_dedup = [
        {
            "timestamp_ms": _ts_to_utc_ms(s.timestamp),
            "direction": s.direction.value if hasattr(s.direction, "value") else str(s.direction),
            "strike": (s.extras or {}).get("strike_rec"),
            "_signal": s,
        }
        for s in kept
    ]
    deduped, dedup_stats = deduplicate_signals(kept_for_dedup)
    dedup_signals = [d["_signal"] for d in deduped]

    # Simulate trade outcomes under BOTH exit strategies (current BE
    # ladder + B+C+D). Lets us see "what's the WR under this cooldown
    # with current exits vs upgraded exits?"
    def _sig_targets(s):
        ext = s.extras or {}
        return {
            "direction": s.direction.value if hasattr(s.direction, "value") else str(s.direction),
            "entry": float(s.spx_price),
            "t1": ext.get("t1"),
            "t2": ext.get("t2"),
            "t3": ext.get("t3"),
            "stop": ext.get("stop"),
        }

    def _stats_for(walker):
        wins = losses = 0
        total_r = 0.0
        n_sim = 0
        for s in dedup_signals:
            tg = _sig_targets(s)
            if not (tg["t1"] and tg["stop"]):
                continue
            ts_ms = _ts_to_utc_ms(s.timestamp)
            fwd = [b for b in bars if b.timestamp_ms >= ts_ms]
            if not fwd:
                continue
            _, r = walker(tg["direction"], tg["entry"], tg["t1"], tg["t2"],
                          tg["t3"], tg["stop"], fwd)
            n_sim += 1
            total_r += r
            if r > 0: wins += 1
            elif r < 0: losses += 1
        return {
            "n_simulated": n_sim,
            "wins": wins, "losses": losses,
            "win_rate_pct": round(wins / max(n_sim, 1) * 100, 1) if n_sim else 0.0,
            "expectancy_r": round(total_r / max(n_sim, 1), 3) if n_sim else 0.0,
            "total_R": round(total_r, 2),
        }

    current_be_stats = _stats_for(_walk_outcome_current_be)
    bcd_stats = _stats_for(_walk_outcome_bcd)

    n_kept_stacked = len(kept)
    n_dedup = dedup_stats.dedup_count
    return {
        "session_date": session_date,
        "cooldown_sec": cooldown_sec,
        "actual_signals_fired": len(actionable),
        # Raw stacked count after applying cooldown filter
        "counterfactual_signals_kept": n_kept_stacked,
        "signals_blocked_by_cooldown": len(actionable) - n_kept_stacked,
        # Dedup count — unique setups inside the kept-stacked set
        "unique_setups_after_dedup": n_dedup,
        "stacked_dupes_within_kept": n_kept_stacked - n_dedup,
        # Outcomes under BOTH exit strategies. The "best combo" reading
        # is whichever (cooldown × strategy) maximizes total_R.
        "outcomes_current_be": current_be_stats,
        "outcomes_bcd": bcd_stats,
        "note": (
            "Per-symbol cooldown filter applied to actual today's signals; "
            "outcomes computed per-DEDUP-setup (unified util, 5-min window). "
            "Two exit strategies simulated: current BE ladder (models +0.5R "
            "MFE → stop-to-entry) and B+C+D (option-T1 at ~0.5R MFE + "
            "trailing peak 15%). Use total_R to compare."
        ),
    }


async def run_cooldown_simulation(session_date: str, cooldowns: list[int] | None = None) -> dict:
    """Side-by-side cooldown comparison."""
    if cooldowns is None:
        cooldowns = [60, 180, 300, 600]
    results = []
    for cd in cooldowns:
        results.append(await simulate_cooldown_interval(session_date, cd))
    return {
        "session_date": session_date,
        "comparisons": results,
    }
