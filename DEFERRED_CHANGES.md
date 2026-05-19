# Deferred Changes — End-of-Day Review Queue

Items flagged during sessions but NOT yet shipped, so the freeze counter
can run uninterrupted. Each item notes (1) what's broken or could be
improved, (2) what category of change it is, (3) whether shipping would
reset the freeze counter.

**Freeze status**: 0/10 sessions since last engine change. Reviewer
(Chat Claude) recommends no engine changes until ≥10.

Add new items at the top. Move shipped items to the "Shipped" section.

---

## OPEN ITEMS

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

### 2026-05-19 — BE ladder uses SPX, not option price [HIGH PRIORITY]
- **Symptom**: BE ladder correctly moves stop to entry SPX, but the
  option premium at "entry SPX" is LOWER than entry option price after a
  round trip. Theta decay (5-15% on 0DTE per ~10 min) + bid-ask slippage
  (~$0.20/contract round trip) means "SPX BE" ≠ "option BE". Trades can
  exit at "0R locked" SPX but actually negative on P/L.
- **EVIDENCE FROM 2026-05-19 (live paper data)**:
  - 9 parent trades, 2 winners, 7 losers
  - **5 of 7 losers** triggered BE ladder at +0.5R MFE, then retraced to
    entry SPX. Exited at "locked +0R" per the system but option-negative.
  - User observation confirmed: "trades that go green then red are the
    majority of losses". Empirically true for today's sample (5/7 = 71%).
  - Secondary observation: late-day stops were WORSE than early stops
    even at the same "+0R locked" classification. Theta acceleration on
    0DTE through the afternoon. Structural feature of 0DTE, not engine
    bug — but the BE design needs to ACCOUNT for it.
- **Two fix variants**:
  - **2a (calibration buffer, easy)**: Change `BE_LADDER` to
    `[(0.5, 0.2), (1.0, 0.6), (1.5, 1.1)]` — at MFE +0.5R, stop moves to
    entry + 0.2R giving theta cushion. Buffer magnitude is still a guess
    without multi-session data.
  - **2b (option-price ladder, correct)**: Capture actual fill price at
    order placement. Watcher polls option mid every N seconds, ratchets
    stop in OPTION-PRICE space. Mathematically guarantees "locked +X%"
    means real P/L. Independent of theta. Requires extra IBKR quote calls
    per parent — adds API load.
  - **2c (time-of-day-aware buffer)**: 2a's buffer scales with minutes-
    to-close. Earlier in day = smaller buffer. Late afternoon = larger
    buffer. Empirical calibration after multi-session data.
- **Category**: Engine / exit-strategy change.
- **Resets freeze?**: YES. The reviewer specifically warned about this
  kind of mid-flight tuning. With today's evidence (5/7 losers fit the
  pattern), this is the highest-priority deferred change, but waiting
  for the freeze to elapse + multi-session data is still the right call.
- **Decision criterion for shipping**: After ≥10 sessions, if the
  "5/7 losers fit BE retrace pattern" ratio holds AT or above 50%, ship
  variant 2b (option-price). If the pattern fades with more data, the
  current SPX-BE may be fine.

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

## SHIPPED ITEMS THIS SESSION

(Nothing yet today. Yesterday's commits are in git log; see
`FEF_TERMINAL_BUILD_REPORT.md` for the full inventory.)

---

## REVIEW PROTOCOL

- At end of each trading day, walk this file
- For each OPEN item, ask:
  - Has more data made the right action clearer?
  - Is it now low-risk to ship?
  - Is the freeze counter near 10/10?
- Promote items to SHIPPED only after they're actually committed
- Never delete items — track history of what was considered + decided

