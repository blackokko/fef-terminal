# Engine Architecture

The FEF engine is the signal-generation core of the terminal. This doc maps
the data flow from raw market snapshots to auto-trade orders, where each gate
lives, and the side-systems (regime, scanner, exit watcher, BE ladder).

## High-level data flow

```
                  ┌──────────────────────────────────────────┐
                  │ QuantData API + IBKR live tick + IBKR     │
                  │ historical bars                            │
                  └─────────────┬─────────────────────────────┘
                                ▼
                  ┌──────────────────────────────────────────┐
                  │ loop.py background tasks (per-symbol):    │
                  │   quantdata_poll_loop  (snapshot every Ns)│
                  │   tick_stream_loop     (live price)       │
                  │   bar_stream_loop      (OHLC bars)        │
                  │   microstructure_loop  (OFI/AVI/microprice)│
                  │   hft_loop             (book depth + vol) │
                  │   fef_tick_loop        ← THE MAIN LOOP    │
                  │   multi_symbol_scanner_loop (parallel SPY/│
                  │      QQQ/IWM scanner with own engine each)│
                  └─────────────┬─────────────────────────────┘
                                ▼
   ┌─────────────────── _run_one_iter ───────────────────────┐
   │  SPX sanity guard (skip if snapshot diverges >50% from   │
   │     live tick — futures-symbol stale-snapshot defense)   │
   │  ↓                                                        │
   │  state.engine.push(inputs)  →  verdict                    │
   │     engine/fef.py FEFEngine._evaluate:                    │
   │       1. wall_collapse + aligned confluence (HARD)        │
   │       2. wall_proximity_skip                              │
   │       3. aligned (drift_bias gate demotes one notch)      │
   │       4. absorption (FE >= 5 opposite-sign OR FE >= 10)   │
   │       5. fallthrough = chop (WAIT)                        │
   │  ↓                                                        │
   │  _maybe_fetch_option_targets (IBKR reqMktData)           │
   │  ↓                                                        │
   │  write SignalIter row (with regime_at_fire in extras)     │
   │  ↓                                                        │
   │  calendar pause check (demotes to WAIT during FOMC/CPI)   │
   │  ↓                                                        │
   │  regime gate (CHOP/PIN/WALL_TEST → demote one notch)      │
   │  ↓                                                        │
   │  spread_recommender (TA-based PCS/CCS suggestion)         │
   │  ↓                                                        │
   │  broadcast IterEvent (UI updates)                         │
   │  ↓                                                        │
   │  maybe_auto_trade (9-gate decision tree → BracketSpec)    │
   │  ↓                                                        │
   │  place_bracket (live_trading.py) — paper-only by default  │
   └───────────────────────────────────────────────────────────┘
                                │
                                ▼ (if order placed)
   ┌──────────────── exit watchers (parallel) ────────────────┐
   │  spx_target_watcher_loop (every 0.5s):                    │
   │     - Check stop (uses dynamic_stop_level if BE ladder    │
   │       has ratcheted it)                                    │
   │     - BE ladder ratchets stop on MFE crossings (2026-07-28  │
   │       schedule): +0.6R → BE (entry), +1.2R → +0.5R         │
   │     - Fire T1/T2/T3 market sells on SPX cross              │
   │     - RUNNER_PROTECT exits remaining qty if SPX retraces   │
   │       to T1 fill level after T1 fires                      │
   │     - dollar_loss_cap check (fire-and-forget background    │
   │       task, polls option premium every 10s)                │
   │  runner_protector_loop (legacy, mostly subsumed by         │
   │     spx_target_watcher's BE ladder)                        │
   │  spread_exit_watcher_loop (CCS/PCS exits)                  │
   │  position_monitor_loop (every 5s — writes LivePosition    │
   │     snapshots)                                             │
   └───────────────────────────────────────────────────────────┘
```

## File map

| File | Responsibility |
|---|---|
| `fef.py` | FEFEngine + `_evaluate` decision tree |
| `strikes.py` | T1/T2/T3/stop targets (anchored to 1R/2R now) |
| `auto_trader.py` | 9-gate decision tree + BlockReason enum |
| `live_trading.py` | place_bracket / place_credit_spread + paper-account safety |
| `spx_target_watcher.py` | BE ladder + T1/T2/T3 sells + dollar_loss_cap |
| `regime.py` | RegimeDetector + 10 regime archetypes (TREND_DAY etc) |
| `multi_symbol_scanner.py` | Per-symbol FEFEngine running in background |
| `spread_recommender.py` | TA-driven PCS/CCS suggestion (EMA + StdDev) |
| `spread_exit_watcher.py` | Credit-spread close at $0.05 / 4× credit |
| `daily_review.py` | Post-session computed DailyReview row |
| `tuning_recommender.py` | Rule engine for TuningRecommendation rows |
| `tuning_runner.py` | Glue: load DB → evaluate → persist |
| `what_if_backtest.py` | Replay signals through proposed filters |
| `engine_change_backfill.py` | Backfill EngineChange rows from git history |
| `regime_backfill.py` | Backfill extras.regime_at_fire on historical rows |
| `option_targets.py` | Translate SPX targets → option-price targets via delta |

## Gates and where they live

The signal pipeline has TWO layers of gates:

**Engine-level gates** (in `fef.py` `_evaluate`):
1. Warmup not complete → WAIT
2. wall_collapse + aligned confluence → fire HIGH
3. wall_proximity_skip → WAIT
4. aligned → fire (confidence via `_classify_confidence`, demoted if drift_bias disagrees)
5. absorption → WAIT
6. Default → WAIT (chop)

**Auto-trade gates** (in `auto_trader.py` `maybe_auto_trade`):
1. DISABLED — auto_trade toggle off
2. ENV_NOT_SET — FEF_LIVE_TRADING env not set
3. VERDICT_WAIT — direction is WAIT
4. BELOW_FLOOR — confidence below user's floor
5. CALENDAR_PAUSE — FOMC/CPI/NFP window
6. COOL_DOWN — per-symbol cool-down still active
7. GLOBAL_CAP / SYMBOL_CAP — hybrid session cap (5 global, 2 per-symbol default)
8. NO_OPTION_PRICING / INVALID_PRICING — option quote NaN or missing
9. NO_STRIKE — verdict has no strike_rec
10. PLACE_ERROR — IBKR submission failed

Each block writes an audit_log row with `reason_code` set to a BlockReason
enum value so `DailyReview.by_block_reason` aggregates cleanly.

## Key state on AppState

| Field | Purpose |
|---|---|
| `engine: FEFEngine` | Production engine instance |
| `shadow_engine: FEFEngine \| None` | Parallel engine for A/B testing |
| `regime_detector: RegimeDetector` | 60-sample window, classifies regime |
| `scanner_engines: dict[str, FEFEngine]` | Per-symbol engines for SPY/QQQ/IWM |
| `last_regime: RegimeAssessment` | Used by the regime gate |
| `last_snapshot` | Cached QuantData snapshot |
| `last_live_tick_spx`, `last_live_tick_ms` | Live SPX from IBKR |
| `prior_close_by_symbol` | For day-change % in Live Quote panel |
| `auto_trade_*` | All auto-trade config + counters |
| `bars_cache` | Historical-bar cache keyed by (symbol, bar_size, duration) — spread recommender's 30m/5D TA fetch + chart regression overlay |

## Lifecycle invariants

- **Engine reset on new session**: `engine.reset()` runs when `state.session_date` changes
- **One engine per symbol**: scanner engines lazily created on first scan
- **Regime detector backfill**: at startup, last 60 SignalIter rows for today are pushed into the detector so it doesn't need to warm up
- **Order status sync**: spx_target_watcher writes parent.status → PARTIAL on tier fires, → CLOSED when all tiers fired, → STOPPED on stop/dollar_stop
- **Live positions panel**: queries ib.positions() directly, NOT stale DB snapshots
- **Paper-account safety**: orders refused unless account ID starts with 'DU' (default; FEF_REQUIRE_PAPER_ACCOUNT=false to override)
