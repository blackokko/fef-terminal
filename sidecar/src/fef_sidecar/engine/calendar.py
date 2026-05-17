"""Calendar / event awareness — auto-pause new signals around macro events.

Seeds a baseline of recurring events on first startup:
  - FOMC: 8 meetings/year, hardcoded near-term dates
  - CPI: monthly, 2nd Tuesday or so at 08:30 ET
  - NFP: 1st Friday of each month, 08:30 ET
  - OPEX: 3rd Friday of each month, market close
  - SPX 0DTE expiry pin: every weekday 16:00 ET (built-in, not user-editable)

The engine consults `is_pause_window(now)` before broadcasting actionable
verdicts. If true, verdicts are demoted to WAIT with reasoning explaining
which event triggered the pause.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from fef_sidecar.db.models import CalendarEvent
from fef_sidecar.db.repository import SignalIterRepository

logger = logging.getLogger(__name__)


ET = timezone(timedelta(hours=-4))  # EST/EDT — close enough for our pause-window math


@dataclass(frozen=True)
class PauseDecision:
    paused: bool
    reason: str
    event: dict | None = None


# Seed dates for FOMC + CPI + NFP through end of 2026 (user can edit/add)
# Times are ET, 24h. Drawn from public Fed/BLS schedules.
_FOMC_DATES = [
    "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]
_FOMC_TIME = "14:00"

_CPI_DATES = [
    "2026-06-11", "2026-07-15", "2026-08-12", "2026-09-10", "2026-10-15",
    "2026-11-13", "2026-12-10",
]
_DATA_TIME = "08:30"

# First Friday of each month for NFP (computed at seed time)


def _first_friday(year: int, month: int) -> str:
    d = datetime(year, month, 1)
    # 0=Mon..6=Sun. First Friday = (4 - weekday) % 7 + 1
    offset = (4 - d.weekday()) % 7
    return (d + timedelta(days=offset)).strftime("%Y-%m-%d")


def _third_friday(year: int, month: int) -> str:
    """OPEX — 3rd Friday of the month."""
    d = datetime(year, month, 1)
    offset = (4 - d.weekday()) % 7
    first_friday = d + timedelta(days=offset)
    return (first_friday + timedelta(days=14)).strftime("%Y-%m-%d")


async def seed_calendar_if_empty() -> None:
    """Populate the CalendarEvent table with recurring events if it's empty.
    Idempotent — only runs when no rows exist."""
    existing = await SignalIterRepository.query_calendar_events()
    if existing:
        return
    seeded = 0

    for d in _FOMC_DATES:
        await SignalIterRepository.upsert_calendar_event(CalendarEvent(
            event_date=d, event_time_et=_FOMC_TIME, category="FOMC",
            title="FOMC Statement + Powell Presser", impact="HIGH",
            auto_pause_minutes_before=30, auto_pause_minutes_after=60,
        ))
        seeded += 1

    for d in _CPI_DATES:
        await SignalIterRepository.upsert_calendar_event(CalendarEvent(
            event_date=d, event_time_et=_DATA_TIME, category="CPI",
            title="US CPI Release", impact="HIGH",
            auto_pause_minutes_before=15, auto_pause_minutes_after=30,
        ))
        seeded += 1

    # NFP + OPEX for next 6 months
    today = datetime.now(ET)
    for i in range(6):
        d = today + timedelta(days=30 * i)
        nfp = _first_friday(d.year, d.month)
        opex = _third_friday(d.year, d.month)
        await SignalIterRepository.upsert_calendar_event(CalendarEvent(
            event_date=nfp, event_time_et=_DATA_TIME, category="NFP",
            title="Non-Farm Payrolls", impact="HIGH",
            auto_pause_minutes_before=15, auto_pause_minutes_after=30,
        ))
        await SignalIterRepository.upsert_calendar_event(CalendarEvent(
            event_date=opex, event_time_et="16:00", category="OPEX",
            title="Monthly OPEX (3rd Friday)", impact="MEDIUM",
            auto_pause_minutes_before=0, auto_pause_minutes_after=0,
        ))
        seeded += 2

    logger.info("Seeded %d calendar events", seeded)


async def is_pause_window(now: datetime | None = None) -> PauseDecision:
    """Check if 'now' falls inside any event's pause window.
    Returns PauseDecision with reason + event details if paused."""
    now = now or datetime.now(ET)
    today = now.strftime("%Y-%m-%d")

    events = await SignalIterRepository.query_calendar_events(
        from_date=today, to_date=today,
    )
    for e in events:
        if e.impact == "LOW":
            continue
        if e.event_time_et is None:
            # All-day event with HIGH impact = full-day pause
            if e.impact == "HIGH":
                return PauseDecision(
                    paused=True,
                    reason=f"Full-day pause: {e.title}",
                    event={"date": e.event_date, "category": e.category, "title": e.title},
                )
            continue
        # Timed event — compute pause window in ET
        try:
            hh, mm = e.event_time_et.split(":")
            event_dt = datetime.combine(
                datetime.strptime(e.event_date, "%Y-%m-%d").date(),
                time(int(hh), int(mm)),
                tzinfo=ET,
            )
        except (ValueError, AttributeError):
            continue

        pause_from = event_dt - timedelta(minutes=e.auto_pause_minutes_before)
        pause_to = event_dt + timedelta(minutes=e.auto_pause_minutes_after)

        if pause_from <= now <= pause_to:
            return PauseDecision(
                paused=True,
                reason=f"{e.category} pause window ({e.title} at {e.event_time_et} ET)",
                event={"date": e.event_date, "category": e.category, "title": e.title,
                       "time_et": e.event_time_et,
                       "pause_from": pause_from.isoformat(),
                       "pause_to": pause_to.isoformat()},
            )

    return PauseDecision(paused=False, reason="")
