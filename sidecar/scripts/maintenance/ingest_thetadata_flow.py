"""Ingest SPXW 0DTE trade prints from the local Theta Terminal, classify each print
as aggressive-buy vs aggressive-sell against our stored NBBO (quote rule), and
aggregate to a compact per-minute SIGNED-FLOW series -> option_flow_minute.

This is the cleanest raw re-test of flow-as-direction: net call/put premium being
*lifted* (bought at the ask) vs *hit* (sold at the bid) is the intraday directional
pressure. We discard the raw prints and keep only the per-minute aggregates.

Run AFTER the NBBO quote ingest (it reads option_quote_history to classify), and
after the quote pull frees the Terminal (trade pulls are heavy).

    python -m scripts.maintenance.ingest_thetadata_flow --probe 20220615
    python -m scripts.maintenance.ingest_thetadata_flow                 # all sessions
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

from scripts.research.backtest.data import Session, connect

BASE = "http://127.0.0.1:25503/v3/option/history/trade"
SYMBOL = "SPXW"
STRIKE_RANGE = 20          # match the quote band so every print has an NBBO to classify against
DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
ET = ZoneInfo("America/New_York")


def _ensure_table():
    c = sqlite3.connect(DB)
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("""CREATE TABLE IF NOT EXISTS option_flow_minute(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_date TEXT, ts_ms INTEGER,
        call_buy_prem REAL, call_sell_prem REAL, put_buy_prem REAL, put_sell_prem REAL,
        call_buy_vol INTEGER, call_sell_vol INTEGER, put_buy_vol INTEGER, put_sell_vol INTEGER,
        net_signed_prem REAL, source TEXT DEFAULT 'thetadata')""")
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_flow_min_unique
        ON option_flow_minute(session_date, ts_ms)""")
    c.commit()
    c.close()


async def fetch(client, date):
    params = {"symbol": SYMBOL, "expiration": date, "date": date,
              "right": "both", "strike_range": str(STRIKE_RANGE), "format": "ndjson"}
    r = await client.get(BASE, params=params, timeout=180)
    return r.status_code, r.text


def _utc_ms(ts_iso):
    naive = dt.datetime.fromisoformat(ts_iso)
    if naive.tzinfo is None:
        naive = naive.replace(tzinfo=ET)
    return int(naive.timestamp() * 1000)


def _classify(price, bid, ask):
    """+1 aggressive buy (lifted ask), -1 aggressive sell (hit bid), 0 neutral."""
    if bid is None or ask is None or ask <= bid:
        return 0
    mid = (bid + ask) / 2
    if price >= ask - 1e-9:
        return 1
    if price <= bid + 1e-9:
        return -1
    return 1 if price > mid else (-1 if price < mid else 0)


def aggregate(con, session_date, raw):
    """Return list of per-minute aggregate rows for the session."""
    sess = Session(con, session_date)
    if sess.empty():
        return []
    rt = {"CALL": "C", "PUT": "P"}
    buckets = {}  # minute_ms -> dict of accumulators
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
            price = float(o["price"]); size = int(o["size"])
            strike = float(o["strike"]); right = rt.get(o.get("right"), o.get("right"))
            tms = _utc_ms(o["timestamp"])
        except Exception:
            continue
        minute = (tms // 60000) * 60000
        qmin = sess.minute_at_or_before(tms)
        q = sess.quote(qmin, strike, right) if qmin else None
        sign = _classify(price, q[0], q[1]) if q else 0
        prem = price * size * 100
        b = buckets.setdefault(minute, dict(cbp=0.0, csp=0.0, pbp=0.0, psp=0.0,
                                            cbv=0, csv=0, pbv=0, psv=0))
        if right == "C":
            if sign > 0:
                b["cbp"] += prem; b["cbv"] += size
            elif sign < 0:
                b["csp"] += prem; b["csv"] += size
        elif right == "P":
            if sign > 0:
                b["pbp"] += prem; b["pbv"] += size
            elif sign < 0:
                b["psp"] += prem; b["psv"] += size
    rows = []
    for minute, b in sorted(buckets.items()):
        net = (b["cbp"] - b["csp"]) - (b["pbp"] - b["psp"])  # bullish > 0
        rows.append((session_date, minute, b["cbp"], b["csp"], b["pbp"], b["psp"],
                     b["cbv"], b["csv"], b["pbv"], b["psv"], net))
    return rows


def persist(rows):
    if not rows:
        return 0
    c = sqlite3.connect(DB)
    c.execute("PRAGMA busy_timeout=30000")
    before = c.execute("SELECT COUNT(*) FROM option_flow_minute").fetchone()[0]
    c.executemany("""INSERT OR IGNORE INTO option_flow_minute
        (session_date,ts_ms,call_buy_prem,call_sell_prem,put_buy_prem,put_sell_prem,
         call_buy_vol,call_sell_vol,put_buy_vol,put_sell_vol,net_signed_prem)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rows)
    c.commit()
    after = c.execute("SELECT COUNT(*) FROM option_flow_minute").fetchone()[0]
    c.close()
    return after - before


async def run():
    _ensure_table()
    con = connect()
    all_sessions = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_quote_history ORDER BY session_date")]
    done = {r[0] for r in con.execute("SELECT DISTINCT session_date FROM option_flow_minute")}
    todo = [d for d in all_sessions if d not in done]
    print(f"FLOW ingest: {len(todo)} sessions to pull ({len(done)} done)")
    ok = ins = 0
    async with httpx.AsyncClient() as client:
        for i, ds in enumerate(todo):
            d = ds.replace("-", "")
            for attempt in range(3):
                try:
                    code, raw = await fetch(client, d)
                except Exception:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if code == 200:
                    ins += persist(aggregate(con, ds, raw))
                    ok += 1
                    break
                if code in (429, 500, 502, 503):
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                break
            if (i + 1) % 50 == 0:
                print(f"  {i+1}/{len(todo)}  ({ds})  minute-rows={ins}")
            await asyncio.sleep(0.3)
    print(f"FLOW ingest done: {ok} sessions ok, {ins} minute-rows inserted")


async def probe(date):
    async with httpx.AsyncClient() as client:
        code, raw = await fetch(client, date)
    print(f"HTTP {code}  ({len(raw)} bytes)")
    con = connect()
    dashed = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    rows = aggregate(con, dashed, raw)
    print(f"aggregated to {len(rows)} minute-rows")
    for r in rows[:3] + rows[-2:]:
        ts = dt.datetime.fromtimestamp(r[1] / 1000, ET).strftime("%H:%M")
        print(f"  {ts}  net_signed=${r[10]:+,.0f}  callBuy=${r[2]:,.0f} callSell=${r[3]:,.0f} "
              f"putBuy=${r[4]:,.0f} putSell=${r[5]:,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe")
    a = ap.parse_args()
    asyncio.run(probe(a.probe) if a.probe else run())


if __name__ == "__main__":
    main()
