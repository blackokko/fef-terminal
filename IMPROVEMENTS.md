# Improvements & Configurability Notes

Running list of things to discuss/iterate on when you're back. Organized by
category. Each entry has: **what**, **why it matters**, **proposed fix**.

---

## Latency / real-time

### 1. Tick stream vs FEF cadence — separate them
**What:** Currently the FEF loop ticks at a fixed interval (default 120s). Chart
candles, price banner, and sidebar wall positions all wait for the same loop.

**Why it matters:** You wanted everything as real-time as possible. The chart
and sidebar can absolutely refresh sub-second without changing FEF cadence.

**Proposed fix:**
- Tick stream → `reqMktData` (IBKR tick-by-tick last/bid/ask) → broadcast
  TickEvents at native cadence (~10-50ms per print)
- Bar stream → keep as 2-min for the chart (or drop to `reqRealTimeBars` 5s
  bars if you want a more responsive chart)
- QuantData poll → independent loop at 15-30s interval for wall updates
- FEF iter → stays on its own cadence (2 min default, configurable)

### 2. Make FEF interval configurable
**What:** `FEF_LOOP_INTERVAL_SEC` env var already exists. But thresholds in
the FEF model (alignment magnitudes, FE ratio cutoffs) were calibrated for
2-min deltas.

**Why it matters:** If you run FEF at 30s, ΔPrice will be much smaller and
the current `abs(delta_price) >= 5` HIGH-confidence threshold will rarely
trigger. Signal quality degrades.

**Proposed fix:** Two options —
- (a) Auto-scale thresholds by interval (interval=30s → divide thresholds by 4)
- (b) Keep 2-min FEF, expose a `/run_iter_now` button in the UI for manual
  off-cadence checks (already implemented in REST API)

I'd recommend (b) — gives you control without breaking the model's calibration.

### 3. QuantData refresh rate
**What:** We fetch the full snapshot (~7 endpoint calls concurrently) per
FEF iter. Each round-trip is ~200-500ms.

**Why it matters:** If you want walls/drift to update in the sidebar at
15-30s cadence, that's separate from FEF and should run independently.

**Proposed fix:** Add a `quantdata_poll_loop` that runs at its own interval
and only refreshes the sidebar (not FEF). Cache the latest snapshot for FEF
to consume.

---

## FEF model

### 4. Strike + targets in SPX-price-space, not option-price-space
**What:** Currently `strike_rec`, `t1`/`t2`/`t3`, `stop` are SPX index
levels. You actually trade options whose pricing is non-linear in SPX.

**Why it matters:** The mental conversion from "SPX at $7,440 = my $7,450C
worth ~$X" is friction during fast trades.

**Proposed fix:** Once IBKR option chain is wired (v1.5), also output option
limit prices for entry + targets. Pull from `reqMktData` on the specific
contract.

### 5. Position sizing in the verdict
**What:** Verdict says "LONG_CALLS" but doesn't suggest qty.

**Why it matters:** You consistently size at 10-20 contracts. Could default
to 15 (your typical) and adjust by confidence: HIGH=15, MEDIUM=10, LOW=5.

**Proposed fix:** Add `suggested_qty` field to FEFVerdict, derived from
confidence + recent loss streak (back off after losses).

### 6. Wall-anchored T1/T2/T3
**What:** Currently T1 = spot + 10, T2 = next wall, T3 = NTM MP or
spot + 35. This is OK but rough.

**Why it matters:** Real exits cluster around wall levels (where price
struggles). Better target placement = better fills.

**Proposed fix:** T1 = nearest minor wall, T2 = next dominant GEX wall,
T3 = NTM MP. Already partially done — could be tightened.

### 7. Wall collapse needs absolute floor too
**What:** Wall collapse triggers if any wall lost >20% in 1 iter. But a
$50M → $40M drop is noise; a $50M → $5M drop is meaningful.

**Why it matters:** False collapse signals could fire on minor walls.

**Proposed fix:** Require both >20% loss AND |prior_net_m| >= some
threshold (e.g., $5M) to filter noise.

---

## Data quality / observability

### 8. Snapshot freshness indicator
**What:** Each market snapshot has `fetched_at_ms` but we don't surface
how stale the QuantData is (server-side refresh interval is unknown).

**Why it matters:** If QuantData server hiccups and serves a 30s-old
snapshot, FEF will run on stale data without knowing.

**Proposed fix:** Track and display the QuantData server timestamp if
available in the response. Flag in UI when staleness > 5s.

### 9. Engine state introspection
**What:** Can't easily see what's in the engine's 3-iter buffer from
outside.

**Why it matters:** Debugging "why did FEF fire/not fire" requires the
buffer state.

**Proposed fix:** Add `GET /engine/state` REST endpoint returning the
current buffer. Display in a dev panel in the UI.

### 10. Per-iter performance metrics
**What:** Don't currently track how long each iter takes (QuantData
fetch + FEF compute + DB write + broadcast).

**Why it matters:** If iters start taking >1s, that's a problem worth
diagnosing.

**Proposed fix:** Add timing to each phase, log + expose via `/status`.

---

## Provider / connection

### 11. IBKR reconnect on TWS restart
**What:** Live provider has retry-on-connect but if TWS dies mid-stream,
the bar stream loop just logs + sleeps 5s. Not bulletproof.

**Why it matters:** TWS restarts (forced updates, crashes) happen.
Currently you'd lose the bar stream silently.

**Proposed fix:** Add health-check ping every 30s; on failure, force full
reconnect and re-subscribe. Surface connection state via WS StatusEvent.

### 12. QuantData token refresh
**What:** We load auth token at startup. No idea what the token's TTL is.

**Why it matters:** If the token expires mid-session, all calls fail.

**Proposed fix:** Detect 401 → trigger re-auth flow (or at minimum log
loudly and emit a status event so UI can prompt).

### 13. Default to live IBKR + mock QuantData (or vice versa)
**What:** Both default to mock right now. For dev convenience but easy
to forget to switch in production.

**Why it matters:** Could accidentally trade on mock signals.

**Proposed fix:** Startup banner showing which providers are active.
Refuse to start in "all mock" unless `FEF_ALLOW_FULL_MOCK=true`.

---

## Order routing (v2 scope)

### 14. Multi-leg orders for PCS / spreads
**What:** Plan is single-leg long calls/puts for v1. You also run PCS.

**Why it matters:** PCS management would benefit from being in the same UI.

**Proposed fix:** v2 — add spread support (vertical, iron condor, etc.) with
multi-leg ticket generation.

### 15. Order pre-flight checks
**What:** Before sending to IBKR, no validation of: buying power, margin,
duplicate order, etc.

**Why it matters:** Easy to fat-finger qty or strike.

**Proposed fix:** v2 — pre-flight modal: "BUY 15× SPX 7450C @ $7.84 LMT —
cost $11,760, BP after = $X, confirm?"

### 16. Take-profit ladder auto-place
**What:** v1 generates the ticket but you manually scale out at T1/T2/T3.

**Why it matters:** Missing T1 on fast moves is a real risk.

**Proposed fix:** v2 — on fill, auto-place 3 child limit orders at T1/T2/T3
with appropriate sizes (e.g., 5/5/5 of 15-contract position).

---

## Frontend / UX

### 17. Verdict pulse + sound
**What:** When FEF fires LONG_CALLS / LONG_PUTS HIGH, the verdict panel
just updates silently.

**Why it matters:** If you're looking away from the screen, you miss it.

**Proposed fix:** Audible chime + window flash on HIGH-confidence fires.
System notification on actionable signals.

### 18. Multi-monitor layout
**What:** Single-window app design.

**Why it matters:** You probably have 2-3 monitors. Could pop the chart
into a separate window.

**Proposed fix:** v1.5 — Tauri secondary windows for chart-only view.

### 19. Replay mode
**What:** No way to replay past sessions to study what FEF would have done.

**Why it matters:** The signal log is rich (every iter persisted). Could
build a "replay 5/15" view that walks through iters with the FEF state
at each point.

**Proposed fix:** v1.5 — `/history/replay/{date}` endpoint + UI panel.

### 20. Auto-tagging FEF iters with outcome
**What:** When you take a trade and close it, the SignalIter that fired
it doesn't know the outcome — only the TradeTicket does.

**Why it matters:** "What % of HIGH-confidence LONG_CALLS fires win?"
requires joining tickets back to signals.

**Proposed fix:** Already half-wired (signal_iter_id on TradeTicket).
Just need the UI to surface "win rate by confidence" stats.

---

## Operational

### 21. Pre-market warmup
**What:** App starts cold each morning — no buffer history.

**Why it matters:** First 3 iters after market open are warmup-WAIT.
You miss the open's first ~6 minutes of signals.

**Proposed fix:** At startup before 9:30 ET, pre-load QuantData snapshots
for the previous 6 mins to seed the FEF buffer.

### 22. Daily session reset detection
**What:** Engine doesn't reset at market open. If app runs through close
into next morning, state carries over.

**Why it matters:** Stale wall data from yesterday will misinform today's
first iters.

**Proposed fix:** On detecting a new session_date, call `engine.reset()`
and clear cached snapshots.

### 23. Backup / export
**What:** SQLite file lives at `~/.fef-terminal/signal_log.db`. No backups.

**Why it matters:** Months of signal data is valuable for analysis.

**Proposed fix:** Daily SQLite dump to JSON + optional cloud sync (S3, Dropbox).

### 24. Settings panel in UI
**What:** All configuration is via env vars. Not discoverable.

**Why it matters:** Changing FEF interval requires editing a .env file
and restarting.

**Proposed fix:** Settings panel with sliders for: FEF interval, QuantData
poll interval, wall proximity threshold, confidence thresholds. Hot-reload.
