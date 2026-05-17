# v2 Roadmap

Items deferred to v2 because they depend on **live IBKR order routing**
— which is intentionally disabled in v1 (see `permissions.deny` in
`~/.claude/settings.json`).

## v2 prerequisites

1. Wire `ib_async.placeOrder` in `LiveIBKRProvider`
2. Wire `ib_async.cancelOrder` likewise
3. Remove `mcp__ibkr__place_order` / `mcp__ibkr__cancel_order` from
   `permissions.deny` after user confirms

Once those land, the items below become tractable:

## Items

### IMPROVEMENTS #14 — Multi-leg orders (PCS / verticals / iron condors)

User actively trades PCS as a complement to long calls/puts. v2 should:
- Add `OrderLeg` model: side (BUY/SELL), strike, right (C/P), expiry, qty
- Add `MultiLegTicket` model: list[OrderLeg] + max debit/min credit
- Strategy templates: vertical, bull/bear spread, iron condor, calendar
- TWS combo order submission (uses `Combo` contract type in ib_async)

### IMPROVEMENTS #15 — Order pre-flight modal

Before any live order send, modal showing:
- Total cost (qty × ask × 100)
- Buying power after
- Duplicate order check (any pending order at same strike?)
- Margin impact (for spreads / PCS)
- Big red SEND button + Cancel

Cannot ship without #14 prereq for accurate cost calc on multi-leg.

### IMPROVEMENTS #16 — Take-profit ladder auto-place

On fill (parent order BUY 15× SPX 7450C):
- Auto-place 3 child SELL LIMIT orders: 5× @ T1, 5× @ T2, 5× @ T3
- Plus 1 STOP LIMIT: 15× @ stop
- OCO grouping so first hit cancels the rest in the same tier

Needs: order ID tracking, fill event subscription, ib_async OCO/bracket
order support (which exists but is non-trivial to drive).

### IMPROVEMENTS #18 — Multi-monitor (pop chart to standalone window)

Not strictly v2-gated but deferred for scope reasons. Approach:
- Tauri 2 `WebviewWindow.new()` for a second window
- Shared WS connection state via window-to-window IPC or by both
  connecting to the same sidecar `/ws`
- Standalone chart route (`/chart-only`) that renders only the
  ChartPane

## Out of scope for v2 (kept here for visibility)

- Cloud backup of signal log (currently local-only via
  `scripts/backup_signal_log.py`)
- Mobile companion app
- Backtest UI on top of the replay endpoint (`/history/replay/{date}`
  exists; UI not built)
- Live order-fill reconciliation (`auto-tag outcomes` endpoint exists
  at `/tickets/{id}/close` but currently manual)
