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
INTERVAL = "1s"
STRIKE_RANGE = 20          # ATM ± 20 strikes (= 41 strikes) — covers the band we trade
RIGHT = "both"
ET = dt.timezone(dt.timedelta(hours=-4))


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
    """Coerce ThetaData timestamp -> UTC ms. Handles: epoch ms, epoch s, ISO
    string, or ms-since-midnight-ET (combined with `date`). VERIFY via probe."""
    if ts is None:
        return None
    try:
        v = float(ts)
        if v > 1e12:           # already ms epoch
            return int(v)
        if v > 1e9:            # s epoch
            return int(v * 1000)
        # else: ms since ET midnight
        d = dt.datetime.strptime(date, "%Y%m%d").replace(tzinfo=ET)
        return int(d.timestamp() * 1000 + v)
    except (TypeError, ValueError):
        pass
    try:                       # ISO string
        s = str(ts).replace("Z", "+00:00")
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
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


async def persist(rows: list) -> int:
    from sqlalchemy.exc import IntegrityError
    from fef_sidecar.db.engine import get_session
    if not rows:
        return 0
    try:
        async with get_session() as s:
            s.add_all(rows)
            await s.flush()
        return len(rows)
    except IntegrityError:
        pass
    n = 0
    for r in rows:
        try:
            async with get_session() as s:
                s.add(r)
                await s.flush()
            n += 1
        except IntegrityError:
            continue
    return n


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


async def ingest_dates(dates: list[str]) -> None:
    from fef_sidecar.db.engine import init_db
    await init_db()
    total = 0
    async with httpx.AsyncClient() as c:
        for date in dates:
            try:
                code, raw = await fetch(c, expiry=date, date=date)
            except httpx.ConnectError:
                print("CONNECT FAILED — start the Theta Terminal first."); return
            if code != 200:
                print(f"{date}: HTTP {code} (skip — likely no 0DTE expiry / holiday)"); continue
            rows = build_rows(raw, "ndjson", date, date, INTERVAL)
            added = await persist(rows)
            qbad = sum(1 for r in rows if r.quote_quality != "ok")
            total += added
            print(f"{date}: {len(rows)} quotes parsed, {added} new ({qbad} flagged non-ok)")
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
    args = ap.parse_args()
    if args.probe:
        asyncio.run(probe(args.probe))
    elif args.dates:
        asyncio.run(ingest_dates(args.dates))
    elif args.range:
        asyncio.run(ingest_dates(_weekdays(args.range[0], args.range[1])))
    else:
        ap.print_help(); sys.exit(1)


if __name__ == "__main__":
    main()
