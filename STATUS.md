# v1 Status — built end-to-end

Built overnight (2026-05-16 → 2026-05-17). All 15 v1 tasks completed,
27 unit tests passing, full toolchain validated, both mock and live
provider paths implemented.

## What works right now

### Backend (Python sidecar)
- ✅ Native async QuantData client (httpx) — 8 endpoints, typed Pydantic
  responses, NTM max pain calc (validated against 5/15 session)
- ✅ Mock QuantData provider with frozen 5/15 data — runs offline
- ✅ IBKR market data via ib_async — live + mock bar streams
- ✅ FEF signal engine — full rule coverage (warmup, pin, wall proximity,
  absorption, alignment, wall collapse, 6-factor secondary)
- ✅ SQLite + sqlmodel persistence — SignalIter, TradeTicket, DailySession
- ✅ FastAPI app: WebSocket `/ws` + REST `/healthz`, `/status`,
  `/history/signals`, `/history/actionable`, `/run_iter_now`
- ✅ Background loops: 2-min FEF tick + continuous IBKR bar stream
- ✅ EventBroadcaster with bounded queues + drop-oldest

### Frontend (React + Tauri)
- ✅ Tauri 2 shell + Vite + React 19 + lightweight-charts v5
- ✅ Dark theme via CSS variables
- ✅ Resizable split-pane (70/30, react-resizable-panels v4)
- ✅ TopBar: SPX price, last iter, provider status, WS connection dot
- ✅ ChartPane: SPX 2-min candles + wall + NTM max pain overlays
  with combined same-strike labels
- ✅ FEFVerdictPanel: direction, confidence, reasoning, deltas, flags,
  collapsible 6-factor scorer
- ✅ QuantDataPanel: drift, NTM/raw MP, top GEX + DEX walls, aggression,
  freshness indicator
- ✅ OrderTicketPanel: editable qty (default 15), strike + staged targets,
  copy-to-clipboard button with TWS-formatted order text
- ✅ SignalLogPanel: polls /history/signals every 15s, color-coded by direction
- ✅ Auto-reconnect WebSocket client (2s backoff)

### Tooling
- ✅ Rust 1.95 / Cargo, Node 24 / npm 11, Python 3.12.10 — all validated
- ✅ MSVC (VS2019) — sufficient for Tauri 2 builds
- ✅ Git repo with 14 commits on `main`
- ✅ `scripts/start.bat` (simple) + `scripts/start.ps1` (configurable launcher)
- ✅ `SMOKE_TEST.md` for first live session
- ✅ `IMPROVEMENTS.md` with 24 categorized refinements

## What needs you (when you're back)

1. **Run the smoke test** during market hours with live providers — that's
   the only thing I can't verify without TWS + open market
2. **Decide which IMPROVEMENTS.md items to prioritize** for v1.1
3. **v2 scope**: live IBKR order sends (currently disabled in `permissions.deny`)

## Stats

- 14 commits
- ~5,000 lines added (~2,800 backend Python, ~1,200 frontend TS/TSX,
  ~700 CSS, ~300 config/docs)
- 27/27 tests passing in <1s
- npm build: 405KB JS / 6KB CSS / 129KB gzipped
- cargo check: 46s cold (only ran once)

## Launching it

```powershell
.\scripts\start.ps1                    # both mock — runs offline
.\scripts\start.ps1 -IbkrMode live     # IBKR live, QD still mock
.\scripts\start.ps1 -QuantDataMode live -IbkrMode live -LoopIntervalSec 60
```

Or `scripts\start.bat` for the double-click version.
