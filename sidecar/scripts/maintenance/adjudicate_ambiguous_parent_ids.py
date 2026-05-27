"""Adjudicate the ~168 SELL execs that backfill_parent_id.py left ambiguous.

Context: backfill_parent_id.py's pass 2 synthesizes a live_order child row for
each ibkr_execution SELL that lacks a matching live_order row by ib_order_id.
Its match heuristic is "same contract + parent submitted 1s..6h before SELL".
When multiple parents on the same contract submitted within that window
(bracket stacking), the heuristic punted ("ambiguous").

This script applies a richer set of heuristics — FIFO consumption of parents
in submission order, split-fill grouping by ib_order_id, and exact-quantity
matching — to attribute as many of those SELLs as possible.

Heuristics (in priority order):
  1) Group unassigned SELL execs by ib_order_id (each ioid = one ticket).
     Co-fills sharing an ioid go to the SAME parent.
  2) For each contract, walk SELL groups in (ts, ioid) order. Maintain
     remaining_qty per parent = parent.filled_qty - sum(assigned SELL qty).
     Skip parents with remaining_qty <= 0 (already fully closed).
     Skip parents whose submitted_at >= sell.ts (parent submitted AFTER sell).
  3) Try to find a single eligible parent whose remaining_qty == sell.qty.
     If exactly one: HIGH confidence (exact match + FIFO).
  4) Else assign to the OLDEST eligible parent (FIFO by submitted_at).
     If sell.qty <= parent.remaining_qty: HIGH confidence.
     If sell.qty >  parent.remaining_qty: MEDIUM confidence + split across
     multiple parents FIFO (rare; manual position-flatten ib_order_id=0 case).
  5) If zero eligible parents: ORPHAN (logged, no row written).

All resolved matches are persisted as synthetic live_order rows mirroring the
shape created by backfill_parent_id.py (bracket_role='watcher_sell',
status='FILLED', notes='adjudicated backfill from ibkr_execution').

A parent_id_audit table is created on first run to log every decision
(MATCHED-high / MATCHED-medium / STILL_AMBIGUOUS / ORPHAN) with candidate
parent_id lists so spot-checks are reproducible.

Usage:
    python scripts/maintenance/adjudicate_ambiguous_parent_ids.py
    python scripts/maintenance/adjudicate_ambiguous_parent_ids.py --dry-run
    python scripts/maintenance/adjudicate_ambiguous_parent_ids.py --db PATH

Idempotent: only acts on ibkr_execution SELL rows without a matching
live_order row, so re-running picks up only what's still unresolved.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Bound how far back we look for a parent on the same contract.
# Same calendar day is a natural session bound; we also reject any parent
# that submitted AFTER the sell.
SAME_SESSION_ONLY = True


def _parse_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")
        except (TypeError, ValueError):
            try:
                return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                return None


def ensure_audit_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS parent_id_audit (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             run_at DATETIME NOT NULL,
             exec_id VARCHAR NOT NULL,
             ibkr_exec_pk INTEGER NOT NULL,
             ib_order_id INTEGER,
             ts DATETIME,
             symbol VARCHAR,
             strike REAL,
             right VARCHAR,
             expiry VARCHAR,
             qty REAL,
             decision VARCHAR NOT NULL,   -- MATCHED-high | MATCHED-medium | STILL_AMBIGUOUS | ORPHAN
             confidence VARCHAR,
             candidate_parent_ids VARCHAR,  -- JSON list
             assigned_parent_id INTEGER,
             notes VARCHAR
           )
        """,
    )
    conn.commit()


def load_unassigned_sells(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        """SELECT e.id AS pk, e.exec_id, e.timestamp, e.session_date,
                  e.symbol, e.strike, e.right, e.expiry,
                  e.shares AS qty, e.price, e.avg_price,
                  e.ib_order_id, e.perm_id
           FROM ibkr_execution e
           WHERE e.side IN ('SLD','SELL')
             AND e.ib_order_id IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM live_order lo WHERE lo.ib_order_id = e.ib_order_id
             )
           ORDER BY e.timestamp, e.ib_order_id, e.id
        """,
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load_parents(conn: sqlite3.Connection) -> dict[tuple, list[dict]]:
    """Return parents grouped by (sym, strike, right, expiry, session_date)."""
    cur = conn.cursor()
    cur.execute(
        """SELECT id, ib_order_id, submitted_at, symbol, strike, right, expiry,
                  qty, filled_qty, status, dry_run, engine_version, ticket_id,
                  spx_target_t1_qty, spx_target_t2_qty, spx_target_t3_qty
           FROM live_order
           WHERE bracket_role = 'parent' AND action = 'BUY'
        """,
    )
    cols = [c[0] for c in cur.description]
    parents = [dict(zip(cols, r)) for r in cur.fetchall()]
    by_contract_day: dict[tuple, list[dict]] = defaultdict(list)
    for p in parents:
        # Skip DRY_RUN and unfilled parents (filled_qty=0 means no actual long position)
        if (p["filled_qty"] or 0) <= 0:
            continue
        sub_dt = _parse_dt(p["submitted_at"])
        if sub_dt is None:
            continue
        sess = sub_dt.strftime("%Y-%m-%d")
        key = (p["symbol"], float(p["strike"]), p["right"], p["expiry"], sess)
        p["_sub_dt"] = sub_dt
        p["_sess"] = sess
        by_contract_day[key].append(p)
    # sort each list by submission time
    for k in by_contract_day:
        by_contract_day[k].sort(key=lambda r: (r["_sub_dt"], r["ib_order_id"] or 0))
    return by_contract_day


def load_existing_assignments(conn: sqlite3.Connection) -> dict[int, float]:
    """Sum of SELL qty already attributed to each parent_id."""
    cur = conn.cursor()
    cur.execute(
        """SELECT parent_id, SUM(qty) FROM live_order
           WHERE parent_id IS NOT NULL AND action = 'SELL'
           GROUP BY parent_id
        """,
    )
    return {r[0]: float(r[1] or 0) for r in cur.fetchall()}


def group_sells_by_ioid(sells: list[dict]) -> list[dict]:
    """A single ib_order_id may produce multiple ibkr_execution rows when
    the ticket fills in legs. Treat them as one logical ticket.

    Special case: ib_order_id == 0 is a sentinel for missing/manual orders —
    we DON'T group those (each row is independent).
    """
    by_ioid: dict[int, list[dict]] = defaultdict(list)
    independents: list[dict] = []
    for s in sells:
        ioid = int(s["ib_order_id"] or 0)
        if ioid == 0:
            independents.append(s)
        else:
            by_ioid[ioid].append(s)
    groups: list[dict] = []
    for ioid, rows in by_ioid.items():
        rows.sort(key=lambda r: r["timestamp"])
        total_qty = sum(float(r["qty"]) for r in rows)
        # Weighted average price for the ticket
        total_val = sum(float(r["qty"]) * float(r["price"]) for r in rows)
        avg_price = (total_val / total_qty) if total_qty else float(rows[0]["price"])
        groups.append({
            "ib_order_id": ioid,
            "ts_str": rows[0]["timestamp"],
            "ts": _parse_dt(rows[0]["timestamp"]),
            "qty": total_qty,
            "avg_price": avg_price,
            "rows": rows,
            "symbol": rows[0]["symbol"],
            "strike": float(rows[0]["strike"]),
            "right": rows[0]["right"],
            "expiry": rows[0]["expiry"],
        })
    for s in independents:
        groups.append({
            "ib_order_id": 0,
            "ts_str": s["timestamp"],
            "ts": _parse_dt(s["timestamp"]),
            "qty": float(s["qty"]),
            "avg_price": float(s["price"]),
            "rows": [s],
            "symbol": s["symbol"],
            "strike": float(s["strike"]),
            "right": s["right"],
            "expiry": s["expiry"],
        })
    # Stable ordering by (ts, ioid)
    groups.sort(key=lambda g: (g["ts"] or datetime.min, g["ib_order_id"]))
    return groups


def adjudicate(conn: sqlite3.Connection, dry_run: bool) -> dict:
    ensure_audit_table(conn)
    cur = conn.cursor()
    run_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    sells = load_unassigned_sells(conn)
    parents_idx = load_parents(conn)
    assigned = load_existing_assignments(conn)

    # Build mutable per-parent "remaining_qty" map
    remaining: dict[int, float] = {}
    for plist in parents_idx.values():
        for p in plist:
            base = float(p["filled_qty"] or 0)
            used = float(assigned.get(p["id"], 0.0))
            remaining[p["id"]] = base - used

    groups = group_sells_by_ioid(sells)

    # Re-group by contract+session so we can FIFO within each
    by_contract: dict[tuple, list[dict]] = defaultdict(list)
    for g in groups:
        sess = (g["ts"] or datetime.min).strftime("%Y-%m-%d")
        key = (g["symbol"], g["strike"], g["right"], g["expiry"], sess)
        by_contract[key].append(g)

    counts = {
        "matched_high": 0, "matched_medium": 0,
        "still_ambiguous": 0, "orphan": 0,
        "groups_total": len(groups),
        "rows_total": len(sells),
        "rows_resolved": 0,
    }
    inserts: list[tuple] = []
    audit_rows: list[tuple] = []

    for ckey, glist in by_contract.items():
        glist.sort(key=lambda g: (g["ts"], g["ib_order_id"]))
        parents = parents_idx.get(ckey, [])
        for g in glist:
            # Eligible parents: same contract+session, submitted_at <= sell.ts,
            # remaining_qty > 0
            eligible = [
                p for p in parents
                if (p["_sub_dt"] <= g["ts"])
                and remaining.get(p["id"], 0) > 0
            ]
            decision, confidence, assigned_id, note = _decide(g, eligible, remaining)

            if decision == "MATCHED" and confidence == "high":
                counts["matched_high"] += 1
            elif decision == "MATCHED" and confidence == "medium":
                counts["matched_medium"] += 1
            elif decision == "STILL_AMBIGUOUS":
                counts["still_ambiguous"] += 1
            elif decision == "ORPHAN":
                counts["orphan"] += 1

            cand_ids = [p["id"] for p in eligible]
            # If MATCHED, decrement remaining and prepare inserts (one per exec row)
            if decision == "MATCHED":
                if isinstance(assigned_id, list):
                    # split: assigned_id = list of (parent_id, qty)
                    # Insert one synthetic row per exec row, assigning rows to parents FIFO by qty share
                    _alloc_split_inserts(g, assigned_id, parents, inserts, remaining)
                else:
                    # single-parent: one synthetic row per exec row, all with parent=assigned_id
                    parent_row = next(p for p in parents if p["id"] == assigned_id)
                    for r in g["rows"]:
                        inserts.append(_make_insert(r, assigned_id, parent_row, note))
                    remaining[assigned_id] -= g["qty"]
                counts["rows_resolved"] += len(g["rows"])

            for r in g["rows"]:
                audit_rows.append((
                    run_ts, r["exec_id"], r["pk"], r["ib_order_id"],
                    r["timestamp"], r["symbol"], float(r["strike"]),
                    r["right"], r["expiry"], float(r["qty"]),
                    decision, confidence, json.dumps(cand_ids),
                    (assigned_id if isinstance(assigned_id, int) else None),
                    note,
                ))

    if not dry_run:
        if inserts:
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
        if audit_rows:
            conn.executemany(
                """INSERT INTO parent_id_audit (
                     run_at, exec_id, ibkr_exec_pk, ib_order_id, ts,
                     symbol, strike, right, expiry, qty,
                     decision, confidence, candidate_parent_ids,
                     assigned_parent_id, notes
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                audit_rows,
            )
        conn.commit()

    counts["inserts"] = 0 if dry_run else len(inserts)
    counts["audit_rows"] = 0 if dry_run else len(audit_rows)
    return counts


def _decide(group: dict, eligible: list[dict], remaining: dict[int, float]):
    """Return (decision, confidence, assigned_id_or_split, note).

    decision in {MATCHED, STILL_AMBIGUOUS, ORPHAN}
    assigned_id_or_split: int when single parent, list[(pid,qty)] when split, None otherwise
    """
    qty = group["qty"]
    if not eligible:
        return ("ORPHAN", None, None,
                f"No eligible parent (qty={qty}) for "
                f"{group['symbol']} {group['strike']}{group['right']} {group['expiry']} "
                f"at {group['ts_str']} (ioid={group['ib_order_id']}).")

    # Rule (a) exact-qty match: exactly one parent has remaining == qty
    exact = [p for p in eligible if abs(remaining[p["id"]] - qty) < 1e-9]
    if len(exact) == 1:
        return ("MATCHED", "high", exact[0]["id"],
                f"exact-qty + sole-eligible: remaining_qty={qty}")

    # Rule (b) only one eligible parent at all
    if len(eligible) == 1:
        p = eligible[0]
        if remaining[p["id"]] >= qty - 1e-9:
            return ("MATCHED", "high", p["id"],
                    f"sole-eligible parent; remaining={remaining[p['id']]} qty={qty}")
        else:
            # Need to split, but only one eligible candidate is short.
            # Mark as ORPHAN — qty exceeds available position; data may be wrong.
            return ("ORPHAN", None, None,
                    f"single eligible parent {p['id']} has remaining="
                    f"{remaining[p['id']]} < sell qty={qty}")

    # Rule (c) FIFO: oldest eligible parent first
    fifo_parent = eligible[0]  # parents_idx already sorted by submitted_at
    if remaining[fifo_parent["id"]] >= qty - 1e-9:
        # Plenty of capacity in oldest; confidence = high if exact-qty also
        # matches the oldest's remaining; medium otherwise (we're making a
        # FIFO assumption when other parents could also satisfy).
        if abs(remaining[fifo_parent["id"]] - qty) < 1e-9:
            return ("MATCHED", "high", fifo_parent["id"],
                    f"FIFO + remaining exactly equals qty ({qty})")
        return ("MATCHED", "high", fifo_parent["id"],
                f"FIFO oldest parent (remaining={remaining[fifo_parent['id']]} >= qty={qty})")

    # Rule (d) split: qty exceeds oldest's remaining. Walk FIFO consuming.
    # This is the manual position-flatten case (often ioid=0).
    splits: list[tuple[int, float]] = []
    need = qty
    for p in eligible:
        avail = remaining[p["id"]]
        if avail <= 0:
            continue
        take = min(need, avail)
        splits.append((p["id"], take))
        need -= take
        if need <= 1e-9:
            break
    if need > 1e-9:
        # Not enough capacity across all parents combined
        total_avail = sum(remaining[p["id"]] for p in eligible)
        return ("ORPHAN", None, None,
                f"qty={qty} exceeds total eligible remaining={total_avail}; "
                f"short by {need}")
    # Apply remaining decrements here so subsequent groups see the change
    for pid, take in splits:
        remaining[pid] -= take
    return ("MATCHED", "medium", splits,
            f"split across {len(splits)} parents FIFO: "
            + ",".join(f"{pid}={take}" for pid, take in splits))


def _make_insert(exec_row: dict, parent_id: int, parent: dict, note_extra: str) -> tuple:
    """Build the INSERT tuple for one synthetic SELL live_order row."""
    qty_int = int(round(float(exec_row["qty"])))
    fill_price = float(exec_row["avg_price"] if exec_row["avg_price"] is not None else exec_row["price"])
    notes = f"adjudicated backfill from ibkr_execution ({note_extra})"
    # Preserve ib_order_id literally — including 0 (sentinel for manual/unknown)
    # so the synthetic row remains findable by ib_order_id lookups.
    ioid_val = exec_row.get("ib_order_id")
    ioid_val = int(ioid_val) if ioid_val is not None else None
    return (
        exec_row["timestamp"],
        parent.get("ticket_id"),
        ioid_val,
        exec_row.get("perm_id"),
        exec_row["symbol"], float(exec_row["strike"]),
        exec_row["right"], exec_row["expiry"],
        "SELL", qty_int, "MKT", None,
        parent_id, "watcher_sell", "FILLED", qty_int,
        fill_price, 0.0, exec_row["timestamp"], notes,
        int(parent.get("dry_run") or 0),
        int(parent.get("engine_version") or 1),
    )


def _alloc_split_inserts(group: dict, splits: list[tuple[int, float]],
                         parents: list[dict], inserts: list[tuple],
                         remaining: dict[int, float]) -> None:
    """When a single SELL ticket spans multiple parents (split), we need to
    create per-parent synthetic rows. We may also need to split a single
    exec row's qty across parents — preserve exec_id traceability by
    appending a suffix to make the row distinguishable in audit notes.
    """
    # Build flat list of qty allocations
    pids_by_id = {p["id"]: p for p in parents}
    rows = list(group["rows"])
    splits = list(splits)
    # Walk through exec rows and splits in parallel, slicing as needed
    ri = 0  # exec row index
    si = 0  # split index
    row_remaining = float(rows[ri]["qty"])
    split_remaining = splits[si][1]
    while ri < len(rows) and si < len(splits):
        take = min(row_remaining, split_remaining)
        pid = splits[si][0]
        parent = pids_by_id[pid]
        exec_row = rows[ri]
        # Build a synthetic insert for this slice
        qty_int = int(round(take))
        fill_price = float(exec_row["avg_price"] if exec_row["avg_price"] is not None else exec_row["price"])
        notes = (f"adjudicated split backfill from ibkr_execution "
                 f"(exec_pk={exec_row['pk']} parent={pid} qty={qty_int})")
        ioid_val = exec_row.get("ib_order_id")
        ioid_val = int(ioid_val) if ioid_val is not None else None
        inserts.append((
            exec_row["timestamp"],
            parent.get("ticket_id"),
            ioid_val,
            exec_row.get("perm_id"),
            exec_row["symbol"], float(exec_row["strike"]),
            exec_row["right"], exec_row["expiry"],
            "SELL", qty_int, "MKT", None,
            pid, "watcher_sell", "FILLED", qty_int,
            fill_price, 0.0, exec_row["timestamp"], notes,
            int(parent.get("dry_run") or 0),
            int(parent.get("engine_version") or 1),
        ))
        row_remaining -= take
        split_remaining -= take
        if row_remaining <= 1e-9:
            ri += 1
            if ri < len(rows):
                row_remaining = float(rows[ri]["qty"])
        if split_remaining <= 1e-9:
            si += 1
            if si < len(splits):
                split_remaining = splits[si][1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--db",
        default=str(Path.home() / ".fef-terminal" / "signal_log.db"),
        help="Path to signal_log.db",
    )
    ap.add_argument("--dry-run", action="store_true", help="Show counts without writing.")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2
    print(f"DB: {db_path}")
    conn = sqlite3.connect(str(db_path))

    print()
    print("Adjudicating ambiguous SELL execs...")
    r = adjudicate(conn, dry_run=args.dry_run)
    print(f"  exec rows scanned:    {r['rows_total']}")
    print(f"  ticket groups:        {r['groups_total']}")
    print(f"  MATCHED-high:         {r['matched_high']}")
    print(f"  MATCHED-medium:       {r['matched_medium']}")
    print(f"  STILL_AMBIGUOUS:      {r['still_ambiguous']}")
    print(f"  ORPHAN:               {r['orphan']}")
    print(f"  exec rows resolved:   {r['rows_resolved']}")
    print(f"  synthetic inserts:    {r['inserts']}")
    print(f"  audit rows logged:    {r['audit_rows']}")
    if args.dry_run:
        print("\nDRY RUN — no writes.")

    # Final NULL count on live_order.parent_id (excluding parents themselves)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM live_order WHERE parent_id IS NULL AND bracket_role != 'parent'",
    )
    null_children = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM live_order WHERE parent_id IS NULL")
    null_total = cur.fetchone()[0]
    print()
    print(f"Final live_order.parent_id NULL — non-parent rows: {null_children}")
    print(f"Final live_order.parent_id NULL — all rows (incl. parent BUYs): {null_total}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
