"""Scanner root-cause fixes + end-to-end trade-source differentiation.

Covers the 2026-07-02 findings:
  - scanner engines must come from the canonical factory (flow-surge gate
    env default honored; scan-interval threshold scaling)
  - scanner evaluations must be visible in signal history
  - every trade carries its signal source end-to-end (signal row + audit
    order line + LiveOrder parent) for scanner vs pattern vs manual
  - near-simultaneous fires on one symbol can no longer double-place
  - /auto_trade/status surfaces scanner activity; scanner config changes
    are audit-logged
"""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.config_store import UIConfig
from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.db.models import LiveOrder
from fef_sidecar.engine.multi_symbol_scanner import (
    _make_engine_for,
    handle_actionable_scanner_verdict,
)
from fef_sidecar.state import AppState, _make_engine

_MIN = 60_000


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.fixture(autouse=True)
def _deterministic_config(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Never read the live ~/.fef-terminal/config.json from tests. Floor
    pinned LOW at the config_store source (auto_trader imports lazily) —
    this suite's LOW-confidence verdicts test provenance, not the
    2026-07-08 always-on confidence floor (test_confidence_floor.py owns
    that). CONFIG_PATH also redirected: api.py bound the original
    load_config at import, and /auto_trade/status now hot-reads the floor
    through it."""
    import fef_sidecar.config_store as cs
    from fef_sidecar.engine import pattern_trigger as pt
    monkeypatch.setattr(pt, "load_config", lambda: UIConfig())
    monkeypatch.setattr(
        cs, "load_config",
        lambda: UIConfig(auto_trade_confidence_floor="LOW"),
    )
    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cs, "config_file_status", lambda: "ok")


@pytest.fixture(autouse=True)
def _pin_scanner_clock(monkeypatch: pytest.MonkeyPatch):
    """Pin the FOMC/TOD gate clock to a clear morning (not an FOMC day,
    outside the 14:00-15:30 no-trade window) so provenance tests trade
    deterministically regardless of when the suite runs."""
    from fef_sidecar.engine import multi_symbol_scanner as _mss
    monkeypatch.setattr(_mss, "_now_et_parts", lambda now_ms=None: ("2026-07-07", 11 * 60))


def _armed_state(monkeypatch: pytest.MonkeyPatch) -> AppState:
    monkeypatch.setenv("FEF_LIVE_TRADING", "true")
    s = AppState.build()
    s.active_symbol = "SPX"
    s.auto_trade_enabled = True
    s.auto_trade_trigger_types = [
        "confluence", "aligned_only",
        "flag_bull", "flag_bear", "pattern_bull", "pattern_bear",
    ]
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


def _scanner_verdict(direction: str = "LONG_CALLS") -> dict:
    return {
        "direction": direction,
        "confidence": "LOW",
        "reasoning": "ALIGNED " + direction + " — dPrice/dDrift in agreement",
        "strike_rec": 625.0,
        "t1": 635.0, "t2": 645.0, "t3": 670.0, "stop": 610.0,
        "aligned": True,
        "wall_collapse_trigger": False,
        "option_pricing": _option_pricing(),
        "suggested_qty": 5,
        "scanner_symbol": "SPY",
    }


# ---------------------------------------------------------------------------
# 1. Engine construction unification
# ---------------------------------------------------------------------------

def test_scanner_is_the_one_canonical_flow_surge_signaler(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONFIG DECISION (2026-07-02): the STRICT scanner is the ONE canonical
    flow-surge signaler; the main engine stays RETIRED. The scanner gate is
    governed EXCLUSIVELY by FEF_SCANNER_FLOW_SURGE_ENABLED (default true =
    today's live behavior), never by the global flag — so enabling/disabling
    either engine can't silently drag the other along."""
    monkeypatch.delenv("FEF_FLOW_SURGE_GATE_ENABLED", raising=False)
    monkeypatch.delenv("FEF_SCANNER_FLOW_SURGE_ENABLED", raising=False)
    # Deploy defaults: scanner ON (canonical signaler), main OFF (retired).
    assert _make_engine_for("SPY")._flow_surge_gate_enabled is True
    assert _make_engine()._flow_surge_gate_enabled is False

    # Scanner has its own kill switch...
    monkeypatch.setenv("FEF_SCANNER_FLOW_SURGE_ENABLED", "false")
    assert _make_engine_for("SPY")._flow_surge_gate_enabled is False

    # ...and the GLOBAL flag does not leak into the scanner in either
    # direction (mixed-threshold populations stay impossible to create
    # by accident).
    monkeypatch.setenv("FEF_FLOW_SURGE_GATE_ENABLED", "true")
    assert _make_engine_for("SPY")._flow_surge_gate_enabled is False  # scanner env wins
    monkeypatch.setenv("FEF_SCANNER_FLOW_SURGE_ENABLED", "true")
    monkeypatch.setenv("FEF_FLOW_SURGE_GATE_ENABLED", "false")
    assert _make_engine_for("SPY")._flow_surge_gate_enabled is True   # scanner env wins


def test_scanner_engine_default_scale_pins_canonical_strictness(monkeypatch: pytest.MonkeyPatch) -> None:
    """CANONICAL CONFIG PIN (not legacy): scanner engines use the
    FEF_LOOP_INTERVAL_SEC scale (120s -> 1.0) at a 60s cadence — ~2x
    stricter per window. Per the 2026-07-02 config decision this strict
    configuration IS the canonical flow-surge signaler being
    forward-tested; a refactor must not silently loosen it."""
    monkeypatch.delenv("FEF_LOOP_INTERVAL_SEC", raising=False)
    monkeypatch.delenv("FEF_SCAN_THRESHOLD_SCALE_SEC", raising=False)
    monkeypatch.setenv("FEF_SCAN_INTERVAL_SEC", "60")  # cadence alone must NOT change scale
    eng = _make_engine_for("QQQ")
    assert eng._threshold_scale == pytest.approx(1.0)  # today's strictness preserved
    # main-loop factory identical
    assert _make_engine()._threshold_scale == pytest.approx(1.0)


def test_scanner_engine_cadence_matched_scale_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEF_LOOP_INTERVAL_SEC", raising=False)
    monkeypatch.setenv("FEF_SCAN_THRESHOLD_SCALE_SEC", "60")
    eng = _make_engine_for("QQQ")
    assert eng._threshold_scale == pytest.approx(0.5)  # explicit opt-in only


def test_scanner_engine_carries_drift_sanity_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEF_DRIFT_SANITY_CEILING_M", "123.0")
    eng = _make_engine_for("SPX")
    assert eng._drift_sanity_ceiling_m == pytest.approx(123.0)


# ---------------------------------------------------------------------------
# 2 + 3. Scanner persistence + end-to-end source differentiation
# ---------------------------------------------------------------------------

async def _parents_by_source() -> dict[str, LiveOrder]:
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    async with get_session() as session:
        rows = (await session.exec(
            select(LiveOrder).where(LiveOrder.bracket_role == "parent")
        )).all()
    out: dict[str, LiveOrder] = {}
    for r in rows:
        for src in ("auto_scanner", "auto_pattern", "auto_main", "manual_ticket"):
            if f"source={src};" in (r.notes or ""):
                out[src] = r
    return out


async def test_scanner_and_pattern_fires_carry_distinct_source_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dual-source deploy requirement: a scanner fire and a pattern fire in
    the same session must be unambiguously attributable at every layer —
    persisted signal row, audit order line, LiveOrder parent. Uses the REAL
    place_bracket (mock provider -> dry-run parent rows)."""
    state = _armed_state(monkeypatch)

    # --- scanner fire on SPY ---
    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )

    # --- pattern fire on SPX (direct trade path with a synthetic flag hit) ---
    from fef_sidecar.engine.pattern_overlay import PatternHit
    from fef_sidecar.engine.pattern_trigger import _maybe_trade_fire

    async def fake_option_quote(contract):
        from datetime import datetime
        from fef_sidecar.ibkr.models import OptionQuote
        return OptionQuote(
            timestamp=datetime.now(), contract=contract,
            last=5.0, bid=4.9, ask=5.1, delta=0.4, underlying_price=7448.0,
        )
    monkeypatch.setattr(state.ibkr, "get_option_quote", fake_option_quote)
    hit = PatternHit(
        index=25, timestamp_ms=int(time.time() * 1000) - 5 * _MIN,
        kind="flag_bull", direction="BULL", label="BULL FLAG > 7460.0",
        price=7448.0, target=7460.0,
        zone={"from_ms": 0, "to_ms": 1, "top": 7449.0, "bottom": 7442.0},
    )
    await _maybe_trade_fire(state, hit, "1m", "SPX", UIConfig())

    # Signal rows: one per source, each self-describing
    signals = await SignalIterRepository.recent(limit=10)
    scanner_rows = [r for r in signals if r.reasoning.startswith("[SCANNER SPY]")]
    pattern_rows = [r for r in signals if r.reasoning.startswith("PATTERN:")]
    assert len(scanner_rows) == 1 and len(pattern_rows) == 1
    assert scanner_rows[0].extras["source"] == "scanner"
    assert scanner_rows[0].extras["scanner_symbol"] == "SPY"
    assert pattern_rows[0].extras["source"] == "pattern"
    assert pattern_rows[0].extras["trigger_type"] == "flag_bull"

    # Audit order lines: prefix + details.source distinguish the two
    orders = await SignalIterRepository.query_audit_log(category="order")
    assert len(orders) == 2
    by_src = {o.details.get("source"): o for o in orders}
    assert set(by_src) == {"auto_scanner", "auto_pattern"}
    assert by_src["auto_scanner"].summary.startswith("AUTO-TRADE[auto_scanner] ")
    assert by_src["auto_pattern"].summary.startswith("AUTO-TRADE[auto_pattern] ")
    assert by_src["auto_scanner"].actor == "system"
    assert by_src["auto_scanner"].details["signal_ref"]["trigger_type"] == "aligned_only"
    assert by_src["auto_pattern"].details["signal_ref"]["trigger_type"] == "flag_bull"

    # LiveOrder parents: notes stamp makes per-source P&L a plain filter
    parents = await _parents_by_source()
    assert set(parents) == {"auto_scanner", "auto_pattern"}
    assert parents["auto_scanner"].symbol == "SPY"
    assert parents["auto_pattern"].symbol == "SPX"

    # Both fires consumed their own per-symbol budget (shared-gate model)
    assert state.auto_trade_count_by_symbol == {"SPY": 1, "SPX": 1}


async def test_scanner_block_is_visible_and_attributed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocked scanner fires must update last_block_reason and write an
    attributed auto_trade_block audit row (previously: silent)."""
    state = _armed_state(monkeypatch)
    # Force a cool-down block: pretend SPY fired 10s ago.
    state.auto_trade_last_at_ms_by_symbol["SPY"] = int(time.time() * 1000) - 10_000
    await handle_actionable_scanner_verdict(
        state, "SPY", spot=620.0, verdict_dict=_scanner_verdict(),
    )
    assert "cool-down" in state.auto_trade_last_block_reason
    assert state.auto_trade_last_block_reason.startswith("[scanner SPY]")
    blocks = await SignalIterRepository.query_audit_log(category="auto_trade_block")
    assert len(blocks) == 1
    assert blocks[0].details["source"] == "auto_scanner"
    assert blocks[0].details["reason_code"] == "COOL_DOWN"
    # The evaluation is still in signal history even though blocked
    signals = await SignalIterRepository.recent(limit=10)
    assert any(r.reasoning.startswith("[SCANNER SPY]") for r in signals)


async def test_scanner_wait_verdicts_not_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _armed_state(monkeypatch)
    v = _scanner_verdict("WAIT")
    v["direction"] = "WAIT"
    await handle_actionable_scanner_verdict(state, "SPY", spot=620.0, verdict_dict=v)
    assert await SignalIterRepository.recent(limit=10) == []


# ---------------------------------------------------------------------------
# 4. Duplicate-fire race guard
# ---------------------------------------------------------------------------

async def test_concurrent_fires_same_symbol_place_exactly_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7/1 regression: two QQQ 720P parents 16ms apart. Two concurrent
    maybe_auto_trade calls for the same symbol must serialize on the
    per-symbol lock — the second sees the first's cool-down stamp."""
    from fef_sidecar.engine.auto_trader import maybe_auto_trade

    state = _armed_state(monkeypatch)
    calls: list = []

    async def slow_place_bracket(ibkr, spec, *, confirm=False, ticket_id=None, **kwargs):
        await asyncio.sleep(0.05)  # widen the race window
        calls.append(spec)
        return {"ok": True, "parent_order_id": len(calls), "dry_run": True}

    import fef_sidecar.engine.live_trading as lt
    monkeypatch.setattr(lt, "place_bracket", slow_place_bracket)

    v1 = _scanner_verdict()
    v2 = _scanner_verdict()
    d1, d2 = await asyncio.gather(
        maybe_auto_trade(state, v1, symbol="QQQ", source="auto_scanner"),
        maybe_auto_trade(state, v2, symbol="QQQ", source="auto_scanner"),
    )
    fired = [d for d in (d1, d2) if d.fired]
    blocked = [d for d in (d1, d2) if not d.fired]
    assert len(fired) == 1 and len(blocked) == 1
    assert blocked[0].reason_code == "COOL_DOWN"
    assert len(calls) == 1
    assert state.auto_trade_count_by_symbol.get("QQQ") == 1


async def test_scanner_spx_fire_blocks_main_loop_double_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No SPX double-coverage: with the main engine's flow-surge gate
    RETIRED, the main loop shouldn't even produce an actionable flow-surge
    verdict — but even if one appeared, the per-symbol lock + shared
    cool-down guarantee a scanner SPX fire blocks a main-loop place."""
    from fef_sidecar.engine.auto_trader import maybe_auto_trade

    # Engine-level guarantee: main + shadow gates are OFF at deploy defaults.
    monkeypatch.delenv("FEF_FLOW_SURGE_GATE_ENABLED", raising=False)
    assert _make_engine()._flow_surge_gate_enabled is False

    state = _armed_state(monkeypatch)
    # Scanner fires SPX first...
    v = _scanner_verdict()
    d1 = await maybe_auto_trade(state, v, symbol="SPX", source="auto_scanner")
    assert d1.fired
    # ...then the main loop path evaluates an actionable SPX verdict.
    d2 = await maybe_auto_trade(
        state, _scanner_verdict(), symbol="SPX", source="auto_main",
    )
    assert not d2.fired and d2.reason_code == "COOL_DOWN"
    assert state.auto_trade_count_by_symbol.get("SPX") == 1
    parents = await _parents_by_source()
    assert set(parents) == {"auto_scanner"}  # exactly one parent, scanner's


async def test_pattern_trigger_is_flow_surge_gate_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pattern trigger never consults the flow-surge gate: with BOTH
    the global flag unset (main retired) and the scanner's own enable
    forced off, a pattern fire still trades."""
    monkeypatch.delenv("FEF_FLOW_SURGE_GATE_ENABLED", raising=False)
    monkeypatch.setenv("FEF_SCANNER_FLOW_SURGE_ENABLED", "false")
    state = _armed_state(monkeypatch)

    from fef_sidecar.engine.pattern_overlay import PatternHit
    from fef_sidecar.engine.pattern_trigger import _maybe_trade_fire

    async def fake_option_quote(contract):
        from datetime import datetime
        from fef_sidecar.ibkr.models import OptionQuote
        return OptionQuote(
            timestamp=datetime.now(), contract=contract,
            last=5.0, bid=4.9, ask=5.1, delta=0.4, underlying_price=7448.0,
        )
    monkeypatch.setattr(state.ibkr, "get_option_quote", fake_option_quote)
    hit = PatternHit(
        index=25, timestamp_ms=int(time.time() * 1000) - 5 * _MIN,
        kind="flag_bull", direction="BULL", label="BULL FLAG > 7460.0",
        price=7448.0, target=7460.0,
    )
    await _maybe_trade_fire(state, hit, "1m", "SPX", UIConfig())
    parents = await _parents_by_source()
    assert set(parents) == {"auto_pattern"}
    assert state.auto_trade_count_by_symbol.get("SPX") == 1


# ---------------------------------------------------------------------------
# 5. Status truth + config audit
# ---------------------------------------------------------------------------

def _fake_request(state: AppState):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fef=state)))


async def test_auto_trade_status_surfaces_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    from fef_sidecar.api import auto_trade_status

    state = _armed_state(monkeypatch)
    state.scan_enabled_symbols["SPY"] = True
    state.auto_trade_count_by_symbol["SPY"] = 2
    state.last_scanner_verdict_by_symbol["SPY"] = {
        "direction": "LONG_CALLS", "confidence": "LOW",
        "reasoning": "x", "spx_price": 620.0, "strike_rec": 625.0,
        "timestamp_ms": 1,
    }
    out = await auto_trade_status(_fake_request(state))
    sc = out["scanner"]
    assert sc["enabled_symbols"] == ["SPY"]
    assert sc["count_by_symbol"]["SPY"] == 2
    assert sc["last_verdict_by_symbol"]["SPY"]["direction"] == "LONG_CALLS"
    assert sc["max_per_symbol"] == state.auto_trade_max_per_symbol


async def test_scanner_config_changes_are_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    from fef_sidecar.api import scanner_symbols_set

    state = _armed_state(monkeypatch)
    out = await scanner_symbols_set(
        _fake_request(state),
        body={"enabled": {"QQQ": True}, "max_per_symbol": 50},
    )
    assert out["ok"] and out["max_per_symbol"] == 50
    rows = await SignalIterRepository.query_audit_log(category="config")
    assert len(rows) == 1
    assert "max_per_symbol 2 -> 50" in rows[0].summary
    assert rows[0].details["enabled_after"]["QQQ"] is True
    # No-op call writes nothing
    await scanner_symbols_set(_fake_request(state), body={"enabled": {"QQQ": True}})
    rows2 = await SignalIterRepository.query_audit_log(category="config")
    assert len(rows2) == 1
