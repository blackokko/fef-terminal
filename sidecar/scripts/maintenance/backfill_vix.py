#!/usr/bin/env python
r"""Backfill historical VIX (and optionally VIX9D / VIX3M term-structure) into the
PRODUCTION historical_bar table so the volatility regime ruler is durable.

WHY
---
`vix_sample` is LIVE-only (starts 2026-05-15). The 2022-2025 backtests need a
historical VIX series aligned with the SPX ruler (historical_bar symbol='SPX',
2022-01-03..) and option_dealer_exposure (iv_atm/gex, 2022-01-03..2025-08-12).
IBKR serves the VIX *index* level with deep history (same as SPX cash bars),
so we pull it here.

WHAT
----
  * MUST-HAVE  : daily bars 2022-01-01 -> today  (~900 rows, tiny, always avail).
  * BEST-EFFORT: 5-min intraday bars, RTH, chunked <=30 days, paced 10s/req.
                 IBKR caps index intraday history -> we pull what's available and
                 report the achieved range (stop after 2 consecutive empty chunks).
  * OPTIONAL   : VIX9D / VIX3M daily (term structure). Skipped silently on error.

Persisted into historical_bar with symbol in ('VIX','VIX9D','VIX3M') and bar_size
matching ('1 day' / '5 mins'). VIX has no real volume -> stored as 0 (fine). The
symbol namespacing means these rows CANNOT collide with existing SPX/ES rows.

SAFETY (a live paper-trading session is active on this DB)
  * IBKR: DEDICATED read-only clientId 171 (sidecar=1/+101, micro-capture=151/152).
    readonly=True -> TWS rejects any order op at the API level. Clean disconnect.
  * DB: ADDITIVE ONLY. PRAGMA busy_timeout=30000. Idempotent -- we read existing
    (symbol,bar_size) timestamps first and INSERT OR IGNORE only new rows, commit
    in <=5k chunks. Never UPDATE/DELETE/DROP. Verifies COUNT before/after.

RERUNNABLE: safe to run repeatedly; only inserts gaps. Run again tomorrow to extend.

RUN
    .\.venv\Scripts\python.exe scripts\maintenance\backfill_vix.py
    ... backfill_vix.py --probe            # connectivity-only, 1 tiny daily req
    ... backfill_vix.py --no-intraday      # daily only
    ... backfill_vix.py --no-term          # skip VIX9D/VIX3M
    ... backfill_vix.py --intraday-chunks 40
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

from ib_async import IB, Index

ET = ZoneInfo("America/New_York")
DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
CLIENT_ID = 171
HOST = os.environ.get("FEF_IB_HOST", "127.0.0.1")
PORT = int(os.environ.get("FEF_IB_PORT", "7497"))


def bar_ts_ms(b, daily: bool) -> int:
    """Robust ts for both intraday (tz-aware datetime) and daily (date) bars."""
    d = b.date
    if hasattr(d, "timestamp"):  # tz-aware datetime (intraday, formatDate=2)
        return int(d.timestamp() * 1000)
    # daily bar -> plain date; anchor at 16:00 ET (the close the OHLC settles to)
    return int(dt.datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).timestamp() * 1000)


def fmt(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).astimezone(ET).strftime("%Y-%m-%d %H:%M")


def day(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


class Store:
    """Idempotent additive writer for historical_bar (symbol,bar_size,ts)."""

    def __init__(self, con: sqlite3.Connection, symbol: str, bar_size: str):
        self.con, self.symbol, self.bar_size = con, symbol, bar_size
        self.existing = {
            r[0] for r in con.execute(
                "SELECT timestamp_ms FROM historical_bar WHERE symbol=? AND bar_size=?",
                (symbol, bar_size),
            )
        }
        self.inserted = 0

    def add(self, bars, daily: bool) -> int:
        rows = []
        for b in bars or []:
            ts = bar_ts_ms(b, daily)
            if ts in self.existing:
                continue
            self.existing.add(ts)
            rows.append((self.symbol, self.bar_size, ts, float(b.open), float(b.high),
                         float(b.low), float(b.close), float(getattr(b, "volume", 0) or 0)))
        # commit in <=5k chunks so we never hold a long write lock
        for i in range(0, len(rows), 5000):
            chunk = rows[i:i + 5000]
            self.con.executemany(
                "INSERT OR IGNORE INTO historical_bar"
                "(symbol,bar_size,timestamp_ms,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?,?)",
                chunk,
            )
            self.con.commit()
        self.inserted += len(rows)
        return len(rows)


def count(con, symbol, bar_size=None):
    if bar_size:
        q = "SELECT COUNT(*),MIN(timestamp_ms),MAX(timestamp_ms) FROM historical_bar WHERE symbol=? AND bar_size=?"
        return con.execute(q, (symbol, bar_size)).fetchone()
    q = "SELECT COUNT(*),MIN(timestamp_ms),MAX(timestamp_ms) FROM historical_bar WHERE symbol=?"
    return con.execute(q, (symbol,)).fetchone()


def pull_daily(ib, contract, label):
    """One big daily request; fall back to yearly chunks if it errors/short."""
    try:
        bars = ib.reqHistoricalData(contract, endDateTime="", durationStr="6 Y",
                                    barSizeSetting="1 day", whatToShow="TRADES",
                                    useRTH=True, formatDate=2, timeout=60)
        if bars and len(bars) > 400:
            return bars
        print(f"  {label} daily: single req returned {len(bars or [])} -> yearly fallback", flush=True)
    except Exception as e:
        print(f"  {label} daily: single req ERR {e} -> yearly fallback", flush=True)
    # yearly fallback
    out, seen = [], set()
    for yr in range(2022, dt.datetime.now(ET).year + 1):
        end = dt.datetime(yr + 1, 1, 1, 0, 0, tzinfo=ET)
        try:
            b = ib.reqHistoricalData(contract, endDateTime=end, durationStr="1 Y",
                                     barSizeSetting="1 day", whatToShow="TRADES",
                                     useRTH=True, formatDate=2, timeout=60)
            for x in b or []:
                k = bar_ts_ms(x, True)
                if k not in seen:
                    seen.add(k)
                    out.append(x)
        except Exception as e:
            print(f"    {label} {yr} ERR {e}", flush=True)
        time.sleep(10)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="connectivity test only (1 tiny daily req)")
    ap.add_argument("--no-intraday", action="store_true")
    ap.add_argument("--no-term", action="store_true", help="skip VIX9D/VIX3M")
    ap.add_argument("--intraday-chunks", type=int, default=30, help="max 30-day 5-min chunks back")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout=30000")

    before = {s: count(con, s)[0] for s in ("VIX", "VIX9D", "VIX3M")}
    print(f"BEFORE historical_bar counts: {before}", flush=True)

    ib = IB()
    ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=20, readonly=True)
    print(f"connected IBKR clientId={CLIENT_ID} readonly=True  accounts={ib.managedAccounts()}", flush=True)
    try:
        vix = Index("VIX", "CBOE", "USD")
        ib.qualifyContracts(vix)
        print(f"qualified VIX conId={vix.conId}", flush=True)

        if args.probe:
            b = ib.reqHistoricalData(vix, endDateTime="", durationStr="10 D",
                                     barSizeSetting="1 day", whatToShow="TRADES",
                                     useRTH=True, formatDate=2, timeout=60)
            print(f"PROBE ok: {len(b or [])} daily bars; last={[(day(bar_ts_ms(x,True)),round(x.close,2)) for x in (b or [])[-3:]]}", flush=True)
            return

        # ---- DAILY (must-have) ----
        st = Store(con, "VIX", "1 day")
        bars = pull_daily(ib, vix, "VIX")
        added = st.add(bars, daily=True)
        print(f"VIX daily: fetched {len(bars or [])}, inserted {added}", flush=True)
        time.sleep(10)

        # ---- INTRADAY 5-min (best-effort) ----
        if not args.no_intraday:
            sti = Store(con, "VIX", "5 mins")
            end = dt.datetime.now(ET)
            empty = 0
            for i in range(args.intraday_chunks):
                lbl = end.strftime("%Y-%m-%d")
                try:
                    b = ib.reqHistoricalData(vix, endDateTime=end, durationStr="30 D",
                                             barSizeSetting="5 mins", whatToShow="TRADES",
                                             useRTH=True, formatDate=2, timeout=60)
                except Exception as e:
                    print(f"  5min <= {lbl}: ERR {e} (stop)", flush=True)
                    break
                got = sti.add(b, daily=False)
                n = len(b or [])
                print(f"  5min <= {lbl}: fetched {n} (+{got}, tot {sti.inserted})", flush=True)
                if n == 0:
                    empty += 1
                    if empty >= 2:
                        print("  two empty chunks -> reached IBKR intraday floor, stopping.", flush=True)
                        break
                else:
                    empty = 0
                end = end - dt.timedelta(days=30)
                time.sleep(10)
            print(f"VIX 5min: inserted {sti.inserted}", flush=True)

        # ---- TERM STRUCTURE (optional) ----
        if not args.no_term:
            for sym in ("VIX9D", "VIX3M"):
                try:
                    c = Index(sym, "CBOE", "USD")
                    ib.qualifyContracts(c)
                    stt = Store(con, sym, "1 day")
                    b = pull_daily(ib, c, sym)
                    a = stt.add(b, daily=True)
                    print(f"{sym} daily: fetched {len(b or [])}, inserted {a}", flush=True)
                    time.sleep(10)
                except Exception as e:
                    print(f"{sym}: skipped ({e})", flush=True)
    finally:
        ib.disconnect()
        print("IBKR disconnected.", flush=True)

    # ---- VERIFY ----
    print("\n=== VERIFY (after) ===", flush=True)
    for s in ("VIX", "VIX9D", "VIX3M"):
        for bs in ("1 day", "5 mins"):
            n, mn, mx = count(con, s, bs)
            if n:
                print(f"  {s} '{bs}': n={n}  {day(mn)}..{day(mx)}", flush=True)
        tot = before.get(s, 0)
        print(f"  {s} total: {tot} -> {count(con, s)[0]}", flush=True)

    # scale spot-checks: tariff crash Apr 2025 (~45-55) vs a calm day (~13-18)
    print("\n  scale check (VIX daily close):", flush=True)
    for target in ("2025-04-08", "2025-04-09", "2025-04-04", "2024-07-03", "2025-01-15"):
        y, m, d = map(int, target.split("-"))
        lo = int(dt.datetime(y, m, d, 0, 0, tzinfo=ET).timestamp() * 1000)
        hi = lo + 86_400_000
        r = con.execute("SELECT open,high,low,close FROM historical_bar WHERE symbol='VIX' "
                        "AND bar_size='1 day' AND timestamp_ms>=? AND timestamp_ms<? LIMIT 1",
                        (lo, hi)).fetchone()
        print(f"    {target}: {tuple(round(x,2) for x in r) if r else 'NO ROW'}  (o/h/l/c)", flush=True)
    con.close()


if __name__ == "__main__":
    main()
