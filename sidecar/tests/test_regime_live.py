"""/regime/live tile assembly — validated live measurements, fail-open.

Covers the task-mandated scenarios against a mocked state + in-memory DB:
  - compressed range regime reading (chop gate SKIPPING fires)
  - post-capitulation flag fires on a >=max(1.5x median, 0.25% spot) 15m move
  - FOMC day flags (pre-14:00 warning + PM 2.2x vol + upcoming OPEX)
  - dealer gamma unknown fallback (no exposure snapshot) + classified path
  - flow-bias tile from engine inputs with the bias veto armed
  - VIX tercile from the trailing 20-session vix_sample distribution
"""

from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.config_store import UIConfig
from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.engine import regime_live as rl
from fef_sidecar.engine.chop_gate import _ET
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
    monkeypatch.setattr(rl, "load_config", lambda: UIConfig())
    import fef_sidecar.engine.multi_symbol_scanner as mss
    monkeypatch.setattr(mss, "load_config", lambda: UIConfig())


@pytest.fixture(autouse=True)
def _restore_mock_coherent_env():
    """AppState.build() setdefaults FEF_MOCK_COHERENT process-wide — leave the
    env as found (same guard as test_chop_gate)."""
    before = os.environ.get("FEF_MOCK_COHERENT")
    yield
    if before is None:
        os.environ.pop("FEF_MOCK_COHERENT", None)
    elif os.environ.get("FEF_MOCK_COHERENT") != before:
        os.environ["FEF_MOCK_COHERENT"] = before


def _et_ms(date_str: str, hh: int, mm: int) -> int:
    y, mo, d = (int(x) for x in date_str.split("-"))
    return int(datetime(y, mo, d, hh, mm, tzinfo=_ET).timestamp() * 1000)


async def _seed_spx_bars(rows: list[tuple[int, float, float, float]]) -> None:
    """rows: (timestamp_ms, high, low, close)."""
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import HistoricalBar
    async with get_session() as session:
        for ts, hi, lo, close in rows:
            session.add(HistoricalBar(
                symbol="SPX", bar_size="1 min", timestamp_ms=ts,
                open=close, high=hi, low=lo, close=close, volume=1.0,
            ))


def _range_bars(date_str: str, hh: int, mm: int, n: int, rng: float) -> list:
    t0 = _et_ms(date_str, hh, mm)
    return [(t0 + i * _MIN, BASE + rng / 2, BASE - rng / 2, BASE) for i in range(n)]


# ---------------------------------------------------------------------------
# 1. RANGE REGIME — compressed reading => SKIPPING fires
# ---------------------------------------------------------------------------


async def test_compressed_range_regime_reports_skipping() -> None:
    rows: list = []
    for s in range(1, 21):
        rows += _range_bars(f"2026-06-{s:02d}", 11, 45, 110, 4.0)
    rows += _range_bars("2026-06-24", 12, 15, 45, 1.0)
    await _seed_spx_bars(rows)

    state = AppState.build()
    out = await rl.build_regime_live(state, at_ms=_et_ms("2026-06-24", 13, 0))
    t = out["tiles"]["range_regime"]
    assert t["label"] == "compressed"
    assert t["percentile"] == pytest.approx(0.0)
    assert "SKIPPING fires" in t["effect_on_bot"]
    assert "r30 1.0pts" in t["reading"]
    assert t["updated_at"] == _et_ms("2026-06-24", 13, 0)


async def test_range_regime_fail_open_on_empty_db() -> None:
    state = AppState.build()
    out = await rl.build_regime_live(state, at_ms=_et_ms("2026-06-24", 13, 0))
    t = out["tiles"]["range_regime"]
    assert t["label"] == "unknown"
    assert "OPEN" in t["effect_on_bot"]  # gate passes open on data gap


# ---------------------------------------------------------------------------
# 2. EXPECTED MOVE — post-capitulation flag
# ---------------------------------------------------------------------------


async def test_post_cap_fires_on_sharp_15m_move() -> None:
    # Flat session from the open, then a 40pt slide over the final 15 minutes:
    # median 15m |move| ~ 0 => threshold = 0.25% x spot ~ 14.9pts; 40 >= that.
    rows: list = []
    t0 = _et_ms("2026-06-24", 9, 30)
    for i in range(195):  # 09:30 .. 12:44 flat
        rows.append((t0 + i * _MIN, BASE + 0.5, BASE - 0.5, BASE))
    for j in range(15):   # 12:45 .. 12:59 sliding down to BASE-40
        c = BASE - (j + 1) * (40.0 / 15.0)
        ts = _et_ms("2026-06-24", 12, 45) + j * _MIN
        rows.append((ts, c + 0.5, c - 0.5, c))
    await _seed_spx_bars(rows)

    state = AppState.build()
    out = await rl.build_regime_live(state, at_ms=_et_ms("2026-06-24", 13, 0))
    t = out["tiles"]["expected_move"]
    assert t["post_cap"] is True
    assert t["m15_current"] == pytest.approx(40.0, abs=0.1)
    assert t["label"] == "post-cap: expect ~2x move"
    assert "POST-CAPITULATION" in t["effect_on_bot"]


async def test_post_cap_does_not_fire_on_quiet_tape() -> None:
    rows = _range_bars("2026-06-24", 9, 30, 210, 1.0)  # flat closes all session
    await _seed_spx_bars(rows)
    state = AppState.build()
    out = await rl.build_regime_live(state, at_ms=_et_ms("2026-06-24", 13, 0))
    t = out["tiles"]["expected_move"]
    assert t["post_cap"] is False
    assert "post-cap" not in t["label"]


# ---------------------------------------------------------------------------
# 3. EVENTS — FOMC day flags
# ---------------------------------------------------------------------------


async def test_fomc_day_pre_statement_warning() -> None:
    state = AppState.build()
    out = await rl.build_regime_live(state, at_ms=_et_ms("2026-06-17", 10, 0))
    t = out["tiles"]["events"]
    assert t["label"] == "FOMC"
    assert "-$61/ct" in t["effect_on_bot"]
    assert "stand down" in t["effect_on_bot"]
    # Next event within 3 sessions: OPEX Thu 06-18 (rolled off Juneteenth).
    kinds = {(e["kind"], e["date"]) for e in t["upcoming"]}
    assert ("OPEX", "2026-06-18") in kinds


async def test_fomc_day_pm_expects_elevated_vol() -> None:
    state = AppState.build()
    out = await rl.build_regime_live(state, at_ms=_et_ms("2026-06-17", 15, 0))
    t = out["tiles"]["events"]
    assert "2.2x" in t["effect_on_bot"]
    assert "-$61/ct" not in t["effect_on_bot"]


async def test_clear_day_events_tile() -> None:
    state = AppState.build()
    out = await rl.build_regime_live(state, at_ms=_et_ms("2026-07-06", 10, 0))
    t = out["tiles"]["events"]
    assert t["label"] == "clear"
    assert t["today"] == []


# ---------------------------------------------------------------------------
# 4. DEALER GAMMA — unknown fallback + classified path
# ---------------------------------------------------------------------------


async def test_dealer_gamma_unknown_without_snapshot() -> None:
    state = AppState.build()
    state.last_snapshot = None
    out = await rl.build_regime_live(state, at_ms=_et_ms("2026-06-24", 13, 0))
    t = out["tiles"]["dealer_gamma"]
    assert t["label"] == "unknown"
    assert t["regime"] == "UNKNOWN"
    assert "unknown" in t["effect_on_bot"]


async def test_dealer_gamma_long_from_snapshot() -> None:
    state = AppState.build()
    state.last_snapshot = SimpleNamespace(
        fetched_at_ms=1234,
        spx_price=6000.0,
        gex=SimpleNamespace(
            spx_price=6000.0,
            rows=[
                SimpleNamespace(strike=5900.0, net_m=-20.0),
                SimpleNamespace(strike=5950.0, net_m=50.0),
                SimpleNamespace(strike=6050.0, net_m=30.0),
            ],
        ),
    )
    out = await rl.build_regime_live(state, at_ms=_et_ms("2026-06-24", 13, 0))
    t = out["tiles"]["dealer_gamma"]
    assert t["label"] == "long"
    assert t["regime"] == "LONG_GAMMA"
    assert t["net_gex_m"] == pytest.approx(60.0)
    assert "dampens" in t["effect_on_bot"]
    assert t["updated_at"] == 1234


# ---------------------------------------------------------------------------
# 5. FLOW BIAS — engine inputs + veto armed
# ---------------------------------------------------------------------------


async def test_flow_bias_from_engine_inputs_with_veto_armed() -> None:
    state = AppState.build()
    state.engine._history.append(SimpleNamespace(  # type: ignore[attr-defined]
        drift_bias="BULLISH", timestamp_ms=999, drift_cum_net_m=42.0,
    ))
    out = await rl.build_regime_live(state, at_ms=_et_ms("2026-06-24", 13, 0))
    t = out["tiles"]["flow_bias"]
    assert t["label"] == "BULLISH"
    assert t["bias_veto_armed"] is True
    assert "ARMED" in t["effect_on_bot"]
    assert "PUTS" in t["effect_on_bot"]  # fires against BULLISH flow = puts
    assert t["updated_at"] == 999


async def test_flow_bias_na_without_inputs() -> None:
    state = AppState.build()
    state.last_snapshot = None
    out = await rl.build_regime_live(state, at_ms=_et_ms("2026-06-24", 13, 0))
    t = out["tiles"]["flow_bias"]
    assert t["reading"] == "n/a"
    assert "unknown" in t["effect_on_bot"]


# ---------------------------------------------------------------------------
# 6. EXPECTED MOVE — VIX tercile vs trailing 20 sessions
# ---------------------------------------------------------------------------


async def test_vix_high_tercile_labels_expected_move() -> None:
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import VixSample
    async with get_session() as session:
        for s in range(1, 21):  # 20 prior sessions, closes 10..29
            session.add(VixSample(
                timestamp_ms=_et_ms(f"2026-06-{s:02d}", 15, 59), value=9.0 + s,
            ))

    state = AppState.build()
    state.last_vix = 28.5
    out = await rl.build_regime_live(state, at_ms=_et_ms("2026-06-24", 13, 0))
    t = out["tiles"]["expected_move"]
    assert t["vix"] == pytest.approx(28.5)
    assert t["vix_percentile"] == pytest.approx(95.0)
    assert "high tercile" in t["reading"]
    assert t["label"] == "~2-3x normal next-15m move"


async def test_tiles_always_present_and_fail_open() -> None:
    """Every tile key exists with the {reading,label,effect_on_bot,updated_at}
    schema even on a totally empty state/DB."""
    state = AppState.build()
    state.last_snapshot = None
    out = await rl.build_regime_live(state, at_ms=_et_ms("2026-06-24", 13, 0))
    assert set(out["tiles"].keys()) == {
        "range_regime", "expected_move", "flow_bias", "events", "dealer_gamma",
    }
    for tile in out["tiles"].values():
        for key in ("reading", "label", "effect_on_bot", "updated_at"):
            assert key in tile
