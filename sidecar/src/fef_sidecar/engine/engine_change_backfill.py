"""Backfill EngineChange rows for the semantic changes already shipped.

Run once via POST /admin/backfill_engine_changes. Idempotent — skips
changes whose `name` already exists in the table.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import EngineChange

logger = logging.getLogger(__name__)


# Each tuple: (applied_iso_date, name, category, description, expected_impact, git_sha_prefix)
SEMANTIC_CHANGES: list[tuple[str, str, str, str, str, str]] = [
    ("2026-05-18T08:00:00", "wall_collapse_requires_aligned", "gate",
     "Wall-collapse trigger now requires aligned=True confluence",
     "Cut volume on wall_collapse-only fires (15% win) and keep only confluence (42% win)",
     "af1f53c"),
    ("2026-05-18T09:00:00", "drift_bias_gate", "gate",
     "Aligned signals demoted one confidence notch when drift_bias disagrees with direction",
     "Lower auto-trade volume on counter-bias signals; +5-10pp win rate on remaining",
     "b6fc72b"),
    ("2026-05-18T09:00:00", "regime_demote_chop_pin_walltest", "gate",
     "Demote actionable confidence by one notch when last_regime in CHOP/PIN_DAY/WALL_TEST",
     "Skip auto-trade in low-edge regimes; ~30% volume cut",
     "b6fc72b"),
    ("2026-05-18T09:00:00", "be_ladder_step_stop", "exit",
     "Profit-locking step-stop at +0.5R / +1.0R / +1.5R MFE thresholds in spx_target_watcher",
     "Convert give-back stops to scratch / partial wins. Today: 57% of stops saw >=0.5R first",
     "b6fc72b"),
    ("2026-05-18T09:00:00", "split_50_30_20", "exit",
     "Auto-trade qty split changed 85/10/5 -> 50/30/20",
     "More capital rides into T2/T3; 80% of winners reach 2R MFE",
     "b6fc72b"),
    ("2026-05-18T09:00:00", "t1_anchored_1r_t2_anchored_2r", "exit",
     "T1 = 1R, T2 = 2R anchored to risk distance (instead of wall-or-+10pt)",
     "Predictable R:R for the 50/30/20 split math",
     "b6fc72b"),
    ("2026-05-18T11:00:00", "auto_trade_market_entry", "exit",
     "Auto-trade uses MarketOrder for parent BUY (not LimitOrder) across all exit strategies",
     "Eliminates premium-walks-away problem on fast 0DTE moves",
     "c7acdce"),
    ("2026-05-18T11:00:00", "auto_trade_dollar_loss_cap", "safety",
     "$3k dollar loss cap; spx_target_watcher polls option premium every 10s",
     "Catches gamma blow-ups before SPX stop reaches level",
     "7e1274e"),
    ("2026-05-18T11:00:00", "fef_high_band_fe_1_to_5", "scoring",
     "HIGH confidence requires FE ratio in [1.0, 5.0]; MEDIUM caps at 8.0",
     "Eliminates noise-driven HIGH; today HIGH 14% win <-> LOW 41% inversion",
     "b6fc72b"),
    ("2026-05-18T11:00:00", "absorption_extended_to_extreme_fe", "gate",
     "Absorption flag now fires for FE >= 10 regardless of sign",
     "Flow-being-absorbed setups demoted to WAIT; today 0/7 win at FE>=10",
     "b6fc72b"),
    ("2026-05-18T11:00:00", "market_hours_only_default", "data",
     "FEF_LOOP_MARKET_HOURS_ONLY=true default in -AutoTrade.bat files",
     "Drop pre-market noise (today: 21 fires 1-2 AM CT with 0 valid outcomes)",
     "9671de1"),
    ("2026-05-18T12:00:00", "regime_backfill_on_startup", "data",
     "Regime detector seeded from SignalIter on sidecar boot",
     "Immediate regime classification on app load (no warmup wait)",
     "f5e9148"),
    ("2026-05-18T13:00:00", "live_position_query_direct", "data",
     "/live/positions queries ib.positions() directly (not stale DB snapshots)",
     "Closed positions disappear immediately from panel",
     "fe1b6c5"),
    ("2026-05-18T13:00:00", "option_quote_no_generic_ticks", "data",
     "Option reqMktData uses empty generic tick list (not '13' which IBKR rejects with error 321)",
     "Option pricing actually populates instead of all-NaN — unblocked auto-trade entirely",
     "323dfd4"),
    ("2026-05-18T13:00:00", "option_delayed_data_fallback", "data",
     "reqMarketDataType(3) for delayed data + modelGreeks.optPrice fallback",
     "Paper accounts without OPRA subscription can still get pricing for auto-trade",
     "b1e6d58"),
    ("2026-05-18T13:00:00", "paper_account_safety_gate", "safety",
     "Refuse to submit orders unless account ID starts with 'DU' (FEF_REQUIRE_PAPER_ACCOUNT=true default)",
     "Prevent leakage to live account when TWS is configured ambiguously",
     "f7dd6e4"),
    ("2026-05-18T14:00:00", "spx_watcher_interval_0p5s", "exit",
     "spx_target_watcher polling 2s -> 0.5s + dollar-loss check moved to background task",
     "TP fire latency 2-7s -> ~300-700ms",
     "e6b186b"),
    ("2026-05-18T15:00:00", "auto_trade_qty_override", "scoring",
     "Configurable per-fire qty override in AutoTrade panel (0 = use suggested by confidence tier)",
     "Manual override of the 15/10/5 default",
     "a7911ce"),
    ("2026-05-18T15:00:00", "auto_trade_strategy_by_qty", "exit",
     "Auto-trade picks exit_strategy by qty: fixed_dollar at qty>=15, else spx_points",
     "fixed_dollar math only sensible at larger sizes; small qty engages new engine work",
     "71e4788"),
    ("2026-05-18T16:00:00", "live_order_status_sync", "safety",
     "spx_target_watcher writes back PARTIAL/CLOSED to parent LiveOrder.status on fills",
     "Audit + DailyReview can detect actual closed trades",
     "27c4847"),
    ("2026-05-18T17:00:00", "spx_sanity_guard", "data",
     "_run_one_iter skips when snapshot.spx_price diverges >50% from live tick",
     "Eliminates futures-symbol stale-snapshot signals (today: delta_price=-7341 garbage)",
     "569cbf2"),
]


async def backfill_engine_changes() -> dict:
    """Insert missing EngineChange rows for today's shipped changes."""
    inserted = 0
    skipped = 0
    async with get_session() as session:
        existing = list((await session.exec(
            select(EngineChange.name)
        )).all())
        existing_names = set(existing)

        for applied_iso, name, category, desc, impact, sha in SEMANTIC_CHANGES:
            if name in existing_names:
                skipped += 1
                continue
            row = EngineChange(
                applied_at=datetime.fromisoformat(applied_iso).replace(tzinfo=timezone.utc),
                name=name, description=desc, category=category,
                expected_impact=impact, git_commit_sha=sha,
            )
            session.add(row)
            inserted += 1
        await session.commit()
    logger.info("EngineChange backfill: %d inserted, %d skipped", inserted, skipped)
    return {"inserted": inserted, "skipped": skipped, "total_known": len(SEMANTIC_CHANGES)}
