# Pre-Registered Hypothesis Ledger — 0DTE Mechanism-Based Edge Discovery

**Locked: 2026-06-29.** Thresholds fixed a priori. No post-hoc tuning. Each hypothesis
names an economic mechanism and a counterparty, takes direction from price structure (never
a flow predictor), is tested one at a time with go/no-go, on ATM real-option P&L (ask in /
bid out, commissions + theta modeled), and must beat all four controls (regime-blind,
random-direction, opposite-side, drift-neutral).

Dev set: 2022-01-03 .. 2025-08-12 (874 sessions). Holdout (2025-08-13 .. 2026-06-26)
reserved, touched once in Phase 5. Kill criterion: DSR @ N_eff >= 0.30 on dev AND beats all
four controls AND survives >1 regime/year — all on ATM real-option P&L.

---

## H1 — Short-gamma continuation  *(test FIRST)*
**Mechanism:** in SHORT_GAMMA regime, dealers hedge with the move (buy rallies / sell
declines), amplifying an established directional move. **Counterparty:** dealers forced to
hedge negative gamma.
**Pre-registered rule (locked):**
- Established move trigger: spot moved **>= 8 pts over the prior 10 min** (the established leg).
- Regime gate: **GEX < 0 (SHORT_GAMMA)** at the trigger minute (from Phase-1 reconstruction).
- Direction: **the side the move already committed to** (up-move -> long CALL, down -> PUT). ATM-at-entry.
- Continuation test: does spot extend **>= 8 more pts** in that direction before a >= 4pt adverse reversal?
- Comparison: continuation rate AND ATM-option P&L in SHORT_GAMMA vs **LONG_GAMMA** (the regime-blind/regime-contrast control).
- One trade/session (first trigger). Entry window: full session 09:30–15:00 ET.
- Exit: trailing / B+C+D (theta-aware) — the move must outrun ATM theta.

## H2 — Charm power-hour flow
**Mechanism:** in the final 90 min, charm-driven re-hedging as 0DTE deltas decay into expiry
produces directional pressure in the underlying. **Counterparty:** dealers re-hedging delta decay.
**Pre-registered rule (locked):**
- Window: **14:30–16:00 ET** (final 90 min).
- Threshold: **|CEX| >= dev-set 70th percentile of |CEX| in that window** (one principled threshold, computed on dev only).
- Direction (locked mapping): **CEX > 0 -> long CALL, CEX < 0 -> long PUT.** (If the true sign is opposite, the opposite-side control will win and reveal it.)
- Entry: first minute in the window where |CEX| >= threshold. ATM-at-entry. Exit: trailing / B+C+D.

## H3 — Vanna on vol-moving days
**Mechanism:** when IV shifts, vanna re-hedging by dealers moves the underlying.
**Counterparty:** dealers re-hedging vega/delta as IV changes.
**Pre-registered rule (locked):**
- Vol-move gate (VIX-proxy, noted substitution — no VIX feed): **|ATM-IV change from 09:30 to entry| >= 3 vol points.**
- Threshold: **|VEX| >= dev-set 70th percentile of |VEX|** on gated sessions.
- Direction (locked mapping): **VEX > 0 -> long CALL, VEX < 0 -> long PUT.**
- Entry: first qualifying minute. ATM-at-entry. Exit: trailing / B+C+D.

---

### Controls (every hypothesis must beat all four, priced on the same ATM real-option basis)
1. **Regime-blind:** same trigger, ignore the gamma/charm/vanna gate. Mechanism must beat this or the conditioning adds nothing.
2. **Random-direction:** same cadence, random side. The floor.
3. **Opposite-side:** opposite direction at the same signals. Should be symmetrically negative if direction is real.
4. **Drift-neutral:** regress trade P&L on session drift; require a surviving drift-neutral intercept.

## H6 — Charm/vanna directional long, DTE sweep (1/2/3 DTE) *(data-pending: multi-DTE ingest)*
Locked: reuse H2/H3 thresholds, recomputed on DTE-appropriate exposures (NEVER reuse 0DTE positioning).
ATM-at-entry long of that DTE, real fills. Exit trailing/B+C+D + fixed-profit-fraction variant.
Deciding diagnostic: gross edge vs theta+spread drag by DTE (0DTE baseline ~$40 edge / ~$130 drag).
Headline control: **drift-neutral** (longer DTE = more beta; edge must survive removing day drift). Three
sub-hypotheses reported, never only the best.

## H7 — Near-ATM credit spread, quick-profit exit *(LOCKED 2026-06-29)*
Structure: 10-wide near-ATM iron fly — short strikes at ATM+/-5, longs at ATM+/-15. Side rule = **GEX gate**:
trade only in LONG_GAMMA (gex>0) at entry; stand aside in SHORT_GAMMA. Entry **14:00 ET** (locked). Exit:
**profit target 50% of credit** (primary) + stop 2x credit + hard close. Realistic near-mid fills, 4-leg commission.
**Deciding metric = expectancy after friction + LOSS DISTRIBUTION** (worst losses, avg-win/avg-loss, does
accumulated credit survive the tail), NOT win rate. Controls: **OTM Dale-style (short ATM+/-20) = HEADLINE**
(near-ATM must beat OTM on tail-priced expectancy or the hypothesis fails); regime-blind; no-quick-exit.

## H7b — CLEAN RE-PRE-REGISTRATION of the H7 candidate *(LOCKED 2026-06-29, zero tuning)*
The H7 diagnostic's +$81 came from the no-quick-exit CONTROL, not the pre-registered primary -> post-hoc
discovery -> must be re-validated clean. Locked config (exactly as the diagnostic found it, NO tuning):
- Structure: near-ATM iron fly, short strikes at ATM+/-5, 10-wide wings (longs ATM+/-15).
- Gate: enter ONLY in LONG_GAMMA (gex>0); stand aside in SHORT_GAMMA and TRANSITION.
- Entry: 14:00 ET. Exit: HOLD TO SETTLEMENT (no quick target, no managed exit). Size: 3 contracts.
- Fills: real chain, 4 legs (shorts bid-in/ask-out, longs ask-in/bid-out), commission + slip every leg.
DECIDING METRIC = the TAIL/DRAWDOWN, not the mean or DSR. Pre-committed kill conditions: (1) DSR @ N_eff
(counting ALL H1-H7 configs/variants/controls) < 0.30; OR (2) max drawdown unsurvivable at realistic account
size (positive expectancy + unsurvivable DD = FAILED); OR (3) doesn't survive >1 year. Report dev full-stack
+ drawdown distribution to user BEFORE touching the holdout. Holdout touched ONCE, config unchanged,
interpretation pre-committed (+$30-50 = real edge deflated by selection; full +$81 = suspect leakage;
negative = selection artifact). See [[project-naked-long-direction-null]].

## H8-H11 — SPY momentum/mean-reversion x daily/intraday *(LOCKED 2026-06-30)*
Headline control ALL: beat buy-and-hold SPY on Sharpe (drift contamination = the H6 killer on the underlying).
Data: daily SPY from IBKR 2011-07..2026-06 (dev 80% chronological = <2023-06-28); intraday = parity-SPX 2022-26 proxy.
Stops modeled pessimistically (gap risk, next-open fill). Max DD survivable at realistic size = binding kill.
- H8 momentum daily: long SPY when close>SMA(50), flat else. Locked.
- H9 momentum intraday: long when price>VWAP and breaks prior 30-bar high; flat by close. Locked.
- H10 mean-rev daily: long when z(close,20)<-1.0; exit z>=0 OR 10-day OR -8% stop (gap next-open). Tail decides. Locked.
- H11 mean-rev intraday: long when intraday z(20-min)<-1.5; exit VWAP reversion OR EOD. Locked.
Step-3 gate: report buy-and-hold-Sharpe + max-drawdown per strategy BEFORE the full stack; dead ones stop there.

### Mandatory diagnostics
- **Speed-of-move** logged on every winner: points needed + minutes to arrive (is the edge immediacy-dependent?).
- N_trials counts **every hypothesis × exit variant** tested (DSR honesty).
- Theta-specific branch: a mechanism that is directionally real but dies once ATM theta is priced is a *finding* (express on 1DTE+), not a discard.
