"""Live progress bar for the ThetaData historical ingest. Polls the local DB and
redraws an in-place bar with rate + ETA. Run it in a terminal and watch it climb:

    python -m scripts.maintenance.ingest_progress              # live, redraws every 5s
    python -m scripts.maintenance.ingest_progress --once       # single snapshot
    python -m scripts.maintenance.ingest_progress --interval 3 # custom refresh

Reads the DB only — completely independent of the ingest job (safe to start/stop
anytime). Ctrl+C to quit the monitor (does NOT affect the ingest).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
# Estimated total 0DTE sessions in 2022-01-03 .. 2026-06-26 (Mon/Wed/Fri from
# Jan 2022, full daily from ~May 2022). Used only for the bar %, labeled "~".
TOTAL_EST = 1099
FWD_CUTOFF = "2025-07-01"   # leftover 2025-07+/2026 rows aren't part of the forward pass


def snapshot(c):
    rows = c.execute("SELECT COUNT(*) FROM option_quote_history").fetchone()[0]
    sess = c.execute("SELECT COUNT(DISTINCT session_date) FROM option_quote_history").fetchone()[0]
    frontier = c.execute(
        "SELECT MAX(session_date) FROM option_quote_history WHERE session_date < ?", (FWD_CUTOFF,)
    ).fetchone()[0]
    size = os.path.getsize(DB) / 1e9 if os.path.exists(DB) else 0.0
    return rows, sess, frontier or "—", size


def bar(pct, width=32):
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


def fmt_eta(minutes):
    if minutes is None or minutes != minutes or minutes <= 0:
        return "—"
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    try:  # Windows consoles default to cp1252 and choke on the bar glyphs
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    t0 = time.monotonic()
    _, sess0, _, _ = snapshot(c)
    last_sess, stalls = sess0, 0
    try:
        while True:
            rows, sess, frontier, size = snapshot(c)
            pct = min(100.0, sess / TOTAL_EST * 100)
            elapsed = time.monotonic() - t0
            gained = sess - sess0
            rate = (gained / elapsed * 60) if elapsed > 5 and gained > 0 else 0.0  # sessions/min
            eta = ((TOTAL_EST - sess) / rate) if rate > 0 else None
            advancing = sess > last_sess
            stalls = 0 if advancing else stalls + 1
            last_sess = sess
            state = "▶ pulling" if (advancing or stalls < 3) else "⏸ idle (done/paused?)"
            line = (f"\r[{bar(pct)}] {pct:5.1f}%  {sess:>4}/~{TOTAL_EST} sessions  "
                    f"{rows/1e6:5.1f}M rows  {size:4.1f}GB  at {frontier}  "
                    f"{rate:4.1f}/min  ETA {fmt_eta(eta):>5}  {state}   ")
            sys.stdout.write(line)
            sys.stdout.flush()
            if args.once or sess >= TOTAL_EST:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        print()
        c.close()


if __name__ == "__main__":
    main()
