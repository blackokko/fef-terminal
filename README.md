# FEF Terminal

Personal quant workstation for SPX 0DTE trading. Combines real-time QuantData options flow, IBKR market data, and the FEF (Flow Effectiveness Filter) signal engine in a single Tauri desktop app.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Tauri Shell (Rust)  ──spawns──>  Python sidecar (FastAPI + uvicorn) │
│         │                                  │                          │
│         │                                  ├─ QuantData client        │
│         ▼                                  ├─ IBKR via ib_async       │
│  React + TypeScript                        ├─ FEF signal engine       │
│  + lightweight-charts                      ├─ SQLite (sqlmodel)       │
│  + WebSocket client  <──ws://127.0.0.1:8765/ws── WebSocket server     │
└──────────────────────────────────────────────────────────────────────┘
```

- **Shell**: Tauri 2 (Rust) — windowing, IPC, sidecar lifecycle
- **Frontend**: React 18 + TypeScript + Vite + lightweight-charts
- **Backend**: Python 3.12 + FastAPI + ib_async + sqlmodel
- **Storage**: SQLite (signal log, trade history, future backtests)

## Prerequisites

| Tool | Version |
|---|---|
| Node | 18+ |
| Rust | stable (rustup default) |
| Python | 3.12 (NOT 3.13/3.14 — `ib_async` compatibility) |
| MSVC Build Tools | 2019 or 2022 with C++ workload |
| TWS or IB Gateway | latest, API enabled on port 7497 (paper) or 7496 (live) |

## Dev workflow

### Option A — one-click launcher (recommended)
```powershell
.\scripts\start.ps1                         # mock providers, 2-min cadence
.\scripts\start.ps1 -IbkrMode live          # IBKR live (requires TWS running)
.\scripts\start.ps1 -QuantDataMode live -IbkrMode live -LoopIntervalSec 60
```

Or just double-click `scripts\start.bat` for the simple version.

### Option B — manual (two terminals)

**Terminal 1 — Python sidecar:**
```powershell
cd sidecar
.venv\Scripts\activate
fef-sidecar
```

**Terminal 2 — Tauri dev (frontend + Rust shell):**
```powershell
npm run tauri dev
```

The frontend talks to the sidecar over `ws://127.0.0.1:8765/ws`.

### Provider modes

Both default to mock so you can run offline. Switch via env vars:
- `FEF_PROVIDER_QUANTDATA=live` — use real QuantData (requires `~/.quantdata-mcp/config.json`)
- `FEF_PROVIDER_IBKR=live` — use real IBKR (requires TWS / Gateway running)
- `FEF_LOOP_INTERVAL_SEC=60` — FEF iter cadence (default 120s)
- `FEF_LOOP_MARKET_HOURS_ONLY=true` — only run during 9:30-16:00 ET
- `FEF_DISABLE_AUTO_LOOP=true` — skip the background loops (for tests)

## Project layout

```
fef-terminal/
├── src/                  # React frontend
├── src-tauri/            # Rust shell + Tauri config
├── sidecar/              # Python backend (FastAPI + signal engine)
│   ├── pyproject.toml
│   └── src/fef_sidecar/
│       ├── main.py       # FastAPI entry
│       ├── quantdata.py  # (task #2) QuantData client
│       ├── ibkr.py       # (task #3) IBKR market data
│       ├── fef.py        # (task #4) FEF signal engine
│       ├── models.py     # (task #5) sqlmodel schemas
│       └── ws.py         # (task #6) WebSocket broadcaster
└── public/
```

## Build for release

(v1.5+) Bundle Python sidecar via PyInstaller into `src-tauri/binaries/`, then `npm run tauri build` produces a single-binary installer.

## Status

v1 in progress. See task list for current milestone.
