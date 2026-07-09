"""Persistent auto-trade quantity (FIX 2026-07-09) — the traded contracts-per-
fire is the config_store value, NEVER the in-memory state field, and the
retired `or 15` fallback is gone: a fire that can't resolve a valid size uses
the validated default (3) with a loud once/session audit, never 15.

Near-miss under test (caught pre-open): auto_trade_qty_override lived only in
memory and reset to 0 on restart while floor/symbols persisted; scanner
verdicts carry suggested_qty=None, so `override if >0 else suggested_qty or 15`
sized a fire at 15 contracts — 5x the operator's 3-lot, against which the
-$1,800 cap + every exit geometry are calibrated.

Mirrors the confidence-floor contract (test_confidence_floor.py):
  - the gate reads config_store (persistent, hot), IGNORES state;
  - POST /config persists + validates (0 / >100 rejected) + audits the change;
  - /auto_trade/toggle + presets IGNORE qty_override with a deprecation audit;
  - /auto_trade/status reports the effective config size;
  - the per-tier 30/40/30 split is preserved unchanged.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.config_store import UIConfig, load_config, save_config
from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.engine.auto_trader import _config_qty, _tier_split, maybe_auto_trade
from fef_sidecar.engine.spx_target_watcher import _all_tiers_banked


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
    """AppState.build() setdefaults FEF_MOCK_COHERENT=true process-wide — leave
    the env exactly as found (see test_confidence_floor.py rationale)."""
    before = os.environ.get("FEF_MOCK_COHERENT")
    yield
    if before is None:
        os.environ.pop("FEF_MOCK_COHERENT", None)
    elif os.environ.get("FEF_MOCK_COHERENT") != before:
        os.environ["FEF_MOCK_COHERENT"] = before


def _patch_config(monkeypatch: pytest.MonkeyPatch, *, qty: int = 3,
                  floor: str = "LOW") -> None:
    """Pin config at the config_store SOURCE (auto_trader imports lazily).
    Floor LOW so a priced verdict fires and lands on the qty resolution;
    config_file_status pinned 'ok' so the fallback audit does not trip."""
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(
        cs, "load_config",
        lambda: UIConfig(auto_trade_confidence_floor=floor,
                         auto_trade_qty_override=qty),
    )
    monkeypatch.setattr(cs, "config_file_status", lambda: "ok")


def _state(**kw) -> SimpleNamespace:
    d = dict(
        auto_trade_enabled=True,
        active_symbol="SPX",
        auto_trade_trigger_types=["confluence", "aligned_only"],
        auto_trade_cool_down_sec=300,
        auto_trade_last_at_ms_by_symbol={},
        auto_trade_count_by_symbol={},
        auto_trade_max_per_symbol=5,
        auto_trade_qty_override=0,          # display-legacy; the gate ignores it
        auto_trade_last_at_ms=0,
        auto_trade_count_session=0,
        exit_strategy_long="spx_points",
        last_spx_price=7440.0,
        ibkr=SimpleNamespace(),
    )
    d.update(kw)
    return SimpleNamespace(**d)


def _priced_verdict(**kw) -> dict:
    """HIGH ALIGNED verdict with option_pricing — passes the floor (LOW or the
    fail-restrictive HIGH) and reaches the qty resolution + fire."""
    v = {
        "direction": "LONG_CALLS",
        "confidence": "HIGH",
        "reasoning": "ALIGNED LONG_CALLS — test",
        "aligned": True,
        "wall_collapse_trigger": False,
        "delta_price": 2.1, "delta_drift_m": 30.0, "fe_ratio": 1.2,
        "drift_bias": "NEUTRAL",
        "strike_rec": 7450.0,
        "t1": 7455.0, "t2": 7465.0, "t3": 7490.0, "stop": 7430.0,
        "option_pricing": {
            "contract_strike": 7450.0, "right": "C", "entry_limit": 5.1,
            "delta": 0.4, "underlying_price_at_quote": 7440.0,
            "t1": 11.1, "t2": 15.1, "t3": 25.1, "stop": 1.1,
        },
        # The legacy engine suggestion — MUST be ignored now (scanner verdicts
        # carry None, which the retired `or 15` turned into 15 contracts).
        "suggested_qty": None,
    }
    v.update(kw)
    return v


def _stub_bracket(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []

    async def fake_place_bracket(ibkr, spec, *, confirm=False, **kwargs):
        calls.append(spec)
        return {"ok": True, "parent_order_id": len(calls), "dry_run": True}

    import fef_sidecar.engine.live_trading as lt
    monkeypatch.setattr(lt, "place_bracket", fake_place_bracket)
    return calls


def _fake_request(state) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fef=state)))


async def _qty_audits(needle: str) -> list:
    return [r for r in await SignalIterRepository.query_audit_log(category="auto_trade_qty")
            if needle in r.summary]


# ---------------------------------------------------------------------------
# 1. Config model — round-trip + validation (0 / >100 rejected)
# ---------------------------------------------------------------------------


def test_qty_config_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    assert UIConfig().auto_trade_qty_override == 3          # operator's live size
    save_config(UIConfig(auto_trade_qty_override=7))
    assert load_config().auto_trade_qty_override == 7


def test_config_model_rejects_qty_zero_and_over_100() -> None:
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        UIConfig(auto_trade_qty_override=0)                 # no 'use suggested' sentinel
    with pytest.raises(pydantic.ValidationError):
        UIConfig(auto_trade_qty_override=101)
    assert UIConfig(auto_trade_qty_override=1).auto_trade_qty_override == 1
    assert UIConfig(auto_trade_qty_override=100).auto_trade_qty_override == 100


async def test_config_post_rejects_invalid_qty_nothing_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    import pydantic
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    from fef_sidecar.api import update_config
    from fef_sidecar.state import AppState

    state = AppState.build()
    for bad in (0, 150):
        with pytest.raises(pydantic.ValidationError):
            await update_config(
                _fake_request(state), updates={"auto_trade_qty_override": bad},
            )
    # Nothing persisted -> the validated default (3), never 15.
    assert load_config().auto_trade_qty_override == 3


async def test_config_post_persists_qty_and_audits_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    from fef_sidecar.api import update_config
    from fef_sidecar.state import AppState

    state = AppState.build()
    out = await update_config(
        _fake_request(state), updates={"auto_trade_qty_override": 10},
    )
    assert out["config"]["auto_trade_qty_override"] == 10
    assert load_config().auto_trade_qty_override == 10       # what the gate hot-reads
    # The generic F4 changed-keys audit covers the new key with no bespoke code.
    rows = await SignalIterRepository.query_audit_log(category="config")
    assert len(rows) == 1
    assert "auto_trade_qty_override: 3 -> 10" in rows[0].summary
    assert rows[0].details["changed"]["auto_trade_qty_override"] == {"old": 3, "new": 10}


# ---------------------------------------------------------------------------
# 2. The gate sizes from config — never state, never suggested_qty, never 15
# ---------------------------------------------------------------------------


async def test_gate_sizes_from_config_not_state_or_suggested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact near-miss shape: state override 0 + suggested_qty None. The
    gate must size at the config default (3), NEVER 15."""
    _patch_config(monkeypatch, qty=3)
    calls = _stub_bracket(monkeypatch)
    state = _state(auto_trade_qty_override=0)
    d = await maybe_auto_trade(state, _priced_verdict(suggested_qty=None))
    assert d.fired
    assert calls[0].qty == 3
    # Per-tier 30/40/30 split preserved: qty 3 -> [1, 1, 1].
    assert (calls[0].t1_qty, calls[0].t2_qty, calls[0].t3_qty) == (1, 1, 1)


async def test_gate_ignores_state_qty_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """state.auto_trade_qty_override is display-legacy — a stomped 50 must NOT
    size the trade; the config value wins (the panel-stomp regression)."""
    _patch_config(monkeypatch, qty=4)
    calls = _stub_bracket(monkeypatch)
    state = _state(auto_trade_qty_override=50)
    d = await maybe_auto_trade(state, _priced_verdict())
    assert d.fired
    assert calls[0].qty == 4


async def test_gate_uses_operator_config_size_and_tier_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, qty=10)
    calls = _stub_bracket(monkeypatch)
    d = await maybe_auto_trade(_state(), _priced_verdict())
    assert d.fired and calls[0].qty == 10
    # 30/40/30 of 10 -> 3/4/3 (unchanged split).
    assert (calls[0].t1_qty, calls[0].t2_qty, calls[0].t3_qty) == (3, 4, 3)


# ---------------------------------------------------------------------------
# 3. Fallback — no valid size resolves -> default 3 + loud once/session audit
# ---------------------------------------------------------------------------


async def test_gate_fallback_uses_default_not_15_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_config raising -> the gate uses the validated default (3), NEVER
    15, and audits the fallback. (The floor also fails restrictive to HIGH; a
    HIGH verdict still fires.)"""
    import fef_sidecar.config_store as cs

    def boom():
        raise RuntimeError("corrupt config")

    monkeypatch.setattr(cs, "load_config", boom)
    calls = _stub_bracket(monkeypatch)
    d = await maybe_auto_trade(_state(), _priced_verdict())
    assert d.fired
    assert calls[0].qty == 3
    rows = await _qty_audits("QTY FALLBACK")
    assert len(rows) == 1
    assert "using default 3" in rows[0].summary
    assert rows[0].details["qty"] == 3


async def test_config_qty_fallback_audits_once_per_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fef_sidecar.config_store as cs

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(cs, "load_config", boom)
    state = SimpleNamespace()
    assert await _config_qty(state) == 3
    assert await _config_qty(state) == 3          # session marker suppresses re-audit
    assert len(await _qty_audits("QTY FALLBACK")) == 1


async def test_config_qty_invalid_file_audits_and_uses_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """config.json EXISTS but is invalid -> load_config silently returns the
    default (qty 3); _config_qty SAYS so (QTY FALLBACK audit) and uses 3."""
    import fef_sidecar.config_store as cs
    cfg = tmp_path / "config.json"
    cfg.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(cs, "CONFIG_PATH", cfg)   # REAL load_config + probe

    assert await _config_qty(SimpleNamespace()) == 3
    rows = await _qty_audits("QTY FALLBACK")
    assert len(rows) == 1
    assert "invalid" in rows[0].summary


async def test_config_qty_missing_file_defaults_quietly(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """A MISSING file is a fresh install — default 3 is legitimate and must NOT
    raise the fallback alarm (mirrors the floor's F1 quiet-missing counterpart)."""
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")  # absent
    assert await _config_qty(SimpleNamespace()) == 3
    assert await _qty_audits("QTY FALLBACK") == []


# ---------------------------------------------------------------------------
# 4. Endpoints — status reports config qty; toggle + presets ignore it
# ---------------------------------------------------------------------------


async def test_status_reports_config_qty(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")
    save_config(UIConfig(auto_trade_qty_override=8))
    from fef_sidecar.api import auto_trade_status
    from fef_sidecar.state import AppState

    state = AppState.build()
    state.auto_trade_qty_override = 99            # stomped legacy field — ignored
    out = await auto_trade_status(_fake_request(state))
    assert out["qty_override"] == 8              # config wins, not the state 99


async def test_toggle_ignores_qty_field_and_logs_deprecation(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")  # default qty 3
    from fef_sidecar.api import auto_trade_toggle

    state = _state(auto_trade_enabled=False, auto_trade_qty_override=3)
    await auto_trade_toggle(
        _fake_request(state), body={"enabled": False, "qty_override": 25},
    )
    # Toggle no longer writes the state field; the gate would ignore it anyway.
    assert state.auto_trade_qty_override == 3
    rows = await _qty_audits("DEPRECATED")
    assert len(rows) == 1
    assert "IGNORED" in rows[0].summary
    assert rows[0].details == {"requested": 25, "config_qty": 3}

    # Same value as config -> no deprecation noise.
    await auto_trade_toggle(
        _fake_request(state), body={"enabled": False, "qty_override": 3},
    )
    assert len(await _qty_audits("DEPRECATED")) == 1


async def test_preset_apply_ignores_qty_and_logs_deprecation(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    import fef_sidecar.config_store as cs
    monkeypatch.setattr(cs, "CONFIG_PATH", tmp_path / "config.json")  # default qty 3
    from fef_sidecar.api import apply_auto_trade_preset, create_auto_trade_preset
    from fef_sidecar.state import AppState

    made = await create_auto_trade_preset(body={
        "name": "qty-test", "confidence_floor": "HIGH",  # == config -> no floor dep
        "cool_down_sec": 180, "trigger_types": ["confluence"],
        "qty_override": 12, "runner_protect_enabled": True,
    })
    state = AppState.build()
    out = await apply_auto_trade_preset(made["preset_id"], _fake_request(state))

    dep = await _qty_audits("DEPRECATED")
    assert len(dep) == 1
    assert dep[0].details == {
        "preset_id": made["preset_id"], "requested": 12, "config_qty": 3,
    }
    # MINOR-1: the response echoes the ENFORCED (config) size, not the preset's.
    assert out["settings"]["qty_override"] == 3


# ---------------------------------------------------------------------------
# 5. Tier split (review MAJOR-1) — qty < 3 must not degenerate; the CLOSED-sync
#    still fires; a MAX_CONTRACTS clip into 1/2 is safe.
# ---------------------------------------------------------------------------


def test_tier_split_small_qty_never_degenerates() -> None:
    assert _tier_split(1) == (1, 0, 0)          # was (-1, 1, 1): T1 churn/naked
    assert _tier_split(2) == (1, 1, 0)          # was (0, 1, 1): T1 never fired
    assert _tier_split(3) == (1, 1, 1)
    # qty >= 3 is the unchanged 30/40/30.
    assert _tier_split(4) == (1, 2, 1)
    assert _tier_split(5) == (2, 2, 1)
    assert _tier_split(10) == (3, 4, 3)
    assert _tier_split(20) == (6, 8, 6)
    for q in range(1, 101):
        t = _tier_split(q)
        assert all(x >= 0 for x in t)           # never negative (no -1 churn)
        assert sum(t) == q                      # parts cover the full position
        assert t[0] >= 1                        # T1 always takes the first partial
        nz = [x for x in t if x > 0]
        assert list(t[:len(nz)]) == nz          # zeros only ever trail (T2/T3)


def test_tier_split_closed_sync_fires_for_small_qty() -> None:
    """Each split fully exits and the sold-qty CLOSED-sync recognizes it: the
    NON-ZERO tiers banking cover the whole position — no deadlock on a 0-qty
    tier, no over/under-sell."""
    for q in (1, 2, 3, 5, 10):
        tier_qtys = list(_tier_split(q))
        fired = [x > 0 for x in tier_qtys]      # only non-zero tiers ever fire
        assert _all_tiers_banked(tier_qtys, fired, q) is True
        assert sum(qq for qq, f in zip(tier_qtys, fired) if qq and f) == q
        # A partial (only T1 sold) closes ONLY when T1 == the whole position.
        assert _all_tiers_banked(tier_qtys, [True, False, False], q) is (q == 1)


async def test_gate_qty_2_places_valid_split(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, qty=2)
    calls = _stub_bracket(monkeypatch)
    d = await maybe_auto_trade(_state(), _priced_verdict())
    assert d.fired and calls[0].qty == 2
    assert (calls[0].t1_qty, calls[0].t2_qty, calls[0].t3_qty) == (1, 1, 0)


async def test_max_contracts_clip_to_2_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """FEF_MAX_CONTRACTS_PER_TRADE clipping a larger config size down to 2 must
    still produce the safe (1,1,0), not the old (0,1,1)."""
    _patch_config(monkeypatch, qty=10)
    monkeypatch.setenv("FEF_MAX_CONTRACTS_PER_TRADE", "2")
    calls = _stub_bracket(monkeypatch)
    d = await maybe_auto_trade(_state(), _priced_verdict())
    assert d.fired and calls[0].qty == 2
    assert (calls[0].t1_qty, calls[0].t2_qty, calls[0].t3_qty) == (1, 1, 0)
