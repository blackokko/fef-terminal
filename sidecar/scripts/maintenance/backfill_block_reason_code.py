"""Backfill audit_log.block_reason_code on historical WAIT/block rows.

Scope: rows where verdict-side activity was blocked (auto_trade_block,
option_quote_fail, safe_sell_blocked, safe_sell_clamped, safe_sell_error,
order_refused). For each row whose `block_reason_code` column is NULL but
the structured code can be inferred from the `details` JSON or the
`summary` text, populate the column.

Usage:
    python scripts/maintenance/backfill_block_reason_code.py
    python scripts/maintenance/backfill_block_reason_code.py --dry-run
    python scripts/maintenance/backfill_block_reason_code.py --db PATH

Idempotent: only updates rows where the column is currently NULL.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Categories that represent block-style outcomes — their NULL rows are
# candidates for backfill. Other categories (kill_switch, config, order,
# spx_watcher, auto_trade_toggle, etc.) describe ACTIONS not blocks, so
# block_reason_code is intentionally NULL for them.
BLOCK_CATEGORIES = {
    "auto_trade_block",
    "option_quote_fail",
    "safe_sell_blocked",
    "safe_sell_clamped",
    "safe_sell_error",
    "order_refused",
}

# Codes from auto_trader.BlockReason — kept in sync manually so this
# script has no engine import dependency.
KNOWN_CODES = {
    "DISABLED", "ENV_NOT_SET", "VERDICT_WAIT", "TRIGGER_NOT_ENABLED",
    "BELOW_FLOOR", "CALENDAR_PAUSE", "COOL_DOWN", "GLOBAL_CAP",
    "SYMBOL_CAP", "NO_OPTION_PRICING", "INVALID_PRICING", "NO_STRIKE",
    "PLACE_ERROR", "DUPLICATE_PARENT", "FIRED",
    "OPTION_QUOTE_FAIL", "SAFE_SELL_NO_POSITION",
    "SAFE_SELL_SHORT_BLOCKED", "SAFE_SELL_CLAMPED", "SAFE_SELL_ERROR",
    "CREDIT_SPREAD_GATE_OFF", "ORDER_REFUSED",
}


def _infer_from_details(details_json: str | None) -> str | None:
    if not details_json:
        return None
    try:
        d = json.loads(details_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    # Direct hits — early callers stuffed code into details.
    for key in ("block_reason_code", "reason_code"):
        v = d.get(key)
        if isinstance(v, str) and v in KNOWN_CODES:
            return v
    return None


def _infer_from_summary(category: str, summary: str) -> str | None:
    """Heuristic mapping based on category + summary keywords. Used for
    rows where details JSON doesn't carry the structured code."""
    s = (summary or "").lower()
    if category == "option_quote_fail":
        return "OPTION_QUOTE_FAIL"
    if category == "order_refused":
        return "ORDER_REFUSED"
    if category == "safe_sell_clamped":
        return "SAFE_SELL_CLAMPED"
    if category == "safe_sell_error":
        return "SAFE_SELL_ERROR"
    if category == "safe_sell_blocked":
        if "credit_spread_gate" in s or "credit_spread_gate" in s:
            return "CREDIT_SPREAD_GATE_OFF"
        if "net_long=0" in s or "no_position" in s:
            return "SAFE_SELL_NO_POSITION"
        if "structural_block" in s or "naked short" in s:
            return "SAFE_SELL_SHORT_BLOCKED"
        return "SAFE_SELL_SHORT_BLOCKED"
    if category == "auto_trade_block":
        # Map common reason strings to codes
        if "disabled" in s:
            return "DISABLED"
        if "fef_live_trading env not set" in s or "env not set" in s:
            return "ENV_NOT_SET"
        if "verdict is wait" in s or "verdict_wait" in s:
            return "VERDICT_WAIT"
        if "trigger" in s and "not in enabled" in s:
            return "TRIGGER_NOT_ENABLED"
        if "below floor" in s:
            return "BELOW_FLOOR"
        if "calendar pause" in s:
            return "CALENDAR_PAUSE"
        if "cool-down" in s or "cool_down" in s:
            return "COOL_DOWN"
        if "cap reached" in s:
            return "SYMBOL_CAP"
        if "no option pricing" in s:
            return "NO_OPTION_PRICING"
        if "invalid option_pricing" in s:
            return "INVALID_PRICING"
        if "no strike" in s:
            return "NO_STRIKE"
        if "place_bracket error" in s:
            return "PLACE_ERROR"
        if "duplicate parent" in s or "already have active parent" in s:
            return "DUPLICATE_PARENT"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db",
        default=str(Path.home() / ".fef-terminal" / "signal_log.db"),
        help="Path to signal_log.db",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Show counts without writing.",
    )
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2
    print(f"DB: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Pull all NULL rows in block categories.
    placeholders = ",".join("?" * len(BLOCK_CATEGORIES))
    cur = conn.execute(
        f"""SELECT id, category, summary, details
            FROM audit_log
            WHERE block_reason_code IS NULL
              AND category IN ({placeholders})
        """,
        tuple(BLOCK_CATEGORIES),
    )
    rows = cur.fetchall()
    print(f"Candidate rows (block categories, block_reason_code NULL): {len(rows)}")

    inferred: list[tuple[int, str]] = []
    no_match: list[int] = []
    by_code: dict[str, int] = {}

    for row in rows:
        code = _infer_from_details(row["details"])
        if code is None:
            code = _infer_from_summary(row["category"], row["summary"])
        if code is None:
            no_match.append(row["id"])
            continue
        inferred.append((row["id"], code))
        by_code[code] = by_code.get(code, 0) + 1

    print(f"  Inferred: {len(inferred)}")
    print(f"  No match: {len(no_match)}")
    print()
    print("By code:")
    for code, n in sorted(by_code.items(), key=lambda kv: -kv[1]):
        print(f"  {code}: {n}")

    if no_match:
        print()
        print(f"First 10 no-match IDs: {no_match[:10]}")
        # Show a sample summary
        for nid in no_match[:5]:
            r = conn.execute(
                "SELECT category, summary FROM audit_log WHERE id=?", (nid,),
            ).fetchone()
            print(f"  #{nid}  [{r['category']}]  {(r['summary'] or '')[:120]}")

    if args.dry_run:
        print()
        print("DRY RUN — no writes.")
        conn.close()
        return 0

    if inferred:
        conn.executemany(
            "UPDATE audit_log SET block_reason_code = ? WHERE id = ?",
            [(code, _id) for _id, code in inferred],
        )
        conn.commit()
        print()
        print(f"Updated {len(inferred)} rows.")
    else:
        print()
        print("Nothing to update.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
