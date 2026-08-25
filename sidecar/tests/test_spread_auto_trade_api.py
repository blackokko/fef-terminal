"""GET /spread_auto_trade/status and POST /spread_auto_trade/toggle.

WHY THESE ROUTES EXIST, AND THEREFORE WHAT THESE TESTS PIN:

The credit-spread auto-trader shipped with NO api surface. That produced two
failures the directional bot has never had, both visible on 2026-08-12:

  1. It could only be armed by restarting the process with an env var, so
     `spread_auto_trade_enabled` defaulted True and the module was live the
     instant the process started -- MORE automatic than the bot beside it,
     which requires a per-session UI toggle. The default is now False and this
     toggle is the intent layer; the env var is the capability layer.

  2. Nothing recorded WHY an evaluation did not fire. Answering that took an
     hour of hand-querying signal_log.db, and the answer was that an order our
     OWN gate had refused consumed the day's once-per-session budget.

The contract under test:
  * status reports the env gates read FRESH from the process environment
  * `would_transmit` is the whole gate stack collapsed to one boolean
  * enabling requires confirm=true (anti stale-fetch / replay)
  * enabling is REFUSED when the process lacks the capability, rather than
    producing a bot that runs every gate and is denied at the broker
  * DISABLING always works -- no confirm, no env, no audit dependency
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.api import spread_auto_trade_status, spread_auto_trade_toggle
from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.state import AppState


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Both env gates absent unless a test sets them. Never inherit the
    operator's real shell -- a test that passes only because the developer
    happened to have FEF_ALLOW_CREDIT_SPREADS set is worse than no test."""
    monkeypatch.delenv("FEF_ALLOW_CREDIT_SPREADS", raising=False)
    monkeypatch.delenv("FEF_LIVE_TRADING", raising=False)
    yield


def _mk_state(**over):
    """Stub state, matching the convention in test_spread_auto_trader.py.
    AppState requires 7 collaborators (quantdata/ibkr/engine/...) that none of
    these routes touch, so constructing a real one would test the constructor,
    not the routes."""
    base = dict(
        spread_auto_trade_enabled=False,
        spread_auto_trade_observe_only=False,
        spread_auto_trade_qty=1,
        spread_auto_trade_last_date="",
        spread_auto_trade_count_session=0,
        spread_auto_trade_last_reason="",
        spread_auto_trade_last_reason_code="",
        spread_auto_trade_last_eval_at_ms=0,
        spread_width=10.0,
        spread_delta_max=0.15,
        spread_delta_floor_frac=0.5,
        spread_min_credit_width_ratio=0.05,
        spread_puts_only=True,
        spread_require_gate=False,
        spread_rec_max_age_sec=300.0,
        last_spread_recommendation=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _req(state):
    """Minimal stand-in for the bits of Request the routes touch."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fef=state)))


def _armed_env(monkeypatch):
    monkeypatch.setenv("FEF_ALLOW_CREDIT_SPREADS", "true")
    monkeypatch.setenv("FEF_LIVE_TRADING", "true")


# --------------------------------------------------------------------- default

async def test_default_is_off():
    """The 2026-08-17 flip. If this ever reverts to True, the module is armed
    on process start with no operator action -- the exact asymmetry the toggle
    was built to remove."""
    fld = AppState.__dataclass_fields__["spread_auto_trade_enabled"]
    assert fld.default is False


# ---------------------------------------------------------------------- status

async def test_status_reports_env_gates_fresh(monkeypatch):
    state = _mk_state()
    r = _req(state)

    out = await spread_auto_trade_status(r)
    assert out["credit_spread_env"] is False
    assert out["live_trading_env"] is False

    # Changing the process environment must be visible on the NEXT call. A
    # cached "true" here is the one lie this endpoint must never tell.
    _armed_env(monkeypatch)
    out2 = await spread_auto_trade_status(r)
    assert out2["credit_spread_env"] is True
    assert out2["live_trading_env"] is True


@pytest.mark.parametrize(
    "enabled,observe,env,expected",
    [
        (True, False, True, True),    # everything lines up
        (False, False, True, False),  # toggle off
        (True, True, True, False),    # observe-only sends nothing
        (True, False, False, False),  # no capability
    ],
)
async def test_would_transmit_is_the_whole_stack(monkeypatch, enabled, observe, env, expected):
    """`would_transmit` answers "will a qualifying setup reach the broker right
    now?". It must be false if ANY layer is down, so the UI never has to
    re-derive the gate stack and get it subtly wrong."""
    if env:
        _armed_env(monkeypatch)
    state = _mk_state(
        spread_auto_trade_enabled=enabled, spread_auto_trade_observe_only=observe
    )
    out = await spread_auto_trade_status(_req(state))
    assert out["would_transmit"] is expected


async def test_status_surfaces_last_block_reason():
    """The 2026-08-12 hour-long question, answerable in one GET."""
    state = _mk_state()
    state.spread_auto_trade_last_reason = "already placed a spread on 2026-08-12"
    state.spread_auto_trade_last_reason_code = "already_today"
    state.spread_auto_trade_last_eval_at_ms = 1_755_000_000_000
    out = await spread_auto_trade_status(_req(state))
    assert out["last_reason_code"] == "already_today"
    assert "already placed" in out["last_reason"]
    assert out["last_eval_at_ms"] == 1_755_000_000_000


# ---------------------------------------------------------------------- toggle

async def test_enable_requires_confirm(monkeypatch):
    _armed_env(monkeypatch)
    state = _mk_state()
    with pytest.raises(HTTPException) as ei:
        await spread_auto_trade_toggle(_req(state), body={"enabled": True})
    assert ei.value.status_code == 400
    assert "confirm=true" in str(ei.value.detail)
    assert state.spread_auto_trade_enabled is False


async def test_enable_refused_without_credit_spread_env(monkeypatch):
    """Refuse UP FRONT rather than arming a bot that runs every gate and is
    then denied at the broker call -- that combination is what made 2026-08-12
    hard to diagnose."""
    monkeypatch.setenv("FEF_LIVE_TRADING", "true")  # only the other gate
    state = _mk_state()
    with pytest.raises(HTTPException) as ei:
        await spread_auto_trade_toggle(_req(state), body={"enabled": True, "confirm": True})
    assert ei.value.status_code == 400
    assert "FEF_ALLOW_CREDIT_SPREADS" in str(ei.value.detail)
    assert state.spread_auto_trade_enabled is False


async def test_enable_refused_without_live_trading_env(monkeypatch):
    monkeypatch.setenv("FEF_ALLOW_CREDIT_SPREADS", "true")
    state = _mk_state()
    with pytest.raises(HTTPException) as ei:
        await spread_auto_trade_toggle(_req(state), body={"enabled": True, "confirm": True})
    assert ei.value.status_code == 400
    assert "FEF_LIVE_TRADING" in str(ei.value.detail)
    assert state.spread_auto_trade_enabled is False


async def test_enable_succeeds_when_fully_armed(monkeypatch):
    _armed_env(monkeypatch)
    state = _mk_state()
    out = await spread_auto_trade_toggle(
        _req(state), body={"enabled": True, "confirm": True}
    )
    assert out["ok"] is True
    assert out["enabled"] is True
    assert state.spread_auto_trade_enabled is True


async def test_disable_needs_neither_confirm_nor_env():
    """Turning it OFF must never be blocked. An operator reaching for the off
    switch is not a moment to enforce ceremony."""
    state = _mk_state()
    state.spread_auto_trade_enabled = True
    out = await spread_auto_trade_toggle(_req(state), body={"enabled": False})
    assert out["enabled"] is False
    assert state.spread_auto_trade_enabled is False


async def test_observe_only_is_settable_and_omission_preserves(monkeypatch):
    _armed_env(monkeypatch)
    state = _mk_state()
    state.spread_auto_trade_observe_only = False

    await spread_auto_trade_toggle(
        _req(state), body={"enabled": True, "confirm": True, "observe_only": True}
    )
    assert state.spread_auto_trade_observe_only is True

    # Omitting the key must NOT silently reset it -- a UI that sends only
    # {enabled} would otherwise flip a live bot out of observe-only.
    await spread_auto_trade_toggle(
        _req(state), body={"enabled": True, "confirm": True}
    )
    assert state.spread_auto_trade_observe_only is True
