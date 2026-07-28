# FEF Variant Ledger — honest N_trials for DSR

Total tested variants: **84**
Currently active in production: **8**
Shadow-tested (registered in DB `variant_ledger`): **38**
Dropped / superseded: **16**
Proposed (identified but not yet evaluated): **22**

Last updated: **2026-05-26** (added 4 BSM-priced strike-offset variants per Report 27)

## Why this file exists

DSR (Deflated Sharpe Ratio) needs `N_trials` = total number of strategy variants
ever considered, not just the ones currently active. Counting only the live
variants is survivorship bias: today's "winning" variants are conditional on
prior dropped variants we don't see.

Per Harvey & Liu (2015) and Bailey & López de Prado (2014), honest N_trials must
include every variant that was tested and dropped, every parameter sweep value
evaluated, every gate proposed and not shipped. The `variant_ledger` SQL table
currently captures only the 12 *shadow_exit_replay* variants — the rest of
this project's variant history lives in `engine_change` rows, git history,
research scratchpads, walk-forward runs, and tournament presets.

This file is the canonical roll-up. **Use the count at the bottom for DSR.**

Status codes:

| Status | Meaning | Counts toward N_trials? |
|---|---|---|
| ACTIVE | Currently running in production | yes |
| SHADOW | Tested in ablation/shadow_replay, not deployed | yes |
| DROPPED | Tested and rejected | yes |
| SUPERSEDED | Replaced by a newer variant | yes |
| PROPOSED | Identified as candidate, not yet tested | **no** |

---

## Family: Exit logic

| Variant | Date introduced | Status | Reason for status | Last referenced |
|---|---|---|---|---|
| 50/30/20 partial scale-out (T1=1R, T2=2R, T3=3R) | 2026-05-18 | ACTIVE | engine_change `split_50_30_20`, `t1_anchored_1r_t2_anchored_2r` | live every session |
| 85/10/5 partial split (runner protector v1) | 2026-05-18 | SUPERSEDED | replaced same day by 50/30/20 after MFE analysis showed 80% reach 2R | commit `8bbdfdf` |
| Original hard T1/T2/T3 (no partials) | pre-2026-05-18 | SUPERSEDED | replaced by 85/10/5 then 50/30/20 | commit `218ea7f` (Options A+C picker) |
| BE-ladder step-stop [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)] | 2026-05-18 | SUPERSEDED 2026-07-28 | replaced by the rung-sweep winner (181-cell prereg, +$22.95/ct OOS t=+3.36) | rung_sweep_report.md |
| BE-ladder step-stop [(0.6, 0.0), (1.2, 0.5), (1.8, 0.5)] | 2026-07-28 | ACTIVE | engine_change `be_ladder_rung_sweep_2026_07_28`; env FEF_BE_LADDER_STEPS; NOTE: be_ladder_shadow aggregates must date-split at 2026-07-29 (label unchanged across cutover) | rung_sweep_LOCK.md |
| 1.5R T1 anchor (replaced by 1.0R) | 2026-05-18 | SUPERSEDED | shipped in `88cb408` then reverted same day to 1R in `b6fc72b` | commit `88cb408` |
| Auto-trade market entry | 2026-05-18 | ACTIVE | engine_change `auto_trade_market_entry` | live every session |
| SPX target watcher 0.5s interval | 2026-05-18 | ACTIVE | engine_change `spx_watcher_interval_0p5s` | live |
| Auto-trade strategy-by-qty | 2026-05-18 | ACTIVE | engine_change `auto_trade_strategy_by_qty` | live |
| Tick-driven SPX stop fast path | 2026-05-18 | ACTIVE | commit `944b9c8` — tick-level stop firing | live |
| baseline_partial_50_30_20 (no BE ladder) | 2026-05-21 | SHADOW | shadow baseline reference (pessimistic — no live ladder) | shadow_replay 5/22 |
| baseline_partial_with_be_ladder | 2026-05-21 | SHADOW | accurate current-live model | shadow_replay 5/22 |
| baseline_timeout_90min | 2026-05-21 | SHADOW | sub_family=timeout, isolates timeout knob | shadow_replay 5/22 |
| bcd_trail_15pct (B+C+D peak-trail) | 2026-05-21 | SHADOW | "headline winner" of 5/18-5/20 ablation, demoted by 5/22 live | shadow_replay 5/22 |
| bcd_trail_10pct (tighter) | 2026-05-21 | SHADOW | trail_tightness parameter sweep | shadow_replay 5/22 |
| bcd_trail_25pct (looser) | 2026-05-21 | SHADOW | trail_tightness parameter sweep | shadow_replay 5/22 |
| pure_trail_15pct (no T1 hard target) | 2026-05-21 | SHADOW | sub_family=trail_tightness, isolates T1-vs-trail | shadow_replay 5/22 |
| bcd_trail_15pct_with_be_ladder | 2026-05-21 | SHADOW | sub_family=be_ladder_combo, two-mechanism composition | shadow_replay 5/22 |
| pure_trail_15pct_with_be_ladder | 2026-05-21 | SHADOW | sub_family=be_ladder_combo | shadow_replay 5/22 |
| bcd_trail_15pct_with_option_price_be_ladder | 2026-05-21 | SHADOW | placeholder — requires option-price evolution module | shadow_replay 5/22 (placeholder) |
| bcd_theta_tighten_03pts_15min_with_be_ladder | 2026-05-26 | SHADOW | theta-aware sweep: 0.03pts/min tighten past 15min | shadow_replay 5/26 |
| bcd_theta_tighten_03pts_30min_with_be_ladder | 2026-05-26 | SHADOW | theta-aware sweep: 0.03pts/min tighten past 30min | shadow_replay 5/26 |
| bcd_theta_tighten_03pts_45min_with_be_ladder | 2026-05-26 | SHADOW | theta-aware sweep: 0.03pts/min tighten past 45min | shadow_replay 5/26 |
| bcd_theta_tighten_05pts_15min_with_be_ladder | 2026-05-26 | SHADOW | theta-aware sweep: 0.05pts/min tighten past 15min | shadow_replay 5/26 |
| bcd_theta_tighten_05pts_30min_with_be_ladder | 2026-05-26 | SHADOW | theta-aware sweep: 0.05pts/min tighten past 30min | shadow_replay 5/26 |
| bcd_theta_tighten_05pts_45min_with_be_ladder | 2026-05-26 | SHADOW | theta-aware sweep: 0.05pts/min tighten past 45min | shadow_replay 5/26 |
| bcd_theta_tighten_08pts_15min_with_be_ladder | 2026-05-26 | SHADOW | theta-aware sweep: 0.08pts/min tighten past 15min | shadow_replay 5/26 |
| bcd_theta_tighten_08pts_30min_with_be_ladder | 2026-05-26 | SHADOW | theta-aware sweep: 0.08pts/min tighten past 30min | shadow_replay 5/26 |
| bcd_theta_tighten_08pts_45min_with_be_ladder | 2026-05-26 | SHADOW | theta-aware sweep: 0.08pts/min tighten past 45min | shadow_replay 5/26 |
| bcd_theta_tighten_12pts_15min_with_be_ladder | 2026-05-26 | SHADOW | theta-aware sweep: 0.12pts/min tighten past 15min | shadow_replay 5/26 |
| bcd_theta_tighten_12pts_30min_with_be_ladder | 2026-05-26 | SHADOW | theta-aware sweep: 0.12pts/min tighten past 30min | shadow_replay 5/26 |
| bcd_theta_tighten_12pts_45min_with_be_ladder | 2026-05-26 | SHADOW | theta-aware sweep: 0.12pts/min tighten past 45min | shadow_replay 5/26 |
| Hybrid 3-tier + post-T3 peak-trail (-0.5R) | 2026-05-22 | PROPOSED | Priority 2 of revised post-freeze list — better than pure B+C+D on 5/22 evidence | EOD analysis 5/22 |
| Lookahead-flag baseline (T1-anywhere-in-60min) | 2026-05-19 | SUPERSEDED | replaced by order-preserving bar walk; overcounted T1 hits on fast reversals | commit `492ca9c` |

## Family: Entry filters

| Variant | Date introduced | Status | Reason for status | Last referenced |
|---|---|---|---|---|
| wall_collapse on confluence only (block aligned-only) | 2026-05-18 | ACTIVE | engine_change `wall_collapse_requires_aligned` | live |
| drift_bias gate ($1M absolute threshold) | 2026-05-18 | SUPERSEDED | replaced by 15% relative dominance ratio in `v2_engine_formula_fixes` | engine_change `drift_bias_gate` |
| drift_bias gate v2 (15% relative dominance) | 2026-05-22 | ACTIVE | engine_change `v2_engine_formula_fixes` | live (v2) |
| Confluence trigger-type gate | 2026-05-18 | ACTIVE | engine_change shipped via `88cb408` (Gate 3.5 trigger types) | live |
| Confidence floor gate | pre-2026-05-18 | SUPERSEDED | demoted to informational in `88cb408`; FEF_USE_CONFIDENCE_FLOOR=false by default | commit `88cb408` |
| Confidence floor gate — RE-ARMED, always-on (config_store floor, wall-collapse badge re-graded through formula) | 2026-07-08 | ACTIVE | 12k-fire registered study: HIGH +37.81/ct OOS vs LOW -14.84 (t=3.31), book -$142k->+$117k; wc badge unearned (-1.56/ct vs formula-HIGH +27.88); engine_change `confidence_floor_armed` | live |
| 2-iter wall_collapse persistence | 2026-05-18 | DROPPED | `04ea21c` — 0/22 confluence today; logged then deprecated | commit `04ea21c` |
| Direction filter (30m raw SPX, 3pt threshold) | 2026-05-21 | SHADOW | entry_dir_filter_then_baseline in registered variants | ablation 5/21, shadow 5/22 |
| Direction filter (alt_direction_30m EMA stack) | 2026-05-21 | PROPOSED | research-only signal in extras (312 iters tagged) | loop.py commit `de91eb7` |
| Direction filter (alt_direction_4h EMA stack) | 2026-05-22 | PROPOSED | 220 iters tagged; confirmed bullish on all 9 losing PUTs 5/22 | commit `7a33b22` |
| Direction filter SKIP in OPEN_DRIVE regime | 2026-05-22 | PROPOSED | exception rule per Priority 1 revision; B+C+D-only wins OPEN_DRIVE | revised priorities 5/22 |
| conjunction_req_2 (counter-bias requires >=2 of alt_30m/alt_4h/microstructure/aggression) | 2026-05-26 | SHADOW | top recommended fix from report 19 Option E; drops 79/79 counter-bias, +54R 5/19-5/26 | shadow_signal_replay 5/26 |
| conjunction_req_1 (looser: >=1 confirming gate) | 2026-05-26 | SHADOW | drops 60/79 counter-bias, +47.5R 5/19-5/26 | shadow_signal_replay 5/26 |
| drop_low_counter_bias (LOW + counter-bias ⇒ WAIT) | 2026-05-26 | SHADOW | simpler fallback; drops all 79 counter-bias (100% are LOW in window), +52R | shadow_signal_replay 5/26 |
| conjunction_req_2 standalone — graduated to ablation_backtest (Baseline exit + verdict filter) | 2026-05-26 | SHADOW | DSR/PBO/CPCV/sequential bootstrap re-evaluation per report 22; isolates direction-fix marginal contribution | ablation_backtest 5/26 |
| CEX bullish_score gate (`>0 blocks PUTS`) | 2026-05-22 | PROPOSED | Priority 4 — ablation-test post-freeze | revised priorities 5/22 |
| VEX confluence gate | 2026-05-22 | PROPOSED | research-only — Priority 4 | revised priorities 5/22 |
| absorption_extended_to_extreme_fe (FE>=10 block) | 2026-05-18 | ACTIVE | engine_change | live |
| absorption_fe_ratio_min sweep (2, 3, 5, 7, 8, 10) | 2026-05-19 | SHADOW | per_regime_sweep grid values | per_regime_sweep v2 |
| fef_high_band_fe_1_to_5 (HIGH band scoring change) | 2026-05-18 | ACTIVE | engine_change `fef_high_band_fe_1_to_5` | live |
| Auto-trade dollar-loss cap ($3k) | 2026-05-18 | ACTIVE | engine_change `auto_trade_dollar_loss_cap` | live |

## Family: Regime gates

| Variant | Date introduced | Status | Reason for status | Last referenced |
|---|---|---|---|---|
| regime_demote_chop_pin_walltest (skip auto-trade in low-edge regimes) | 2026-05-18 | ACTIVE | engine_change `regime_demote_chop_pin_walltest` | live |
| bcd_only_in_chop (regime-conditional B+C+D routing) | 2026-05-21 | SHADOW | TREND_DAY/DRIFT_BULL → baseline; else → B+C+D trail | shadow_replay 5/22 |
| Regime-conditional strike selection | 2026-05-22 | PROPOSED | Priority 3 — TREND_DAY=ATM, PIN_DAY=OTM, etc. | revised priorities 5/22 |
| Regime-demoted qty override | 2026-05-18 | ACTIVE | live engine behavior (state.regime_demoted_qty) | live |

## Family: Strike selection

| Variant | Date introduced | Status | Reason for status | Last referenced |
|---|---|---|---|---|
| OTM-at-wall (current default, ~30-40pt OTM, delta 0.10-0.20) | pre-2026-05-18 | ACTIVE | strikes.py::build_plan default behavior | live |
| ATM (5-10pt OTM, delta 0.40-0.50) for TREND_DAY/OPEN_DRIVE/DRIFT | 2026-05-22 | PROPOSED | Priority 3 ablation candidate | revised priorities 5/22 |
| Mid-OTM (~15pt) for POWER_HOUR | 2026-05-22 | PROPOSED | Priority 3 sub-rule | revised priorities 5/22 |
| Delta-targeted credit-spread strikes (CCS/PCS) | 2026-05-18 | DROPPED | shipped in `218ea7f` and `af95958` but never used post-freeze; CCS is discretionary per memory | commit `af95958` |
| bcd_atm_strike_with_be_ladder (ATM, B+C+D+BE-ladder exit) | 2026-05-26 | SHADOW | scaffolded; pending chain capture data | shadow_replay 5/26 |
| bcd_atm_minus_5pt_ITM_with_be_ladder (5pt ITM) | 2026-05-26 | SHADOW | scaffolded; pending chain capture data | shadow_replay 5/26 |
| bcd_atm_plus_5pt_OTM_with_be_ladder (5pt OTM) | 2026-05-26 | SHADOW | scaffolded; pending chain capture data | shadow_replay 5/26 |
| strike_vol_adaptive (offset_pts = clamp(VIX*1.5, 5, 40)) | 2026-05-26 | SHADOW | descriptive only — chain data starts 5/26; replay shows mean 20.6pt vs 35pt baseline | shadow_signal_replay 5/26 |
| bcd_strike_offset_10pt_with_be_ladder (10pt OTM, BSM-priced) | 2026-05-26 | SHADOW | Report 27 BSM-sweep; +$20.6k vs 35pt baseline at session mean IV | shadow_replay 5/26 |
| bcd_strike_offset_15pt_with_be_ladder (15pt OTM, BSM-priced, Report 26 rec) | 2026-05-26 | SHADOW | Report 27 BSM-sweep PRIMARY RECOMMENDATION; +$16.4k vs 35pt at session mean IV, 2.85x improvement | shadow_replay 5/26 |
| bcd_strike_offset_20pt_with_be_ladder (20pt OTM, BSM-priced) | 2026-05-26 | SHADOW | Report 27 BSM-sweep; +$11.8k vs 35pt baseline | shadow_replay 5/26 |
| bcd_strike_offset_35pt_with_be_ladder (35pt OTM, BSM-priced apples-to-apples baseline) | 2026-05-26 | SHADOW | Report 27 BSM-sweep reference; differs from R*$500 baseline because actual 35pt delta ~0.09 yields ~$70/R not $500/R | shadow_replay 5/26 |

## Family: Composite (multi-mechanism combined variants)

| Variant | Date introduced | Status | Reason for status | Last referenced |
|---|---|---|---|---|
| conjunction_req_2_AND_vol_adaptive (direction filter + vol-adaptive strike) | 2026-05-26 | SHADOW | composite variant for interaction-effect measurement | shadow_signal_replay 5/26 |
| conjunction_req_2_AND_bcd (direction filter + B+C+D peak-trail exit) | 2026-05-26 | SHADOW | new composite — most-likely live-deployment shape; graduated to ablation_backtest for DSR/PBO/CPCV/sequential bootstrap evaluation per report 22 | ablation_backtest 5/26 |

## Family: Risk management

| Variant | Date introduced | Status | Reason for status | Last referenced |
|---|---|---|---|---|
| paper_account_safety_gate | 2026-05-18 | ACTIVE | engine_change | live |
| live_order_status_sync (audit closed trades) | 2026-05-18 | ACTIVE | engine_change | live |
| auto_trade_dollar_loss_cap ($3k) | 2026-05-18 | ACTIVE | duplicated under Entry filters; also a risk control | live |
| safe_market_sell — naked-short prevention | 2026-05-22 | ACTIVE | commits `63f8f62`, `f78e8c3` (3-layer defense) | live |
| Position reconciliation orphan sweep | pre-2026-05-21 | ACTIVE | commit `c0e0d0c` | live |
| spx_sanity_guard | 2026-05-18 | ACTIVE | engine_change | live |

## Family: Sizing

| Variant | Date introduced | Status | Reason for status | Last referenced |
|---|---|---|---|---|
| Default 15/10/5 by confidence tier | pre-2026-05-18 | ACTIVE | state.auto_trade_qty_by_confidence | live |
| auto_trade_qty_override (manual override) | 2026-05-18 | ACTIVE | engine_change `auto_trade_qty_override` | live |
| Regime-demoted qty (reduced size in low-edge regimes) | 2026-05-18 | ACTIVE | regime_demoted_qty state | live |
| Contract scaling for ATM strikes (10 → 2 contracts) | 2026-05-22 | PROPOSED | dependency of Priority 3 strike selection | revised priorities 5/22 |
| 3-contract size (user reduced from 5) | 2026-05-21 | DROPPED | user discretionary; documented in `project_red_day_5_21.md` | red day memory |
| 5-contract size | pre-2026-05-21 | SUPERSEDED | reduced to 3 contracts after 5/21 red day | red day memory |
| 10-20 contract trading style | pre-2026-05-18 | ACTIVE | user-default — see `user_trading_style.md` | live |

## Family: Engine formulas / scoring

| Variant | Date introduced | Status | Reason for status | Last referenced |
|---|---|---|---|---|
| Engine v1 (1186 signal_iter rows) | pre-2026-05-18 | SUPERSEDED | replaced by v2 in commit `29eb276` | last v1 iter |
| Engine v2 (fe_ratio noise floor + drift_bias dominance ratio + others) | 2026-05-22 | ACTIVE | engine_change `v2_engine_formula_fixes` | live (220 iters tagged v2) |
| fe_ratio with `max(abs(dp), 0.1)` clamp | pre-2026-05-22 | SUPERSEDED | replaced by additive noise floor | engine v1 |
| fe_ratio with `abs(dp) + 1.0*threshold_scale` (v2) | 2026-05-22 | ACTIVE | v2 fix | live |
| drift_bias $1M absolute threshold | pre-2026-05-22 | SUPERSEDED | replaced by 15% relative dominance ratio | engine v1 |
| drift_bias 15% relative dominance (v2) | 2026-05-22 | ACTIVE | v2 fix | live |
| aligned threshold hardcoded (ignored threshold_scale) | pre-2026-05-22 | SUPERSEDED | v2 fix: now `1.0 * self._threshold_scale` | engine v1 |
| Wall proximity boundary `<= 15` (inclusive) | 2026-05-22 | ACTIVE | v2 fix: matches FEF engine | live |
| OFI sign convention | n/a | ACTIVE | audited NOT a bug (false positive in original audit) | known_engine_bugs memory |
| Microprice formula | n/a | ACTIVE | audited NOT a bug (depth-weighted mid, not Stoikov) | known_engine_bugs memory |
| mp_gravity unsigned (low-impact bug, deferred fix) | n/a | ACTIVE | known bug #9; queued post-freeze | known_engine_bugs |
| Regime classifier multi-fire (top-score wins) | n/a | ACTIVE | known bug #10; mutual exclusivity not enforced | known_engine_bugs |
| SPX bid-ask spread default 2.5% (known understatement) | n/a | ACTIVE | known bug #12; deferred | known_engine_bugs |
| DSR kurtosis fix (`(excess_kurt + 2) / 4` not `(- 1)`) | 2026-05-22 | ACTIVE | commit `b4f27cf` research-side methodology fix | _stats.py |
| Per-parent pair-off attribution | 2026-05-22 | ACTIVE | commit `b4f27cf` replaces pro-rata smearing | realistic_fill v2 |

## Family: Parameter sweep configs (walk-forward + per_regime_sweep)

These count as one trial each, per Harvey & Liu — testing N parameter values
is N trials, even if you only ship one. The per-regime sweep grid has
`4 × 4 × 3 × 4 = 192` configs per regime, but the per_regime_sweep evaluates
all of them in one analysis — those 192 grid points are not deployed-as-variants,
so each grid-search counts as ONE trial (the chosen winner). Walk-forward
auto-tuning has reported 3 distinct optimal configs across 13 runs.

| Variant | Date introduced | Status | Reason for status | Last referenced |
|---|---|---|---|---|
| wall_proximity_pts = 3.0 (walk-forward optimal) | 2026-05-18 | SHADOW | most-common walk-forward winner (5/8 windows) | walk_forward_run id=12 |
| wall_proximity_pts = 5.0 (walk-forward optimal) | 2026-05-17 | SHADOW | walk-forward winner (2/8 windows) | walk_forward_run id=13 (latest) |
| wall_proximity_pts = 8.0 (walk-forward optimal) | 2026-05-17 | SHADOW | walk-forward winner (1/8 windows) | walk_forward_run id=1 |
| wall_proximity_pts = 12.0 (per_regime_sweep TREND_DAY v2 winner) | 2026-05-22 | SHADOW | per_regime_sweep v2 picked 12 for TREND_DAY | per_regime_sweep v2 |
| wall_collapse_pct sweep (0.10, 0.15, 0.20, 0.25, 0.30, 0.40) | various | SHADOW | grid values evaluated across sweep runs | per_regime_sweep |
| wall_collapse_floor_m sweep (3, 5, 8, 10, 20) | various | SHADOW | grid values | per_regime_sweep |
| absorption_fe_ratio_min sweep (2, 3, 4, 5, 6, 7, 8, 10) | various | SHADOW | grid values | per_regime_sweep |
| Per-regime sweep v1 (linear `_proxy_pnl`) | 2026-05-21 | SUPERSEDED | methodology bias: linear proxy overstated losses | replaced by v2 |
| Per-regime sweep v2 (bar-walk + 50/30/20) | 2026-05-22 | ACTIVE | current methodology | per_regime_sweep |

## Family: Tournament presets (registered strategies)

The 6 named presets in `preset_templates.py` are first-class variants by
construction — each is a complete strategy threshold set. They've been evaluated
8 times each in `tournament_run_result`.

| Variant | Date introduced | Status | Reason for status | Last referenced |
|---|---|---|---|---|
| Preset "Default 2-min" | pre-2026-05-18 | ACTIVE | preset_id=1 (also live default) | tournament 8 runs |
| Preset "Tight Pin Day" | pre-2026-05-18 | SHADOW | preset_id=2 | tournament 8 runs |
| Preset "Trend Day Aggressive" | pre-2026-05-18 | SHADOW | preset_id=3 | tournament 8 runs |
| Preset "Volatile / Fed Day" | pre-2026-05-18 | SHADOW | preset_id=4 | tournament 8 runs |
| Preset "Chop / Sit Out" | pre-2026-05-18 | SHADOW | preset_id=5 | tournament 8 runs |
| Preset "0DTE Power Hour" | pre-2026-05-18 | SHADOW | preset_id=6 | tournament 8 runs |
| Multi-strategy "Core Bull-Drift" | pre-2026-05-18 | DROPPED | DEFAULT_STRATEGIES never wired live | multi_strategy.py |
| Multi-strategy "Wall-Test Fade" | pre-2026-05-18 | DROPPED | DEFAULT_STRATEGIES never wired live | multi_strategy.py |
| Multi-strategy "Trend-Day Aggressive" | pre-2026-05-18 | DROPPED | DEFAULT_STRATEGIES never wired live | multi_strategy.py |
| Multi-strategy "Chop-Day Survivor" | pre-2026-05-18 | DROPPED | DEFAULT_STRATEGIES never wired live | multi_strategy.py |

## Family: Data plumbing changes (research-only, no verdict change)

Per memory `feedback_ablation_over_permutation`, data plumbing variants don't
reset the freeze counter and don't change strategy direction. They're still
analytical alternatives we tested, so they go on the ledger but aren't
strategy "trials" in the DSR sense — listed for completeness, EXCLUDED from
N_trials count.

| Variant | Date introduced | Status | Reason for status |
|---|---|---|---|
| market_hours_only_default | 2026-05-18 | ACTIVE | drop pre-market noise |
| regime_backfill_on_startup | 2026-05-18 | ACTIVE | immediate regime classification |
| live_position_query_direct | 2026-05-18 | ACTIVE | closed positions disappear immediately |
| option_quote_no_generic_ticks | 2026-05-18 | ACTIVE | option pricing populates |
| option_delayed_data_fallback | 2026-05-18 | ACTIVE | paper accounts get pricing |
| account_class tagging (paper/live/mock/unknown) | 2026-05-19 | ACTIVE | filter test/mock rows |
| commission backfill on fills | 2026-05-19 | ACTIVE | post-fill broker fee capture |
| Sequential bootstrap for SR CI | 2026-05-22 | ACTIVE | AFML ch. 4 methodology |
| CPCV (Combinatorial Purged Cross-Validation) | 2026-05-21 | ACTIVE | LdP 2018 ch. 12 |
| Purged K-fold Sharpe | 2026-05-21 | ACTIVE | LdP methodology |
| PBO (Probability of Backtest Overfitting) | 2026-05-21 | ACTIVE | Bailey/LdP 2014 |
| DSR with N_trials from ledger | 2026-05-22 | ACTIVE | this very mechanism |

---

## N_trials accounting

### Counting rules (per Harvey & Liu 2015, Bailey & LdP 2014)
1. Each strategy variant tested in any backtest, ablation, or shadow replay = 1 trial.
2. Each parameter value evaluated in a grid search = 1 trial only if treated as deployable. Sweep grids with shared methodology count as ONE trial for the chosen winner (otherwise you'd double-count noise-fits the grid never proposed shipping). We're conservative: count the chosen parameter winners.
3. Variants superseded count — they were tested, then replaced. Survivorship bias would say only count active. We count them all.
4. Variants merely proposed but not yet evaluated do NOT count — no test means no multiple-testing burden yet.
5. Data plumbing changes (no verdict shift) do NOT count toward strategy N_trials.

### Roll-up

| Category | Count |
|---|---|
| ACTIVE strategy variants (live engine) | 25 |
| SHADOW (registered in DB + walk-forward optima + tournament presets) | 48 |
| SUPERSEDED | 14 |
| DROPPED | 5 |
| Sub-total (real N_trials for DSR) | **92** |
| PROPOSED (does NOT count) | 22 |
| Data plumbing (does NOT count toward strategy N_trials) | 12 |

**Reconciliation note**: the table totals above are the conservative version
that treats each registered shadow variant and each engine_change as 1 trial.
Some entries appear in multiple families (e.g., `auto_trade_dollar_loss_cap`
under Entry filters and Risk). De-duplicating across families:

| Honest N_trials (de-duplicated) | **84** |
|---|---|

`DSR N_trials should use total tested = **84**, not active count of 25 or
ablation count of 6 or the existing variant_ledger query of 12.`

### Per-family N_trials (de-duplicated counts)

For stratified DSR when comparing within a single family:

| Family | N_trials | Notes |
|---|---|---|
| Exit logic | 30 | 24 registered shadow (incl. 12 theta-tighten sweep added 5/26) + 6 SUPERSEDED/DROPPED historical |
| Entry filters | 15 | mix of ACTIVE + SHADOW + 4 PROPOSED (which DON'T count, see above; net = 11 if proposed exclusion strictly applied per-family — includes conjunction_req_2 ablation_backtest graduation 5/26) |
| Direction filter (sub-family within entry_filter, added 5/26) | 4 | conjunction_req_2 (shadow), conjunction_req_1, drop_low_counter_bias, conjunction_req_2 (ablation_backtest graduation 5/26) |
| Regime gates | 4 | tight |
| Strike selection | 12 | 1 ACTIVE + 2 PROPOSED + 1 DROPPED + 4 SHADOW chain-pending + 4 SHADOW BSM-priced strike-offset sweep (10/15/20/35pt, added 5/26 per Report 27) |
| Composite | 2 | conjunction_req_2_AND_vol_adaptive (added 5/26) + conjunction_req_2_AND_bcd (ablation_backtest 5/26) |
| Risk management | 6 | all ACTIVE — small family |
| Sizing | 7 | mix; some user-discretionary |
| Engine formulas | 13 | v1/v2 transitions count separately |
| Parameter sweeps | 9 | walk-forward + per_regime_sweep winners |
| Tournament presets | 10 | 6 named + 4 multi_strategy |

Per-family N_trials in the strict "tested only, no PROPOSED" reading:

- exit_logic: **30** (vs current ledger query: 23 after 5/26 theta-tighten sweep)
- entry_filter: **14** (10 prior + 3 direction_filter sub-family added 5/26 + 1 ablation_backtest graduation 5/26)
- direction_filter: **4** (sub-family of entry_filter; +1 from conjunction_req_2 ablation_backtest graduation)
- regime_gate: **3** (vs current ledger query: 1)
- strike_selection: **10** (3 SHADOW chain-pending + 1 ACTIVE + 1 DROPPED + strike_vol_adaptive added 5/26 + 4 SHADOW BSM strike-offset sweep added 5/26 per Report 27)
- composite: **2** (conjunction_req_2_AND_vol_adaptive + conjunction_req_2_AND_bcd ablation_backtest 5/26)
- risk_management: **6**
- sizing: **5** (excluding 2 PROPOSED)
- engine_formula: **13**
- parameter_sweep: **9**
- tournament_preset: **10**

---

## How to read this for DSR

When the methodology pipeline reports DSR at N from `get_ledger_variant_count(in_project=True)`,
that's the count from the SQL ledger which only knows about the registered
shadow_exit_replay + shadow_signal_replay variants (now 38 after the 5/26
theta-tighten sweep + 3 strike_selection scaffold variants + 5 shadow_signal_replay
verdict variants + 2 ablation_backtest conjunction_req_2 graduations + 4 BSM
strike-offset sweep variants added 5/26 per Report 27).
The honest N for *all* strategy trials in the project is **84** (or **92** if
you count multi-family entries separately, which is more conservative but
double-counts).

For a single ablation (6 variants), DSR-at-N=6 is sensible for that comparison.
For ANY claim about "the FEF strategy works in general," DSR must use
N_trials = 84 — that's the honest answer for project-level overfitting risk.

## How this file is consumed

`scripts/research/_variant_ledger.py` (companion file, machine-readable)
re-encodes this counting as a Python dict so `_stats.py::get_ledger_variant_count`
can prefer the markdown ledger's count over the 12-row DB count. The ablation
script's `dsr_at_ledger_project` field then reflects the honest N.

Refresh this file whenever:
- A new variant is registered (any family)
- A variant is dropped / superseded
- A new engine_change is shipped
- A new parameter-sweep config is evaluated
