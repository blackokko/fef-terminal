"""Tests for the generalized cached bar-fetch helper + the
/chart/regression_channel endpoint.

Covers:
  - _get_cached_bars caches by (symbol, bar_size, duration), serves within
    TTL, re-fetches on a different key, and never serves an empty result.
  - _get_spread_bars_30m still routes the spread recommender's exact
    30-min / 5-D / active-symbol fetch through the generalized helper
    (behaviour unchanged).
  - the regression-channel endpoint returns center + sigma bands as
    epoch-SECOND {time, value} arrays, with the custom +/-dev*sigma band
    matching the fixed sigma bands at integer multipliers, and fails soft.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta

import pytest

from fef_sidecar.api import chart_regression_channel
from fef_sidecar.ibkr.models import Bar
from fef_sidecar.loop import _BARS_CACHE_TTL_MS, _get_cached_bars, _get_spread_bars_30m


class _RecordingIBKR:
    """Minimal IBKR stand-in that records every fetch_historical_bars call
    and returns a fixed (copied) bar list so the caller can't mutate ours."""

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars
        self.calls: list[tuple[str, str, str]] = []

    async def fetch_historical_bars(self, contract, *, bar_size: str, duration: str) -> list[Bar]:
        self.calls.append((contract.symbol, bar_size, duration))
        return list(self._bars)


def _mk_bars(closes: list[float], *, start: datetime | None = None,
             step: timedelta = timedelta(days=1)) -> list[Bar]:
    t0 = start or datetime(2026, 1, 2, 15, 0, 0)
    return [
        Bar(timestamp=t0 + step * i, open=c, high=c + 1, low=c - 1, close=c, volume=100)
        for i, c in enumerate(closes)
    ]


def _state(ibkr, *, active_symbol: str = "SPX") -> types.SimpleNamespace:
    return types.SimpleNamespace(ibkr=ibkr, bars_cache={}, active_symbol=active_symbol)


def _request(state) -> types.SimpleNamespace:
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(fef=state)))


# ---------------------------------------------------------------------------
# _get_cached_bars — caching semantics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cached_bars_caches_by_full_key() -> None:
    ibkr = _RecordingIBKR(_mk_bars([100, 101, 102, 103]))
    state = _state(ibkr)

    first = await _get_cached_bars(state, "SPX", "1 day", "6 M")
    assert len(first) == 4
    assert ibkr.calls == [("SPX", "1 day", "6 M")]
    # cache is keyed by the full (symbol, bar_size, duration) tuple
    assert ("SPX", "1 day", "6 M") in state.bars_cache

    # Second identical call within TTL is served from cache — no re-fetch.
    second = await _get_cached_bars(state, "SPX", "1 day", "6 M")
    assert second == first
    assert len(ibkr.calls) == 1


@pytest.mark.asyncio
async def test_cached_bars_distinct_keys_refetch() -> None:
    ibkr = _RecordingIBKR(_mk_bars([100, 101, 102]))
    state = _state(ibkr)

    await _get_cached_bars(state, "SPX", "1 day", "6 M")
    await _get_cached_bars(state, "SPX", "5 mins", "5 D")   # different bar_size
    await _get_cached_bars(state, "ES", "1 day", "6 M")      # different symbol

    assert ibkr.calls == [
        ("SPX", "1 day", "6 M"),
        ("SPX", "5 mins", "5 D"),
        ("ES", "1 day", "6 M"),
    ]
    assert set(state.bars_cache) == {
        ("SPX", "1 day", "6 M"), ("SPX", "5 mins", "5 D"), ("ES", "1 day", "6 M"),
    }


@pytest.mark.asyncio
async def test_cached_bars_stale_entry_refetches() -> None:
    ibkr = _RecordingIBKR(_mk_bars([100, 101, 102]))
    state = _state(ibkr)
    await _get_cached_bars(state, "SPX", "1 day", "6 M")
    # Backdate the cache entry beyond the TTL -> next call re-fetches.
    ts, bars = state.bars_cache[("SPX", "1 day", "6 M")]
    state.bars_cache[("SPX", "1 day", "6 M")] = (ts - _BARS_CACHE_TTL_MS - 1, bars)
    await _get_cached_bars(state, "SPX", "1 day", "6 M")
    assert len(ibkr.calls) == 2


@pytest.mark.asyncio
async def test_cached_bars_empty_result_not_served() -> None:
    """A transient empty fetch must NOT be cached-served — it keeps retrying
    (matches the pre-refactor spread-recommender behaviour)."""
    ibkr = _RecordingIBKR([])
    state = _state(ibkr)
    assert await _get_cached_bars(state, "SPX", "1 day", "6 M") == []
    assert await _get_cached_bars(state, "SPX", "1 day", "6 M") == []
    assert len(ibkr.calls) == 2  # re-fetched, not served from the empty cache


@pytest.mark.asyncio
async def test_cached_bars_fetch_failure_is_soft() -> None:
    class _Boom:
        calls: list = []

        async def fetch_historical_bars(self, contract, *, bar_size, duration):
            _Boom.calls.append(1)
            raise RuntimeError("IBKR down")

    state = _state(_Boom())
    assert await _get_cached_bars(state, "SPX", "1 day", "6 M") == []


# ---------------------------------------------------------------------------
# _get_spread_bars_30m — spread recommender path UNCHANGED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spread_bars_30m_uses_30min_5d_active_symbol() -> None:
    ibkr = _RecordingIBKR(_mk_bars([100, 101, 102, 103, 104]))
    state = _state(ibkr, active_symbol="ES")

    bars = await _get_spread_bars_30m(state)
    assert len(bars) == 5
    # Exact contract the spread recommender relied on pre-refactor.
    assert ibkr.calls == [("ES", "30 mins", "5 D")]
    assert ("ES", "30 mins", "5 D") in state.bars_cache

    # Cached on the second call — no extra IBKR hit (25-min TTL preserved).
    await _get_spread_bars_30m(state)
    assert len(ibkr.calls) == 1


# ---------------------------------------------------------------------------
# /chart/regression_channel endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_regression_channel_shapes_and_epoch_seconds() -> None:
    closes = [100, 102, 101, 103, 102, 104, 103, 105, 104, 106]
    bars = _mk_bars(closes)
    state = _state(_RecordingIBKR(bars))

    out = await chart_regression_channel(
        _request(state), symbol="SPX", bar_size="1 day", duration="6 M", dev=2.0,
    )
    assert out["status"] == "ok"
    assert out["bar_count"] == len(closes)
    assert out["dev"] == 2.0 and out["bar_size"] == "1 day" and out["duration"] == "6 M"

    keys = {"center", "upper_1", "upper_2", "upper_3",
            "lower_1", "lower_2", "lower_3", "upper_dev", "lower_dev"}
    for k in keys:
        assert len(out[k]) == len(closes)

    # times are epoch SECONDS (matching pattern_overlay), ascending + distinct.
    times = [p["time"] for p in out["center"]]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert times[0] == int(bars[0].timestamp.timestamp())

    # bands are symmetric around center and correctly ordered
    for i in range(len(closes)):
        c = out["center"][i]["value"]
        u1, u2, u3 = out["upper_1"][i]["value"], out["upper_2"][i]["value"], out["upper_3"][i]["value"]
        l1 = out["lower_1"][i]["value"]
        assert u1 > c and u2 > u1 and u3 > u2
        # tolerance covers the endpoint's 4-decimal rounding of each value
        assert abs((u1 - c) - (c - l1)) < 1e-3           # symmetric 1 sigma
        assert abs((u2 - c) - 2 * (u1 - c)) < 1e-3       # 2 sigma == 2x 1 sigma


@pytest.mark.asyncio
async def test_regression_channel_dev_matches_integer_sigma_bands() -> None:
    bars = _mk_bars([100, 102, 101, 103, 102, 104, 103, 105])
    state = _state(_RecordingIBKR(bars))

    # dev=2 -> the custom band must coincide with the fixed 2 sigma band.
    out2 = await chart_regression_channel(
        _request(state), symbol="SPX", bar_size="1 day", duration="6 M", dev=2.0,
    )
    for i in range(out2["bar_count"]):
        assert abs(out2["upper_dev"][i]["value"] - out2["upper_2"][i]["value"]) < 1e-6
        assert abs(out2["lower_dev"][i]["value"] - out2["lower_2"][i]["value"]) < 1e-6

    # dev=3 -> coincides with the fixed 3 sigma band.
    state3 = _state(_RecordingIBKR(bars))
    out3 = await chart_regression_channel(
        _request(state3), symbol="SPX", bar_size="1 day", duration="6 M", dev=3.0,
    )
    for i in range(out3["bar_count"]):
        assert abs(out3["upper_dev"][i]["value"] - out3["upper_3"][i]["value"]) < 1e-6


@pytest.mark.asyncio
async def test_regression_channel_bad_dev_defaults_to_two() -> None:
    bars = _mk_bars([100, 101, 102, 103])
    for bad in (0.0, -1.0):
        state = _state(_RecordingIBKR(bars))
        out = await chart_regression_channel(
            _request(state), symbol="SPX", bar_size="1 day", duration="6 M", dev=bad,
        )
        assert out["dev"] == 2.0


@pytest.mark.asyncio
async def test_regression_channel_no_bars_is_soft() -> None:
    state = _state(_RecordingIBKR([]))
    out = await chart_regression_channel(
        _request(state), symbol="SPX", bar_size="1 day", duration="6 M", dev=2.0,
    )
    assert out["status"].startswith("no bars")
    assert out["bar_count"] == 0
    assert out["center"] == [] and out["upper_dev"] == []
