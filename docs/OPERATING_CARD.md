# FEF Operating Card — deployed 2026-07-02

The whole machine on one page. If a panel isn't named here, it doesn't affect trading.

## The one pipeline that trades

```
Scanner (SPX/SPY/QQQ, 60s, strict flow-surge ALIGNED — the canonical signaler)
  → 250M drift garbage ceiling            (vendor-glitch guard)
  → CHOP GATE      skip if trailing-30m range < 33rd TOD-percentile   [sweep: compressed fires −$25..−$48/ct]
  → BIAS VETO      skip if engine's own flow-bias disagrees           [3 of 5 losers on 7/2; −$17 vs −$1.85/ct at scale]
  → auto-trader stack (300s/symbol cooldown, per-symbol cap, qty 3, calendar pause, dup-lock)
  → nearest-ATM strike (all symbols — ETF strikes fixed 7/2)
  → dollar ladder: SPX 300/500/1000/−400 · ETFs auto-scaled (~1/10 at SPY) · runner-protect
```
Every skipped fire is persisted with its counterfactual (`skipped_by`, scored outcome) — the gates grade themselves live.

## Advisory only (never trades)
- **Impulse flag** — ≥6pt move + flow agrees + non-compressed → alert (the "why didn't it enter?" answerer)
- **BE-ladder shadow** — every trade logs "SHADOW BE-ladder would have made $X vs real $Y" (exit A/B, live)
- Pattern overlay (PAT toggle), main-engine "would be ALIGNED" lines, phase detector, wall context stamps

## The four knobs (POST /config, hot-reload — everything else is not a knob)
1. `chop_gate_pct_threshold` (33.3) / `chop_gate_enabled`
2. `bias_veto_enabled` (on)
3. Exit style — fixed_dollar live; BE-ladder shadow accumulating the evidence to switch (+$11/ct historical)
4. Cooldown / per-symbol cap (300s / default 2)

## The two lines (pre-committed 2026-07-02 — judged on $/contract from broker fills at ~50 SPX fires)
- **Kill: ≤ $0/ct** → the signal retires, search moves offline
- **Redemption: ≥ +$40/ct** → the backtest missed a live-specific mechanism; we dig

Scoreboard basis: `ibkr_execution` fills attributed by parent + source stamp. Never watcher-line dollars, never day P&L.

## Closed doors (13 direction nulls, 2022–2026, real fills, OOS — don't re-ask without new data)
flow-direction (both vendors) · EMA/trend · candle patterns (55 parameterizations) · price-primary impulse ·
cross-symbol confluence · reaction latency (dead at +0s) · magnitude-gated continuation · drift-magnitude floors
(era artifact AND noise-floor variant) · wall-proximity gating (subsumed by chop) · pullback limit entries
(adverse selection) · wider stops (real reversals) · TP1 lowering (widens the gap) · vendor-agreement (lookahead)

## Validated (build on these)
- **Magnitude** ranks |move| ~2.7× OOS (VIX/RVOL/post-cap) → attention flags + exit sizing
- **Exit asymmetry** is real at scale: wins ~$1,050 vs losses ~$550 (2.2×); the ladder beats naive exits by ~$11/ct
- **Chop + bias gates** (harm reduction, OOS-backed)
- **Put-fade** mean reversion (every year, both rulers) — monetizes only as SHORT premium → Dale's system's lane

## Data notes
- Live drift = QuantData 0DTE net-drift. Garbage ceiling 250M. Noise: vendors disagree ~2M/min median —
  thin fires are unconfirmable as real flow (but a 5M floor tested WORSE; don't add one).
- ThetaData = research ground truth (the tape). 6/30 is excluded from QD-based research (permanent garbage minute).
- `flow_series_point` understates flow (persists pre-revision minutes) — don't validate against it.
- 7/1–7/2 live-captured 1-min bars carry artifacts (2-min cadence + newborn-bar OHLC); capture fixed 7/2.
