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

Open two terminals.

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
