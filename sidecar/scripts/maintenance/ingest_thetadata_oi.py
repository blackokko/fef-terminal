"""Ingest start-of-day Open Interest per strike for SPXW 0DTE from the local Theta
Terminal -> option_open_interest. Enables dealer GEX / gamma-flip / max-pain features.

Tiny vs the quote ingest (one OI snapshot per strike/right/day). Pulls OI only for
sessions already present in option_quote_history (the confirmed 0DTE days), so it
stays aligned with the quote data and skips non-0DTE weekdays.

    python -m scripts.maintenance.ingest_thetadata_oi --probe 20220615
    python -m scripts.maintenance.ingest_thetadata_oi            # all sessions in DB
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3

import httpx

BASE = "http://127.0.0.1:25503/v3/option/history/open_interest"
SYMBOL = "SPXW"
STRIKE_RANGE = 30  # ATM +/- 30 -> fuller gamma profile for GEX (OI rows are cheap)
DB = os.path.expanduser("~/.fef-terminal/signal_log.db")


def _ensure_table():
    c = sqlite3.connect(DB)
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("""CREATE TABLE IF NOT EXISTS option_open_interest(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_date TEXT, expiry TEXT, strike REAL, right TEXT,
        open_interest INTEGER, snap_ts TEXT, source TEXT DEFAULT 'thetadata')""")
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_ooi_unique
        ON option_open_interest(expiry, strike, right)""")
    c.commit()
    c.close()


async def fetch(client, date):
    params = {"symbol": SYMBOL, "expiration": date, "date": date,
              "right": "both", "strike_range": str(STRIKE_RANGE), "format": "ndjson"}
    r = await client.get(BASE, params=params, timeout=60)
    return r.status_code, r.text


def parse(raw):
    out = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def persist(date, rows):
    rt = {"CALL": "C", "PUT": "P"}
    data = []
    for o in rows:
        try:
            data.append((date, str(o.get("expiration", "")).replace("-", ""),
                         float(o["strike"]), rt.get(o.get("right"), o.get("right")),
                         int(o.get("open_interest", 0)), o.get("timestamp")))
        except Exception:
            continue
    if not data:
        return 0
    c = sqlite3.connect(DB)
    c.execute("PRAGMA busy_timeout=30000")
    before = c.execute("SELECT COUNT(*) FROM option_open_interest").fetchone()[0]
    c.executemany("INSERT OR IGNORE INTO option_open_interest"
                  "(session_date,expiry,strike,right,open_interest,snap_ts) VALUES(?,?,?,?,?,?)", data)
    c.commit()
    after = c.execute("SELECT COUNT(*) FROM option_open_interest").fetchone()[0]
    c.close()
    return after - before


def _session_dates():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    ds = [r[0] for r in c.execute("SELECT DISTINCT session_date FROM option_quote_history ORDER BY session_date")]
    c.close()
    return ds


async def run():
    _ensure_table()
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    done = {r[0] for r in c.execute("SELECT DISTINCT session_date FROM option_open_interest")}
    c.close()
    todo = [d for d in _session_dates() if d not in done]
    print(f"OI ingest: {len(todo)} sessions to pull ({len(done)} already done)")
    ok = ins = 0
    async with httpx.AsyncClient() as client:
        for i, ds in enumerate(todo):
            d = ds.replace("-", "")
            for attempt in range(3):
                try:
                    code, raw = await fetch(client, d)
                except Exception:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                if code == 200:
                    ins += persist(ds, parse(raw))
                    ok += 1
                    break
                if code in (429, 500, 502, 503):
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                break
            if (i + 1) % 100 == 0:
                print(f"  {i+1}/{len(todo)}  ({ds})  rows={ins}")
            await asyncio.sleep(0.15)
    print(f"OI ingest done: {ok} sessions ok, {ins} rows inserted")


async def probe(date):
    code, raw = await fetch(httpx.AsyncClient(), date) if False else (None, None)
    async with httpx.AsyncClient() as client:
        code, raw = await fetch(client, date)
    print(f"HTTP {code}")
    for line in parse(raw)[:6]:
        print(line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe")
    a = ap.parse_args()
    if a.probe:
        asyncio.run(probe(a.probe))
    else:
        asyncio.run(run())


if __name__ == "__main__":
    main()
