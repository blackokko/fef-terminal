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

### 2026-05-19 — BE ladder UI label is misleading
- **Symptom**: Live Execution panel shows `"locked +0.5R"` when the BE
  ladder fires at the 0.5R MFE trigger. Actual locked profit is **0R**
  (stop moved to entry SPX, the BE step). User watched a trade go
  negative despite the panel saying 0.5R was locked.
- **Root cause**: I'm displaying `be_ladder_max_step_R` (the trigger that
  fired) as if it were the locked-profit-R. The audit log gets it right
  ("locks 0.0R") but the UI doesn't.
- **Fix (UI honesty, NO engine change)**: Compute locked-R from
  `(dynamic_stop_level - entry_spx_price) / risk` and display that
  instead. Could ship without freeze impact.
- **Category**: UI labeling bug. Pure render-time math.
- **Resets freeze?**: No.

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

