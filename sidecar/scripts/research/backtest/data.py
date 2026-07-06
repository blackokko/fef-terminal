"""Read-only access to historical option quotes + parity-reconstructed SPX spot.

We have no historical underlying bars for 2022-2025, so the underlying price path
is reconstructed from the option chain via put-call parity (0DTE, r~=0):
    spot ~= strike + C_mid - P_mid   (taken at the money, median of nearest strikes)
"""
from __future__ import annotations

import os
import sqlite3
import statistics
from zoneinfo import ZoneInfo

DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
ET = ZoneInfo("America/New_York")
BAD = {"crossed", "locked", "zero"}  # quote qualities we must not fill against


def connect():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def sessions(con, start, end):
    """Distinct session_date strings with data in [start, end] (YYYY-MM-DD)."""
    return [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_quote_history "
        "WHERE session_date BETWEEN ? AND ? ORDER BY session_date", (start, end))]


class Session:
    """All quotes for one session loaded once and indexed for fast in-memory access."""

    def __init__(self, con, date):
        self.date = date
        self.q = {}  # (ts_ms, strike, right) -> (bid, ask, quality)
        mins, strikes = set(), set()
        for ts, k, r, bid, ask, qual in con.execute(
            "SELECT timestamp_ms,strike,right,bid,ask,quote_quality "
            "FROM option_quote_history WHERE session_date=?", (date,)):
            self.q[(ts, k, r)] = (bid, ask, qual)
            mins.add(ts)
            strikes.add(k)
        self.minutes = sorted(mins)
        self.strikes = sorted(strikes)
        self._spot = {}

    def empty(self):
        return not self.minutes

    def quote(self, ts, strike, right):
        return self.q.get((ts, strike, right))

    def mid(self, ts, strike, right):
        v = self.q.get((ts, strike, right))
        if not v or v[2] in BAD:
            return None
        return (v[0] + v[1]) / 2

    def spot(self, ts):
        """Put-call-parity forward at `ts`: median of (K + C_mid - P_mid) over the
        5 strikes where call and put mids are closest (i.e. nearest the money)."""
        if ts in self._spot:
            return self._spot[ts]
        fwds = []
        for k in self.strikes:
            c = self.mid(ts, k, "C")
            p = self.mid(ts, k, "P")
            if c is not None and p is not None:
                fwds.append((abs(c - p), k + c - p))
        if not fwds:
            self._spot[ts] = None
            return None
        fwds.sort()
        s = statistics.median([f for _, f in fwds[:5]])
        self._spot[ts] = s
        return s

    def atm(self, ts):
        s = self.spot(ts)
        if s is None:
            return None
        return min(self.strikes, key=lambda k: abs(k - s))

    def minute_at_or_after(self, ts):
        for m in self.minutes:
            if m >= ts:
                return m
        return None

    def minute_at_or_before(self, ts):
        prev = None
        for m in self.minutes:
            if m > ts:
                break
            prev = m
        return prev


def load_oi(con, session_date):
    """Start-of-day per-strike open interest: (strike, right) -> contracts.

    Shared harness util (relocated from the archived gex_features probe). Reads the
    option_open_interest table for one session.
    """
    out = {}
    for k, r, oi in con.execute(
        "SELECT strike,right,open_interest FROM option_open_interest WHERE session_date=?",
        (session_date,)):
        out[(float(k), r)] = int(oi)
    return out


def roc_thrust(sess, thrust_pts, lookback_min, start_min=5, end_min=360):
    """Momentum-confirmed entry: the first minute spot has moved >= thrust_pts over the
    last lookback_min, in the thrust direction (CALL on up-thrust, PUT on down-thrust).
    First trigger per session. Shared harness util (relocated from the archived momentum
    probe).
    """
    t_open = sess.minutes[0]
    for ts in sess.minutes:
        dm = (ts - t_open) / 60000
        if dm < start_min:
            continue
        if dm > end_min:
            break
        s = sess.spot(ts)
        if s is None:
            continue
        back = sess.minute_at_or_before(ts - lookback_min * 60000)
        sb = sess.spot(back) if back else None
        if sb is None:
            continue
        if s - sb >= thrust_pts:
            return (ts, "CALL")
        if s - sb <= -thrust_pts:
            return (ts, "PUT")
    return None
