"""Auto-trader phase-detector veto gate (Gate 3b in maybe_auto_trade).

active mode vetoes a trade only when a FRESH, actionable phase verdict
DISAGREES with the engine direction; it fails OPEN otherwise (advisory / off /
agree / stale) so a broken feed can't silently disable auto-trade.
"""
from types import SimpleNamespace

import pytest

from fef_sidecar.engine.auto_trader import BlockReason, maybe_auto_trade


def _state(phase_mode, phase_verdict):
    # Enough attributes to reach Gate 3b, then (when not vetoed) fall through to
    # Gate 3.5 which blocks on trigger-type — a clean "phase did NOT veto" marker.
    return SimpleNamespace(
        auto_trade_enabled=True,
        active_symbol="SPX",
        phase_mode=phase_mode,
        phase_last_verdict=phase_verdict,
        auto_trade_trigger_types=["confluence", "aligned_only"],
    )


def _verdict():
    # direction actionable, but no aligned/wall flags → trigger_type "other".
    return {"direction": "LONG_PUTS"}


def _pv(direction, *, actionable=True, age_ms=0):
    import time
    return {
        "state": "FADE", "direction": direction, "actionable": actionable,
        "score": 4, "threshold": 4, "reasons": ["stall", "bounce"],
        "ts_ms": int(time.time() * 1000) - age_ms,
    }


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    monkeypatch.setattr(
        "fef_sidecar.engine.live_trading.is_live_trading_enabled", lambda: True
    )


async def test_active_vetoes_on_fresh_disagreement():
    # engine wants PUTS, phase says CALLS (fade) → veto.
    d = await maybe_auto_trade(_state("active", _pv("LONG_CALLS")), _verdict())
    assert not d.fired and d.reason_code == BlockReason.PHASE_DISAGREE


async def test_active_allows_when_phase_agrees():
    d = await maybe_auto_trade(_state("active", _pv("LONG_PUTS")), _verdict())
    # Phase agrees → no veto; falls through to the trigger-type gate.
    assert d.reason_code == BlockReason.TRIGGER_NOT_ENABLED


async def test_advisory_never_vetoes():
    d = await maybe_auto_trade(_state("advisory", _pv("LONG_CALLS")), _verdict())
    assert d.reason_code == BlockReason.TRIGGER_NOT_ENABLED


async def test_active_fails_open_on_stale_phase():
    # Disagreeing but 2 minutes old (> 90s freshness) → fail open, no veto.
    d = await maybe_auto_trade(_state("active", _pv("LONG_CALLS", age_ms=120_000)), _verdict())
    assert d.reason_code == BlockReason.TRIGGER_NOT_ENABLED


async def test_active_fails_open_when_phase_not_actionable():
    d = await maybe_auto_trade(_state("active", _pv("LONG_CALLS", actionable=False)), _verdict())
    assert d.reason_code == BlockReason.TRIGGER_NOT_ENABLED
