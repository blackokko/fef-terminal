"""Backfill EngineChange rows for the semantic changes already shipped.

Runs at sidecar startup (main.lifespan) and via POST
/admin/backfill_engine_changes. Idempotent — skips changes whose `name`
already exists in the table, so restarts and manual runs are safe. The
2026-07-06 EOD flagged the empty ledger as a traceability gap: the startup
invocation guarantees the ledger is populated on every boot going forward.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import EngineChange

logger = logging.getLogger(__name__)


# Each tuple: (applied_iso_date, name, category, description, expected_impact,
# git_sha_prefix_or_None — None for changes shipped from the uncommitted
# working tree, where no commit sha exists yet)
SEMANTIC_CHANGES: list[tuple[str, str, str, str, str, str | None]] = [
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
    # ---- 2026-07-02 deploys (ledger backfill 2026-07-06 — the 7/6 EOD
    # flagged the empty ledger as a traceability gap) ----
    ("2026-07-02T16:00:00", "chop_gate", "gate",
     "Scanner CHOP skip-gate: skip actionable fires when trailing 30-min SPX range "
     "percentile (TOD-matched vs prior 20 sessions) < 33.3; counterfactual signal row "
     "persisted with extras.skipped_by='chop_gate' + 'chop_gate' audit row; fail-open "
     "on missing data; hot-read via UIConfig.chop_gate_enabled/_pct_threshold",
     "22,683-fire sweep: compressed-regime fires lose -$25 to -$48/ct in every config; "
     "expanded fires ~breakeven-to-positive — cut the harm tercile",
     None),
    ("2026-07-02T16:00:00", "bias_veto", "gate",
     "Scanner bias-disagree veto promoted from tag to SKIP: fires whose reasoning "
     "carries '(bias-disagrees, demoted)' are skipped (extras.skipped_by='bias_veto', "
     "'bias_veto' audit row); chop gate wins when both would skip; hot-read via "
     "UIConfig.bias_veto_enabled",
     "3 of 5 live losers on 7/2 wore the tag (-$1,710); sweep OOS disagree -$17.00/ct "
     "vs agree -$1.85/ct",
     None),
    ("2026-07-02T16:00:00", "etf_dollar_exit_scaling", "exit",
     "fixed_dollar $ targets scaled per symbol by the strike fix's notional ratio "
     "(_price_scale = spot/7500 below 2000; SPX-class bit-identical at 1.0); whole "
     "dollars, $10 minimum magnitude, sign preserved; hot-read via "
     "UIConfig.etf_dollar_exit_scaling",
     "SPX-notional $300 T1 on a 3-lot SPY was ~15-20 SPX-equivalent points away "
     "(7/2 SPY 743C: profitable, couldn't bank) — scaled ladder makes ETF exits reachable",
     None),
    # ---- 2026-07-06 deploys ----
    ("2026-07-06T12:00:00", "dollar_loss_cap_1800", "safety",
     "Auto-trade catastrophic dollar-loss cap default set to -$1,800 per position "
     "(FEF_DOLLAR_LOSS_CAP; watcher polls option premium and market-exits remaining "
     "qty when open P&L breaches the cap)",
     "Sits beyond the -10pt ladder stop; binds only on gap disasters (gamma blow-ups) "
     "so normal exits stay ladder-managed",
     None),
    ("2026-07-06T12:00:00", "watcher_oversell_fix", "safety",
     "Oversell defense-in-depth: per-parent LIFETIME sell cap (cumulative watcher "
     "sells can never exceed filled qty, independent of broker position snapshots), "
     "per-(kind,parent) in-flight check latch (no concurrent stop/tier evaluations "
     "from stale flags), optimistic status/tier latch BEFORE awaited sells with "
     "revert-on-refusal",
     "Kills the 7520C double-stop (-3 naked short) and the QQQ 724C post-flatten T3 "
     "fire — watcher can no longer sell more than the parent owns",
     None),
    ("2026-07-06T12:00:00", "scanner_input_glitch_guard", "data",
     "Per-symbol scanner input sanity guard: incoming spot deviating >20% from the "
     "engine's last ACCEPTED spot rejects the whole iteration (no engine.push, no "
     "verdict cache, no trade path) + 'scanner_input_glitch' audit row; QD page-filter "
     "race closed by loop._qd_scope_lock (atomic set+fetch)",
     "Stops cross-symbol contamination (GLD scan fetched SPX-priced snapshot -> "
     "'ALIGNED +7147pts') from polluting engine windows or firing trades",
     None),
    # ---- 2026-07-06 overnight batch ----
    ("2026-07-06T23:00:00", "fomc_morning_gate", "gate",
     "Scanner FOMC-morning skip-gate: actionable fires on event_calendar FOMC days "
     "before 14:00 ET are skipped (extras.skipped_by='fomc_gate', 'fomc_gate' audit "
     "row, FOMC_GATE_SKIP); hot-read via UIConfig.fomc_gate_enabled; gate order "
     "chop -> bias -> fomc -> tod",
     "Event study: FOMC pre-statement fires -$61/ct (t=-4.4), 80% of FOMC sessions "
     "negative; next FOMC 2026-07-29",
     None),
    ("2026-07-06T23:00:00", "tod_no_trade_window", "gate",
     "Scanner time-of-day no-trade window: fires with ET clock in "
     "[tod_gate_start_et, tod_gate_end_et) (default 14:00-15:30) are skipped "
     "(extras.skipped_by='tod_gate', 'tod_gate' audit row, TOD_GATE_SKIP); window "
     "hot-tunable via POST /config",
     "Entry-filter sweep: worst TOD bucket 14:00-15:30 ET at -$21/ct OOS; "
     "counterfactual rows grade the window in production",
     None),
    ("2026-07-06T23:00:00", "shadow_plumbing_fixes", "data",
     "Exit-shadow plumbing: entry_spx sourced from the symbol's own CASH spot with "
     "provenance + 0.5% cross-check (never the active-chart/ES-fed tick); missing "
     "fill premiums rescued from the nearest captured quote within ±2 min else the "
     "row is marked unusable via shadow_exit_outcome.status (never $0); close-time "
     "recompute UPSERTS (replaces) the (parent, variant) row; coverage extended to "
     "non-SPX watcher parents via _price_scale geometry",
     "7/6 EOD sec. 2: morning rows recorded ES-priced 7561/7567 vs cash 7511/7508, "
     "$0-premium rows polluted the A/B, parent 747 froze at entry-time compute",
     None),
    ("2026-07-06T23:00:00", "exit_shadow_flip_conditional", "exit",
     "Shadow variant now conditional on the parent's REAL exit_strategy: "
     "fixed_dollar parents shadow the BE-ladder (be_ladder_shadow, as before); "
     "spx_points parents shadow the fixed-$ ladder (fixed_dollar_shadow: 300/500/"
     "1000/-400 cumulative-realized, ETF-scaled) — supports the 2026-07-07 live "
     "default flip to spx_points with the A/B still measuring both sides",
     "Exit A/B keeps producing paired counterfactuals after the live default flips",
     None),
    ("2026-07-06T23:00:00", "scanner_reference_pollution_guard", "data",
     "Input-glitch guard extended: the implied per-iteration delta is validated "
     "against max(50, 3% of spot) vs every reference in the engine's own history "
     "window; a violation rejects the iteration AND resets/quarantines the window "
     "('scanner_input_glitch' audit row, kind='reference_pollution')",
     "19:07 residual: 6 post-fix rows had VALID current spots against a polluted "
     "PREVIOUS reference (cross-stream ref in the delta window) producing dP ±6,794",
     None),
    ("2026-07-07T20:00:00", "lowvix_shadow_prior_close_basis", "data",
     "Documented basis deviation for the lo-VIX day classifier "
     "(engine/lo_vix_day.py): the registered lowvix backtest labeled days on "
     "the VIX daily-bar OPEN percentile; live intraday classification runs on "
     "the PRIOR-CLOSE basis because the VIX 1-day rows land at the 16:00 ET "
     "close. 8.9% of historical sessions flip labels between the two bases.",
     "None expected — the adversarial reviewer RE-VALIDATED the registered "
     "ARM-B (m=0.6 shrink on lo days) under prior-close labels on the same "
     "bars: OOS +$4.69/ct t=2.33 (pess), +$4.63/ct t=2.30 (mid), both OOS "
     "years positive, TRAIN >= 0. Shadow-only either way (lowvix_shrink_shadow).",
     None),
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
