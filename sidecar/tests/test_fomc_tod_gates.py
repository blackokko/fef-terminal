"""FOMC-morning gate + time-of-day no-trade window (2026-07-06 batch).

Evidence:
  - FOMC gate: event study over scanner-fire history — FOMC pre-14:00 fires
    average -$61/ct (t=-4.4), 80% of FOMC sessions negative.
  - TOD window: user request; the entry-filter sweep's worst TOD bucket is
    14:00-15:30 ET at -$21/ct OOS. NOTE: 7/5's one winner fired 14:09 ET and
    WOULD have been tod-skipped — the counterfactual rows persisted by this
    gate grade that trade-off in production.

Covers: gate ordering (chop -> bias -> fomc -> tod, first gate wins, all
four), FOMC date membership via engine/event_calendar, pre/post-statement
boundary, TOD window boundaries (start inclusive, end exclusive), config
hot-read + toggles, misconfigured-window fail-open, and the counterfactual
persistence contract (signal row + distinct audit category + block code).
"""

from __future__ import annotations

import os
import time

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.config_store import UIConfig
from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.engine import chop_gate
from fef_sidecar.engine import multi_symbol_scanner as mss
from fef_sidecar.engine.chop_gate import ChopAssessment
from fef_sidecar.engine.multi_symbol_scanner import handle_actionable_scanner_verdict
from fef_sidecar.state import AppState

FOMC_DAY = "2026-07-29"      # next FOMC statement day (event_calendar)
CLEAR_DAY = "2026-07-07"     # ordinary session, not FOMC


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.fixture(autouse=True)
def _deterministic_config(monkeypatch: pytest.MonkeyPatch):
    """Never read the live ~/.fef-terminal/config.json from tests. Floor
    pinned LOW at the config_store source (auto_trader imports lazily) —
    this suite's LOW-confidence verdicts test the FOMC/TOD gates, not the
    2026-07-08 always-on confidence floor (test_confidence_floor.py owns
    that)."""
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(mss, "load_config", lambda: UIConfig())
    monkeypatch.setattr(
        cs, "load_config",
        lambda: UIConfig(auto_trade_confidence_floor="LOW"),
    )
    monkeypatch.setattr(cs, "config_file_status", lambda: "ok")


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


def _pin_clock(monkeypatch: pytest.MonkeyPatch, date_str: str, hh: int, mm: int) -> None:
    monkeypatch.setattr(
        mss, "_now_et_parts", lambda now_ms=None: (date_str, hh * 60 + mm),
    )


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
        # Fresh fire-time quote — gate 9.5 (marketable-LMT entry, 2026-07-16)
        # prices the entry from this ask without a re-fetch.
        "bid": 4.9, "ask": 5.1,
        "quote_ts_ms": int(__import__("time").time() * 1000),
    }


def _scanner_verdict(direction: str = "LONG_CALLS", *, bias_disagree: bool = False) -> dict:
    reasoning = f"ALIGNED {direction} — ΔPrice +2.10pts, ΔDrift +30.00M, FE 1.20"
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


def _expanded_assessment() -> ChopAssessment:
    """Non-compressed regime — the chop gate passes."""
    return ChopAssessment(
        bar_symbol="SPX", session_date=CLEAR_DAY,
        at_ms=int(time.time() * 1000), tod_bucket="10:30-11:00",
        r30=8.2, percentile=66.6, n_samples=200, n_sessions=20, data_gap=None,
    )


def _compressed_assessment() -> ChopAssessment:
    return ChopAssessment(
        bar_symbol="SPX", session_date=CLEAR_DAY,
        at_ms=int(time.time() * 1000), tod_bucket="10:30-11:00",
        r30=1.1, percentile=12.0, n_samples=200, n_sessions=20, data_gap=None,
    )


def _patch_assess(monkeypatch: pytest.MonkeyPatch, assessment: ChopAssessment) -> None:
    async def fake(state, *, bar_symbol="SPX", at_ms=None):
        return assessment
    monkeypatch.setattr(chop_gate, "assess_chop", fake)


def _record_auto_trades(monkeypatch: pytest.MonkeyPatch) -> list:
    from fef_sidecar.engine import auto_trader
    real = auto_trader.maybe_auto_trade
    calls: list = []

    async def wrapper(state, verdict_dict, **kwargs):
        calls.append((verdict_dict.get("direction"), kwargs))
        return await real(state, verdict_dict, **kwargs)

    monkeypatch.setattr(auto_trader, "maybe_auto_trade", wrapper)
    return calls


async def _last_signal():
    rows = await SignalIterRepository.recent(limit=10)
    assert rows, "expected a persisted signal row"
    return rows[0]


# ---------------------------------------------------------------------------
# 1. FOMC-morning gate
# ---------------------------------------------------------------------------


async def test_fomc_gate_skips_pre_statement_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FOMC day, 10:30 ET (pre-statement): fire is SKIPPED with the full
    counterfactual persisted — signal row tagged skipped_by='fomc_gate',
    reasoning suffix, dedicated 'fomc_gate' audit row, FOMC_GATE_SKIP code,
    no maybe_auto_trade call, no budget consumed."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, FOMC_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0, verdict_dict=_scanner_verdict("LONG_PUTS"),
    )

    assert calls == []
    assert state.auto_trade_count_by_symbol == {}
    row = await _last_signal()
    assert row.extras["skipped_by"] == "fomc_gate"
    assert "[FOMC-GATE SKIPPED: pre-statement]" in row.reasoning
    assert row.extras["fomc_gate"]["fomc_day"] is True
    assert row.extras["fomc_gate"]["pre_statement"] is True
    assert row.extras["fomc_gate"]["skipped"] is True
    assert row.extras["fomc_gate"]["minute_et"] == 10 * 60 + 30
    assert row.extras["option_pricing"]["entry_limit"] == pytest.approx(5.1)

    audits = await SignalIterRepository.query_audit_log(category="fomc_gate")
    assert len(audits) == 1
    a = audits[0]
    assert a.summary == (
        "FOMC-GATE SKIPPED SCANNER LONG_PUTS SPX: "
        f"pre-statement (10:30 ET < 14:00 on FOMC day {FOMC_DAY})"
    )
    assert a.block_reason_code == "FOMC_GATE_SKIP"
    assert a.details["signal_iter_id"] == row.id
    assert a.details["session_date"] == FOMC_DAY
    assert "FOMC-GATE SKIPPED" in state.auto_trade_last_block_reason
    # Distinct surfaces: no other gate category logged.
    assert await SignalIterRepository.query_audit_log(category="chop_gate") == []
    assert await SignalIterRepository.query_audit_log(category="tod_gate") == []


async def test_fomc_gate_allows_post_statement_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same FOMC day at 15:45 ET (post-statement AND outside the default TOD
    window): the fire trades, with the FOMC tag still recorded."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, FOMC_DAY, 15, 45)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )

    assert len(calls) == 1
    row = await _last_signal()
    assert "skipped_by" not in row.extras
    assert row.extras["fomc_gate"]["fomc_day"] is True
    assert row.extras["fomc_gate"]["pre_statement"] is False
    assert row.extras["fomc_gate"]["skipped"] is False
    assert await SignalIterRepository.query_audit_log(category="fomc_gate") == []


async def test_fomc_gate_statement_minute_is_not_pre_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly 14:00 ET on an FOMC day is NOT pre-statement (the gate covers
    the morning; 14:00 falls to the TOD window, which then skips it under
    the default 14:00-15:30 config — distinct category)."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, FOMC_DAY, 14, 0)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0, verdict_dict=_scanner_verdict(),
    )

    assert calls == []
    row = await _last_signal()
    assert row.extras["skipped_by"] == "tod_gate"          # NOT fomc_gate
    assert row.extras["fomc_gate"]["pre_statement"] is False
    assert await SignalIterRepository.query_audit_log(category="fomc_gate") == []
    assert len(await SignalIterRepository.query_audit_log(category="tod_gate")) == 1


async def test_fomc_gate_non_fomc_day_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )

    assert len(calls) == 1
    row = await _last_signal()
    assert "skipped_by" not in row.extras
    assert row.extras["fomc_gate"]["fomc_day"] is False
    assert await SignalIterRepository.query_audit_log(category="fomc_gate") == []


async def test_fomc_gate_config_toggle_hot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fomc_gate_enabled=false: the same FOMC-morning fire trades (config is
    re-read per evaluation — no restart)."""
    state = _armed_state(monkeypatch)
    monkeypatch.setattr(mss, "load_config", lambda: UIConfig(fomc_gate_enabled=False))
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, FOMC_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )

    assert len(calls) == 1
    row = await _last_signal()
    assert "skipped_by" not in row.extras
    assert row.extras["fomc_gate"]["enabled"] is False
    assert row.extras["fomc_gate"]["pre_statement"] is True   # tag still recorded
    assert await SignalIterRepository.query_audit_log(category="fomc_gate") == []


# ---------------------------------------------------------------------------
# 2. TOD no-trade window
# ---------------------------------------------------------------------------


async def test_tod_gate_skips_inside_default_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """14:09 ET (the exact clock of 7/5's one winner — this gate would have
    skipped it; the persisted counterfactual grades that trade-off): SKIPPED
    with signal row + 'tod_gate' audit + TOD_GATE_SKIP, no trade offered."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 14, 9)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0, verdict_dict=_scanner_verdict("LONG_PUTS"),
    )

    assert calls == []
    assert state.auto_trade_count_by_symbol == {}
    row = await _last_signal()
    assert row.extras["skipped_by"] == "tod_gate"
    assert "[TOD-GATE SKIPPED: 14:09 in 14:00-15:30 ET]" in row.reasoning
    assert row.extras["tod_gate"]["in_window"] is True
    assert row.extras["tod_gate"]["window_et"] == "14:00-15:30"
    assert row.extras["tod_gate"]["skipped"] is True
    assert row.extras["option_pricing"]["entry_limit"] == pytest.approx(5.1)

    audits = await SignalIterRepository.query_audit_log(category="tod_gate")
    assert len(audits) == 1
    a = audits[0]
    assert a.summary == (
        "TOD-GATE SKIPPED SCANNER LONG_PUTS SPX: "
        "14:09 ET inside no-trade window 14:00-15:30"
    )
    assert a.block_reason_code == "TOD_GATE_SKIP"
    assert a.details["signal_iter_id"] == row.id
    assert a.details["window_et"] == "14:00-15:30"
    assert "TOD-GATE SKIPPED" in state.auto_trade_last_block_reason
    assert await SignalIterRepository.query_audit_log(category="fomc_gate") == []


@pytest.mark.parametrize(
    ("hh", "mm", "should_skip"),
    [
        (13, 59, False),   # just before the window
        (14, 0, True),     # start is INCLUSIVE
        (14, 9, True),
        (15, 29, True),    # last minute inside
        (15, 30, False),   # end is EXCLUSIVE
        (15, 31, False),
    ],
)
async def test_tod_gate_window_boundaries(
    monkeypatch: pytest.MonkeyPatch, hh: int, mm: int, should_skip: bool,
) -> None:
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, hh, mm)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )

    row = await _last_signal()
    if should_skip:
        assert calls == []
        assert row.extras["skipped_by"] == "tod_gate"
    else:
        assert len(calls) == 1
        assert "skipped_by" not in row.extras


async def test_tod_gate_window_is_hot_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom 10:00-11:00 window: 10:30 skips, 14:09 (default window) trades
    — proving the edges come from config at call time."""
    monkeypatch.setattr(mss, "load_config", lambda: UIConfig(
        tod_gate_start_et="10:00", tod_gate_end_et="11:00",
    ))
    _patch_assess(monkeypatch, _expanded_assessment())

    state1 = _armed_state(monkeypatch)
    _pin_clock(monkeypatch, CLEAR_DAY, 10, 30)
    calls1 = _record_auto_trades(monkeypatch)
    await handle_actionable_scanner_verdict(
        state1, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )
    assert calls1 == []
    row = await _last_signal()
    assert row.extras["skipped_by"] == "tod_gate"
    assert row.extras["tod_gate"]["window_et"] == "10:00-11:00"

    state2 = _armed_state(monkeypatch)
    _pin_clock(monkeypatch, CLEAR_DAY, 14, 9)
    calls2 = _record_auto_trades(monkeypatch)
    await handle_actionable_scanner_verdict(
        state2, "QQQ", spot=550.0, verdict_dict=_scanner_verdict(),
    )
    assert len(calls2) == 1


async def test_tod_gate_config_toggle_hot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _armed_state(monkeypatch)
    monkeypatch.setattr(mss, "load_config", lambda: UIConfig(tod_gate_enabled=False))
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 14, 9)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )

    assert len(calls) == 1
    row = await _last_signal()
    assert "skipped_by" not in row.extras
    assert row.extras["tod_gate"]["enabled"] is False
    assert row.extras["tod_gate"]["in_window"] is True   # tag still recorded
    assert await SignalIterRepository.query_audit_log(category="tod_gate") == []


async def test_tod_gate_misconfigured_window_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverted window (start >= end): the gate must FAIL OPEN (trade), never
    guess — harm-reduction filters may not block on their own misconfig."""
    state = _armed_state(monkeypatch)
    monkeypatch.setattr(mss, "load_config", lambda: UIConfig(
        tod_gate_start_et="15:00", tod_gate_end_et="14:00",
    ))
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 14, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )

    assert len(calls) == 1
    row = await _last_signal()
    assert "skipped_by" not in row.extras
    assert row.extras["tod_gate"]["enabled"] is False   # disabled by misconfig
    assert await SignalIterRepository.query_audit_log(category="tod_gate") == []


# ---------------------------------------------------------------------------
# 3. Gate ordering — chop -> bias -> fomc -> tod, first gate wins (all four)
# ---------------------------------------------------------------------------


async def test_ordering_chop_wins_over_fomc_and_tod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compressed regime on an FOMC morning: chop_gate is the skipper; the
    fomc/tod tags are still recorded but no fomc/tod audit rows exist."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _compressed_assessment())
    _pin_clock(monkeypatch, FOMC_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0, verdict_dict=_scanner_verdict(),
    )

    assert calls == []
    row = await _last_signal()
    assert row.extras["skipped_by"] == "chop_gate"
    assert row.extras["fomc_gate"]["pre_statement"] is True   # would have skipped
    assert row.extras["fomc_gate"]["skipped"] is False        # but chop won
    assert len(await SignalIterRepository.query_audit_log(category="chop_gate")) == 1
    assert await SignalIterRepository.query_audit_log(category="fomc_gate") == []
    assert await SignalIterRepository.query_audit_log(category="tod_gate") == []


async def test_ordering_bias_wins_over_fomc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, FOMC_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0,
        verdict_dict=_scanner_verdict("LONG_PUTS", bias_disagree=True),
    )

    assert calls == []
    row = await _last_signal()
    assert row.extras["skipped_by"] == "bias_veto"
    assert row.extras["fomc_gate"]["skipped"] is False
    assert len(await SignalIterRepository.query_audit_log(category="bias_veto")) == 1
    assert await SignalIterRepository.query_audit_log(category="fomc_gate") == []


async def test_ordering_fomc_wins_over_tod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom 10:00-11:00 TOD window on an FOMC morning at 10:30 — both gates
    would skip; fomc_gate (earlier in the chain) is the recorded skipper."""
    state = _armed_state(monkeypatch)
    monkeypatch.setattr(mss, "load_config", lambda: UIConfig(
        tod_gate_start_et="10:00", tod_gate_end_et="11:00",
    ))
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, FOMC_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0, verdict_dict=_scanner_verdict(),
    )

    assert calls == []
    row = await _last_signal()
    assert row.extras["skipped_by"] == "fomc_gate"
    assert row.extras["tod_gate"]["in_window"] is True    # would have skipped
    assert row.extras["tod_gate"]["skipped"] is False     # but fomc won
    assert len(await SignalIterRepository.query_audit_log(category="fomc_gate")) == 1
    assert await SignalIterRepository.query_audit_log(category="tod_gate") == []


async def test_ordering_tod_is_last_resort_skipper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expanded regime, bias agrees, non-FOMC day, 14:09 ET: only the TOD
    gate skips — proving the chain reaches gate 4 when 1-3 pass."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 14, 9)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0, verdict_dict=_scanner_verdict(),
    )

    assert calls == []
    row = await _last_signal()
    assert row.extras["skipped_by"] == "tod_gate"
    assert row.extras["chop_gate"]["skipped"] is False
    assert row.extras["bias_disagree"] is False
    assert row.extras["fomc_gate"]["skipped"] is False
    assert len(await SignalIterRepository.query_audit_log(category="tod_gate")) == 1
    for cat in ("chop_gate", "bias_veto", "fomc_gate"):
        assert await SignalIterRepository.query_audit_log(category=cat) == []


async def test_all_gates_pass_trades_with_all_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clear morning, expanded regime, bias agrees: the fire trades and every
    gate's tag rides along for research."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )

    assert len(calls) == 1
    assert state.auto_trade_count_by_symbol == {"SPY": 1}
    row = await _last_signal()
    ex = row.extras
    assert "skipped_by" not in ex
    assert ex["chop_gate"]["skipped"] is False
    assert ex["fomc_gate"] == {
        "enabled": True, "fomc_day": False, "pre_statement": False,
        "minute_et": 10 * 60 + 30, "statement_minute_et": 14 * 60,
        "skipped": False,
    }
    assert ex["tod_gate"] == {
        "enabled": True, "window_et": "14:00-15:30", "in_window": False,
        "minute_et": 10 * 60 + 30, "skipped": False,
    }
    for cat in ("chop_gate", "bias_veto", "fomc_gate", "tod_gate"):
        assert await SignalIterRepository.query_audit_log(category=cat) == []


# ---------------------------------------------------------------------------
# 4. Helpers
# ---------------------------------------------------------------------------


def test_parse_hhmm() -> None:
    assert mss._parse_hhmm("14:00") == 840
    assert mss._parse_hhmm("15:30") == 930
    assert mss._parse_hhmm("00:00") == 0
    assert mss._parse_hhmm("23:59") == 23 * 60 + 59
    assert mss._parse_hhmm("24:00") is None
    assert mss._parse_hhmm("14:60") is None
    assert mss._parse_hhmm("garbage") is None
    assert mss._parse_hhmm(None) is None


def test_fmt_minute_et() -> None:
    assert mss._fmt_minute_et(840) == "14:00"
    assert mss._fmt_minute_et(849) == "14:09"
    assert mss._fmt_minute_et(930) == "15:30"
