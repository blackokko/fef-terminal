# FEF Terminal — Build Report

**Author**: Claude (Sonnet via Claude Code)
**Operator**: Diante
**Period**: First full live-paper trading day, 2026-05-18, with extensive iteration

This document describes everything we built and learned in one session. It's
structured so another Claude (or any LLM/quant reviewer) can read it cold and
either critique the design or propose improvements.

---

## 1. What this is

**FEF Terminal** is a personal SPX 0DTE quant trading workstation. It combines:

- **QuantData API** for options-flow data (gamma exposure, delta exposure, max pain, drift, aggression, IV rank)
- **IBKR (Interactive Brokers)** via `ib_async` for live market data, order placement, and account state
- **A proprietary FEF (Flow Effectiveness Filter) model** that the operator developed by hand, which we encoded into a Python engine
- **A Tauri + React frontend** (Rust shell, TypeScript UI) with ~25 specialized panels
- **A FastAPI sidecar** running locally on `127.0.0.1:8765` that hosts the engine, exit watchers, scanner, and all API endpoints
- **SQLite via SQLModel + aiosqlite** for persistence — every signal, every order, every audit event
- **An MCP (Model Context Protocol) server** so Claude Code sessions can query the historical DB without running the sidecar

The system is designed for **discretionary semi-automated trading**: the FEF
engine produces verdicts ("LONG_CALLS HIGH at SPX 7410"), and the operator
either manually clicks "Send to IBKR" or has auto-trade enabled with safety
gates that fire qualifying signals automatically.

The model is the operator's intellectual property; my role was to encode it,
debug it, and build the analytical infrastructure around it.

---

## 2. The FEF model — what it actually does

### Inputs (per iteration, every 120s during RTH)

For each iteration, the engine receives a market snapshot:

- `spx_price` — current SPX index value (or whatever the active_symbol is)
- `drift_cum_net_m` — cumulative net delta-flow in $M (proxy for directional flow pressure)
- `drift_bias` — "BULLISH" / "BEARISH" / "NEUTRAL" tag from QuantData
- `top_gex_walls` — top 5 strikes by absolute gamma exposure (with net_m signed)
- `top_dex_walls` — top 5 strikes by absolute delta exposure
- `ntm_max_pain` — near-the-money pin price
- `aggression_call_pct` / `aggression_put_pct` — proportion of aggressive (market) orders on each side
- `iv_rank_call_pct` / `iv_rank_put_pct` — implied volatility rank for calls/puts

### The decision tree (engine/fef.py `_evaluate`)

The engine keeps a rolling window of the last 3 iters. On each push:

1. **Compute deltas**: `ΔPrice = price[-1] - price[-3]`, `ΔDrift = drift[-1] - drift[-3]`, `FE_ratio = |ΔDrift| / max(|ΔPrice|, 0.1)`
2. **Compute aligned**: same-sign movement AND `|ΔPrice| >= 1.0 AND |ΔDrift| >= 1.0`
3. **Compute absorption**: `FE >= 5.0 AND not same_sign AND |ΔDrift| >= 3.0` OR `FE >= 10.0 AND |ΔDrift| >= 5.0` (extended today)
4. **Risk filters**:
   - `wall_proximity_skip`: spot within 5pts of dominant wall → WAIT
   - `wall_collapse`: dominant wall lost >20% of its mass in 1 iter (only fires when in a confluence — see §4)

5. **Branch (in order)**:
   - If `wall_collapse + aligned + same direction` → fire HIGH (confluence path)
   - If `wall_proximity_skip` → WAIT
   - If `aligned` → fire (confidence via `_classify_confidence`, demoted if `drift_bias` disagrees)
   - If `absorption` → WAIT with reasoning
   - Otherwise → WAIT (chop)

### Confidence scoring (`_classify_confidence`)

- **HIGH**: `|ΔPrice| >= 5 AND |ΔDrift| >= 10 AND FE in [1.0, 5.0]`
- **MEDIUM**: `|ΔPrice| >= 2 AND |ΔDrift| >= 3 AND FE <= 8.0`
- **LOW**: everything else that's actionable

### Strike + target plan (`engine/strikes.py`)

For an actionable verdict, the engine picks:
- **strike_rec**: nearest 5-point OTM strike from spot (the contract to buy)
- **stop**: 10 SPX points against spot
- **T1**: 1R (10pts) — wall override only if 5-10pt wall exists
- **T2**: 2R (20pts) — wall override only if a wall sits at 0.8R-1.3R from T1
- **T3**: NTM max-pain (if reachable) or 5R fixed

These SPX-point targets are what the `spx_target_watcher` fires market sells against.

---

## 3. Auto-trade pipeline (`engine/auto_trader.py`)

When a verdict is actionable, the auto-trader walks 9 gates:

| Gate | Block reason code | What it checks |
|---|---|---|
| 1 | `DISABLED` | auto_trade toggle is on |
| 2 | `ENV_NOT_SET` | `FEF_LIVE_TRADING=true` env present |
| 3 | `VERDICT_WAIT` | direction != WAIT |
| 4 | `BELOW_FLOOR` | confidence >= user's floor (HIGH/MEDIUM/LOW) |
| 5 | `CALENDAR_PAUSE` | not in FOMC/CPI/NFP pause window |
| 6 | `COOL_DOWN` | per-symbol cool-down elapsed |
| 7a | `GLOBAL_CAP` | < 5 fires/session (hybrid cap) |
| 7b | `SYMBOL_CAP` | < 2 fires/session for this specific symbol |
| 8 | `NO_OPTION_PRICING` / `INVALID_PRICING` | option quote populated + finite + > 0 |
| 9 | `NO_STRIKE` / `PLACE_ERROR` | strike_rec set, IBKR submission succeeded |

Each block writes an `auto_trade_block` audit row with the structured
`reason_code` (BlockReason enum). The DailyReview aggregates these into
`by_block_reason` so the operator can see "today we had 24 cool-down blocks,
12 floor blocks, 3 invalid pricing blocks…"

If all gates pass:

1. Exit strategy picked dynamically:
   - `qty >= 15` → `fixed_dollar` (per-contract dollar math is sane)
   - `qty < 15` → `spx_points` (engages BE ladder + 50/30/20 + 1R/2R)
2. BracketSpec constructed with:
   - `use_market_entry=True` (no more limit-chases-premium failures)
   - `dollar_loss_cap=-3000` (hard $ stop)
   - 50/30/20 split (T1 50%, T2 30%, T3 20%)
   - `entry_spx_price` captured at fire time for BE ladder reference
3. `place_bracket` called with `confirm=True`
4. Counters bumped (global session + per-symbol)
5. `auto_trade_last_at_ms_by_symbol[sym]` updated for per-symbol cool-down

---

## 4. Engine changes shipped today (chronological)

| Time | Change | Category | Why |
|---|---|---|---|
| 08:00 | wall_collapse_requires_aligned | gate | Wall-collapse-only had 15% win rate; +aligned had 42% |
| 09:00 | drift_bias_gate | gate | Independent confluence layer for direction agreement |
| 09:00 | regime_demote_chop_pin_walltest | gate | Low-edge regimes demoted instead of fired |
| 09:00 | be_ladder_step_stop | exit | Profit-locking: stop ratchets at +0.5R / +1R / +1.5R MFE |
| 09:00 | split_50_30_20 | exit | Let runners run (was 85/10/5, too aggressive on T1) |
| 09:00 | t1_anchored_1r_t2_anchored_2r | exit | Predictable R:R structure for the new split |
| 11:00 | auto_trade_market_entry | exit | Eliminate limit-chases-premium failures |
| 11:00 | auto_trade_dollar_loss_cap | safety | Cap option-price collapse at -$3k |
| 11:00 | fef_high_band_fe_1_to_5 | scoring | HIGH/LOW inversion: magnitude alone was misleading |
| 11:00 | absorption_extended_to_extreme_fe | gate | FE ≥ 10 flagged absorption even same-sign |
| 11:00 | market_hours_only_default | data | Drop pre-market noise (21 fires with 0 valid outcomes) |
| 12:00 | regime_backfill_on_startup | data | Regime classification immediate on app load |
| 13:00 | live_position_query_direct | data | Stale DB snapshots → query IBKR live |
| 13:00 | option_quote_no_generic_ticks | data | IBKR error 321 (genericTickList=13 invalid for OPT) |
| 13:00 | option_delayed_data_fallback | data | Paper accounts without OPRA get model price |
| 13:00 | paper_account_safety_gate | safety | Refuse orders unless account ID starts with 'DU' |
| 14:00 | spx_watcher_interval_0p5s | exit | TP fire latency 2-7s → ~300-700ms |
| 15:00 | auto_trade_qty_override | scoring | Configurable per-fire size in panel |
| 15:00 | auto_trade_strategy_by_qty | exit | Dynamic fixed_dollar vs spx_points by qty |
| 16:00 | live_order_status_sync | safety | parent.status updates on tier fills |
| 17:00 | spx_sanity_guard | data | Skip iter when snapshot.spx diverges >50% from live tick |

All 21 are persisted to the `EngineChange` SQL table for the TuningRecommender
to correlate against DailyReview pre/post expectancy.

---

## 5. The "did the model work today?" answer

```
Theoretical signal stats (from DailyReview):
  Total iterations: 430
  Actionable (LONG_CALLS/LONG_PUTS): 115
  Wins: 23 (20.0% win rate)
  Expectancy: -0.252R per signal
  T1 hits: 10, T2 hits: 8, T3 hits: 5
  Stops: 60, Timeouts: 32

Confluence-only subset (the only profitable configuration):
  17 fires, 7 wins (41.2%), +0.369R expectancy

Aligned-only subset:
  59 fires, 13 wins (22%), -0.259R

Wall-collapse-only subset (NOW BLOCKED post-fix):
  39 fires, 3 wins (7.7%), -0.512R

Actual trades taken (from live_order rows, real fills):
  6 entries, 4 wins, 2 losses
  ~67% real win rate
```

**The theoretical signal rate is negative; the operator's manual filtering
to take only confluence + visually-confirmed setups was the value-add.**

The day saw heavy iteration:
- Two trades stopped within 3-13 minutes (the wall_collapse-direction bug
  causing LONG_CALLS into a bearish move). Fixed mid-day.
- Four winning LONG_PUTS trades in the afternoon (7355P / 7350P series),
  all hit T1 with runner_protect on T2/T3.

### One critical safety incident
The operator's TWS was logged into the live account but exposing its API on
port 7497 (paper's conventional port). The first auto-trade therefore fired
on the live account (`U20269091`). The operator caught it via the trade
appearing in their live account UI, closed it manually for a profit. We then
shipped `FEF_REQUIRE_PAPER_ACCOUNT=true` as a default safety gate that
inspects the account ID prefix (`DU` = paper, `U` = live) before submission.

---

## 6. Today's forensic findings (research)

Win/loss separation analysis on N=84 RTH signals:

| Feature | Win mean | Loss mean | Separation (σ) | Verdict |
|---|---|---|---|---|
| sec.drift (bias score) | +0.42 | +0.70 | **0.963 strong** | Already gated |
| sec.mp_gravity | +0.58 | +0.66 | 0.41 mod | Not gated |
| sec.aggression | −0.02 | +0.02 | 0.34 mod | Not gated |
| ΔDrift_M | −1.57 | −0.22 | 0.33 mod | Used by engine |
| wall_distance | 55.9 | 44.9 | 0.24 mod | Not gated |
| FE ratio | 0.55 | 0.58 | 0.02 weak | Already gated |

**Boolean discriminators:**
- `aligned=True` → 82% of winners vs 50% of losers
- `wall_collapse=True` → 38% of winners vs 62% of losers (a loser signal alone, hence the requirement gate)
- `absorption=True` → 0% in both (no actionable signal ever absorbed)

**Time-of-day pattern:**
- 8 AM CT: 30% win, slightly negative
- 9-10 AM CT: 12-20% win, badly negative
- **11 AM-12 PM CT: 54-67% win, positive**
- 1 PM CT: 0% win, -1R
- 2 PM CT: 50% win, positive

---

## 7. Architecture map

### Process layout

```
fef-terminal.exe (Tauri shell, 9MB)
   ↓ spawns
fef_sidecar (Python uvicorn @ 127.0.0.1:8765)
   ↓ connects to
TWS / IB Gateway @ 127.0.0.1:7497 (paper) or :7496 (live)
   ↓ also reaches out to
QuantData API (HTTPS)

Tauri webview ←→ sidecar via HTTP + WebSocket
```

### Background loops in the sidecar (all asyncio tasks)

| Loop | Cadence | Purpose |
|---|---|---|
| `quantdata_poll_loop` | 15s | Fetch market snapshot, write FlowSnapshot rows |
| `tick_stream_loop` | streaming | Live SPX/symbol ticks from IBKR, broadcast TickEvent |
| `bar_stream_loop` | per bar | 1-min bars (configurable), broadcast TickEvent with bar |
| `fef_tick_loop` | 120s | Main FEF iteration → write SignalIter |
| `multi_symbol_scanner_loop` | 60s | Per-symbol FEF on enabled SPY/QQQ/IWM |
| `microstructure_loop` | 0.5s | OFI / AVI / microprice (not yet gated) |
| `hft_loop` | 0.25s | Cancel-to-trade, book imbalance (not yet gated) |
| `ibkr_health_check_loop` | 30s | Ping IBKR, reconnect on failure |
| `vix_poll_loop` | 30s | VIX quote |
| `account_poll_loop` | 30s | accountSummary + reqPnL → AccountEvent |
| `position_monitor_loop` | 5s | Write LivePosition snapshots with greeks |
| `auto_walk_forward_scheduler` | 22:00 UTC daily | Run sweep → DailyReview → TuningRec |
| `runner_protector_loop` | 5s | Legacy, mostly subsumed by BE ladder |
| `spx_target_watcher_loop` | **0.5s** | The hot loop: BE ladder + T1/T2/T3 sells + dollar cap |
| `spread_exit_watcher_loop` | 5s | CCS/PCS exit at $0.05 / 4× credit |

### Persistence (SQLite tables)

```
SignalIter         — every FEF iter (430 rows today)
TradeTicket        — manual tickets when user clicks "Copy to clipboard"
LiveOrder          — every order submitted to IBKR (with BE ladder state)
LivePosition       — periodic position snapshots (with greeks)
HistoricalBar      — bar history (for chart + analysis)
FlowSnapshot       — QuantData snapshots
MicrostructureSample
VixSample
DailySession      — per-session counters (legacy)
DailyReview       — post-session computed report card (NEW today)
EngineChange      — semantic engine changes (NEW today)
ShadowComparison  — A/B test rows (NEW today)
TuningRecommendation — rule-engine output (NEW today)
AuditLog          — every gate decision, every cancel, every panic
Pattern           — user-defined pattern conditions
StrategyPreset    — named threshold sets
WalkForwardRun    — nightly optimization runs
ChartAnnotation   — user-drawn horizontal levels
CalendarEvent     — FOMC/CPI/NFP windows
```

---

## 8. The analytical infrastructure (the meta-system)

This is what separates a hobbyist quant from one that actually improves.
Built in pieces today; the full stack is now:

### `DailyReview` — the daily report card

Computed by `engine/daily_review.py` either nightly (via `auto_walk_forward_scheduler`)
or on-demand via `POST /admin/daily_review`. For each session:

- Theoretical signal counts + win/stop/timeout breakdown
- Expectancy in R-multiples (using the 50/30/20 weighted split as realized R)
- Tier hits (T1/T2/T3 counts)
- 8 by_X breakdowns: trigger, confidence, hour_ct, direction, regime,
  wall_distance, fe_ratio, block_reason
- 4 actual-trade columns: trades_taken, wins, losses, total_R (real fills)

### `EngineChange` — semantic change log

Every engine change writes a row with: name, category (gate/threshold/exit/safety/scoring/data),
description, expected_impact, measured_impact_r (filled in post-hoc), and
git_commit_sha. 21 rows backfilled for today's changes.

### `TuningRecommendation` — the auto-recommender

`engine/tuning_recommender.py` runs 5 pure-function rules over the DailyReview +
EngineChange data:

1. `_state_of_model`: always emits a STATE_OF_MODEL recommendation with backing_data.state of GREEN/AMBER/RED based on rolling 5-session expectancy
2. `_check_freeze`: if expectancy < -0.5R for 3+ consecutive sessions, emit FREEZE
3. `_check_eligible_for_change`: if confluence subset stable at >=35% for 5+ sessions AND >=5 days since last engine change, emit ELIGIBLE_FOR_CHANGE
4. `_check_feature_separation`: for each by_X bucket, if best-bucket vs worst-bucket win-rate gap >=25pp across 5+ sessions, emit PROPOSE_GATE
5. `_check_post_change_impact`: for each EngineChange, compute pre/post expectancy delta; if post < pre - 0.2R, emit ROLLBACK_CANDIDATE

Auto-runs after every `compute_daily_review` call. Also manual recompute
via `POST /tuning/recompute` with optional date range.

### `ShadowComparison` — A/B testing without risk

`POST /shadow/enable` with custom FEFEngine params spins up a parallel engine.
Both production + shadow get each iter's inputs. Auto-trade ignores shadow.
A `ShadowComparison` row records what each engine produced (direction +
confidence + reasoning) + agreement flags. Analyze afterward to see if a
proposed threshold change would have generated different signals.

### `what_if_backtest` — replay through proposed filters

`POST /engine/backtest` with `{filters: ['confluence_only', 'fe_band_1_3']}`
replays every signal_iter through the AND of those filters and returns:

- Baseline: total signals, win rate, expectancy R, total R
- Filtered: same metrics
- Expectancy delta + total R delta

Five built-in filters: `confluence_only`, `aligned_only`, `wall_proximity_5_15`,
`fe_band_1_3`, `market_hours_only`. Extending the list is one function added
to `FILTERS` dict.

### MCP server (`fef-mcp`)

Read-only access to the SQLite DB via Model Context Protocol. 15 tools total:

**Raw data:**
- `list_available_data()`
- `get_signals(from_date, to_date, direction, confidence, limit)`
- `get_trades(status, won, signal_iter_id, ...)`
- `get_bars(symbol, bar_size, from_ts_ms, to_ts_ms, limit)`
- `get_flow_snapshot_at(timestamp_ms, symbol, tolerance_seconds)`
- `get_microstructure(symbol, from_ts_ms, to_ts_ms, limit)`

**Pre-baked analyses:**
- `analyze_trade(ticket_id)`
- `daily_summary(session_date)`
- `find_similar_signals(reference_signal_id, k)`
- `attribution_breakdown(from_date, to_date)`
- `propose_pattern(based_on_signal_ids OR based_on_won_trades_last_n)`

**New today (added in the gym-session shipment):**
- `get_daily_reviews(limit)`
- `get_engine_changes(limit)`
- `get_tuning_recommendations(status, limit)`
- `get_shadow_comparisons(session_date, limit)`

Registered with Claude Code via `claude mcp add fef-quant` so any new
Claude session can do research without context bleed.

---

## 9. Frontend (Tauri + React)

~25 panels organized into 5 tabs in a sidebar layout:

**Trade tab** (the main operating console):
AccountScorecard, Calendar, RiskOverlay, Regime, LiveQuote, FEFVerdict,
HFT, Microstructure, CrossSymbol, OrderTicket, ExitStrategy, AutoTrade,
Scanner, **Tuning** (new), LiveExecution, LivePositions, OptionChain

**Data tab**: QuantData, SignalLog

**Patterns tab**: Tournament, MultiStrategy, ML, PatternLibrary, StrategyPresets

**History tab** (added 4 panels today):
**VerdictTimeline** (new), **ModelEvolution** (new), **StreamHealth** (new),
Analytics, Attribution, WalkForward, Optimize, Replay

**Config tab**: Settings, DataMaintenance, AuditLog, ChartAnnotations, Glossary

**TopBar** (always visible): Symbol selector, live price with flash, VIX,
today P&L, last iter, QuantData status, IBKR status, **MDL state pill** (new),
**🚨 FLAT button** (new), connection dot.

**Chart pane** (uses `lightweight-charts` v5):
- EMA 8 (orange) + EMA 21 (cyan)
- Centered-OLS Standard Deviation Channel (±1σ/2σ/3σ bands, gray dotted /
  amber dashed / red solid)
- Walls overlay (top GEX/DEX from snapshot)
- T1/T2/T3 / STOP horizontal lines for active verdicts
- CST timezone via `tickMarkFormatter`
- Lookback selector + symbol switcher

---

## 10. The full path to an "A" signal model (still mostly ahead)

Today: **D grade** (negative expectancy, but infrastructure complete and confluence subset has +0.37R edge).

Phased path:

**Phase 1 (Days 1-15): Data collection discipline**
- Trade under current gate stack untouched
- Daily reviews accumulate
- Look for which features SURVIVE across sessions vs noise
- *Discipline rule*: no engine changes for 10 sessions

**Phase 2 (Days 16-30): Replace hand-coded confidence with trained classifier**
- Scikit-learn scaffolding already in `ml_patterns.py`
- Train logistic regression / random forest on (every feature) → P(win) using DailyReview labels
- Replace `_classify_confidence` HIGH/MEDIUM/LOW thresholds with calibrated P(win) score
- Expected lift: 5-10pp win rate

**Phase 3 (Days 31-60): Add 2-3 orthogonal signals**
- Microstructure (OFI/AVI/microprice): different timeframe, independent of FEF
- HFT (cancel-to-trade, book imbalance): different mechanism
- VIX state: regime variable that's currently informational only
- Each orthogonal 60%-accurate filter compounds: 40% → 55%

**Phase 4 (Days 61-90): Regime-specific thresholds**
- Train separate parameter sets per regime (TREND_DAY vs CHOP vs OPEN_DRIVE)
- WalkForwardRun already runs nightly; output per-regime thresholds instead of one global

**Phase 5 (Days 91+): Pattern-driven entries**
- Invert current architecture: 5-10 named setup patterns become the entry trigger
- FEF/confluence/drift become scores on each pattern
- Pattern table already exists

---

## 11. Operational details (for reproducing the workflow)

### Launch options
- `FEF-Paper.bat` — paper account, no auto-trade
- `FEF-Paper-AutoTrade.bat` — paper account + `FEF_LIVE_TRADING=true` env (auto-trade can fire)
- `FEF-Live.bat` — live account (DANGER)
- `FEF-Live-AutoTrade.bat` — live + auto-trade (MAX DANGER)
- `FEF-Mock.bat` — mock providers, replay-driven testing

Both AutoTrade bats now include `FEF_LOOP_MARKET_HOURS_ONLY=true` to skip
pre-market noise.

### Key env vars
- `FEF_LIVE_TRADING` — required for any real order submission
- `FEF_REQUIRE_PAPER_ACCOUNT` — default true; refuses non-DU account IDs
- `FEF_LOOP_INTERVAL_SEC` — FEF iter cadence (default 120)
- `FEF_REGIME_WINDOW` — regime detector samples (default 60)
- `SPX_WATCHER_INTERVAL_SEC` — exit watcher polling (default 0.5)
- `FEF_TICK_STALL_THRESHOLD_SEC` — tick stream watchdog (default 30)
- `FEF_MAX_CONTRACTS_PER_TRADE` — cap on auto-trade qty (default 20)

### Daily workflow
1. Launch via `FEF-Paper-AutoTrade.bat`
2. Verify in-panel: AutoTrade hero shows "LIVE ENV READY", Scanner shows desired symbols
3. Configure: confidence floor (HIGH/MEDIUM/LOW), cool-down, max-per-session, contracts-per-fire
4. Click ENABLE AUTO-TRADE (window.confirm popup)
5. Monitor: TopBar MDL pill, OrderTicket panel for verdicts, AuditLog for blocks
6. End of session: `POST /admin/daily_review` (or wait for 22:00 UTC scheduler)
7. Review: TuningPanel, ModelEvolutionPanel, VerdictTimelinePanel

### Backfill commands (one-time after relaunch)
```bash
curl -X POST http://127.0.0.1:8765/admin/backfill_engine_changes
curl -X POST http://127.0.0.1:8765/admin/backfill_regime -d '{}'
curl -X POST http://127.0.0.1:8765/admin/daily_review -d '{"session_date":"2026-05-18"}'
curl -X POST http://127.0.0.1:8765/tuning/recompute -d '{}'
```

---

## 12. Specific things that would benefit from external review

If you (Chat Claude or another reviewer) wanted to add the most value, these
are the points most worth scrutinizing:

### A. The FEF model's core assumptions

The whole engine pivots on `ΔPrice`, `ΔDrift`, `FE ratio`, and `aligned`.
These are computed over a 3-iter window (so ~6 minutes of context).

**Question**: Is a 6-minute window the right horizon for these signals? Why
not 10 minutes? Why not 2? The choice was made empirically by the operator
before this codebase existed. Worth re-examining with a sweep.

### B. The confidence scoring is still hand-coded

Three thresholds for HIGH (`|ΔPrice| >= 5`, `|ΔDrift| >= 10`, `FE in [1,5]`).
Three for MEDIUM. Today's data showed HIGH was inverted vs LOW (14% vs 41%).
The drift_bias gate + regime gate should fix it, but the underlying
assumption that magnitude alone classifies confidence is questionable.

**Question**: What does a trained classifier on today's labels say is the
right separator? Is there a single feature (or combo) that beats `|ΔDrift|`?

### C. The R:R is asymmetric in a bad way (today: 1:1 at T1)

Stop = 10pts, T1 = 10pts → 1:1 R:R at the first exit (which gets 50% of size
under the new split). Math says we need 55%+ win rate to be profitable at
1:1. Today was 20%. Even with confluence at 41%, we lose at 1:1.

**Question**: Should T1 be at 1.5R or 2R instead of 1R? Trade-off: less of
the position exits at T1, more rides into T2/T3 where MFE data says 80% of
winners reach 2R MFE.

### D. The wall_collapse trigger logic

After my fix, wall_collapse requires:
- `prev_top` wall (by `abs(net_m)`) lost >20% in 1 iter
- AND aligned=True
- AND aligned direction matches collapse_dir (call_wall_above → LONG_CALLS,
  put_wall_below → LONG_PUTS)
- AND drift_cum_net_m sign agrees

That's 4 gates on a single trigger. Maybe too much. Today only 17 signals
passed, of which 7 won (41%). Sample is tiny.

**Question**: Is the "wall lost 20% in 1 iter" signal even real? Or is it
just noise in QuantData's snapshot? Worth a session of detailed examination.

### E. The 50/30/20 split + BE ladder is untested

Today's session had the 50/30/20 split + BE ladder code shipped mid-day.
Most of today's signals fired under the old 85/10/5. We don't yet have
real data on whether the new split actually improves realized R.

**Question**: Run `POST /engine/backtest` with `filters=['confluence_only']`
and inspect — but the backtest uses the same 50/30/20 weighted R as DailyReview,
so the answer is "yes it improves on theoretical signals". The empirical
test needs real fills.

### F. The MEDIUM=50% (n=4) vs HIGH=14% (n=72) result

This screams that confidence scoring is wrong. After today's `fef_high_band_fe_1_to_5`
fix, the distribution should change. But it's a strong indicator that the
operator's intuition about magnitude-driven HIGH is misaligned with what
actually works.

**Question**: Should HIGH require confluence (wall_collapse + aligned) AND
nothing else? That'd be the cleanest interpretation.

### G. We're not using microstructure or HFT signals at all

Both subsystems compute real data (OFI, AVI, microprice, cancel-to-trade,
book imbalance, weighted_mid_price) every 0.25-0.5s. None of those values
feed into the FEF decision tree. They show in panels but don't gate.

**Question**: Worth a session of building a "microstructure agreement" gate?
The operator deferred this until more sessions of data.

---

## 13. The "Things I'd explicitly NOT do" list (deferred for operator review)

These were flagged at the start of my unsupervised work and held for review:

- Auto-pattern-proposal automation (engine watches DB and proposes new
  Pattern rows from winning streaks without user input)
- Any new engine gate beyond what's shipped
- Anything that auto-modifies thresholds or auto-applies recommendations
- Anything touching live order routing

---

## 14. Today's commit log (engine + analytics)

`git log --oneline --since="2026-05-18"`:

```
b3xxx Ship 14: tuning + engine-change + tests + topbar + shadow + backtest + chart + docs + health + flat + enum + regime backfill + timeline + mcp
569cbf2 Engine: SPX sanity guard
27c4847 Daily review: ship A + B + C
71e4788 Auto-trade exit strategy: pick by qty
e74533d LivePositionsPanel: null-safe rendering
b6fc72b FEF iteration: split 50/30/20, T1=1R/T2=2R, drift bias gate, BE ladder, regime gate, DailyReview
a7911ce Auto-trade: configurable contract size override
fe1b6c5 Live positions: query IBKR directly
e6b186b spx_watcher: speed up TP fills
9671de1 FEF_LOOP_MARKET_HOURS_ONLY=true in -AutoTrade.bat
a8faa8b FEF engine: gate wall_collapse on aligned
fb3430b Scanner: stop broadcasting IterEvent
c7acdce Fix: all exit strategies honor use_market_entry
387b64f Kill All Orders: refresh + reqGlobalCancel
de93027 Fix sidecar startup crash: scanner import
9ab0518 Scanner: UI panel, drop futures, per-symbol cool-down
c03db52 Multi-symbol scanner foundation
9351180 LiveExecutionPanel: render in CT
0f4cb35 spx_watcher: verify long position before SELL
f5e9148 Backfill regime detector from DB on startup
af1f53c Fix wall_collapse direction logic + drift confluence guard
7e1274e Auto-trade safety: market entry + $3k dollar loss cap
f7dd6e4 Refuse to submit orders to non-paper IBKR accounts
b1e6d58 Enable delayed market data + model-price fallback
492bdd2 Fix realized/unrealized P&L: subscribe to reqPnL
323dfd4 Drop invalid genericTickList=13 from option reqMktData
5cb1a90 Capture IBKR errors during reqMktData + /diag/option_quote
4f0ba43 Audit-log option quote failures
4d0ea0d Auto-trade visibility + timestamp display fixes
62de1dc OrderTicket: visible banner explaining why Send is disabled
54b2bd5 Fix two bugs found during live paper testing
... (30+ commits today)
```

---

## 15. Summary for the reviewer

**What was built**: Full-stack paper-trading workstation with discretionary
auto-trade, multi-symbol scanner, exit-watcher hot loop, paper-account
safety gates, dual-engine A/B testing infrastructure, daily review pipeline,
and rule-based tuning recommender — built and operated in one trading day.

**What works**: Confluence (wall_collapse + aligned) subset shows +0.37R
edge over today's signals. The infrastructure (data persistence, audit
logging, exit watchers, scanner, MCP) is solid.

**What doesn't**: Theoretical model is at −0.25R expectancy. HIGH/LOW
confidence inversion suggests the scoring is wrong. Today's data is N=1
session so most tuning is overfit-bait.

**What's needed**: Discipline to collect 10+ sessions of data before tuning
anything else. Then a trained classifier on the labeled DailyReview data,
adding microstructure/HFT/VIX as orthogonal signals, and eventually
pattern-driven entries.

**What I'd most want a reviewer to challenge**: The FEF model's core
assumptions (window size, FE ratio interpretation, magnitude-based
confidence, wall_collapse meaning). Those are inherited from the operator's
intuition and haven't been statistically validated.
