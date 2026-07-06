"""Ingest 1/2/3-DTE SPXW option chains (as priced on each session date) for the DEV set,
for H6 (charm/vanna directional signal across DTE). For session D, the N-DTE expiry is the
Nth following SPXW expiry (= Nth following 0DTE session date in our calendar). Same
ATM+/-20, 1-min NBBO. DEV ONLY (2022..2025-08-12); holdout never pulled here.

    python -m scripts.maintenance.ingest_thetadata_multidte --probe
    python -m scripts.maintenance.ingest_thetadata_multidte
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sqlite3
from zoneinfo import ZoneInfo

import httpx

BASE = "http://127.0.0.1:25503/v3/option/history/quote"
SYMBOL, INTERVAL, STRIKE_RANGE, RIGHT = "SPXW", "1m", 20, "both"
DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
ET = ZoneInfo("America/New_York")
DEV_START, DEV_END = "2022-01-01", "2025-08-12"


def _ensure():
    c = sqlite3.connect(DB)
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("""CREATE TABLE IF NOT EXISTS option_quote_multidte(
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_date TEXT, dte INTEGER, expiry TEXT,
        strike REAL, right TEXT, timestamp_ms INTEGER, bid REAL, ask REAL,
        bid_size INTEGER, ask_size INTEGER, quote_quality TEXT)""")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mdte "
              "ON option_quote_multidte(session_date,dte,strike,right,timestamp_ms)")
    c.commit()
    c.close()


def _to_ms(ts):
    naive = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if naive.tzinfo is None:
        naive = naive.replace(tzinfo=ET)
    return int(naive.timestamp() * 1000)


def _quality(bid, ask):
    if not bid or not ask or bid <= 0 or ask <= 0:
        return "zero"
    if bid > ask:
        return "crossed"
    if (ask - bid) / ((ask + bid) / 2) > 0.5:
        return "wide"
    return "ok"


async def fetch(client, expiry, date):
    params = {"symbol": SYMBOL, "expiration": expiry, "date": date, "right": RIGHT,
              "interval": INTERVAL, "strike_range": str(STRIKE_RANGE), "format": "ndjson"}
    r = await client.get(BASE, params=params, timeout=120)
    return r.status_code, r.text


def parse(raw):
    rt = {"CALL": "C", "PUT": "P"}
    out = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
            out.append((float(o["strike"]), rt.get(o.get("right"), o.get("right")),
                        _to_ms(o["timestamp"]), o.get("bid"), o.get("ask"),
                        o.get("bid_size"), o.get("ask_size")))
        except Exception:
            continue
    return out


def persist(date, dte, expiry, rows):
    if not rows:
        return 0
    data = [(date, dte, expiry, s, r, ts, b, a, bs, az, _quality(b, a))
            for (s, r, ts, b, a, bs, az) in rows]
    c = sqlite3.connect(DB)
    c.execute("PRAGMA busy_timeout=30000")
    before = c.execute("SELECT COUNT(*) FROM option_quote_multidte").fetchone()[0]
    c.executemany("INSERT OR IGNORE INTO option_quote_multidte"
                  "(session_date,dte,expiry,strike,right,timestamp_ms,bid,ask,bid_size,ask_size,quote_quality)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?)", data)
    c.commit()
    after = c.execute("SELECT COUNT(*) FROM option_quote_multidte").fetchone()[0]
    c.close()
    return after - before


def _plan():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cal = [r[0] for r in c.execute("SELECT DISTINCT session_date FROM option_quote_history ORDER BY session_date")]
    try:
        done = {(r[0], r[1]) for r in c.execute("SELECT DISTINCT session_date,dte FROM option_quote_multidte")}
    except Exception:
        done = set()
    c.close()
    idx = {d: i for i, d in enumerate(cal)}
    todo = []
    for d in cal:
        if not (DEV_START <= d <= DEV_END):
            continue
        i = idx[d]
        for dte in (1, 2, 3):
            if i + dte < len(cal) and (d, dte) not in done:
                todo.append((d, dte, cal[i + dte]))
    return todo


async def run():
    _ensure()
    todo = _plan()
    print(f"multi-DTE ingest (DEV): {len(todo)} (session,dte) pulls to do", flush=True)
    ok = ins = 0
    async with httpx.AsyncClient() as client:
        for n, (d, dte, exp) in enumerate(todo):
            for attempt in range(3):
                try:
                    code, raw = await fetch(client, exp.replace("-", ""), d.replace("-", ""))
                except Exception:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if code == 200:
                    ins += persist(d, dte, exp, parse(raw))
                    ok += 1
                    break
                if code in (429, 500, 502, 503):
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                break
            if (n + 1) % 100 == 0:
                print(f"  {n+1}/{len(todo)} ({d} dte{dte}) rows={ins}", flush=True)
            await asyncio.sleep(0.3)
    print(f"multi-DTE ingest done: {ok} pulls, {ins} rows", flush=True)


async def probe():
    todo = _plan()
    if not todo:
        print("nothing to do")
        return
    d, dte, exp = todo[0]
    code, raw = await fetch(httpx.AsyncClient(), exp.replace("-", ""), d.replace("-", "")) if False else (None, None)
    async with httpx.AsyncClient() as client:
        code, raw = await fetch(client, exp.replace("-", ""), d.replace("-", ""))
    rows = parse(raw)
    print(f"probe {d} dte{dte} expiry={exp}: HTTP {code}, {len(rows)} parsed rows")
    for r in rows[:3]:
        print("  ", r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    asyncio.run(probe() if a.probe else run())


if __name__ == "__main__":
    main()
