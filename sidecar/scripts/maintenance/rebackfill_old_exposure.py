"""Replace the broken pre-2026-05-28 exposure_chain_point backfill (old dashboard
API — isolated put concentration at spot -> absurd far-OTM gamma flips) with
clean official-API data. Deletes source='backfill' rows for the listed sessions,
re-pulls all 4 greeks at one EOD snapshot/session. Idempotent-ish (delete+insert).
"""
import asyncio
import datetime as dt
import sqlite3
import os
import sys

from fef_sidecar.quantdata.client import QuantDataClient
from fef_sidecar.quantdata.models import GreekType

DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
SESSIONS = ["2026-05-18", "2026-05-19", "2026-05-20", "2026-05-21",
            "2026-05-22", "2026-05-26", "2026-05-27"]


async def main():
    sessions = sys.argv[1:] or SESSIONS
    c = sqlite3.connect(DB)
    async with QuantDataClient() as client:
        for sd in sessions:
            # EOD timestamp (16:00 ET = 20:00 UTC) as the single session snapshot
            ts_ms = int(dt.datetime.fromisoformat(sd + "T20:00:00+00:00").timestamp() * 1000)
            deleted = c.execute(
                "DELETE FROM exposure_chain_point WHERE session_date=? AND source='backfill'", (sd,)
            ).rowcount
            c.commit()
            await client.set_page_filter(session_date=sd, ticker="SPX")
            total = 0
            for g in (GreekType.GAMMA, GreekType.DELTA, GreekType.CHARM, GreekType.VANNA):
                try:
                    snap = await client.get_exposure_by_strike(g)
                except Exception as e:
                    print(f"  {sd} {g.value}: fetch failed {e}")
                    continue
                spot = snap.spx_price
                for r in snap.rows:
                    c.execute(
                        "INSERT INTO exposure_chain_point "
                        "(captured_at,timestamp_ms,session_date,symbol,greek_type,strike,net_m,call_m,put_m,spot_at_capture,source) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (dt.datetime.now(dt.UTC).isoformat(), ts_ms, sd, "SPX", g.value,
                         r.strike, r.net_m, r.call_m, r.put_m, spot, "backfill"),
                    )
                    total += 1
            c.commit()
            print(f"  {sd}: deleted {deleted} old rows, inserted {total} new-API rows (spot {spot:.0f})")
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
