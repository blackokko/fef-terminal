"""Bounded wait for the mirror's SPY spot anchor (2026-07-29).

Incident: the mirror went 0-for-6 across 7/28-7/29 under fresh-or-skip. The warm
kick is fire-and-forget and was sized against the retired limit+chase entry
runway; on the static-MKT path the mirror read the cache in the SAME SECOND the
kick fired. Fix = wait (bounded) for a qualifying entry instead of racing it,
WITHOUT relaxing the 10s freshness bound the 7/27 incident bought.
"""
import asyncio
import time

import pytest

from fef_sidecar.engine import spx_target_watcher as W


class _FakeIBKR:
    """get_last_quote resolves after `delay` with a fixed bid/ask."""

    def __init__(self, delay=0.0, bid=738.0, ask=738.04):
        self.delay, self.bid, self.ask, self.calls = delay, bid, ask, 0

    async def get_last_quote(self, contract):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return type("Q", (), {"bid": self.bid, "ask": self.ask})()


class _State:
    def __init__(self, ibkr):
        self.ibkr = ibkr


@pytest.mark.asyncio
async def test_default_is_still_nonblocking_on_a_cold_cache():
    """The 0.1s poll loop contract: no wait arg -> returns immediately, None."""
    st = _State(_FakeIBKR(delay=0.5))
    t0 = time.monotonic()
    spot, src = await W._watcher_stock_quote_spot(st, "SPY", max_age_ms=10_000)
    elapsed = time.monotonic() - t0
    assert spot is None and src is None
    assert elapsed < 0.2, f"default path blocked for {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_bounded_wait_lands_the_refresh_and_serves_it():
    """THE fix: the fire path waits and gets the anchor the racing call missed."""
    st = _State(_FakeIBKR(delay=0.3))
    spot, src = await W._watcher_stock_quote_spot(
        st, "SPY", max_age_ms=10_000, wait_for_refresh_sec=3.0)
    assert src == "ibkr_stock_quote"
    assert spot == pytest.approx(738.02, abs=1e-6)   # bid/ask MID, not last


@pytest.mark.asyncio
async def test_wait_returns_early_and_does_not_burn_the_full_bound():
    st = _State(_FakeIBKR(delay=0.2))
    t0 = time.monotonic()
    spot, _ = await W._watcher_stock_quote_spot(
        st, "SPY", max_age_ms=10_000, wait_for_refresh_sec=3.0)
    elapsed = time.monotonic() - t0
    assert spot is not None
    assert elapsed < 1.0, f"waited {elapsed:.2f}s for a 0.2s fetch"


@pytest.mark.asyncio
async def test_wait_is_bounded_when_the_feed_never_answers():
    """A dead feed must not stall the fire hook beyond the bound."""
    st = _State(_FakeIBKR(delay=30.0))
    t0 = time.monotonic()
    spot, src = await W._watcher_stock_quote_spot(
        st, "SPY", max_age_ms=10_000, wait_for_refresh_sec=0.4)
    elapsed = time.monotonic() - t0
    assert spot is None and src is None
    assert elapsed < 1.5, f"unbounded wait: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_wait_does_NOT_relax_the_freshness_bound():
    """A 20s-old entry stays REFUSED at max_age 10s even with a wait — the
    7/27 guarantee (no stale anchor for a ~1pt-1R plan) is untouched. The fetch
    is slow enough that no fresh entry can land inside the bound."""
    st = _State(_FakeIBKR(delay=30.0))
    W._stock_spot_state(st)
    st._watcher_stock_spot_cache["SPY"] = (int(time.time() * 1000) - 20_000, 738.0)
    spot, src = await W._watcher_stock_quote_spot(
        st, "SPY", max_age_ms=10_000, wait_for_refresh_sec=0.3)
    assert spot is None and src is None, "served an anchor older than the bound"
    # ...while the looser exit-evaluation bound still accepts the same entry.
    spot30, src30 = await W._watcher_stock_quote_spot(st, "SPY")
    assert spot30 == 738.0 and src30 == "ibkr_stock_quote"


@pytest.mark.asyncio
async def test_fresh_entry_short_circuits_without_waiting():
    st = _State(_FakeIBKR(delay=30.0))
    W._stock_spot_state(st)
    st._watcher_stock_spot_cache["SPY"] = (int(time.time() * 1000) - 500, 737.5)
    t0 = time.monotonic()
    spot, src = await W._watcher_stock_quote_spot(
        st, "SPY", max_age_ms=10_000, wait_for_refresh_sec=3.0)
    assert (spot, src) == (737.5, "ibkr_stock_quote")
    assert time.monotonic() - t0 < 0.2


@pytest.mark.asyncio
async def test_mirror_wait_default_and_env_handling(monkeypatch):
    """Wire-up guard. Asserts the DEFAULT, never the live env-influenced value —
    review NIT: the previous `_MIRROR_SPOT_WAIT_SEC > 0` assertion turned the
    ship gate RED for anyone using the documented FEF_SPY_MIRROR_SPOT_WAIT_SEC=0
    rollback, i.e. exercising the escape hatch blocked all shipping."""
    from fef_sidecar.engine import spy_mirror as M

    monkeypatch.delenv("FEF_SPY_MIRROR_SPOT_WAIT_SEC", raising=False)
    assert M._mirror_spot_wait_sec() == 4.5
    # Must STRICTLY DOMINATE one provider attempt, else a slow-but-alive feed is
    # permanently starved AND pays the full wait (review MINOR-3 dead band).
    assert M._mirror_spot_wait_sec() > W._STOCK_SPOT_TIMEOUT_SEC
    # The 7/27 freshness guarantee is untouched by this change.
    assert M._MIRROR_SPOT_MAX_AGE_MS == 10_000

    # 0 is a legitimate kill-switch back to the racing behaviour.
    monkeypatch.setenv("FEF_SPY_MIRROR_SPOT_WAIT_SEC", "0")
    assert M._mirror_spot_wait_sec() == 0.0
    # Pathological values are clamped / rejected, never crash at import.
    monkeypatch.setenv("FEF_SPY_MIRROR_SPOT_WAIT_SEC", "600")
    assert M._mirror_spot_wait_sec() == 10.0
    monkeypatch.setenv("FEF_SPY_MIRROR_SPOT_WAIT_SEC", "abc")
    assert M._mirror_spot_wait_sec() == 4.5
    monkeypatch.setenv("FEF_SPY_MIRROR_SPOT_WAIT_SEC", "nan")
    assert M._mirror_spot_wait_sec() == 4.5
    monkeypatch.setenv("FEF_SPY_MIRROR_SPOT_WAIT_SEC", "-5")
    assert M._mirror_spot_wait_sec() == 0.0


@pytest.mark.asyncio
async def test_resolved_refresh_failure_breaks_early_instead_of_burning_the_bound():
    """Review MAJOR-2: a refresh that RESOLVES UNUSABLY (bid/ask NaN -> cached
    as (now, None)) must end the wait at once. The first draft could not tell
    'resolved and failed' from 'still pending' and burned the whole bound on
    every fire against a broken feed."""
    st = _State(_FakeIBKR(delay=0.2, bid=float("nan"), ask=float("nan")))
    t0 = time.monotonic()
    spot, src = await W._watcher_stock_quote_spot(
        st, "SPY", max_age_ms=10_000, wait_for_refresh_sec=5.0)
    elapsed = time.monotonic() - t0
    assert spot is None and src is None
    assert elapsed < 1.5, (
        f"burned {elapsed:.2f}s of a 5.0s bound after the refresh had already "
        f"resolved — the early-exit condition is not working")


@pytest.mark.asyncio
async def test_stale_entry_inside_TTL_does_not_burn_the_bound():
    """The nastiest case from review MAJOR-2: an entry 2s old is INSIDE the 5s
    TTL so NO refresh is kicked, yet it is too stale for the mirror's 10s bound.
    The loop must not spin the full wait making zero provider calls."""
    ib = _FakeIBKR(delay=30.0)
    st = _State(ib)
    W._stock_spot_state(st)
    st._watcher_stock_spot_cache["SPY"] = (int(time.time() * 1000) - 2_000, None)
    t0 = time.monotonic()
    spot, _ = await W._watcher_stock_quote_spot(
        st, "SPY", max_age_ms=10_000, wait_for_refresh_sec=5.0)
    elapsed = time.monotonic() - t0
    assert spot is None
    assert elapsed < 1.5, f"spun {elapsed:.2f}s with no refresh in flight"
