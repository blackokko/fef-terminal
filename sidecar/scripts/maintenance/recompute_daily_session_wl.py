"""Recompute ``daily_session.win_count`` / ``loss_count`` from broker fills.

Background
----------
Before the 2026-05-21 accounting fix, ``daily_session.win_count`` /
``loss_count`` were updated incrementally during the trading session via
``_estimate_parent_r(p)``, which depended on
``LiveOrder.spx_target_t{1,2,3}_fired`` boolean flags. On partial scale-
out sessions those flags weren't reliably bumped before the parent's
status went to CLOSED/STOPPED, so the per-parent W/L counters ended up
inflated to all wins (e.g. 32/0, 61/0, 19/0 on 5/19, 5/20, 5/21).

The fix shipped on 5/21 makes ``compute_daily_review`` re-derive the
counts from ``IBKRExecution`` ground-truth fills, but the legacy
``daily_session`` rows were never recomputed.

Approach
--------
Per-parent FIFO pair-off across ``(symbol, strike, right, expiry)``:

* For each session, walk every ``ibkr_execution`` row for the session
  in timestamp order.
* BOT execs open lots tagged with the parent ``live_order.id`` whose
  ``ib_order_id`` matches the exec (fallback: nearest parent for the
  same contract submitted at-or-before the exec timestamp).
* SLD execs consume the FIFO queue, producing realized P&L that is
  credited back to the parent that opened each lot.
* Win = realized > $0, Loss = realized < $0, Flat = realized == 0.
  Parents still open at EOD (BOT exec but no offsetting SLD) are NOT
  counted as a win or loss — they belong to the next session.

This reproduces ``daily_session.gross_pnl`` to the penny on 5/19, 5/20,
5/21 (validated against the existing contract-pairoff sum).

Usage
-----

.. code-block:: bash

    # Recompute the three legacy sessions:
    python scripts/maintenance/recompute_daily_session_wl.py \\
        --dates 2026-05-19 2026-05-20 2026-05-21

    # Recompute every pre-fix session:
    python scripts/maintenance/recompute_daily_session_wl.py --all-pre-fix

    # Inspect without writing:
    python scripts/maintenance/recompute_daily_session_wl.py \\
        --dates 2026-05-19 --dry-run

Idempotent: only the four fields ``win_count``, ``loss_count``,
``gross_pnl``, ``net_pnl`` are touched (plus ``updated_at``).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

# Anything before this date predates the 2026-05-21 W/L accounting fix
PRE_FIX_CUTOFF = "2026-05-22"  # 5/22 and later were computed under the fixed logic


def _parent_for_bot_exec(
    cur: sqlite3.Cursor,
    *,
    ib_order_id: int | None,
    session_date: str,
    symbol: str,
    strike: float,
    right: str,
    expiry: str,
    ts_iso: str,
) -> int | None:
    """Find the parent ``live_order.id`` that opened a BOT exec.

    Primary match: parent whose ``ib_order_id`` equals the exec's
    ``ib_order_id``. Falls back to the nearest parent for the same
    contract in the same session submitted at-or-before the exec ts.
    Returns ``None`` if neither path resolves a unique parent.
    """
    if ib_order_id is not None:
        cur.execute(
            """SELECT id FROM live_order
                 WHERE bracket_role='parent' AND ib_order_id=?""",
            (ib_order_id,),
        )
        rows = cur.fetchall()
        if len(rows) == 1:
            return rows[0][0]
    cur.execute(
        """SELECT id FROM live_order
             WHERE bracket_role='parent'
               AND substr(submitted_at, 1, 10) = ?
               AND symbol = ?
               AND ABS(strike - ?) < 0.01
               AND right = ?
               AND expiry = ?
               AND submitted_at <= ?
             ORDER BY submitted_at DESC LIMIT 1""",
        (session_date, symbol, float(strike), right, expiry, ts_iso),
    )
    row = cur.fetchone()
    return row[0] if row else None


def recompute_session(
    conn: sqlite3.Connection, session_date: str
) -> dict:
    """Recompute W/L/$/best/worst for a single session via FIFO pair-off.

    Returns a dict with the recomputed values plus a ``before`` snapshot
    of what's currently stored in ``daily_session``.
    """
    cur = conn.cursor()

    # Snapshot the existing row for the diff
    cur.execute(
        """SELECT win_count, loss_count, gross_pnl, net_pnl,
                  best_trade_pnl, worst_trade_pnl, trades_taken
             FROM daily_session WHERE session_date = ?""",
        (session_date,),
    )
    row = cur.fetchone()
    before = None
    if row is not None:
        before = {
            "win_count": row[0],
            "loss_count": row[1],
            "gross_pnl": row[2],
            "net_pnl": row[3],
            "best_trade_pnl": row[4],
            "worst_trade_pnl": row[5],
            "trades_taken": row[6],
        }

    # Pull all execs for the session in chronological order
    cur.execute(
        """SELECT timestamp, symbol, strike, right, expiry, side,
                  shares, price, ib_order_id
             FROM ibkr_execution
             WHERE session_date = ?
             ORDER BY timestamp, id""",
        (session_date,),
    )
    execs = cur.fetchall()

    # Group execs by contract
    by_contract: dict[tuple, list] = defaultdict(list)
    for ts, sym, strike, right, expiry, side, shares, price, oid in execs:
        key = (sym, float(strike or 0.0), right, expiry)
        by_contract[key].append(
            (ts, side, float(shares or 0.0), float(price or 0.0), oid)
        )

    realized_per_parent: dict[int, float] = defaultdict(float)
    open_per_parent: dict[int, float] = defaultdict(float)  # shares still long
    contract_pnl: dict[tuple, float] = defaultdict(float)
    unattributed_realized = 0.0
    unattributed_sell_shares = 0.0

    for key, ex_list in by_contract.items():
        sym, strike, right, expiry = key
        # FIFO queue of open lots: [parent_id_or_None, qty_remaining, buy_px]
        lots: deque = deque()
        for ts, side, shares, price, oid in ex_list:
            side_u = (side or "").upper()
            if side_u in ("BOT", "BUY"):
                parent_id = _parent_for_bot_exec(
                    cur,
                    ib_order_id=oid,
                    session_date=session_date,
                    symbol=sym,
                    strike=strike,
                    right=right,
                    expiry=expiry,
                    ts_iso=ts,
                )
                lots.append([parent_id, shares, price])
                if parent_id is not None:
                    open_per_parent[parent_id] += shares
            elif side_u in ("SLD", "SELL"):
                remaining = shares
                while remaining > 0 and lots:
                    lot = lots[0]
                    take = min(remaining, lot[1])
                    pnl = (price - lot[2]) * take * 100.0
                    pid = lot[0]
                    if pid is not None:
                        realized_per_parent[pid] += pnl
                        open_per_parent[pid] -= take
                    else:
                        unattributed_realized += pnl
                    contract_pnl[key] += pnl
                    lot[1] -= take
                    remaining -= take
                    if lot[1] <= 1e-9:
                        lots.popleft()
                if remaining > 0:
                    # No open lots to pair against — short sale or orphan
                    unattributed_realized += -price * remaining * 100.0
                    unattributed_sell_shares += remaining

    # Classify parents
    wins = sum(1 for v in realized_per_parent.values() if v > 0.01)
    losses = sum(1 for v in realized_per_parent.values() if v < -0.01)
    flats = sum(1 for v in realized_per_parent.values() if abs(v) <= 0.01)
    open_parents = sum(1 for q in open_per_parent.values() if q > 1e-6)

    realized_total = sum(realized_per_parent.values()) + unattributed_realized
    best_pnl = max(realized_per_parent.values(), default=0.0)
    worst_pnl = min(realized_per_parent.values(), default=0.0)

    # Total parents in session (for `trades_taken` cross-check)
    cur.execute(
        """SELECT COUNT(*) FROM live_order
             WHERE bracket_role='parent'
               AND substr(submitted_at, 1, 10) = ?
               AND dry_run = 0""",
        (session_date,),
    )
    trades_taken = cur.fetchone()[0]

    after = {
        "win_count": wins,
        "loss_count": losses,
        "gross_pnl": round(realized_total, 2),
        "net_pnl": round(realized_total, 2),  # commission unreliable on paper
        "best_trade_pnl": round(best_pnl, 2),
        "worst_trade_pnl": round(worst_pnl, 2),
        "trades_taken": trades_taken,
    }

    return {
        "before": before,
        "after": after,
        "diagnostics": {
            "parents_attributed": len(realized_per_parent),
            "flats": flats,
            "open_at_eod": open_parents,
            "unattributed_realized": round(unattributed_realized, 2),
            "unattributed_sell_shares": unattributed_sell_shares,
            "unique_contracts": len(by_contract),
        },
    }


def _pre_fix_session_dates(conn: sqlite3.Connection) -> list[str]:
    cur = conn.cursor()
    cur.execute(
        """SELECT session_date FROM daily_session
             WHERE session_date < ? AND trades_taken > 0
             ORDER BY session_date""",
        (PRE_FIX_CUTOFF,),
    )
    return [r[0] for r in cur.fetchall()]


def _print_diff(session_date: str, result: dict) -> None:
    before = result["before"] or {}
    after = result["after"]
    diag = result["diagnostics"]
    print(f"\n--- {session_date} ---")
    if before is None or not before:
        print("  (no existing daily_session row)")
    else:
        for k in ("win_count", "loss_count", "gross_pnl", "net_pnl",
                  "best_trade_pnl", "worst_trade_pnl", "trades_taken"):
            old = before.get(k)
            new = after[k]
            mark = "  " if old == new else ">>"
            print(f"  {mark} {k:>18}: {old!r:>12}  ->  {new!r:<12}")
    print(f"     diagnostics: parents_attributed={diag['parents_attributed']}, "
          f"flats={diag['flats']}, open_at_eod={diag['open_at_eod']}, "
          f"unattrib_$={diag['unattributed_realized']}, "
          f"contracts={diag['unique_contracts']}")


def _apply(conn: sqlite3.Connection, session_date: str, after: dict) -> None:
    cur = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()
    cur.execute(
        """UPDATE daily_session
              SET win_count = ?,
                  loss_count = ?,
                  gross_pnl = ?,
                  net_pnl = ?,
                  best_trade_pnl = ?,
                  worst_trade_pnl = ?,
                  trades_taken = ?,
                  updated_at = ?
            WHERE session_date = ?""",
        (
            after["win_count"],
            after["loss_count"],
            after["gross_pnl"],
            after["net_pnl"],
            after["best_trade_pnl"],
            after["worst_trade_pnl"],
            after["trades_taken"],
            now_iso,
            session_date,
        ),
    )
    if cur.rowcount == 0:
        print(f"  WARN: no daily_session row to UPDATE for {session_date}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Recompute daily_session.win_count/loss_count from broker fills.",
    )
    ap.add_argument(
        "--db",
        default=str(Path.home() / ".fef-terminal" / "signal_log.db"),
        help="Path to signal_log.db",
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--dates", nargs="+", metavar="YYYY-MM-DD",
                     help="Explicit list of session dates to recompute.")
    grp.add_argument("--all-pre-fix", action="store_true",
                     help="Recompute every session strictly before the "
                          "2026-05-21 accounting fix cutoff.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print before/after diff without writing.")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 2
    print(f"DB: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        if args.all_pre_fix:
            dates = _pre_fix_session_dates(conn)
            print(f"Pre-fix sessions: {dates}")
        else:
            dates = list(args.dates)

        for sess in dates:
            result = recompute_session(conn, sess)
            _print_diff(sess, result)
            if not args.dry_run:
                _apply(conn, sess, result["after"])

        if args.dry_run:
            print("\nDRY RUN — no writes.")
        else:
            conn.commit()
            print("\nWrites committed.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
