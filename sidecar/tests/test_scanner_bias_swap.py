"""Bias-source parity swap (2026-07-17): when flow_source='theta' and the
theta read is fresh, the scanner's inputs.drift_bias must be replaced by the
validated call/put-leg bias — not left on the QuantData snapshot bias (which
contradicted the theta drift on 2026-07-17 and demoted every aligned HIGH
call). Directly exercises _apply_flow_source_and_shadow.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from fef_sidecar.engine import multi_symbol_scanner as mss
from fef_sidecar.engine.types import FEFInputs


def _inputs(drift_bias: str, drift_m: float) -> FEFInputs:
    return FEFInputs(
        timestamp_ms=int(time.time() * 1000), spx_price=7450.0,
        drift_cum_net_m=drift_m, drift_bias=drift_bias,
        top_gex_walls=[], top_dex_walls=[], ntm_max_pain=0.0, raw_max_pain=0.0)


def _state_with_theta(bias: str, drift_m: float, *, stale: bool = False,
                      age_min: float = 0.0):
    now = int(time.time() * 1000)
    return SimpleNamespace(
        theta_flow={
            "enabled": False,  # skip the in-session shadow-row DB write
            "stale": stale,
            "drift_theta_cum_m": drift_m,
            "drift_theta_bias": bias,
            "drift_theta_d3_m": 0.0,
            "last_final_minute_ms": now - int(age_min * 60_000),
        },
        flow_shadow_qd_history=[],
    )


async def _run(monkeypatch, state, inputs, flow_source: str):
    monkeypatch.setattr(mss, "load_config",
                        lambda: SimpleNamespace(flow_source=flow_source))
    return await mss._apply_flow_source_and_shadow(state, inputs, engine=None)


@pytest.mark.asyncio
async def test_theta_mode_swaps_bias_to_validated_legs(monkeypatch):
    # Live QD snapshot says BEARISH; validated theta legs say BULLISH.
    state = _state_with_theta("BULLISH", drift_m=21.0)
    out = await _run(monkeypatch, state, _inputs("BEARISH", -8.0), "theta")
    assert out.drift_bias == "BULLISH"          # swapped
    assert out.drift_cum_net_m == 21.0          # drift swapped too


@pytest.mark.asyncio
async def test_qd_mode_leaves_bias_untouched(monkeypatch):
    state = _state_with_theta("BULLISH", drift_m=21.0)
    out = await _run(monkeypatch, state, _inputs("BEARISH", -8.0), "qd")
    assert out.drift_bias == "BEARISH"          # default mode: no swap
    assert out.drift_cum_net_m == -8.0


@pytest.mark.asyncio
async def test_stale_theta_keeps_qd_bias(monkeypatch):
    # theta present but stale -> fail-soft: keep the QD bias, do NOT blank it.
    state = _state_with_theta("BULLISH", drift_m=21.0, stale=True)
    out = await _run(monkeypatch, state, _inputs("BEARISH", -8.0), "theta")
    assert out.drift_bias == "BEARISH"
    assert out.drift_cum_net_m == -8.0          # drift also falls back


@pytest.mark.asyncio
async def test_old_theta_minute_keeps_qd_bias(monkeypatch):
    # Fresh flag fails on age even if not marked stale.
    state = _state_with_theta("BULLISH", drift_m=21.0, age_min=20.0)
    out = await _run(monkeypatch, state, _inputs("BEARISH", -8.0), "theta")
    assert out.drift_bias == "BEARISH"
