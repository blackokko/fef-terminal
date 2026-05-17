"""End-of-day summary generator.

Pulls today's session from signal_log.db and produces a markdown summary
(also exits with stats useful for piping to Discord/email).

Usage:
    python scripts/daily_summary.py [--date YYYY-MM-DD] [--out FILE]

If --out is omitted, writes to ~/.fef-terminal/summaries/YYYY-MM-DD.md.

IMPROVEMENTS #P.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = Path.home() / ".fef-terminal" / "signal_log.db"
DEFAULT_OUT_DIR = Path.home() / ".fef-terminal" / "summaries"


def _today_et() -> str:
    et = timezone(timedelta(hours=-4))
    return datetime.now(et).strftime("%Y-%m-%d")


def generate(db_path: Path, date: str) -> str:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found at {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    daily = conn.execute(
        "SELECT * FROM daily_session WHERE session_date = ?", (date,)
    ).fetchone()

    iters = conn.execute(
        "SELECT * FROM signal_iter WHERE session_date = ? ORDER BY iter_num", (date,)
    ).fetchall()

    actionable = [r for r in iters if r["direction"] != "WAIT"]
    high_conf = [r for r in actionable if r["confidence"] == "HIGH"]

    tickets = conn.execute(
        """
        SELECT t.* FROM trade_ticket t
        JOIN signal_iter s ON t.signal_iter_id = s.id
        WHERE s.session_date = ?
        ORDER BY t.created_at
        """,
        (date,),
    ).fetchall()

    closed = [t for t in tickets if t["status"] in ("CLOSED", "STOPPED")]
    total_pnl = sum((t["pnl_dollars"] or 0) for t in closed)
    wins = [t for t in closed if (t["pnl_dollars"] or 0) > 0]
    losses = [t for t in closed if (t["pnl_dollars"] or 0) < 0]

    conn.close()

    lines = [
        f"# FEF Terminal — Session Summary: {date}",
        "",
        "## Activity",
        f"- Total iters: **{len(iters)}**",
        f"- Actionable verdicts: **{len(actionable)}** ({100 * len(actionable) / max(1, len(iters)):.1f}%)",
        f"- HIGH-conviction fires: **{len(high_conf)}**",
        f"- Tickets created: **{len(tickets)}**",
        f"- Tickets closed: **{len(closed)}**",
        "",
        "## P/L",
        f"- Net P/L: **${total_pnl:+,.2f}**",
        f"- Wins: {len(wins)} ({'+' if sum((t['pnl_dollars'] or 0) for t in wins) >= 0 else ''}${sum((t['pnl_dollars'] or 0) for t in wins):,.2f})",
        f"- Losses: {len(losses)} (${sum((t['pnl_dollars'] or 0) for t in losses):,.2f})",
    ]

    if wins:
        best = max(wins, key=lambda t: t["pnl_dollars"] or 0)
        lines.append(f"- Best trade: ${best['pnl_dollars']:+,.2f} ({best['contract_strike']:.0f}{best['direction'][-1]})")
    if losses:
        worst = min(losses, key=lambda t: t["pnl_dollars"] or 0)
        lines.append(f"- Worst trade: ${worst['pnl_dollars']:+,.2f} ({worst['contract_strike']:.0f}{worst['direction'][-1]})")

    if high_conf:
        lines.extend(["", "## HIGH-conviction iters"])
        for r in high_conf[:10]:
            t = r["timestamp"][:16] if r["timestamp"] else "—"
            lines.append(
                f"- `#{r['iter_num']}` {t} — {r['direction']} @ ${r['spx_price']:.2f} — {r['reasoning'][:80]}"
            )

    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--date", default=_today_et())
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    try:
        summary = generate(args.db, args.date)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    out = args.out or (DEFAULT_OUT_DIR / f"{args.date}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(summary, encoding="utf-8")
    print(summary)
    print(f"\n[saved to {out}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
