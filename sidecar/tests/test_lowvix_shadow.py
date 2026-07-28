"""Lo-VIX day classifier + lowvix_shrink_shadow (2026-07-07, shadow-first).

Registered backtest (scratchpad lowvix_score.py / regime_exit_score.py):
shrinking the spx_points ladder to m=0.6 on lo-VIX days = ADOPT on both
fill conventions. Per the operator's standing deployment rule it ships as
a SHADOW variant only — zero live behavior change.

Covers:
  1. Classifier (engine/lo_vix_day.py): open-basis percentile, prior-close
     same-series fallback, <10-pool unknown, fail-open, session cache with
     exactly ONE audit row per session, transient-error retry (not cached).
  2. Scanner stamping: extras.lo_vix_day (true/false/null) on FIRED and
     gate-SKIPPED signals.
  3. Plan transformation (_lowvix_scaled_plan): exact equality with the
     backtest's scaled_plan (reference ported verbatim), m=1.0 identity,
     wall keep/fall-back band arithmetic, plan-anchor recovery from the
     stored stop (SPX + ETF class, calls + puts).
  4. Shadow integration: lo-VIX day => spx_points parent carries TWO
     shadows (fixed_dollar_shadow + lowvix_shrink_shadow) and finalize
     writes TWO ShadowExitOutcome rows for one parent (the
     ux_shadow_exit_parent_variant key supports it); hand-walked m=0.6
     ladder with exact level/R/$ math; normal/unknown days => no extra
     row; fixed_dollar parents => no lo-VIX row; unusable plans decline
     once (memoized). ZERO order placement anywhere.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date, datetime
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from sqlmodel import select

from fef_sidecar.config_store import UIConfig
from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import FEFDirection, LiveOrder, ShadowExitOutcome
from fef_sidecar.engine import chop_gate
from fef_sidecar.engine import lo_vix_day as lvd
from fef_sidecar.engine import multi_symbol_scanner as mss
from fef_sidecar.engine.chop_gate import _ET, ChopAssessment
from fef_sidecar.engine.lo_vix_day import (
    LoVixAssessment,
    assess_lo_vix,
    invalidate_cache_on_fresher_vix,
    is_lo_vix_day,
    vix_open_percentile,
)
from fef_sidecar.engine.multi_symbol_scanner import handle_actionable_scanner_verdict
from fef_sidecar.engine.spx_target_watcher import (
    _LOWVIX_SHADOW_VARIANT_LABEL,
    _lowvix_scaled_plan,
    _lowvix_shadow_init,
    _plan_anchor_from_stop,
    _shadow_be_ladder_cycle,
)
from fef_sidecar.engine.vix_daily_ingest import (
    completed_daily_rows,
    run_vix_daily_ingest_once,
    upsert_vix_daily,
    vix_daily_bar_ts_ms,
)
from fef_sidecar.state import AppState


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.fixture(autouse=True)
def _deterministic_config(monkeypatch: pytest.MonkeyPatch):
    """Never read the live ~/.fef-terminal/config.json from tests."""
    monkeypatch.setattr(mss, "load_config", lambda: UIConfig())


@pytest.fixture(autouse=True)
def _pin_scanner_clock(monkeypatch: pytest.MonkeyPatch):
    """Clear morning (2026-07-07 11:00 ET): not FOMC, outside the TOD
    window — scanner tests exercise stamping, not those gates."""
    monkeypatch.setattr(mss, "_now_et_parts", lambda now_ms=None: ("2026-07-07", 11 * 60))


@pytest.fixture(autouse=True)
def _restore_mock_coherent_env():
    """AppState.build() setdefaults FEF_MOCK_COHERENT=true process-wide —
    leave the env exactly as found (see test_chop_gate.py rationale)."""
    before = os.environ.get("FEF_MOCK_COHERENT")
    yield
    if before is None:
        os.environ.pop("FEF_MOCK_COHERENT", None)
    elif os.environ.get("FEF_MOCK_COHERENT") != before:
        os.environ["FEF_MOCK_COHERENT"] = before


def _et_ms(date_str: str, hh: int, mm: int) -> int:
    y, mo, d = (int(x) for x in date_str.split("-"))
    return int(datetime(y, mo, d, hh, mm, tzinfo=_ET).timestamp() * 1000)


def _daily(rows: list[tuple[str, float, float]]) -> list[tuple[int, float, float]]:
    """(ET date, open, close) -> (ts, open, close) stamped at the 16:00 ET
    close, matching the live VIX 1-day backfill convention."""
    return [(_et_ms(d, 16, 0), o, c) for d, o, c in rows]


# ---------------------------------------------------------------------------
# 1. Classifier — pure percentile math (mirror of the backtest's vix_pct_of)
# ---------------------------------------------------------------------------


def _prior_20(opens_closes) -> list[tuple[str, float, float]]:
    """2026-06-01..2026-06-20 with (open, close) from the callable."""
    return [(f"2026-06-{i:02d}", *opens_closes(i)) for i in range(1, 21)]


def test_open_basis_lo_true() -> None:
    rows = _daily(_prior_20(lambda i: (20.0 + i, 20.0 + i))    # opens 21..40
                  + [("2026-06-24", 15.0, 15.5)])              # today's open lowest
    a = vix_open_percentile(rows, "2026-06-24")
    assert a.lo is True
    assert a.basis == "open"
    assert a.vix_pct == pytest.approx(0.0)
    assert a.value == pytest.approx(15.0)
    assert a.n_pool == 20
    assert (a.pool_from, a.pool_to) == ("2026-06-01", "2026-06-20")
    assert a.data_gap is None


def test_open_basis_not_lo() -> None:
    rows = _daily(_prior_20(lambda i: (20.0 + i, 20.0 + i))
                  + [("2026-06-24", 50.0, 49.0)])
    a = vix_open_percentile(rows, "2026-06-24")
    assert a.lo is False
    assert a.vix_pct == pytest.approx(100.0)


def test_open_basis_tercile_boundary() -> None:
    """Midrank percentile against opens 21..40: 26.5 has 6 below -> 30.0
    (< 33.33, lo); 27.5 has 7 below -> 35.0 (not lo)."""
    base = _prior_20(lambda i: (20.0 + i, 20.0 + i))
    lo = vix_open_percentile(_daily(base + [("2026-06-24", 26.5, 0.0)]), "2026-06-24")
    hi = vix_open_percentile(_daily(base + [("2026-06-24", 27.5, 0.0)]), "2026-06-24")
    assert lo.vix_pct == pytest.approx(30.0) and lo.lo is True
    assert hi.vix_pct == pytest.approx(35.0) and hi.lo is False


def test_prior_close_fallback_when_today_bar_missing() -> None:
    """No bar for the session (the live intraday case — 1-day rows land at
    the close): the PRIOR session's CLOSE is ranked against the 20 closes
    before it. Descending closes 44..25 -> 06-20's 25 is below all -> lo."""
    rows = _daily(_prior_20(lambda i: (45.0 - i, 45.0 - i)))
    a = vix_open_percentile(rows, "2026-06-24")
    assert a.basis == "prior_close"
    assert a.value == pytest.approx(25.0)          # 06-20 close
    assert a.n_pool == 19                          # 06-01..06-19
    assert (a.pool_from, a.pool_to) == ("2026-06-01", "2026-06-19")
    assert a.vix_pct == pytest.approx(0.0)
    assert a.lo is True


def test_insufficient_pool_is_unknown() -> None:
    rows = _daily([(f"2026-06-{i:02d}", 20.0 + i, 20.0 + i) for i in range(1, 6)]
                  + [("2026-06-24", 15.0, 15.0)])
    a = vix_open_percentile(rows, "2026-06-24")
    assert a.lo is None
    assert "only 5 prior VIX open values" in (a.data_gap or "")


def test_no_rows_is_unknown_and_future_rows_ignored() -> None:
    assert vix_open_percentile([], "2026-06-24").lo is None
    # A row dated after the session must not affect (or enable) the compute.
    rows = _daily([("2026-06-25", 1.0, 1.0)])
    a = vix_open_percentile(rows, "2026-06-24")
    assert a.lo is None and "no VIX 1-day bars" in (a.data_gap or "")


def test_prior_close_stale_is_unknown_fail_open() -> None:
    """Review MAJOR-3a: a lapsed backfill (series ends 06-20, session 06-26
    = 6 calendar days) must classify UNKNOWN with reason 'stale_vix', not
    rank a week-old close confidently. 4 days (Fri close -> Tue) is fresh;
    the cutoff is tunable (assess reads FEF_VIX_DAILY_MAX_STALE_DAYS)."""
    rows = _daily(_prior_20(lambda i: (45.0 - i, 45.0 - i)))     # ends 06-20
    a = vix_open_percentile(rows, "2026-06-26")
    assert a.lo is None
    assert (a.data_gap or "").startswith("stale_vix")
    assert a.value_date == "2026-06-20"
    assert a.basis == "prior_close"
    assert vix_open_percentile(rows, "2026-06-24").lo is True    # 4 days: fresh
    assert vix_open_percentile(rows, "2026-06-26", max_stale_days=10).lo is True


# ---------------------------------------------------------------------------
# 1b. Classifier — assess end-to-end: DB read, session cache, single audit
# ---------------------------------------------------------------------------


async def _seed_vix_daily(rows: list[tuple[str, float, float]]) -> None:
    from fef_sidecar.db.models import HistoricalBar
    async with get_session() as session:
        for d, op, cl in rows:
            session.add(HistoricalBar(
                symbol="VIX", bar_size="1 day", timestamp_ms=_et_ms(d, 16, 0),
                open=op, high=max(op, cl) + 0.5, low=min(op, cl) - 0.5,
                close=cl, volume=0.0,
            ))


async def test_assess_lo_day_cached_with_single_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_vix_daily(_prior_20(lambda i: (45.0 - i, 45.0 - i)))
    state = AppState.build()
    at = _et_ms("2026-06-24", 10, 0)

    a = await assess_lo_vix(state, at_ms=at)
    assert a.lo is True and a.basis == "prior_close"
    assert a.vix_pct == pytest.approx(0.0) and a.n_pool == 19
    assert "2026-06-24" in state.lo_vix_day_cache
    assert await is_lo_vix_day(state, at_ms=at) is True

    # Second call must hit the session cache — a re-query would explode.
    async def _boom(*args, **kwargs):
        raise AssertionError("VIX daily bars re-queried within the same session")
    monkeypatch.setattr(SignalIterRepository, "query_bars", _boom)
    b = await assess_lo_vix(state, at_ms=at + 60_000)
    assert b.lo is True

    # Exactly ONE audit row per session, carrying the determination,
    # percentile, basis, and the source rows' date range.
    audits = await SignalIterRepository.query_audit_log(category="lo_vix_day")
    assert len(audits) == 1
    row = audits[0]
    assert "LO-VIX DAY YES for 2026-06-24" in row.summary
    assert "prior_close" in row.summary
    assert "(2026-06-20)" in row.summary          # the ranked close's own date
    assert "2026-06-01..2026-06-19" in row.summary
    assert row.details["lo"] is True
    assert row.details["vix_pct"] == pytest.approx(0.0)
    assert row.details["value_date"] == "2026-06-20"
    assert row.details["n_pool"] == 19
    assert row.details["threshold_pct"] == pytest.approx(33.333)


async def test_assess_stale_series_unknown_and_audited() -> None:
    """Review MAJOR-3a end-to-end: series ends 06-20, session 06-26 ->
    is_lo_vix_day None (fail-open, no shadow row) with the stale reason and
    the ranked close's own date audited."""
    await _seed_vix_daily(_prior_20(lambda i: (45.0 - i, 45.0 - i)))
    state = AppState.build()
    at = _et_ms("2026-06-26", 10, 0)
    assert await is_lo_vix_day(state, at_ms=at) is None
    audits = await SignalIterRepository.query_audit_log(category="lo_vix_day")
    assert len(audits) == 1
    assert "LO-VIX DAY UNKNOWN" in audits[0].summary
    assert "stale_vix" in audits[0].summary
    assert audits[0].details["lo"] is None
    assert audits[0].details["value_date"] == "2026-06-20"


async def test_assess_empty_db_unknown_fail_open() -> None:
    state = AppState.build()
    at = _et_ms("2026-06-24", 10, 0)
    assert await is_lo_vix_day(state, at_ms=at) is None
    audits = await SignalIterRepository.query_audit_log(category="lo_vix_day")
    assert len(audits) == 1
    assert "LO-VIX DAY UNKNOWN" in audits[0].summary
    assert "treated as NOT lo" in audits[0].summary
    assert audits[0].details["lo"] is None
    # The unknown is cached too — one compute + one audit per session.
    assert await is_lo_vix_day(state, at_ms=at + 60_000) is None
    assert len(await SignalIterRepository.query_audit_log(category="lo_vix_day")) == 1


async def test_assess_transient_error_not_cached_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FAILED read returns unknown WITHOUT caching or auditing; the next
    call retries and caches the definitive answer."""
    await _seed_vix_daily(_prior_20(lambda i: (45.0 - i, 45.0 - i)))
    state = AppState.build()
    at = _et_ms("2026-06-24", 10, 0)

    real = SignalIterRepository.query_bars
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB error")
        return await real(*args, **kwargs)

    monkeypatch.setattr(SignalIterRepository, "query_bars", flaky)
    a = await assess_lo_vix(state, at_ms=at)
    assert a.lo is None and "assessment error" in (a.data_gap or "")
    assert state.lo_vix_day_cache == {}
    assert await SignalIterRepository.query_audit_log(category="lo_vix_day") == []

    b = await assess_lo_vix(state, at_ms=at)
    assert b.lo is True
    assert "2026-06-24" in state.lo_vix_day_cache
    assert len(await SignalIterRepository.query_audit_log(category="lo_vix_day")) == 1


# ---------------------------------------------------------------------------
# 2. Scanner stamping — extras.lo_vix_day on fired AND gate-skipped signals
# ---------------------------------------------------------------------------


def _patch_lo_vix(monkeypatch: pytest.MonkeyPatch, value: bool | None) -> None:
    async def fake(state, *, at_ms=None):
        return value
    monkeypatch.setattr(lvd, "is_lo_vix_day", fake)


def _patch_assess(monkeypatch: pytest.MonkeyPatch, percentile: float) -> None:
    a = ChopAssessment(
        bar_symbol="SPX", session_date="2026-07-07",
        at_ms=_et_ms("2026-07-07", 11, 0), tod_bucket="11:00-11:30",
        r30=5.0, percentile=percentile, n_samples=200, n_sessions=20,
        data_gap=None,
    )

    async def fake(state, *, bar_symbol="SPX", at_ms=None):
        return a
    monkeypatch.setattr(chop_gate, "assess_chop", fake)


def _scanner_verdict(direction: str = "LONG_CALLS") -> dict:
    return {
        "direction": direction,
        "confidence": "LOW",
        "reasoning": f"ALIGNED {direction} — ΔPrice +2.10pts, ΔDrift +30.00M.",
        "strike_rec": 7450.0,
        "t1": 7455.0, "t2": 7465.0, "t3": 7490.0, "stop": 7430.0,
        "aligned": True,
        "wall_collapse_trigger": False,
        "option_pricing": {
            "contract_strike": 7450.0, "right": "C", "entry_limit": 5.1,
            "delta": 0.4, "underlying_price_at_quote": 7440.0,
            "t1": 11.1, "t2": 15.1, "t3": 25.1, "stop": 1.1,
        },
        "suggested_qty": 3,
    }


async def _last_signal():
    rows = await SignalIterRepository.recent(limit=5)
    assert rows, "expected a persisted signal row"
    return rows[0]


@pytest.mark.parametrize("flag", [True, False])
async def test_scanner_stamps_lo_vix_day_on_fired_signal(
    monkeypatch: pytest.MonkeyPatch, flag: bool,
) -> None:
    state = AppState.build()
    state.active_symbol = "SPX"
    _patch_assess(monkeypatch, percentile=66.0)   # expanded — no gate skips
    _patch_lo_vix(monkeypatch, flag)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7440.0, verdict_dict=_scanner_verdict(),
    )
    row = await _last_signal()
    assert row.extras.get("skipped_by") is None
    assert row.extras["lo_vix_day"] is flag


async def test_scanner_stamps_lo_vix_day_on_gate_skipped_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState.build()
    state.active_symbol = "SPX"
    _patch_assess(monkeypatch, percentile=10.0)   # compressed — chop skip
    _patch_lo_vix(monkeypatch, True)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7440.0, verdict_dict=_scanner_verdict("LONG_PUTS"),
    )
    row = await _last_signal()
    assert row.extras["skipped_by"] == "chop_gate"
    assert row.extras["lo_vix_day"] is True


async def test_scanner_stamps_null_when_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AppState.build()
    state.active_symbol = "SPX"
    _patch_assess(monkeypatch, percentile=66.0)
    _patch_lo_vix(monkeypatch, None)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7440.0, verdict_dict=_scanner_verdict(),
    )
    row = await _last_signal()
    assert "lo_vix_day" in row.extras
    assert row.extras["lo_vix_day"] is None


# ---------------------------------------------------------------------------
# 3. Plan transformation — exact mirror of the backtest's scaled_plan
# ---------------------------------------------------------------------------


def _reference_scaled_plan(spx_sig, t1, t2, t3, stop, is_lc, m):
    """regime_exit_score.scaled_plan, ported VERBATIM (SPX, scale 1.0) —
    the registered machinery the live shadow must reproduce bit-for-bit."""
    EPS = 1e-6
    sgn = 1.0 if is_lc else -1.0
    off1 = sgn * (t1 - spx_sig)
    off2 = sgn * (t2 - spx_sig)
    off3 = sgn * (t3 - spx_sig)
    R = 10.0 * m
    ns = spx_sig - sgn * R
    if abs(off1 - 15.0) < EPS:
        n1 = spx_sig + sgn * 15.0 * m
    elif 0.5 * R - EPS <= off1 <= 2.0 * R + EPS:
        n1 = t1
    else:
        n1 = spx_sig + sgn * 15.0 * m
    if abs(off2 - 25.0) < EPS:
        n2 = spx_sig + sgn * 25.0 * m
    elif (2.0 * R - EPS <= off2 <= 3.0 * R + EPS) and abs(t2 - n1) > 3.0:
        n2 = t2
    else:
        n2 = spx_sig + sgn * 25.0 * m
    if abs(off3 - 50.0) < EPS:
        n3 = spx_sig + sgn * 50.0 * m
    elif sgn * (t3 - n2) > 0:
        n3 = t3
    else:
        n3 = spx_sig + sgn * 50.0 * m
    return n1, n2, n3, ns


def test_lowvix_scaled_plan_equals_backtest_reference() -> None:
    """Grid over stored-plan shapes x m x direction: the live transformation
    must equal the ported backtest function EXACTLY (SPX scale 1.0)."""
    sig = 7440.0
    for is_lc in (True, False):
        sgn = 1.0 if is_lc else -1.0
        stop = sig - sgn * 10.0
        d = lambda o: sig + sgn * o  # noqa: E731
        plans = [
            (d(15), d(25), d(50), stop),        # all-default
            (7447.0 if is_lc else 7433.0, d(25), d(50), stop),   # T1 wall off 7
            (7461.0 if is_lc else 7419.0, d(25), d(50), stop),   # T1 wall off 21
            (d(15), 7454.0 if is_lc else 7426.0, d(50), stop),   # T2 wall off 14
            (d(15), d(25), 7500.0 if is_lc else 7380.0, stop),   # T3 anchor off 60
            (d(15), d(25), 7462.0 if is_lc else 7418.0, stop),   # T3 anchor off 22
            (7447.0 if is_lc else 7433.0,                        # all walls
             7454.0 if is_lc else 7426.0,
             7500.0 if is_lc else 7380.0, stop),
        ]
        for m in (0.6, 0.7, 1.0, 1.3):
            for t1, t2, t3, stp in plans:
                got = _lowvix_scaled_plan(sig, t1, t2, t3, stp, is_lc, m, 1.0)
                want = _reference_scaled_plan(sig, t1, t2, t3, stp, is_lc, m)
                assert got == pytest.approx(want, abs=1e-12), (
                    f"is_lc={is_lc} m={m} plan={(t1, t2, t3, stp)}"
                )


def test_lowvix_scaled_plan_m1_reproduces_stored_plan() -> None:
    """The backtest's per-fire machinery guard: m=1.0 == the stored plan."""
    sig = 7440.0
    for t1, t2, t3 in [(7455.0, 7465.0, 7490.0),      # defaults
                       (7447.0, 7465.0, 7500.0)]:     # T1 wall + MP anchor
        got = _lowvix_scaled_plan(sig, t1, t2, t3, 7430.0, True, 1.0, 1.0)
        assert got == pytest.approx((t1, t2, t3, 7430.0), abs=1e-9)


def test_lowvix_scaled_plan_wall_bands_hand_math() -> None:
    """m=0.6 (R'=6): T1 wall kept iff offset in [3, 12]; T2 wall kept iff in
    [12, 18] AND >3pts from the new T1; T3 kept iff beyond the new T2."""
    sig = 7440.0
    # T1 wall off 7 in [3,12] -> KEPT; T2 off 14 in [12,18], 7pts from n1 -> KEPT.
    n1, n2, n3, ns = _lowvix_scaled_plan(sig, 7447.0, 7454.0, 7500.0, 7430.0, True, 0.6, 1.0)
    assert (n1, n2, n3, ns) == pytest.approx((7447.0, 7454.0, 7500.0, 7434.0))
    # T1 wall off 22 > 12 -> scaled default 7449; T2 wall off 12 but only
    # 3pts from n1 (not >3) -> scaled default 7455; T3 7462 > n2 -> KEPT.
    n1, n2, n3, ns = _lowvix_scaled_plan(sig, 7462.0, 7452.0, 7462.0, 7430.0, True, 0.6, 1.0)
    assert (n1, n2, n3, ns) == pytest.approx((7449.0, 7455.0, 7462.0, 7434.0))
    # T3 anchor NOT beyond the new T2 (7454 < scaled-default n2 7455)
    # -> scaled default 7470.
    n1, n2, n3, ns = _lowvix_scaled_plan(sig, 7455.0, 7465.0, 7454.0, 7430.0, True, 0.6, 1.0)
    assert n2 == pytest.approx(7455.0)
    assert n3 == pytest.approx(7470.0)
    # Puts mirror: defaults scale to 7431/7425/7410/7446.
    got = _lowvix_scaled_plan(sig, 7425.0, 7415.0, 7390.0, 7450.0, False, 0.6, 1.0)
    assert got == pytest.approx((7431.0, 7425.0, 7410.0, 7446.0))


def test_plan_anchor_from_stop_inversion() -> None:
    from fef_sidecar.engine.strikes import _price_scale, build_plan
    # SPX class: exact +/-10.
    assert _plan_anchor_from_stop(7430.0, True) == pytest.approx(7440.0)
    assert _plan_anchor_from_stop(7450.0, False) == pytest.approx(7440.0)
    # ETF class: round-trips build_plan's own stop to the plan spot.
    for spot, is_long in ((743.2, True), (620.0, False)):
        direction = FEFDirection.LONG_CALLS if is_long else FEFDirection.LONG_PUTS
        plan = build_plan(spot=spot, direction=direction,
                          top_gex_walls=[], top_dex_walls=[], ntm_max_pain=None)
        got = _plan_anchor_from_stop(plan.stop, is_long)
        assert got == pytest.approx(spot, abs=1e-9)
        assert _price_scale(got) == pytest.approx(_price_scale(spot))
    # Garbage stops recover nothing.
    assert _plan_anchor_from_stop(0.0, True) is None
    assert _plan_anchor_from_stop(-5.0, False) is None


# ---------------------------------------------------------------------------
# 4. Shadow integration — two rows on lo days, none otherwise, zero orders
# ---------------------------------------------------------------------------


def _const_quote_state(bid: float) -> SimpleNamespace:
    """Fake state whose option quotes always return the same bid/mid — any
    capture-task ordering yields the same premiums (deterministic $ math)."""

    async def get_option_quote(contract):
        return SimpleNamespace(bid=bid, ask=bid + 0.2, mid=bid + 0.1)

    return SimpleNamespace(
        ibkr=SimpleNamespace(get_option_quote=get_option_quote),
        shadow_be_ladder={},
        shadow_lowvix={},
        _watcher_banked_cum={},
        runner_protect_enabled=True,
    )


def _spx_parent(**kw) -> SimpleNamespace:
    """spx_points parent with build_plan's default geometry at spot 7440
    (t1/t2/t3/stop = +15/+25/+50/-10) and the live 1/1/1 tier split."""
    d = dict(
        id=201, symbol="SPX", strike=7450.0, right="C", expiry="20260707",
        qty=3, exit_strategy="spx_points", status="FILLED",
        entry_spx_price=7440.0,
        spx_target_t1=7455.0, spx_target_t2=7465.0, spx_target_t3=7490.0,
        spx_target_stop=7430.0,
        spx_target_t1_qty=1, spx_target_t2_qty=1, spx_target_t3_qty=1,
        avg_fill_price=5.0, limit_price=5.0,
    )
    d.update(kw)
    return SimpleNamespace(**d)


def _vet(state, *pids: int) -> None:
    """Mark parents as plan-sanity-vetted — the MAJOR-1 ordering gate: the
    lo-VIX shadow refuses to init before _maybe_reanchor_plan has seen the
    parent (production populates state._watcher_plan_sanity in the same
    watcher iteration, right after the shadow cycle)."""
    memo = getattr(state, "_watcher_plan_sanity", None)
    if not isinstance(memo, dict):
        memo = {}
        state._watcher_plan_sanity = memo
    for pid in pids:
        memo.setdefault(pid, None)


def _declined_ids(state) -> dict:
    memo = getattr(state, "_lowvix_shadow_declined", None)
    return memo["ids"] if isinstance(memo, dict) else {}


async def _settle_tasks() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


async def _outcome_rows() -> list[ShadowExitOutcome]:
    async with get_session() as session:
        return list((await session.exec(select(ShadowExitOutcome))).all())


async def test_lo_day_two_shadows_exact_m06_arithmetic_and_two_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core contract: on a lo-VIX day an spx_points parent carries the
    fixed_dollar_shadow AND the lowvix_shrink_shadow; the lo-VIX levels are
    the hand-built m=0.6 plan (7449/7455/7470/7434 from 7455/7465/7490/7430
    anchored at 7440); the walk banks T1 at 7449 (BE-ladder ratchets to
    +0.5R = 7443 on the 2026-07-28 schedule), trails out at 7443, and
    finalize writes TWO ShadowExitOutcome rows for ONE parent."""
    _patch_lo_vix(monkeypatch, True)
    state = _const_quote_state(6.0)
    p = _spx_parent()
    _vet(state, 201)

    await _shadow_be_ladder_cycle(state, [p], 7440.0)       # init both
    assert state.shadow_be_ladder[201]["variant"] == "fixed_dollar_shadow"
    lsh = state.shadow_lowvix[201]
    assert lsh["variant"] == _LOWVIX_SHADOW_VARIANT_LABEL == "lowvix_shrink_shadow"
    # EXACT m=0.6 arithmetic vs the hand-built plan.
    assert lsh["t1"] == pytest.approx(7449.0, abs=1e-9)     # 7440 + 15*0.6
    assert lsh["t2"] == pytest.approx(7455.0, abs=1e-9)     # 7440 + 25*0.6
    assert lsh["t3"] == pytest.approx(7470.0, abs=1e-9)     # 7440 + 50*0.6
    assert lsh["stop"] == pytest.approx(7434.0, abs=1e-9)   # 7440 - 10*0.6
    assert lsh["tier_qtys"] == [1, 1, 1]                    # parent's real split
    assert lsh["entry_spx"] == pytest.approx(7440.0)
    ve = lsh["variant_extra"]
    assert ve["m_scale"] == pytest.approx(0.6)
    assert ve["plan_anchor"] == pytest.approx(7440.0)
    assert ve["original_levels"] == {
        "t1": 7455.0, "t2": 7465.0, "t3": 7490.0, "stop": 7430.0,
    }

    await _shadow_be_ladder_cycle(state, [p], 7449.0)       # scaled T1 touch
    await _settle_tasks()
    assert lsh["tier_fired"] == [True, False, False]
    # risk = |7440-7434| = 6; MFE 9/6 = 1.5R -> 1.2R rung locks +0.5R = 7443.
    assert lsh["be_step"] == pytest.approx(1.2)
    assert lsh["dyn_stop"] == pytest.approx(7443.0)

    await _shadow_be_ladder_cycle(state, [p], 7443.0)       # trail stop
    await _settle_tasks()
    assert lsh["done"] and lsh["exit_reason"] == "trail"

    await _shadow_be_ladder_cycle(state, [], 7443.0)        # real close
    await _settle_tasks()
    assert state.shadow_be_ladder == {} and state.shadow_lowvix == {}

    rows = await _outcome_rows()
    assert len(rows) == 2                                   # ONE parent, TWO variants
    assert all(r.parent_order_id == 201 for r in rows)
    assert {r.variant_label for r in rows} == {
        "fixed_dollar_shadow", "lowvix_shrink_shadow",
    }
    lv = next(r for r in rows if r.variant_label == "lowvix_shrink_shadow")
    # R math: T1 slice 1.5R (1/3) + trail residual 0.5R (2/3) = 0.8333.
    assert lv.realized_r == pytest.approx(1.0 / 3 * 1.5 + 2.0 / 3 * 0.5, abs=1e-4)
    assert lv.peak_r == pytest.approx(1.5, abs=1e-6)
    assert lv.exit_reason == "trail"
    assert lv.status == "ok"
    # $ math at constant bid 6.0 vs entry 5.0: T1 1ct +$100, STOP 2ct +$200.
    assert lv.variant_params["shadow_dollars"] == pytest.approx(300.0)
    assert lv.variant_params["levels"] == {
        "t1": pytest.approx(7449.0), "t2": pytest.approx(7455.0),
        "t3": pytest.approx(7470.0), "stop": pytest.approx(7434.0),
    }
    assert lv.variant_params["levels_source"] == "lowvix_scaled_plan(parent_spx_targets)"
    assert lv.variant_params["m_scale"] == pytest.approx(0.6)
    assert lv.variant_params["plan_anchor"] == pytest.approx(7440.0)
    assert lv.variant_params["original_levels"]["t1"] == pytest.approx(7455.0)
    fd = next(r for r in rows if r.variant_label == "fixed_dollar_shadow")
    # FD shadow untouched by the new variant: one 6.1-mid sample -> no tier,
    # 3ct MTM at the last sampled mid on close = +$330.
    assert fd.exit_reason == "real_closed_mtm"
    assert fd.variant_params["shadow_dollars"] == pytest.approx(330.0)

    audits = await SignalIterRepository.query_audit_log(category="shadow_exit")
    assert len(audits) == 2
    lv_audit = next(a for a in audits
                    if a.details["variant_label"] == "lowvix_shrink_shadow")
    assert "SHADOW lo-VIX m=0.6 ladder would have made +$300" in lv_audit.summary
    # ZERO real ordering from any shadow path.
    assert await SignalIterRepository.query_audit_log(category="order") == []
    assert await SignalIterRepository.query_audit_log(category="spx_watcher") == []
    async with get_session() as session:
        assert list((await session.exec(select(LiveOrder))).all()) == []


@pytest.mark.parametrize("flag", [False, None])
async def test_normal_or_unknown_day_no_extra_row(
    monkeypatch: pytest.MonkeyPatch, flag: bool | None,
) -> None:
    """NOT-lo (False) and unknown (None — fail-open) days: the spx_points
    parent gets ONLY the fixed_dollar_shadow; no lo-VIX dict entry, and
    finalize writes exactly one row."""
    _patch_lo_vix(monkeypatch, flag)
    state = _const_quote_state(6.0)
    p = _spx_parent(id=202)
    _vet(state, 202)  # isolate the lo-day gate (sanity gate satisfied)

    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    await _shadow_be_ladder_cycle(state, [p], 7449.0)
    await _settle_tasks()
    assert 202 in state.shadow_be_ladder
    assert state.shadow_lowvix == {}

    await _shadow_be_ladder_cycle(state, [], 7449.0)
    await _settle_tasks()
    rows = await _outcome_rows()
    assert [r.variant_label for r in rows] == ["fixed_dollar_shadow"]


async def test_lo_day_fixed_dollar_parent_gets_no_lowvix_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The variant rides spx_points parents only — a fixed_dollar parent on
    a lo day keeps its single be_ladder_shadow."""
    _patch_lo_vix(monkeypatch, True)
    state = _const_quote_state(6.0)
    p = _spx_parent(id=203, exit_strategy="fixed_dollar")

    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    assert state.shadow_be_ladder[203]["variant"] == "be_ladder_shadow"
    assert state.shadow_lowvix == {}


async def test_lo_day_unrecoverable_plan_declines_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hand-set stop (anchor inversion lands >0.5% from entry) means the
    backtest transformation is undefined: no lo-VIX row, and the decline is
    memoized (against the declined levels, session-scoped) so the 0.1s loop
    doesn't re-evaluate or re-log every tick."""
    _patch_lo_vix(monkeypatch, True)
    state = _const_quote_state(6.0)
    p = _spx_parent(id=204, spx_target_stop=7350.0)     # anchor 7360 vs entry 7440
    _vet(state, 204)

    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    await _shadow_be_ladder_cycle(state, [p], 7441.0)
    assert state.shadow_lowvix == {}
    assert 204 in _declined_ids(state)
    assert 204 in state.shadow_be_ladder                # primary unaffected

    # Missing stored plan declines with its own reason (direct init).
    p2 = _spx_parent(id=205, spx_target_t2=None)
    made = _lowvix_shadow_init(state, p2, 7440.0, spot_source="active_tick")
    assert made == "parent has no stored spx_target_* plan"


async def test_lowvix_waits_for_plan_sanity_then_inits_from_repaired_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review MAJOR-1 (re-anchor race): a 751-class corrupt plan — stop
    collapsed to 0.3pt from entry — inverts to an anchor 0.13% from entry
    and would PASS the 0.5% cross-check, so the ordering gate is the only
    defense. The shadow must WAIT (no sanity memo), then init from the
    REPAIRED plan once _maybe_reanchor_plan has run."""
    _patch_lo_vix(monkeypatch, True)
    state = _const_quote_state(6.0)
    p = _spx_parent(id=210, spx_target_stop=7439.7)     # corrupt: R = 0.3pt

    await _shadow_be_ladder_cycle(state, [p], 7440.0)   # sanity not run yet
    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    assert 210 not in state.shadow_lowvix               # waited — no corrupt init
    assert 210 not in _declined_ids(state)

    # The repair fires: sanity rewrites the stop and memoizes the parent
    # (exactly what _maybe_reanchor_plan persists + re-applies).
    p.spx_target_stop = 7430.0
    p.dynamic_stop_level = 7430.0
    state._watcher_plan_sanity = {210: {"spx_target_stop": 7430.0}}

    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    lsh = state.shadow_lowvix[210]
    # Scaled from the REPAIRED plan (anchor 7440), not the corrupt one
    # (which would anchor 7449.7 -> stop 7443.7 / t1 7458.7).
    assert lsh["variant_extra"]["plan_anchor"] == pytest.approx(7440.0)
    assert lsh["stop"] == pytest.approx(7434.0, abs=1e-9)
    assert lsh["t1"] == pytest.approx(7449.0, abs=1e-9)
    assert lsh["variant_extra"]["original_levels"]["stop"] == pytest.approx(7430.0)


async def test_lowvix_declined_parent_readmitted_after_plan_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review MAJOR-1 mirror: a decline is memoized AGAINST the levels it
    declined — a later plan rewrite re-admits the parent."""
    _patch_lo_vix(monkeypatch, True)
    state = _const_quote_state(6.0)
    p = _spx_parent(id=211, spx_target_stop=7350.0)     # unrecoverable anchor
    _vet(state, 211)

    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    assert 211 in _declined_ids(state)
    assert 211 not in state.shadow_lowvix

    p.spx_target_stop = 7430.0                          # plan repaired
    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    assert 211 not in _declined_ids(state)              # re-admitted
    assert state.shadow_lowvix[211]["stop"] == pytest.approx(7434.0, abs=1e-9)


async def test_lowvix_drops_stale_shadow_on_post_init_plan_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review MAJOR-1: a plan rewritten AFTER init supersedes the walked
    levels — the stale shadow is dropped WITHOUT emitting a row and a fresh
    one inits from the current plan."""
    _patch_lo_vix(monkeypatch, True)
    state = _const_quote_state(6.0)
    p = _spx_parent(id=212)
    _vet(state, 212)

    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    first = state.shadow_lowvix[212]
    assert first["t3"] == pytest.approx(7470.0, abs=1e-9)   # scaled default

    p.spx_target_t3 = 7500.0                            # plan mutated (T3 wall)
    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    second = state.shadow_lowvix[212]
    assert second is not first                          # re-initialized
    # T3 wall 7500 beyond the new T2 (7455) -> KEPT by the transformation.
    assert second["t3"] == pytest.approx(7500.0, abs=1e-9)
    assert second["variant_extra"]["original_levels"]["t3"] == pytest.approx(7500.0)
    assert await _outcome_rows() == []                  # stale shadow: NO row


async def test_lowvix_runner_protect_retrace_hand_math(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review MAJOR-2: the lo-VIX walk runs the live T1-retrace runner-
    protect (~13% of the adopted arm's lo-fire exits). T1 banks at 7449
    (t1_at anchor), the NEXT sample retraces to 7448.5 (< t1_at, still
    above the ratcheted 7446 trail) -> RUNNER_PROTECT exits the remainder.
    R = 1/3*1.5 + 2/3*((7448.5-7440)/6) = 1.4444; $ at bid 6.0 = +$300."""
    _patch_lo_vix(monkeypatch, True)
    state = _const_quote_state(6.0)
    p = _spx_parent(id=213)
    _vet(state, 213)

    await _shadow_be_ladder_cycle(state, [p], 7440.0)   # init
    lsh = state.shadow_lowvix[213]
    assert lsh["rp_protect"] is True

    await _shadow_be_ladder_cycle(state, [p], 7449.0)   # T1 fill sample
    await _settle_tasks()
    # The fill sample itself must NOT insta-trigger rp (spot == t1_at).
    assert lsh["tier_fired"] == [True, False, False]
    assert not lsh["done"]
    assert lsh["t1_at_spx"] == pytest.approx(7449.0)

    await _shadow_be_ladder_cycle(state, [p], 7448.5)   # retrace < t1_at
    await _settle_tasks()
    assert lsh["done"] and lsh["exit_reason"] == "runner_protect"
    assert lsh["fills"][-1]["tier"] == "RUNNER_PROTECT"
    assert lsh["fills"][-1]["qty"] == 2

    await _shadow_be_ladder_cycle(state, [], 7448.5)    # close -> finalize
    await _settle_tasks()
    lv = next(r for r in await _outcome_rows()
              if r.variant_label == "lowvix_shrink_shadow")
    assert lv.exit_reason == "runner_protect"
    assert lv.realized_r == pytest.approx(
        1.0 / 3 * 1.5 + 2.0 / 3 * ((7448.5 - 7440.0) / 6.0), abs=1e-4,
    )
    assert lv.variant_params["shadow_dollars"] == pytest.approx(300.0)
    assert lv.variant_params["runner_protect"] is True
    assert "60min" in lv.variant_params["horizon_note"]


async def test_be_ladder_shadow_keeps_no_rp_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression pin: the shipped be_ladder_shadow (fixed_dollar parents)
    does NOT runner-protect — a T1-retrace that stays above the ratcheted
    trail leaves the runners open."""
    _patch_lo_vix(monkeypatch, False)
    state = _const_quote_state(6.0)
    p = _spx_parent(id=214, exit_strategy="fixed_dollar")

    await _shadow_be_ladder_cycle(state, [p], 7440.0)   # init (be_ladder)
    sh = state.shadow_be_ladder[214]
    assert sh["variant"] == "be_ladder_shadow"
    await _shadow_be_ladder_cycle(state, [p], 7456.0)   # T1 @ 7455, MFE 1.6R
    await _settle_tasks()
    assert sh["tier_fired"] == [True, False, False]
    await _shadow_be_ladder_cycle(state, [p], 7455.0)   # retrace below t1_at
    await _settle_tasks()
    # dyn_stop ratcheted to 7450 (+1.0R); 7455 > 7450 and no rp -> still open.
    assert not sh["done"]
    assert all(f["tier"] != "RUNNER_PROTECT" for f in sh["fills"])


async def test_one_raising_parent_does_not_stall_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review minor: the cycle's per-parent guard — one parent whose
    advance raises must not prevent the other parents' shadows from
    initializing/stepping; the failure logs once per session."""

    class _RaisingQty:
        def __bool__(self) -> bool:
            return True

        def __int__(self) -> int:
            raise RuntimeError("boom")

    _patch_lo_vix(monkeypatch, False)
    state = _const_quote_state(6.0)
    bad = _spx_parent(id=220, qty=_RaisingQty())
    good = _spx_parent(id=221)

    await _shadow_be_ladder_cycle(state, [bad, good], 7440.0)
    assert 221 in state.shadow_be_ladder                # good parent inited
    assert 220 not in state.shadow_be_ladder
    assert 220 in getattr(state, "_shadow_cycle_err_logged", {})


async def test_lowvix_init_recovers_anchor_when_entry_drifted() -> None:
    """entry_spx (7442) != plan anchor (7440): the transformation anchors on
    the RECOVERED plan spot — default detection intact — while the walk's
    risk unit uses entry-to-scaled-stop (the backtest walker convention)."""
    state = SimpleNamespace()
    p = _spx_parent(id=206, entry_spx_price=7442.0)
    sh = _lowvix_shadow_init(state, p, 7442.0, spot_source="active_tick")
    assert isinstance(sh, dict)
    assert sh["entry_spx"] == pytest.approx(7442.0)
    assert sh["variant_extra"]["plan_anchor"] == pytest.approx(7440.0)
    assert sh["t1"] == pytest.approx(7449.0, abs=1e-9)   # anchored on 7440
    assert sh["stop"] == pytest.approx(7434.0, abs=1e-9)


async def test_non_spx_parent_excluded_from_lowvix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review minor: the adopted backtest arm was SPX-only — the
    _price_scale generalization is UNVALIDATED, so non-SPX spx_points
    parents get no lo-VIX row (cycle short-circuits; direct init declines
    with the reason)."""
    from fef_sidecar.engine.strikes import build_plan
    spot = 743.2
    plan = build_plan(spot=spot, direction=FEFDirection.LONG_CALLS,
                      top_gex_walls=[], top_dex_walls=[], ntm_max_pain=None)
    _patch_lo_vix(monkeypatch, True)
    state = _const_quote_state(6.0)
    p = _spx_parent(
        id=207, symbol="SPY", strike=744.0, entry_spx_price=spot,
        spx_target_t1=plan.t1, spx_target_t2=plan.t2,
        spx_target_t3=plan.t3, spx_target_stop=plan.stop,
        avg_fill_price=2.0, limit_price=2.0,
    )
    _vet(state, 207)
    await _shadow_be_ladder_cycle(state, [p], spot)
    assert state.shadow_lowvix == {}                    # excluded, no row
    assert 207 in state.shadow_be_ladder                # primary FD shadow fine
    made = _lowvix_shadow_init(state, p, spot, spot_source="active_tick")
    assert made == "non-SPX parent (backtest arm was SPX-only)"


async def test_lowvix_init_puts_direction() -> None:
    state = SimpleNamespace()
    p = _spx_parent(
        id=208, right="P", entry_spx_price=7440.0,
        spx_target_t1=7425.0, spx_target_t2=7415.0, spx_target_t3=7390.0,
        spx_target_stop=7450.0,
    )
    sh = _lowvix_shadow_init(state, p, 7440.0, spot_source="active_tick")
    assert isinstance(sh, dict)
    assert sh["is_long"] is False
    assert sh["t1"] == pytest.approx(7431.0, abs=1e-9)
    assert sh["t2"] == pytest.approx(7425.0, abs=1e-9)
    assert sh["t3"] == pytest.approx(7410.0, abs=1e-9)
    assert sh["stop"] == pytest.approx(7446.0, abs=1e-9)


# ---------------------------------------------------------------------------
# 5. VIX daily ingest (review MAJOR-3c) — completed-session normalization,
#    additive idempotent upsert, live-provider-only gating
# ---------------------------------------------------------------------------


def _ib_daily_bar(date_str: str, o: float, c: float) -> SimpleNamespace:
    """A fetched daily bar as the Bar model coerces it (naive midnight —
    ib_async daily bars carry a plain date)."""
    y, mo, d = (int(x) for x in date_str.split("-"))
    return SimpleNamespace(
        timestamp=datetime(y, mo, d), open=o, high=max(o, c) + 0.5,
        low=min(o, c) - 0.5, close=c, volume=0.0,
    )


def test_completed_daily_rows_excludes_todays_partial_pre_close() -> None:
    """Today's still-forming bar must NOT be ingested pre-close (a frozen
    partial close would poison future prior-close pools); post-close it is.
    The timestamp anchor byte-matches the backfill script's 16:00 ET."""
    bars = [_ib_daily_bar("2026-07-06", 17.0, 16.5),
            _ib_daily_bar("2026-07-07", 16.8, 16.2)]
    pre = completed_daily_rows(bars, _et_ms("2026-07-07", 10, 0))
    assert [r[0] for r in pre] == [vix_daily_bar_ts_ms(date(2026, 7, 6))]
    post = completed_daily_rows(bars, _et_ms("2026-07-07", 16, 30))
    assert [r[0] for r in post] == [
        vix_daily_bar_ts_ms(date(2026, 7, 6)),
        vix_daily_bar_ts_ms(date(2026, 7, 7)),
    ]
    assert vix_daily_bar_ts_ms(date(2026, 7, 6)) == _et_ms("2026-07-06", 16, 0)


async def test_upsert_vix_daily_additive_and_idempotent() -> None:
    """Only missing sessions are inserted; a second run inserts nothing and
    existing rows keep their values (additive-only)."""
    await _seed_vix_daily([("2026-07-01", 17.1, 16.0),
                           ("2026-07-02", 16.2, 15.9)])
    bars = [_ib_daily_bar(d, 16.0, 15.8)
            for d in ("2026-07-01", "2026-07-02", "2026-07-06")]
    rows = completed_daily_rows(bars, _et_ms("2026-07-07", 10, 0))
    assert await upsert_vix_daily(rows) == 1            # only 07-06 missing
    assert await upsert_vix_daily(rows) == 0            # idempotent
    got = await SignalIterRepository.query_bars("VIX", "1 day", limit=50)
    assert len(got) == 3
    by_ts = {b.timestamp_ms: b for b in got}
    # 07-01 keeps its SEEDED open (17.1), not the fetcher's 16.0.
    assert by_ts[_et_ms("2026-07-01", 16, 0)].open == pytest.approx(17.1)
    assert by_ts[vix_daily_bar_ts_ms(date(2026, 7, 6))].open == pytest.approx(16.0)


async def test_run_ingest_once_gates_on_live_provider_and_audits() -> None:
    """SAME-SERIES RULE: a non-live provider is skipped (synthetic mock bars
    must never enter the real VIX series); a live provider's pass inserts
    the missing completed sessions and writes one audit row."""
    state = SimpleNamespace(ibkr=SimpleNamespace())     # not LiveIBKRProvider
    assert await run_vix_daily_ingest_once(state) is None
    assert await SignalIterRepository.query_audit_log(category="vix_daily_ingest") == []

    from fef_sidecar.ibkr.live import LiveIBKRProvider
    prov = LiveIBKRProvider()                           # never connected

    async def fake_fetch(contract, **kw):
        assert contract.symbol == "VIX"
        return [_ib_daily_bar("2026-07-06", 17.0, 16.5)]

    prov.fetch_historical_bars = fake_fetch             # type: ignore[method-assign]
    state2 = SimpleNamespace(ibkr=prov)
    inserted = await run_vix_daily_ingest_once(
        state2, now_ms=_et_ms("2026-07-07", 10, 0),
    )
    assert inserted == 1
    audits = await SignalIterRepository.query_audit_log(category="vix_daily_ingest")
    assert len(audits) == 1
    assert audits[0].details["inserted"] == 1
    # Second pass: nothing missing, no extra audit noise.
    assert await run_vix_daily_ingest_once(
        state2, now_ms=_et_ms("2026-07-07", 10, 0),
    ) == 0
    assert len(await SignalIterRepository.query_audit_log(category="vix_daily_ingest")) == 1


# ---------------------------------------------------------------------------
# 6. Boot-race cache invalidation (2026-07-09) — a fresher VIX ingest drops the
#    stale-basis lo_vix_day session cache so the next classify recomputes.
#    Observed: classifier cached '7/07 close -> LO-VIX YES' at 12:46:54Z; the
#    ingest inserted the 7/08 close 29s later; the correct 7/09 basis is 7/08
#    (-> NOT lo). Fix is cache-invalidation (robust to loop scheduling), not
#    boot-ordering.
# ---------------------------------------------------------------------------


def _flip_series() -> list[tuple[str, float, float]]:
    """VIX daily (date, open, close) tuned so 2026-07-09's prior-close basis
    flips YES->NO when the 2026-07-08 close lands: on the 7/07 basis the ranked
    close 16.13 sits at the 15th pctile of the prior 20 (LO-VIX YES); on the
    7/08 basis the ranked close 16.9 sits at the 40th pctile (NOT lo). The
    oldest pooled session (2026-06-17) is a high value that drops OUT of the
    20-session pool when the basis advances to 7/08."""
    # 19 closes for the sessions 2026-06-18..2026-07-06 (P2..P20): 3 below
    # 16.13, 4 in (16.13, 16.9), 12 above 16.9.
    mid = [14.0, 14.5, 15.0, 16.3, 16.4, 16.5, 16.6,
           17.1, 17.2, 17.3, 17.4, 17.5, 17.6, 17.7, 17.8, 17.9, 18.0, 18.1, 18.2]
    rows = [("2026-06-17", 17.0, 17.0)]          # P1 (oldest) — a high; drops at 7/08 basis
    d = date(2026, 6, 18)
    for c in mid:
        rows.append((d.isoformat(), c, c))
        d = date.fromordinal(d.toordinal() + 1)
    rows.append(("2026-07-07", 16.13, 16.13))    # ranked on the 7/07 basis
    return rows


async def _invalidation_audits() -> list:
    return [r for r in await SignalIterRepository.query_audit_log(category="lo_vix_day")
            if "cache invalidated" in r.summary]


async def test_ingest_invalidates_stale_lo_vix_cache_yes_to_no_flip() -> None:
    """The full boot-race repro with the exact 7/7->7/8 numbers: 16.13 @ 15pct
    YES on the 7/07 basis, then 16.9 @ 40pct NO once the 7/08 close is
    ingested and the stale-basis cache is invalidated."""
    await _seed_vix_daily(_flip_series())
    state = AppState.build()
    at = _et_ms("2026-07-09", 10, 0)

    # Boot classification off the then-current series (ends 7/07): LO-VIX YES.
    a = await assess_lo_vix(state, at_ms=at)
    assert a.lo is True and a.basis == "prior_close"
    assert a.value_date == "2026-07-07"
    assert a.value == pytest.approx(16.13)
    assert a.vix_pct == pytest.approx(15.0)
    assert "2026-07-09" in state.lo_vix_day_cache

    # The vix_daily_ingest boot pass lands the 2026-07-08 close (16.9).
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    prov = LiveIBKRProvider()                    # never connected

    async def fake_fetch(contract, **kw):
        assert contract.symbol == "VIX"
        return [_ib_daily_bar("2026-07-08", 16.8, 16.9)]

    prov.fetch_historical_bars = fake_fetch       # type: ignore[method-assign]
    state.ibkr = prov
    assert await run_vix_daily_ingest_once(state, now_ms=at) == 1

    # The stale-basis cache was invalidated (7/08 > basis 7/07) + ONE note.
    assert state.lo_vix_day_cache == {}
    inval = await _invalidation_audits()
    assert len(inval) == 1
    assert "fresher VIX (2026-07-08)" in inval[0].summary
    assert "was basis 2026-07-07" in inval[0].summary
    assert inval[0].details["was_basis"] == "2026-07-07"
    assert inval[0].details["newest_ingested"] == "2026-07-08"
    assert inval[0].details["was_lo"] is True

    # Next classification recomputes against the now-current series: NO.
    b = await assess_lo_vix(state, at_ms=at)
    assert b.lo is False and b.basis == "prior_close"
    assert b.value_date == "2026-07-08"
    assert b.value == pytest.approx(16.9)
    assert b.vix_pct == pytest.approx(40.0)


async def test_ingest_same_or_older_session_does_not_invalidate() -> None:
    """The guard: a same/older-session ingest (a deep-history gap fill) does
    NOT move the prior-close basis, so it must NOT invalidate the cache."""
    state = AppState.build()
    state.lo_vix_day_cache["2026-07-09"] = LoVixAssessment(
        session_date="2026-07-09", lo=False, vix_pct=40.0, value=16.9,
        value_date="2026-07-08", basis="prior_close", n_pool=20,
    )
    # Older gap-fill (2026-07-03) -> basis unchanged -> no invalidation.
    assert await invalidate_cache_on_fresher_vix(
        state, [_et_ms("2026-07-03", 16, 0)]
    ) is False
    # Same-session re-ingest (2026-07-08) -> not STRICTLY newer -> no-op.
    assert await invalidate_cache_on_fresher_vix(
        state, [_et_ms("2026-07-08", 16, 0)]
    ) is False
    assert "2026-07-09" in state.lo_vix_day_cache
    assert await _invalidation_audits() == []


async def test_invalidate_supersedes_unknown_basis_cache() -> None:
    """A cached UNKNOWN with no usable basis (empty/short series, value_date
    None) is superseded by ANY fresh session landing."""
    state = AppState.build()
    state.lo_vix_day_cache["2026-07-09"] = LoVixAssessment(
        session_date="2026-07-09", lo=None, value_date=None,
        basis="prior_close", data_gap="no prior VIX daily bars",
    )
    assert await invalidate_cache_on_fresher_vix(
        state, [_et_ms("2026-07-08", 16, 0)]
    ) is True
    assert state.lo_vix_day_cache == {}
    inval = await _invalidation_audits()
    assert len(inval) == 1
    assert "was basis n/a" in inval[0].summary


async def test_invalidate_noop_on_empty_cache() -> None:
    """No cached assessment -> nothing to invalidate (the happy path where the
    ingest ran BEFORE any consumer classified — no stale label ever produced)."""
    state = AppState.build()
    assert await invalidate_cache_on_fresher_vix(
        state, [_et_ms("2026-07-08", 16, 0)]
    ) is False
    assert await SignalIterRepository.query_audit_log(category="lo_vix_day") == []


async def test_assess_recomputes_when_fresher_session_lands_mid_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review MAJOR-2 (the SYMMETRIC race cache-invalidation alone can't close):
    a consumer suspends at query_bars reading the PRE-insert series while the
    ingest commits the 7/08 close + sets state.vix_latest_ingested_date, then
    the (empty-cache) invalidation no-ops — so the consumer would cache the
    stale YES. The mid-read marker guard forces a recompute against the now-
    committed series instead -> the 16.13->16.9 flip resolves to NO even in
    this interleaving, and NO is what gets cached."""
    await _seed_vix_daily(_flip_series())               # series ends 7/07
    state = AppState.build()
    at = _et_ms("2026-07-09", 10, 0)

    real_query = SignalIterRepository.query_bars
    calls = {"n": 0}

    async def racing_query(symbol, bar_size, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # Read the pre-insert series (ends 7/07), THEN simulate the ingest
            # committing 7/08 + advancing the monotonic marker DURING this read.
            bars = await real_query(symbol, bar_size, **kw)
            await _seed_vix_daily([("2026-07-08", 16.8, 16.9)])
            state.vix_latest_ingested_date = "2026-07-08"
            return bars
        return await real_query(symbol, bar_size, **kw)

    monkeypatch.setattr(SignalIterRepository, "query_bars", racing_query)
    a = await assess_lo_vix(state, at_ms=at)

    assert calls["n"] == 2                              # exactly one recompute
    assert a.lo is False                               # NOT the stale YES
    assert a.value_date == "2026-07-08"
    assert a.value == pytest.approx(16.9)
    assert a.vix_pct == pytest.approx(40.0)
    # The CORRECT (recomputed) result is what got cached.
    assert state.lo_vix_day_cache["2026-07-09"].lo is False
    # Exactly one session audit — the final NO, not the discarded YES.
    audits = [r for r in await SignalIterRepository.query_audit_log(category="lo_vix_day")
              if "LO-VIX DAY" in r.summary]
    assert len(audits) == 1
    assert audits[0].summary.startswith("LO-VIX DAY NO")
