# FEF Terminal — ground rules for any Claude session in this repo

SPX 0DTE paper-trading platform: Tauri/React frontend + Python sidecar (`sidecar/`),
IBKR TWS paper account, sidecar API on `127.0.0.1:8765`. System card: `docs/OPERATING_CARD.md`.

**`AGENTS.md` is the authoritative rules file for every agent (Claude, Codex, other) and
`docs/STATUS.md` is the current operational state and priority list — read both first.**
This file keeps the Claude-specific notes.

## Hard safety rules
- **NEVER edit `sidecar/src/` or restart the sidecar while it is running live.** The user says
  "terminal closed" before any deploy window opens. If unsure whether it's live, check port 8765.
- Nothing ships without the **full test suite green**: from `sidecar/`,
  `./.venv/Scripts/python.exe -m pytest tests/ -q` (955 passing as of 2026-08-25).
- Changes to the live trading path (`auto_trader.py`, `live_trading.py`, `spread_auto_trader.py`,
  `strike_picker.py`, `spread_exit_watcher.py`, `spx_target_watcher.py`, `multi_symbol_scanner.py`,
  `loop.py`, `position_reconcile.py`) get an adversarial second-context review before shipping —
  green tests alone missed the 7/6 oversell race, the price-contamination bug, and the 8/12
  `Bag()` constructor error that silently killed 197 spread orders.
- **Evidence-gated shipping**: strategy/gate changes require a backtest (out-of-sample) showing
  improvement before going live. Every gate skip logs a counterfactual. 15 direction-signal nulls
  are closed doors — do not re-open without new evidence (see user memory + OPERATING_CARD).
- Never place, modify, or cancel broker orders outside the bot's own logic unless the user asks.
- Do not `git commit` unless the user asks.

## Attribution (getting P&L right)
- Bot P&L = fills attributed to LiveOrder **parents** only. The paper account mixes in the user's
  manual trades (often sharing strikes) — never use aggregate/FIFO account math for bot claims.
- Ground truth for "did the bot fire": `GET /auto_trade/status` counters + `ibkr_execution` fills.
  The audit-log actor tag is hardcoded "user" — useless for attribution.
- `daily_review` win-counting is unreliable on partial-scaleout sessions — don't cite it.

## Data integrity (research)
- Research on **ThetaData historical** (real NBBO to 2022). Never research on live-captured
  `historical_bar` rows from May–Jul 2026 (stale ~40% of minutes) or on 2026-06-30 QuantData.
- QuantData was **cancelled 2026-08-25**; the 0DTE directional bot it fed is retired. Backtest
  inputs must still match live inputs (Theta-vs-QD parity broke us once already).
- The in-app research tools that produced untrustworthy numbers (walk-forward/optimize $, tournament,
  ML, what-if, tuning recs, strategy presets) were **deleted 2026-07-30**, along with their routes and
  the nightly sweep that fed them. Do not rebuild them. `RESEARCH_TAB_TEARDOWN.md` in the archive says
  what went and why; registry K0 says why a UI button cannot carry research discipline.
- **Research lives in `C:\Users\diant\fef-research-archive\`. `RESEARCH_REGISTRY.md` is the index.**
  Read **G0** (the strategy's measured verdict), **G00** (the strategic reframe), **G★** (the structural
  answer) and **K** (methodology rules) before proposing any study or strategy change.

## The verdict on the 0DTE strategy (2026-07-30, E4 + synthinject)
- Measured under CONV-LEVEL (fill no better than the trigger level): **−$48.72/ct across 1,435 fires,
  t=−3.80, negative in all five years, 0 of 181 grid cells positive OOS.** The published +$65,543
  is **−$209,737** at 3 lots. This is well-powered — detection floor ~$27.71/ct vs a −$48.72 effect.
- **The harness cannot detect an edge below ~5.0 index points** (k₄=0.47 makes it ~10). So the ~12
  intraday directional nulls were **uninformative by construction** — cite G★ (structural), never the
  null count. Rule 18: no null may be reported without its detection threshold.
- The bot's job is now **execution truth** (fills, latency, qualify timeouts, broker behaviour) at
  1 lot — not P&L. Do not score it on session P&L; that question is settled.

## Environment gotchas
- Windows console is cp1252: scripts print with `sys.stdout.reconfigure(errors="replace")`.
- Epoch timestamps are **milliseconds** everywhere in the DB.
- Throwaway files go to the session scratchpad, not the repo. **Anything that must survive —
  caches, study scripts, ledger state — goes to `D:\fef-data`**, never the scratchpad: it sits
  under `AppData\Local\Temp`, and cleanups there destroyed research data twice before the
  2026-09-26 move. See `AGENTS.md` (authoritative rules) and `docs/STATUS.md`.
- Frontend rebuilds: cargo PATH prefix + kill the running binary first (Tauri lock).

## Sidecar API quick refs
- `POST /exit_strategy` `{"long_strategy": "spx_points"}` (valid: spx_points | fixed_dollar | option_price)
- `POST /scanner/symbols` `{"enabled": {"SPX": true, ...}, "max_per_symbol": N}`
- `GET /auto_trade/status` (config + counters), `GET /regime/live` (5-tile regime read)
- Gate order on scanner fires: chop → bias-disagree → FOMC → TOD window, first skip wins.
