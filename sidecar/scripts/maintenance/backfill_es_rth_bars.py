"""Backfill ES RTH 1-min OHLCV (with volume) into historical_bar under a CLEAN
key so VWAP can be detected on the right bars.

The existing symbol='ES' bar_size='1 min' rows are the OVERNIGHT session
(~18:00-00:00 ET) — wrong for RTH-anchored VWAP. This writes RTH bars under
bar_size='1 min RTH' (distinct key, no collision).

Read-only: connects a SEPARATE IBKR client id (77) and only requests historical
data — cannot place/modify orders, won't disturb the sidecar's trading client.
"""
import asyncio
import datetime as dt
import os
import sqlite3
import sys

from fef_sidecar.ibkr.models import Contract
from fef_sidecar.ibkr.live import _to_ib_contract

DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
BAR_SIZE = "1 min RTH"


async def main():
    duration = sys.argv[1] if len(sys.argv) > 1 else "15 D"
    port = int(os.environ.get("FEF_IB_PORT", "7497"))
    from ib_async import IB
    ib = IB()
    await ib.connectAsync("127.0.0.1", port, clientId=77, readonly=True)
    try:
        ibc = _to_ib_contract(Contract.for_symbol("ES"))
        await ib.qualifyContractsAsync(ibc)
        bars = await ib.reqHistoricalDataAsync(
            ibc, endDateTime="", durationStr=duration, barSizeSetting="1 min",
            whatToShow="TRADES", useRTH=True, formatDate=2, keepUpToDate=False,
        )
    finally:
        ib.disconnect()

    c = sqlite3.connect(DB)
    existing = {
        r[0] for r in c.execute(
            "SELECT timestamp_ms FROM historical_bar WHERE symbol='ES' AND bar_size=?", (BAR_SIZE,)
        ).fetchall()
    }
    added = 0
    for b in bars:
        ts_ms = int(b.date.timestamp() * 1000) if hasattr(b.date, "timestamp") else int(b.date * 1000)
        if ts_ms in existing:
            continue
        c.execute(
            "INSERT INTO historical_bar (symbol,bar_size,timestamp_ms,open,high,low,close,volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("ES", BAR_SIZE, ts_ms, float(b.open), float(b.high), float(b.low),
             float(b.close), float(b.volume or 0)),
        )
        added += 1
    c.commit()

    def f(m):
        return dt.datetime.fromtimestamp(m / 1000, dt.UTC).strftime("%m-%d %H:%M")
    if bars:
        print(f"ES RTH bars: fetched {len(bars)}, added {added}.  span {f(int(bars[0].date.timestamp()*1000))}Z "
              f"-> {f(int(bars[-1].date.timestamp()*1000))}Z")
    else:
        print("no bars returned")


if __name__ == "__main__":
    asyncio.run(main())
