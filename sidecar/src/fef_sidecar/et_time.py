"""US Eastern / Central wall-clock time, DST-correct (zoneinfo).

The sidecar used to hard-code Eastern Time as a fixed UTC-4 offset
(``timezone(timedelta(hours=-4))``) in ~50 places, and Central Time as a fixed
UTC-5 in a few more. Those offsets are right only in daylight time. From the
first Sunday in November to the second Sunday in March, Eastern is UTC-5, and
each of those sites ran an hour off: the loop's market-hours window became
08:30-15:00 real ET, session dates rolled over at 23:00 ET, and so on. Every
site that used a fixed offset now goes through this module. A few modules
already used zoneinfo directly and were left alone (spread_auto_trader,
theta_flow_ingest, strikes, vix_daily_ingest, the nightly scheduler, the
scanner's in-session window); they are DST-correct too.

Which function to use:

- The current ET time or date: ``now_et()`` / ``today_et()``. Both read the
  clock through ``_utc_now()``, so a test can freeze every clock that reads
  through this module by patching that one name
  (``monkeypatch.setattr(et_time, "_utc_now", ...)``). It does not freeze the
  modules listed above, nor ``multi_symbol_scanner._now_et_parts()`` and
  ``option_chain_capture._within_rth()`` when they are called with no argument.
- A historical instant: ``to_et(dt)``, ``et_from_ms(ms)``,
  ``session_date_et(ms)``. zoneinfo applies the offset in force at that
  instant (EDT for a July fill, EST for a December fill), never the offset in
  force now.
- An ET wall-clock time on a given date: ``datetime(..., tzinfo=ET)``.
- Central Time: ``CT`` / ``to_ct(dt)``.

Naive datetimes passed to ``to_et``/``to_ct`` are taken as UTC, which is how
the DB stores them (e.g. ``submitted_at``).

Windows has no system IANA database, so zoneinfo reads the ``tzdata`` package,
declared in ``pyproject.toml`` (``tzdata>=2025.2``; ib_async pulls it too). If it is ever missing,
importing this module raises ``ZoneInfoNotFoundError`` at startup. That is
deliberate: the old per-site fallbacks silently went back to a fixed offset,
which is the bug this module fixes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")


def _utc_now() -> datetime:
    """The one wall-clock read behind now_et()/today_et(). Tests patch this."""
    return datetime.now(timezone.utc)


def now_et() -> datetime:
    """The current time as a tz-aware America/New_York datetime."""
    return _utc_now().astimezone(ET)


def today_et() -> str:
    """The current ET calendar date, 'YYYY-MM-DD'."""
    return now_et().strftime("%Y-%m-%d")


def to_et(dt: datetime) -> datetime:
    """``dt`` in ET, at the offset in force at that instant. Naive = UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET)


def to_ct(dt: datetime) -> datetime:
    """``dt`` in Central Time, at the offset in force at that instant. Naive = UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CT)


def et_from_ms(ts_ms: float) -> datetime:
    """Epoch milliseconds as a tz-aware ET datetime."""
    return datetime.fromtimestamp(ts_ms / 1000, ET)


def session_date_et(ts_ms: float) -> str:
    """The ET calendar date, 'YYYY-MM-DD', of an epoch-ms instant."""
    return et_from_ms(ts_ms).strftime("%Y-%m-%d")
