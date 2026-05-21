"""Realistic-fill P/L estimator — paper theoretical vs IBKR-fill ground truth.

Measures the empirical paper-to-real haircut per session. Tonight's 5/21
audit surfaced an 80% lookahead-WR vs 24% execution-WR gap. Claude Chat's
2026-05-20 review warned that execution-based edges typically degrade
50-80% in the paper-to-live transition vs 10-20% for signal-based edges.
This estimator quantifies that delta on whatever sessions are already in
the DB.

Spec: DEFERRED_CHANGES.md, section
  "2026-05-20 — Realistic-fill estimator [FREEZE-SAFE — PRIORITY #0 for
   sessions 4-10]"

What this v1 does (phase B of the spec):
  - Paper P/L (lookahead-theoretical): SUM per fired LONG_CALLS/LONG_PUTS
    signal of +1R*$DOLLARS_PER_R when T1 hit (and not stop), -1R when
    stop hit (and not T1), 0 otherwise. Source: signal_lookahead_outcome
    at the 60-minute horizon.
  - Real P/L (IBKR fills): pair-off realized from ibkr_execution,
    grouped by (symbol, strike, right, expiry). Mirrors the SQL in
    sidecar/src/fef_sidecar/engine/daily_review.py lines 433-490 (the
    2026-05-21 fix). Open BOT rows contribute to open_cost, NOT realized.
  - Haircut = Paper - Real, both in $ and as a percentage.
  - Optional --verbose breaks the real_$ figure out per parent (by
    contract group) so the user can audit individual trades.

What this v1 does NOT do (phase A of the spec — deferred):
  - It does not capture exit-side L1 bid/ask (that needs runtime
    additions to the spx_target_watcher / BE-ladder code paths).
  - It does not simulate a worse-side spread fill — once phase A
    captures exit-side books for new sessions, a v2 of this script can
    re-walk paper exits at (ask + $0.05) / (bid - $0.05) and produce a
    third "realistic-fill (spread-model)" column.
  - It does not persist DailyReview.realistic_fill_dollars (that's a
    schema migration the spec explicitly defers).

Known data-quality caveat (per DEFERRED_CHANGES.md "Bracket child orders
not persisted"): only parent BUY rows show up in live_order. Child
T1/T2/T3/STOP SELL fills go to ibkr_execution but cannot be linked to a
specific parent_id. So per-parent real-$ is pro-rated by paired-BOT-qty
within each contract group rather than truly per-parent.

CLI:
    python realistic_fill_estimator.py 2026-05-19 2026-05-21
    python realistic_fill_estimator.py 2026-05-19 2026-05-21 --verbose
    python realistic_fill_estimator.py 2026-05-19 2026-05-21 --json
    python realistic_fill_estimator.py 2026-05-19 2026-05-21 --out fills.md

Programmatic:
    from realistic_fill_estimator import run_realistic_fill_estimator
    result = run_realistic_fill_estimator("2026-05-19", "2026-05-21")
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DEFAULT_DB = str(Path.home() / ".fef-terminal" / "signal_log.db")
DOLLARS_PER_R = 400.0  # 5 contracts x $2000 risk per trade (matches ablation_backtest.py)


def _parse_account_class_arg(raw: str) -> set[str]:
    if not raw:
        return {"paper"}
    raw = raw.strip().lower()
    if raw == "all":
        return {"paper", "live", "mock", "unknown"}
    return {x.strip() for x in raw.split(",") if x.strip()}


def _passes_account_filter(extras_raw, accept: set[str]) -> bool:
    """Return True if the row's extras.account_class is in accept.
    'unknown' (pre-tag rows) is treated as paper-equivalent when paper
    accepted. Matches the convention from ablation_backtest.py."""
    if not extras_raw:
        return ("paper" in accept) or ("unknown" in accept)
    try:
        ex = json.loads(extras_raw) if isinstance(extras_raw, str) else extras_raw
    except json.JSONDecodeError:
        return ("paper" in accept) or ("unknown" in accept)
    cls = (ex.get("account_class") or "unknown") if isinstance(ex, dict) else "unknown"
    if cls in accept:
        return True
    if cls == "unknown" and "paper" in accept:
        return True
    return False


def _session_date_of(submitted_at_raw: str) -> str:
    """Extract YYYY-MM-DD from a SQLite-stored datetime string. live_order
    has no session_date column — daily_review derives it from submitted_at
    (see daily_review.py line 181), so we mirror that."""
    if not submitted_at_raw:
        return ""
    # ISO format: "2026-05-19 14:12:18.780812" or "2026-05-19T14:12:18"
    return submitted_at_raw[:10]


def _paper_r_from_lookahead(hit_t1: bool, hit_stop: bool) -> float:
    """Lookahead-theoretical R per fired signal. Matches the per-spec
    rule: T1 hit (not stop) -> +1R, stop hit (not T1) -> -1R, both/neither
    -> 0. The both-hit case is rare on 60-min walks and the lookahead
    walker resolves ambiguous bars conservatively, so we treat 'both
    flagged' as a wash (0R) rather than guessing order."""
    if hit_t1 and not hit_stop:
        return +1.0
    if hit_stop and not hit_t1:
        return -1.0
    return 0.0


def run_realistic_fill_estimator(
    date_from: str,
    date_to: str,
    db_path: str = DEFAULT_DB,
    accept_classes: set[str] | None = None,
    dollars_per_r: float = DOLLARS_PER_R,
) -> dict:
    """Compute paper-theoretical vs IBKR-realized P/L per session.

    Returns a dict with:
      - sessions: list of per-day rows
        {date, n_signals, paper_dollars, real_dollars, open_cost_dollars,
         haircut_dollars, haircut_pct, contracts: list of per-contract
         pair-off rows, parents: list of per-parent rows}
      - totals: cross-session aggregate
    """
    accept = accept_classes or {"paper"}
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    # ----------- Paper-theoretical leg (signal_iter + lookahead@60min) -----------
    raw_signals = cur.execute(
        """
        SELECT id, session_date, direction, extras
        FROM signal_iter
        WHERE session_date BETWEEN ? AND ?
          AND direction IN ('LONG_CALLS','LONG_PUTS')
        ORDER BY session_date, timestamp
        """,
        (date_from, date_to),
    ).fetchall()
    signals = [s for s in raw_signals if _passes_account_filter(s["extras"], accept)]
    sig_ids = [s["id"] for s in signals]
    sig_id_to_date = {s["id"]: s["session_date"] for s in signals}

    look_by_sid: dict[int, sqlite3.Row] = {}
    if sig_ids:
        # Chunk in groups of 900 to stay under SQLite's variable cap.
        for i in range(0, len(sig_ids), 900):
            chunk = sig_ids[i : i + 900]
            placeholders = ",".join("?" * len(chunk))
            for r in cur.execute(
                f"""
                SELECT signal_iter_id, would_have_hit_t1, would_have_hit_stop
                FROM signal_lookahead_outcome
                WHERE lookahead_minutes = 60
                  AND signal_iter_id IN ({placeholders})
                """,
                chunk,
            ):
                look_by_sid[r["signal_iter_id"]] = r

    paper_r_by_date: dict[str, float] = defaultdict(float)
    paper_n_by_date: dict[str, int] = defaultdict(int)
    paper_wins_by_date: dict[str, int] = defaultdict(int)
    paper_losses_by_date: dict[str, int] = defaultdict(int)
    paper_flats_by_date: dict[str, int] = defaultdict(int)
    for s in signals:
        date = s["session_date"]
        paper_n_by_date[date] += 1
        look = look_by_sid.get(s["id"])
        if not look:
            paper_flats_by_date[date] += 1
            continue
        r = _paper_r_from_lookahead(bool(look["would_have_hit_t1"]),
                                    bool(look["would_have_hit_stop"]))
        paper_r_by_date[date] += r
        if r > 0.05:
            paper_wins_by_date[date] += 1
        elif r < -0.05:
            paper_losses_by_date[date] += 1
        else:
            paper_flats_by_date[date] += 1

    # ----------- Real (IBKR-fill) leg: pair-off by contract group -----------
    # Mirror daily_review.py:464-489 — group ibkr_execution rows by
    # (symbol, strike, right, expiry), pair min(bot_qty, sld_qty),
    # realized = (avg_sld - avg_bot) * paired_qty * 100.
    real_by_date: dict[str, dict] = {}
    for date in _date_range(date_from, date_to):
        execs = cur.execute(
            """
            SELECT symbol, strike, right, expiry, side, shares, price, ib_order_id
            FROM ibkr_execution
            WHERE session_date = ?
            ORDER BY timestamp
            """,
            (date,),
        ).fetchall()
        by_contract: dict[tuple, dict[str, float]] = {}
        for e in execs:
            key = (e["symbol"], float(e["strike"] or 0), e["right"], e["expiry"])
            slot = by_contract.setdefault(
                key, {"bot_v": 0.0, "sld_v": 0.0, "bot_q": 0.0, "sld_q": 0.0}
            )
            side = (e["side"] or "").upper()
            sh = float(e["shares"] or 0)
            pr = float(e["price"] or 0)
            if side == "BOT":
                slot["bot_v"] += sh * pr
                slot["bot_q"] += sh
            elif side == "SLD":
                slot["sld_v"] += sh * pr
                slot["sld_q"] += sh
        realized = 0.0
        open_cost = 0.0
        contract_rows: list[dict] = []
        for key, slot in by_contract.items():
            bot_q, sld_q = slot["bot_q"], slot["sld_q"]
            if bot_q == 0 and sld_q == 0:
                continue
            paired = min(bot_q, sld_q)
            open_q = bot_q - sld_q
            avg_bot = slot["bot_v"] / bot_q if bot_q > 0 else 0.0
            avg_sld = slot["sld_v"] / sld_q if sld_q > 0 else 0.0
            row_realized = (avg_sld - avg_bot) * paired * 100 if paired > 0 else 0.0
            row_open_cost = avg_bot * open_q * 100 if open_q > 0 else 0.0
            realized += row_realized
            open_cost += row_open_cost
            contract_rows.append({
                "symbol": key[0], "strike": key[1], "right": key[2], "expiry": key[3],
                "bot_qty": bot_q, "sld_qty": sld_q, "paired_qty": paired,
                "avg_bot": round(avg_bot, 3), "avg_sld": round(avg_sld, 3),
                "realized_dollars": round(row_realized, 2),
                "open_qty": max(0.0, open_q),
                "open_cost_dollars": round(row_open_cost, 2),
            })
        real_by_date[date] = {
            "realized_dollars": realized,
            "open_cost_dollars": open_cost,
            "contracts": contract_rows,
        }

    # ----------- Per-parent breakdown (verbose-mode detail) -----------
    # We can match parent live_order rows to ibkr_execution by ib_order_id
    # for the entry (BOT) leg. Exit (SLD) fills do NOT carry a parent
    # linkage (bracket child orders aren't persisted) — see the docstring
    # caveat. For each parent we report:
    #   - paper_theoretical_$: signal's lookahead-R * dollars_per_r (only
    #     if we can link parent -> ticket -> signal_iter -> lookahead).
    #   - real_realized_$: pro-rated share of the parent's contract-group
    #     realized $, weighted by this parent's paired BOT qty within the
    #     group. When only one parent participates in a contract group on
    #     a given session, this is exact.
    parents = cur.execute(
        """
        SELECT lo.id AS parent_id, lo.submitted_at, lo.ib_order_id,
               lo.ticket_id, lo.symbol, lo.strike, lo.right, lo.expiry,
               lo.qty, lo.filled_qty, lo.status, lo.avg_fill_price,
               lo.entry_spx_price, lo.spx_target_t1, lo.spx_target_stop,
               tt.signal_iter_id
        FROM live_order lo
        LEFT JOIN trade_ticket tt ON tt.id = lo.ticket_id
        WHERE lo.bracket_role = 'parent'
          AND lo.dry_run = 0
        ORDER BY lo.submitted_at
        """
    ).fetchall()
    parents = [p for p in parents
               if date_from <= _session_date_of(p["submitted_at"]) <= date_to]

    # For each parent, fetch its entry-side fills (BOT executions matching
    # ib_order_id) to get a per-parent paired_qty. Children can't be
    # linked, so we pro-rate the contract-group realized $ by the parent's
    # paired-BOT share.
    parents_by_date: dict[str, list[dict]] = defaultdict(list)
    for p in parents:
        date = _session_date_of(p["submitted_at"])
        # Pull this parent's entry executions
        entry_execs = cur.execute(
            """
            SELECT shares, price, side
            FROM ibkr_execution
            WHERE ib_order_id = ? AND session_date = ? AND side = 'BOT'
            """,
            (p["ib_order_id"], date),
        ).fetchall()
        parent_bot_q = sum(float(e["shares"] or 0) for e in entry_execs)
        parent_bot_v = sum(float(e["shares"] or 0) * float(e["price"] or 0)
                           for e in entry_execs)
        parent_avg_bot = parent_bot_v / parent_bot_q if parent_bot_q > 0 else None

        # Find this parent's contract group within real_by_date
        key = (p["symbol"], float(p["strike"] or 0), p["right"], p["expiry"])
        group_realized = 0.0
        group_paired = 0.0
        group_bot_total = 0.0
        for c in real_by_date.get(date, {}).get("contracts", []):
            if (c["symbol"], c["strike"], c["right"], c["expiry"]) == key:
                group_realized = c["realized_dollars"]
                group_paired = c["paired_qty"]
                group_bot_total = c["bot_qty"]
                break

        # Pro-rate: weight group_realized by this parent's share of the
        # group's BOT qty. Exact when one parent per contract-per-session,
        # approximate otherwise (children aren't persisted with parent
        # linkage — see docstring caveat).
        if group_bot_total > 0 and parent_bot_q > 0:
            share = parent_bot_q / group_bot_total
            parent_real = group_realized * share
        else:
            parent_real = 0.0

        # Paper-theoretical: needs a signal -> lookahead link.
        parent_paper = 0.0
        paper_label = "no_signal_link"
        if p["signal_iter_id"] is not None and p["signal_iter_id"] in look_by_sid:
            look = look_by_sid[p["signal_iter_id"]]
            r = _paper_r_from_lookahead(bool(look["would_have_hit_t1"]),
                                        bool(look["would_have_hit_stop"]))
            parent_paper = r * dollars_per_r
            paper_label = (
                "t1" if (r > 0.05) else ("stop" if (r < -0.05) else "flat")
            )

        parents_by_date[date].append({
            "parent_id": p["parent_id"],
            "submitted_at": p["submitted_at"],
            "ib_order_id": p["ib_order_id"],
            "signal_iter_id": p["signal_iter_id"],
            "contract": f"{p['symbol']} {int(p['strike'])} {p['right']} {p['expiry']}",
            "qty": int(p["qty"] or 0),
            "filled_qty": int(p["filled_qty"] or 0),
            "status": p["status"],
            "parent_bot_qty": parent_bot_q,
            "parent_avg_bot": round(parent_avg_bot, 3) if parent_avg_bot else None,
            "paper_theoretical_dollars": round(parent_paper, 2),
            "paper_label": paper_label,
            "real_realized_dollars": round(parent_real, 2),
            "haircut_dollars": round(parent_paper - parent_real, 2),
        })

    # ----------- Roll up per-session summary -----------
    sessions: list[dict] = []
    for date in _date_range(date_from, date_to):
        paper_r = paper_r_by_date.get(date, 0.0)
        paper_dollars = paper_r * dollars_per_r
        real_dollars = real_by_date.get(date, {}).get("realized_dollars", 0.0)
        open_cost = real_by_date.get(date, {}).get("open_cost_dollars", 0.0)
        haircut_dollars = paper_dollars - real_dollars
        haircut_pct = (haircut_dollars / paper_dollars * 100) if paper_dollars else None
        sessions.append({
            "date": date,
            "n_signals": paper_n_by_date.get(date, 0),
            "paper_wins": paper_wins_by_date.get(date, 0),
            "paper_losses": paper_losses_by_date.get(date, 0),
            "paper_flats": paper_flats_by_date.get(date, 0),
            "paper_sum_r": round(paper_r, 2),
            "paper_dollars": round(paper_dollars, 2),
            "real_dollars": round(real_dollars, 2),
            "open_cost_dollars": round(open_cost, 2),
            "haircut_dollars": round(haircut_dollars, 2),
            "haircut_pct": round(haircut_pct, 1) if haircut_pct is not None else None,
            "n_parents": len(parents_by_date.get(date, [])),
            "contracts": real_by_date.get(date, {}).get("contracts", []),
            "parents": parents_by_date.get(date, []),
        })

    # ----------- Cross-session totals -----------
    total_paper = sum(s["paper_dollars"] for s in sessions)
    total_real = sum(s["real_dollars"] for s in sessions)
    total_haircut = total_paper - total_real
    totals = {
        "n_signals": sum(s["n_signals"] for s in sessions),
        "n_parents": sum(s["n_parents"] for s in sessions),
        "paper_dollars": round(total_paper, 2),
        "real_dollars": round(total_real, 2),
        "haircut_dollars": round(total_haircut, 2),
        "haircut_pct": round(total_haircut / total_paper * 100, 1) if total_paper else None,
        "open_cost_dollars": round(sum(s["open_cost_dollars"] for s in sessions), 2),
    }

    return {
        "date_from": date_from,
        "date_to": date_to,
        "dollars_per_r": dollars_per_r,
        "accept_classes": sorted(accept),
        "sessions": sessions,
        "totals": totals,
    }


def _date_range(date_from: str, date_to: str) -> list[str]:
    """Inclusive [date_from, date_to] iterator as YYYY-MM-DD strings."""
    from datetime import timedelta
    d0 = datetime.fromisoformat(date_from)
    d1 = datetime.fromisoformat(date_to)
    out: list[str] = []
    cur = d0
    while cur <= d1:
        out.append(cur.strftime("%Y-%m-%d"))
        cur = cur + timedelta(days=1)
    return out


def format_report(result: dict, verbose: bool = False) -> str:
    """Markdown report string. verbose=True adds per-parent and
    per-contract breakdowns under each session."""
    lines: list[str] = []
    lines.append(f"## Realistic-Fill Estimator: {result['date_from']} to {result['date_to']}")
    lines.append("")
    lines.append(
        f"Account-class filter: `{','.join(result['accept_classes'])}` | "
        f"$/R: ${result['dollars_per_r']:.0f}"
    )
    lines.append("")
    lines.append(
        "Paper = lookahead-theoretical (T1 hit = +1R, stop hit = -1R, "
        "neither = 0). Real = IBKR-fill pair-off (mirrors daily_review)."
    )
    lines.append("")

    # ----- Per-session table -----
    lines.append("### Per-session summary")
    lines.append("")
    lines.append(
        "| Date | n sigs | paper W/L/flat | paper sum R | paper $ | "
        "real $ | haircut $ | haircut % | open cost $ | n parents |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in result["sessions"]:
        wlf = f"{s['paper_wins']}/{s['paper_losses']}/{s['paper_flats']}"
        haircut_pct = f"{s['haircut_pct']:+.1f}%" if s['haircut_pct'] is not None else "n/a"
        lines.append(
            f"| {s['date']} | {s['n_signals']} | {wlf} | "
            f"{s['paper_sum_r']:+.2f} | {s['paper_dollars']:+,.0f} | "
            f"{s['real_dollars']:+,.0f} | {s['haircut_dollars']:+,.0f} | "
            f"{haircut_pct} | {s['open_cost_dollars']:,.0f} | {s['n_parents']} |"
        )
    lines.append("")

    # ----- Totals -----
    t = result["totals"]
    lines.append("### Totals")
    lines.append("")
    lines.append(f"- Fired signals (paper-class): **{t['n_signals']}**")
    lines.append(f"- Live parent orders: **{t['n_parents']}**")
    lines.append(f"- Paper P/L (lookahead-theoretical): **${t['paper_dollars']:+,.0f}**")
    lines.append(f"- Real P/L (IBKR fills, pair-off): **${t['real_dollars']:+,.0f}**")
    lines.append(
        f"- Haircut: **${t['haircut_dollars']:+,.0f}** "
        f"({(str(t['haircut_pct']) + '%') if t['haircut_pct'] is not None else 'n/a'} of paper)"
    )
    lines.append(f"- Open-position cost (carried to next session): ${t['open_cost_dollars']:,.0f}")
    lines.append("")

    if not verbose:
        return "\n".join(lines)

    # ----- Per-parent breakdown -----
    lines.append("### Per-parent breakdown (verbose)")
    lines.append("")
    lines.append(
        "Real-$ is pro-rated by parent's BOT-qty share within its "
        "contract group (exact when one parent per contract per session, "
        "approximate otherwise — bracket children aren't persisted with "
        "parent_id linkage)."
    )
    lines.append("")
    for s in result["sessions"]:
        if not s["parents"]:
            continue
        lines.append(f"#### {s['date']} — {len(s['parents'])} parents")
        lines.append("")
        lines.append(
            "| parent_id | contract | qty | status | paper $ | "
            "paper label | real $ | haircut $ |"
        )
        lines.append("|---:|---|---:|---|---:|---|---:|---:|")
        for p in s["parents"]:
            lines.append(
                f"| {p['parent_id']} | {p['contract']} | {p['qty']} | "
                f"{p['status']} | {p['paper_theoretical_dollars']:+,.0f} | "
                f"{p['paper_label']} | {p['real_realized_dollars']:+,.0f} | "
                f"{p['haircut_dollars']:+,.0f} |"
            )
        lines.append("")

    # ----- Per-contract pair-off detail -----
    lines.append("### Per-contract pair-off (verbose)")
    lines.append("")
    for s in result["sessions"]:
        if not s["contracts"]:
            continue
        lines.append(f"#### {s['date']} — {len(s['contracts'])} contracts")
        lines.append("")
        lines.append(
            "| contract | BOT qty | SLD qty | paired | avg BOT | "
            "avg SLD | realized $ | open qty | open cost $ |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for c in s["contracts"]:
            contract = f"{c['symbol']} {int(c['strike'])} {c['right']} {c['expiry']}"
            lines.append(
                f"| {contract} | {c['bot_qty']:.0f} | {c['sld_qty']:.0f} | "
                f"{c['paired_qty']:.0f} | {c['avg_bot']:.2f} | "
                f"{c['avg_sld']:.2f} | {c['realized_dollars']:+,.0f} | "
                f"{c['open_qty']:.0f} | {c['open_cost_dollars']:,.0f} |"
            )
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Realistic-fill estimator: paper-theoretical vs IBKR-realized P/L."
    )
    ap.add_argument("date_from", help="YYYY-MM-DD")
    ap.add_argument("date_to", help="YYYY-MM-DD")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--account-class", default="paper",
                    help="Filter by extras.account_class. 'paper' (default), 'live', "
                         "'mock', 'unknown', 'all', or comma-separated.")
    ap.add_argument("--dollars-per-r", type=float, default=DOLLARS_PER_R,
                    help=f"$ per 1R (default {DOLLARS_PER_R:.0f}).")
    ap.add_argument("--json", action="store_true", help="Emit raw JSON instead of markdown.")
    ap.add_argument("--verbose", action="store_true",
                    help="Include per-parent + per-contract breakdowns in markdown.")
    ap.add_argument("--out", default=None, help="Write report to file instead of stdout.")
    args = ap.parse_args()

    accept = _parse_account_class_arg(args.account_class)
    result = run_realistic_fill_estimator(
        args.date_from, args.date_to, args.db, accept, args.dollars_per_r,
    )
    if args.json:
        body = json.dumps(result, indent=2, default=str)
    else:
        body = format_report(result, verbose=args.verbose)

    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(f"Wrote {args.out} ({len(body)} bytes)")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
