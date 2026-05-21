"""One-shot maintenance: backfill live_order fills from ibkr_execution,
then recompute DailyReview + DailySession for given session dates.

Use when the sidecar is offline. Same logic as the
/admin/backfill_live_order_fills and /admin/daily_review endpoints.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "sidecar" / "src"))


async def _backfill_fills(session_date: str | None) -> dict:
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import IBKRExecution, LiveOrder
    from sqlmodel import select

    async with get_session() as ss:
        q = select(IBKRExecution)
        if session_date:
            q = q.where(IBKRExecution.session_date == session_date)
        execs = list((await ss.exec(q)).all())
        by_order: dict[int, list] = {}
        for e in execs:
            if e.ib_order_id is None:
                continue
            by_order.setdefault(int(e.ib_order_id), []).append(e)

        candidates = list((await ss.exec(select(LiveOrder))).all())
        if session_date:
            candidates = [p for p in candidates
                          if p.submitted_at
                          and p.submitted_at.strftime("%Y-%m-%d") == session_date]

        updated = 0
        skipped_no_match = 0
        skipped_already = 0
        for p in candidates:
            if p.ib_order_id is None:
                continue
            fills = by_order.get(int(p.ib_order_id))
            if not fills:
                skipped_no_match += 1
                continue
            total = sum(float(f.shares or 0) for f in fills)
            if total <= 0:
                skipped_no_match += 1
                continue
            wpx = sum(float(f.shares or 0) * float(f.price or 0) for f in fills) / total
            new_filled = int(round(total))
            new_px = round(wpx, 4)
            if (p.filled_qty == new_filled
                    and p.avg_fill_price is not None
                    and abs((p.avg_fill_price or 0) - new_px) < 0.005):
                skipped_already += 1
                continue
            p.filled_qty = new_filled
            p.avg_fill_price = new_px
            if p.status in ("SUBMITTED", "ACCEPTED", "WORKING", None):
                qt = int(p.qty or 0)
                if qt > 0 and new_filled >= qt:
                    p.status = "FILLED"
                elif new_filled > 0:
                    p.status = "PARTIAL"
            ss.add(p)
            updated += 1
        await ss.commit()
    return {"session_date": session_date or "all", "updated": updated,
            "skipped_no_match": skipped_no_match,
            "skipped_already_filled": skipped_already,
            "execs_scanned": len(execs)}


async def _recompute(session_date: str) -> dict:
    from fef_sidecar.engine.daily_review import compute_daily_review
    r = await compute_daily_review(session_date)
    return {
        "session_date": r.session_date,
        "actionable": r.actionable_signals,
        "wins": r.wins, "stops": r.stops, "timeouts": r.timeouts,
        "win_rate_pct": r.win_rate_pct,
        "expectancy_r": r.expectancy_r,
        "actual_total_R": r.actual_total_R,
        "actual_wins": r.actual_wins,
        "actual_losses": r.actual_losses,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    args = ap.parse_args()

    # Build date list
    from datetime import date, timedelta
    d0 = date.fromisoformat(args.date_from)
    d1 = date.fromisoformat(args.date_to)
    dates = []
    d = d0
    while d <= d1:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    print(f"Backfilling live_order fills across {dates[0]} to {dates[-1]}...")
    for sd in dates:
        r = await _backfill_fills(sd)
        print(f"  {sd}: updated={r['updated']:>3}  skipped_no_match={r['skipped_no_match']:>3}  skipped_already_filled={r['skipped_already_filled']:>3}  execs_scanned={r['execs_scanned']:>4}")

    print()
    print(f"Recomputing daily_review for each date...")
    for sd in dates:
        try:
            r = await _recompute(sd)
            print(f"  {sd}: wins={r['wins']:>3} stops={r['stops']:>3} timeouts={r['timeouts']:>3} WR={r['win_rate_pct']:>5.1f}% expR={r['expectancy_r']:+.3f}  actualR={r['actual_total_R']:+.2f} actual W/L={r['actual_wins']}/{r['actual_losses']}")
        except Exception as e:
            print(f"  {sd}: FAILED ({type(e).__name__}: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
