# fef-mcp — Quant analysis MCP server for Claude Code

Exposes the FEF sidecar's historical signals, trades, bars, microstructure samples, and flow snapshots as MCP tools callable from any Claude Code session.

## Why this exists

The sidecar already persists everything in SQLite. This server lets Claude run analyses across that data: daily debriefs, trade autopsies, kNN signal lookups, pattern discovery, P/L attribution.

## Setup

1. The server installs as part of the sidecar package (`pip install -e .` in `sidecar/`).
2. Add to your Claude Code `mcp.json`:

```json
{
  "mcpServers": {
    "fef-quant": {
      "command": "C:\\Users\\diant\\projects\\fef-terminal\\sidecar\\.venv\\Scripts\\python.exe",
      "args": ["-m", "fef_quant_mcp.server"],
      "env": {
        "FEF_DB_PATH": "C:\\Users\\diant\\.fef-terminal\\signal_log.db"
      }
    }
  }
}
```

3. Restart Claude Code. Verify with: "Use fef-quant to show me what data is available."

The sidecar does NOT need to be running — the MCP server reads SQLite directly.

## Tools

**Data access:**
- `list_available_data()` — inventory of what's in the DB
- `get_signals(from_date, to_date, direction, confidence, limit)` — query FEF iters
- `get_trades(status, won, signal_iter_id, ...)` — query closed tickets
- `get_bars(symbol, bar_size, from_ts_ms, to_ts_ms, limit)` — OHLCV bars
- `get_flow_snapshot_at(timestamp_ms, symbol, tolerance_seconds)` — walls/drift/MP at a moment
- `get_microstructure(symbol, from_ts_ms, to_ts_ms, limit)` — OFI/AVI/microprice samples

**Pre-baked analyses:**
- `analyze_trade(ticket_id)` — full trade autopsy
- `daily_summary(session_date)` — assembled session debrief
- `find_similar_signals(reference_signal_id, k)` — kNN over feature space
- `attribution_breakdown(from_date, to_date)` — P/L by direction/confidence/hour/weekday
- `propose_pattern(based_on_signal_ids OR based_on_won_trades_last_n)` — generate new pattern from winners

## Example prompts that work

> "What did my session look like on 2026-05-15?"

Claude calls `daily_summary("2026-05-15")` and writes a narrative.

> "Why did trade #47 lose money?"

Claude calls `analyze_trade(47)`, then `get_flow_snapshot_at(entry_ts)` for context, then explains.

> "Find me historical signals similar to today's first HIGH-conviction fire, and show how they played out."

Claude calls `get_signals(confidence='HIGH', limit=1)`, then `find_similar_signals(that_id, k=10)`.

> "Propose a new pattern based on my last 30 winning trades."

Claude calls `propose_pattern(based_on_won_trades_last_n=30)` and shows the proposed conditions for review.

> "Am I underperforming on Tuesdays?"

Claude calls `attribution_breakdown(from_date, to_date)` and reads the `by_weekday` bucket.
