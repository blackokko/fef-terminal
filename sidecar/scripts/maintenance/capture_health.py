#!/usr/bin/env python
r"""READ-ONLY health report for the microstructure_sample / hft_sample capture.

Answers: for each ET session, how many rows did we capture, roughly what % of the
RTH session (09:30-16:00 ET) was covered, which symbols, and which trading days
are MISSING entirely (the gaps). Surfaces the month-long June 2026 gap and any
suspiciously thin sessions.

SAFETY: opens signal_log.db in sqlite read-only mode (mode=ro) with
PRAGMA query_only -- it cannot write, migrate, or lock the live DB's writer
(WAL gives it a consistent snapshot). Safe to run any time, incl. while the
sidecar is trading.

Usage:
    .\.venv\Scripts\python.exe scripts\maintenance\capture_health.py
    ... capture_health.py --db <path> --since 2026-05-01 --min-cov 50
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RTH_SECONDS = int(6.5 * 3600)  # 23400 -- full 09:30-16:00 session at 1 sample/s

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:
    pass

# 2026 NYSE holidays (so they aren't flagged as capture gaps).
HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
}


class SessionAgg:
    __slots__ = ("total", "rth_secs", "symbols", "first_ms", "last_ms")

    def __init__(self) -> None:
        self.total = 0
        self.rth_secs: set[int] = set()
        self.symbols: Counter = Counter()
        self.first_ms = 0
        self.last_ms = 0


def _scan(conn: sqlite3.Connection, table: str) -> dict[date, SessionAgg]:
    """One streaming pass over (timestamp_ms, symbol); bucket by ET session date.

    ET conversion is DST-correct via zoneinfo. RTH-second coverage counts the
    number of DISTINCT wall-clock seconds inside 09:30-16:00 ET that have at
    least one row -- i.e. 'was the capture actually running', independent of how
    many symbols were captured."""
    out: dict[date, SessionAgg] = defaultdict(SessionAgg)
    cur = conn.execute(f"SELECT timestamp_ms, symbol FROM {table}")
    for ms, symbol in cur:
        et = datetime.fromtimestamp(ms / 1000, ET)
        d = et.date()
        agg = out[d]
        agg.total += 1
        agg.symbols[symbol or "?"] += 1
        if agg.first_ms == 0 or ms < agg.first_ms:
            agg.first_ms = ms
        if ms > agg.last_ms:
            agg.last_ms = ms
        # RTH gate (weekday + 09:30-16:00)
        if et.weekday() < 5 and (et.hour, et.minute) >= (9, 30) and et.hour < 16:
            agg.rth_secs.add(int(ms // 1000))
    return out


def _fmt_et(ms: int) -> str:
    if not ms:
        return "--:--"
    return datetime.fromtimestamp(ms / 1000, ET).strftime("%H:%M")


def _cov_pct(agg: SessionAgg | None) -> float:
    if agg is None:
        return 0.0
    return min(100.0, 100.0 * len(agg.rth_secs) / RTH_SECONDS)


def main() -> None:
    ap = argparse.ArgumentParser(description="Microstructure/HFT capture health report (read-only)")
    ap.add_argument("--db", default=os.path.expanduser("~/.fef-terminal/signal_log.db"))
    ap.add_argument("--since", default=None, help="YYYY-MM-DD; only report sessions on/after this date")
    ap.add_argument("--min-cov", type=float, default=50.0, help="RTH coverage %% below which a session is flagged LOW")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=ON")
    try:
        micro = _scan(conn, "microstructure_sample")
        hft = _scan(conn, "hft_sample")
    finally:
        conn.close()

    all_dates = sorted(set(micro) | set(hft))
    if not all_dates:
        print("No microstructure_sample / hft_sample rows found.")
        return

    since = None
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").date()

    print("=" * 100)
    print("MICROSTRUCTURE / HFT CAPTURE HEALTH  (ET session dates; RTH = 09:30-16:00; 1 sample/s => 23,400/full session)")
    print(f"DB: {args.db}")
    print(f"data span: {all_dates[0]}  ->  {all_dates[-1]}   (report generated {datetime.now(ET):%Y-%m-%d %H:%M ET})")
    print("=" * 100)
    hdr = (f"{'session':<12}{'dow':<5}{'micro_rows':>11}{'m_cov%':>8}"
           f"{'hft_rows':>10}{'h_cov%':>8}{'first':>7}{'last':>7}  {'symbols':<22}{'flag'}")
    print(hdr)
    print("-" * 100)

    shown = 0
    for d in all_dates:
        if since and d < since:
            continue
        m = micro.get(d)
        h = hft.get(d)
        m_rows = m.total if m else 0
        h_rows = h.total if h else 0
        m_cov = _cov_pct(m)
        h_cov = _cov_pct(h)
        first_ms = min([x.first_ms for x in (m, h) if x and x.first_ms] or [0])
        last_ms = max([x.last_ms for x in (m, h) if x] or [0])

        # symbol summary (union, top by count)
        symct: Counter = Counter()
        for x in (m, h):
            if x:
                symct.update(x.symbols)
        sym_str = ",".join(f"{s}:{c}" for s, c in symct.most_common(3))
        if len(symct) > 3:
            sym_str += ",..."

        cov = max(m_cov, h_cov)
        if cov >= 90:
            flag = "OK"
        elif cov >= args.min_cov:
            flag = "PARTIAL"
        else:
            flag = "LOW"
        notes = []
        if h_rows and not m_rows:
            notes.append("no-micro")
        if m_rows and not h_rows:
            notes.append("no-hft")
        if "SPX" not in symct and "ES" in symct:
            notes.append("ES-only")
        if notes:
            flag += " (" + ",".join(notes) + ")"

        print(f"{str(d):<12}{d.strftime('%a'):<5}{m_rows:>11,}{m_cov:>8.1f}"
              f"{h_rows:>10,}{h_cov:>8.1f}{_fmt_et(first_ms):>7}{_fmt_et(last_ms):>7}  {sym_str:<22}{flag}")
        shown += 1

    print("-" * 100)
    print(f"{shown} session(s) with data.")

    # --- Missing trading days (the gaps) ---------------------------------
    start = all_dates[0]
    end = datetime.now(ET).date()
    have = set(all_dates)
    missing: list[date] = []
    dd = start
    while dd <= end:
        if dd.weekday() < 5 and dd not in HOLIDAYS_2026 and dd not in have:
            missing.append(dd)
        dd += timedelta(days=1)

    print()
    print("=" * 100)
    print(f"MISSING TRADING DAYS (weekday, non-holiday, ZERO capture) between {start} and {end}:")
    print("=" * 100)
    if not missing:
        print("  none -- every trading day in range has some capture.")
    else:
        by_month: dict[str, list[date]] = defaultdict(list)
        for md in missing:
            by_month[md.strftime("%Y-%m")].append(md)
        for mo in sorted(by_month):
            days = by_month[mo]
            print(f"  {mo}:  {len(days)} missing  ->  " + ", ".join(str(x.day) for x in days))
        print(f"\n  TOTAL MISSING TRADING DAYS: {len(missing)}")

    # --- headline flags --------------------------------------------------
    low = []
    for d in all_dates:
        if since and d < since:
            continue
        cov = max(_cov_pct(micro.get(d)), _cov_pct(hft.get(d)))
        if cov < args.min_cov:
            low.append((d, cov))
    print()
    print("=" * 100)
    print(f"THIN SESSIONS (RTH coverage < {args.min_cov:.0f}%):")
    print("=" * 100)
    if not low:
        print("  none.")
    else:
        for d, cov in low:
            print(f"  {d} ({d.strftime('%a')}): {cov:.1f}% RTH coverage")


if __name__ == "__main__":
    main()
