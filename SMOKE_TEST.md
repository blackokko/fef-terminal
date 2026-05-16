# v1 Smoke Test Checklist

Run this the first time you connect to live data during market hours.
The goal: catch any divergences between the live data path and the
mocked one we developed against. Each item is small enough to verify
in under a minute.

## Pre-flight (do these BEFORE 9:30 ET)

- [ ] **TWS or IB Gateway running** with API enabled (port 7497 for paper,
      7496 for live), trusted IP `127.0.0.1`
- [ ] **QuantData config exists** at `%USERPROFILE%\.quantdata-mcp\config.json`
      with valid `auth_token` and `instance_id`
- [ ] **Python sidecar boots clean** in mock mode:
      ```powershell
      cd sidecar; .venv\Scripts\activate; fef-sidecar
      ```
      Should print `fef-sidecar ready (QD=MockQuantDataClient, IBKR=MockIBKRProvider, auto_loops=True)`
- [ ] **Tauri dev app opens** with the chart, sidebar, and a green WS dot
      in the top right
- [ ] **All 27 unit tests pass**:
      ```powershell
      cd sidecar; .venv\Scripts\activate; pytest
      ```

## Switch to live providers

Stop both processes. Then:
```powershell
.\scripts\start.ps1 -QuantDataMode live -IbkrMode live
```

Verify in the sidecar console:
- [ ] `IBKR connected: host=127.0.0.1 port=7497 client_id=1` — confirms TWS reachable
- [ ] No `QuantDataNotConfiguredError` — config file readable

## During market hours (9:30 - 16:00 ET)

### Backend signals
- [ ] **First TickEvent within 10 seconds** of bar-stream starting
  (visible in sidecar console as `Iter X — SPX $Y.YY — DIRECTION`)
- [ ] **Chart shows live SPX candles** updating as bars close
- [ ] **First IterEvent within 2 minutes** of FEF loop starting
  (visible in QuantData panel: walls populate, drift number shows
  current cumulative)
- [ ] **Top bar SPX price updates in real time** (no stale value > 5s old)

### Wall overlay
- [ ] **Horizontal lines drawn at major GEX/DEX strikes**
- [ ] **Combined labels at same strike** (per [[signal-bridge-labels]] memory)
  — e.g., "GEX +X | DEX +Y" on one line, not two stacked
- [ ] **NTM max pain dashed amber line** distinct from walls
- [ ] **Walls update each iter** — old lines removed, new ones drawn

### FEF verdict panel
- [ ] **Direction badge color-coded** (green for LONG_CALLS, red for LONG_PUTS,
      muted for WAIT)
- [ ] **Confidence label visible** (HIGH/MEDIUM/LOW/NONE)
- [ ] **Reasoning text matches the rule that fired** — e.g., "ALIGNED
      LONG_CALLS — ΔPrice +X, ΔDrift +Y, FE Z"
- [ ] **Delta metrics populated** after 3 warm-up iters

### Order ticket
- [ ] **Strike rec is reasonable** — 25-50pts OTM in trade direction,
      rounded to nearest 5
- [ ] **T1/T2/T3 staged correctly** — T1 closer than T2 closer than T3
- [ ] **Stop is opposite direction** of T1
- [ ] **Qty defaults to 15** (matches [[user-trading-style]])
- [ ] **Copy button** writes formatted text like `BUY 15 SPX MAY15'26 7450 CALL @ LMT`

### Signal log
- [ ] **Polls every 15s + on each iter** — list grows as new iters land
- [ ] **Color coding matches verdict** (LONG_CALLS green, etc.)

## Edge cases to verify

- [ ] **Wall proximity skip fires** when SPX gets within 5pts of mega
      call wall (e.g., $7,400-area): verdict should show "WAIT —
      WALL PROXIMITY SKIP" with reasoning naming the wall
- [ ] **Manual /run_iter_now works** off-cadence — POST to
      `http://127.0.0.1:8765/run_iter_now`, verify a new IterEvent
      broadcasts and a SignalIter row lands in the log
- [ ] **WS reconnects** after sidecar restart — kill the sidecar
      console, restart it, the frontend's WS dot goes amber → green
      within 4 seconds without app reload

## Known limitations (v1)

- **Tickets are read-only**: copy to clipboard, paste into TWS manually.
  Live IBKR sends come in v2.
- **Targets are SPX levels** not option prices. Eyeball the option
  ticket entry from the chain.
- **No pre-market warmup**: first 3 iters after market open are warmup-WAIT.
- **Engine state doesn't reset across days**: if you leave the app
  running overnight, manually restart to clear the stale 3-iter
  buffer.
- **No win/loss tracking yet**: tickets log to DB but the loop doesn't
  reconcile them with actual IBKR fills.

If anything fails the checklist, note which item and what you saw — see
`IMPROVEMENTS.md` for the running list of refinements.
