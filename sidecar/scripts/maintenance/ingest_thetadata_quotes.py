"""Ingest historical SPXW 0DTE option NBBO bid/ask from ThetaData into
option_quote_history. Pulls from the LOCAL Theta Terminal REST API
(http://127.0.0.1:25503/v3/option/history/quote) — the Terminal app must be
running and logged in (Options Standard sub).

THE unlock (2026-06-29): real historical option-dollar data so we can validate
signals in option-$ and backtest credit spreads on YEARS of data.

Endpoint params (per v3 docs): symbol(root=SPXW), expiration(YYYYMMDD),
right(call/put/both), date|start_date/end_date, interval(tick/1s/1m/...),
strike_range(ATM±N), format(json/ndjson/csv). Sub-minute intervals = single-day
only; each 0DTE expiry IS one day, so we request per-session.

USAGE — run --probe FIRST on first connect to confirm the live response shape,
THEN bulk:
    python -m scripts.maintenance.ingest_thetadata_quotes --probe 20260626
    python -m scripts.maintenance.ingest_thetadata_quotes --range 20260101 20260629
    python -m scripts.maintenance.ingest_thetadata_quotes --dates 20260626 20260627

VERIFY-ON-CONNECT (can't be checked until the Terminal is live):
  1. the SPXW root string (default "SPXW"; some vendors use "SPX" for index +
     a flag — confirm via --probe output).
  2. the exact response field names + TIMESTAMP format (the probe dumps raw so
     we finalize _parse_rows()). Defaults below follow the documented schema.
  3. that Options Standard actually returns index (SPXW) quotes (the caveat).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

import httpx

BASE_URL = "http://127.0.0.1:25503/v3/option/history/quote"
SYMBOL = "SPXW"
INTERVAL = "1m"            # bulk default: ~32k rows/day @ ATM±20. Use 1s/tick
                          # on-demand for specific contracts/windows (execution fidelity).
STRIKE_RANGE = 20          # ATM ± 20 strikes (= 41 strikes) — covers the band we trade
RIGHT = "both"

# ThetaData timestamps are NAIVE Eastern wall-clock (e.g. "2026-06-26T09:30:00.000"
# = RTH open). Parse as America/New_York (DST-correct) — NOT system-local (this
# box is Central -> would be 60min off) and NOT a fixed -4 (wrong in winter/EST).
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None


def _et_tz(naive: "dt.datetime"):
    """ET tzinfo for a naive datetime, DST-correct. zoneinfo if available, else
    manual US-Eastern DST (EDT UTC-4 from 2nd-Sun-Mar to 1st-Sun-Nov, else EST)."""
    if _ET is not None:
        return _ET
    y = naive.year
    mar = dt.date(y, 3, 8)
    dst_start = mar + dt.timedelta(days=(6 - mar.weekday()) % 7)
    nov = dt.date(y, 11, 1)
    dst_end = nov + dt.timedelta(days=(6 - nov.weekday()) % 7)
    edt = dst_start <= naive.date() < dst_end
    return dt.timezone(dt.timedelta(hours=-4 if edt else -5))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


async def fetch(client: httpx.AsyncClient, expiry: str, date: str, *,
                interval: str = INTERVAL, strike_range: int = STRIKE_RANGE,
                fmt: str = "ndjson") -> tuple[int, str]:
    params = {
        "symbol": SYMBOL, "expiration": expiry, "date": date,
        "right": RIGHT, "interval": interval, "strike_range": str(strike_range),
        "format": fmt,
    }
    r = await client.get(BASE_URL, params=params, timeout=120)
    return r.status_code, r.text


# ---------------------------------------------------------------------------
# Parse (VERIFY-ON-CONNECT: finalize against --probe output)
# ---------------------------------------------------------------------------


def _parse_rows(raw: str, fmt: str) -> list[dict]:
    """Best-effort parse of the documented NBBO fields. ndjson = one JSON object
    per line; json = list of objects (or v2 {header,response}). Returns dicts
    with keys: strike, right, timestamp, bid, ask, bid_size, ask_size,
    bid_exchange, ask_exchange. Robust to missing keys; the probe confirms shape.
    """
    import json
    rows: list[dict] = []
    raw = (raw or "").strip()
    if not raw:
        return rows
    if fmt == "ndjson":
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except Exception:
                continue
        return rows
    # json
    try:
        data = json.loads(raw)
    except Exception:
        return rows
    if isinstance(data, list):
        return [o for o in data if isinstance(o, dict)]
    if isinstance(data, dict):
        # v2-style {header:{format:[...]}, response:[[...]]}
        resp = data.get("response")
        hdr = (data.get("header") or {}).get("format")
        if isinstance(resp, list) and isinstance(hdr, list):
            return [dict(zip(hdr, row)) for row in resp if isinstance(row, list)]
        if isinstance(resp, list):
            return [o for o in resp if isinstance(o, dict)]
    return rows


def _g(d: dict, *names, default=None):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


def _to_ms(ts, date: str) -> int | None:
    """Coerce ThetaData timestamp -> UTC ms. ThetaData v3 returns ISO naive ET
    wall-clock; also handle epoch ms/s and ms-since-ET-midnight defensively."""
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            naive = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if naive.tzinfo is None:           # naive ET wall-clock -> localize to ET
                naive = naive.replace(tzinfo=_et_tz(naive))
            return int(naive.timestamp() * 1000)
        except Exception:
            return None
    try:
        v = float(ts)
        if v > 1e12:           # epoch ms
            return int(v)
        if v > 1e9:            # epoch s
            return int(v * 1000)
        base = dt.datetime.strptime(date, "%Y%m%d")   # ms since ET midnight
        base = base.replace(tzinfo=_et_tz(base))
        return int(base.timestamp() * 1000 + v)
    except (TypeError, ValueError):
        return None


def _qa(bid: float, ask: float) -> str:
    if bid <= 0 and ask <= 0:
        return "zero"
    if bid > ask:
        return "crossed"
    if bid == ask:
        return "locked"
    if ask > 0 and (ask - bid) / ((ask + bid) / 2) > 0.5:
        return "wide"
    return "ok"


def build_rows(raw: str, fmt: str, expiry: str, date: str, interval: str) -> list:
    from fef_sidecar.db.models import OptionQuoteHistory
    sd = dt.datetime.strptime(date, "%Y%m%d").strftime("%Y-%m-%d")
    out = []
    for d in _parse_rows(raw, fmt):
        bid = float(_g(d, "bid", default=0) or 0)
        ask = float(_g(d, "ask", default=0) or 0)
        ts_ms = _to_ms(_g(d, "timestamp", "ms_of_day", "time"), date)
        strike = _g(d, "strike")
        right = _g(d, "right")
        if ts_ms is None or strike is None or right is None:
            continue
        r = str(right).upper()
        r = "C" if r.startswith("C") else ("P" if r.startswith("P") else r)
        out.append(OptionQuoteHistory(
            symbol=SYMBOL, session_date=sd, expiry=expiry, strike=float(strike), right=r,
            timestamp_ms=ts_ms, bid=bid, ask=ask,
            bid_size=int(_g(d, "bid_size", default=0) or 0),
            ask_size=int(_g(d, "ask_size", default=0) or 0),
            bid_exchange=str(_g(d, "bid_exchange", default="") or "") or None,
            ask_exchange=str(_g(d, "ask_exchange", default="") or "") or None,
            quote_quality=_qa(bid, ask), interval=interval, source="thetadata",
        ))
    return out


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------


def persist(rows: list) -> int:
    """Fast idempotent bulk insert via sqlite executemany + INSERT OR IGNORE
    (relies on the unique index). Returns rows actually added."""
    import os
    import sqlite3
    if not rows:
        return 0
    db = os.path.expanduser("~/.fef-terminal/signal_log.db")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    data = [(now, r.symbol, r.session_date, r.expiry, r.strike, r.right, r.timestamp_ms,
             r.bid, r.ask, r.bid_size, r.ask_size, r.bid_exchange, r.ask_exchange,
             r.quote_quality, r.interval, r.source) for r in rows]
    c = sqlite3.connect(db, timeout=30)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        before = c.execute("SELECT COUNT(*) FROM option_quote_history").fetchone()[0]
        c.executemany(
            "INSERT OR IGNORE INTO option_quote_history "
            "(ingested_at,symbol,session_date,expiry,strike,right,timestamp_ms,bid,ask,"
            "bid_size,ask_size,bid_exchange,ask_exchange,quote_quality,interval,source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
        c.commit()
        after = c.execute("SELECT COUNT(*) FROM option_quote_history").fetchone()[0]
        return after - before
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


async def probe(date: str) -> None:
    """First-connect smoke test: tiny pull (ATM±2, this date's 0DTE), dump RAW so
    we confirm field names + timestamp format before trusting the bulk parser."""
    async with httpx.AsyncClient() as c:
        try:
            code, raw = await fetch(c, expiry=date, date=date, strike_range=2, fmt="ndjson")
        except httpx.ConnectError:
            print("CONNECT FAILED — is the Theta Terminal running + logged in on "
                  "localhost:25503? (Options Standard sub).")
            return
    print(f"HTTP {code}.  First 1500 chars of RAW ndjson response:\n{'-'*60}\n{raw[:1500]}\n{'-'*60}")
    parsed = _parse_rows(raw, "ndjson")
    print(f"parsed {len(parsed)} rows. first parsed dict:\n  {parsed[0] if parsed else '(none)'}")
    if parsed:
        built = build_rows(raw, "ndjson", date, date, INTERVAL)
        print(f"built {len(built)} OptionQuoteHistory rows. sample:")
        for b in built[:3]:
            print(f"  {b.strike}{b.right} ts={b.timestamp_ms} bid={b.bid} ask={b.ask} q={b.quote_quality}")
        print("\nIf field names/timestamps look wrong above, fix _parse_rows/_to_ms "
              "before bulk ingest.")


async def ingest_dates(dates: list[str], interval: str = INTERVAL) -> None:
    import os
    import sqlite3
    from fef_sidecar.db.engine import init_db
    await init_db()
    # resume cheaply: skip sessions already fully ingested (per-day inserts are
    # atomic, so "has any rows" == "done"). Re-run is then ~free after a crash.
    dbp = os.path.expanduser("~/.fef-terminal/signal_log.db")
    _c = sqlite3.connect(dbp)
    done = {r[0] for r in _c.execute("SELECT DISTINCT session_date FROM option_quote_history")}
    _c.close()
    total = 0
    async with httpx.AsyncClient() as c:
        for date in dates:
            sd = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
            if sd in done:
                continue                       # already ingested
            code, raw = 0, ""
            for attempt in range(3):           # retry transient errors / rate limits
                try:
                    code, raw = await fetch(c, expiry=date, date=date, interval=interval)
                    if code == 200:
                        break
                    if code == 429 or code >= 500:
                        await asyncio.sleep(2 * (attempt + 1)); continue
                    break                       # other non-200 -> don't retry
                except httpx.ConnectError:
                    print("CONNECT FAILED — start the Theta Terminal first."); return
                except Exception as e:
                    print(f"{date}: {type(e).__name__} (retry {attempt+1}/3)")
                    await asyncio.sleep(2 * (attempt + 1))
            if code != 200:
                print(f"{date}: HTTP {code} (skip — no 0DTE expiry / holiday / rate-limited)")
                await asyncio.sleep(0.3); continue
            rows = build_rows(raw, "ndjson", date, date, interval)
            added = persist(rows)
            qbad = sum(1 for r in rows if r.quote_quality != "ok")
            total += added
            print(f"{date}: {len(rows)} quotes parsed, {added} new ({qbad} flagged non-ok)")
            await asyncio.sleep(0.3)            # gentle pacing for long runs
    print(f"DONE — {total} new quote rows across {len(dates)} session(s)")


def _weekdays(start: str, end: str) -> list[str]:
    d0 = dt.datetime.strptime(start, "%Y%m%d").date()
    d1 = dt.datetime.strptime(end, "%Y%m%d").date()
    out = []
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += dt.timedelta(days=1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", metavar="YYYYMMDD", help="smoke-test one date, dump raw shape")
    ap.add_argument("--dates", nargs="+", metavar="YYYYMMDD", help="ingest these dates")
    ap.add_argument("--range", nargs=2, metavar=("START", "END"), help="ingest weekdays in range")
    ap.add_argument("--interval", default=INTERVAL, help="tick/1s/5s/.../1m (default 1m for bulk)")
    args = ap.parse_args()
    if args.probe:
        asyncio.run(probe(args.probe))
    elif args.dates:
        asyncio.run(ingest_dates(args.dates, interval=args.interval))
    elif args.range:
        asyncio.run(ingest_dates(_weekdays(args.range[0], args.range[1]), interval=args.interval))
    else:
        ap.print_help(); sys.exit(1)


if __name__ == "__main__":
    main()
