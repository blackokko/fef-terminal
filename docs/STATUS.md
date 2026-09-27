# STATUS — 2026-09-27

Current state of the operation, what was just learned, and what to do next, in order. Written for an
agent picking this up cold. The research detail behind every number here is in
`blackokko/fef-research-archive` — `RESEARCH_REGISTRY.md` §H‡‡ (`vbreak`) and rule 36.

---

## 1. The operational state — what is live, and what has never fired

| component | state |
|---|---|
| **0DTE directional bot** | **Retired 2026-08-25.** Its data source (QuantData) was cancelled; `snapshot_to_inputs` was the only bridge into the FEF engine. Its premise was independently falsified: intraday direction on SPX is structurally foreclosed (achievable edge 0.08–0.35 index pts vs a 0.442 pt friction floor; the inverted signal performs identically at 49.2%). |
| **Credit-spread bot** (0DTE SPX put vertical, ~0.15Δ, 1 lot, sells every session) | **Armed, never filled.** 200 parent orders 2026-08-12 → 08-25, zero fills: 197 died on `Bag()` being called positionally (ib_async is keyword-only), 3 on an env gate. **All four known blockers are now fixed** — env var in the launcher, strike picker rewritten to nearest-delta with a credit/width floor, `Bag(symbol=...)`, and the short leg held to a two-sided quote. Toggle is in the UI (`SpreadAutoTradePanel`). Default is OFF; the launcher flag is the capability, the panel is the intent. |
| **Sleeve executor** (trend v3 + PEAD → MOC orders) | **Built, tested, never transmitted.** 33 tests, five guards mutation-verified. Six MOC buys have been pending since August (trend v3: EFA, VNQ; PEAD: single names). Quantities come from `plan` — do not hard-code them, the ledgers move. Needs TWS up on 7497 before 15:45 ET. Carry v1 disabled (measured de-allocate, −4.46 %/yr alpha). |
| **Reconciliation harness** (`recon.py`) | **Built and tested; no measurement yet.** Zero credit-spread fills exist to reconcile. Two of its four Rule-3 join gates currently fail — the join is sound, the recorded anchors are contaminated — so it correctly publishes no aggregate. The adverse-selection prior stays at −$16.34/ct, CI [−156, +84]: unmeasured. Its advisory cross-check read `chainfill_cache.db`, now gone (§3); the primary path (live `signal_log.db` + ThetaData via `recon_quotes.db`) is intact. |
| **Collectors** | `runner_watch.py` and `caphead_watch.py` run from `D:\fef-data` (restarted there 2026-09-26, detached). Do not kill them during market hours. `FEF-DailyOpen` restarts them at 08:30 CT if they are down. Find them with `Get-CimInstance Win32_Process` filtered on the script name; PIDs change. |
| **Paper ledgers** | Trend v3, PEAD, overnight, carry v1, caphead grading and runner outcomes update daily via `FEF-DailyClose` from `D:\fef-data`, mirrored to the archive. **The carry ledger failed silently on 16 runs, 2026-09-03 → 09-25** (§3); repaired 2026-09-26, and the one rebalance it missed (2026-09-08) was booked late by the same mechanical rule. |

**The honest book-level number:** portfolio alpha **+17.75 %/yr at t = +1.50** — economically
interesting, statistically unconfirmed. Portfolio R² against 11 factors is **0.005** (not a
purchasable ETF in disguise); residual correlation maxes at **0.098** (diversification is real).
Trend v3 is the best sleeve and is not MTUM in disguise (MOM beta +0.036), but ~31 % of its raw
return is purchasable (SPY/IWM/DBC minus credit), leaving **+$682/yr alpha per $10k**; it clears
t ≥ 2 and BH-FDR and fails only the jackknife (drop 2025 → t +1.40). The registry's own caveat:
"best thing in the book" is window-conditional. **Only live fills over time resolve t = 1.50.**

## 2. What was just learned — `vbreak` (2026-09-16)

Tested the *published approximation* of Cracking Markets' intraday volatility breakout
(`close ± k×ATR` stop-entry, `k×ATR` stop, exit 1h / EOD / 2d) on QQQ, SPX, and an IWM stand-in.
54 cells, two independent implementations differenced, his horizon-ladder control plus our full
stack, five adversarial audit lenses. Full lock and report in the archive.

**Verdict: the published approximation does not survive on our data, and our data cannot see his
claim either way.**

- **His mechanism inverts.** He reports the edge at 1–10 h dying at 2 d (Sharpe 1.59 → 0.21).
  Here **2-day is the best horizon in 18 of 18 ladders** (his ladder fails 54/54), and the 2-day
  gain is long-only drift.
- **On QQQ — the only real ETF tested — it is geometry, not level.** His example cell vs a random
  entry with the identical stop and exit: **41.0% / PF 1.081 / +0.044 R vs 40.4% / 1.083 / +0.044 R,
  49th percentile.** "Buy the open with the same stop" beats it in every EOD and 2-day cell.
- **SPX looked better and the audit found why.** Four 1h cells beat the twin at t ≈ 2 — but SPX is a
  calculated index with 1-min ρ₁ = +0.045, and running NDX vs QQQ (index vs its own ETF) shows the
  index beating the ETF by +0.04 R at t 3.4–3.6 on exactly those cells. **On anything tradeable, G2
  is 0/18.**
- **IWM was never tested.** Its bars were gone (§3); the runner substituted RUT/10. Those 18 cells
  cleared 7/8 gates at t up to 10.8 — **every one an artifact** (RUT ρ₁ = +0.336). Flagged, not
  quoted. This became **registry rule 36**: a calculated index is never a stand-in for its ETF at
  1-minute horizon. **Corollary still open:** our own "SPX trends at exactly the 1-minute horizon"
  finding must be re-checked on SPY/ES before it is cited again.
- **Underpowered by design, pre-registered as such.** Three instruments = 13.5 market-years;
  expected t ≈ 0.9 *even if his forty-market claim is exactly right*. t ≥ 3 needs ~142 market-years.
  Detection floor 8–20× above breakeven in 54/54 cells. **Branch E, not a null.**

**What it does and does not say about him:** it refutes the free version of his rule on QQQ/SPX and
contradicts the mechanism he describes; it **cannot** refute forty markets. Costs do not decide it
(his "~10% commission" reproduces at 8.1%). The strategy shape plausibly earns a little on trending
instruments — the twin earns the same +0.044 R — so "modestly profitable" and "the level is why"
are different claims, and only the second one failed.

## 2b. This week's studies (2026-09-27) — scope: equity options and shares only

Pre-registered in the archive before any data, each run by a separate agent. Registry section E and H.

| study | what | state |
|---|---|---|
| `lasthalf` | SPY 15:00–15:30 move predicts 15:30–16:00, traded as SPY shares | **Closed: no edge.** +0.87 bp per trade before costs, −0.13 bp after a 1 bp round trip, 2017–21. The July "survives OOS" claim was a regression t, not a trade (registry J). |
| `hedgevol` | SPY iron fly ~45 DTE, delta-hedged daily with SPY shares, 2016–2026 | **Closed: Branch C, not harvestable at retail.** −$27.07 per monthly position at bid/ask, +$3.00 even at the mid; both halves negative. Hedged straddle/strangle without wings +$15/+$31, not significant, with 0.5 SPY correlation. ATM implied beat realized volatility by only +0.80 vol points on average; the premium sits in the crash-protection wings. |
| `earnvol` | S&P 500 single-stock options around earnings: buy the run-up, sell the crush | **Closed: both legs Branch C.** 2,566 / 2,358 events 2016–2026. Run-up −5.58 % of premium per event at bid/ask (+0.79 % at mid, not significant), no better than a non-earnings date. Crush iron fly −23.40 % of max loss (−0.77 % at mid). Negative in all 11 years. Spread cost $72 per straddle, $171 per fly, with no premium underneath at the mid. |

**What the week's four in-scope tests say together:** on SPY and single-stock options, the premia are
priced close to fair at the mid, and the bid-ask spread — which the market maker collects — is the
largest term in every result. Paying it is the retail trader's structural cost.
| `vixroll` / `vixroll2` | Short VIX futures in contango | Closed at the harness gate (benchmark error, rule 37), then **cancelled: out of scope** |

**Open question for the operator:** the trend v3 paper sleeve holds GLD, DBC and HYG alongside equity
ETFs. Under the equities-only scope those would be out; not changed pending the operator's answer.

## 3. Data loss, and the move out of Temp (DONE 2026-09-26)

**What was lost while the data lived in Temp.** `chainfill_cache.db` (574 million option quote rows,
964 sessions, 49.8 GB — the backfill behind every credit-spread study and `recon.py`'s cross-check),
`iwm_bars_cache.db`, `conf_siblings.db` and ~50 other databases, last seen 2026-08-25. **Also
`carry_v1_study.py`**, deleted between the 2026-09-02 and 09-03 ledger runs: the carry ledger imports
it, so it failed on 16 consecutive runs and nobody noticed (the scheduled task went red; nothing
reads that). Cause of the deletions not determined; Storage Sense is enabled.

**The move.** The whole session scratchpad (2,332 files, 34.9 GB) now lives in **`D:\fef-data`**
(Samsung 990 PRO NVMe, 1.2 TB free) with the same relative layout, so an old report's
`...\scratchpad\X` is now `D:\fef-data\X`. Every file was SHA-256-verified against its original
before the originals were deleted. 318 code files had hard-coded Temp paths rewritten; each rewritten
Python file was proven path-only by an AST comparison. The ledger job and the collectors run from
D: and passed a full run. `carry_v1_study.py` was rebuilt by replaying the session transcript that
wrote it and reproduces the study's saved output **bit-exact over 4,915 days**. Record, manifest,
proofs and a zip of the pre-rewrite code: `D:\fef-data\_migration_2026-09-26\`. Git backups of every
ledger updater and its imports: `fef-research-archive\ledger_updaters\`.

**Still to rebuild when a study needs it:** `chainfill_cache.db` from ThetaData (hours of pulls);
`iwm_bars` from Yahoo/IBKR. D: is not backed up anywhere; the archive's git copies cover code and
ledgers, not caches.

## 4. Priorities, in order

1. **Run what exists.** This is the actual next step and has been for a month. Start TWS on 7497,
   then in PowerShell from the archive, before 15:45 ET:
   ```powershell
   cd C:\Users\diant\fef-research-archive
   python sleeve_executor.py plan
   $env:FEF_SLEEVE_EXEC_LIVE = "true"
   python sleeve_executor.py execute --i-mean-it --accept-unknown-turnover
   ```
   Launch the terminal via `FEF-Paper-AutoTrade.bat` and flip the credit-spread panel on. **Every
   remaining uncertainty that matters — real slippage, adverse selection, whether t = 1.50 is real
   — is resolved by fills, not by research.** The `Bag()` bug that killed 197 orders was found by
   *trying to run*, not by any study.
2. ~~**Move the research caches out of Temp.**~~ **Done 2026-09-26** (§3). Rebuild
   `chainfill_cache.db` from ThetaData only when a study needs it.
3. **The one cheap study that needs no new data:** the leverage question from `sizing` — the book
   runs at mean gross **0.753** and hits its 1.0 cap on **27 %** of rebalances. Standalone lock,
   directly changes the book that would be traded.
4. **Then breadth, or stop.** Every research thread since `powercal` has ended at the same wall:
   the control stack certifies 13.4 %/yr on 289 names, ~4 % on 3,000, and three instruments cannot
   see a forty-market effect. Within the equities-only scope, breadth means many stocks, not many
   asset classes: single-stock options events (`earnvol`) are the first study built that way. **Do
   not run another single-instrument intraday study — we have measured that it cannot resolve the
   answer either way.** New data goes to `D:\fef-data`, never the agent scratchpad.

## 5. Do not

- Re-open a closed family (registry §H) without new evidence. Exposure/GEX (closed by proof),
  candle patterns, entry timing on trend v3, 1/N sizing, multi-timeframe confluence, indicator
  confluence, channel levels, volume-at-price — all measured, all closed.
- Substitute an index for an ETF. Substitute a proxy for missing data at all — report PARTIAL.
- Trust a gross Sharpe as net, a backtest as live P&L, or a stats page as a track record. Nobody
  we have looked at — including the two practitioners referenced in the archive — has published
  a live-vs-backtest reconciliation. `recon.py` is built to do exactly that, once it has fills.
- Use `effect/MDE ≥ 2` as a pass gate. It is a disguised |t| 3.9–5.0 demand that killed injected
  signal 40/40 in two harnesses (`cocbar` / `entrytime` locks). Report the detection floor with
  every null (rule 18); do not gate on it.
- Compare weight schemes at unmatched gross or risk (rule 34), or run a scramble control without
  proving it can move the statistic (rule 33).

## 6. Leftovers an agent will trip over

- **QuantData was cancelled 2026-08-25.** The poll loop, chain capture, panel, MCP server and API
  routes are gone; each site carries a `[REMOVED 2026-08-25]` tombstone. The provider name survives
  in the engine's input schema, `config_store.py`, older docs (`README.md`, `SETUP.md`,
  `SMOKE_TEST.md`, `docs/OPERATING_CARD.md`) and the launchers' `-QuantDataMode` flag, which nothing
  in the sidecar reads any more. Cleanup is safe but not urgent. Do not rebuild a QuantData client.
- **Open review items on the credit-spread path** (from the 2026-08-25 adversarial review; none
  blocks a first fill): narrow the `try` in `live_trading`, TP sentinel on a crossed pair,
  `confirm=True` actor mislabel, half-day sessions, `/panic/flat` does not disarm the spread bot,
  scan step coupled to spread width.
- **The next research lock must state the entry-bar stop convention.** The `vbreak` implementation
  diff found +201 gross R over 54 cells riding on it (99 of 123 affected trades opened beyond the
  stop; one implementation books −1 R, the other rides to +5…+8.5 R).
