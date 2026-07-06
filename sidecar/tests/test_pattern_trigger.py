"""Pattern-trigger path tests: confirmed-bar fire -> audit row -> signal_iter
row -> auto-trade attempt through the REAL maybe_auto_trade guardrails with a
mocked broker (place_bracket stub + deterministic option quote)."""

from __future__ import annotations

import os
import time
from datetime import datetime

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.config_store import UIConfig
from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.engine import pattern_trigger as pt
from fef_sidecar.engine.pattern_overlay import PatternHit
from fef_sidecar.ibkr.models import OptionQuote
from fef_sidecar.state import AppState

_MIN = 60_000

PATTERN_TRIGGERS = ["flag_bull", "flag_bear", "pattern_bull", "pattern_bear"]


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.fixture(autouse=True)
def _deterministic_config(monkeypatch: pytest.MonkeyPatch):
    """Don't read the developer's real ~/.fef-terminal/config.json."""
    monkeypatch.setattr(pt, "load_config", lambda: UIConfig())


@pytest.fixture(autouse=True)
def _mechanism_timeframes(monkeypatch: pytest.MonkeyPatch):
    """These tests exercise the fire mechanism on compact synthetic 1-min
    series; pin the stream list so the production default (5m+ only, per
    the 2026-07-02 user decision) doesn't starve them of confirmed bars.
    The default itself is pinned by test_default_timeframes_are_5m_plus."""
    monkeypatch.setattr(pt, "TIMEFRAMES", (("1m", 1), ("5m", 5)))


def test_default_timeframes_are_5m_plus(monkeypatch: pytest.MonkeyPatch):
    """USER DECISION 2026-07-02: no 1-minute trading timeframe — 1m signals
    go stale inside the 300s trade cooldown. Default = 5m/15m/30m/4h/1D."""
    monkeypatch.delenv("FEF_PATTERN_TIMEFRAMES", raising=False)
    assert pt._parse_timeframes() == (
        ("5m", 5), ("15m", 15), ("30m", 30), ("4h", 240), ("1D", 1440),
    )
    # env override still honored (and dedupes/validates)
    monkeypatch.setenv("FEF_PATTERN_TIMEFRAMES", "1m, 5m, bogus, 5m, 1d")
    assert pt._parse_timeframes() == (("1m", 1), ("5m", 5), ("1D", 1440))


@pytest.fixture()
def state(monkeypatch: pytest.MonkeyPatch) -> AppState:
    monkeypatch.setenv("FEF_LIVE_TRADING", "true")
    s = AppState.build()  # mock providers by default env
    s.active_symbol = "SPX"
    s.auto_trade_enabled = True
    s.auto_trade_trigger_types = list(PATTERN_TRIGGERS)
    s.auto_trade_qty_override = 3  # mirrors the "Pattern signals v1" preset
    s.auto_trade_cool_down_sec = 300

    async def fake_option_quote(contract):
        from fef_sidecar.ibkr.models import Contract  # noqa: F401
        return OptionQuote(
            timestamp=datetime.now(),
            contract=contract,
            last=5.0, bid=4.9, ask=5.1,
            delta=0.40 if contract.right == "C" else -0.40,
            gamma=0.01, vega=0.5, theta=-1.2, iv=0.18,
            underlying_price=7448.0,
        )

    monkeypatch.setattr(s.ibkr, "get_option_quote", fake_option_quote)
    return s


@pytest.fixture()
def bracket_calls(monkeypatch: pytest.MonkeyPatch) -> list:
    """Stub place_bracket at its import source (auto_trader imports it lazily)."""
    calls: list = []

    async def fake_place_bracket(ibkr, spec, *, confirm=False, ticket_id=None, **kwargs):
        calls.append({"spec": spec, "confirm": confirm, **kwargs})
        return {"ok": True, "parent_order_id": 4242, "dry_run": False}

    import fef_sidecar.engine.live_trading as lt
    monkeypatch.setattr(lt, "place_bracket", fake_place_bracket)
    return calls


def _bar(ts_ms: int, o: float, h: float, low: float, c: float) -> dict:
    return {"ts": ts_ms, "o": o, "h": h, "l": low, "c": c}


_WARMUP = 20         # flat bars before the pole (cycle needs >= 25 confirmed)
_BREAKOUT_IDX = _WARMUP + 5  # pole(3) + 2 more flag bars, then the breakout


def _flag_bar_specs(start_ms: int, base: float) -> list[dict]:
    """Same geometry as the overlay tests: flat warmup, 3-bar +10pt pole,
    tight 3-bar flag window (incl. pole top) — breakout bar NOT included
    (appended by tests at index _BREAKOUT_IDX)."""
    bars = [
        _bar(start_ms + i * _MIN, base, base + 0.1, base - 0.1, base)
        for i in range(_WARMUP)
    ]
    i = start_ms + _WARMUP * _MIN
    bars.append(_bar(i, base, base + 4.6, base - 0.1, base + 4.5))
    bars.append(_bar(i + _MIN, base + 4.5, base + 9.9, base + 4.4, base + 9.8))
    bars.append(_bar(i + 2 * _MIN, base + 9.8, base + 10.3, base + 9.7, base + 10.0))
    bars.append(_bar(i + 3 * _MIN, base + 10.0, base + 10.3, base + 9.6, base + 10.1))
    bars.append(_bar(i + 4 * _MIN, base + 10.1, base + 10.25, base + 9.7, base + 9.9))
    return bars


async def _seed_bars(specs: list[dict]) -> None:
    for b in specs:
        await SignalIterRepository.write_bar(
            symbol="SPX", bar_size="1 min", timestamp_ms=b["ts"],
            open_=b["o"], high=b["h"], low=b["l"], close=b["c"], volume=100,
        )


def _series_start(now_ms: int, total_bars: int) -> int:
    """Start ts so the LAST seeded bar closed ~5 minutes ago (confirmed),
    aligned to minute boundaries."""
    end = (now_ms // _MIN) * _MIN - 5 * _MIN
    return end - (total_bars - 1) * _MIN


async def test_full_fire_path_places_bracket(state: AppState, bracket_calls: list) -> None:
    now_ms = int(time.time() * 1000)
    start = _series_start(now_ms, _BREAKOUT_IDX + 1)
    base = 7438.0
    await _seed_bars(_flag_bar_specs(start, base))

    last_processed: dict[str, int] = {}
    fires = await pt.run_pattern_cycle(state, last_processed)
    assert fires == []  # first cycle arms without replaying history
    assert "1m" in last_processed

    # Breakout bar closes above the flag high (base+10.3)
    bt = start + _BREAKOUT_IDX * _MIN
    await _seed_bars([_bar(bt, base + 10.0, base + 11.0, base + 9.9, base + 10.8)])
    fires = await pt.run_pattern_cycle(state, last_processed)

    assert [f["kind"] for f in fires] == ["flag_bull"]
    assert fires[0]["armed"] is True and fires[0]["offered_to_auto_trader"] is True

    # 1. Advisory audit row
    audits = await SignalIterRepository.query_audit_log(category="pattern_overlay")
    assert len(audits) == 1
    assert "flag_bull 1m SPX" in audits[0].summary
    assert audits[0].details["measured_target"] == pytest.approx(base + 10.8 + 10.0)

    # 2. signal_iter row with PATTERN reasoning
    signals = await SignalIterRepository.recent(limit=10)
    pattern_rows = [r for r in signals if r.reasoning.startswith("PATTERN:")]
    assert len(pattern_rows) == 1
    row = pattern_rows[0]
    assert row.direction.value == "LONG_CALLS"
    assert row.confidence.value == "LOW"
    assert "flag_bull 1m" in row.reasoning
    assert row.extras["trigger_type"] == "flag_bull"
    assert row.extras["pattern_trigger"]["kind"] == "flag_bull"
    assert row.extras["option_pricing"]["entry_limit"] == 5.1

    # 3. Bracket placed through the normal path with the preset qty
    assert len(bracket_calls) == 1
    spec = bracket_calls[0]["spec"]
    assert spec.right == "C" and spec.symbol == "SPX"
    assert spec.qty == 3
    assert bracket_calls[0]["confirm"] is True
    # Attribution: auto_trader stamps its source + the trigger context
    assert bracket_calls[0]["source"] == "auto_pattern"
    assert bracket_calls[0]["signal_ref"]["trigger_type"] == "flag_bull"
    assert state.auto_trade_count_by_symbol["SPX"] == 1


async def test_unarmed_trigger_types_stay_advisory_only(
    state: AppState, bracket_calls: list,
) -> None:
    state.auto_trade_trigger_types = ["confluence", "aligned_only"]  # app default
    now_ms = int(time.time() * 1000)
    start = _series_start(now_ms, _BREAKOUT_IDX + 1)
    base = 7438.0
    await _seed_bars(_flag_bar_specs(start, base))
    last_processed: dict[str, int] = {}
    await pt.run_pattern_cycle(state, last_processed)
    await _seed_bars([_bar(start + _BREAKOUT_IDX * _MIN, base + 10.0, base + 11.0, base + 9.9, base + 10.8)])
    fires = await pt.run_pattern_cycle(state, last_processed)

    assert [f["kind"] for f in fires] == ["flag_bull"]
    assert fires[0]["armed"] is False and fires[0]["offered_to_auto_trader"] is False
    # audit row yes; signal row + bracket NO — metrics stay clean by default
    audits = await SignalIterRepository.query_audit_log(category="pattern_overlay")
    assert len(audits) == 1
    signals = await SignalIterRepository.recent(limit=10)
    assert [r for r in signals if r.reasoning.startswith("PATTERN:")] == []
    assert bracket_calls == []


async def test_auto_trade_disabled_still_audits_signal_but_no_order(
    state: AppState, bracket_calls: list,
) -> None:
    state.auto_trade_enabled = False
    now_ms = int(time.time() * 1000)
    start = _series_start(now_ms, _BREAKOUT_IDX + 1)
    base = 7438.0
    await _seed_bars(_flag_bar_specs(start, base))
    last_processed: dict[str, int] = {}
    await pt.run_pattern_cycle(state, last_processed)
    await _seed_bars([_bar(start + _BREAKOUT_IDX * _MIN, base + 10.0, base + 11.0, base + 9.9, base + 10.8)])
    await pt.run_pattern_cycle(state, last_processed)

    signals = await SignalIterRepository.recent(limit=10)
    assert len([r for r in signals if r.reasoning.startswith("PATTERN:")]) == 1
    assert bracket_calls == []
    assert state.auto_trade_last_block_reason == "auto_trade disabled"


async def test_cooldown_guardrail_blocks_and_writes_block_audit(
    state: AppState, bracket_calls: list,
) -> None:
    # Symbol fired 10s ago -> per-symbol cool-down (300s) must block.
    state.auto_trade_last_at_ms_by_symbol["SPX"] = int(time.time() * 1000) - 10_000
    now_ms = int(time.time() * 1000)
    start = _series_start(now_ms, _BREAKOUT_IDX + 1)
    base = 7438.0
    await _seed_bars(_flag_bar_specs(start, base))
    last_processed: dict[str, int] = {}
    await pt.run_pattern_cycle(state, last_processed)
    await _seed_bars([_bar(start + _BREAKOUT_IDX * _MIN, base + 10.0, base + 11.0, base + 9.9, base + 10.8)])
    await pt.run_pattern_cycle(state, last_processed)

    assert bracket_calls == []
    blocks = await SignalIterRepository.query_audit_log(category="auto_trade_block")
    assert len(blocks) == 1
    assert blocks[0].details["reason_code"] == "COOL_DOWN"
    assert "PATTERN" in blocks[0].summary
    # signal row still exists — fully auditable even when blocked
    signals = await SignalIterRepository.recent(limit=10)
    assert len([r for r in signals if r.reasoning.startswith("PATTERN:")]) == 1


async def test_unconfirmed_bar_never_fires(state: AppState, bracket_calls: list) -> None:
    now_ms = int(time.time() * 1000)
    start = _series_start(now_ms, _BREAKOUT_IDX + 1)
    base = 7438.0
    await _seed_bars(_flag_bar_specs(start, base))
    last_processed: dict[str, int] = {}
    await pt.run_pattern_cycle(state, last_processed)
    # Breakout bar stamped RIGHT NOW -> still forming, not confirmed
    fresh_ts = (now_ms // _MIN) * _MIN
    await _seed_bars([_bar(fresh_ts, base + 10.0, base + 11.0, base + 9.9, base + 10.8)])
    fires = await pt.run_pattern_cycle(state, last_processed)
    assert fires == []
    assert bracket_calls == []
    assert await SignalIterRepository.query_audit_log(category="pattern_overlay") == []


def test_trigger_type_and_kind_eligibility_mapping() -> None:
    cfg = UIConfig()  # pattern_trade_kinds defaults to ["engulfing"]
    flag = PatternHit(0, 0, "flag_bull", "BULL", "BULL FLAG", 100.0)
    engulf = PatternHit(0, 0, "engulf_bear", "BEAR", "Engulf", 100.0)
    hammer = PatternHit(0, 0, "hammer", "BULL", "Hammer", 100.0)
    coil = PatternHit(0, 0, "inside_coil", "NEUTRAL", "coil", 100.0)

    assert pt.fire_trigger_type(flag) == "flag_bull"
    assert pt.fire_trigger_type(engulf) == "pattern_bear"
    assert pt.fire_trigger_type(hammer) == "pattern_bull"
    assert pt.fire_trigger_type(coil) is None

    assert pt.is_trade_eligible_kind(flag, cfg) is True
    assert pt.is_trade_eligible_kind(engulf, cfg) is True
    assert pt.is_trade_eligible_kind(hammer, cfg) is False  # not in default kinds
    cfg2 = UIConfig(pattern_trade_kinds=["engulfing", "hammer"])
    assert pt.is_trade_eligible_kind(hammer, cfg2) is True


def test_auto_trader_honors_explicit_trigger_type() -> None:
    from fef_sidecar.engine.auto_trader import _verdict_trigger_type

    assert _verdict_trigger_type({"trigger_type": "flag_bull"}) == "flag_bull"
    assert _verdict_trigger_type({"aligned": True, "wall_collapse_trigger": True}) == "confluence"
    assert _verdict_trigger_type({"aligned": True}) == "aligned_only"
    assert _verdict_trigger_type({}) == "other"
