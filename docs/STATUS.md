# STATUS — 2026-10-04

Current state of the operation, what was just learned, and what to do next, in order. Written for an
agent picking this up cold. The research detail behind every number here is in
`blackokko/fef-research-archive` — `RESEARCH_REGISTRY.md` (§E for the studies below, §J corrections).

**⚠ 2026-10-04 — URGENT, `govcap` (E62 → H55, archive 23be31b):** the sleeve executor's 20× turnover cap on `main` is UNSAFE.
- It will probably refuse the Wed 11-04 month-end rebalance, exits included: P 0.83. It can bind from Fri 10-30.
- The `sleeve-capital-250k` branch lowers that to P 0.34, but its reduce-only lock can last until the book is empty.
- **Recommended: build G6 CHURN on top of the branch.**
  - Remove rule 28 as a gate.
  - Attribute the own book to sleeves, with a NET bucket.
  - Withhold only a sleeve's re-entry within 5 sessions of its own reduction.
  - Keep the gross guard.
  - Remove the wrapper's `--accept-unknown-turnover`.
- **Needs the operator's go**, then a build, tests and an adversarial review before 10-30.
- The PC ran critically low on memory at 23:10 CT on 10-03. Run at most 2 heavy executors.

- **`dipslots` RAN (E56 → H57):** keep the dip-buy at 5 × 20 %. The alternatives earn fewer rule-D dollars because the cap rarely binds. Branch D is on a negative-side vehicle gate, within its measured false-failure rate.
- **`earnprem` RAN (E32 → H56, archive ecc9b01):** the earnings-announcement premium is Tier 0 after costs (−$1.07 an event, t −0.46; +$0.03 before costs). The shared earnings harness `eh` is frozen, which unblocks E33 / E34 / E52 / E53 / E67.

**2026-10-03 (Sat):**
- **`spypcs2` is FINAL:** G0(h) PASS, 20/20 tick windows (archive 0979519).
- **Running:** `pead2`, `chanx` (its pull waits for `spxwide2`'s tick checks), and `idxdip` (the SPY/QQQ/IWM share twin of the channel touch, scored pre-2017 only).
- **The PC force-slept 23:06 Fri → 12:48 Sat.** The FEF tasks have WakeToRun=False. The operator must keep it awake (and TWS up) Monday.
- **`bookdd` RAN (E65 → H54, archive 22e8024): pool the budget, keep the mix.**
  - The four Tier-1 sleeves diversify each other: DR 1.75. No re-weighting beats the rule-D proportions.
  - Author ruling: pooling scales only by the diversification credit, and only for sleeves with their own ≥ 3-month forward record. None qualifies yet, so **no size changes**.
  - The book kill line is report-only.
- **`carrycost` RAN (E57 → H53, archive cfc8857):** at IBKR, idle cash earns ≈ 0.46 %/yr under T-bills (0 % on the first $10k), and a ≈ $100k SPY short hedge costs ≈ 4.5–4.9 %/yr (0 % on the first $100k of proceeds).
  - **K40 is amended to the measured rates.** No tier changes.
  - Operator scope call (O4): a T-bill ETF for idle cash, ≈ +$270–650/yr.
- **`chanx` RAN (E40 → H52, archive c0eff18): SPYPCS-4 is UNREPLICATED on QQQ + IWM.**
  - +$3.51/chain, t 0.46, P 0.678 → Watch.
  - Its timing is no better than random, and a plain "fell 1 %" rule does as well.
  - The live RE rule loses to its own index exposure there too (α −$107/mo).
  - **The S3B book stays on paper at 4 lots** under its kill rule (its SPY tier is unchanged). No step toward live money before SPY's own forward record and a live fill check.
  - The pull took 10,155 requests in 6.1 h.
- **`regate` RAN (E63 → H51, archive 155f594): RE's size is the only lever (Branch C).**
  - Book excess Sharpe: + S3B at matched risk 0.731; + SPY shares (≈ $38.5k) 0.691; no sleeve 0.642; fewer RE lots 0.60; **RE at 5 lots 0.591**.
  - The drawdown gate cuts RE's added DD 58 %, but at matched risk it is no better than fewer lots.
  - Operator decision: how much to lever, and in what.
- **Batch 4 pre-registered (archive d6633c1 + this commit), E62–E69.**
  - Decision support:
    - **govcap** (E62): the executor turnover governor, report due 10-16; running.
    - **regate** (E63): the RE leverage sleeve's sizing; running.
    - dipexec (E64) and bookdd (E65).
  - Strategies:
    - **dipmid** (E66): the S&P 400 dip-buy, P(T1) ≈ 0.40. Its ≈ 8k EOD pull is queued ahead of `sspcs`.
    - earnmid (E67) and insidmid (E68): likely to fail on the mid-cap drift under a SPY-only hedge.
    - dipvol (E69): a `spxwide2` variant covered by O-FIX.
- **`bookcomp` RAN (E58 → H50, archive eca60cf): no deployable strategy pays when the book loses.** Only the trend short side does, and it is SPY beta, not deployable, era-unstable and costly.
  - Better than SPY shares at matched risk: `spxwide2` (+$223/mo, t 2.93) and overnight arm B (+$50/mo).
  - Not better: the RE rule (pro-cyclical) and idxdip's S3b × SPY (−$39/mo), so idxdip gets a ledger only.
  - §H is delivered as `bookcomp_h.py`. LOCK_TEMPLATE §7c points to it, with Amendment 4's gap-tested labels.
- **`peadrisk` RAN (E55 → H49, archive 965aaad):** if PEAD keeps running, use 10 × $12.5k slots instead of 5 × $25k.
  - That cuts the bad-year drawdown 29.5 % at no measurable cost. It is not an edge, and it is universe- and threshold-dependent.
  - Caps, the SPY hedge and equal-risk sizing don't earn their keep.
  - Puts straddle: the real-quote check needs the operator's yes.
  - The change touches the ledger and the executor, so it needs the operator's go and a review.
- **`spxwide2` checks applied (archive 7507898):** Branch A and Tier 1 hold: +14.17 %/yr, P 0.985, ≈ $19k (cautious $11.6k). Without 2025–26 it is +5.7 %/yr.
  - G0(f) gap fills are PARTIAL (39/50). 19 requests run Sunday after `chanx`.
  - Runner note: entries must join the listing exchange's opening auction.
  - The forward ledger stays the plan, no runner unless the operator asks.
- **`pead2` FINAL (E19 → H48, archive 49bb7ae): Branch C, Tier 0.** The live PEAD edge is market beta plus a look-ahead universe.
  - P1 hedged: −$4.95/event (t −0.70). The live-like hedged cell: −$1.61 (P 0.42).
  - The live lag costs nothing measurable.
  - The study recommends stopping the live PEAD orders and keeping a tracker. It is the operator's call; their 10-02 choice to run it at $125k stands until they change it.
  - PARTIAL: 14 T1 days are unpulled (ThetaData queue). Resume with `p2_repull.py pull` → `p2_seq.py intraday` → the report / registry / bundle scripts (`D:\fef-data\pead2\`). It cannot change the verdict.
- **`idxdip` RAN (E26 → H47, archive c98c651):**
  - **S3b channel touch on SPY SHARES is Tier 1 by rule D** (P 0.919, ≈ $9.9k), out of sample on 1993–2016. It is NOT confirmed:
    - a plain 5-day-drop twin earns 90 % as much;
    - time reversal earns more;
    - the edge is ≈ 0 since 2010;
    - it is the same bet as SPYPCS-4.
  - RSI(2) is Tier 0: the 15:30 clock eats 97 %.
  - **No runner without the operator's go**, plus a ≥ 1-month forward ledger and L1–L5 (incl. the per-sleeve governor).
  - K43 added.
- **Batch 3 pre-registered (archive 18032e4), E52–E61:**
  - datetime: the honest backtest is impossible on disk, so it is Watch-capped;
  - peerxfer, dipnews, fipmom;
  - four decision-support studies on paper money: peadrisk, dipslots, carrycost, bookcomp;
  - ctrydip;
  - ivinnov: a duplicate record.
- **LOCK_TEMPLATE §7c:** a "book fit" exhibit is now required.
- **spxwide2 O-FIX ruling:** the runner stays on P1.
- **Fixed:** the ledger mirror had overwritten the archive's `spxwide2_trades.csv` (never committed; restored). The ledger now mirrors as `spxwide2_ledger_trades.csv`.
- **The executor's 20× governor** refuses whole plans and flat-book plans, so every new sleeve needs a per-sleeve governor. It is a live-path item, bundled with the trend MOO change, awaiting the operator.

**2026-10-02 late afternoon — TWS's API stopped accepting connections: today's sleeve rebalance did NOT trade.**
- **What happened:**
  - At 14:30 CT the sleeve executor found port 7497 refusing connections and placed nothing. The trend month-end
    rebalance (12 MOC orders at 1:1) did not trade.
  - `record-fills` at 15:15 (exit 2) and the SPY runner's `record` at 15:25 (exit 7) failed the same way.
- **The state of TWS:**
  - TWS is running and logged in to paper (PID 2436, started 07:49).
  - It listens on `[::]:7497` but completes no connection; the sidecar's reconnects sit in SYN_SENT.
  - The API worked at 08:43 CT, when the morning `-PlanOnly` read positions.
- **Nothing was at risk.**
  - The SPY RE book was flat (no Friday entries).
  - No executor order was placed, so no day is uncertified: certified 09-30, and 10-01 placed nothing.
- **The operator must fix TWS** (a pop-up, or restart it). Agents: never kill TWS, and never restart the sidecar.
- **The $250k resize: the operator chose option 3 on 10-02 (trend v3 $125k + PEAD $125k).**
  - `sleeve-capital-250k` (review round 2 SHIP, 182 tests) merges once TWS accepts connections again. The order is:
    1. `record-fills` by hand;
    2. merge and test;
    3. push;
    4. `run_sleeve_executor.ps1 -PlanOnly`.
  - Monday 10-05's plan is then the missed rebalance plus the resize.
- **Channel-touch S3B runner** (operator: "Build the channel touch runner. It should be doing those orders even when
  other things are open…"; patient-mid A/B: "run it"):
  - Built: an independent book, client 184, 4 lots, take-profit + plain stop, binding kill rule. 395 tests, mutation
    106/106.
  - Review round 1: **SHIP AFTER FIXES**, 9 findings. The major one: an unreadable other-book state must not block an
    expiration-day close. Also, the wrapper's `max()` of exit codes hid alarms.
  - Review round 2 (16:15 CT): all nine fixes verified, 960 archive tests pass, mutation 173/173. **SHIP AFTER
    FIXES**:
    - a usage error (exit 2) must alarm in the wrapper — required;
    - a degraded RE must reach its existing "roll failed on data → close after 2 marks" fallback — option (b), the
      reviewer's pick, taken by the author; the operator was told and may override;
    - two low items and some test gaps.
  - Review round 3: **SHIP** (970 tests, mutation 192/192, wrapper 149/149 under PS 5.1).
  - **MERGED 17:30 CT (archive 3b85aa3) and INSTALLED.**
    - `fef-watchers\run_spypcs_executor.ps1` + `spypcs_wrapper_lib.ps1` (from `deploy/`). It runs RE then S3B on
      every command, with per-book toasts. All three switches are on: RE live, S3B live, A/B on.
    - Smoke test `status`: both books flat, exit 0.
    - Rollback: `D:\fef-data\watchers_backup\20261002\run_spypcs_executor.ps1.pre_s3b`, which runs RE alone with
      the merged code.
  - **S3B's earliest entry is Tue 10-06 at 14:30 CT** (Tue–Thu only, like RE). RE may rest patiently (the A/B) from
    Mon 10-05.
  - LOW follow-ups from round 3 (non-blocking):
    - the reason text when an entry's final check blocks;
    - test gaps Ma/Mk;
    - a RUNBOOK line for the ghost-fill case.
  - `fillq` amendments:
    - Amendment 1 (f3e5173): the A/B covers both books, and RE is forced L while S3B holds a chain;
    - Amendment 6 (db2fb13): a rest ending AT OR AFTER the limit is forced.
- **`fillq` Phase 0 built (E25, archive 40a1238).**
  - Every decision cell is PENDING until 20 forward fills per runner; the SPY runner's first report comes about
    2026-11-20. F1 (MID+1) stays the fill model.
  - **G5 flagged: IBKR paper MOC fills are not the official close** (09-30: 1 of 10 equal; mean +7.6 bp). So
    `FORWARD_CHECKS.md` §1–2 now price the sleeves' MOC P&L and trend kill rule (c) at the official close plus
    commission.
  - New task **FEF-Fillq** at 18:15 CT (`run_ledgers.ps1 -Fillq`; no orders, no broker).
  - The live check (LV-1 / LV-M) needs the operator's real-money approval; LV-0 needs only their export.
- **Dispatch 16:30 CT:** executors started for `ovlay` (E41) and `volman` (E23), both on disk. `pead2` is still
  running and pulls after `volxs` tonight. `volxs` files at about 21:00 CT.
- **`ovlay` RAN (E41 → H43, archive 9b05dbf), the operator's "better entry rules" question.** Every variant is
  Tier 0:
  - P1, the operator's "sell when SPY ≤ my cost basis": INSUFFICIENT, 25 months, −$1.04/chain.
  - P2, book drawdown ≥ 3 %: does not pay, −$6.11.
  - P3, protective puts: a hedge at a price, the same as holding ~4 % less stock.
  - **The live RE rule is PRO-CYCLICAL:** −$152 a lot in the book's losing months vs +$93, t −4.76.
  - At 5 lots RE is **no better than SPY shares at the same risk** (−$474 a month, t −5.39). It deepens the book's
    max drawdown from −16.4 % to −19.1 %.
  - The channel touch (S3B) is the only SPY spread rule that is neither pro-cyclical nor worse than shares.
  - SPYPCS-4 re-charged to M_eff 39 scores P 0.852 and stays Tier 1, labelled FILL-DEPENDENT. It holds while fills
    stay ≤ 1.60¢ a leg from the mid (author ruling).
  - **Operator decision open:** what to do with RE's 5 lots.
- **`trendbreadth` RAN (E48 → H46, archive 4c7514e):** adding sector and style ETFs does not help trend v3 (Branch C, Tier 0; −0.33 %/yr at the live book's risk). It also MEASURED the live clock: the executor fills at the close of T+3.
  - That costs **0.88 pp/yr CAGR vs the next open (≈ $1,100/yr at $125k)** and deepens the max DD to −16.3 % (vs −14.5 %).
  - The rule-D size on the live clock is ≈ $20.4k, and the FORWARD_CHECKS §2 drawdown line moves to 24.5 %.
  - **Flag:** the executor's 20× rule-28 governor would have refused ~23 % of trend-only rebalances; with PEAD in the denominator this is unmeasured.
  - Candidate change, ONE reviewed live-path change on the operator's go: MOO at T+1 plus a per-sleeve or post-resize-anchored governor.
  - **Monday after 15:15 CT:** run `python -B D:/fef-data/trendbreadth/tb_g0d_check.py` (read-only) to confirm the 09-30 rebalance traded on 10-05.
- **`volxs` FINAL (E12 → H45, archive ba90b6c):** single-stock option volatility sorts on the PIT S&P 500 come out Branch E on both signals (HV − IV and the term slope), with G0 PASS and nothing to trade.
  - HV − IV: +0.26 %/month at the mid (t +0.12). Term slope: +0.62 %/month (t +0.24).
  - Selling the rich names' iron flies loses −11.2 % of max loss a month at bid/ask.
  - This is the third confirmation that single-stock option spreads eat any volatility premium.
  - M facts: a ThetaData option-data hole (roots LB…MCO, 2019-08 → 2020-03), and `expiration=*` drops.
- **Batch 2 pre-registered (20:00 CT, archive e38c084), E42–E51:** divmonth, insider4, mergarb, divpay, vixts, tailtime, trendbreadth, spybwb, resmom and retseas.
  - Most are long shots. vixts and divpay are INSUFFICIENT by construction.
  - `spytiming_HARNESS` Amendment 1: Tiered decision costs; N_eff,clusters is computed before any return, and below 30 is INSUFFICIENT. That is likely for batch 1A's fomcwk, credgate and vrptime.
  - The scout added 13 cards (backlog: 60). EX-3 `trendclock` is the top dollar item: trend v3's live T+3 close vs next open.
- **`volman` RAN (E23 → H44, archive 3132207):** vol-managed sizing adds no exposure-matched return. TREND is Branch
  D (harness) and SPY Branch F (one episode); both Tier 0.
  - **§J:** trend v3's backtest filled at the NEXT OPEN, but the live MOC executor fills at a later close. The next
    close alone costs 0.6–1.0 pp/yr.
  - Candidate fix: MOO orders at S+1, a live-path change that needs the operator's go and a review.
- **`spxwide2` (E21 → H42): Branch A, PROVISIONAL.**
  - P 0.986; +14.26 %/yr net β-matched alpha (t 2.19).
  - The alpha is concentrated in 2025–26: without it, +5.8 %/yr, t 1.56. Tick checks are pending.
  - Forward ledger only. There is no runner unless the operator asks.
  - **The forward PAPER ledger is LIVE:** task FEF-Spxwide2Ledger runs weekdays at 17:30 CT
    (`run_ledgers.ps1 -Spxwide2`) and places no orders.
    - The 10-02 dry run was exit 0, all FINAL: 504 members, 568 ThetaData EOD requests.
    - The wrapper now waits, up to 40 min, while another study's pull log is fresh, then stamps its own log. If the
      wait runs out it books with `--no-theta`; those days stay provisional.
- **`ovlay` (E41) pre-registered:** the operator's "sell puts when SPY is below my cost basis" idea, plus protective
  puts, judged at book level.
- **Operator answers, 10-02 ~13:00 CT** (they close the "decisions open" list below):
  - TWS API precautions: done.
  - The RE runner is a "leverage sleeve": keep 5 lots and research better entries (`ovlay`).
  - Build S3B; run the A/B.
  - The real-money fill test comes later. The manual mid fills were real money (export pending).
  - Commissions: Tiered.
  - The SEC User-Agent is approved, for SEC requests only.
  - A SPY short hedge is OK on paper.

**2026-10-02 afternoon — research pipeline running** (operator: "I want to keep pre-registering 10s of approaches every few hours"):
- `IDEAS_BACKLOG.md` holds 47 ranked ideas. **E21–E40 are pre-registered**, with shared harness docs for SPY timing,
  earnings and SPY options.
- **Results so far:**
  - `spypcs2` RAN (H41): the operator's live SPY rule is Tier 0, α −$44.75/month; the channel-touch × plain stop is
    Tier 1 (P 0.87, 4 lots).
  - `pead2` interim: PEAD Tier 0. The operator keeps it at $125k on paper; see `FORWARD_CHECKS.md`.
  - `spxwide2` found look-ahead in the old dip-buy code; it is re-testing now, with an independent paper ledger due
    to go live at 17:30 CT.
- **Running:** `spxwide2`, `pead2` (intraday cells pending ThetaData), `fillq`.
- **Session crons** run new batches at 06:41/17:41 CT and dispatch every 4 h. Session-only: re-arm them if the session
  restarts.
- ~~**Operator decisions open:** the TWS API precautions (before Mon); the SPY runner's size and the channel-touch build;
  a real-money fill test; the SEC contact string; a SPY short hedge; the IBKR commission plan.~~ All answered at
  ~13:00 CT (see the late-afternoon block above).

**2026-10-02 late morning — rule D adopted, and the archive re-scored** (operator: "Do 1 and 2. Hold on 3.").
- **The rule** (registry §D★, AGENTS.md rule 7): probability, not proof, decides what runs on paper.
  - P = Φ(t − E[max of M_eff null t's]) at realistic fills.
  - Tier 1 (P ≥ 0.75, capped worst case, kill rule) gets small paper orders.
  - Watch (0.60–0.75) gets a ledger only.
- **`DECISION_SCORES.md`** (archive b19c81d): 532 rows; **2 Tier 1, 14 Watch**.
  - Tier 1: S3b channel-touch SPY spreads with a plain stop (P 0.93, 4 lots — ON HOLD per the operator) and trend
    v3 (P 0.79; rule-D size ≈ $22k vs the planned $125k).
  - **PEAD is Tier 0:** the `pead2` interim (PIT universe, β-hedged) gives −$1.83 to −$5.07 per event; the old edge
    was market beta plus a hindsight stock list.
  - The operator's SPY RE rule scores P 0.58 at MID+1 (0.90 if fills really are at the mid).
- ~~**The $250k resize (`sleeve-capital-250k`, review SHIP) is HELD unmerged until the operator decides PEAD and
  trend sizes.**~~ Decided: option 3 (trend $125k + PEAD $125k). It is now held only until TWS's API is back (see the
  late-afternoon block). If it is not merged, Monday's run trades at the old 1:1 size (safe).

**2026-10-02 morning — operator requests in flight:**
- **Sleeves sized to a $250k base** (50/50: trend v3 $125k, PEAD $125k, x12.5 on the $10k ledgers). Built on archive branch
  `sleeve-capital-250k` (133 tests, mutation 60/60), under adversarial review. It merges only after a SHIP verdict; the
  resize trades at the next 14:30 CT run after the merge (target Mon 10-05).
- **Five equity studies pre-registered** (archive c3c8420, registry E16-E20):
  - `sspcs`: single-stock put spreads / CSPs;
  - `sswheel`: single-stock wheel and buy-write;
  - `dispersion`: SPY vs constituents;
  - `pead2`: the live PEAD re-tested on a PIT universe, with real earnings dates, a beta hedge and entry timing;
  - `strev`: short-term reversal.
  The `pead2` executor is running (daily-data cells first). The others queue on ThetaData behind `volxs`.
- **Fixed 10-02 (operator: "fix the timing issues"): the executor traded ledger bookings a session late.** The
  15:15 CT ledger job booked yesterday's events after that day's 14:30 CT executor run, so PEAD filled at the day+2
  close and trend three sessions after the signal.
  - New weekday task **`FEF-LedgerPreExec` at 08:45 CT** runs `fef-watchers/run_ledgers.ps1 -PreExec`: the pead + trend
    updaters only, with no record-fills and no broker contact.
  - The executor now trades PEAD at the day+1 close and trend at the S+2 close.
  - A same-day re-run leaves the ledgers byte-identical.
  - It was run by hand at 08:41 CT today, so **today's 14:30 run trades the trend month-end rebalance at the old 1:1
    size**: 12 MOC orders (SELL SPY, IWM, EFA, HYG, GLD and PEAD's NVS and DELL; BUY QQQ, EEM, USO, UUP and DBC).
    Monday's $250k change is then a pure resize. **It did not trade: TWS's API was down at 14:30; see the
    late-afternoon block. Monday trades the rebalance and the resize together.**
  - `fef-watchers` is not in git: backups of the edited `run_ledgers.ps1` / `install_tasks.ps1` are in the session
    scratchpad.

**2026-10-02 early — `ivsignal` RAN** (archive H40):
- **SKEW:** Branch E — the right sign, but underpowered (FM t 1.43).
- **VS:** Branch C, nothing.
- Neither is a sleeve.
- The shared single-stock option panel is complete (`D:\fef-data\xsopt\PANEL_DONE.txt`). The **`volxs` executor started ~04:10 CT.**
- It also found that the "8-hour ThetaData stall" of the night of 10-01 was the PC being put to sleep by another program;
  corrected in registry J.

**2026-10-01 evening — the `spypcs` verdict landed: Branch E (underpowered).** (archive H39)
- **Results:**
  - The operator's process run mechanically: primary W3 +$1.63 per chain (t +0.28; detection floor $14.42).
  - The live rule RE: +$5.18 (t +1.30).
  - About +0.7–3.4 %/yr on $10k at 1 lot, vs SPY +15.2 %/yr.
- **What hurts:**
  - The take-profit schedule COSTS money: holding to expiration day earns +$16.30 per chain (t +2.12).
  - Rolling COSTS money: a plain 2x stop beats it by $3.08; there was never a second roll.
  - It is a mid-fill strategy: the edge is about 1 cent of slippage per leg.
- **Sizing:** 5 lots on $10k would have been wiped out in March 2020. One chain's max loss under 10 % of equity needs
  about $48,900.
- **A post-hoc lead:** the S3b channel-touch entry with a plain stop, +$24.75 (t +3.81). It needs its own pre-registration
  and a forward ledger.
- **The runner keeps running on paper** (RE + 5 lots, merged tonight). The operator decides what to change.

**2026-10-01:** the **SPY put-spread runner went LIVE on paper** at the operator's request ("just start
paper today. we can look at the backtest later today or going into friday and this weekend") — ahead of
the `spypcs` study's verdict, whose data pull is 63 % done (it crawled after midnight on the ThetaData side
and pauses for market hours; it resumes 16:05 ET). Tasks FEF-SPYPCS-Tick / -Backstop / -Record registered
at 08:30 CT. **First entry FILLED 10:00 CT:** SPY 760.34, sold 744P / bought 734P exp 2026-10-12 (11 days),
|delta| 0.197, credit **1.05 at the mid on the first ladder step** (ThetaData mid 1.055, natural 1.03); chain
2026-10-01-1 **closed the same day by take-profit** (30 % target on the opening session): the 13:00 CT
try at 0.73 did not fill and was cancelled cleanly; 14:00 CT filled at debit **0.64** (mid 0.635), **+$38.20 net**
(+39 % of the credit). Reconciled by the 15:25 CT record task. One entry per ISO week, so the next is Tue 10/6
10:00 CT. When the verdict lands, compare it with what the runner
is doing; the operator decides whether it keeps running.

Also 2026-10-01: **`mtflevels` pre-registered** (archive E15, `mtflevels_LOCK.md`) at the operator's request. It
tests a profitable NQ trader's multi-timeframe structure marking (5m to 1D plus tick charts: swing points,
multi-touch zones, midpoints, fib 0.5 / 0.618 / 0.75 / 0.87, pivots, prior-session levels, round numbers) as a
mechanical marker on **QQQ**. Phase 0 asks whether price reacts differently at marked levels than at matched
placebo levels. Discovery uses the 2022-26 IBKR cache, so no ThetaData is needed during market hours;
confirmation uses a 2017-22 IBKR pull after 16:35 ET. Phase 1 trade rules (QQQ shares) are fixed in the lock
and run only on a Phase-0 pass, after review. **Discovery result (2022-26), 11:30 CT: no edge.**
- Price is rejected *less* at mechanically marked levels than at random nearby prices: -1.34 pp, t -2.79. Prior swing
  and prior-day extremes get run through.
- Breaks, reclaims and retests: nil.
- Fib ratios and the midpoint are not special.
- Midpoint entries: -0.02 R/trade, which is the simulator's own bias. The best lead, 5m break -> midpoint, is +0.05 R at
  t 1.7 and does not pass.
- The two independent implementations agree (99.7 % of events).
**FINAL: study CLOSED, no edge.** The 2017-22 confirmation repeats the negative rejection effect (-1.06 pp, t -2.33).
The 5m break -> midpoint lead did not replicate (-0.027 R).

At the operator's request, a display-only **"Levels" chart overlay** was built on branch `feature/structure-levels`:
- the engine and `GET /chart/structure_levels` match the study's own code on 3,378/3,378 levels;
- the suite gives 1,113 passed, plus one ThetaData-dependent parity test that failed only because ThetaData was busy with the
  spypcs pull;
- `tsc` is clean.
It awaits "terminal closed" to merge, rebuild and restart.

The operator also asked for two changes to the SPY spread runner (spypcs Amendment 9):
- **re-entry** on any flat Tue-Thu at 10:00 CT, with no weekly cap and still no overlap;
- **5 lots** with real partial-fill handling.
**Both are MERGED** (archive 26968e1, 2026-10-01 ~20:25 CT) after three adversarial review rounds: SHIP AFTER
FIXES, SHIP AFTER FIXES, then SHIP. There are 278 tests and the mutation check catches 114/114. A read-only probe of the real
paper TWS (client 179) timed the new position and execution reads at a few ms. The next entry is Tue 10/6 at 10:00 CT,
for 5 lots. TODO before Fri 11-27: an early-close-day retry tick. **USO approved** for the trend
sleeve, so the whole trend universe trades automatically.

**2026-09-30 in one paragraph:** the sleeve executor placed its first orders (10 MOC buys, all filled,
booked and certified that evening). Two adversarial reviews of its new "own book" logic found and fixed
real duplicate-order paths. The trend and carry paper ledgers turned out to have been frozen at
2026-09-09 by a hard-coded Yahoo end date; fixed. A standalone 0DTE put-credit-spread runner (SPX and
SPY side by side, paper) was built and reviewed, then left unscheduled when `pcs0dte` found its strategy
does not replicate. The terminal's daylight-time assumption (~55 places) was fixed and deployed. Five
studies were pre-registered: `pcs0dte` (closed, does not replicate), `ivsignal` (pulling), `volxs`,
`snflow2`, and in the evening **`spypcs`**, the operator's own SPY put-credit-spread process as a machine
rule (pulling). A paper runner for `spypcs` was built and reviewed (three rounds, SHIP) — live from
2026-10-01, above.

---

## 1. The operational state — what is live, and what has never fired

| component | state |
|---|---|
| **0DTE directional bot** | **Retired 2026-08-25.** Its data source (QuantData) was cancelled; `snapshot_to_inputs` was the only bridge into the FEF engine. Its premise was independently falsified: intraday direction on SPX is structurally foreclosed (achievable edge 0.08–0.35 index pts vs a 0.442 pt friction floor; the inverted signal performs identically at 49.2%). |
| **Credit-spread bot** (in the terminal; 0DTE put vertical, ~0.15Δ, 1 lot) | **OFF, and its order would be REJECTED.** Verified 2026-09-30 with an IBKR what-if (nothing transmitted): its combo form — legs SELL short / BUY long, order **SELL at a positive limit** — is refused by IBKR as a "riskless/guaranteed-loss combination". The accepted form is the same legs with order **BUY at a negative limit** (what-if margin +$200 on a $2 SPY spread). It also prices at the mid only, runs one symbol at a time (an SPX spread blocks a SPY one) and has no close-before-16:00 for SPY. **Superseded for paper by the put-spread runner below; do not turn it on.** Earlier history: 200 parents 08-12 → 08-25, zero fills (`Bag()` positional, env gate). |
| **Put-spread runner** (`fef-research-archive/options_executor.py`, standalone, client 178, orderRef `fef-pcs-*`) | **Built 2026-09-30; three adversarial review rounds → SHIP (archive b15f16e; 82 tests, 53/53 mutations). NOT scheduled: `pcs0dte` found its strategy does not replicate; running it is the operator's call (§4.3).** Its IBKR what-ifs passed 2026-09-30 evening (SPX/CBOE open +$2,500 initial margin, SPY and SPX close forms $0; nothing transmitted, probe client 179; `D:\fef-data\_probes\2026-09-30_ibkr_whatif\`), so if the operator wants it, `install_tasks.ps1 -Only FEF-PCS` registers its five tasks. After the first 10:00 entry, read its log: "FILLED … credit from the legs" confirms IBKR's combo reporting; "FILL SUSPECT" means that instance blocks itself until `resolve`. Paper 0DTE put credit spreads per `pcs0dte_LOCK.md`: `spx25` (SPXW 25-wide 0.15Δ, hold to cash settlement) and `spy2` (SPY $2-wide 0.15Δ, closed at 15:30 ET if spot < short × 1.003) side by side, entries 10:00/12:00/14:00 ET at the natural credit. Strikes chosen with the study's own code on a ThetaData snapshot. 36 tests, 26/26 mutations caught. Wrapper `fef-watchers\run_options_executor.ps1`. Purpose: execution truth (fills vs natural, fees, SPY assignment); `pcs0dte` is the P&L evidence. |
| **SPY put-spread runner** (`fef-research-archive/spypcs_executor.py`, standalone, client 180, orderRef `fef-spypcs-*`) | **LIVE on paper since 2026-10-01** (operator: "just start paper today", ahead of the study's verdict). Built 2026-09-30; three adversarial review rounds → SHIP (182 tests, 121/122 mutations); IBKR what-ifs incl. the 4-leg roll passed. Tasks FEF-SPYPCS-Tick (09:00–14:30 CT every 30 min), -Backstop (14:45), -Record (15:25) (§4.4b). **Never press the terminal's sweep-orphans / panic-flat / kill-all while it holds a spread** — the terminal lists its legs as unmanaged orphans. The operator's own process (7–14 DTE $10-wide SPY put spreads at the mid, take profit by sessions held, roll down and out at a 2× loss, Tue–Thu 10:00 CT entries), multi-day positions, one chain at a time. |
| **Sleeve executor** (trend v3 + PEAD → MOC orders, client 177, orderRef `fef-sleeve`) | **LIVE on paper since 2026-09-30.** First run: 10 MOC buys (SPY 2, IWM 4, EFA 15, EWJ 9, HYG 32, DBC 22, GLD 1; NVS 12, DELL 4, ARM 6), **all filled at the close and certified that evening**; its own book equals the account's stock positions. Trades only its OWN book (design rule 6); per-day certification — a later run can never certify an earlier day; `certify --date D --i-have-reconciled [--own SYM=N]` is the manual path. 93 tests, 49/49 mutations caught, three adversarial review rounds → **SHIP** (archive 9b5641c). **Daily task `FEF-SleeveExecute` (14:30 CT) ENABLED** — re-enabled 2026-09-30 after the review, confirmed by the operator the same day ("Keep it enabled"); first unattended run 2026-10-01. `record-fills` runs first in `FEF-DailyClose`. **The first large unattended rebalance is expected ~Fri 10-02**, once the trend ledger books the month-end it missed while frozen: look at `run_sleeve_executor.ps1 -PlanOnly` before 14:30 CT that day. Carry v1 disabled (de-allocate). |
| **Reconciliation harness** (`recon.py`) | **Built and tested; no measurement yet.** Zero credit-spread fills exist to reconcile. Two of its four Rule-3 join gates currently fail — the join is sound, the recorded anchors are contaminated — so it correctly publishes no aggregate. The adverse-selection prior stays at −$16.34/ct, CI [−156, +84]: unmeasured. Its advisory cross-check read `chainfill_cache.db`, now gone (§3); the primary path (live `signal_log.db` + ThetaData via `recon_quotes.db`) is intact. |
| **Collectors** | `runner_watch.py` and `caphead_watch.py` run from `D:\fef-data` (restarted there 2026-09-26, detached). Do not kill them during market hours. `FEF-DailyOpen` restarts them at 08:30 CT if they are down. Find them with `Get-CimInstance Win32_Process` filtered on the script name; PIDs change. |
| **Paper ledgers** | Trend v3, PEAD, overnight, carry v1, caphead grading and runner outcomes update daily via `FEF-DailyClose` from `D:\fef-data`, mirrored to the archive. **The carry ledger failed silently on 16 runs, 2026-09-03 → 09-25** (§3); repaired 2026-09-26. **And the trend and carry books were FROZEN at 2026-09-09 from 09-10 to 09-30**: both asked Yahoo for `period2=1789000000`, a "beyond today" constant that is 2026-09-09 20:26 ET, while every run logged "ok". Fixed 2026-09-30 (archive 4c41f41): dynamic end date, and each ledger now exits 3 when its latest close is > 5 days old. The trend book the sleeve executor reads refreshes from the 10-01 run. |

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

**Pre-registered 2026-09-30** (operator: "run this on SPY for the put credit spreads… have it fill both";
"I want you to pre-register all three of those tests. Especially since I have to stock data now"):

| study | what | state |
|---|---|---|
| `pcs0dte` (E10) | 0DTE put credit spreads, SPX vs SPY, 2016–2026: does the corrected `tenorsell` cell (+$19.63/ct, t 1.27) replicate on unseen sessions; SPX vs SPY per $1,000 of width; does it help the trend + PEAD book | **Closed 2026-09-30: does NOT replicate** (Branch D formally — the reproduction check missed by $0.22 because `tenorsell` settled on the last 1-min bar, not the official close; substantively Branch C). SPX 25-wide 0.15Δ at bid/ask **−$6.95/session** (t −0.83, 1,928 sessions); on the 1,362 sessions `tenorsell` never saw **−$16.42** (t −1.58); worsens the book (Sharpe 1.73 → 1.38 OOS). SPX beats SPY (t +2.78). A paper run needs 670–1,530 sessions to rank them. Registry H38. |
| `ivsignal` (E11) | Do single-stock option prices (smirk, call−put IV spread) predict next-month S&P 500 returns beyond past returns, as a monthly share sleeve after costs? Builds the shared monthly option panel `D:\fef-data\xsopt\` | **Pulling** (stock quotes done; EOD and the 59,450 monthly chains in progress, outside 09:25–16:05 ET only; `PANEL_DONE.txt` marks the end) |
| `volxs` (E12) | Rank single-stock volatility (HV − IV; IV term slope): long cheap straddles / short rich iron flies at real bid/ask | **Queued** for `ivsignal`'s panel |
| `snflow2` (E13) | Single-name option flow INTRADAY (TSLA/NVDA vs SMH/QQQ) on the real stock tape; Stage 2 of the July track, whose kill gates bind | **Queued**, runs last |
| `spypcs` (E14) | **The operator's own process:** SPY put credit spreads 7–14 DTE, $10 wide, short at the largest \|Δ\| in 0.15–0.20, filled at the MID, take profit by sessions held (30/40/50 %), roll down and out for a credit at a 2× loss (max 3 rolls), closed on expiration day; entries Tue–Thu only, at 10:00 CT (operator amendments 1–2); weekly (W3, primary) plus signal arms (down 1 %, VIX spike, 21-EMA touch, channel touch) against random timing; 2017-01 → 2026-09 | **Pulling** (GO 2026-09-30 18:22 CT; one `expiration=*` chain per session, ~10 h at one request at a time). Executor amendments 3–6 record the request shape, the constructions, a ThetaData endpoint that silently drops contracts (detected against the contract list and patched), and a second random-timing control that G6 must also pass |

**ThetaData now includes the STOCK tier** (bought 2026-09-30; the terminal had to be restarted to see it).
There is no index tier and none is needed (the SPX level comes with option greeks). Etiquette for every
study: at most 2 concurrent requests, **no bulk pulls 09:25–16:05 ET** (the live flow feed shares the
terminal), and a `PULL_DONE.txt` marker hands the terminal to the next study.

**Resolved 2026-09-30:** the trend v3 sleeve keeps GLD, DBC and HYG (operator: "I want all of those also
added today alongside the stocks sleeves").

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

1. ~~**Deploy the daylight-saving fix before Monday 2026-11-02.**~~ **DONE 2026-09-30** (operator:
   "terminal closed, go ahead and install the DST fix"). The sidecar hard-coded ET as UTC−4 in ~55 places
   (market-hours window, VWAP anchor, session dates, pause windows, expiry resolution); after clocks fall
   back the loop would have run 08:30–15:00 real ET. Every site now goes through one zoneinfo helper,
   `fef_sidecar/et_time.py`; `tzdata` is declared in `pyproject.toml`. Full suite **1072 passed** on the
   live checkout (+117 tests pinning both transitions); zero behaviour change on every daylight-time
   minute of 2026; adversarial review SHIP. **Rule from here: never write a fixed UTC offset for ET/CT —
   use `et_time`.** Runbook and patch kept in `D:\fef-data\_deploy\2026-10_dst_fix\`. Known leftovers
   with no trading effect are listed in its `DEPLOY.md` (the frontend's `ReplayPanel.todayET()` needs an
   app rebuild; replay default date only; `pattern_overlay` 4h buckets matter only if pattern triggers
   are armed). Watch the first sessions after Sun 11-01: the loop should run 09:30–16:00 ET (14:30–21:00Z).
2. **Sleeve executor: running daily** (review SHIP; re-enabled 2026-09-30 and confirmed by the operator).
   It trades at 14:30 CT; `record-fills` books the fills at 15:15 CT. Watch the first run after the trend
   book refreshes (the missed month-end rebalance books on 10-01/10-02 — check `-PlanOnly` first). A day
   whose fills it cannot see exits 4 and needs `certify`.
3. **Put-spread runner: NOT scheduled — the operator decides.** Its review passed, but `pcs0dte` says the
   strategy it trades does not replicate (above). Running it on paper would only show execution
   (fills vs the natural, fees, SPY assignment), at a paper loss; it cannot measure profit. If the operator
   still wants it: `install_tasks.ps1 -Only FEF-PCS` (its IBKR what-ifs passed 2026-09-30 evening).
4. **Research queue** (ThetaData-sequenced): ~~`pcs0dte`~~ (closed) → `ivsignal` (pulling) and `spypcs`
   (pulling, one request at a time until `ivsignal`'s panel is done) → `volxs` → `snflow2`.
4b. **`spypcs` paper runner: LIVE on paper since 2026-10-01** (operator's call, ahead of the study's verdict;
   see the top). `fef-research-archive/spypcs_executor.py` (client
   180, orderRef `fef-spypcs-<chain>-<event>-<step>`) runs the study's primary cell (W3 × MID × roll) on the
   paper account: a 30-minute `tick` (manage, then the 10:00 CT entry), a 14:45 CT backstop for an
   expiration-day close, and an after-close `record`. Every order starts at the mid and steps a cent at a
   time toward the natural with confirmed cancels, so `spypcs_fills.csv` is the fill-vs-mid record the
   backtest cannot give. **Three adversarial review rounds (3 blockers → 0 → 0; every finding fixed and
   pinned by a test): 181 tests, 121/122 mutations caught (the one survivor is an equivalent backup).**
   Wrapper `fef-watchers\run_spypcs_executor.ps1` (a Windows toast on URGENT / BLOCKED / crash); its three
   tasks are in `install_tasks.ps1` as OPT-IN (a plain run never registers a trading runner; `-Only
   FEF-SPYPCS` does). IBKR what-ifs passed 2026-09-30 evening (nothing transmitted, probe client 179): the
   $10 open (+$1,000 initial margin, a credit spread), the close ($0) and **the 4-leg roll, accepted at a
   net −0.01 and at 0.00** (`D:\fef-data\_probes\2026-09-30_ibkr_whatif\`). Registered 2026-10-01 08:30 CT
   with `install_tasks.ps1 -Only FEF-SPYPCS`; `-Remove -Only FEF-SPYPCS` stops it. Its wrapper starts
   ThetaTerminal if it is not serving. Read its logs in `fef-watchers\logs\spypcs_exec.*.log`; its record is
   `spypcs_ledger.csv` (chains) and `spypcs_fills.csv` (every order vs the mid).
5. ~~**Run what exists.**~~ **Done 2026-09-30** for the sleeves; see §1. ~~**Move the research caches
   out of Temp.**~~ **Done 2026-09-26** (§3).
6. **The one cheap study that needs no new data:** the leverage question from `sizing` — the book
   runs at mean gross **0.753** and hits its 1.0 cap on **27 %** of rebalances. Standalone lock,
   directly changes the book that would be traded.
7. **Then breadth, or stop.** Every research thread since `powercal` has ended at the same wall:
   the control stack certifies 13.4 %/yr on 289 names, ~4 % on 3,000, and three instruments cannot
   see a forty-market effect. Within the equities-only scope, breadth means many stocks, not many
   asset classes: single-stock options events (`earnvol`) are the first study built that way. **Do
   not run another single-instrument intraday study — we have measured that it cannot resolve the
   answer either way.** New data goes to `D:\fef-data`, never the agent scratchpad. *(Tension, stated:
   `snflow2` is intraday on two names. It was pre-registered at the operator's explicit request, is
   event-based rather than a timing rule on one index, keeps the July kill gates, and has an
   UNDERPOWERED branch — read its detection floor before its verdict.)*

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
- **Open review items on the credit-spread path** (from the 2026-08-25 adversarial review): narrow the
  `try` in `live_trading`, TP sentinel on a crossed pair, `confirm=True` actor mislabel, half-day
  sessions, `/panic/flat` does not disarm the spread bot, scan step coupled to spread width. **Plus,
  2026-09-30: the combo is built the form IBKR rejects (SELL at a positive limit; see §1)**, a resting
  unfilled SELL combo is marked CLOSED by the phantom sweep within 300 s, and the fill backfill sums
  both legs of a combo as if they were one. Do not revive this bot without fixing all three.
- **Operator rule while the put-spread runner holds spreads (2026-09-30): do NOT use the terminal's
  kill / `/panic/flat` / `/admin/sweep_orphans`** — they market-close every option position in the
  account, including the runner's legs (a sold long leg leaves the short put naked). The sidecar will
  log the runner's legs as "ORPHAN POSITIONS … call /admin/sweep_orphans" every 300 s: expected, ignore.
  The operator places manual trades in TWS, never through the terminal's ticket, so no sidecar parent
  can adopt the runner's legs. A sidecar-side protection of the runner's legs is a later item.
- **Two order-placing programs live OUTSIDE the terminal**, in the archive, run by Task Scheduler:
  `sleeve_executor.py` (client 177) and `options_executor.py` (client 178). Same review rules as the
  live path. Each trades only what it placed itself (its own client id / orderRef); never "fix" either
  by editing its state file — use its `certify` / `status` commands.
- **Never write a fixed UTC offset for ET or CT** (use `zoneinfo`), and **never a fixed "far-future"
  epoch** for a data request's end date (the 2026-09-09 ledger freeze).
- **The next research lock must state the entry-bar stop convention.** The `vbreak` implementation
  diff found +201 gross R over 54 cells riding on it (99 of 123 affected trades opened beyond the
  stop; one implementation books −1 R, the other rides to +5…+8.5 R).
