"""Tier-scoped gate bypass (2026-07-09 registered study).

Change under test: the scanner CHOP / BIAS-VETO / TOD gates run BEFORE the
2026-07-08 HIGH confidence floor, so on a HIGH fire they only ever pre-empt
trades the floor already passes (profitable ones — study: ~+$3,134/ct and
~35% of fire frequency left on the table). So a HIGH-confidence verdict now
BYPASSES chop / bias-veto / TOD; LOW/MEDIUM fires run the full chain
unchanged. The FOMC gate is DELIBERATELY NOT bypassed — it binds on ALL
tiers as an event-risk guard (blocks HIGH fires that lose -$28/ct on FOMC
mornings). Toggle: UIConfig.gate_high_bypass_enabled (default true, hot-read);
false restores the old all-tiers behavior.

Covered contract:
  - HIGH fire bypasses chop / bias-veto / TOD (trades, carries the
    extras.high_bypassed_gate tag + reasoning note),
  - LOW *and* MEDIUM fires are still chop/TOD-blocked (counterfactual skip
    row + distinct audit category unchanged),
  - FOMC still blocks a HIGH fire (all tiers) — even when chop would also
    have fired (fomc wins, NO bypass tag: the fire is still skipped),
  - clear-tape HIGH fire trades with NO bypass tag (nothing to grade),
  - gate_high_bypass_enabled=false restores the old chop/TOD block on HIGH.

The floor itself (LOW/MEDIUM blocked at maybe_auto_trade) is owned by
test_confidence_floor.py; this suite pins the config-store floor LOW so the
gate-BYPASS behavior is isolated (a HIGH fire that reaches maybe_auto_trade
fires cleanly, letting us assert it actually traded).
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
    """Never read live ~/.fef-terminal/config.json. Scanner gate config comes
    from mss.load_config (default UIConfig => gate_high_bypass_enabled=True);
    the auto_trader floor is pinned LOW at the config_store source so a HIGH
    fire that BYPASSES the gates then fires cleanly (isolates gate scoping
    from the floor, which test_confidence_floor.py owns)."""
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


@pytest.fixture(autouse=True)
def _no_kill_switch(monkeypatch: pytest.MonkeyPatch):
    """The MAJOR-2 floor coupling makes high_bypass require the floor ARMED
    (FEF_DISABLE_CONFIDENCE_FLOOR unset). Pin a clean baseline so a stray
    kill-switch from another module can't silently disable the bypass under
    test; the MAJOR-2 case sets it back on explicitly."""
    monkeypatch.delenv("FEF_DISABLE_CONFIDENCE_FLOOR", raising=False)


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


def _verdict(
    direction: str = "LONG_CALLS",
    *,
    confidence: str = "HIGH",
    bias_disagree: bool = False,
) -> dict:
    reasoning = f"ALIGNED {direction} — ΔPrice +2.10pts, ΔDrift +30.00M, FE 1.20"
    if bias_disagree:
        reasoning += " (bias-disagrees, demoted)"
    return {
        "direction": direction,
        "confidence": confidence,
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
    """Non-compressed regime — the chop gate passes (r30 pct 66.6 >= 33.3)."""
    return ChopAssessment(
        bar_symbol="SPX", session_date=CLEAR_DAY,
        at_ms=int(time.time() * 1000), tod_bucket="10:30-11:00",
        r30=8.2, percentile=66.6, n_samples=200, n_sessions=20, data_gap=None,
    )


def _compressed_assessment() -> ChopAssessment:
    """Low-range tape — r30 pct 12.0 < 33.3 (the chop gate would block)."""
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
# 1. CHOP gate — HIGH bypasses, LOW/MEDIUM still blocked
# ---------------------------------------------------------------------------


async def test_high_fire_bypasses_chop_on_low_range_tape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH fire, compressed tape (r30 pct 12 < 33.3): NOT chop-blocked — it
    reaches maybe_auto_trade and trades, carries extras.high_bypassed_gate=
    'chop' + a reasoning note, and writes NO chop_gate audit row."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _compressed_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_verdict(confidence="HIGH"),
    )

    assert len(calls) == 1
    assert state.auto_trade_count_by_symbol == {"SPY": 1}   # actually traded
    row = await _last_signal()
    assert "skipped_by" not in row.extras
    assert row.extras["high_bypassed_gate"] == "chop"
    assert row.extras["high_bypass_enabled"] is True
    assert "[HIGH-BYPASS chop]" in row.reasoning
    assert row.extras["chop_gate"]["skipped"] is False        # gate did not skip
    assert row.extras["chop_gate"]["r30_pct"] == pytest.approx(12.0)
    # No skip audit row for any gate — the fire traded.
    for cat in ("chop_gate", "bias_veto", "fomc_gate", "tod_gate"):
        assert await SignalIterRepository.query_audit_log(category=cat) == []


@pytest.mark.parametrize("confidence", ["LOW", "MEDIUM"])
async def test_sub_floor_fire_still_chop_blocked_on_low_range_tape(
    monkeypatch: pytest.MonkeyPatch, confidence: str,
) -> None:
    """LOW *and* MEDIUM fires are unaffected by the bypass: compressed tape
    still chop-blocks them with the full counterfactual (skip row + audit),
    and NO high_bypassed_gate tag is written."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _compressed_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0,
        verdict_dict=_verdict("LONG_PUTS", confidence=confidence),
    )

    assert calls == []
    assert state.auto_trade_count_by_symbol == {}
    row = await _last_signal()
    assert row.extras["skipped_by"] == "chop_gate"
    assert "high_bypassed_gate" not in row.extras
    assert row.extras["high_bypass_enabled"] is True    # flag on, just not HIGH
    assert "[CHOP-GATE SKIPPED" in row.reasoning
    audits = await SignalIterRepository.query_audit_log(category="chop_gate")
    assert len(audits) == 1
    assert audits[0].block_reason_code == "CHOP_GATE_SKIP"


async def test_high_bypass_disabled_restores_chop_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gate_high_bypass_enabled=false: the SAME HIGH fire on compressed tape
    is chop-blocked again (old all-tiers behavior, hot-read — no restart)."""
    state = _armed_state(monkeypatch)
    monkeypatch.setattr(mss, "load_config", lambda: UIConfig(gate_high_bypass_enabled=False))
    _patch_assess(monkeypatch, _compressed_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0, verdict_dict=_verdict(confidence="HIGH"),
    )

    assert calls == []
    row = await _last_signal()
    assert row.extras["skipped_by"] == "chop_gate"
    assert "high_bypassed_gate" not in row.extras
    assert row.extras["high_bypass_enabled"] is False
    assert len(await SignalIterRepository.query_audit_log(category="chop_gate")) == 1


# ---------------------------------------------------------------------------
# 2. BIAS-VETO gate — HIGH bypasses (belt-and-suspenders; the study found
#    HIGH is only assigned when direction AGREES with drift bias, so this is
#    future-proofing, but the tier check must still hold).
# ---------------------------------------------------------------------------


async def test_high_fire_bypasses_bias_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0,
        verdict_dict=_verdict("LONG_PUTS", confidence="HIGH", bias_disagree=True),
    )

    assert len(calls) == 1
    row = await _last_signal()
    assert "skipped_by" not in row.extras
    assert row.extras["high_bypassed_gate"] == "bias"
    assert "[HIGH-BYPASS bias]" in row.reasoning
    assert await SignalIterRepository.query_audit_log(category="bias_veto") == []


async def test_low_fire_still_bias_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0,
        verdict_dict=_verdict("LONG_PUTS", confidence="LOW", bias_disagree=True),
    )

    assert calls == []
    row = await _last_signal()
    assert row.extras["skipped_by"] == "bias_veto"
    assert "high_bypassed_gate" not in row.extras
    assert len(await SignalIterRepository.query_audit_log(category="bias_veto")) == 1


# ---------------------------------------------------------------------------
# 3. TOD no-trade window — HIGH bypasses, LOW still blocked
# ---------------------------------------------------------------------------


async def test_high_fire_bypasses_tod_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH fire at 14:09 ET (inside the default 14:00-15:30 no-trade window):
    NOT tod-blocked — it trades and carries high_bypassed_gate='tod'."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 14, 9)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_verdict(confidence="HIGH"),
    )

    assert len(calls) == 1
    assert state.auto_trade_count_by_symbol == {"SPY": 1}
    row = await _last_signal()
    assert "skipped_by" not in row.extras
    assert row.extras["high_bypassed_gate"] == "tod"
    assert "[HIGH-BYPASS tod]" in row.reasoning
    assert row.extras["tod_gate"]["in_window"] is True     # window tag still recorded
    assert row.extras["tod_gate"]["skipped"] is False
    assert await SignalIterRepository.query_audit_log(category="tod_gate") == []


async def test_low_fire_still_tod_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 14, 9)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0, verdict_dict=_verdict("LONG_PUTS", confidence="LOW"),
    )

    assert calls == []
    row = await _last_signal()
    assert row.extras["skipped_by"] == "tod_gate"
    assert "high_bypassed_gate" not in row.extras
    assert len(await SignalIterRepository.query_audit_log(category="tod_gate")) == 1


# ---------------------------------------------------------------------------
# 4. FOMC gate — UNCHANGED: binds on ALL tiers (including HIGH)
# ---------------------------------------------------------------------------


async def test_high_fire_still_fomc_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FOMC-stays-on-HIGH: a HIGH fire on an FOMC morning (10:30 ET,
    pre-statement) is STILL fomc-blocked — the FOMC gate is not in the bypass
    set. Skip row + fomc_gate audit + FOMC_GATE_SKIP, NO high_bypassed_gate
    tag (the fire is skipped, so there is no bypass to grade)."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, FOMC_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0, verdict_dict=_verdict(confidence="HIGH"),
    )

    assert calls == []
    assert state.auto_trade_count_by_symbol == {}
    row = await _last_signal()
    assert row.extras["skipped_by"] == "fomc_gate"
    assert "high_bypassed_gate" not in row.extras
    assert row.extras["fomc_gate"]["pre_statement"] is True
    assert row.extras["fomc_gate"]["skipped"] is True
    audits = await SignalIterRepository.query_audit_log(category="fomc_gate")
    assert len(audits) == 1
    assert audits[0].block_reason_code == "FOMC_GATE_SKIP"


async def test_fomc_wins_over_bypassed_chop_on_high_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interaction: HIGH fire, compressed tape AND FOMC morning. Chop is
    bypassed (HIGH) but FOMC still binds, so the fire is fomc-skipped — proving
    FOMC re-catches a HIGH fire whose chop would otherwise have won, with NO
    high_bypassed_gate tag (outcome is still a skip, nothing to grade)."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _compressed_assessment())
    _pin_clock(monkeypatch, FOMC_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0, verdict_dict=_verdict(confidence="HIGH"),
    )

    assert calls == []
    row = await _last_signal()
    assert row.extras["skipped_by"] == "fomc_gate"
    assert "high_bypassed_gate" not in row.extras
    assert row.extras["chop_gate"]["skipped"] is False        # chop was bypassed
    assert len(await SignalIterRepository.query_audit_log(category="fomc_gate")) == 1
    assert await SignalIterRepository.query_audit_log(category="chop_gate") == []


# ---------------------------------------------------------------------------
# 5. Clear tape — HIGH fire trades with NO bypass tag (nothing to grade)
# ---------------------------------------------------------------------------


async def test_high_fire_clear_tape_no_bypass_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH fire, expanded regime, bias agrees, non-FOMC day, outside the TOD
    window: no gate would have blocked it under either logic, so it trades
    with NO high_bypassed_gate tag (only the always-on high_bypass_enabled
    flag rides along)."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _expanded_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_verdict(confidence="HIGH"),
    )

    assert len(calls) == 1
    assert state.auto_trade_count_by_symbol == {"SPY": 1}
    row = await _last_signal()
    assert "skipped_by" not in row.extras
    assert "high_bypassed_gate" not in row.extras
    assert "[HIGH-BYPASS" not in row.reasoning
    assert row.extras["high_bypass_enabled"] is True


# ---------------------------------------------------------------------------
# 6. Adversarial-review fixes (2026-07-09): tag corruption (MAJOR-1), floor
#    coupling (MAJOR-2), fail-restrictive config read (MINOR).
# ---------------------------------------------------------------------------


async def test_wall_collapse_bypass_then_floor_block_sets_no_bypass_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAJOR-1 (negative): a wall-collapse HIGH badge on compressed tape BYPASSES
    chop (raw HIGH), reaches maybe_auto_trade, but the floor re-grades the badge
    (missing regrade inputs => LOW, fail-restrictive) and BLOCKS it. The row must
    carry skipped_by='confidence_floor' and NO high_bypassed_gate - the bypass
    tag marks only fires that actually TRADED, so it never co-occurs with a floor
    skip merged onto the same row."""
    import fef_sidecar.config_store as cs
    state = _armed_state(monkeypatch)
    # Floor ARMED at HIGH so the re-graded LOW badge is blocked.
    monkeypatch.setattr(cs, "load_config", lambda: UIConfig(auto_trade_confidence_floor="HIGH"))
    monkeypatch.setattr(cs, "config_file_status", lambda: "ok")
    _patch_assess(monkeypatch, _compressed_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    verdict = {
        "direction": "LONG_CALLS",
        "confidence": "HIGH",                 # hardcoded wall-collapse badge
        "reasoning": "WALL COLLAPSE LONG_CALLS - 7440C wall thinned.",
        "strike_rec": 625.0,
        "t1": 635.0, "t2": 645.0, "t3": 670.0, "stop": 610.0,
        "aligned": True,
        "wall_collapse_trigger": True,        # => 'confluence' trigger + floor re-grade
        "option_pricing": _option_pricing(),
        "suggested_qty": 5,
        # NO delta_price / delta_drift_m / fe_ratio => re-grade LOW (fail-restrictive)
    }

    await handle_actionable_scanner_verdict(state, "SPY", spot=620.0, verdict_dict=verdict)

    assert len(calls) == 1                         # bypassed chop -> reached the floor
    assert state.auto_trade_count_by_symbol == {}  # floor blocked -> did NOT trade
    row = await _last_signal()
    assert row.extras["skipped_by"] == "confidence_floor"
    assert "high_bypassed_gate" not in row.extras  # <- the MAJOR-1 fix
    assert "[FLOOR SKIPPED" in row.reasoning
    assert "[HIGH-BYPASS" not in row.reasoning
    assert len(await SignalIterRepository.query_audit_log(category="confidence_floor")) >= 1
    assert await SignalIterRepository.query_audit_log(category="chop_gate") == []


async def test_traded_bypass_sets_tag_and_no_skipped_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAJOR-1 (positive): a HIGH fire that bypasses chop AND actually TRADES
    carries high_bypassed_gate='chop' with NO skipped_by - the clean
    'bypassed a gate AND traded' population the grading counts. The tag is
    written post-fire, proving the timing move did not drop it."""
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _compressed_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_verdict(confidence="HIGH"),
    )

    assert len(calls) == 1
    assert state.auto_trade_count_by_symbol == {"SPY": 1}   # traded
    row = await _last_signal()
    assert row.extras["high_bypassed_gate"] == "chop"
    assert "skipped_by" not in row.extras
    assert "[HIGH-BYPASS chop]" in row.reasoning


async def test_kill_switch_disables_bypass_high_fire_chop_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAJOR-2: with the floor kill-switch ON (FEF_DISABLE_CONFIDENCE_FLOOR),
    the floor can't catch the regrade gap, so the bypass must be OFF - a HIGH
    fire on compressed tape is chop-blocked (all tiers), never reaching the
    (disabled) floor."""
    monkeypatch.setenv("FEF_DISABLE_CONFIDENCE_FLOOR", "true")
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _compressed_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0, verdict_dict=_verdict(confidence="HIGH"),
    )

    assert calls == []
    assert state.auto_trade_count_by_symbol == {}
    row = await _last_signal()
    assert row.extras["skipped_by"] == "chop_gate"
    assert "high_bypassed_gate" not in row.extras
    assert len(await SignalIterRepository.query_audit_log(category="chop_gate")) == 1


async def test_config_read_failure_disables_bypass_fail_restrictive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MINOR: a gate-config read failure must FAIL RESTRICTIVE for the bypass
    (mirrors the floor's fail-restrictive HIGH default) - high_bypass_enabled
    flips False, so a HIGH fire on compressed tape is chop-blocked, not silently
    opened."""
    def _boom() -> UIConfig:
        raise RuntimeError("config.json unreadable")
    monkeypatch.setattr(mss, "load_config", _boom)
    state = _armed_state(monkeypatch)
    _patch_assess(monkeypatch, _compressed_assessment())
    _pin_clock(monkeypatch, CLEAR_DAY, 10, 30)
    calls = _record_auto_trades(monkeypatch)

    await handle_actionable_scanner_verdict(
        state, "SPX", spot=7450.0, verdict_dict=_verdict(confidence="HIGH"),
    )

    assert calls == []
    row = await _last_signal()
    assert row.extras["skipped_by"] == "chop_gate"
    assert row.extras["high_bypass_enabled"] is False
    assert "high_bypassed_gate" not in row.extras
    assert len(await SignalIterRepository.query_audit_log(category="chop_gate")) == 1
