"""Surgical per-trade tick fetcher (ThetaData API v3) + local cache.

Enforces the tick-data mandate WITHOUT filling the production DB: for a single
trade window, pull 1-second option NBBO for the exact (symbol, expiry, strike,
right, date, time-window), cache it in a SEPARATE analytics DB (scratch), and
return the 1s bid/ask path for tick-accurate first-touch fill scoring.

ThetaData v3 (confirmed working 2026-07-01): Theta Terminal on 127.0.0.1:25503,
GET /v3/option/history/quote?symbol=SPXW&expiration=YYYYMMDD&start_date=&end_date=
&strike=<DOLLARS>&right=C&interval=1s  (times optional, hh:mm:ss.SSS). Options tier
covers 1s; the SPX *index* intraday endpoint needs a Standard sub (FREE gets EOD).
"""
from __future__ import annotations

import os
import sqlite3

import httpx

BASE = "http://127.0.0.1:25503/v3/option/history/quote"
_SCRATCH = (r"D:\fef-data")
CACHE_DB = os.path.join(_SCRATCH, "tick_cache.db")   # NOT the production DB


def _cache():
    c = sqlite3.connect(CACHE_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS opt_1s(
        symbol TEXT, expiry TEXT, strike REAL, right TEXT, date TEXT,
        ts_ms INTEGER, bid REAL, ask REAL,
        PRIMARY KEY(symbol,expiry,strike,right,date,ts_ms))""")
    c.execute("""CREATE TABLE IF NOT EXISTS pulled(
        symbol TEXT, expiry TEXT, strike REAL, right TEXT, date TEXT,
        PRIMARY KEY(symbol,expiry,strike,right,date))""")
    return c


def _et_ms_of_day(ts_iso: str) -> int:
    """'2025-08-11T09:30:01.000' -> ms since ET midnight (time-of-day only)."""
    t = ts_iso.split("T")[1]
    hh, mm, rest = t.split(":")
    ss, ms = (rest.split(".") + ["0"])[:2]
    return ((int(hh) * 3600 + int(mm) * 60 + int(ss)) * 1000 + int(ms.ljust(3, "0")[:3]))


def fetch_option_1s(symbol: str, expiry: str, strike: float, right: str, date: str,
                    *, force: bool = False) -> list[tuple[int, float, float]]:
    """Return [(ms_of_day, bid, ask), ...] at 1s for one contract-day. Caches the
    full RTH day once (a day is tiny), then window in-memory per trade.
    expiry/date = 'YYYYMMDD'; strike in DOLLARS; right 'C'/'P'."""
    c = _cache()
    key = (symbol, expiry, float(strike), right, date)
    have = c.execute("SELECT 1 FROM pulled WHERE symbol=? AND expiry=? AND strike=? "
                     "AND right=? AND date=?", key).fetchone()
    if have and not force:
        rows = c.execute("SELECT ts_ms,bid,ask FROM opt_1s WHERE symbol=? AND expiry=? "
                         "AND strike=? AND right=? AND date=? ORDER BY ts_ms", key).fetchall()
        c.close()
        return rows
    params = {"symbol": symbol, "expiration": expiry, "start_date": date, "end_date": date,
              "strike": str(int(strike)) if float(strike).is_integer() else str(strike),
              "right": right, "interval": "1s"}
    r = httpx.get(BASE, params=params, timeout=120)
    r.raise_for_status()
    lines = r.text.strip().splitlines()
    if not lines or "," not in lines[0]:
        c.close()
        return []
    hdr = [h.strip().strip('"') for h in lines[0].split(",")]
    i_ts, i_bid, i_ask = hdr.index("timestamp"), hdr.index("bid"), hdr.index("ask")
    out = []
    for ln in lines[1:]:
        f = [x.strip().strip('"') for x in ln.split(",")]
        try:
            out.append((_et_ms_of_day(f[i_ts]), float(f[i_bid]), float(f[i_ask])))
        except (ValueError, IndexError):
            continue
    with c:
        c.executemany("INSERT OR IGNORE INTO opt_1s VALUES(?,?,?,?,?,?,?,?)",
                      [(symbol, expiry, float(strike), right, date, ts, b, a) for ts, b, a in out])
        c.execute("INSERT OR IGNORE INTO pulled VALUES(?,?,?,?,?)", key)
    c.close()
    return out


def window(rows, start_ms: int, end_ms: int):
    """Slice a cached 1s path to [start_ms, end_ms] (ms-of-day)."""
    return [r for r in rows if start_ms <= r[0] <= end_ms]


if __name__ == "__main__":
    # self-test: pull one contract-day, window a 5-min slice
    rows = fetch_option_1s("SPXW", "20250811", 6400, "C", "20250811")
    print(f"pulled {len(rows)} 1s quotes for SPXW 20250811 6400C")
    if rows:
        w = window(rows, 10 * 3600_000, 10 * 3600_000 + 5 * 60_000)  # 10:00-10:05
        print(f"  10:00-10:05 window: {len(w)} quotes; first={w[0] if w else None} last={w[-1] if w else None}")
        # cadence check
        difs = [w[i][0] - w[i - 1][0] for i in range(1, min(len(w), 12))]
        print(f"  spacings(ms): {difs}")
        print(f"  cache file: {CACHE_DB} ({os.path.getsize(CACHE_DB)//1024} KB)")
