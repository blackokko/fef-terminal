# Deferred Changes — End-of-Day Review Queue

Items flagged during sessions but NOT yet shipped, so the freeze counter
can run uninterrupted. Each item notes (1) what's broken or could be
improved, (2) what category of change it is, (3) whether shipping would
reset the freeze counter.

**Freeze status**: 0/10 sessions since last engine change. Reviewer
(Chat Claude) recommends no engine changes until ≥10.

Add new items at the top. Move shipped items to the "Shipped" section.

---

## POST-FREEZE SHIP ORDER (re-ranked 2026-05-20 via ablation backtest)

**Re-ranked from Chat Claude's original order after ablation backtest
(scripts/research/ablation_backtest.py) on 186 fired signals across
5/18-5/20. Data-driven priority, not narrative-driven. Re-run via
`python scripts/research/research_dump.py 2026-05-18 2026-05-20`.**

| Variant | Sum R | $ P/L (5ct × $400/R) | WR | vs Baseline |
|---|---:|---:|---:|---:|
| Baseline (current) | −21.25 | −$8,500 | 40.8% | — |
| Direction filter only | +8.25 | +$3,300 | 51.6% | +$11,800 |
| **B+C+D only** | **+14.52** | **+$5,808** | **70.4%** | **+$14,308** |
| Both stacked | +14.66 | +$5,864 | 72.3% | +$14,364 |

**Stacking direction filter on top of B+C+D adds +$56 — they overlap.**
B+C+D's peak-trail captures upside on whichever side was right intraday;
the −1R stop caps downside that direction filter would also block. The
two filters protect against the same losses, so the second one in stack
adds nothing.

1. **B+C+D exit-strategy upgrade** (NEW #1) — variant 2b option-price
   BE ladder + dual SPX/option T1/T2/T3 + 15% peak-trail + time-decay-aware
   T1. Ablation flips 5/18 from −29.95R to +3.00R (the chop-day disaster
   that direction filter alone fails to fix). WR rises 40.8% → 70.4%.
   Mechanism is auditable: 102/186 trades exit via peak-trail capturing
   partial MFE before reversal.
2. **Per-cluster stacking cap** (max 2 fires) — Shapley showed stacking
   adds risk without proportional reward; tail-risk concentrates at 3+
   stacks. Independent of B+C+D — reduces position size on losers.
3. **Regime / chop filter** — would address residual 5/18 false-signal
   exposure that B+C+D doesn't fully fix. Suppress fires when SPX in
   chop regime. Lower priority than originally ranked since B+C+D already
   recovers most of the chop-day damage via peak-trail exits.
4. **Direction-of-day filter** (DEMOTED from #1) — adds essentially zero
   when B+C+D is in place. Worth re-evaluating ONLY if (a) intermediate-
   timeframe EMA stack version performs materially differently than the
   30m-SPX-change proxy I tested, or (b) sessions 4-10 show direction
   confusion on days where peak-trail also fails. Otherwise skip.
5. **Microstructure quality gate** — weaker mechanism (n=27 correlation,
   no clear causal story). Test only if 1+2+3 don't close the gap.
6. **Polling/scanner adjustments** — multi-symbol expiry resolution.
7. **Calibration / preset differentiation** — only if there's a workable
   system worth tuning by then.

**Ablation methodology** (per Chat Claude 2026-05-20): test each change
INDIVIDUALLY against current baseline using the what-if / counterfactual
infrastructure on the existing dataset. Bundle changes that move
realistic-fill P/L positively into a new baseline, then test the next.
Linear, not combinatorial. Stop adding layers as soon as the system is
workable — each additional layer is another thing that can break in live.

**Caveats on the 5/18-5/20 ablation** (sample size + in-sample):
- 186 fired signals over 3 days is thin. Out-of-sample validation needs
  sessions 4-10 of the freeze.
- Both filters were designed FROM 5/18-5/20 data, so the lift numbers
  are upper-bound estimates. Expect 30-50% degradation on out-of-sample.
- B+C+D simulation uses SPX as option-price proxy (linear). Reality
  has gamma asymmetry (helps winners more) + theta decay (subtracts
  ~5-10%/hr from option premium). Net: probably slight overstatement
  on 60min windows.
- Direction filter uses 30min SPX change > 3pt. EMA-stack version may
  perform differently but on 1min bars the signals are correlated.

**Ablation methodology** (per Chat Claude 2026-05-20): test each change
INDIVIDUALLY against current baseline using the what-if / counterfactual
infrastructure on the existing 10-session dataset. Bundle the ones that
move realistic-fill P/L positively into a new baseline, then test the next.
Linear, not combinatorial — 7 changes in 7 evaluations, not 128. Stop
adding layers as soon as the system is workable (each additional layer is
another thing that can break in live).

---

## OPEN ITEMS

### 2026-05-21 — Known data-integrity issues (ongoing audit, not all bugs yet) [LIVING LIST]
Discovered during 5/20 daily_review debugging. Each one risks
silent misinterpretation of strategy performance. Cross-check
ibkr_execution dollar sums against everything else.
- **live_order.filled_qty / avg_fill_price never populated** on
  parents (all NULL on 5/20's 61 parents). The ENTRY fill info is
  lost. Workaround: pull from ibkr_execution by ib_order_id.
  Where: live_trading.py around place_bracket(). Needs ORDER_STATUS
  callback to update parent row on fill.
- **Bracket child orders not persisted as live_order rows**. Only
  parents have rows; the T1/T2/T3/STOP child orders never get
  written. Means per-parent exit attribution requires matching
  ibkr_execution by (symbol, strike, right, date) with no clean
  link back to a specific parent when stacking. Fix: persist
  child orders with parent_id pointing back.
- **SignalIter.spx_price corruption** — 15 rows on 5/18, 4 on 5/19,
  5 on 5/20 had spx_price=67.2 (or similar non-SPX value). Probably
  a SPY/futures→SPX conversion bug. Filter is `BETWEEN 1000 AND
  10000`. Root cause: see loop.py snapshot read; suspect upstream
  fallback when SPX quote unavailable.
- **SignalIter.extras numeric garbage** — some delta_price values
  reach +3508 (impossible for an SPX delta), pa_score/agg_score
  occasionally clamp to ±1.0 placeholders, nearest_wall_distance
  hits +7317pts. Suggests fallback values are leaking into the
  signal record. Audit each upstream computation for defensive
  defaults that should have been None.
- **HFT/micro snapshot coverage gap** — 65/218 fired signals 5/18-
  5/20 have hft_at_fire/microstructure_at_fire. Feature rolled out
  mid-day 5/19. Don't draw HFT conclusions until coverage is 100%
  (~5/25 onward).
- **daily_session.gross_pnl/net_pnl never written** — all five rows
  have $0. Either nothing populates this table or it's only filled
  by operator manual entry. Decide whether to compute auto from
  ibkr_execution or remove the field.
- **Cross-check queries to run periodically**:
  ```sql
  -- daily_review.actual_total_R × R_DOLLARS should ≈ ibkr_execution sum
  SELECT dr.session_date, dr.actual_total_R,
         (SELECT SUM(CASE WHEN side='SLD' THEN shares*price*100
                          ELSE -shares*price*100 END)
          FROM ibkr_execution WHERE session_date=dr.session_date) AS realized_$
  FROM daily_review dr;

  -- live_order parents on date X should ≈ count of distinct ib_order_id
  -- in ibkr_execution where side='BOT' on same date
  ```
- **Resets freeze?**: NO — all observation / read-side / fill-tracking.
  None changes signal generation or exit logic.

### 2026-05-20 — daily_review accounting bugs invalidate session-level metrics [SHIPPED 2026-05-21]
- **Status**: Fixed in `engine/daily_review.py`. Two changes:
  1. `_trace_outcome` caller restructured to credit partial scale-outs
     (T1/T2/T3) even when residual subsequently stopped. Old logic
     classified any STOPPED trade as -1R outcome; new logic computes
     weighted 50/30/20 partial-bank + residual-stop R. tier_counts
     now reflects the highest tier reached per signal, so top-level
     `wins / t1_hits / t2_hits / t3_hits` columns match
     by_direction/by_hour_ct/by_regime sub-aggregates.
  2. `actual_total_R` now sourced from session-level ibkr_execution
     dollar sum ÷ `FEF_R_DOLLARS` env (default 400). Ground truth.
     Verified 5/20: -29.5R → +17.04R (= $6,815 / $400/R, matches
     IBKR realized P/L exactly).
  3. New `_estimate_parent_r()` helper uses spx_target_t{1,2,3}_fired
     + qty splits + dynamic stop level for per-parent classification
     (actual_wins / actual_losses). Approximate — assumes 1:1 option-
     to-SPX move; for dollar-accurate per-parent R needs the bracket-
     child fill linking (see data-integrity list above).
- **Verification script**: scripts/research/research_dump.py
  will surface the fix on next daily_review recompute (scheduled
  nightly at 22:00 ET via auto_walk_forward, or manual via
  POST /admin/daily_review).
- **Resets freeze?**: NO — measurement-only fix.

### 2026-05-20 — daily_review accounting bugs invalidate session-level metrics [SUPERSEDED — see SHIPPED entry above]
- **Symptom**: 5/20 IBKR realized P/L = **+$6,815** but daily_review reports
  `wins=0, t1_hits=0, t2_hits=0, t3_hits=0, win_rate_pct=0.0,
  actual_total_R=-29.5`. Two stacked bugs:
  - **Bug 1 — top-level rollup is zeroed**: `wins/t1_hits/t2_hits/t3_hits`
    are 0 while every sub-aggregate in the same row agrees on 9 wins
    (`by_direction LONG_CALLS=3 + LONG_PUTS=6`, `by_hour_ct=9`,
    `by_regime=9`, `by_confidence=9`). Aggregation path for those four
    columns is broken.
  - **Bug 2 — actual_total_R ignores partial scale-outs**: live_order
    on 5/20 shows 46 parents with `status=STOPPED` AND all three
    `spx_target_t{1,2,3}_fired=1`. These are the 50/30/20 scale-outs
    where T1/T2/T3 all fired BEFORE the 20% residual hit the BE-ladder
    stop. Engine credits each as -1R; reality is closer to +1.5R per
    trade (0.5×1R + 0.3×2R + 0.2×~0R = +1.1R minimum). 46 trades
    mispriced ≈ 70R total error.
- **Why it matters**:
  - Reviewer narratives ("5/20 bloodbath", "execution-dependent edge")
    are sourced from these fields. Both readings are wrong on 5/20.
  - Shapley Run A / Run B decomposition uses actual_total_R as input.
    The signal-vs-execution split is corrupted; signal contribution
    may be understated and execution overstated.
  - dedup_expectancy_r and sharpe_ratio derive from the same broken
    accounting. dedup-near-neutral may be artifact, not signal.
  - daily_review.WR is the operator's session report card. 0% WR on
    a +$6.8k day destroys signal-vs-actual trust.
- **Where to look**:
  - `engine/daily_review.py` — function computing top-level `wins/
    t{1,2,3}_hits` (likely a `sum(by_direction.wins for ...)` or
    similar that's pulling from the wrong dict key)
  - Same file — function computing `actual_total_R`. Currently treats
    parent.status as the source of truth. Should aggregate partial
    fills: read `spx_target_t1_qty * 1.0 + t2_qty * 2.0 + t3_qty *
    3.0` minus `residual_qty * 1.0` (if stopped) for each parent.
- **Verification query**:
  ```sql
  SELECT status, spx_target_t1_fired, spx_target_t2_fired,
         spx_target_t3_fired, COUNT(*)
  FROM live_order
  WHERE bracket_role='parent' AND dry_run=0
    AND DATE(submitted_at)='2026-05-20'
  GROUP BY 1,2,3,4;
  ```
- **Fix scope**: ~2 hours. Pure read-side; doesn't change which trades
  fire, only how outcomes are summarized.
- **Resets freeze?**: **NO**. This is a measurement/reporting bug, not
  an engine signal-generation or exit-logic change. The actual trades
  fired identically with or without this fix. Recompute is read-only.
- **Re-run after fix**:
  - Recompute daily_review for 5/18, 5/19, 5/20
  - Re-run ablation backtest — the baseline R-unit math uses
    would_have_hit_t1/stop from lookahead so it's mostly independent,
    but the "both T1 and stop hit → -0.15R" assumption needs review
    against real partial-fill economics
  - Re-evaluate the Shapley Run A/B/C/D decomposition; signal share
    is likely larger than reported
- **Where this came from**: investigated 2026-05-20 evening session.
  IBKR realized P/L cross-checked via direct sum of ibkr_execution
  rows: BOT notional=$93,535, SLD notional=$100,350, gross +$6,815.

### 2026-05-20 — Direction-of-day filter [POST-FREEZE TOP PRIORITY]
- **Why this is now #1** (per Chat Claude 2026-05-20 review of lookahead
  data): on 5/19, LONG_CALLS had 94% T1-hit and LONG_PUTS had 85% stop-hit
  — same engine, same session, opposite outcomes. On 5/20 the split is
  even cleaner: calls 100% T1, puts 100% stop, avg MAE on puts −22pts.
  Engine is firing puts on days where macro direction is clearly up. This
  isn't sample noise — it's a structural direction-confusion problem.
- **Hypothesis**: the engine's flow signal is GOOD at finding entries but
  has NO context of whether the broader move is with or against the entry.
  Wrong-side fires are the catastrophic losses; right-side fires are the
  big wins. The dedup-R neutral comes from these canceling.
- **Why this beats microstructure / exit fixes**: entry gates prevent
  bad trades from being TAKEN; exit fixes only reduce the damage on
  trades already opened. Higher leverage. Also addresses the Run A
  signal-alone net-negative finding more directly than execution rescue.
- **Three implementation candidates**:
  - **A. EMA-stack permission gate** — require 30m or 1h 8/21 EMA
    bullish stack with rising slope before allowing LONG_CALLS; mirror
    for puts. Dale's framework applied as a filter on top of flow.
  - **B. Drift-bias / intermediate-EMA agreement** — require
    `drift_bias` direction matches an intermediate-timeframe EMA
    direction before firing.
  - **C. Higher-timeframe trend overlay** — SPX above N-period MA on
    a 30m chart and trending up → block puts; mirror for calls.
- **Ablation backtest to run BEFORE shipping**: replay 5/18-5/20 with
  each candidate filter applied retroactively. Count: how many bad
  fires blocked, how many good fires preserved, realistic-fill P/L
  delta. This is doable on existing data — lookahead + SignalIter rows
  + 1min SPX bars are all there.
- **Open question**: 5/18 was a chop day where both sides lost — a
  direction filter wouldn't have helped (no clear direction to gate on).
  Need separate regime filter for chop days (next item).
- **Category**: New entry gate. Reduces volume, intended to raise WR.
- **Resets freeze?**: YES (changes which signals fire). Top of the
  post-freeze ship queue.

### 2026-05-20 — Regime / chop filter [POST-FREEZE #2]
- **Why**: 5/18 LONG_CALLS had 63% stop-hit at 60min and Close Δ −7.8pts
  — calls fired into moves that reverted. Direction filter doesn't catch
  this because there was no clean direction on 5/18. Need separate gate
  that recognizes chop regime and suppresses fires entirely.
- **Three implementation candidates**:
  - **A. ATR-based volatility regime** — block fires when ATR is in
    contraction relative to recent baseline (low realized vol = chop).
  - **B. Range/trend classifier** — explicit classification of session
    as TREND vs CHOP based on price extension vs range over rolling
    window. Block fires when CHOP.
  - **C. 30m-open-hold rule** — SPX above 30min open + holding =
    bullish regime, allow calls only; mirror inverse. Sit out when
    price oscillates around the 30min open.
- **Ablation backtest**: same as direction filter — replay 5/18 with
  each candidate, count avoided losses.
- **WAIT validation in chop**: lookahead data shows WAIT verdicts
  correctly skip chop on 5/19/5/20 (avg MFE only 12-22pts). But we
  don't yet have evidence WAIT works in a CHOP regime — 5/18 had 197
  WAIT signals; should run a separate aggregate to see if WAIT's MFE
  stays low on chop days too. If yes, the engine's WAIT layer already
  handles chop and the new regime gate is redundant. If WAIT MFE
  balloons on 5/18, the gate is needed.
- **Category**: New entry gate. Reduces volume in adverse regimes.
- **Resets freeze?**: YES. Second in post-freeze queue.

### 2026-05-20 — Lookahead "non-fire blind spot" — VALIDATED
- **Result of analysis run 2026-05-20** (496 signal-lookahead rows
  across 5/18-5/20 after backfilling 5/18):
  - WAIT verdicts at 60min lookahead: MFE +12 to +29pts, MAE only −2
    to −8pts. Engine is NOT silently missing big moves at the 60min
    horizon. The WAIT layer is doing its job.
  - 5/19 calls 94% T1-hit, 5/20 calls 100% T1-hit — signal generation
    is genuinely strong on right-side directional fires.
  - 5/19 puts 85% stop-hit, 5/20 puts 100% stop-hit — wrong-side
    fires are the catastrophic losses. (See direction-of-day filter.)
  - 5/18 calls 63% stop-hit + close Δ −7.8 — chop-day false signals.
    (See regime filter.)
- **Implication**: problem isn't "more aggressive entry criteria
  needed." Problem is "better filters on entries already being taken."
  Easier problem to solve, more concentrated engineering effort.
- **Data quality note**: 6 SignalIter rows on 5/18 had `spx_price=67.2`
  (likely SPY-vs-SPX ingest bug). Filtered for the aggregate. Worth a
  separate look at the upstream write path — search for any conversion
  that would produce ~67 from a real SPX price.
- **Category**: Pure analytics finding. No engine impact.
- **Resets freeze?**: NO.


### 2026-05-19 — Auto-trade stacks contracts on existing positions
- **Symptom**: User saw 7400C grow to 20 contracts as subsequent signals
  fired while the position was already open. With 5-contract auto-trade
  size, 4 successive same-strike fires = 20 contracts on one contract.
- **Why this happens**: The 9-gate auto-trader doesn't check existing
  IBKR positions before submitting a new BUY. It checks confidence,
  cool-down, session cap, etc., but treats each fire as independent.
  When the cool-down is lowered for data-gathering velocity, the same
  contract can fire multiple times before earlier positions resolve.
- **What it puts at risk**:
  - The dollar_loss_cap is per-parent ($3k each), so 4 parents = $12k
    of potential drawdown on one contract instead of $3k
  - The BE ladder is per-parent. 4 parents on same contract = 4
    different `dynamic_stop_level` values, all trying to manage the
    same underlying long
  - The position-check we added prevents accidental shorting on EXIT,
    but doesn't prevent stacking on ENTRY
  - SYMBOL_CAP gate (max 2 per session per symbol) was supposed to
    cap this, but user has it at higher / disabled for data collection
- **Two fix variants**:
  - **A (per-contract cap)**: Gate auto-trade — if IBKR positions
    already include this exact (symbol, strike, right, expiry) at >=N
    contracts, block with `BlockReason.POSITION_ALREADY_OPEN`. N is a
    user setting (default 1: never stack).
  - **B (per-contract cool-down)**: Track last-fire time per
    (strike, right, expiry) tuple instead of per-symbol. So even with
    a short symbol-cool-down, the exact same contract requires a
    longer fresh-trade cool-down (say 5 min).
- **Current workaround**: Keep SYMBOL_CAP low (2/session per symbol) +
  use longer cool-down. User said they want short cool-down for max
  data volume, so this is a real tension.
- **Resets freeze?**: YES (new auto-trader gate). But it's a safety
  feature, not engine tuning — defensible whenever the user wants it.

### 2026-05-19 — 2-iter wall_collapse persistence may be too strict
- **Symptom**: TODAY zero confluence signals fired (0/22 actionable were
  confluence; all 22 were aligned_only). User noticed only aligned-only
  was firing despite having both trigger types checked.
- **Root cause**: The 2-iter persistence we shipped yesterday (reviewer's
  Priority 4) requires the wall to lose >20% across BOTH iter[-2]→[-1]
  AND iter[-3]→[-2]. Effectively ~36%+ cumulative drop over 4+ minutes
  on a 120s cadence. Very rare.
- **Yesterday's data (under old 1-iter rule)**: 17/115 actionable were
  confluence (~15%), of which 7 won (41% win rate, +0.37R).
- **Today's data (under new 2-iter rule)**: 0/22 confluence.
- **Three interpretations**:
  - 2-iter persistence is correct and wall collapses are rare → keep
  - 2-iter is too strict → relax to 1-iter, or try alternative gates
    (e.g. iter[-1] vs iter[-3] >25%)
  - Today's market just didn't have collapses → wait for more sessions
- **Decision criterion**: After 5 more sessions, if confluence rate
  stays at 0-2%, the persistence is overkill — revert to 1-iter OR
  switch to a 2-of-3-window vote variant. If confluence rate climbs
  back to ~5-15%, the 2-iter filter is working as intended.
- **Resets freeze?**: YES.

### 2026-05-19 — BE ladder UI label is misleading [SHIPPED 2026-05-19]
- Fixed in commit. LiveExecution panel now computes true locked-R from
  `(dynamic_stop_level - entry_spx_price) / risk` instead of displaying
  the trigger-R as if it were locked profit. New states:
    - `armed (orig stop X)` — no ratchet yet
    - `at BE (stop X, trigger 0.5R)` — stop moved to entry, 0 actually locked
    - `locked +0.5R → stop X (trigger 1.0R)` — actual locked profit shown

### 2026-05-20 — Stacking cap candidate (per-cluster max 2 fires) [POST-FREEZE STRUCTURAL TEST]
- **Why now elevated**: tonight's Shapley decomposition v1 surfaced an
  unexpected finding (per Claude Chat 2026-05-20 review):
  - Run C (stacking + pure SPX exits) is **MORE negative** than Run A
    (single-fire + pure SPX exits) — stacking with weak exits is WORSE
    than no stacking
  - Run D (actual full system) appears LESS profitable than Run B
    (single-fire + BE simulated) — stacking + execution is comparable
    to or less than single-fire + execution alone
  - Implication: stacking adds RISK without proportional REWARD. Edge
    might be execution-only, with stacking inflating tail-risk exposure
    (35-stack on losers = −$15-30k tail) without expected-value gain.
- **Counter-evidence**: fire_order analysis showed fire_order=2 had
  58.8% WR vs 50% on first-fires under ACTUAL execution. So stacks
  DO win more often — they're just more execution-dependent.
- **Reconciliation**: stacks need BE ladder + watcher to be profitable.
  When execution gets paper-to-live haircut (30-60%), stacks may
  collapse faster than first-fires.
- **Proposed structural test (post-freeze)**: per-cluster max 2 fires
  (`fire_order ≤ 2`). Captures first-fire + persistence-confirmation
  signal benefit. Drops 3+ stacks (where tail risk concentrates).
  Live-haircut robust because each individual position is smaller.
- **Implementation**: new auto-trader gate — block when fire_order > 2.
  ~30 min change. Defensive constraint, not engine tuning.
- **Decision criterion**: run side-by-side variants in the post-freeze
  shadow phase — current unlimited stacks vs 2-fire-max. Compare:
    (a) Realistic-fill P/L per session
    (b) Max drawdown
    (c) Variance / Sharpe
  If 2-fire-max has 70%+ of P/L with 30%+ less drawdown, switch live.
- **Category**: Defensive auto-trader gate. Reduces volume.
- **Resets freeze?**: YES (changes which trades fire). Bundle with
  variant 2b + B+C+D + microstructure gate post-freeze.

### 2026-05-20 — Shapley decomposition: signal vs execution vs stacking [POST-FREEZE PRIORITY #1]
- **Why this is the single most important post-freeze analysis** (per
  Claude Chat 2026-05-20 review): determines what FRACTION of paper
  edge will survive live trading. Different components survive at
  different rates: signal ~85%, stacking ~75% (if signal-driven) or
  ~50% (if execution-driven), execution ~45%.
- **Methodology** (Shapley-style counterfactual replay):
  - **Run A**: single-fire only (no stacking) + pure SPX-target exits
    (no BE ladder, no watcher) + realistic fills → SIGNAL component
  - **Run B**: single-fire + BE ladder + watcher + realistic fills
    → EXECUTION = Run B − Run A
  - **Run C**: stacking enabled + pure SPX exits + realistic fills
    → STACKING = Run C − Run A
  - **Run D**: full system + realistic fills → REALITY
  - **Interaction** = Run D − (Run A + (B−A) + (C−A))
- **Projected live edge formula**:
  Live ≈ (Signal × 0.85) + (Stacking_signal × 0.75 + Stacking_exec × 0.50)
         + (Execution × 0.45) + (Interaction × 0.60)
- **When**: run ONCE post-freeze on full 10-session dataset. Not during
  freeze (analytical work, but consumes meaningful compute and time).
- **Decision rule at completion**:
  - If signal_pct > 40% of paper edge → strategy is signal-durable,
    proceed to cautious live
  - If signal_pct < 20% and execution_pct > 50% → edge is paper-only,
    do NOT go live, iterate (timeframe → instrument → signal-philosophy)
  - If middle (signal_pct 20-40%) → continue paper for 10 more sessions
- **Category**: Pure analytics. Zero engine impact.
- **Resets freeze?**: **NO**.

### 2026-05-20 — Capital sizing framework [DOCUMENTED, deploy when going live]
- **Framework** (per Claude Chat 2026-05-20 review, for $50-100k retail
  account trading SPX 0DTE under genuine edge uncertainty):
  - **Per-trade risk**: 0.5-1% NLV (at $75k = $375-$750/trade)
  - **Daily loss cap**: 2-3% NLV (at $75k = $1500-$2250) — HARD stop
  - **Weekly loss cap**: 5-7% NLV (at $75k = $3750-$5250) — stop
    trading rest of week if hit
  - **Max concurrent open exposure**: 3-5% NLV across all open
    positions (sum of |position $ at risk|)
  - **First-30-session ramp**: 25% → 50% → 75% → 100% in 4 segments
  - **Never exceed quarter-Kelly** even when data supports larger size
- **Current default of 5 contracts × ~$4 entry = $2,000 risk = 2.7%
  NLV per trade** — already exceeds the 1% cap. Live transition
  should start at 2 contracts max for first 10 sessions.
- **Implementation**: codify as new auto-trader gates (per-trade $
  cap, daily $ cap, weekly $ cap, concurrent exposure cap, session
  ramp factor). Defensive — does not affect signal generation.
- **Category**: New auto-trader gates. Defensive caps, not edge tuning.
- **Resets freeze?**: YES, but bundles with any live-transition
  preparation work. Ship together with B+C+D / variant 2b before
  going live.

### 2026-05-20 — Iteration playbook if FEF fails [DOCUMENTED STRATEGY]
- **Per Claude Chat 2026-05-20 review**: order of strategic iteration if
  current FEF signals don't survive live transition. Highest-leverage to
  lowest:
  1. **Timeframe** (most productive): same flow signals on 7-45 DTE
     instead of 0DTE. Less HFT competition, less theta destruction,
     more time for thesis to develop. Many 0DTE failures find edge here.
  2. **Instrument**: same strategy on /ES futures, XSP (mini-SPX, less
     competition), or specific equities where you have informational
     advantage.
  3. **Signal philosophy**: flow → price action / volatility / fundamental.
     Bigger pivot but doable on the same platform.
  4. **Execution** (least productive at retail size): pros optimize
     execution; retail finds edge first.
- **Why platform durability matters**: ~70-80% of FEF Terminal
  infrastructure transfers across these pivots (data pipeline,
  auto-trader, watcher, counterfactual framework, friction modeling,
  microstructure capture). Only the FEF engine + specific feature
  names are strategy-coupled.
- **Realistic timeline expectation**: most successful retail quants
  describe 2-5 years of failed strategies before finding edge. Common
  thread isn't strategy genius — it's platform that enables cheap
  iteration. Don't expect THIS strategy to be "the one."

### 2026-05-20 — Session-10 decision framework [DOCUMENTED RUBRIC]
- **Per Claude Chat 2026-05-20 review**, multi-criteria framework rather
  than single statistical test:
- **Tier 1 — Hard rejection** (any one met → abandon strategy):
  - Realistic-fill P/L < 0 across 10+ sessions
  - Max drawdown > 50% of paper account
  - Edge entirely concentrated in 1-2 sessions (single-session dependence)
  - Less than 2 distinct market regimes in the sample
- **Tier 2 — Continue paper** (3+ met → continue paper for 10 more sessions):
  - Realistic-fill P/L positive but wide confidence intervals
  - Sharpe estimate 0.3-1.0
  - Mixed regime performance
  - Edge identified but mechanism unclear (decomposition not yet conclusive)
- **Tier 3 — Cautious live at 25% size** (ALL met):
  - Realistic-fill P/L > 0 with p < 0.10
  - Sharpe estimate > 1.0 after realistic-fill modeling
  - Max drawdown < 30% of paper account
  - 3+ distinct regimes represented, profitable in at least 2
  - Confluence subset / core thesis consistently profitable
  - Decomposition shows signal-component is the meaningful driver
- **Bayesian update layer**:
  - Prior: 25-40% probability of real edge (Claude Chat's estimate)
  - Each session: realistic-fill outcome is Bernoulli trial
  - Compute likelihood ratio, update prior, observe posterior
  - At session 10: deploy if posterior > 60%, continue paper if 30-60%,
    abandon if < 30%
  - More honest than frequentist hypothesis testing — incorporates
    uncertainty about uncertainty.

### 2026-05-20 — Realistic-fill estimator [FREEZE-SAFE — PRIORITY #0 for sessions 4-10]
- **Why this is the most important analysis to do during the freeze**
  (per Claude Chat 2026-05-20 review): execution-based edges degrade
  50-80% in paper-to-live transition vs 10-20% for signal-based edges.
  Bot's edge is currently execution + stacking, not raw signal — so the
  live haircut may be MUCH bigger than the current friction model
  estimates. This estimator measures the real haircut before risking
  capital.
- **What to build**:
  1. **Exit-side bid/ask capture** (new — additive, not engine change):
     each time the watcher fires a T1/T2/T3/STOP/BE-ladder exit, capture
     the current L1 book (bid/ask/bidSize/askSize/spread). Persist in a
     new `LiveOrderExit` table or on the existing exit-audit records.
  2. **Realistic fill estimator**: replay every paper trade under the
     assumption that fills happen at the WORSE side of the spread:
     - BUYs fill at ask + $0.05 (instead of paper's mid/limit)
     - SELLs fill at bid − $0.05 (instead of paper's mid)
     - Apply across BE ladder triggers, T1/T2/T3 partial fires, market
       stops, and manual closes
  3. **Re-compute session P/L** under realistic-fill assumption:
     - If paper +$6,820 → realistic +$3k = edge survives the haircut
     - If paper +$6,820 → realistic ~$0 or negative = edge is paper-only
  4. **Persist as a new column** on `DailyReview.realistic_fill_dollars`
     and surface alongside `actual_total_R` in the Model Evolution panel.
- **Implementation note**: forward-only — start capturing exit-side
  bid/ask on tomorrow's session (4 of 10). Retroactive runs use whatever
  bid/ask data was already captured at fire time as a fallback.
- **Decision criterion**: by session 10, if realistic-fill P/L is
  consistently positive (>50% of sessions), the live transition is
  viable. If realistic-fill P/L is consistently near-zero or negative
  while paper is positive, edge is paper-only — do NOT go live, iterate.
- **Category**: Pure analytics + new data capture. ZERO engine impact.
- **Resets freeze?**: **NO**. Read-only modeling on top of additional
  observational data capture.

### 2026-05-20 — Per-direction-per-strike hard exposure cap [HIGH PRIORITY before live]
- **Why**: per Claude Chat 2026-05-20 review, 35-contract stack on a
  single strike (today's 7375P) is tail-risk that could be −$15-30k on
  a Fed surprise or flash crash. Current "stacking allowed" with only
  60s cooldown has no hard exposure cap.
- **Proposed rule**:
  - Per (direction × strike × expiry): max 15-20 contracts
  - Per session aggregate: max ~15-20% of account NLV in open exposure
    (sum of |position $ at risk|). At $50k NLV → ~$8-10k aggregate cap.
- **Category**: New auto-trader gate — hard exposure constraint.
  Defensive, not edge-related.
- **Resets freeze?**: YES, but bundled with B+C+D + microstructure gate
  for one logical post-freeze ship.

### 2026-05-20 — Microstructure quality gate (book/stuffing/C-to-T filter)
- **Symptom / opportunity**: Across 33 actionable signals from 5/19-5/20
  that have BOTH HFT/microstructure snapshots AND realized outcomes, a
  clear pattern emerged: the bot's wins are concentrated when the
  microstructure book is CALM. Skewed/stuffed/extreme-activity books
  correlate with losses.
- **Evidence (n=33 across 2 sessions, small but directionally strong)**:
  - `|book_imbalance| ≤ 0.03` (balanced): **n=15, WR=87%, +$10,035**
  - `|book_imbalance| > 0.03` (any skew): n=18, WR=22%, −$789
  - `stuffing_severity = NORMAL`: n=31, WR=58%, +$9,729
  - `stuffing_severity = ELEVATED`: n=2, WR=0%, −$482
  - `5 ≤ cancel_to_trade ≤ 15` (healthy book): n=28, WR=61%, +$10,697
  - `cancel_to_trade > 15 or < 5` (stale/manipulated): n=5, WR=20%, −$1,451
  - `vol_regime = CONTRACTION`: n=4, WR=75%, +$2,005 (smaller sample
    but consistent with "cheap premium = easier wins on 0DTE")
  - `microstructure.direction = BEARISH_PRESSURE`: n=3, WR=0%, −$964
    (tiny sample; flag for monitoring)
- **Interpretation**: When the bot fires into a calm balanced book,
  fills are clean and exits work. When it fires into a skewed or
  manipulated book, HFTs front-run / fade your size and your stops
  trigger at bad prices. Three of these features (book_imbalance,
  stuffing, cancel-to-trade) are correlated — they all describe
  "calm liquid book conditions" — so the underlying edge might be
  ONE factor (book health) showing through three correlated proxies.
- **Proposed fix — composite microstructure gate**:
  Add a Gate 9.5 to the auto-trader that blocks fires when ANY of:
  - `|hft.book_imbalance| > 0.05` (skewed book)
  - `hft.stuffing_severity == "ELEVATED"`
  - `hft.cancel_to_trade_ratio > 20 OR < 5` (manipulated or stale)
  Optionally also block when `microstructure.direction` opposes verdict
  direction (e.g., LONG_PUTS verdict with `direction=BULLISH_PRESSURE`).
- **Open questions to resolve with more data**:
  - Does the 87% WR hold at n=100+? Or was it a 2-session fluke?
  - Are the three "book health" features really one underlying factor,
    or do they have independent signal? PCA / correlation matrix worth
    running once data accumulates.
  - Does vol_regime add information beyond the book features?
- **Decision criterion for shipping**: After ≥10 sessions worth of
  HFT-tagged data (~150-200 signals), if `|book_imbalance| ≤ 0.05` WR
  is at least 15pp higher than its skewed counterpart with p < 0.05,
  ship the gate. If patterns wash out, drop it.
- **Category**: New auto-trader gate (signal filter). Reduces volume,
  intended to raise WR.
- **Resets freeze?**: YES — it changes which signals fire. Same logical
  ship as variant 2b + B+C+D (one combined freeze-reset for "exit
  strategy upgrade + signal quality filter").
- **Bundle**: ship together with variant 2b + B+C+D after freeze.

### 2026-05-20 — Late-session strike selection picks far-OTM lottery strikes
- **Symptom**: After ~12:30pm CT (and especially during power hour),
  the auto-trade picks strikes that are far OTM relative to the
  realistic distance SPX can travel in the remaining time. With theta
  accelerating in the last 90 minutes, those lottery strikes rarely
  pay — risk/reward is bad even when directional signal is correct.
  User observed this pattern across multiple sessions on the last few
  trades of the day.
- **Root cause**: Strike selection logic (`engine/strikes.py`
  `build_plan`) uses a fixed offset-from-spot regardless of time of
  day. SPX 7400 with 3hrs to close → buy 7450C is reasonable.
  Same SPX 7400 with 30min to close → 7450C is essentially a lottery
  ticket; theta destroys the premium even on a 10-pt favorable move.
- **Two fix variants**:
  - **A (time-decay-aware strike offset)**: Strike offset from spot
    scales with minutes-to-close. Earlier in session = wider OTM.
    Later in session = tighter (1-3pt OTM or even slightly ITM) so
    the trade has real chance to monetize.
  - **B (hard time gate)**: Disable auto-trade after a configurable
    hour (default 13:30 CT / 14:00 ET). Simpler. Bot just stops
    trading; existing positions still managed by the watcher.
- **Recommendation**: Ship B first (simple, zero risk, predictable).
  Then build A as the higher-effort upgrade when ready to trade the
  full session.
- **Category**: Engine — affects which strikes are picked and when
  trades fire. RESETS FREEZE.
- **Defer to**: post-freeze ship alongside variant 2b + B+C+D.

### 2026-05-20 — Multi-symbol scanner: per-symbol expiry resolution [FREEZE-SAFE — ship tonight]
- **Symptom**: User enabled scanner for QQQ/IWM/GLD/SMH/TLT. Per-symbol
  verdicts ARE being produced (visible in scanner panel) but ZERO auto-
  trades fire for these symbols even on strong directional signals
  (e.g., today's SPX rally — QQQ would have moved similarly and produced
  matching call signals).
- **Root cause**: `multi_symbol_scanner.py:115` hard-codes
  `expiry = _today_et().replace("-", "")` when fetching the option
  quote. Works for SPX (daily 0DTE). Fails silently for:
  - **GLD / SMH / TLT**: no 0DTE listings — only weeklies (Fri) or
    monthlies (third Fri). Today's contract simply doesn't exist.
  - **SPY / QQQ / IWM on Tu/Th**: 0DTE only listed M/W/F. Half the days
    no contract.
  When the option-quote fetch returns nothing, `verdict_dict
  ["option_pricing"]` is absent and **Gate 8** (`NO_OPTION_PRICING`)
  silently blocks the auto-trade.
- **Fix**: Resolve nearest available expiry per symbol — use IBKR's
  reqContractDetails to find the next listed expiry ≥ today, then cache
  per (symbol, expiry_date) to avoid hitting IBKR on every iter.
  Pseudo-impl:
  ```python
  async def resolve_nearest_expiry(symbol: str) -> str:
      if symbol in state.expiry_cache:  # cached for today's session
          return state.expiry_cache[symbol]
      details = await ib.reqContractDetailsAsync(Option(symbol, '', 0, '', 'SMART'))
      expiries = sorted({d.contract.lastTradeDateOrContractMonth for d in details})
      today_yyyymmdd = today_et().replace("-", "")
      nearest = next((e for e in expiries if e >= today_yyyymmdd), today_yyyymmdd)
      state.expiry_cache[symbol] = nearest
      return nearest
  ```
- **Implementation cost**: ~30 min. Cache per session_date so we hit
  IBKR once per symbol per day, not per iter. Add `expiry_cache` to
  AppState. Modify scanner to call resolver instead of hard-coding today.
- **Category**: Bug fix in scanner. Does NOT change verdict generation
  or engine thresholds.
- **Resets freeze?**: **NO**. The engine's verdict for these symbols is
  already being computed identically. We're only fixing the broker-side
  contract resolution — that's plumbing, not tuning.
- **Ship tonight** with the rest of the freeze-safe batch.

### 2026-05-20 — Take-profits use SPX, not option price [HIGH PRIORITY — bundle with variant 2b]
- **Symptom**: User up +$6.1k unrealized at session peak, gave back to
  +$3.4k as positions retraced without any T1/T2/T3 firing. Specific
  example today: SPX 7345P went from $5.87 entry → $8.50 peak (+45%
  option-price gain) → $7.50 current — never fired T1 because SPX
  itself never crossed the T1 SPX level (7365.18, needed -5pts more
  drop). The option made a complete round trip on a 15pt SPX move that
  didn't reach the target.
- **Root cause**: T1/T2/T3 are SPX-distance triggers, same blind spot
  as the BE ladder. Theta-fast, gamma-driven, or IV-spike moves can
  produce big option-price excursions without crossing SPX targets,
  and there's no mechanism to capture them or trail behind the peak.
- **Decision (user, 2026-05-20)**: Combination of three independent
  trigger pathways, each fires partial qty if hit:
  - **B. Dual trigger (SPX OR option-price)**: T1/T2/T3 fire on
    whichever crosses first. SPX targets remain as the safety net.
    Option targets default: T1 = entry × 1.30 (+30%), T2 = entry × 1.75
    (+75%), T3 = entry × 2.50 (+150%). Tunable per preset.
  - **C. Trailing peak lock**: Track per-parent option MFE. When option
    mid pulls back N% from peak (default 15%), fire partial exit on
    remaining qty. Catches the "captured the move, now giving it back"
    pattern that variant B alone misses if peak doesn't cross the
    option-T1 threshold.
  - **D. Time-decay-aware T1 scaling**: T1 option-price threshold
    shrinks as session progresses to reflect accelerating theta. Suggested
    schedule (tunable):
    - Pre-1pm CT: T1 = entry × 1.30 (+30%)
    - 1pm–2pm CT: T1 = entry × 1.20 (+20%)
    - Post-2pm CT: T1 = entry × 1.15 (+15%)
    Only T1 scales; T2/T3 stay at fixed multiples since they're
    home-run captures less sensitive to theta acceleration.
- **Implementation outline** — reuses infra from variant 2b so they
  ship together on one freeze reset:
  1. Variant 2b's `state.live_option_mid_by_parent` cache is the
     source of truth for current option mid. No new subscription
     plumbing needed.
  2. New LiveOrder columns:
     - `t1_option_price`, `t2_option_price`, `t3_option_price` (computed
       at place_bracket time from entry option price × multipliers)
     - `option_peak_mid` (initialized to entry option price, ratcheted
       up on every tick that exceeds previous peak)
     - `peak_drawdown_trigger_pct` (default 0.15 = 15%; per-preset
       tunable)
  3. Migration entries for the five new columns.
  4. `spx_target_watcher` gains parallel option-price checks:
     - On each tick: if option mid >= t1_option_price AND not already
       fired → market sell t1_qty
     - If option mid > option_peak_mid → update peak
     - If option mid <= option_peak_mid × (1 - peak_drawdown_trigger_pct)
       AND remaining qty > 0 AND any tier already fired → market sell
       remaining qty (peak-trail exit)
  5. Time-decay scaling applied at place_bracket time based on current
     CT hour. Stamp the chosen multiplier into LiveOrder so audit can
     replay the decision.
  6. `BracketSpec` extended with `option_t1_multiplier`,
     `option_t2_multiplier`, `option_t3_multiplier`,
     `peak_drawdown_trigger_pct` so presets can override defaults.
  7. `StrategyPreset` rows get the same four columns + migration so
     per-regime tuning (tight pin vs trend day) works.
  8. New env: `FEF_TP_MODE` = `'spx'` (current), `'option'` (option-only),
     `'dual'` (B), `'dual_with_trail'` (B + C), `'dual_with_trail_decay'`
     (B + C + D — the user's chosen target). Default `spx` until
     validated.
  9. Audit endpoint extension: per-parent, surface which trigger fired
     each tier (SPX vs option vs peak-trail) so we can A/B compare
     trigger source effectiveness across sessions.
  10. UI: LiveExecution panel shows BOTH SPX target distance AND option
      mid % from each target, plus peak option mid + drawdown-from-peak.
      So user can see at a glance "T1 is 5pts away OR option needs to
      gain +8% more, peak was $8.50 down 12% from peak."
- **Today's data (2026-05-20, intraday snapshot)** justifying the change:
  - 9 open puts at peak unrealized +$6.1k → gave back to +$3.4k (giveback
    of $2.7k, ~44% of peak)
  - 7345P specifically: $5.87 → $8.50 peak (+45%) → $7.50 ($1.00 above
    entry) — NO take-profit fired during the entire round trip
  - Hypothetical: trigger B (option T1 +30%) would have fired at ~$7.63
    on the way up, locking 1/3 of the position at +30%
  - Hypothetical: trigger C (15% peak-trail) would have fired at $7.23
    on the way down after $8.50 peak, locking remaining qty at +23%
  - **Counterfactual run** at midday: actual realized +$8,045 vs B+C+D
    counterfactual +$20,611. Incremental upside +$12,566 (+156%).
  - **Second giveback later in session**: peak daily gain $23k → $14.5k
    after a large SPX candle reversed. Second BE-ladder giveback of $8.5k
    in one session. Compounds the case — BE-ladder kills happening on
    EVERY meaningful intraday giveback.
  - **Per-trade BE-ladder kill rate today**: 10 of 12 stopped trades
    (83%) reached at least +0.5R MFE before stopping out. Up from
    79% (11/14) on 5/19. Pattern is worsening, not improving.
- **Category**: Engine / exit-strategy change. **Bundles with variant
  2b** (BE ladder option-price rewrite) since they share the same
  market-data subscription infrastructure and same freeze reset.
- **Resets freeze?**: YES (same engine-change reset as 2b — together
  they're one logical ship: "exit strategy rewrite to option-price space").
- **Decision criterion**: Ship together with variant 2b once freeze
  counter ≥10. If BE-ladder-stops option-negative rate holds ≥50% in
  the freeze window (currently 79% on 5/19, 4/4 today), the case for
  this companion change is already proven by the same data.

### 2026-05-19 — BE ladder uses SPX, not option price [HIGH PRIORITY]
- **Symptom**: BE ladder correctly moves stop to entry SPX, but the
  option premium at "entry SPX" is LOWER than entry option price after a
  round trip. Theta decay (5-15% on 0DTE per ~10 min) + bid-ask slippage
  (~$0.20/contract round trip) means "SPX BE" ≠ "option BE". Trades can
  exit at "0R locked" SPX but actually negative on P/L.
- **EVIDENCE FROM 2026-05-19 (full execution audit, 32 parents)**:
  - 13 full-run trades (T1+T2+T3) = 41% true win rate
  - 19 stopped before any tier hit = 59%
  - Of the 19 stopped, **14 had BE ladder activated** (be_step_reached_R > 0)
  - **11/14 BE-ladder-activated stops exited OPTION-NEGATIVE (79%)**
  - Clean unstacked cases: 4/4 = 100% option-negative
    (PIDs 38 SPX 7305P, 41 SPX 7385C, 44 SPX 7315P, 61 SPX 7425C)
  - Worst: PID 40 SPX 7310P at -$1.70/contract; #62 SPX 7355P at -$0.55
  - The 3 "option-flat+" cases (43/48/67) are stacking artifacts where
    the parent entered late and the contract-avg sell was dominated by
    earlier parents' sells at higher prices. Per-parent attribution would
    likely show them as option-negative too.
- **Pre-audit observation (eyeball)**: "5 of 7 losers triggered BE ladder
  at +0.5R MFE, then retraced to entry SPX." Now confirmed quantitatively
  across all 32 trades, not just visual sample.
- **Three fix variants**:
  - **2a (calibration buffer, easy)**: Change `BE_LADDER` to
    `[(0.5, 0.2), (1.0, 0.6), (1.5, 1.1)]` — at MFE +0.5R, stop moves to
    entry + 0.2R giving theta cushion. Buffer magnitude is still a guess
    without multi-session data.
  - **2b (option-price ladder, correct)**: Capture actual fill price at
    order placement. Watcher reads streaming option mid, ratchets stop in
    OPTION-PRICE space. Mathematically guarantees "locked +X%" means real
    P/L. Independent of theta.
  - **2c (time-of-day-aware buffer)**: 2a's buffer scales with minutes-
    to-close. Earlier in day = smaller buffer. Late afternoon = larger
    buffer. Empirical calibration after multi-session data.
- **Implementation outline for variant 2b (option-price ladder)** —
  reference for when freeze counter allows shipping:
  1. New LiveOrder column: `entry_option_price` (capture from BUY fill
     avg_fill_price at place_bracket time). Migration entry.
  2. New state: `state.live_option_mid_by_parent: dict[int, float]`
     populated from streaming quotes.
  3. At `place_bracket()` after BUY confirms: call
     `ib.reqMktData(option_contract)` to subscribe. Wire a callback that
     updates `state.live_option_mid_by_parent[parent_id]` on every tick.
  4. New `BE_LADDER_OPTION = [(0.5, 1.0), (1.0, 1.25), (1.5, 1.5)]`
     meaning: at +50% option gain (mid >= entry × 1.5), lock at entry × 1.0
     (true BE in option price). Subsequent steps lock at 1.25× and 1.5×.
  5. New LiveOrder column: `dynamic_stop_option_price` (defaults to None).
     Replaces dynamic_stop_level for option-price exit strategy.
  6. spx_target_watcher gets a parallel option-price stop check: if cached
     option mid <= dynamic_stop_option_price → market sell remaining qty.
  7. At parent close/stop: call `ib.cancelMktData(contract)` to free the
     subscription slot. IBKR cap is 100 simultaneous mktdata; with cool-
     down + auto-trade caps, normal usage is <10 simultaneous open.
  8. Latency: streaming push gives ~50-250ms tick-to-cache; watcher reads
     cache (no API call) every SPX_WATCHER_INTERVAL_SEC=2s. Total tick-
     to-action ≤2.25s — same or faster than current SPX-based watcher.
  9. New env: `FEF_BE_LADDER_MODE` = 'spx_points' (current) or 'option_price'
     (new). Default stays spx_points until validated across sessions.
  10. Audit endpoint extension: surface "option_pnl_at_stop_time" for each
      stopped trade so we can A/B compare option_price vs spx_points modes.
- **Decision criterion**: Stay on spx_points through this week (5/19 - 5/22).
  Re-pull audit each day, track BE-ladder-activated-stops option-negative %.
  If pattern holds >60% across all sessions, ship variant 2b after freeze
  elapses. If it drops below 40% on calmer days (low theta acceleration),
  the SPX-BE may be acceptable with variant 2a buffer.
- **Category**: Engine / exit-strategy change.
- **Resets freeze?**: YES. The reviewer specifically warned about this
  kind of mid-flight tuning. With today's evidence (5/7 losers fit the
  pattern), this is the highest-priority deferred change, but waiting
  for the freeze to elapse + multi-session data is still the right call.
- **Decision criterion for shipping**: After ≥10 sessions, if the
  BE-ladder-activated-stops option-negative rate holds AT or above 50%,
  ship variant 2b (option-price). 2026-05-19 baseline = 79% (11/14).
  If the pattern fades with more data, variant 2a buffer may suffice.

### 2026-05-19 — IBKR reqPnL returning 0 for DUM098107
- **Symptom**: Today's P/L pill shows 0 from IBKR despite 16 closed
  trades. The account ID `DUM098107` is unusual (typical paper is `DU`
  + 6 digits; "M" prefix is non-standard).
- **Current workaround**: TopBar falls back to `/diag/session_pnl_estimate`
  (computed from our LiveOrder records). Label changes to "Today P/L ~"
  with tilde to flag the approximation.
- **Action**: Check TWS directly to see if IBKR has the P/L on their
  side. If yes, our subscription is reading the wrong field. If no, it's
  an IBKR quirk for this account format and the estimate fallback is the
  best we can do.
- **Diagnostic**: `curl http://127.0.0.1:8765/diag/pnl`
- **Resets freeze?**: No (diagnostic + read path).

### 2026-05-19 — IBKR-resident STP backstop for offline crash scenarios
- **Symptom**: Position reconciliation on startup handles "bot was down,
  restart, resume management" cleanly. But if SPX crosses the stop level
  WHILE the bot is offline (sidecar crash, IBKR disconnect mid-loop), the
  position is exposed — no exchange-resident stop to enforce.
- **Fix**: At `place_bracket` time, submit a real STP order to IBKR at
  `spx_target_stop`. Stays at IBKR's matching engine. Fires even if our
  bot is dead.
- **Complication**: SPX-points stops aren't directly compatible with
  IBKR option-STP orders (option STPs trigger on option price, not on
  underlying SPX). Need ib_async **conditional orders** — option market
  sell triggered by SPX crossing X. More complex.
- **Category**: New safety feature (not tuning).
- **Resets freeze?**: Debatable. It's belt-and-suspenders, not a model
  change. Likely safe but tag the EngineChange row anyway.

---

### 2026-05-19 — Preset versioning + champion/challenger promotion [FREEZE-IMPACTING]
- **Current state**: `StrategyPreset` rows are single-version configs.
  Overwriting an "improved" preset destroys the comparison baseline.
  `TuningRecommendation` queue surfaces ideas but doesn't apply them.
  No mechanism ensures the BETTER version of a preset is the one used live.
- **Architecture to ship (post-freeze)**:
  1. Add columns: `StrategyPreset.version: int`, `parent_id: int | None`,
     `is_champion: bool`, `champion_since: datetime`.
  2. Tournament-driven promotion gate: when a v2 candidate beats v1 champion
     by statistical margin (t-test p<0.05 OR 10+ sessions consistent
     dominance), UI shows "Promote v2" button.
  3. Live engine loads `WHERE name=X AND is_champion=True` — always uses
     champion, never outdated.
  4. EngineChange row logged on every promotion + the prior champion stays
     in DB for backtest comparison forever.
  5. Optional auto-promotion: nightly job promotes when challenger has
     beaten champion across N consecutive tournaments by margin M.
- **Resets freeze?**: YES. Auto-loading presets to live engine + auto-
  applying recommendations are both in the reviewer's "do not do alone"
  list. Manual promotion (with explicit button click) is the right
  middle ground — gives you the lineage without auto-mutating behavior.
- **Decision criterion for shipping**: After 10+ sessions of TournamentRun
  history with regime tagging, evaluate whether the rankings stabilize
  enough that promotion would be data-driven (not noise-driven). Likely
  ship version 1 (manual-button promotion) first, version 2 (nightly
  auto-promote) only after another 10+ sessions of validated rankings.

### 2026-05-19 — Auto-apply preset based on regime classification [FREEZE-IMPACTING]
- **Current state**: RegimeDetector runs and identifies PIN_DAY / TREND_DAY /
  CHOP / etc. with a recommended_preset_hint. Stored in `state.last_regime`.
  Used to DEMOTE signals when regime disagrees with direction.
- **Missing wiring**: No code path automatically loads the recommended
  preset into the engine. User has to manually hit "Apply" on the matching
  StrategyPreset each morning (or accept the defaults). The "sizing" field
  (STAND_DOWN/REDUCED/STANDARD/FULL) is also not wired into qty_override.
- **Fix outline**:
  1. New background loop: every M iters, check if last_regime.recommended_preset_hint
     matches a StrategyPreset row by name. If yes AND it's different from active
     preset, apply via existing /presets/{id}/apply path.
  2. Map regime.sizing to qty_override scaling (e.g. STAND_DOWN → qty=0, blocks
     trades; REDUCED → 0.5×; FULL → 1.5×).
  3. Audit-log every auto-switch with the regime reasoning that triggered it.
  4. Add a "Manual override" toggle so user can pin a preset and prevent
     auto-switching (for backtest comparison, days they want consistent config).
- **Why freeze-impacts**: changes engine thresholds in real-time based on
  classifier output. Changes firing behavior. Need to validate regime
  classifier accuracy across many sessions first.
- **Decision criterion for shipping**: After 10+ sessions, check that
  `by_regime` in DailyReview shows clear edge differentiation across regimes
  (e.g. TREND_DAY has +0.3R expectancy, CHOP has -0.2R). If regimes don't
  separate, the classifier isn't reliable enough to drive auto-switching.

### 2026-05-19 — Presets currently don't differentiate enough [FREEZE-IMPACTING]
- **Symptom**: Preset Tournament backtest across 6 presets on 5/18-5/19 data
  produced 4 identical result rows (Default, Tight Pin, Trend Aggressive,
  Power Hour). Only Volatile/Fed Day and Chop/Sit Out produced distinct
  results, and Volatile's "edge" came from filtering 4 trades — 3 losses
  + 1 small win = +2.06R from sample-size luck, not a real edge.
- **Root cause**: The 4 threshold knobs presets currently tune
  (wall_proximity_pts, wall_collapse_pct, wall_collapse_floor_m,
  absorption_fe_ratio_min) only affect EDGE-CASE behavior:
  - `wall_proximity_skip` — only fires when SPX is near a dominant wall
  - `absorption` — only fires in specific FE-ratio + drift divergence setups
  - `wall_collapse_trigger` — extra confidence flag, doesn't change direction
  - `absorption_fe_min` — gates absorption only
  The CORE direction decision (aligned = sign(ΔPrice) == sign(ΔDrift)) is
  threshold-free. Most iters fire the same direction regardless of preset.
- **What would actually differentiate presets**:
  - Minimum |ΔPrice| / |ΔDrift| to fire (filter weak moves)
  - Minimum FE ratio for ALL fires (not just absorption)
  - Max nearest-wall-distance (only fire if wall far away — opposite gate)
  - Tunable confidence-tier thresholds (HIGH/MEDIUM/LOW boundaries)
  - Regime-conditional gates (different threshold values per detected regime)
- **Resets freeze?**: YES (new engine gates). Major architectural change.
- **Decision criterion for shipping**: After 10+ sessions of data, if
  Preset Tournament continues to show 4 of 6 identical, add at least
  one differentiating gate (likely minimum |ΔPrice| since it's least
  controversial). Otherwise the multi-strategy framework is performative.

## OPEN — FROM EARLIER (pre-gym-session "do not do alone" list)

### Auto-pattern-proposal automation
Engine watches DB for winning streaks and auto-proposes new Pattern rows
without user input. Deferred for explicit review of the design.
**Resets freeze?**: Yes.

### New engine gates beyond what's shipped
Specifically: microstructure direction, HFT book imbalance, VIX state,
secondary_scores sum threshold. Each is "level 5 thinking" per the
reviewer — only add after the existing model has known edge.
**Resets freeze?**: Yes.

### Auto-modifying thresholds / auto-applying recommendations
The TuningRecommendation panel currently surfaces "Apply" buttons but
they're informational. Wiring them to actually mutate engine state
without explicit code review is risky.
**Resets freeze?**: Yes.

### Live deployment (real money)
User said 2-3 months on paper before any live trading. Holding that line.
**Resets freeze?**: N/A (operational, not code).

---

## TIER 4 INFRASTRUCTURE — REFERENCE ONLY, NOT BUILDING

These are HFT-grade infrastructure items that real prop shops run. None
apply to FEF Terminal's strategy (2-minute decision cadence, not microsecond)
but documenting so we don't forget the landscape exists.

- **FPGA-based feed handlers** — sub-microsecond market data ingestion.
  Citadel/Virtu/Jump-grade infra. Not relevant for mid-frequency systematic.
- **Kernel-bypass networking** (DPDK, Solarflare OpenOnload) — saves ~10µs
  per packet vs standard TCP stack. Same — not our timescale.
- **Co-located servers** in CME Aurora / NY4 / LD4 / TY3 — physical proximity
  to exchange matching engines. Pricing: $5-20k/month per cabinet.
- **Multi-venue smart order routing** — splitting orders across CBOE / NYSE /
  NASDAQ / ARCA / BATS / IEX for best execution. IBKR's SMART routing already
  does a reasonable version for us; native multi-venue would require direct
  exchange connectivity (FIX gateways, market data subscriptions per venue).
- **ASIC / custom hardware order entry** — sub-microsecond order generation.
  Way beyond our needs.
- **Tick-by-tick storage** (every quote update, every trade) — KX/InfluxDB/
  TimescaleDB scale, TB/day per market. We persist 1Hz HFT samples which is
  several orders of magnitude lighter; sufficient for our timescale.
- **PTP / GPS clock sync** — sub-microsecond synchronization across data
  centers. Required for HFT cross-venue arbitrage; meaningless at 2-min cadence.
- **C++/Rust/Java hot path** — Python is fine at our cadence (60s scan
  intervals leave plenty of compute budget). HFT shops rewrite the hot path
  to C++ for sub-millisecond decision loops.
- **Market making infrastructure** — two-sided continuous quoting, inventory
  management, adverse selection models. Different business entirely; we're
  directional taking, not market making.

If we ever pivot to market making, multi-venue arb, or sub-second strategies,
revisit this list. For SPX 0DTE directional, ignore.

---

## SHIPPED ITEMS THIS SESSION

### 2026-05-19 — IBKR executions now persisted to local DB
- **Was**: `reqExecutionsAsync` only returns ~7 days from IBKR. 5/18 fills
  were already wiped from cache when we tried to pull them this evening
  for the day-over-day audit.
- **Now**: New `ibkr_execution` table (unique exec_id) + write-through
  capture on every `/diag/ibkr_executions` call. Existing rows skipped on
  duplicate exec_id (idempotent). New `/diag/ibkr_executions_persisted`
  endpoint pulls historical data from local DB. Survives 7-day cap.
- **Resets freeze?**: No (data persistence, no engine path).

### 2026-05-19 — /diag/signal_audit_buckets endpoint
- **Was**: Per-side win/loss bucket analysis required ad-hoc Python script
  each session (the one we ran this evening).
- **Now**: Single endpoint takes `session_date`, joins every parent with
  its firing SignalIter, buckets by (right, status), returns medians/means
  for 19 numeric fields and counters for 11 categorical. Includes
  HFT/micro snapshot coverage stats. Returns full per-trade rows for
  downstream filtering.
- **Resets freeze?**: No (read-only diagnostic).

### 2026-05-19 — /diag/hour_of_day_stats endpoint
- **Was**: `DailyReview.by_hour_ct` JSON existed but wasn't aggregated
  across sessions.
- **Now**: Endpoint rolls up by_hour_ct across recent DailyReview rows,
  returns per-hour totals (signals, wins, stops, timeouts, sample-weighted
  expectancy) and per-session breakdown for trend analysis. Lets us verify
  the "11am-noon CT prints money" hypothesis across multi-session data.
- **Resets freeze?**: No (read-only aggregation).

### 2026-05-19 — GLD, SMH, TLT added to tradable universe
- **What**: Extended `available_symbols`, `scan_enabled_symbols`,
  `OPTION_SYMBOLS`, and FE `SCANNER_SYMBOLS` to include GLD (gold),
  SMH (semiconductors), TLT (long-dated treasuries). All three disabled
  by default — user flips toggles in ScannerPanel when ready.
- **Why**: User wants uncorrelated underlyings to improve sharpe — equity
  ETFs (SPY/QQQ/IWM) all correlate ~0.9 with SPX. GLD/TLT historically
  uncorrelated; SMH has its own semi cycle driver.
- **Verify tomorrow morning (same checklist as SPY/QQQ)**:
  - QuantData `set_page_filter(ticker="GLD")` returns flow data (may not be
    subscribed for all 3 — QD coverage varies)
  - Option contract routing: all three use `exchange="SMART"`, equity-style
    strikes (GLD 1pt, SMH 1pt, TLT 1pt)
  - Notional sizing: SMH ~$200, GLD ~$200, TLT ~$100 — contract values are
    a tenth to a twentieth of SPX. `auto_trade_qty_override` math needs
    review before trusting on real money; defaults will overshoot dollar risk
  - HFT depth proxy: loop.py:973 fallback uses the ticker itself for L2
    depth. GLD/TLT depth typically thin on equity exchanges — may want
    to map GLD→GC, TLT→ZN, SMH→SMH later (requires NYMEX/CBOT data subs)
  - Trading hours: equity ETFs honor regular RTH only by default, vs SPX
    which has its own SOQ. Calendar pause logic should already handle this
    but worth verifying with a live test
- **Resets freeze?**: No (config, no engine path change).

### 2026-05-19 — Per-symbol cap is now the only auto-trade budget gate
- **Was**: Two independent caps fought each other — `auto_trade_max_per_session`
  (global, default 5/20) AND `auto_trade_max_per_symbol` (per-symbol, default 2).
  Whichever fired first blocked the trade. With only SPX enabled in the scanner,
  per-symbol=2 became the binding constraint and global=20 was effectively dead.
  User found the dual-cap mental model confusing.
- **Now**: Gate 7a (GLOBAL_CAP) removed from auto_trader.py. Only Gate 7b
  (SYMBOL_CAP) remains. Each symbol carries its own independent budget. The
  "global total" displayed in UI is auto-derived as `max_per_symbol × enabled_symbols`
  (e.g. 4 symbols × cap=2 = total 8). No way for the two numbers to disagree.
- **API**: `/scanner/symbols` still returns `max_per_session_global` for backwards
  compat with FE, but now computes it on the fly (= implied global).
- **UI**: ScannerPanel header changed from "Global: 2/20 · per-symbol cap: 2"
  to "Per-symbol cap: 2 · total budget: 2/8 (auto = cap × enabled symbols)".
- **Resets freeze?**: No (tactical safety gate change, not signal logic).
  Reviewer's freeze targets FEF signal generation / scoring / triggers —
  the auto-trader cap layer is downstream of all of that.
- **Note**: BlockReason.GLOBAL_CAP enum value preserved (no DB writes use it
  in active code paths going forward, but historical SignalIter rows may
  reference it).

### 2026-05-19 — Kill button now also market-closes open positions
- **Was**: `/live/kill` only cancelled WORKING orders (limit/stop sitting at
  IBKR). With market-entry auto-trade, working orders are rare — so the
  button was mostly a no-op while leaving filled positions untouched.
- **Now**: `/live/kill` does BOTH: (1) `reqGlobalCancel()` + per-trade cancel,
  (2) iterates `ib.positions()` and submits a `MarketOrder` SELL for each
  long OPT, BUY for each short. Auto-trade + scanner stay ON — use 🚨 FLAT
  for the full kill-switch (cancel + close + disable auto + disable scanner).
- **UI**: Button label changed to "KILL ORDERS + CLOSE POSITIONS". Confirm
  dialog explicitly mentions market-close. Toast shows both counts.
- **Resets freeze?**: No (UI/safety, no engine path).
- **Audit logged**: `category=kill_switch` with `closed_positions` detail.

---

## PATTERN OBSERVATIONS (NOT for shipping — cross-session tracking)

Findings that look promising but need multi-session validation before
they get turned into rules. **Do NOT bake any of these into engine gates
during the freeze period.** The point is to accumulate evidence then
test the rule on out-of-sample data after the freeze elapses.

### 2026-05-19 — Winning-trade signature (PARTIALLY DISCONFIRMED on same-day test)
Trade #65 (SPX 7350P @ 13:26 CT) = textbook +$770 win. Initial n=1
hypothesis had THREE factors. After same-day cross-check across all
32 trades, only ONE survived; the others overfit on the single win.

1. **PA score Goldilocks band [-0.20, -0.10]: DISCONFIRMED for calls,
   weak for puts.** Call-side data shows no usable band — "late" bucket
   (0.20-0.30) had 57% WR vs Goldilocks (0.10-0.20) at 50%. Puts only
   had n=1 in the band.

   **HOWEVER, a related rule DID survive**: 5/5 PUT trades with
   PA < -0.20 LOST today. The rule isn't "fire in the Goldilocks band"
   but "DON'T fire puts on already-rolled-over markets" (asymmetric;
   no equivalent rule for calls in today's data).

2. **Strike pinned at major GEX wall (>2000M)**: #65 picked 7350P
   directly on the -3316M put wall. #44 went 15pts past nearest wall
   (no gravity to lean on). #62 was 5pts off (close but not exact).
   Hypothesis: market-maker hedging concentrates at major walls, so
   strikes AT the wall benefit from gamma magnet pull toward the level.

3. **Microprice deviation > 15 SPX pts from spot**: only #65 has HFT/
   microstructure snapshots (feature shipped mid-day). Microprice was
   7403 vs actual 7382 = 20pts ABOVE = order book had not yet repriced
   for the drop. Interpretation: the move was LEADING flow; when flow
   catches up, the move accelerates. Need to verify across more wins
   to confirm this isn't coincidence.

**Validation plan (post-freeze, NOT during)**:
- Track these 3 fields on every fired trade for next 10+ sessions
- After 50+ trades worth of data, run a logistic regression:
  outcome ~ PA_band + strike_at_wall + microprice_dev
- If any single factor or combo has p<0.05 AND survives out-of-sample,
  consider promoting to a gate (with freeze counter reset acknowledged)
- Bias awareness: I'm cherry-picking AFTER seeing one win. Hindsight
  bias is real. Treat as hypothesis, not conclusion.

### 2026-05-19 — Full-session field audit (32 trades, by side + outcome)
Cross-bucket comparison of every captured field. Findings worth tracking
across sessions (NOT for shipping):

**Calls (n=11W / 12L):**
- drift_cum_net_m: WIN median 9.5M, LOSS 12.0M. Higher cumulative bullish
  drift on losers — possible over-extension.
- nearest_wall_distance: WIN median 32.9pts, LOSS 24.9pts. Winners had
  more room to run above current price.
- pa_score, fe_ratio: similar across W/L; not distinguishing today.

**Puts (n=2W / 7L — small sample):**
- pa_score: WIN -0.104, LOSS median -0.283. 5/5 losses had PA < -0.20
  (the "don't fade established drops" rule from earlier).
- delta_price: WIN median -2.08, LOSS -5.65. Same theme as PA.
- fe_ratio: WIN 0.96, LOSS median 0.21. Winners had MUCH stronger
  flow ratio. Losers fired on thin flow.

**Universal observations:**
- ALL 32 trades had aligned=True, absorption=False, wall_collapse=False
- 30/32 trades fired at LOW confidence (the OLD high/medium label, now
  demoted to display-only). The model can't currently distinguish its
  own conviction levels — every actionable signal looks the same to the
  confidence classifier. Worth revisiting confidence calibration after
  freeze.

**HFT/Microstructure**: only 5/32 trades had snapshots (feature deployed
mid-session 5/19). Cannot draw HFT conclusions yet. By 5/22 we'll have
3 full sessions of HFT/micro data on every trade.

### 2026-05-19 — Setup Analyzer panel (build deferred)
User idea: a terminal panel showing each trade with key signal fields
side-by-side, color-coded by outcome, with bucket comparisons at top.
All data already in DB; ~4-6hr frontend build. Defer until we have
5+ sessions of full HFT/micro data (otherwise the panel shows a half-
populated table). Until then, ad-hoc analysis via /diag/signal_execution_audit
+ /history/actionable serves the same purpose.

### 2026-05-19 — Hour-of-day profitability
Both sessions showed 11am–noon CT as consistently profitable
(5/18: +1.04R, 5/19: +1.038R per user observation). Not yet
quantified across the full trade set; should add an "hour_of_day_R"
breakdown to DailyReview if we want to track this rigorously.
Currently captured implicitly via DailyReview.by_hour_ct.

---

## REVIEW PROTOCOL

- At end of each trading day, walk this file
- For each OPEN item, ask:
  - Has more data made the right action clearer?
  - Is it now low-risk to ship?
  - Is the freeze counter near 10/10?
- Promote items to SHIPPED only after they're actually committed
- Never delete items — track history of what was considered + decided

