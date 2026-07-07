# Multi-Symbol Expansion Plan — flow-surge beyond the index complex

**Status: PLANNING ONLY (2026-07-07). Nothing here is approved for implementation.**
Owner intent: longer-term track, iterated behind the SPX core. Nothing goes live —
or even gets built — until its phase gate passes. Candidate universe, in order:
SMH → TSLA / NVDA / AAPL / META / PLTR.

## Why this might work (and why the SPX nulls don't condemn it)

All 15 direction nulls were measured on **index** flow, which is structurally
hedging-dominated (dealers and funds laying off exposure) — the documented
mechanism behind "flow follows price." Single-name option flow is where informed
directional traders actually express views; sweep-flow direction signal is
plausibly real there in a way it demonstrably isn't on SPX. This is a new
hypothesis, not a re-litigation. Prior stays skeptical: expect null until the
data says otherwise.

## Why not just flip the symbols on (the two hard blockers)

1. **Expiry structure.** SPX/SPY/QQQ expire daily. SMH/TSLA/PLTR have weeklies:
   "0DTE" exists ~1-2 days/week; other days are 1-4 DTE — different theta/gamma,
   different exit geometry, different magnitude baselines. The engine is 0DTE-shaped.
2. **Threshold calibration.** Surge thresholds are absolute $M levels tuned to SPX
   flow magnitude. On a single name they either never fire or fire on noise
   (live exhibit 2026-07-06: GLD out-fired SPX 47:37 on garbage). Fix = per-symbol
   **percentile** thresholds calibrated from trailing flow distributions — the same
   build that fixes the queued pattern-overlay scaling defect.

Single-name extras the index never had: earnings/guidance events, halts, meme
squeezes, chunkier strike grids, retail-lottery flow character (PLTR/TSLA
especially). SMH dodges most of these (ETF: no own earnings, no halts) — which is
why it's the bridge candidate — but it still gaps on constituent news (NVDA).

## Phase 0 — data audit (zero risk, ~1 evening)
- Coverage matrix per candidate: ThetaData option NBBO history depth (strikes ×
  expiries × years), QuantData live flow/exposure quality, expiry calendars.
- Deliverable: which symbols are even researchable at our standards
  ([[tick-data-mandate]]: real NBBO at decision points, no 1-min-only tests).
- **Gate:** a symbol without deep historical option quotes is OUT (no exceptions —
  that rule is what caught the flow-surge spot-reconstruction artifact).

## Phase 1 — flow-character research (zero risk, the big one)
- Per symbol: drift/surge distributions by time of day; percentile threshold
  candidates; vendor sanity (QD vs Theta agreement on single names).
- Re-run the strict-replay methodology per symbol with per-symbol thresholds:
  surge → forward real-underlying move (never chain-derived spot), T+1 entry,
  real NBBO fills, session-clustered stats, OOS split. Both direction AND
  magnitude layers.
- Earnings interaction study: surge behavior in the N days around earnings
  (likely needs an exclusion window, like FOMC mornings on SPX).
- **Gate (pre-committed):** direction edge after friction ≥ +$15/ct OOS with
  t ≥ 2 on ≥1,000 fires for a symbol to advance. Magnitude-only findings route to
  decision-support, not auto-trade. All-null → track closes with receipts, like
  the other 15.

## Phase 2 — plumbing build (freeze-safe, only for symbols that passed Phase 1)
- DTE-aware contract selection (nearest weekly; DTE-scaled exit ladder + theta
  expectations; magnitude baselines per DTE).
- Per-symbol percentile threshold auto-calibration (trailing N sessions,
  refreshed daily; kills hand-tuned constants). Ships behind a per-symbol flag.
- Earnings calendar gate (extend event_calendar; same skip-with-counterfactual
  pattern as FOMC/TOD).
- Per-symbol spot/bars capture (already fixed 2026-07-07 to follow symbols) +
  chop-gate distributions per symbol (already per-symbol by design).
- Suite + adversarial review per the post-7/7 standard for any watcher-adjacent code.

## Phase 3 — shadow (signals fire, nothing trades)
- Symbol-level log-only mode in the scanner: full verdicts, gate tags,
  counterfactual option-$ grading vs same-day chain — zero orders.
- Run ≥ 3-4 weeks / ≥ 30 shadow fires per symbol. Grade exactly like the gates:
  clean-signal $/ct, all-fires, would-be exit performance.
- **Gate:** shadow clean-signal ≥ backtest expectation minus friction reality
  (no "it'll be different live" exceptions).

## Phase 4 — live paper, capped
- 1-2 contracts, per-symbol daily fire cap (2-3), full audit + EOD counterfactual
  cards, 50-fire verdict window per symbol (kill/redemption thresholds set from
  Phase 1 numbers before the first live fire).

## Standing conditions across all phases
- **SPX core verdict comes first.** This track never jumps the queue while the
  50-fire window (currently 6/50) is open. Research phases may run in parallel
  overnight; build/live phases wait.
- Evidence-gated shipping applies at every step; every gate has pre-committed
  kill criteria written BEFORE looking at results.
- No shares, ever (owner decision 2026-07-06). Options only.
- Exotic-thin books (GLD/TLT class) stay out regardless of phase results.

## Immediate next actions (when owner says go — all zero-risk)
1. Phase 0 coverage matrix (one overnight agent).
2. Percentile-threshold prototype on SMH history (doubles as the pattern-overlay
   scaling fix we already owe SPX-complex symbols).
