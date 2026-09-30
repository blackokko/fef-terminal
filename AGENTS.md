# AGENTS.md — rules for any AI agent working in this repo

This file is the entry point for Codex, Claude Code, or any other agent. `CLAUDE.md` holds the
same rules plus Claude-specific notes; **the rules below are the authoritative copy and are
inlined so no agent depends on reading a second file.** Current operational state and priorities
live in `docs/STATUS.md` — read it before starting any task. (The root-level `STATUS.md` was the
2026-05-16 v1 build record; it now lives at `docs/STATUS_2026-05-16_v1_build.md`.)

FEF Terminal is an SPX/ETF paper-trading platform: Tauri/React frontend + Python sidecar
(`sidecar/`, FastAPI on `127.0.0.1:8765`) + IBKR TWS paper account (`DU`-prefixed, port 7497).
Research lives in a separate repo, `blackokko/fef-research-archive`; its `RESEARCH_REGISTRY.md` is
the index of ~40 studies and 36 methodology rules, and its `README.md` orients a cold reader.

## Hard safety rules — non-negotiable

1. **NEVER edit `sidecar/src/` or restart the sidecar while it is running live.** Before touching
   the trading path, check port 8765. If it is listening, stop: the operator says "terminal closed"
   to open a deploy window. If unsure, it is live.
2. **Nothing ships without the full suite green.** From `sidecar/`:
   `./.venv/Scripts/python.exe -m pytest tests/ -q` — **1072 passing** as of 2026-09-30 (the ET/DST fix added 117). A red
   suite is a blocker, not a note.
3. **Changes to the live trading path get an adversarial second-context review before shipping** —
   `auto_trader.py`, `live_trading.py`, `spread_auto_trader.py`, `strike_picker.py`,
   `spread_exit_watcher.py`, `spx_target_watcher.py`, `multi_symbol_scanner.py`, `loop.py`,
   `position_reconcile.py`. Green tests alone missed the 7/6 oversell race, the price-contamination
   bug, and the `Bag()` constructor error that silently killed 197 orders. The reviewer must be a
   different context than the author.
4. **Never place, modify, or cancel broker orders outside the bot's own logic unless the operator
   explicitly asks.** No script, test, or "just checking" call touches TWS order endpoints.
5. **Do not `git commit` unless the operator asks. Never push without being asked.**
6. **No purchases, signups, trials, or credentials** — including data subscriptions and site
   memberships — without explicit approval.
7. **Evidence-gated shipping.** Strategy or gate changes require an out-of-sample backtest showing
   improvement, pre-registered in a `*_LOCK.md` in the archive before any data is touched.
   Fifteen direction-signal nulls and the closed doors in registry §H stay closed; do not re-open
   one without new evidence.
8. **Research runs on ThetaData historical** (real NBBO to 2022), never on live-captured
   `historical_bar` rows from May–Jul 2026 (~40% stale minutes). Open every research DB
   `file:...?mode=ro` with `pragma query_only=1`.
9. **Bot P&L = fills attributed to `LiveOrder` parents only.** The paper account mixes in the
   operator's manual trades; never use aggregate account math for bot claims.
10. **Scope (operator, 2026-09-27): options and shares on equities only** — single stocks, equity
    ETFs and their options. SPX/NDX index options only once something works on equities. **No
    futures, no VIX products, no FX/rates/commodities.** Do not propose or run studies outside this.

## Two-agent protocol (Claude + Codex on the same repo)

- **One writes, the other reviews. Never both editing `sidecar/src/` at once.** The live path has
  no safety net beyond the suite, and the suite has already missed three live-path bugs (rule 3).
- **The human merges to `main`.** Neither agent deploys, restarts, or arms anything.
- **Research locks are written by one agent and executed by the other.** An executor cannot bend a
  gate it did not write. Pre-data amendments are numbered and may only make a study stricter.
- **Review scope is "blocking issues only."** Fix what would change a verdict or break the trading
  path; do not loop on wording. The reviewer may be pushed back on, with evidence.
- **Report what you checked that came back clean**, not only what you found. Thirty-seven
  consecutive research agents have found real bugs in their *own* controls; that only happens when
  looking is expected.

## Environment gotchas

- Windows console is cp1252: `sys.stdout.reconfigure(errors="replace")` in every script.
- Epoch timestamps are **milliseconds** everywhere in the DB. `submitted_at` is UTC.
- `multidte_cache.spx_1m` prices are in **CENTS**; RTH is `m ∈ [570, 960)`; the 09:30 bar
  (`m=570`) is dead on 53.6% of sessions upstream (94.6% in 2025) — never anchor a level, entry
  or daily bar on it.
- **SPX, NDX and RUT are calculated indices, not tradeable instruments.** They carry positive
  1-minute autocorrelation from nonsynchronous component prints (RUT ρ₁ = +0.336, SPX +0.045,
  QQQ the ETF +0.008). A breakout on an index "continues by construction." Never substitute an
  index for its ETF at intraday horizon — registry rule 36.
- Frontend rebuilds need `export PATH="/c/Users/diant/.cargo/bin:$PATH"` first (a build without it
  reports success and produces no binary) and the running `fef-terminal.exe` closed (Cargo cannot
  overwrite a locked binary). Ask before killing the operator's app. **Any change under `src/`
  needs an app rebuild to reach the window**; the sidecar runs from source, the window does not.
  The 2026-08-25 QuantData removal ran against an 8/18 window until 2026-09-30, and the old chart
  code crashed the whole shell on the sidecar's now-empty `snapshot`.
- **Research data and working scripts live in `D:\fef-data`** (moved out of `AppData\Local\Temp`
  on 2026-09-26, after Temp cleanups destroyed `chainfill_cache.db` and, silently, a module the
  carry ledger imports). The agent session scratchpad is for throwaway files only: **anything that
  must survive goes to `D:\fef-data` or a repo, never the scratchpad.** Old reports that cite
  `...\Temp\claude\C--Users-diant\13885fae-...\scratchpad\X` now mean `D:\fef-data\X`.

## Where things are

| what | where |
|---|---|
| sidecar API | `sidecar/src/fef_sidecar/api.py`; `GET /auto_trade/status`, `GET /spread_auto_trade/status` |
| credit-spread bot | `engine/spread_auto_trader.py`, `spread_recommender.py`, `strike_picker.py`, `spread_entry.py`, `spread_exit_watcher.py` |
| sleeve executor | `fef-research-archive/sleeve_executor.py` (+ `test_sleeve_executor.py`) |
| backtest↔live reconciliation | `fef-research-archive/recon.py` (+ tests) — awaiting fills, see STATUS |
| launchers | `scripts/FEF-Paper-AutoTrade.bat` → `scripts/start.ps1 -LiveTrading -CreditSpreads -IbkrPort 7497` |
| research index | `fef-research-archive/RESEARCH_REGISTRY.md` — §G★ structural result, §H closed doors, §J corrections, §K rules |
| research data, caches, working study scripts | `D:\fef-data` (not in git; 35 GB; `_migration_2026-09-26\` holds the move's manifest and proofs) |
| paper-ledger jobs | Task Scheduler `FEF-DailyClose` 15:15 CT → `C:\Users\diant\fef-watchers\run_ledgers.ps1` (runs updaters in `D:\fef-data`, mirrors to the archive); `FEF-DailyOpen` 08:30 CT starts the two collectors |
