# FEF Terminal — Setup / Migration Guide

How to clone this repo and run it on a new machine (laptop for travel, second desktop, etc.).

## What's in the repo vs what isn't

**In the repo (cloned):** all source — Python sidecar (`sidecar/`), React frontend (`src/`), Tauri shell (`src-tauri/`), research/analysis tooling (`scripts/research/`), config files.

**NOT in the repo (you provide per machine):**
- The SQLite database (`~/.fef-terminal/signal_log.db`) — copy it over separately
- QuantData credentials (`~/.quantdata-mcp/config.json`) — provision via `quantdata-mcp setup`
- Your `.env` (copy from `.env.example`)
- IBKR TWS / Gateway install
- `node_modules/`, Python `.venv/`, Rust build artifacts — regenerated on setup

## Prerequisites

- **Python 3.12+** (3.14 not yet supported)
- **Node.js 18+** and npm
- **Rust toolchain** (rustup) — only needed if building the Tauri desktop app
- **IBKR TWS or IB Gateway** — only needed for live/paper trading (not for analysis)
- **QuantData account** — for live market data (not needed for analysis on a copied DB)

## Quick start (analysis only — no broker needed)

This is all you need to run the research/analysis tooling against a copied database. No TWS, no QuantData.

```bash
git clone <repo-url> fef-terminal
cd fef-terminal/sidecar
python -m venv .venv
# Windows:  .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -e .

# Copy the database to the default location:
#   Windows: %USERPROFILE%\.fef-terminal\signal_log.db
#   macOS/Linux: ~/.fef-terminal/signal_log.db
# (or set FEF_DB_URL to point wherever you put it)

# Run any analysis script:
python scripts/research/ablation_backtest.py 2026-05-19 2026-05-26
python scripts/research/research_dump.py 2026-05-19 2026-05-26
```

That covers ~everything we do in EOD analysis sessions.

## Full stack (live/paper trading)

```bash
# 1. Frontend deps
cd fef-terminal
npm install

# 2. Sidecar (as above)
cd sidecar && python -m venv .venv && <activate> && pip install -e . && cd ..

# 3. Env
cp .env.example .env
# Edit .env: set FEF_PROVIDER_IBKR=false, FEF_IBKR_PORT to your TWS/Gateway port

# 4. QuantData credentials
quantdata-mcp setup --auth-token <TOKEN> --instance-id <ID>
# (writes ~/.quantdata-mcp/config.json)

# 5. Database — copy signal_log.db to ~/.fef-terminal/ (or start fresh; migrations auto-run)

# 6. Start TWS / IB Gateway, log into paper, enable API on the configured port

# 7. Run the sidecar
cd sidecar && <activate> && fef-sidecar     # serves on 127.0.0.1:8765

# 8. Run the desktop app (separate terminal)
npm run tauri dev      # dev mode
# or: npm run tauri build  → binary in the cargo target dir
```

## Machine-specific gotchas

1. **Cargo target dir.** `.cargo/config.toml` redirects Rust builds to `D:/fef-terminal-builds/target` (the original dev machine had a full C: drive). On a new machine that path won't exist — either edit it to a path that does, or delete the `[build] target-dir` line to use the default `./target`.

2. **The sidecar is an editable install.** `pip install -e .` means code changes take effect on sidecar restart — no rebuild step. Only the Tauri shell needs `npm run tauri build`.

3. **Backend-only changes need just a sidecar restart**, not a Tauri rebuild.

4. **Tauri binaries are per-OS.** A Windows build won't run on macOS. Rebuild on each platform.

## Verifying the setup

After setup, confirm the loop works end-to-end:
- Sidecar logs show "QuantData poll" + "FEF iter" lines
- Frontend shows live SPX (or mock SPX if `FEF_PROVIDER_IBKR=true`)
- `pytest` in `sidecar/` passes (currently 129/129)

## Env reference

See `.env.example` for the full annotated list. The flags that matter most:

| Var | Default | Effect |
|---|---|---|
| `FEF_PROVIDER_IBKR` | `true` (mock) | `false` connects to real TWS/Gateway |
| `FEF_LIVE_TRADING` | `false` | must be `true` to send any order |
| `FEF_REQUIRE_PAPER_ACCOUNT` | `true` | refuses non-`DU` accounts (paper-only guard) |
| `FEF_IBKR_PORT` | `7497` | TWS paper; 7496 live, 4002/4001 Gateway |
| `FEF_DB_URL` | `~/.fef-terminal/signal_log.db` | point at a copied DB |
| `FEF_CHAIN_CAPTURE_ENABLED` | `true` | option chain capture loop |
