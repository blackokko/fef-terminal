"""Event calendar rules — 3rd-Friday OPEX (incl. holiday roll), FOMC date
membership, EOM/quarter-end on the true NYSE calendar, NFP first-Friday rule,
and the upcoming-events window that feeds the EVENTS regime tile."""

from __future__ import annotations

from fef_sidecar.engine import event_calendar as cal


# ---------------------------------------------------------------------------
# Trading-day calendar
# ---------------------------------------------------------------------------


def test_trading_day_weekends_and_holidays() -> None:
    assert cal.is_trading_day("2026-07-06")          # Monday
    assert not cal.is_trading_day("2026-07-04")      # Saturday
    assert not cal.is_trading_day("2026-07-03")      # July 4th observed
    assert not cal.is_trading_day("2026-06-19")      # Juneteenth
    assert cal.prior_trading_day("2026-07-06") == "2026-07-02"  # skips 7/3-7/5
    assert cal.next_trading_day("2026-07-02") == "2026-07-06"


# ---------------------------------------------------------------------------
# OPEX — 3rd Friday, rolled to the prior session when the market is closed
# ---------------------------------------------------------------------------


def test_opex_is_third_friday() -> None:
    assert cal.third_friday(2026, 7).isoformat() == "2026-07-17"
    assert cal.opex_date(2026, 7) == "2026-07-17"
    assert cal.opex_date(2025, 8) == "2025-08-15"


def test_opex_rolls_back_on_holiday_third_friday() -> None:
    # June 2026's 3rd Friday IS Juneteenth (2026-06-19, closed) -> Thursday.
    assert cal.third_friday(2026, 6).isoformat() == "2026-06-19"
    assert cal.opex_date(2026, 6) == "2026-06-18"
    # 2025-04-18 Good Friday was also a 3rd Friday -> rolls to 04-17.
    assert cal.opex_date(2025, 4) == "2025-04-17"


# ---------------------------------------------------------------------------
# FOMC — hardcoded statement days
# ---------------------------------------------------------------------------


def test_fomc_membership() -> None:
    assert "2026-06-17" in cal.FOMC_DATES
    assert "2026-07-29" in cal.FOMC_DATES          # next upcoming (H2-2026)
    assert "2025-12-10" in cal.FOMC_DATES
    assert "2026-06-16" not in cal.FOMC_DATES      # day 1 of the meeting
    assert "2026-06-18" not in cal.FOMC_DATES


def test_is_fomc_day_backs_the_scanner_gate() -> None:
    """is_fomc_day is the exact membership check the FOMC-morning gate
    consumes — the next statement day and the full H2-2026 tail must hit."""
    assert cal.is_fomc_day("2026-07-29") is True   # next FOMC after 2026-07-06
    assert cal.is_fomc_day("2026-09-16") is True
    assert cal.is_fomc_day("2026-10-28") is True
    assert cal.is_fomc_day("2026-12-09") is True   # last hardcoded (through 2026-12)
    assert cal.is_fomc_day("2026-07-28") is False  # day 1 of the meeting
    assert cal.is_fomc_day("2026-07-06") is False  # ordinary session
    assert cal.is_fomc_day("2026-07-07") is False


def test_events_on_fomc_day() -> None:
    evts = cal.events_on("2026-06-17")
    kinds = [e["kind"] for e in evts]
    assert kinds[0] == "FOMC"                      # priority-ordered
    assert all(e["approx"] is False for e in evts if e["kind"] == "FOMC")


# ---------------------------------------------------------------------------
# EOM / quarter-end — last trading day of month/quarter
# ---------------------------------------------------------------------------


def test_eom_and_quarter_end() -> None:
    assert cal.is_eom_session("2026-06-30")
    assert cal.is_quarter_end_session("2026-06-30")
    assert cal.is_eom_session("2026-07-31")
    assert not cal.is_quarter_end_session("2026-07-31")
    assert not cal.is_eom_session("2026-07-30")
    # Month ending on a weekend: May 2026 ends Sun 5/31 -> last session 5/29.
    assert cal.is_eom_session("2026-05-29")
    assert not cal.is_eom_session("2026-05-31")


def test_events_on_quarter_end_reports_quarter_not_eom() -> None:
    kinds = [e["kind"] for e in cal.events_on("2026-06-30")]
    assert "QUARTER_END" in kinds and "EOM" not in kinds
    kinds_jul = [e["kind"] for e in cal.events_on("2026-07-31")]
    assert "EOM" in kinds_jul and "QUARTER_END" not in kinds_jul


# ---------------------------------------------------------------------------
# CPI / NFP — approximate flags
# ---------------------------------------------------------------------------


def test_cpi_is_flagged_approximate() -> None:
    evts = cal.events_on("2026-06-11")
    cpi = [e for e in evts if e["kind"] == "CPI"]
    assert cpi and cpi[0]["approx"] is True


def test_nfp_first_friday_with_exceptions_and_holiday_drop() -> None:
    assert cal.nfp_date(2026, 6) == "2026-06-05"
    assert cal.nfp_date(2026, 1) == "2026-01-09"   # hardcoded exception
    assert cal.nfp_date(2026, 7) is None           # first Friday 7/3 = closed
    assert cal.nfp_date(2025, 11) is None          # shutdown-dropped month


# ---------------------------------------------------------------------------
# Upcoming events window (feeds the "next: X in N sessions" line)
# ---------------------------------------------------------------------------


def test_upcoming_events_within_three_sessions() -> None:
    # From Mon 2026-06-15: FOMC on Wed 06-17 = 2 sessions away, OPEX on
    # Thu 06-18 (rolled off Juneteenth) = 3 sessions away.
    up = cal.upcoming_events("2026-06-15", within_sessions=3)
    fomc = [e for e in up if e["kind"] == "FOMC"]
    opex = [e for e in up if e["kind"] == "OPEX"]
    assert fomc and fomc[0]["date"] == "2026-06-17" and fomc[0]["sessions_away"] == 2
    assert opex and opex[0]["date"] == "2026-06-18" and opex[0]["sessions_away"] == 3


def test_upcoming_events_empty_when_clear() -> None:
    # 2026-07-06 (Mon): next 3 sessions 7/7-7/9 carry no flags.
    assert cal.upcoming_events("2026-07-06", within_sessions=3) == []
