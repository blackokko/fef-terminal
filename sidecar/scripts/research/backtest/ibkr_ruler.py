"""Shared INDEPENDENT IBKR SPX 1-min ruler for the comprehensive 2022-2026 re-runs.

The QuantData stockPrice ruler only reaches 2025-26. This reads the IBKR SPX 1-min cash bars
backfilled into an isolated scratch DB (independent of the ThetaData option chain -> no spot-
reconstruction artifact) so every flow test can be re-run across the full 2022-2026 history.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

import numpy as np

ET = ZoneInfo("America/New_York")
IBKR_DB = (r"C:\Users\diant\AppData\Local\Temp\claude\C--Users-diant"
           r"\13885fae-da20-4300-a83a-94697ac91ce4\scratchpad\ibkr_spx_1m.db")


def ibkr_open():
    return sqlite3.connect(f"file:{IBKR_DB}?mode=ro", uri=True)


def ibkr_series(ic, d):
    """(ts_ms[], close[]) RTH from the IBKR scratch DB for session d. None if sparse/missing.
    Glitch-filtered to a per-session median +/-5% band (same as QuantData real_series)."""
    y, m, dd = map(int, d.split("-"))
    lo = int(dt.datetime(y, m, dd, 0, 0, tzinfo=ET).timestamp() * 1000)
    hi = lo + 86_400_000
    rows = ic.execute("SELECT ts_ms, close FROM bar WHERE ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                      (lo, hi)).fetchall()
    if len(rows) < 30:
        return None
    ts = np.array([r[0] for r in rows], np.int64)
    px = np.array([r[1] for r in rows], float)
    med = np.median(px)
    mask = np.abs(px - med) <= 0.05 * med
    ts, px = ts[mask], px[mask]
    return (ts, px) if len(ts) >= 30 else None
