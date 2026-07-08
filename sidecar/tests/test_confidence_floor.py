"""Confidence floor (ARMED 2026-07-08) + cross-symbol direction cap.

Floor contract under test:
  - ALWAYS ON: LOW/MEDIUM blocked under the default HIGH floor, HIGH passes;
    the floor comes from config_store (persistent, hot-read), never from
    state.auto_trade_confidence_floor.
  - Wall-collapse exclusion: the hardcoded HIGH badge is re-graded through
    the exact ALIGNED formula (fef.classify_confidence + bias demotion) and
    the FLOOR decision uses the recomputed grade.
  - Counterfactual mechanics identical to the scanner gates: signal row
    tagged extras.skipped_by='confidence_floor' + reasoning suffix,
    'confidence_floor' audit row, CONFIDENCE_FLOOR_SKIP code, no cooldown
    or budget consumed.
  - Kill switch FEF_DISABLE_CONFIDENCE_FLOOR: fail-open + ONE loud audit
    row per session.
  - /auto_trade/toggle IGNORES confidence_floor in its body (deprecation
    audit line when the value differs); POST /config changes it
    persistently; GET /auto_trade/status reports the config floor +
    floor_enforced.

Cross-symbol cap contract: same-right fire blocked while a watcher-managed
parent (SUBMITTED/FILLED/PARTIAL) is open on ANOTHER index-complex symbol;
opposite right passes; own symbol never triggers it (dormant when only SPX
is enabled); hot-disabled via UIConfig.cross_symbol_cap_enabled.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.config_store import UIConfig, load_config, save_config
from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.db.models import FEFConfidence, FEFDirection, LiveOrder
from fef_sidecar.engine.auto_trader import BlockReason, maybe_auto_trade


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.fixture(autouse=True)
def _live(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "fef_sidecar.engine.live_trading.is_live_trading_enabled", lambda: True
    )


@pytest.fixture(autouse=True)
def _no_kill_switch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FEF_DISABLE_CONFIDENCE_FLOOR", raising=False)


@pytest.fixture(autouse=True)
def _restore_mock_coherent_env():
    """AppState.build() setdefaults FEF_MOCK_COHERENT=true process-wide;
    this file sorts before test_ibkr_mock.py whose default-bars stream test
    hangs in coherent mode — leave the env exactly as found."""
    before = os.environ.get("FEF_MOCK_COHERENT")
    yield
    if before is None:
        os.environ.pop("FEF_MOCK_COHERENT", None)
    elif os.environ.get("FEF_MOCK_COHERENT") != before:
        os.environ["FEF_MOCK_COHERENT"] = before


def _patch_floor(monkeypatch: pytest.MonkeyPatch, floor: str = "HIGH",
                 cap_enabled: bool = True) -> None:
    """Pin the config at the config_store SOURCE — auto_trader's in-function
    import resolves it at call time (never reads ~/.fef-terminal live)."""
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(
        cs, "load_config",
        lambda: UIConfig(
            auto_trade_confidence_floor=floor,
            cross_symbol_cap_enabled=cap_enabled,
        ),
    )


def _state(**kw) -> SimpleNamespace:
    d = dict(
        auto_trade_enabled=True,
        active_symbol="SPX",
        auto_trade_trigger_types=["confluence", "aligned_only"],
        auto_trade_cool_down_sec=300,
        auto_trade_last_at_ms_by_symbol={},
        auto_trade_count_by_symbol={},
        auto_trade_max_per_symbol=2,
        auto_trade_qty_override=0,
        auto_trade_last_at_ms=0,
        auto_trade_count_session=0,
        last_spx_price=7440.0,
        ibkr=SimpleNamespace(),
    )
    d.update(kw)
    return SimpleNamespace(**d)


def _verdict(confidence: str = "LOW", **kw) -> dict:
    """ALIGNED verdict — passes the trigger-type gate; NO option_pricing, so
    a verdict that clears the floor lands on NO_OPTION_PRICING (the clean
    'floor passed' marker, same style as test_phase_gate)."""
    d = {
        "direction": "LONG_CALLS",
        "confidence": confidence,
        "reasoning": f"ALIGNED LONG_CALLS — test ({confidence})",
        "aligned": True,
        "wall_collapse_trigger": False,
        "delta_price": 2.1, "delta_drift_m": 30.0, "fe_ratio": 1.2,
        "drift_bias": "NEUTRAL",
        "strike_rec": 7450.0,
    }
    d.update(kw)
    return d


def _priced_verdict(confidence: str = "HIGH", right: str = "C", **kw) -> dict:
    """Full verdict with option_pricing — reaches the duplicate/cap gates
    and (with place_bracket stubbed) can fire."""
    direction = "LONG_CALLS" if right == "C" else "LONG_PUTS"
    v = _verdict(confidence)
    v.update({
        "direction": direction,
        "reasoning": f"ALIGNED {direction} — test",
        "t1": 7455.0, "t2": 7465.0, "t3": 7490.0, "stop": 7430.0,
        "option_pricing": {
            "contract_strike": 7450.0, "right": right, "entry_limit": 5.1,
            "delta": 0.4 if right == "C" else -0.4,
            "underlying_price_at_quote": 7440.0,
            "t1": 11.1, "t2": 15.1, "t3": 25.1, "stop": 1.1,
        },
        "suggested_qty": 3,
    })
    v.update(kw)
    return v


def _stub_bracket(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []

    async def fake_place_bracket(ibkr, spec, *, confirm=False, ticket_id=None, **kwargs):
        calls.append(spec)
        return {"ok": True, "parent_order_id": len(calls), "dry_run": True}

    import fef_sidecar.engine.live_trading as lt
    monkeypatch.setattr(lt, "place_bracket", fake_place_bracket)
    return calls


async def _floor_audit_rows():
    return await SignalIterRepository.query_audit_log(category="confidence_floor")


async def _write_signal(reasoning: str = "ALIGNED LONG_CALLS — test (LOW)"):
    return await SignalIterRepository.write_iter(
        spx_price=7440.0,
        direction=FEFDirection.LONG_CALLS,
        confidence=FEFConfidence.LOW,
        reasoning=reasoning,
        drift_cum_net_m=30.0, drift_bias="NEUTRAL",
        ntm_max_pain=7440.0, raw_max_pain=7440.0,
        top_gex_walls=[], top_dex_walls=[],
    )


# ---------------------------------------------------------------------------
# 1. Floor gate — block/pass matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("conf", ["LOW", "MEDIUM"])
async def test_floor_high_blocks_low_and_medium(
    monkeypatch: pytest.MonkeyPatch, conf: str,
) -> None:
    _patch_floor(monkeypatch, "HIGH")
    d = await maybe_auto_trade(_state(), _verdict(conf))
    assert not d.fired
    assert d.reason_code == BlockReason.CONFIDENCE_FLOOR_SKIP
    assert f"{conf} below floor HIGH" in d.reason


async def test_floor_high_passes_high(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_floor(monkeypatch, "HIGH")
    d = await maybe_auto_trade(_state(), _verdict("HIGH"))
    # Past the floor — lands on the option-pricing gate.
    assert d.reason_code == BlockReason.NO_OPTION_PRICING


async def test_floor_medium_blocks_low_passes_medium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_floor(monkeypatch, "MEDIUM")
    low = await maybe_auto_trade(_state(), _verdict("LOW"))
    assert low.reason_code == BlockReason.CONFIDENCE_FLOOR_SKIP
    med = await maybe_auto_trade(_state(), _verdict("MEDIUM"))
    assert med.reason_code == BlockReason.NO_OPTION_PRICING


async def test_floor_ignores_state_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """The panel-stomp regression: an in-memory state floor of LOW must NOT
    open the gate — enforcement reads config_store only."""
    _patch_floor(monkeypatch, "HIGH")
    state = _state(auto_trade_confidence_floor="LOW")  # stomped in memory
    d = await maybe_auto_trade(state, _verdict("LOW"))
    assert d.reason_code == BlockReason.CONFIDENCE_FLOOR_SKIP


async def test_floor_config_read_failure_fails_restrictive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fef_sidecar.config_store as cs

    def boom():
        raise RuntimeError("corrupt config")

    monkeypatch.setattr(cs, "load_config", boom)
    d = await maybe_auto_trade(_state(), _verdict("MEDIUM"))
    assert d.reason_code == BlockReason.CONFIDENCE_FLOOR_SKIP
    assert "below floor HIGH" in d.reason


# ---------------------------------------------------------------------------
# 2. Counterfactual mechanics (scanner-gate contract)
# ---------------------------------------------------------------------------


async def test_floor_skip_writes_full_counterfactual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_floor(monkeypatch, "HIGH")
    state = _state()
    row = await _write_signal()

    d = await maybe_auto_trade(state, _verdict("LOW"), signal_iter_id=row.id)
    assert d.reason_code == BlockReason.CONFIDENCE_FLOOR_SKIP

    # Signal row keeps the counterfactual visible (scanner-gate contract).
    fresh = (await SignalIterRepository.recent(limit=5))[0]
    assert fresh.id == row.id
    assert fresh.reasoning.endswith("[FLOOR SKIPPED: LOW < HIGH]")
    assert fresh.extras["skipped_by"] == "confidence_floor"

    # Dedicated audit category + structured code.
    rows = await _floor_audit_rows()
    assert len(rows) == 1
    assert rows[0].block_reason_code == BlockReason.CONFIDENCE_FLOOR_SKIP
    assert rows[0].details["stored_confidence"] == "LOW"
    assert rows[0].details["effective_confidence"] == "LOW"
    assert rows[0].details["floor"] == "HIGH"
    assert rows[0].details["signal_iter_id"] == row.id

    # No cooldown consumed, no budget spent.
    assert state.auto_trade_last_at_ms_by_symbol == {}
    assert state.auto_trade_count_by_symbol == {}
    assert state.auto_trade_count_session == 0


async def test_floor_skip_without_signal_id_still_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_floor(monkeypatch, "HIGH")
    d = await maybe_auto_trade(_state(), _verdict("LOW"))  # no signal_iter_id
    assert d.reason_code == BlockReason.CONFIDENCE_FLOOR_SKIP
    assert len(await _floor_audit_rows()) == 1


# ---------------------------------------------------------------------------
# 3. Wall-collapse exclusion — the unearned HIGH badge is re-graded
# ---------------------------------------------------------------------------


def _wc_verdict(dp: float, dd: float, fe: float, bias: str = "NEUTRAL") -> dict:
    """Wall-collapse verdict — fef.py stamps these HIGH regardless of the
    formula. trigger_type resolves to 'confluence' (wall + aligned)."""
    return _verdict(
        "HIGH",
        wall_collapse_trigger=True,
        delta_price=dp, delta_drift_m=dd, fe_ratio=fe,
        drift_bias=bias,
        reasoning="WALL COLLAPSE + ALIGNED LONG_CALLS — test",
    )


async def test_wall_collapse_high_regraded_and_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dp=1.5 fails even the MEDIUM band -> formula LOW -> blocked at HIGH
    floor even though the stored badge says HIGH."""
    _patch_floor(monkeypatch, "HIGH")
    row = await _write_signal("WALL COLLAPSE + ALIGNED LONG_CALLS — test")
    d = await maybe_auto_trade(
        _state(), _wc_verdict(dp=1.5, dd=20.0, fe=2.0), signal_iter_id=row.id,
    )
    assert d.reason_code == BlockReason.CONFIDENCE_FLOOR_SKIP
    assert "wall-collapse re-graded" in d.reason
    rows = await _floor_audit_rows()
    assert len(rows) == 1
    assert rows[0].details["stored_confidence"] == "HIGH"
    assert rows[0].details["effective_confidence"] == "LOW"
    assert rows[0].details["wall_collapse_regraded"] is True
    fresh = (await SignalIterRepository.recent(limit=5))[0]
    assert fresh.reasoning.endswith("[FLOOR SKIPPED: LOW < HIGH]")
    assert fresh.extras["skipped_by"] == "confidence_floor"


async def test_wall_collapse_medium_formula_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dp=3 clears MEDIUM but not HIGH -> formula MEDIUM -> blocked."""
    _patch_floor(monkeypatch, "HIGH")
    d = await maybe_auto_trade(_state(), _wc_verdict(dp=3.0, dd=8.0, fe=2.0))
    assert d.reason_code == BlockReason.CONFIDENCE_FLOOR_SKIP
    assert "MEDIUM below floor HIGH" in d.reason


async def test_wall_collapse_earning_high_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dp=6/dd=12/fe=2 with agreeing bias IS formula-HIGH -> passes the
    floor; no re-grade divergence note (formula == stored badge)."""
    _patch_floor(monkeypatch, "HIGH")
    d = await maybe_auto_trade(_state(), _wc_verdict(dp=6.0, dd=12.0, fe=2.0))
    assert d.reason_code == BlockReason.NO_OPTION_PRICING
    assert await _floor_audit_rows() == []


async def test_wall_collapse_regrade_applies_bias_demotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Formula-HIGH magnitudes + disagreeing bias -> demoted MEDIUM (the
    exact fef.py demotion) -> blocked at HIGH floor."""
    _patch_floor(monkeypatch, "HIGH")
    d = await maybe_auto_trade(
        _state(), _wc_verdict(dp=6.0, dd=12.0, fe=2.0, bias="BEARISH"),
    )
    assert d.reason_code == BlockReason.CONFIDENCE_FLOOR_SKIP
    assert "MEDIUM below floor HIGH" in d.reason


async def test_wall_collapse_missing_inputs_fail_restrictive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_floor(monkeypatch, "HIGH")
    v = _wc_verdict(dp=6.0, dd=12.0, fe=2.0)
    v["delta_price"] = None
    d = await maybe_auto_trade(_state(), v)
    assert d.reason_code == BlockReason.CONFIDENCE_FLOOR_SKIP
    assert "LOW below floor HIGH" in d.reason


async def test_wall_collapse_regrade_matches_engine_formula() -> None:
    """import/reuse pin: the gate's re-grade IS fef.py's classifier +
    demotion — a drift in either helper breaks this equivalence check."""
    from fef_sidecar.engine.fef import (
        FEFEngine,
        classify_confidence,
        demote_confidence_on_bias,
        formula_confidence,
    )

    eng = FEFEngine()
    for dp, dd, fe in ((1.5, 20.0, 2.0), (3.0, 8.0, 2.0), (6.0, 12.0, 2.0),
                       (6.0, 12.0, 7.0), (0.5, 1.0, 0.2)):
        assert eng._classify_confidence(dp, dd, fe) == classify_confidence(dp, dd, fe)
        for bias in ("NEUTRAL", "BULLISH", "BEARISH"):
            expected, _ = demote_confidence_on_bias(
                classify_confidence(dp, dd, fe), FEFDirection.LONG_CALLS, bias,
            )
            got = formula_confidence(
                dp, dd, fe, direction="LONG_CALLS", drift_bias=bias,
            )
            assert got == expected


# ---------------------------------------------------------------------------
# 4. Kill switch — fail-open, loud once per session
# ---------------------------------------------------------------------------


async def test_kill_switch_fails_open_with_one_audit_per_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_floor(monkeypatch, "HIGH")
    monkeypatch.setenv("FEF_DISABLE_CONFIDENCE_FLOOR", "true")
    state = _state()

    d1 = await maybe_auto_trade(state, _verdict("LOW"))
    d2 = await maybe_auto_trade(state, _verdict("LOW"))
    # Gate off -> LOW sails past the floor to the pricing gate.
    assert d1.reason_code == BlockReason.NO_OPTION_PRICING
    assert d2.reason_code == BlockReason.NO_OPTION_PRICING

    rows = await _floor_audit_rows()
    assert len(rows) == 1  # loud, but once per session
    assert "DISABLED" in rows[0].summary
    assert "FEF_DISABLE_CONFIDENCE_FLOOR" in rows[0].summary


# ---------------------------------------------------------------------------
# 5. Cross-symbol direction cap
# ---------------------------------------------------------------------------


async def _open_parent(symbol: str, right: str, *, strike: float = 620.0,
                       status: str = "SUBMITTED",
                       exit_strategy: str = "spx_points") -> LiveOrder:
    return await SignalIterRepository.write_live_order(LiveOrder(
        symbol=symbol, strike=strike, right=right, expiry="20260708",
        action="BUY", qty=3, order_type="MKT", limit_price=5.0,
        bracket_role="parent", status=status, exit_strategy=exit_strategy,
        dry_run=False,
    ))


async def test_cap_blocks_same_right_cross_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_floor(monkeypatch, "LOW")  # isolate the cap from the floor
    calls = _stub_bracket(monkeypatch)
    open_p = await _open_parent("QQQ", "C")
    state = _state()
    row = await _write_signal()

    d = await maybe_auto_trade(
        state, _priced_verdict(right="C"), signal_iter_id=row.id,
    )
    assert not d.fired
    assert d.reason_code == BlockReason.CROSS_SYMBOL_CAP_SKIP
    assert calls == []  # nothing placed

    # Counterfactual row + dedicated audit category.
    fresh = (await SignalIterRepository.recent(limit=5))[0]
    assert fresh.extras["skipped_by"] == "cross_symbol_cap"
    assert "[CROSS-SYMBOL CAP SKIPPED: QQQ 620C open]" in fresh.reasoning
    rows = await SignalIterRepository.query_audit_log(category="cross_symbol_cap")
    assert len(rows) == 1
    assert rows[0].block_reason_code == BlockReason.CROSS_SYMBOL_CAP_SKIP
    assert rows[0].details["open_parent_id"] == open_p.id
    assert rows[0].details["open_parent_symbol"] == "QQQ"

    # No cooldown consumed, no budget spent.
    assert state.auto_trade_last_at_ms_by_symbol == {}
    assert state.auto_trade_count_by_symbol == {}


async def test_cap_allows_opposite_right(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_floor(monkeypatch, "LOW")
    calls = _stub_bracket(monkeypatch)
    await _open_parent("QQQ", "P")
    state = _state()

    d = await maybe_auto_trade(state, _priced_verdict(right="C"))
    assert d.fired
    assert len(calls) == 1
    # Fires DO consume the cooldown/budget (the blocks above must not).
    assert state.auto_trade_count_by_symbol.get("SPX") == 1
    assert state.auto_trade_last_at_ms_by_symbol.get("SPX", 0) > 0


async def test_cap_ignores_own_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dormant-when-only-SPX property: an open SPX parent never cap-blocks
    another SPX fire (per-symbol cooldown/cap + duplicate-parent guard own
    same-symbol stacking)."""
    _patch_floor(monkeypatch, "LOW")
    calls = _stub_bracket(monkeypatch)
    await _open_parent("SPX", "C", strike=7400.0)  # different strike
    state = _state()

    d = await maybe_auto_trade(state, _priced_verdict(right="C"))
    assert d.fired and len(calls) == 1


async def test_cap_ignores_resolved_and_non_watcher_parents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_floor(monkeypatch, "LOW")
    calls = _stub_bracket(monkeypatch)
    await _open_parent("QQQ", "C", status="CLOSED")           # resolved
    await _open_parent("SPY", "C", status="STOPPED")          # resolved
    await _open_parent("IWM", "C", status="DRY_RUN")          # dry-run
    await _open_parent("QQQ", "C", exit_strategy="option_price")  # not watcher-managed
    d = await maybe_auto_trade(_state(), _priced_verdict(right="C"))
    assert d.fired and len(calls) == 1


async def test_cap_disabled_is_dormant(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_floor(monkeypatch, "LOW", cap_enabled=False)
    calls = _stub_bracket(monkeypatch)
    await _open_parent("QQQ", "C")
    d = await maybe_auto_trade(_state(), _priced_verdict(right="C"))
    assert d.fired and len(calls) == 1
    assert await SignalIterRepository.query_audit_log(category="cross_symbol_cap") == []


async def test_cap_put_side_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_floor(monkeypatch, "LOW")
    calls = _stub_bracket(monkeypatch)
    await _open_parent("SPY", "P")
    d = await maybe_auto_trade(_state(), _priced_verdict(right="P"))
    assert not d.fired
    assert d.reason_code == BlockReason.CROSS_SYMBOL_CAP_SKIP
    assert calls == []


# ---------------------------------------------------------------------------
# 6. Endpoints — toggle ignores floor; /config persists it; status reports it
# ---------------------------------------------------------------------------


def _fake_request(state) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fef=state)))


def _toggle_state() -> SimpleNamespace:
    return _state(
        auto_trade_enabled=False,
        auto_trade_confidence_floor="HIGH",  # legacy display field, untouched
    )


async def test_toggle_ignores_floor_field_and_logs_deprecation(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")  # default HIGH
    from fef_sidecar.api import auto_trade_toggle

    state = _toggle_state()
    out = await auto_trade_toggle(
        _fake_request(state), body={"enabled": False, "confidence_floor": "LOW"},
    )
    # Response reports the ENFORCED (config) floor, not the requested one.
    assert out["confidence_floor"] == "HIGH"
    assert state.auto_trade_confidence_floor == "HIGH"  # state not stomped
    rows = await _floor_audit_rows()
    assert len(rows) == 1
    assert "DEPRECATED" in rows[0].summary and "IGNORED" in rows[0].summary
    assert rows[0].details == {"requested": "LOW", "config_floor": "HIGH"}

    # Same value as config -> no deprecation noise.
    await auto_trade_toggle(
        _fake_request(state), body={"enabled": False, "confidence_floor": "HIGH"},
    )
    assert len(await _floor_audit_rows()) == 1


async def test_config_post_changes_floor_persistently(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    from fef_sidecar.api import update_config
    from fef_sidecar.state import AppState

    state = AppState.build()
    out = await update_config(
        _fake_request(state), updates={"auto_trade_confidence_floor": "MEDIUM"},
    )
    assert out["config"]["auto_trade_confidence_floor"] == "MEDIUM"
    # Persisted: a fresh read (what the gate hot-reads) sees MEDIUM.
    assert load_config().auto_trade_confidence_floor == "MEDIUM"
    # And the gate itself honors it (LOW blocked, MEDIUM passes).
    low = await maybe_auto_trade(_state(), _verdict("LOW"))
    assert low.reason_code == BlockReason.CONFIDENCE_FLOOR_SKIP
    med = await maybe_auto_trade(_state(), _verdict("MEDIUM"))
    assert med.reason_code == BlockReason.NO_OPTION_PRICING


async def test_config_rejects_invalid_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    import pydantic
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    from fef_sidecar.api import update_config
    from fef_sidecar.state import AppState

    state = AppState.build()
    with pytest.raises(pydantic.ValidationError):
        await update_config(
            _fake_request(state), updates={"auto_trade_confidence_floor": "SUPER"},
        )
    # Nothing persisted.
    assert load_config().auto_trade_confidence_floor == "HIGH"


async def test_status_reports_config_floor_and_enforcement(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    save_config(UIConfig(auto_trade_confidence_floor="MEDIUM"))
    from fef_sidecar.api import auto_trade_status
    from fef_sidecar.state import AppState

    state = AppState.build()
    state.auto_trade_confidence_floor = "LOW"  # stomped legacy field — ignored
    out = await auto_trade_status(_fake_request(state))
    assert out["confidence_floor"] == "MEDIUM"  # config wins
    assert out["floor_enforced"] is True

    monkeypatch.setenv("FEF_DISABLE_CONFIDENCE_FLOOR", "true")
    out2 = await auto_trade_status(_fake_request(state))
    assert out2["floor_enforced"] is False


# ---------------------------------------------------------------------------
# 7. Ledger + block-reason codes
# ---------------------------------------------------------------------------


async def test_engine_change_ledger_has_floor_entry() -> None:
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import EngineChange
    from fef_sidecar.engine.engine_change_backfill import backfill_engine_changes

    await backfill_engine_changes()
    async with get_session() as session:
        row = (await session.exec(
            select(EngineChange).where(EngineChange.name == "confidence_floor_armed")
        )).first()
    assert row is not None
    assert row.category == "gate"
    # The study numbers ride in expected_impact for the EOD tier reader.
    assert "37.81" in row.expected_impact and "3.31" in row.expected_impact


def test_new_block_reason_codes_exist() -> None:
    assert BlockReason.CONFIDENCE_FLOOR_SKIP == "CONFIDENCE_FLOOR_SKIP"
    assert BlockReason.CROSS_SYMBOL_CAP_SKIP == "CROSS_SYMBOL_CAP_SKIP"
