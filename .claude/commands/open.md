---
description: Morning ritual — configure and verify the trading stack after the user opens terminal/sidecar/TWS
---

The user has opened the FEF terminal, sidecar, and TWS for today's session. Execute the morning
ritual. Scanner/auto-trade config is **in-memory and resets on every restart** — it must be pushed
each morning. Do NOT enable auto-trade; the user flips it ON themselves in the terminal.

## Steps

1. **Health**: confirm sidecar answers on `127.0.0.1:8765` (`GET /auto_trade/status`). If it
   doesn't, stop and tell the user.
2. **Exit strategy**: `POST /exit_strategy` body `{"long_strategy": "spx_points"}` (live default
   since 2026-07-07 per user decision; the coded default is already spx_points — this asserts it
   deterministically). Read back with `GET /exit_strategy`.
3. **Scanner symbols**: `POST /scanner/symbols` body
   `{"enabled": {"SPX": true, "SPY": true, "QQQ": true}, "max_per_symbol": 50}`
   (50 = user's current choice; a 2–3/day cap offer is pending — don't change it unilaterally).
4. **Auto-trade config** (via the same endpoints used in prior sessions — verify against
   `GET /auto_trade/status`, don't assume): qty **3**, cooldown **300s**, flow/scanner triggers
   only (patterns stay DISARMED), `enabled: false`.
5. **Verify & report**: echo the effective config from `GET /auto_trade/status` + `GET /regime/live`
   morning read in ONE short confirmation line (alert-mode style), ending with "flip auto-trade ON
   when ready."

## Post-restart watchlist (check during the first hour, report only anomalies)

- Contamination counters at literal zero: no `scanner_input_glitch` / `reference_pollution` audit
  rows, no signal rows with |Δspot| > 100.
- First fires carry all four gate tag blocks (chop / bias / fomc / tod) in extras.
- First multi-tier exit sells each tier ONCE (oversell fix live-verification — watch fills).
- `vix_at_fire` is a live value, not frozen (17.0231 was the 7/6 stale constant).
- Engine-change ledger backfill ran at boot (32 entries).
