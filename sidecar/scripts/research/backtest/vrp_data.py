"""Shared market-data loaders for the VRP term-structure study (parts 1-3).

READ-ONLY. All series come from the main signal_log.db `historical_bar` table:
  SPX 1-min  (2022-01-03..2026+)  -> independent underlying ruler + daily closes
  VIX / VIX9D / VIX3M  1-day      -> implied vol at 30d / 9d / ~93d tenor
No option chain is used to reconstruct spot (avoids the parity-spot artifact,
MEMORY [[validate-against-independent-price]]).
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
from collections import defaultdict
from zoneinfo import ZoneInfo

import numpy as np

ET = ZoneInfo("America/New_York")
DB = os.path.expanduser("~/.fef-terminal/signal_log.db")

# split boundaries (from holdout.py — duplicated so this module is self-contained)
DEV_START, DEV_END = "2022-01-01", "2025-08-12"
HOLDOUT_START, HOLDOUT_END = "2025-08-13", "2026-06-26"


def connect():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def et_date(ms):
    return dt.datetime.fromtimestamp(ms / 1000, ET).strftime("%Y-%m-%d")


def split_of(date):
    return "DEV" if date <= DEV_END else "HOLD"


def load_spx_daily(con):
    """Ordered [(date_str, close)] — the last RTH 1-min bar's close per ET session.
    Also returns a {date: close} dict.  Uses SPX 1-min from historical_bar (independent
    of the option chain)."""
    rows = con.execute(
        "SELECT timestamp_ms, close FROM historical_bar "
        "WHERE symbol='SPX' AND bar_size='1 min' ORDER BY timestamp_ms").fetchall()
    last = {}          # date -> (ts, close) of the latest bar seen that day
    for ts, c in rows:
        if c is None or c <= 0:
            continue
        d = et_date(ts)
        if d not in last or ts > last[d][0]:
            last[d] = (ts, c)
    daily = sorted((d, v[1]) for d, v in last.items())
    return daily, {d: c for d, c in daily}


def load_vix(con, symbol):
    """{date_str: close} for a daily VIX-family symbol (VIX / VIX9D / VIX3M).
    The 1-day bar is stamped at the 16:00 ET close, so the ET date is the reading's date."""
    out = {}
    for ts, c in con.execute(
        "SELECT timestamp_ms, close FROM historical_bar "
        "WHERE symbol=? AND bar_size='1 day' ORDER BY timestamp_ms", (symbol,)):
        if c is not None and c > 0:
            out[et_date(ts)] = float(c)
    return out


def spx_intraday(con, date):
    """(ts_ms[], close[]) RTH 1-min SPX for one ET session, or None if sparse.
    Independent underlying ruler for entry-time spot in the real-data (part 2) sim."""
    y, m, dd = map(int, date.split("-"))
    lo = int(dt.datetime(y, m, dd, 0, 0, tzinfo=ET).timestamp() * 1000)
    hi = lo + 86_400_000
    rows = con.execute(
        "SELECT timestamp_ms, close FROM historical_bar "
        "WHERE symbol='SPX' AND bar_size='1 min' AND timestamp_ms>=? AND timestamp_ms<? "
        "ORDER BY timestamp_ms", (lo, hi)).fetchall()
    if len(rows) < 30:
        return None
    ts = np.array([r[0] for r in rows], np.int64)
    px = np.array([r[1] for r in rows], float)
    return ts, px


def forward_rvol(daily, h, annualize=252.0):
    """Forward realized vol (annualized, %) over the next `h` trading days for each date.
    Zero-mean / variance-swap convention: RV_t = sqrt(ann/h * sum_{i=1..h} r_i^2)*100,
    r_i = log return.  Returns {date: rv_pct} only where the full forward window exists."""
    dates = [d for d, _ in daily]
    px = np.array([c for _, c in daily], float)
    logret = np.empty(len(px))
    logret[0] = np.nan
    logret[1:] = np.log(px[1:] / px[:-1])
    out = {}
    n = len(dates)
    for i in range(n):
        j = i + h
        if j >= n:
            break
        seg = logret[i + 1:j + 1]                 # next h daily returns
        if np.any(~np.isfinite(seg)) or len(seg) < h:
            continue
        rv = np.sqrt(annualize / h * np.sum(seg * seg)) * 100.0
        out[dates[i]] = float(rv)
    return out
