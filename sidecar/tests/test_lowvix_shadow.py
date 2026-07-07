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
from datetime import datetime
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
    assess_lo_vix,
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
    assert "2026-06-01..2026-06-19" in row.summary
    assert row.details["lo"] is True
    assert row.details["vix_pct"] == pytest.approx(0.0)
    assert row.details["n_pool"] == 19
    assert row.details["threshold_pct"] == pytest.approx(33.333)


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
    +1.0R = 7446), trails out at 7446, and finalize writes TWO
    ShadowExitOutcome rows for ONE parent."""
    _patch_lo_vix(monkeypatch, True)
    state = _const_quote_state(6.0)
    p = _spx_parent()

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
    # risk = |7440-7434| = 6; MFE 9/6 = 1.5R -> ratchet locks +1.0R = 7446.
    assert lsh["be_step"] == pytest.approx(1.5)
    assert lsh["dyn_stop"] == pytest.approx(7446.0)

    await _shadow_be_ladder_cycle(state, [p], 7446.0)       # trail stop
    await _settle_tasks()
    assert lsh["done"] and lsh["exit_reason"] == "trail"

    await _shadow_be_ladder_cycle(state, [], 7446.0)        # real close
    await _settle_tasks()
    assert state.shadow_be_ladder == {} and state.shadow_lowvix == {}

    rows = await _outcome_rows()
    assert len(rows) == 2                                   # ONE parent, TWO variants
    assert all(r.parent_order_id == 201 for r in rows)
    assert {r.variant_label for r in rows} == {
        "fixed_dollar_shadow", "lowvix_shrink_shadow",
    }
    lv = next(r for r in rows if r.variant_label == "lowvix_shrink_shadow")
    # R math: T1 slice 1.5R (1/3) + trail residual 1.0R (2/3) = 1.1667.
    assert lv.realized_r == pytest.approx(1.0 / 3 * 1.5 + 2.0 / 3 * 1.0, abs=1e-4)
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
    memoized so the 0.1s loop doesn't re-evaluate (or re-log) every tick."""
    _patch_lo_vix(monkeypatch, True)
    state = _const_quote_state(6.0)
    p = _spx_parent(id=204, spx_target_stop=7350.0)     # anchor 7360 vs entry 7440

    await _shadow_be_ladder_cycle(state, [p], 7440.0)
    await _shadow_be_ladder_cycle(state, [p], 7441.0)
    assert state.shadow_lowvix == {}
    assert 204 in state._lowvix_shadow_declined
    assert 204 in state.shadow_be_ladder                # primary unaffected

    # Missing stored plan declines with its own reason (direct init).
    p2 = _spx_parent(id=205, spx_target_t2=None)
    made = _lowvix_shadow_init(state, p2, 7440.0, spot_source="active_tick")
    assert made == "parent has no stored spx_target_* plan"


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


async def test_lowvix_init_etf_class_default_detection() -> None:
    """A SPY spx_points parent whose plan came from build_plan (scaled
    defaults): anchor inversion + _price_scale generalization must detect
    the defaults and scale them by m — SPX math is bit-identical to the
    backtest (scale 1.0), ETF class gets the same effective moneyness."""
    from fef_sidecar.engine.strikes import _price_scale, build_plan
    spot = 743.2
    plan = build_plan(spot=spot, direction=FEFDirection.LONG_CALLS,
                      top_gex_walls=[], top_dex_walls=[], ntm_max_pain=None)
    state = SimpleNamespace()
    p = _spx_parent(
        id=207, symbol="SPY", strike=744.0, entry_spx_price=spot,
        spx_target_t1=plan.t1, spx_target_t2=plan.t2,
        spx_target_t3=plan.t3, spx_target_stop=plan.stop,
        avg_fill_price=2.0, limit_price=2.0,
    )
    sh = _lowvix_shadow_init(state, p, spot, spot_source="active_tick")
    assert isinstance(sh, dict)
    s = _price_scale(spot)
    assert sh["variant_extra"]["plan_anchor"] == pytest.approx(spot, abs=1e-9)
    assert sh["variant_extra"]["price_scale"] == pytest.approx(s, abs=1e-9)
    assert sh["t1"] == pytest.approx(spot + 15.0 * s * 0.6, abs=1e-6)
    assert sh["t2"] == pytest.approx(spot + 25.0 * s * 0.6, abs=1e-6)
    assert sh["t3"] == pytest.approx(spot + 50.0 * s * 0.6, abs=1e-6)
    assert sh["stop"] == pytest.approx(spot - 10.0 * s * 0.6, abs=1e-6)
    # The scaled T1 is NOT the stored (unscaled) T1 — the shrink is real.
    assert abs(sh["t1"] - plan.t1) > 0.1


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
