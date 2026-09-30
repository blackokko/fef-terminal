"""DST pins for the shared ET/CT helper (fef_sidecar.et_time) and the call
sites that used a fixed UTC-4 for Eastern (and a fixed UTC-5 for Central).

2026 US transitions:
  spring forward  Sun 2026-03-08  02:00 EST -> 03:00 EDT   (07:00Z; CT at 08:00Z)
  fall back       Sun 2026-11-01  02:00 EDT -> 01:00 EST   (06:00Z; CT at 07:00Z)
Trading days either side: Fri 03-06 (EST) | Mon 03-09 (EDT),
                          Fri 10-30 (EDT) | Mon 11-02 (EST).

"Now" is frozen by patching et_time._utc_now, the one clock read behind
now_et()/today_et(). Cases marked "fixed-offset bug" are ones the old
timezone(timedelta(hours=-4)) got wrong.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from sqlmodel import select

from fef_sidecar import api, et_time, loop
from fef_sidecar.db import dispose_db, init_db, repository
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import CalendarEvent, IBKRExecution
from fef_sidecar.engine import calendar as cal
from fef_sidecar.engine import chop_gate as cg
from fef_sidecar.engine import counterfactual as cf
from fef_sidecar.engine import expiry_resolver as er
from fef_sidecar.engine import multi_symbol_scanner as mss
from fef_sidecar.engine import option_chain_capture as occ
from fef_sidecar.engine import perf_attribution as pa
from fef_sidecar.engine import regime, regime_live
from fef_sidecar.engine import regime_backfill as rb
from fef_sidecar.engine import spx_target_watcher as stw
from fef_sidecar.engine import what_if_backtest as wib
from fef_sidecar.engine.signals import shadow_vwap_loss_short as svls
from fef_sidecar.engine.signals import vwap
from fef_sidecar.engine.signals.dale import Bar


# Expected values are built from this zone, not from et_time.ET, so a broken
# helper cannot make its own expectations agree with it.
NY = ZoneInfo("America/New_York")


def _utc(y: int, mo: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, mo, d, hh, mm, tzinfo=UTC)


def _et(y: int, mo: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, mo, d, hh, mm, tzinfo=NY)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.fixture
def freeze(monkeypatch):
    """freeze(instant): every now_et()/today_et() in the app reads `instant`."""
    def _freeze(instant: datetime) -> None:
        assert instant.tzinfo is not None
        monkeypatch.setattr(et_time, "_utc_now", lambda: instant.astimezone(UTC))
    return _freeze


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("instant, et_hours, ct_hours", [
    (_utc(2026, 3, 8, 6, 59), -5, -6),   # 01:59 EST / 00:59 CST
    (_utc(2026, 3, 8, 7, 0), -4, -6),    # 03:00 EDT / 01:00 CST (CT changes an hour later)
    (_utc(2026, 3, 8, 8, 0), -4, -5),    # 04:00 EDT / 03:00 CDT
    (_utc(2026, 11, 1, 5, 59), -4, -5),  # 01:59 EDT / 00:59 CDT
    (_utc(2026, 11, 1, 6, 0), -5, -5),   # 01:00 EST / 01:00 CDT
    (_utc(2026, 11, 1, 7, 0), -5, -6),   # 02:00 EST / 01:00 CST
])
def test_helper_offsets_each_side_of_both_transitions(instant, et_hours, ct_hours):
    assert et_time.to_et(instant).utcoffset() == timedelta(hours=et_hours)
    assert et_time.et_from_ms(_ms(instant)).utcoffset() == timedelta(hours=et_hours)
    assert et_time.to_ct(instant).utcoffset() == timedelta(hours=ct_hours)
    # Naive input is UTC (the DB convention), not the machine's local time.
    # (Compared as timestamps: PEP 495 makes == between zones False inside the
    # repeated fall-back hour, even for the same instant.)
    assert et_time.to_et(instant.replace(tzinfo=None)).timestamp() == instant.timestamp()
    assert et_time.to_ct(instant.replace(tzinfo=None)).timestamp() == instant.timestamp()


def test_now_and_today_follow_the_frozen_clock(freeze):
    freeze(_utc(2026, 11, 3, 4, 30))             # Mon 23:30 EST
    assert et_time.now_et() == _et(2026, 11, 2, 23, 30)
    assert et_time.now_et().utcoffset() == timedelta(hours=-5)
    assert et_time.today_et() == "2026-11-02"    # fixed-offset bug: 2026-11-03
    freeze(_utc(2026, 10, 31, 3, 30))            # Fri 23:30 EDT
    assert et_time.now_et().utcoffset() == timedelta(hours=-4)
    assert et_time.today_et() == "2026-10-30"


# ---------------------------------------------------------------------------
# loop._in_market_hours_et  (spread tick loop, bar-stall watchdog, pattern trigger)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("instant, expected", [
    # Fri 2026-03-06, EST: 09:30-16:00 ET = 14:30Z-21:00Z
    (_utc(2026, 3, 6, 13, 30), False),   # 08:30 EST (fixed-offset bug: open)
    (_utc(2026, 3, 6, 14, 29), False),
    (_utc(2026, 3, 6, 14, 30), True),
    (_utc(2026, 3, 6, 20, 30), True),    # 15:30 EST (fixed-offset bug: closed)
    (_utc(2026, 3, 6, 21, 0), True),
    (_utc(2026, 3, 6, 21, 1), False),
    (_utc(2026, 3, 8, 15, 0), False),    # Sunday of the change
    # Mon 2026-03-09, EDT: 13:30Z-20:00Z
    (_utc(2026, 3, 9, 13, 29), False),
    (_utc(2026, 3, 9, 13, 30), True),
    (_utc(2026, 3, 9, 20, 0), True),
    (_utc(2026, 3, 9, 20, 1), False),
    # Fri 2026-10-30, EDT
    (_utc(2026, 10, 30, 13, 29), False),
    (_utc(2026, 10, 30, 13, 30), True),
    (_utc(2026, 10, 30, 20, 0), True),
    (_utc(2026, 10, 30, 20, 1), False),
    (_utc(2026, 11, 1, 15, 0), False),   # Sunday of the change
    # Mon 2026-11-02, EST
    (_utc(2026, 11, 2, 13, 30), False),  # 08:30 EST (fixed-offset bug: open)
    (_utc(2026, 11, 2, 14, 29), False),
    (_utc(2026, 11, 2, 14, 30), True),
    (_utc(2026, 11, 2, 20, 30), True),   # 15:30 EST (fixed-offset bug: closed)
    (_utc(2026, 11, 2, 21, 0), True),
    (_utc(2026, 11, 2, 21, 1), False),
])
def test_in_market_hours_et_both_sides_of_both_transitions(freeze, instant, expected):
    freeze(instant)
    assert loop._in_market_hours_et() is expected


# ---------------------------------------------------------------------------
# "Today" in ET: session_date for new rows, 0DTE expiry, expiry-cache key
# ---------------------------------------------------------------------------

_MIDNIGHT_CASES = [
    (_utc(2026, 3, 7, 4, 59), "2026-03-06"),    # Fri 23:59 EST (fixed-offset bug: 03-07)
    (_utc(2026, 3, 7, 5, 0), "2026-03-07"),     # Sat 00:00 EST
    (_utc(2026, 3, 10, 3, 59), "2026-03-09"),   # Mon 23:59 EDT
    (_utc(2026, 3, 10, 4, 0), "2026-03-10"),    # Tue 00:00 EDT
    (_utc(2026, 10, 31, 3, 59), "2026-10-30"),  # Fri 23:59 EDT
    (_utc(2026, 10, 31, 4, 0), "2026-10-31"),   # Sat 00:00 EDT
    (_utc(2026, 11, 3, 4, 59), "2026-11-02"),   # Mon 23:59 EST (fixed-offset bug: 11-03)
    (_utc(2026, 11, 3, 5, 0), "2026-11-03"),    # Tue 00:00 EST
]


@pytest.mark.parametrize("instant, day", _MIDNIGHT_CASES)
def test_today_et_rolls_over_at_et_midnight_everywhere(freeze, instant, day):
    freeze(instant)
    ymd = day.replace("-", "")
    assert loop._today_et() == day
    assert repository._today_et() == day
    assert mss._today_et() == day
    assert er._today_et_session() == day
    assert er._today_et_ymd() == ymd
    assert occ._today_session_date_et() == day
    assert occ._today_expiry_yyyymmdd() == ymd
    assert stw._et_now_hhmm_ymd()[1] == ymd


@pytest.mark.asyncio
async def test_expiry_resolver_spx_0dte_is_the_et_date(freeze):
    state = SimpleNamespace()
    freeze(_utc(2026, 11, 3, 4, 30))    # Mon 23:30 EST (fixed-offset bug: Tuesday's expiry)
    assert await er.resolve_nearest_expiry(state, "SPX") == "20261102"
    freeze(_utc(2026, 11, 3, 5, 30))    # Tue 00:30 EST: new session, new cache key
    assert await er.resolve_nearest_expiry(state, "SPX") == "20261103"
    freeze(_utc(2026, 3, 10, 3, 30))    # Mon 23:30 EDT
    assert await er.resolve_nearest_expiry(state, "SPX") == "20260309"
    assert set(state.expiry_cache_by_key) == {
        ("SPX", "2026-11-02"), ("SPX", "2026-11-03"), ("SPX", "2026-03-09")}


# ---------------------------------------------------------------------------
# Historical instants: session date at the instant's OWN offset, whatever "now" is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("instant, day", _MIDNIGHT_CASES)
def test_session_date_of_a_timestamp_uses_its_own_offset(freeze, instant, day):
    freeze(_utc(2026, 7, 15, 15, 0))    # "now" is mid-July EDT; must not leak in
    ms = _ms(instant)
    assert et_time.session_date_et(ms) == day
    assert loop._et_date_from_ms(ms) == day      # ES session VWAP key (refresh_es_vwap)
    assert cg.session_date_et(ms) == day         # chop-gate session grouping
    assert vwap.session_key(ms) == day           # anchored-VWAP session anchor
    assert svls._et_date(ms) == day              # frozen vwap_loss_short shadow
    assert mss._now_et_parts(ms)[0] == day


def _fill(exec_id: str, when: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        execution=SimpleNamespace(
            execId=exec_id, time=when, side="BOT", shares=1.0, price=1.0,
            cumQty=1.0, avgPrice=1.0, orderId=1, permId=1, acctNumber="DU0000000"),
        contract=SimpleNamespace(
            secType="OPT", symbol="SPY", strike=500.0, right="C",
            lastTradeDateOrContractMonth="20261102"),
        commissionReport=None,
    )


@pytest.fixture
async def _db():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.mark.asyncio
async def test_fill_session_date_uses_the_fill_times_own_offset(_db, freeze):
    """api._persist_ibkr_executions stamps ibkr_execution.session_date, which
    every per-session P&L and daily_review query filters on."""
    freeze(_utc(2026, 7, 15, 15, 0))
    cases = {
        "spring-est-2359": (_utc(2026, 3, 7, 4, 59), "2026-03-06"),   # fixed-offset bug
        "spring-edt-rth": (_utc(2026, 3, 9, 19, 59), "2026-03-09"),
        "fall-edt-2359": (_utc(2026, 10, 31, 3, 59), "2026-10-30"),
        "fall-est-rth": (_utc(2026, 11, 2, 20, 59), "2026-11-02"),
        "fall-est-2359": (_utc(2026, 11, 3, 4, 59), "2026-11-02"),     # fixed-offset bug
        "fall-est-naive": (datetime(2026, 11, 3, 4, 59), "2026-11-02"),  # naive = UTC
    }
    n = await api._persist_ibkr_executions([_fill(k, t) for k, (t, _) in cases.items()])
    assert n == len(cases)
    async with get_session() as s:
        rows = (await s.exec(select(IBKRExecution))).all()
    assert {r.exec_id: r.session_date for r in rows} == {k: d for k, (_, d) in cases.items()}


# ---------------------------------------------------------------------------
# VWAP session anchor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("first_bar, real_midnight, old_midnight", [
    # EST: ET midnight is 05:00Z; the fixed UTC-4 anchored at 04:00Z
    (_utc(2026, 3, 7, 3, 57), _utc(2026, 3, 7, 5, 0), _utc(2026, 3, 7, 4, 0)),
    (_utc(2026, 11, 3, 3, 57), _utc(2026, 11, 3, 5, 0), _utc(2026, 11, 3, 4, 0)),
    # EDT: both at 04:00Z
    (_utc(2026, 3, 10, 2, 57), _utc(2026, 3, 10, 4, 0), None),
    (_utc(2026, 10, 31, 2, 57), _utc(2026, 10, 31, 4, 0), None),
])
def test_vwap_session_anchor_resets_at_et_midnight(first_bar, real_midnight, old_midnight):
    bars = [
        Bar(ts_ms=_ms(first_bar + timedelta(minutes=i)), open=100.0 + i, high=100.0 + i,
            low=100.0 + i, close=100.0 + i, volume=1.0)
        for i in range(70)
    ]
    states = vwap.anchored_vwap(bars, vwap.session_key)
    # With rising prices the running VWAP equals the bar's own typical price
    # only on the first bar of a session.
    resets = [datetime.fromtimestamp(b.ts_ms / 1000, UTC)
              for b, s in zip(bars, states) if s.vwap == (b.high + b.low + b.close) / 3]
    assert resets == [first_bar, real_midnight]
    assert old_midnight not in resets


@pytest.mark.parametrize("ts, monday", [
    (_utc(2026, 11, 2, 4, 30), "2026-10-26"),   # Sun 23:30 EST (fixed-offset bug: 11-02)
    (_utc(2026, 11, 2, 5, 30), "2026-11-02"),   # Mon 00:30 EST
    (_utc(2026, 3, 9, 3, 30), "2026-03-02"),    # Sun 23:30 EDT
    (_utc(2026, 3, 9, 4, 30), "2026-03-09"),    # Mon 00:30 EDT
])
def test_vwap_week_key_is_the_et_monday(ts, monday):
    assert vwap.week_key(_ms(ts)) == monday


def test_rth_session_keys_match_the_old_fixed_offset_all_of_2026():
    """The vwap_loss_short shadow is frozen by policy and scores ES RTH bars.
    Its date keys must not move on any RTH minute of 2026, EDT or EST, so the
    switch to zoneinfo leaves the frozen definition's output unchanged."""
    fixed = timezone(timedelta(hours=-4))   # the old convention, as the reference
    day = datetime(2026, 1, 1)
    checked = 0
    while day.year == 2026:
        if day.weekday() < 5:
            for minute in range(9 * 60, 17 * 60 + 1, 3):   # 09:00-17:00 ET, wider than RTH
                t = datetime(day.year, day.month, day.day, minute // 60, minute % 60,
                             tzinfo=NY)
                ms = _ms(t)
                old = datetime.fromtimestamp(ms / 1000, fixed)
                old_key = old.strftime("%Y-%m-%d")
                assert vwap.session_key(ms) == old_key
                assert svls._et_date(ms) == old_key
                assert vwap.week_key(ms) == (old - timedelta(days=old.weekday())).strftime("%Y-%m-%d")
                checked += 1
        day += timedelta(days=1)
    assert checked > 40_000


# ---------------------------------------------------------------------------
# chop_gate (and regime_live / scanner clocks that share its _ET)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("instant, day", [
    (_utc(2026, 3, 6, 14, 45), "2026-03-06"),    # 09:45 EST
    (_utc(2026, 3, 9, 13, 45), "2026-03-09"),    # 09:45 EDT
    (_utc(2026, 10, 30, 13, 45), "2026-10-30"),  # 09:45 EDT
    (_utc(2026, 11, 2, 14, 45), "2026-11-02"),   # 09:45 EST
])
def test_chop_gate_and_scanner_clock_read_0945_on_every_side(instant, day):
    ms = _ms(instant)
    assert cg._minute_of_day_et(ms) == 9 * 60 + 45
    assert cg.tod_bucket(ms) == "09:30-10:00"
    assert cg.session_date_et(ms) == day
    assert regime_live._minute_of_day_et(ms) == 9 * 60 + 45
    assert mss._now_et_parts(ms) == (day, 9 * 60 + 45)


def _session_bars(day: tuple[int, int, int], *, end: tuple[int, int],
                  wide_until: tuple[int, int] | None = None) -> list[tuple[int, float, float]]:
    """(ts_ms, high, low) 1-min bars on the ET clock, 09:30..end inclusive.
    wide_until: bars before that ET time span 95-105, bars after 99-101;
    None: every bar spans 98-102."""
    y, m, d = day
    t, stop = _et(y, m, d, 9, 30), _et(y, m, d, *end)
    out = []
    while t <= stop:
        if wide_until is None:
            hi, lo = 102.0, 98.0
        else:
            hi, lo = (105.0, 95.0) if (t.hour, t.minute) < wide_until else (101.0, 99.0)
        out.append((_ms(t), hi, lo))
        t += timedelta(minutes=1)
    return out


@pytest.mark.parametrize("prior_days, current_day", [
    # spring: 10 prior sessions in EST, current session in EDT
    ([(2026, 2, d) for d in (23, 24, 25, 26, 27)] + [(2026, 3, d) for d in (2, 3, 4, 5, 6)],
     (2026, 3, 9)),
    # fall: 10 prior sessions in EDT, current session in EST
    ([(2026, 10, d) for d in (19, 20, 21, 22, 23, 26, 27, 28, 29, 30)], (2026, 11, 2)),
])
def test_chop_gate_tod_distribution_lines_up_on_the_et_clock(prior_days, current_day):
    """Prior sessions: r30 = 10 up to 11:29 ET, 2 after. Current session: r30 = 4.
    Matched on the ET clock, a 10:30 fire sees only the wide samples (percentile
    0) and a 12:15 fire only the narrow ones (100). A fixed UTC-4 puts the EST
    side an hour late, so across either transition it mixes the two."""
    by_session: dict[str, list] = {}
    for day in prior_days:
        bars = _session_bars(day, end=(12, 0), wide_until=(11, 0))
        by_session[cg.session_date_et(bars[0][0])] = bars
    cur = _session_bars(current_day, end=(12, 14))
    by_session[cg.session_date_et(cur[0][0])] = cur
    assert len(by_session) == len(prior_days) + 1
    y, m, d = current_day
    assert cg.r30_tod_percentile(by_session, _ms(_et(y, m, d, 10, 30))) == 0.0
    assert cg.r30_tod_percentile(by_session, _ms(_et(y, m, d, 12, 15))) == 100.0


# ---------------------------------------------------------------------------
# calendar pause windows
# ---------------------------------------------------------------------------


@pytest.fixture
def fomc_every_day(monkeypatch):
    """Every date carries a HIGH event at 14:00 ET, pause 30 min before / 60 after."""
    async def _events(from_date=None, to_date=None, **_kw):
        return [CalendarEvent(
            event_date=from_date, event_time_et="14:00", category="FOMC", title="FOMC",
            impact="HIGH", auto_pause_minutes_before=30, auto_pause_minutes_after=60)]
    monkeypatch.setattr(cal.SignalIterRepository, "query_calendar_events",
                        staticmethod(_events))


@pytest.mark.parametrize("instant, paused", [
    # Fri 2026-03-06, EST: event 19:00Z, pause 18:30Z-20:00Z
    (_utc(2026, 3, 6, 17, 45), False),   # 12:45 EST (fixed-offset bug: paused)
    (_utc(2026, 3, 6, 18, 29), False),
    (_utc(2026, 3, 6, 18, 30), True),
    (_utc(2026, 3, 6, 19, 30), True),    # 14:30 EST (fixed-offset bug: open)
    (_utc(2026, 3, 6, 20, 0), True),
    (_utc(2026, 3, 6, 20, 1), False),
    # Mon 2026-03-09, EDT: event 18:00Z, pause 17:30Z-19:00Z
    (_utc(2026, 3, 9, 17, 29), False),
    (_utc(2026, 3, 9, 17, 30), True),
    (_utc(2026, 3, 9, 19, 0), True),
    (_utc(2026, 3, 9, 19, 1), False),
    # Fri 2026-10-30, EDT
    (_utc(2026, 10, 30, 17, 29), False),
    (_utc(2026, 10, 30, 17, 30), True),
    (_utc(2026, 10, 30, 19, 0), True),
    (_utc(2026, 10, 30, 19, 1), False),
    # Mon 2026-11-02, EST
    (_utc(2026, 11, 2, 17, 45), False),  # 12:45 EST (fixed-offset bug: paused)
    (_utc(2026, 11, 2, 18, 29), False),
    (_utc(2026, 11, 2, 18, 30), True),
    (_utc(2026, 11, 2, 19, 30), True),   # 14:30 EST (fixed-offset bug: open)
    (_utc(2026, 11, 2, 20, 0), True),
    (_utc(2026, 11, 2, 20, 1), False),
])
@pytest.mark.asyncio
async def test_calendar_pause_window_is_on_the_et_clock(fomc_every_day, freeze, instant, paused):
    freeze(instant)
    decision = await cal.is_pause_window()                           # default "now"
    assert decision.paused is paused
    assert (await cal.is_pause_window(instant.astimezone(NY))).paused is paused
    if paused:
        off = "-05:00" if instant.astimezone(NY).utcoffset() == timedelta(hours=-5) else "-04:00"
        assert decision.event["pause_from"].endswith(off)
        assert decision.event["pause_to"].endswith(off)


# ---------------------------------------------------------------------------
# Other ET clocks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("instant, expected", [
    (_utc(2026, 3, 6, 13, 30), False),   # 08:30 EST (fixed-offset bug: open)
    (_utc(2026, 3, 6, 14, 30), True),
    (_utc(2026, 3, 6, 20, 30), True),    # 15:30 EST (fixed-offset bug: closed)
    (_utc(2026, 3, 6, 21, 0), True),
    (_utc(2026, 3, 6, 21, 1), False),
    (_utc(2026, 3, 9, 13, 29), False),
    (_utc(2026, 3, 9, 13, 30), True),
    (_utc(2026, 3, 9, 20, 0), True),
    (_utc(2026, 3, 9, 20, 1), False),
    (_utc(2026, 10, 30, 13, 30), True),
    (_utc(2026, 10, 30, 20, 0), True),
    (_utc(2026, 10, 30, 20, 1), False),
    (_utc(2026, 11, 2, 13, 30), False),  # 08:30 EST (fixed-offset bug: open)
    (_utc(2026, 11, 2, 14, 30), True),
    (_utc(2026, 11, 2, 20, 30), True),   # 15:30 EST (fixed-offset bug: closed)
    (_utc(2026, 11, 2, 21, 0), True),
    (_utc(2026, 11, 2, 21, 1), False),
])
def test_option_chain_capture_rth_window_both_sides(instant, expected):
    assert occ._within_rth(instant) is expected
    assert occ._within_rth(instant.replace(tzinfo=None)) is expected   # naive = UTC


@pytest.mark.parametrize("instant", [
    _utc(2026, 3, 6, 14, 30), _utc(2026, 3, 9, 13, 30),
    _utc(2026, 10, 30, 13, 30), _utc(2026, 11, 2, 14, 30),
])
def test_minutes_since_open_is_zero_at_the_et_open(freeze, instant):
    freeze(instant)
    assert regime.now_minutes_since_open_et() == 0


@pytest.mark.parametrize("instant, ymd", [
    (_utc(2026, 3, 6, 20, 40), "20260306"),
    (_utc(2026, 3, 9, 19, 40), "20260309"),
    (_utc(2026, 10, 30, 19, 40), "20261030"),
    (_utc(2026, 11, 2, 20, 40), "20261102"),
])
def test_watcher_eod_flat_clock_reads_1540_et(freeze, instant, ymd):
    freeze(instant)
    assert stw._et_now_hhmm_ymd() == ("15:40", ymd)


@pytest.mark.parametrize("naive_utc", [
    datetime(2026, 3, 6, 15, 0), datetime(2026, 3, 9, 14, 0),
    datetime(2026, 10, 30, 14, 0), datetime(2026, 11, 2, 15, 0),
])
def test_regime_backfill_minutes_since_open(naive_utc):
    row = SimpleNamespace(extras={}, timestamp=naive_utc, spx_price=1.0, drift_cum_net_m=0.0,
                          drift_bias="", direction="WAIT", confidence="NONE")
    assert rb._to_sample(row).minutes_since_open == 30   # 10:00 ET on every side


# ---------------------------------------------------------------------------
# Central Time (was a fixed UTC-5 in daily_review / what_if / counterfactual)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("naive_utc", [
    datetime(2026, 3, 6, 15, 0),    # 09:00 CST (fixed UTC-5 said 10)
    datetime(2026, 3, 9, 14, 0),    # 09:00 CDT
    datetime(2026, 10, 30, 14, 0),  # 09:00 CDT
    datetime(2026, 11, 2, 15, 0),   # 09:00 CST (fixed UTC-5 said 10)
])
def test_central_time_hour_buckets_both_sides(naive_utc):
    assert et_time.to_ct(naive_utc).hour == 9                 # daily_review by_hour_ct
    assert pa._utc_dt_to_ct_hour_label(naive_utc) == "9 AM CT"
    parent = {"qty_filled": 3, "actual": {"avg_buy_price": 1.0},
              "signal": {"be_step_reached_R": 0.5}, "submitted_at": naive_utc.isoformat()}
    assert cf.simulate_one(parent)["hour_ct"] == 9
    assert wib._filter_market_hours_only(SimpleNamespace(timestamp=naive_utc), {}) is True


def test_what_if_market_hours_filter_on_the_ct_clock():
    row = SimpleNamespace
    assert wib._filter_market_hours_only(row(timestamp=datetime(2026, 10, 30, 13, 30)), {}) is True
    # 07:30 CST — the fixed UTC-5 read 08:30 and let it through
    assert wib._filter_market_hours_only(row(timestamp=datetime(2026, 11, 2, 13, 30)), {}) is False


# ---------------------------------------------------------------------------
# Daylight time: unchanged
# ---------------------------------------------------------------------------


def test_daylight_time_behaviour_is_unchanged(monkeypatch):
    """Every 37 minutes across 2026's EDT period, the zoneinfo versions equal
    the old fixed-UTC-4 formulas exactly."""
    fixed = timezone(timedelta(hours=-4))   # the old convention, as the reference
    now = [_utc(2026, 3, 8, 7, 0)]
    monkeypatch.setattr(et_time, "_utc_now", lambda: now[0])
    end = _utc(2026, 11, 1, 6, 0)
    n = 0
    while now[0] < end:
        t = now[0]
        old = t.astimezone(fixed)
        old_minute = old.hour * 60 + old.minute
        assert loop._today_et() == old.strftime("%Y-%m-%d")
        assert loop._in_market_hours_et() == (old.weekday() < 5 and 570 <= old_minute <= 960)
        assert loop._et_date_from_ms(_ms(t)) == old.strftime("%Y-%m-%d")
        assert regime.now_minutes_since_open_et() == (old.hour - 9) * 60 + (old.minute - 30)
        assert mss._now_et_parts(_ms(t)) == (old.strftime("%Y-%m-%d"), old_minute)
        assert cg._minute_of_day_et(_ms(t)) == old_minute
        assert occ._within_rth(t) == ((13, 30) <= (t.hour, t.minute) <= (20, 0))
        now[0] = t + timedelta(minutes=37)
        n += 1
    assert n > 9_000
