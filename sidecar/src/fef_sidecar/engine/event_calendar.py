"""Deterministic market-event calendar — feeds the EVENTS tile of /regime/live.

Ported from the validated scratchpad reference (ev_calendar.py, used by the
2026-07 event-conditioned drift study) with the hardcoded ranges extended
through end-2026 so the LIVE tile stays correct past the study window.

Event classes:
  FOMC        — statement days 14:00 ET. HARDCODED official schedule
                2022-2026 (high confidence; 2026-06-17 cross-checked against
                the app's own calendar_event table; H2-2026 dates are the
                Fed's published meeting calendar, day-2 statement days).
  CPI         — BLS release days 08:30 ET. APPROXIMATE — reconstructed
                offline; 2025 Q4 shutdown-disrupted; H2-2026 dates are
                schedule-pattern estimates. Always flagged approx.
  NFP         — first Friday 08:30 ET with known exceptions. APPROXIMATE.
                Oct-Dec 2025 dropped entirely (shutdown irregularity);
                months whose first Friday is a market holiday are dropped
                (release happens but there is no tradeable session).
  OPEX        — 3rd Friday of the month, rolled to the prior trading day
                when the market is closed (e.g. 2026-06-19 Juneteenth ->
                2026-06-18). Deterministic.
  EOM / QUARTER_END — last trading day of the month / quarter, computed on
                the true NYSE calendar (weekdays minus HOLIDAYS).

Pure date-string functions — no I/O, no timezone logic (callers pass ET
session dates, consistent with session_date_et elsewhere in the app).
"""

from __future__ import annotations

import datetime as _dt

# NYSE full-closure holidays 2022-2026 (2022-06/2026-06 validated against the
# DB session list by the drift study; H2-2026 appended from the NYSE calendar).
HOLIDAYS: frozenset[str] = frozenset({
    "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20",
    "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26",
    "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29",
    "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25",
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
    "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
    "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
})

# FOMC statement days (14:00 ET), official schedule. H2-2026 = published
# meeting calendar (Jul 28-29, Sep 15-16, Oct 27-28, Dec 8-9; day 2 listed).
FOMC_DATES: frozenset[str] = frozenset({
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15",
    "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14",
    "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
})

# CPI release days (08:30 ET) — APPROXIMATE throughout (see module docstring).
CPI_DATES: frozenset[str] = frozenset({
    "2022-01-12", "2022-02-10", "2022-03-10", "2022-04-12", "2022-05-11", "2022-06-10",
    "2022-07-13", "2022-08-10", "2022-09-13", "2022-10-13", "2022-11-10", "2022-12-13",
    "2023-01-12", "2023-02-14", "2023-03-14", "2023-04-12", "2023-05-10", "2023-06-13",
    "2023-07-12", "2023-08-10", "2023-09-13", "2023-10-12", "2023-11-14", "2023-12-12",
    "2024-01-11", "2024-02-13", "2024-03-12", "2024-04-10", "2024-05-15", "2024-06-12",
    "2024-07-11", "2024-08-14", "2024-09-11", "2024-10-10", "2024-11-13", "2024-12-11",
    "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10", "2025-05-13", "2025-06-11",
    "2025-07-15", "2025-08-12", "2025-09-11",
    "2025-10-24",  # Sep-data, shutdown-delayed
    "2025-12-18",  # Nov-data (no October-data release)
    "2026-01-13", "2026-02-11", "2026-03-11", "2026-04-10", "2026-05-12",
    "2026-06-11",  # from app calendar_event
    # H2-2026: schedule-pattern ESTIMATES (BLS calendar not hardcoded-verified)
    "2026-07-14", "2026-08-12", "2026-09-11", "2026-10-13", "2026-11-10", "2026-12-10",
})

# NFP: first Friday, with hardcoded exceptions; Oct-Dec 2025 dropped (shutdown).
NFP_EXCEPTIONS: dict[str, str] = {"2025-01": "2025-01-10", "2026-01": "2026-01-09"}
NFP_DROP_MONTHS: frozenset[str] = frozenset({"2025-10", "2025-11", "2025-12"})

# Kinds ordered by display priority (highest impact first).
KIND_PRIORITY: tuple[str, ...] = ("FOMC", "CPI", "NFP", "OPEX", "QUARTER_END", "EOM")


# ---------------------------------------------------------------------------
# Trading-day calendar (weekdays minus HOLIDAYS)
# ---------------------------------------------------------------------------


def is_fomc_day(date_str: str) -> bool:
    """True when date_str (YYYY-MM-DD ET) is an FOMC statement day (14:00 ET
    statement). Backs the scanner's FOMC-morning skip-gate."""
    return date_str in FOMC_DATES


def is_trading_day(date_str: str) -> bool:
    d = _dt.date.fromisoformat(date_str)
    return d.weekday() < 5 and date_str not in HOLIDAYS


def prior_trading_day(date_str: str) -> str:
    d = _dt.date.fromisoformat(date_str)
    for _ in range(10):
        d -= _dt.timedelta(days=1)
        if is_trading_day(d.isoformat()):
            return d.isoformat()
    return d.isoformat()  # pragma: no cover — >10 consecutive closures impossible


def next_trading_day(date_str: str) -> str:
    d = _dt.date.fromisoformat(date_str)
    for _ in range(10):
        d += _dt.timedelta(days=1)
        if is_trading_day(d.isoformat()):
            return d.isoformat()
    return d.isoformat()  # pragma: no cover


def month_trading_days(year: int, month: int) -> list[str]:
    d = _dt.date(year, month, 1)
    out: list[str] = []
    while d.month == month:
        ds = d.isoformat()
        if is_trading_day(ds):
            out.append(ds)
        d += _dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Event-date rules
# ---------------------------------------------------------------------------


def first_friday(year: int, month: int) -> _dt.date:
    d = _dt.date(year, month, 1)
    while d.weekday() != 4:
        d += _dt.timedelta(days=1)
    return d


def third_friday(year: int, month: int) -> _dt.date:
    return first_friday(year, month) + _dt.timedelta(days=14)


def opex_date(year: int, month: int) -> str:
    """Monthly OPEX session: 3rd Friday, or the prior trading day when the
    market is closed that Friday (e.g. 2026-06-19 Juneteenth -> 2026-06-18)."""
    d = third_friday(year, month).isoformat()
    return d if is_trading_day(d) else prior_trading_day(d)


def nfp_date(year: int, month: int) -> str | None:
    """APPROXIMATE NFP session: first Friday with hardcoded exceptions.
    None when the month is dropped or the first Friday is a market holiday
    (release happens but there is no tradeable session that day)."""
    ym = f"{year}-{month:02d}"
    if ym in NFP_DROP_MONTHS:
        return None
    if ym in NFP_EXCEPTIONS:
        return NFP_EXCEPTIONS[ym]
    d = first_friday(year, month).isoformat()
    return d if is_trading_day(d) else None


def is_eom_session(date_str: str) -> bool:
    """Last trading day of the date's month (true NYSE calendar)."""
    d = _dt.date.fromisoformat(date_str)
    days = month_trading_days(d.year, d.month)
    return bool(days) and date_str == days[-1]


def is_quarter_end_session(date_str: str) -> bool:
    d = _dt.date.fromisoformat(date_str)
    return d.month in (3, 6, 9, 12) and is_eom_session(date_str)


# ---------------------------------------------------------------------------
# Composite queries
# ---------------------------------------------------------------------------


def events_on(date_str: str) -> list[dict]:
    """All event flags for one session date, priority-ordered.
    Each: {kind, label, approx}. Empty list = clear day."""
    d = _dt.date.fromisoformat(date_str)
    out: list[dict] = []
    if date_str in FOMC_DATES:
        out.append({"kind": "FOMC", "label": "FOMC statement 2:00 PM ET", "approx": False})
    if date_str in CPI_DATES:
        out.append({"kind": "CPI", "label": "CPI 8:30 AM ET (approx date)", "approx": True})
    if nfp_date(d.year, d.month) == date_str:
        out.append({"kind": "NFP", "label": "NFP 8:30 AM ET (approx date)", "approx": True})
    if opex_date(d.year, d.month) == date_str:
        out.append({"kind": "OPEX", "label": "monthly OPEX", "approx": False})
    if is_quarter_end_session(date_str):
        out.append({"kind": "QUARTER_END", "label": "quarter-end", "approx": False})
    elif is_eom_session(date_str):
        out.append({"kind": "EOM", "label": "month-end", "approx": False})
    out.sort(key=lambda e: KIND_PRIORITY.index(e["kind"]))
    return out


def upcoming_events(date_str: str, within_sessions: int = 3) -> list[dict]:
    """Events on the next `within_sessions` trading days AFTER date_str.
    Each: {kind, label, approx, date, sessions_away} (sessions_away >= 1)."""
    out: list[dict] = []
    d = date_str
    for away in range(1, within_sessions + 1):
        d = next_trading_day(d)
        for e in events_on(d):
            out.append({**e, "date": d, "sessions_away": away})
    return out
