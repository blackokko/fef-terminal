"""Backfill live_order.parent_id on watcher-fired SELL rows.

Scope: pairs every child SELL row (live_order.action='SELL' AND bracket_role
in t1/t2/t3/stop/runner_protect/dollar_cap/trail_after_t1) with the
most-likely entry parent BUY by contract + temporal + status match.

This script doesn't help today (the new writer is the structural fix), but
if any future code path persists a child without parent_id, this is the
recovery tool.

Heuristic for unambiguous match:
  - same symbol + strike + right + expiry
  - parent.action='BUY' AND parent.bracket_role='parent'
  - parent.submitted_at < child.submitted_at by 1s..6h
  - exactly ONE candidate satisfies — else flagged ambiguous

Backfill also covers ibkr_execution-only rows by writing synthetic
LiveOrder child rows where a SELL exec has no matching child but a
parent BUY can be uniquely identified. This makes per-parent P&L
attribution computable across the historical window.

Usage:
    python scripts/maintenance/backfill_parent_id.py
    python scripts/maintenance/backfill_parent_id.py --dry-run
    python scripts/maintenance/backfill_parent_id.py --db PATH

Idempotent: only writes rows where parent_id is currently NULL.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timedelta

CHILD_ROLES = ("t1", "t2", "t3", "stop", "runner_protect",
               "dollar_cap", "trail_after_t1")

MATCH_WINDOW_MIN_SEC = 1
MATCH_WINDOW_MAX_SEC = 6 * 3600  # 6 hours


def _parse_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")
        except (TypeError, ValueError):
            return None


def _fmt_dt(dt: datetime) -> str:
    """Format a datetime as SQLite stores it — space-separated, no 'T'.
    Python's isoformat() uses 'T' which breaks string-based BETWEEN
    comparisons against SQLite's default space-separated DATETIME values.
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _find_parent(
    cur: sqlite3.Cursor,
    *,
    child_submitted_at: datetime,
    symbol: str, strike: float, right: str, expiry: str,
) -> tuple[list[int], int]:
    """Return (candidate_parent_ids, count). Empty list when no candidate."""
    lo = _fmt_dt(child_submitted_at - timedelta(seconds=MATCH_WINDOW_MAX_SEC))
    hi = _fmt_dt(child_submitted_at - timedelta(seconds=MATCH_WINDOW_MIN_SEC))
    cur.execute(
        """SELECT id FROM live_order
           WHERE bracket_role = 'parent'
             AND action = 'BUY'
             AND symbol = ? AND right = ? AND expiry = ?
             AND ABS(strike - ?) < 0.01
             AND submitted_at BETWEEN ? AND ?
           ORDER BY submitted_at DESC
        """,
        (symbol, right, expiry, float(strike), lo, hi),
    )
    rows = cur.fetchall()
    return [r[0] for r in rows], len(rows)


def backfill_existing_children(conn: sqlite3.Connection, dry_run: bool) -> dict:
    """Pass 1: update live_order rows that already exist but lack parent_id."""
    cur = conn.cursor()
    placeholders = ",".join("?" * len(CHILD_ROLES))
    cur.execute(
        f"""SELECT id, submitted_at, symbol, strike, right, expiry,
                   bracket_role, ib_order_id
           FROM live_order
           WHERE parent_id IS NULL
             AND bracket_role IN ({placeholders})
        """,
        CHILD_ROLES,
    )
    rows = cur.fetchall()
    matched = 0
    ambiguous = 0
    no_match = 0
    updates: list[tuple[int, int]] = []
    for r in rows:
        child_id, sub_at_s, sym, strike, right, expiry, role, _ib = r
        sub_at = _parse_dt(sub_at_s)
        if sub_at is None:
            no_match += 1
            continue
        candidates, n = _find_parent(
            cur, child_submitted_at=sub_at,
            symbol=sym, strike=strike, right=right, expiry=expiry,
        )
        if n == 0:
            no_match += 1
        elif n == 1:
            updates.append((candidates[0], child_id))
            matched += 1
        else:
            ambiguous += 1
    if updates and not dry_run:
        conn.executemany(
            "UPDATE live_order SET parent_id = ? WHERE id = ?",
            updates,
        )
        conn.commit()
    return {
        "scanned": len(rows), "matched": matched,
        "ambiguous": ambiguous, "no_match": no_match,
        "updated": 0 if dry_run else len(updates),
    }


def backfill_synthetic_children(conn: sqlite3.Connection, dry_run: bool) -> dict:
    """Pass 2: for ibkr_execution SELL rows with no matching live_order child,
    synthesize a LiveOrder child if a unique parent can be identified.

    Synthetic child: parent.symbol/strike/right/expiry; action='SELL';
    bracket_role='watcher_sell' (generic, since we can't recover tier
    label from exec alone); qty=shares; avg_fill_price=price;
    ib_order_id=exec.ib_order_id; engine_version=parent.engine_version.
    """
    cur = conn.cursor()
    # SELL execs with no matching live_order row by ib_order_id
    cur.execute(
        """SELECT e.id, e.exec_id, e.timestamp, e.session_date, e.symbol,
                  e.strike, e.right, e.expiry, e.shares, e.price, e.avg_price,
                  e.ib_order_id, e.perm_id
           FROM ibkr_execution e
           WHERE e.side IN ('SLD', 'SELL')
             AND e.ib_order_id IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM live_order lo
               WHERE lo.ib_order_id = e.ib_order_id
             )
        """,
    )
    rows = cur.fetchall()
    matched = 0
    ambiguous = 0
    no_match = 0
    inserts: list[tuple] = []
    for r in rows:
        (_eid, _exec_id, ts_s, _sess, sym, strike, right, expiry,
         shares, price, avg_price, ib_oid, perm_id) = r
        ts = _parse_dt(ts_s)
        if ts is None:
            no_match += 1
            continue
        candidates, n = _find_parent(
            cur, child_submitted_at=ts,
            symbol=sym, strike=strike, right=right, expiry=expiry,
        )
        if n == 0:
            no_match += 1
        elif n == 1:
            parent_id = candidates[0]
            # Grab parent dry_run + engine_version + ticket_id for the synthetic row
            cur.execute(
                "SELECT dry_run, engine_version, ticket_id FROM live_order WHERE id=?",
                (parent_id,),
            )
            pdat = cur.fetchone()
            p_dry = pdat[0] if pdat else 0
            p_engv = pdat[1] if pdat else 1
            p_tid = pdat[2] if pdat else None
            inserts.append((
                ts_s, p_tid, ib_oid, perm_id, sym, float(strike), right, expiry,
                "SELL", int(round(float(shares))), "MKT", None,
                parent_id, "watcher_sell", "FILLED", int(round(float(shares))),
                float(avg_price if avg_price is not None else price),
                0.0, ts_s, "synthetic backfill from ibkr_execution",
                p_dry, p_engv,
            ))
            matched += 1
        else:
            ambiguous += 1
    if inserts and not dry_run:
        conn.executemany(
            """INSERT INTO live_order (
                 submitted_at, ticket_id, ib_order_id, ib_perm_id,
                 symbol, strike, right, expiry,
                 action, qty, order_type, limit_price,
                 parent_id, bracket_role, status, filled_qty,
                 avg_fill_price, commission, last_status_at, notes,
                 dry_run, engine_version
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            inserts,
        )
        conn.commit()
    return {
        "scanned": len(rows), "matched": matched,
        "ambiguous": ambiguous, "no_match": no_match,
        "synthesized": 0 if dry_run else len(inserts),
    }


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
    ap.add_argument(
        "--no-synthetic", action="store_true",
        help="Skip the ibkr_execution synthetic-child pass.",
    )
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2
    print(f"DB: {db_path}")

    conn = sqlite3.connect(str(db_path))

    print()
    print("Pass 1: existing live_order child rows with parent_id NULL")
    r1 = backfill_existing_children(conn, dry_run=args.dry_run)
    print(f"  scanned:   {r1['scanned']}")
    print(f"  matched:   {r1['matched']}")
    print(f"  ambiguous: {r1['ambiguous']}")
    print(f"  no_match:  {r1['no_match']}")
    print(f"  updated:   {r1['updated']}")

    if not args.no_synthetic:
        print()
        print("Pass 2: ibkr_execution SELL rows with no matching live_order")
        r2 = backfill_synthetic_children(conn, dry_run=args.dry_run)
        print(f"  scanned:     {r2['scanned']}")
        print(f"  matched:     {r2['matched']}")
        print(f"  ambiguous:   {r2['ambiguous']}")
        print(f"  no_match:    {r2['no_match']}")
        print(f"  synthesized: {r2['synthesized']}")

    if args.dry_run:
        print()
        print("DRY RUN — no writes.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
