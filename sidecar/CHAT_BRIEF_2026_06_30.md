# Chat brief — flow-surge edge: reframed, decisions made, pressure-test requested (2026-06-30)

**What I want from you:** blind-spot / framing review on (1) whether we're rationalizing past a kill criterion, (2) whether the monetization number is too-good / noise-mined, and (3) whether the cross-source non-equivalence is fatal. The methodology stack (scripts + data below) is the arbiter — challenge the framing, not just the numbers.

---

## 0. One-paragraph context
We mined real paper trades and found a directional edge: a **top-decile aggressive options flow surge leads SPX price**, direction = surge sign. Earlier this was framed as "FIRST validated directional edge, ready to ship." This session we (a) fixed how it's monetized, (b) tested whether it transfers from the research data source (ThetaData) to the live engine's source (QuantData), and (c) re-validated it directly on QuantData. The honest result is **more equivocal than the earlier framing**, and we made a judgment call to proceed anyway. That call is what I most want you to pressure-test.

## 1. The edge as we now understand it (the "new edge")
- **Entry gate:** a flow surge = `|ΔDrift|` (3-iter delta of cumulative signed-premium flow) in the **top decile (p90) of the recent rolling distribution**, computed on *each data source's own scale* (adaptive, not a fixed threshold).
- **Direction:** sign of the surge.
- **Exit:** **fast and edge-sized.** The favorable move is only ~+1pt and (on live data) decays/reverses within ~30 min, so we exit tight — `be_ladder` R=1pt (T1 +1.5 / T2 +2.5 / T3 +5pt, stop −1pt) or a watcher-managed fixed-$ ladder.
- **Honest one-liner:** a *thin (~+1pt), fast (~10-min, mean-reverting) intraday directional lean from aggressive flow surges, traded with tight exits.* This is a meaningful **downgrade** from the earlier "robust, all-horizon, every-year" framing.

## 2. The three results (hard numbers, scripts cited)

### 2a. Monetization FIX — real, but flagged as possibly too-good (`flow_monetize_test.py`)
The earlier "regime-contingent P&L" was an exit-scaling artifact: fixed-$ targets ($300/500/1000) need ~+2-3pt but the edge is ~+1pt. Re-running by-year with an **edge-sized exit**:
- Best = `be_ladder` **R=1.0pt**. Baseline fixed-$ → R=1pt by dev year: 2022 −10.98→**+2.19**, 2023 −9.68→**−0.46**, 2024 −5.26→**+11.91**, 2025 +14.06→+9.60. Dev ALL −4.42 → **+5.58/ct (t=1.69)**.
- **Holdout (touched once): +$36.86/ct, t=8.09, win 54.5%** vs the fixed-$ holdout +$24.95.
- **IV-conditioning is NOT the lever** (it flatters the pooled mean by concentrating on good years; dropped).
- **🚩 Caveats I want you to weigh:** (i) R=1pt stop = 1 SPX point is *inside the noise of the parity-reconstructed spot* (median of 5 near-money K+C−P @1-min); **59% of exits are stops**, and smaller R monetizes monotonically better — which is consistent with *both* "edge is small" *and* "small-R is mining spot-reconstruction noise." Can't fully disentangle without true-tick spot. (ii) 2022/23 are not statistically positive. (iii) Holdout has no 2022-style bear regime. (iv) t-stats optimistic (session-clustered overlapping entries).
- **Q for you:** is +$36.86/t=8.09 a real exit fix, or are we noise-mining a sub-point R against a reconstructed spot? What's the cleanest test to kill that hypothesis without buying tick data?

### 2b. Source-equivalence FAILED (`source_equivalence.py`)
Edge validated on ThetaData `net_signed_prem`; live engine fires on QuantData `drift_cum_net_m`. They are **not equivalent** on 9 overlapping sessions (2 corrupted):
- Surge series correlate only **Spearman ~0.40**; **direction disagrees ~25-34%** at surge points.
- The old 25M gate (ThetaData-scale) **~never fires on QuantData** (live `|ΔDrift|` p90≈2.67M / p99≈15M — ~6.5× scale gap). *Fixed* by the adaptive p90 gate (self-calibrates per source). But that fixes scale, **not** the direction disagreement.

### 2c. Direct re-validation on QuantData's OWN surges — UNDERPOWERED/INCONCLUSIVE (`directional_validate_quantdata.py`)
7 clean sessions, all one **calm-bull fortnight** (2026-05-18..27), ~150 surges/horizon, session-clustered stats (N_eff=7):
- **+10min:** hit **57.0%** (t=2.21 vs 50%, p≈0.07 *borderline*); favorable **+0.67pt (t=1.25, NOT sig, below the +1pt benchmark)**. Controls line up: favorable +0.67 > opposite −0.67 > random −0.56 → surge sign carries *some* short-horizon info.
- **+30min / EOD:** sign **reverses negative** (−1.69pt / −1.93pt).
- **Benchmark it's compared to:** ThetaData gave ~54-55% hit, +1pt favorable **every year**, large t over **1093 sessions**.
- **Verdict:** cannot confirm (the +1pt magnitude isn't reproduced at significance), cannot cleanly deny (right-sign, controls-beating, borderline-57% at +10min). Need ~20-30 clean sessions across bear/chop/bull to resolve.

## 3. The decision we made (please pressure-test)
The user's call, which I implemented: **proceed on the percentile-normalized adaptive gate as the live approach** — trust the 1093-session ThetaData validation; treat the live QuantData shortfall as *incomplete data, not disproof* (the volume is genuine, just smaller-scale; 2 sessions are corrupted captures). We **deprioritized** a multi-week data-pipeline fix in favor of running the (now glitch-guarded, correctly-scaled) gate forward to accumulate its own track record.

**The blind-spot question I most want challenged:** there is a standing **hard kill criterion** from the 2026-05-27 pivot ("after direction+exit fix, 10 v2 sessions, if DSR@N_eff<0.30 OR real P&L negative → ABANDON"). We now *have* a direction (flow surge) + an exit (edge-sized), and the live evidence is underpowered/weak with a +30min sign reversal. **Are we moving the goalposts / rationalizing past that kill criterion by reframing "didn't replicate" as "underpowered"?** Or is "large-sample cross-regime validation + a thin/fast live signal + a known small live sample" a legitimate reason to keep it alive and gather more data? Where's the line, and what pre-committed stopping rule should we set *now* so we don't rationalize later?

Secondary framing Qs:
- Does the **+30min sign reversal** + the **cross-source direction disagreement** smell more like "real signal, broken cash↔source mapping" or "no robust directional edge, just short-horizon autocorrelation we're dressing up"?
- If we proceed: what's the minimum pre-registered success bar on live paper (e.g. N sessions, hit-rate CI lower bound, real net P&L sign) that would convert "proceed-and-watch" into "validated" vs "abandon"?

## 4. Engineering shipped this session (plumbing — not the point, but for completeness)
Adaptive p90 gate + **glitch guard** (rejects corrupted ΔDrift like QuantData's −2839M before it false-fires/poisons the percentile); watcher is now **dual-mode** (spx_points in point-terms AND fixed_dollar in $-terms, removing a buggy $→option-price translation that turned −$500 into a −$1640 stop); strategy-aware position reconcile; continuous fill-sync. 148 sidecar tests pass. All deploys on restart, **needs paper verification** (couldn't test vs live IBKR).

## 5. Reproduce (read-only on the DB)
From `sidecar/`, `./.venv/Scripts/python.exe -m scripts.research.backtest.<name>`:
- `flow_monetize_test` — by-year monetization + the R-sweep + IV test.
- `source_equivalence` — ThetaData vs QuantData equivalence.
- `directional_validate_quantdata` — direct QuantData directional re-validation.
- `confluence_test` — the original surge baseline + confluence factors.
Key tables: `option_flow_minute` (ThetaData signed flow, 2022-2026, 1093 sessions), `signal_iter` (live QuantData drift + spx, 11 sessions), `option_quote_history` (0DTE NBBO). Holdout = 2025-08-13..2026-06-26 (touched once).
