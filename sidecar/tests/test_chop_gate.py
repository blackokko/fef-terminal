"""CHOP skip-gate — r30 math, TOD-matched percentile, scanner wiring.

Covers the 2026-07-02 harm-reduction filter (22,683-fire sweep):
  - r30 window math (incl. 2-min-cadence 7/1-7/2 rows + sub-minute dups)
  - TOD-matched percentile vs synthetic 20-session fixtures (era-safe,
    time-of-day-relative by construction)
  - fail-open on every missing-data condition
  - scanner wiring: compressed fire => NO maybe_auto_trade call, signal row
    persisted + tagged extras.skipped_by='chop_gate', 'chop_gate' audit row
  - config hot-read: chop_gate_enabled / chop_gate_pct_threshold from
    UIConfig each evaluation (tunable without restart)
  - tag-only annotations (bias_disagree / tod_bucket / r30_pct) on every
    scanner fire, skipped or not
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.config_store import UIConfig
from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.db.models import LiveOrder
from fef_sidecar.engine import chop_gate
from fef_sidecar.engine import multi_symbol_scanner as mss
from fef_sidecar.engine.chop_gate import (
    _ET,
    ChopAssessment,
    assess_chop,
    build_tod_distribution,
    percentile_of,
    r30,
    r30_tod_percentile,
    tod_bucket,
    tod_percentile,
)
from fef_sidecar.engine.multi_symbol_scanner import handle_actionable_scanner_verdict
from fef_sidecar.state import AppState

_MIN = 60_000
BASE = 6000.0


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
    """Pin the FOMC/TOD gate clock to a clear morning (2026-07-07 11:00 ET —
    not an FOMC day, outside the 14:00-15:30 no-trade window) so these tests
    exercise the chop/bias gates deterministically regardless of when the
    suite runs. The FOMC/TOD gates have their own test module."""
    monkeypatch.setattr(mss, "_now_et_parts", lambda now_ms=None: ("2026-07-07", 11 * 60))


@pytest.fixture(autouse=True)
def _restore_mock_coherent_env():
    """AppState.build() setdefaults FEF_MOCK_COHERENT=true process-wide
    (make_quantdata_client). This file sorts alphabetically BEFORE
    test_ibkr_mock.py, whose default-bars stream test hangs forever in
    coherent-track mode — so leave the env exactly as we found it."""
    before = os.environ.get("FEF_MOCK_COHERENT")
    yield
    if before is None:
        os.environ.pop("FEF_MOCK_COHERENT", None)
    elif os.environ.get("FEF_MOCK_COHERENT") != before:
        os.environ["FEF_MOCK_COHERENT"] = before


def _et_ms(date_str: str, hh: int, mm: int) -> int:
    y, mo, d = (int(x) for x in date_str.split("-"))
    return int(datetime(y, mo, d, hh, mm, tzinfo=_ET).timestamp() * 1000)


def _mk_bars(
    date_str: str, start_hh: int, start_mm: int, n: int, rng: float,
    *, step_min: int = 1,
) -> list[tuple[int, float, float]]:
    """n bars from start time, each with high-low == rng (so any valid r30
    over them equals rng exactly)."""
    t0 = _et_ms(date_str, start_hh, start_mm)
    return [(t0 + i * step_min * _MIN, BASE + rng / 2, BASE - rng / 2) for i in range(n)]


# ---------------------------------------------------------------------------
# 1. r30 window math
# ---------------------------------------------------------------------------


def test_r30_is_high_low_of_last_30_one_min_bars() -> None:
    d = "2026-06-24"
    # 10:00..10:29 — bar i has high 100+i, low 100-i => range 58 at 10:30
    bars = [(_et_ms(d, 10, 0) + i * _MIN, 100.0 + i, 100.0 - i) for i in range(30)]
    at = _et_ms(d, 10, 30)
    assert r30(bars, at) == pytest.approx(58.0)
    # A bar STARTING at at_ms (still forming) must not count...
    bars_plus = bars + [(at, 1000.0, 90.0)]
    assert r30(bars_plus, at) == pytest.approx(58.0)
    # ...nor may a bar just before the window (09:59).
    bars_pre = [(_et_ms(d, 9, 59), 999.0, 50.0)] + bars
    assert r30(bars_pre, at) == pytest.approx(58.0)


def test_r30_none_on_insufficient_bars() -> None:
    d = "2026-06-24"
    assert r30([], _et_ms(d, 10, 30)) is None
    few = _mk_bars(d, 10, 25, 5, 2.0)  # 5 bars < MIN_BARS
    assert r30(few, _et_ms(d, 10, 30)) is None


def test_r30_none_on_poor_span_coverage() -> None:
    d = "2026-06-24"
    # 10 bars all clustered in the final 10 minutes: count passes, span fails
    clustered = _mk_bars(d, 10, 20, 10, 2.0)
    assert r30(clustered, _et_ms(d, 10, 30)) is None


def test_r30_handles_two_min_cadence() -> None:
    """7/1-7/2 capture wrote '1 min'-labeled rows every 120s. 15 bars still
    span the window, and max(high)-min(low) is cadence-agnostic."""
    d = "2026-07-01"
    bars = _mk_bars(d, 10, 0, 15, 3.5, step_min=2)  # 10:00..10:28
    assert r30(bars, _et_ms(d, 10, 30)) == pytest.approx(3.5)


def test_r30_merges_sub_minute_duplicates() -> None:
    """6/30 backfill overlap wrote rows seconds apart. Dups merge into one
    point per minute (widest range) and must not inflate the bar count."""
    d = "2026-06-30"
    t0 = _et_ms(d, 10, 24)
    raw: list[tuple[int, float, float]] = []
    for i in range(6):  # 6 distinct minutes, 3 sub-minute rows each
        for off_s, pad in ((0, 0.0), (2, 0.25), (5, 0.1)):
            raw.append((t0 + i * _MIN + off_s * 1000, BASE + 1 + pad, BASE - 1 - pad))
    # 18 raw rows but only 6 minutes => still below MIN_BARS => None
    assert r30(raw, _et_ms(d, 10, 30)) is None
    # With enough distinct minutes the merged range keeps the widest extremes
    more = raw + _mk_bars(d, 10, 14, 10, 2.0)
    assert r30(more, _et_ms(d, 10, 30)) == pytest.approx(2.5)  # widest dup pad 0.25


def test_percentile_midrank_ties() -> None:
    assert percentile_of([4.0] * 20, 4.0) == pytest.approx(50.0)  # ties never read as compressed
    assert percentile_of([1.0, 2.0, 3.0, 4.0], 2.5) == pytest.approx(50.0)


def test_tod_bucket_labels() -> None:
    assert tod_bucket(_et_ms("2026-06-24", 13, 47)) == "13:30-14:00"
    assert tod_bucket(_et_ms("2026-06-24", 9, 31)) == "09:30-10:00"


# ---------------------------------------------------------------------------
# 2. TOD-matched percentile vs synthetic 20-session fixtures
# ---------------------------------------------------------------------------


def _twenty_prior_sessions(rng_for_session, *, start_hh=11, start_mm=45, n=110):
    """20 prior sessions 2026-06-01..-20, bars 11:45->13:35 ET."""
    out = {}
    for s in range(1, 21):
        d = f"2026-06-{s:02d}"
        out[d] = _mk_bars(d, start_hh, start_mm, n, rng_for_session(s))
    return out


def test_r30_tod_percentile_ranks_against_prior_20_sessions() -> None:
    cur_date = "2026-06-24"
    at = _et_ms(cur_date, 13, 0)
    bars = _twenty_prior_sessions(lambda s: s * 0.5)  # prior values 0.5..10.0

    def with_current(rng: float):
        m = dict(bars)
        m[cur_date] = _mk_bars(cur_date, 12, 15, 45, rng)  # 12:15..12:59
        return m

    assert r30_tod_percentile(with_current(0.2), at) == pytest.approx(0.0)
    assert r30_tod_percentile(with_current(5.1), at) == pytest.approx(50.0)   # 10 of 20 below
    assert r30_tod_percentile(with_current(100.0), at) == pytest.approx(100.0)


def test_percentile_is_time_of_day_matched() -> None:
    """Same current range, opposite verdicts by clock: mornings are quiet
    (range 1), afternoons wild (range 10) in prior history."""
    cur_date = "2026-06-24"
    bars: dict[str, list] = {}
    for s in range(1, 21):
        d = f"2026-06-{s:02d}"
        bars[d] = _mk_bars(d, 10, 0, 61, 1.0) + _mk_bars(d, 14, 0, 61, 10.0)
    bars[cur_date] = _mk_bars(cur_date, 10, 0, 61, 5.0) + _mk_bars(cur_date, 14, 0, 61, 5.0)

    morning = r30_tod_percentile(bars, _et_ms(cur_date, 10, 45))
    afternoon = r30_tod_percentile(bars, _et_ms(cur_date, 14, 45))
    assert morning == pytest.approx(100.0)   # 5.0 dwarfs the quiet-morning history
    assert afternoon == pytest.approx(0.0)   # 5.0 is compressed vs wild afternoons


def test_percentile_none_when_too_few_prior_sessions() -> None:
    cur_date = "2026-06-24"
    at = _et_ms(cur_date, 13, 0)
    bars = {f"2026-06-{s:02d}": _mk_bars(f"2026-06-{s:02d}", 11, 45, 110, 2.0)
            for s in (1, 2, 3)}  # 3 sessions < MIN_TOD_SESSIONS
    bars[cur_date] = _mk_bars(cur_date, 12, 15, 45, 1.0)
    assert r30_tod_percentile(bars, at) is None


def test_percentile_none_when_current_r30_unavailable() -> None:
    cur_date = "2026-06-24"
    bars = _twenty_prior_sessions(lambda s: 2.0)
    # no current-session bars at all
    assert r30_tod_percentile(bars, _et_ms(cur_date, 13, 0)) is None


def test_distribution_uses_last_20_prior_sessions_only() -> None:
    """A 21st (oldest) session with an extreme range must fall out of the
    window; the current session must never feed its own distribution."""
    cur_date = "2026-06-24"
    at = _et_ms(cur_date, 13, 0)
    bars = _twenty_prior_sessions(lambda s: s * 0.5)
    bars["2026-05-15"] = _mk_bars("2026-05-15", 11, 45, 110, 1000.0)  # oldest, extreme
    bars[cur_date] = _mk_bars(cur_date, 12, 15, 45, 999.0)

    samples = build_tod_distribution(bars, session_date=cur_date)
    dates = {d for d, _, _ in samples}
    assert dates == {f"2026-06-{s:02d}" for s in range(1, 21)}  # no 05-15, no current
    # 999 sits above every retained sample => 100th percentile
    assert tod_percentile(samples, at, 999.0) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 3. assess_chop — DB read + per-session cache (deterministic fixed at_ms)
# ---------------------------------------------------------------------------


async def _seed_spx_bars(rows: list[tuple[int, float, float]]) -> None:
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import HistoricalBar
    async with get_session() as session:
        for ts, hi, lo in rows:
            session.add(HistoricalBar(
                symbol="SPX", bar_size="1 min", timestamp_ms=ts,
                open=(hi + lo) / 2, high=hi, low=lo, close=(hi + lo) / 2, volume=1.0,
            ))


def _seed_rows(prior_rng: float, current_rng: float) -> list[tuple[int, float, float]]:
    rows: list[tuple[int, float, float]] = []
    for s in range(1, 21):
        rows += _mk_bars(f"2026-06-{s:02d}", 11, 45, 110, prior_rng)
    rows += _mk_bars("2026-06-24", 12, 15, 45, current_rng)
    return rows


async def test_assess_chop_empty_db_passes_open() -> None:
    state = AppState.build()
    a = await assess_chop(state, at_ms=_et_ms("2026-06-24", 13, 0))
    assert a.percentile is None and a.r30 is None
    assert a.data_gap is not None
    assert a.is_compressed(33.3) is False  # missing data can never skip


async def test_assess_chop_compressed_end_to_end_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_spx_bars(_seed_rows(prior_rng=4.0, current_rng=1.0))
    state = AppState.build()
    at = _et_ms("2026-06-24", 13, 0)

    a = await assess_chop(state, at_ms=at)
    assert a.r30 == pytest.approx(1.0)
    assert a.percentile == pytest.approx(0.0)
    assert a.n_sessions == 20
    assert a.tod_bucket == "13:00-13:30"
    assert a.is_compressed(33.3) is True
    assert ("SPX", "2026-06-24") in state.chop_tod_dist_cache

    # Second call must hit the per-(symbol, session) cache — a rebuild would
    # explode via this monkeypatch.
    def _boom(*args, **kwargs):
        raise AssertionError("distribution rebuilt within the same session")
    monkeypatch.setattr(chop_gate, "build_tod_distribution", _boom)
    b = await assess_chop(state, at_ms=at + _MIN)
    assert b.percentile == pytest.approx(0.0)


async def test_assess_chop_expanded_end_to_end() -> None:
    await _seed_spx_bars(_seed_rows(prior_rng=1.0, current_rng=4.0))
    state = AppState.build()
    a = await assess_chop(state, at_ms=_et_ms("2026-06-24", 13, 0))
    assert a.percentile == pytest.approx(100.0)
    assert a.is_compressed(33.3) is False


# ---------------------------------------------------------------------------
# 4. Scanner wiring — skip path, fail-open, config hot-read, tag-only extras
# ---------------------------------------------------------------------------


def _armed_state(monkeypatch: pytest.MonkeyPatch) -> AppState:
    monkeypatch.setenv("FEF_LIVE_TRADING", "true")
    s = AppState.build()
    s.active_symbol = "SPX"
    s.auto_trade_enabled = True
    s.auto_trade_trigger_types = ["confluence", "aligned_only"]
    s.auto_trade_qty_override = 3
    s.auto_trade_cool_down_sec = 300
    return s


def _option_pricing() -> dict:
    return {
        "contract_strike": 625.0, "right": "C", "entry_limit": 5.1,
        "delta": 0.4, "underlying_price_at_quote": 620.0,
        "t1": 11.1, "t2": 15.1, "t3": 25.1, "stop": 1.1,
    }


def _scanner_verdict(direction: str = "LONG_CALLS", *, bias_disagree: bool = False) -> dict:
    reasoning = f"ALIGNED {direction} — ΔPrice +2.10pts, ΔDrift +30.00M, FE 1.20, bias=NEUTRAL"
    if bias_disagree:
        reasoning += " (bias-disagrees, demoted)"
    return {
        "direction": direction,
        "confidence": "LOW",
        "reasoning": reasoning + ".",
        "strike_rec": 625.0,
        "t1": 635.0, "t2": 645.0, "t3": 670.0, "stop": 610.0,
        "aligned": True,
        "wall_collapse_trigger": False,
        "option_pricing": _option_pricing(),
        "suggested_qty": 5,
        "scanner_symbol": "SPY",
    }


def _stub_assessment(pct: float | None, *, r30v: float | None = 2.5,
                     gap: str | None = None) -> ChopAssessment:
    return ChopAssessment(
        bar_symbol="SPX", session_date="2026-07-02",
        at_ms=int(time.time() * 1000), tod_bucket="13:30-14:00",
        r30=r30v, percentile=pct,
        n_samples=200 if pct is not None else 0,
        n_sessions=20 if pct is not None else 0, data_gap=gap,
    )


def _patch_assess(monkeypatch: pytest.MonkeyPatch, assessment: ChopAssessment) -> None:
    async def fake(state, *, bar_symbol="SPX", at_ms=None):
        return assessment
    monkeypatch.setattr(chop_gate, "assess_chop", fake)


def _record_auto_trades(monkeypatch: pytest.MonkeyPatch) -> list:
    """Wrap the real maybe_auto_trade with a call recorder."""
    from fef_sidecar.engine import auto_trader
    real = auto_trader.maybe_auto_trade
    calls: list = []

    async def wrapper(state, verdict_dict, **kwargs):
        calls.append((verdict_dict.get("direction"), kwargs))
        return await real(state, verdict_dict, **kwargs)

    monkeypatch.setattr(auto_trader, "maybe_auto_trade", wrapper)
    return calls


async def _parents() -> list[LiveOrder]:
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    async with get_session() as session:
        return list((await session.exec(
            select(LiveOrder).where(LiveOrder.bracket_role == "parent")
        )).all())


async def test_compressed_fire_is_skipped_with_full_counterfactual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Actionable verdict + compressed regime => NO trade offered; the signal
    row stays in history tagged skipped_by='chop_gate' and a dedicated
    'chop_gate' audit row records exactly what the filter withheld."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _stub_assessment(18.4, r30v=3.9))
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0, verdict_dict=_scanner_verdict("LONG_PUTS"),
    )

    assert calls == []                       # maybe_auto_trade never invoked
    assert await _parents() == []            # no order placed
    assert state.auto_trade_count_by_symbol == {}  # no budget consumed

    signals = await SignalIterRepository.recent(limit=10)
    assert len(signals) == 1
    row = signals[0]
    assert row.reasoning.startswith("[SCANNER SPX]")
    assert "[CHOP-GATE SKIPPED: r30 pct 18.4 < 33.3]" in row.reasoning
    ex = row.extras
    assert ex["skipped_by"] == "chop_gate"
    assert ex["r30_pct"] == pytest.approx(18.4)
    assert ex["tod_bucket"] == "13:30-14:00"
    assert ex["bias_disagree"] is False
    assert ex["chop_gate"]["skipped"] is True
    assert ex["chop_gate"]["enabled"] is True
    assert ex["chop_gate"]["threshold_pct"] == pytest.approx(33.3)
    assert ex["option_pricing"]["entry_limit"] == pytest.approx(5.1)  # counterfactual intact

    audits = await SignalIterRepository.query_audit_log(category="chop_gate")
    assert len(audits) == 1
    a = audits[0]
    assert a.summary == "CHOP-GATE SKIPPED SCANNER LONG_PUTS SPX: r30 pct 18.4 < 33.3"
    assert a.block_reason_code == "CHOP_GATE_SKIP"
    assert a.details["signal_iter_id"] == row.id
    assert a.details["r30_pct"] == pytest.approx(18.4)
    assert a.details["source"] == "auto_scanner"
    assert "CHOP-GATE SKIPPED" in state.auto_trade_last_block_reason
    # The skip is NOT an auto-trader block — that surface stays clean.
    assert await SignalIterRepository.query_audit_log(category="auto_trade_block") == []


async def test_expanded_fire_trades_and_carries_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _stub_assessment(66.6, r30v=8.2))
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )

    assert len(calls) == 1
    parents = await _parents()
    assert len(parents) == 1 and "source=auto_scanner;" in (parents[0].notes or "")
    assert state.auto_trade_count_by_symbol == {"SPY": 1}

    row = (await SignalIterRepository.recent(limit=10))[0]
    ex = row.extras
    assert "skipped_by" not in ex
    assert ex["r30_pct"] == pytest.approx(66.6)
    assert ex["tod_bucket"] == "13:30-14:00"
    assert ex["bias_disagree"] is False
    assert ex["chop_gate"]["skipped"] is False
    assert "CHOP-GATE" not in row.reasoning
    assert await SignalIterRepository.query_audit_log(category="chop_gate") == []


async def test_missing_data_passes_open_and_logs_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """percentile None (thin/absent history) must NEVER skip the trade."""
    state = _armed_state(monkeypatch)
    gap = "TOD distribution too thin (4 samples / 1 sessions; need >=25/8)"
    _patch_assess(monkeypatch, _stub_assessment(None, r30v=None, gap=gap))
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )

    assert len(calls) == 1
    assert len(await _parents()) == 1
    row = (await SignalIterRepository.recent(limit=10))[0]
    ex = row.extras
    assert "skipped_by" not in ex
    assert ex["r30_pct"] is None
    assert ex["chop_gate"]["data_gap"] == gap
    assert await SignalIterRepository.query_audit_log(category="chop_gate") == []


async def test_config_toggle_disables_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """chop_gate_enabled=false in UIConfig => compressed regime trades anyway.
    Config is re-read per evaluation, so flipping it needs no restart."""
    state = _armed_state(monkeypatch)
    monkeypatch.setattr(mss, "load_config", lambda: UIConfig(chop_gate_enabled=False))
    _patch_assess(monkeypatch, _stub_assessment(5.0))
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )

    assert len(calls) == 1
    assert len(await _parents()) == 1
    ex = (await SignalIterRepository.recent(limit=10))[0].extras
    assert "skipped_by" not in ex
    assert ex["chop_gate"]["enabled"] is False
    assert ex["chop_gate"]["skipped"] is False
    assert ex["r30_pct"] == pytest.approx(5.0)  # tag still recorded for research


async def test_config_threshold_is_hot_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same 40th-percentile fire: skipped under threshold 50, trades under
    the default 33.3 — proving the threshold comes from config at call time."""
    _patch_assess(monkeypatch, _stub_assessment(40.0))

    state1 = _armed_state(monkeypatch)
    monkeypatch.setattr(
        mss, "load_config", lambda: UIConfig(chop_gate_pct_threshold=50.0)
    )
    calls1 = _record_auto_trades(monkeypatch)
    await handle_actionable_scanner_verdict(
        state1, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )
    assert calls1 == []
    audits = await SignalIterRepository.query_audit_log(category="chop_gate")
    assert len(audits) == 1
    assert audits[0].summary == "CHOP-GATE SKIPPED SCANNER LONG_CALLS SPY: r30 pct 40.0 < 50.0"

    # Back to default threshold: identical fire now passes.
    state2 = _armed_state(monkeypatch)
    monkeypatch.setattr(mss, "load_config", lambda: UIConfig())
    calls2 = _record_auto_trades(monkeypatch)
    await handle_actionable_scanner_verdict(
        state2, "QQQ", spot=550.0, verdict_dict=_scanner_verdict(),
    )
    assert len(calls2) == 1
    assert state2.auto_trade_count_by_symbol == {"QQQ": 1}


async def test_bias_disagree_tag_parsed_from_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tag parsing in isolation (veto disabled): a disagreeing fire still
    trades and carries bias_disagree=True for research."""
    state = _armed_state(monkeypatch)
    monkeypatch.setattr(mss, "load_config", lambda: UIConfig(bias_veto_enabled=False))
    _patch_assess(monkeypatch, _stub_assessment(75.0))
    calls = _record_auto_trades(monkeypatch)
    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0,
        verdict_dict=_scanner_verdict(bias_disagree=True),
    )
    assert len(calls) == 1  # veto off => trade offered
    ex = (await SignalIterRepository.recent(limit=10))[0].extras
    assert ex["bias_disagree"] is True
    assert "skipped_by" not in ex
    assert await SignalIterRepository.query_audit_log(category="bias_veto") == []


# ---------------------------------------------------------------------------
# 4b. Bias-disagree veto — promoted from tag to SKIP (2026-07-02)
# ---------------------------------------------------------------------------


async def test_bias_veto_skips_disagreeing_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default config: an expanded-regime fire whose reasoning carries the
    bias-disagree demotion is SKIPPED — persisted + audited with its own
    category, never offered to the auto-trader."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _stub_assessment(75.0))  # NOT compressed
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0,
        verdict_dict=_scanner_verdict("LONG_PUTS", bias_disagree=True),
    )

    assert calls == [] and await _parents() == []
    assert state.auto_trade_count_by_symbol == {}   # no budget consumed
    row = (await SignalIterRepository.recent(limit=10))[0]
    assert row.extras["skipped_by"] == "bias_veto"
    assert row.extras["bias_disagree"] is True
    assert "[BIAS-VETO SKIPPED]" in row.reasoning
    assert row.extras["option_pricing"]["entry_limit"] == pytest.approx(5.1)  # counterfactual
    audits = await SignalIterRepository.query_audit_log(category="bias_veto")
    assert len(audits) == 1
    a = audits[0]
    assert a.summary == (
        "BIAS-VETO SKIPPED SCANNER LONG_PUTS SPX: "
        "drift bias disagrees with fire direction"
    )
    assert a.block_reason_code == "BIAS_VETO_SKIP"
    assert a.details["signal_iter_id"] == row.id
    assert "BIAS-VETO" in state.auto_trade_last_block_reason
    # Distinct surfaces: no chop_gate row for a bias skip.
    assert await SignalIterRepository.query_audit_log(category="chop_gate") == []


async def test_gate_ordering_chop_wins_over_bias_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fire that is BOTH compressed and bias-disagree logs chop_gate as
    the skipper — first gate wins, counterfactual populations never blur."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _stub_assessment(12.0))  # compressed
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0,
        verdict_dict=_scanner_verdict("LONG_PUTS", bias_disagree=True),
    )

    assert calls == []
    row = (await SignalIterRepository.recent(limit=10))[0]
    assert row.extras["skipped_by"] == "chop_gate"      # first gate wins
    assert row.extras["bias_disagree"] is True          # tag still recorded
    assert "[CHOP-GATE SKIPPED" in row.reasoning
    assert "[BIAS-VETO SKIPPED]" not in row.reasoning
    assert len(await SignalIterRepository.query_audit_log(category="chop_gate")) == 1
    assert await SignalIterRepository.query_audit_log(category="bias_veto") == []


# ---------------------------------------------------------------------------
# 5. Impulse attention flag — advisory only, never trades
# ---------------------------------------------------------------------------


from fef_sidecar.engine.multi_symbol_scanner import maybe_impulse_flag  # noqa: E402


async def _impulse_rows():
    signals = await SignalIterRepository.recent(limit=20)
    audits = await SignalIterRepository.query_audit_log(category="impulse_flag")
    return (
        [r for r in signals if (r.extras or {}).get("source") == "impulse_flag"],
        audits,
    )


async def test_impulse_flag_fires_on_impulse_agree_noncompressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Impulse + flow agreement + non-compressed regime => advisory flag:
    audit row + WAIT signal row; the auto-trader is never touched even on a
    fully ARMED state."""
    state = _armed_state(monkeypatch)   # armed on purpose — must still not trade
    _patch_assess(monkeypatch, _stub_assessment(49.0))
    calls = _record_auto_trades(monkeypatch)

    fired = await maybe_impulse_flag(
        state, "SPX", spot=7441.7, delta_price=9.4, delta_drift_m=1.6,
    )
    assert fired is True
    assert calls == [] and await _parents() == []   # advisory only

    rows, audits = await _impulse_rows()
    assert len(rows) == 1 and len(audits) == 1
    row = rows[0]
    assert row.direction.value == "WAIT"            # untradeable by construction
    assert row.confidence.value == "NONE"
    assert row.extras["advisory"] is True
    assert row.extras["impulse_direction"] == "UP"
    assert row.extras["flow_agree"] is True
    assert row.extras["r30_pct"] == pytest.approx(49.0)
    assert row.reasoning.startswith("[IMPULSE UP]")
    a = audits[0]
    assert a.summary == "IMPULSE UP SPX @ 7441.7 (dP +9.4pts, dDrift +1.6M agree, r30 pct 49)"
    assert a.details["signal_iter_id"] == row.id
    assert a.details["symbol"] == "SPX"


async def test_impulse_flag_vetoed_when_flow_disagrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disagree-veto is the validated core (7/2 17:58 disagreeing
    impulse = session-low puts trap): big dPrice with opposing dDrift stays
    silent under the default require_agree=true."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _stub_assessment(60.0))
    assert await maybe_impulse_flag(
        state, "SPX", spot=7400.0, delta_price=-9.0, delta_drift_m=+2.0,
    ) is False
    rows, audits = await _impulse_rows()
    assert rows == [] and audits == []

    # Opt-out: require_agree=false flags it, honestly labeled 'disagree'.
    monkeypatch.setattr(
        mss, "load_config", lambda: UIConfig(impulse_flag_require_agree=False)
    )
    assert await maybe_impulse_flag(
        state, "SPX", spot=7400.0, delta_price=-9.0, delta_drift_m=+2.0,
    ) is True
    _, audits2 = await _impulse_rows()
    assert "dDrift +2.0M disagree" in audits2[0].summary


async def test_impulse_flag_suppressed_in_compressed_regime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _stub_assessment(12.0))  # confirmed compressed
    assert await maybe_impulse_flag(
        state, "SPX", spot=7400.0, delta_price=9.0, delta_drift_m=2.0,
    ) is False
    rows, audits = await _impulse_rows()
    assert rows == [] and audits == []


async def test_impulse_flag_unknown_regime_passes_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Percentile None (no bar history) must not silence a real impulse —
    only a CONFIRMED compressed regime vetoes (fail-open, like the gate)."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _stub_assessment(None, r30v=None, gap="no bars"))
    assert await maybe_impulse_flag(
        state, "SPX", spot=7400.0, delta_price=9.0, delta_drift_m=2.0,
    ) is True
    _, audits = await _impulse_rows()
    assert "r30 pct n/a" in audits[0].summary


async def test_impulse_flag_cooldown_per_symbol_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _stub_assessment(60.0))

    assert await maybe_impulse_flag(
        state, "SPX", spot=7400.0, delta_price=9.0, delta_drift_m=2.0,
    ) is True
    # Same symbol+direction inside the 600s window: suppressed.
    assert await maybe_impulse_flag(
        state, "SPX", spot=7405.0, delta_price=8.0, delta_drift_m=2.0,
    ) is False
    # Opposite direction has its own clock.
    assert await maybe_impulse_flag(
        state, "SPX", spot=7395.0, delta_price=-9.0, delta_drift_m=-2.0,
    ) is True
    # Expire the UP clock -> re-flags.
    state.impulse_flag_last_at_ms[("SPX", "UP")] -= 601_000
    assert await maybe_impulse_flag(
        state, "SPX", spot=7410.0, delta_price=9.0, delta_drift_m=2.0,
    ) is True
    rows, audits = await _impulse_rows()
    assert len(rows) == 3 and len(audits) == 3


async def test_impulse_flag_threshold_scales_for_etf_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6.0 SPX-calibrated pts => ~0.50 pts on SPY @620 (spot/7500), while
    SPX-class keeps the raw threshold (scale 1.0)."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _stub_assessment(60.0))
    # SPY: 0.6 pts >= 6.0 * 620/7500 (=0.496) -> fires
    assert await maybe_impulse_flag(
        state, "SPY", spot=620.0, delta_price=0.6, delta_drift_m=1.0,
    ) is True
    # SPX: 5.9 pts < 6.0 (scale pinned to 1.0 for spot >= 2000) -> silent
    assert await maybe_impulse_flag(
        state, "SPX", spot=7441.0, delta_price=5.9, delta_drift_m=1.0,
    ) is False


async def test_impulse_flag_disabled_via_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _stub_assessment(60.0))
    monkeypatch.setattr(
        mss, "load_config", lambda: UIConfig(impulse_flag_enabled=False)
    )
    assert await maybe_impulse_flag(
        state, "SPX", spot=7400.0, delta_price=9.0, delta_drift_m=2.0,
    ) is False
    rows, audits = await _impulse_rows()
    assert rows == [] and audits == []


async def test_impulse_flag_advisory_row_rejected_by_auto_trader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt + suspenders: even if someone replayed the advisory row into the
    auto-trader, its WAIT direction hard-blocks at gate 3."""
    from fef_sidecar.engine.auto_trader import maybe_auto_trade

    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _stub_assessment(60.0))
    await maybe_impulse_flag(
        state, "SPX", spot=7400.0, delta_price=9.0, delta_drift_m=2.0,
    )
    rows, _ = await _impulse_rows()
    replay = {
        "direction": rows[0].direction.value,   # WAIT
        "confidence": rows[0].confidence.value,
        "option_pricing": _option_pricing(),
        "strike_rec": 7410.0,
    }
    decision = await maybe_auto_trade(state, replay, symbol="SPX", source="auto_scanner")
    assert not decision.fired and decision.reason_code == "VERDICT_WAIT"
    assert await _parents() == []
