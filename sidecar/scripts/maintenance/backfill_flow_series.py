# [REMOVED 2026-08-25] QuantData subscription CANCELLED. THIS SCRIPT NO LONGER
# RUNS: it imports fef_sidecar.quantdata, which is deleted, and it fetched from
# the QuantData REST API, which we no longer have credentials for. Kept as the
# provenance record of the study/backfill it performed (see RESEARCH_REGISTRY);
# delete it only as a deliberate archive decision, not as a cleanup.
"""Backfill the intraday call/put premium-flow series (net-drift + net-flow)
into flow_series_point, for one or more session dates.

The official QuantData API serves the FULL intraday series per sessionDate, so
this recovers days where the live poll was down (e.g. the 2026-05-28 outage).

Usage:
    python -m scripts.maintenance.backfill_flow_series 2026-05-28
    python -m scripts.maintenance.backfill_flow_series 2026-05-18 ... 2026-05-29
    python -m scripts.maintenance.backfill_flow_series          # most recent day
"""
import asyncio
import sys

from fef_sidecar.db.engine import get_session, init_db
from fef_sidecar.db.models import FlowSeriesPoint
from fef_sidecar.quantdata.client import QuantDataClient, _most_recent_trading_day_et


async def backfill_one(client: QuantDataClient, session_date: str) -> int:
    await client.set_page_filter(session_date=session_date, ticker="SPX")
    drift = await client.get_net_drift()
    flow = await client.get_net_flow()

    # Merge the two series by minute timestamp.
    merged: dict[int, dict[str, float]] = {}
    for e in drift.entries:
        m = merged.setdefault(e.timestamp_ms, {})
        m["drift_net_call"] = e.call_dollars
        m["drift_net_put"] = e.put_dollars
        m["spx_price"] = e.spx_price
    for e in flow.entries:
        m = merged.setdefault(e.timestamp_ms, {})
        m["flow_call_sum"] = e.call_dollars
        m["flow_put_sum"] = e.put_dollars

    rows = [
        FlowSeriesPoint(
            timestamp_ms=ts,
            session_date=session_date,
            symbol="SPX",
            drift_net_call=v.get("drift_net_call", 0.0),
            drift_net_put=v.get("drift_net_put", 0.0),
            flow_call_sum=v.get("flow_call_sum", 0.0),
            flow_put_sum=v.get("flow_put_sum", 0.0),
            spx_price=v.get("spx_price", 0.0),
            source="backfill",
        )
        for ts, v in sorted(merged.items())
    ]

    # Idempotent per-row insert (swallow unique-constraint dupes).
    from sqlalchemy.exc import IntegrityError

    added = 0
    for r in rows:
        try:
            async with get_session() as s:
                s.add(r)
                await s.flush()
            added += 1
        except IntegrityError:
            continue
    print(f"{session_date}: {len(rows)} minutes fetched, {added} new rows inserted")
    return added


async def main() -> None:
    dates = sys.argv[1:] or [_most_recent_trading_day_et()]
    await init_db()
    total = 0
    async with QuantDataClient() as client:
        for d in dates:
            try:
                total += await backfill_one(client, d)
            except Exception as e:
                print(f"{d}: FAILED {type(e).__name__}: {e}")
    print(f"DONE — {total} rows inserted across {len(dates)} session(s)")


if __name__ == "__main__":
    asyncio.run(main())
