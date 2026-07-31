"""Qualified-contract cache + the mirror's qualify budget (2026-07-30) — tests.

THE INCIDENT: the SPY mirror placed ZERO orders from 7/28 on. The fire-time
ask_diag row that finally exposed it read

    stream_opener_present=true, stream_opened=FALSE, stream_ever_ticked=false,
    stream_poll_count=0, stream_wait_ms=3000, stream_error="TimeoutError",
    fallback_ran=true, fallback_source="none"

`LiveIBKRProvider.open_option_quote_stream` re-qualified the mapped SPY option
on EVERY fire under a 3.0s budget — the tightest qualify budget in the repo
(entry 8.0s, watcher 8.0s + cache, price stream 10.0s) — against the slow,
rate-limited TWS contract-details farm the SPX entry had queued a DIFFERENT
contract on ~2s earlier. The snapshot fallback then failed for the same reason,
so the 2026-07-21 streaming-first fix has likely never once served an ask.

Contract under test:
  * ibkr/live.qualified_option_contract — process-lifetime cache, keyed on the
    option identity, caching ONLY successful qualifies (never a negative),
    options only, bounded await. Pattern copied from
    engine/spx_target_watcher._qualified_ib_contract.
  * ibkr/quote_stream.quote_stream_qualify_timeout_sec — 8.0s default, still
    env-overridable, still floored at 0.5s.
  * The open_option_quote_stream wiring: cache used, telemetry stamped on the
    handle, pre-qualified contracts still do ZERO round-trips, disconnected
    still raises promptly.
  * spy_mirror: qualify telemetry lands in ask_diag, and the refusal semantics
    are UNCHANGED — both legs dead still means a no_fresh_quote SKIP with no
    order placed.
"""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from sqlmodel import select

from fef_sidecar.config_store import UIConfig
from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import AuditLog, LiveOrder
from fef_sidecar.engine import spy_mirror as sm
from fef_sidecar.engine.spy_mirror import maybe_place_spy_mirror
from fef_sidecar.ibkr import live as live_mod
from fef_sidecar.ibkr import quote_stream as qs
from fef_sidecar.ibkr.live import LiveIBKRProvider, qualified_option_contract
from fef_sidecar.ibkr.mock import MockOptionQuoteStream
from fef_sidecar.ibkr.models import Contract, ContractKind

_TODAY = time.strftime("%Y%m%d")
_SPY_SPOT = 745.00


# ===========================================================================
# Doubles
# ===========================================================================


class _FakeEvent:
    """ib_async Event stand-in supporting the += / -= handler protocol."""

    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, h):
        self.handlers.append(h)
        return self

    def __isub__(self, h):
        if h in self.handlers:
            self.handlers.remove(h)
        return self


class _FakeTicker:
    def __init__(self) -> None:
        self.updateEvent = _FakeEvent()
        self.bid = None
        self.ask = None


class _FakeIB:
    """Duck-typed ib_async.IB covering ONLY the qualify/subscribe path.

    qualifyContractsAsync mimics the real one faithfully in the two ways this
    fix depends on: it MUTATES each contract in place (stamping a conId), and
    on an unknown/ambiguous contract it does NOT raise — it returns a None slot
    and leaves conId at 0. That silent-failure asymmetry is exactly why the
    cache checks conId instead of "did it raise"."""

    def __init__(self, *, connected: bool = True, delay_sec: float = 0.0,
                 fail_times: int = 0, unknown: bool = False) -> None:
        self._connected = connected
        self.delay_sec = delay_sec
        self.fail_times = fail_times
        self.unknown = unknown
        self.qualify_calls = 0
        self.subscribed: list = []
        self.cancelled: list = []
        self.md_type: int | None = None
        self._next_con_id = 500_000

    def isConnected(self) -> bool:
        return self._connected

    async def qualifyContractsAsync(self, *contracts):
        self.qualify_calls += 1
        if self.delay_sec:
            await asyncio.sleep(self.delay_sec)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("contract-details farm wedged (scripted)")
        if self.unknown:
            return [None for _ in contracts]
        out = []
        for c in contracts:
            self._next_con_id += 1
            c.conId = self._next_con_id
            out.append(c)
        return out

    def reqMarketDataType(self, t: int) -> None:
        self.md_type = t

    def reqMktData(self, contract, *_a, **_kw) -> _FakeTicker:
        self.subscribed.append(contract)
        return _FakeTicker()

    def cancelMktData(self, contract) -> None:
        self.cancelled.append(contract)


def _opt(symbol: str = "SPY", strike: float = 749.0, right: str = "C",
         expiry: str = "20260730") -> Contract:
    return Contract(symbol=symbol, kind=ContractKind.OPTION, exchange="SMART",
                    expiry=expiry, strike=strike, right=right)


def _provider(ib) -> LiveIBKRProvider:
    """A LiveIBKRProvider wired to a fake IB — constructed, never connected."""
    p = LiveIBKRProvider()
    p._ib = ib
    return p


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """The cache is PROCESS-lifetime by design, so it must be cleared on both
    sides of every test — a leaked entry would silently satisfy another test's
    round-trip assertions (here or in another module)."""
    for k in ("FEF_QUOTE_STREAM_MAX_AGE_SEC",
              "FEF_QUOTE_STREAM_FIRST_TICK_WAIT_SEC",
              "FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC",
              "FEF_SPY_MIRROR_ENABLED", "FEF_LIVE_TRADING"):
        monkeypatch.delenv(k, raising=False)
    live_mod.clear_qualified_contract_cache()
    yield
    live_mod.clear_qualified_contract_cache()


@pytest.fixture
async def _db():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


# ===========================================================================
# A. The cache itself
# ===========================================================================


async def test_cache_hit_avoids_a_second_round_trip() -> None:
    """THE POINT: the same option is qualified against the CDS farm once per
    process, not once per fire."""
    ib = _FakeIB()
    first = await qualified_option_contract(ib, _opt(), timeout_sec=8.0)
    assert ib.qualify_calls == 1
    assert first.cached is False
    assert first.contract.conId > 0

    second = await qualified_option_contract(ib, _opt(), timeout_sec=8.0)
    assert ib.qualify_calls == 1          # no second round-trip
    assert second.cached is True
    assert second.elapsed_ms == 0
    assert second.contract.conId == first.contract.conId


async def test_cache_hit_hands_out_a_copy_not_the_template() -> None:
    """A consumer mutating what it got back must not poison the cache (qualify
    itself writes THROUGH the object it is handed)."""
    ib = _FakeIB()
    first = await qualified_option_contract(ib, _opt(), timeout_sec=8.0)
    con_id = first.contract.conId
    first.contract.conId = 0              # a consumer scribbles on its copy
    second = await qualified_option_contract(ib, _opt(), timeout_sec=8.0)
    assert second.cached is True
    assert second.contract.conId == con_id
    assert second.contract is not first.contract


async def test_cache_key_separates_symbol_expiry_strike_right() -> None:
    ib = _FakeIB()
    await qualified_option_contract(ib, _opt(), timeout_sec=8.0)
    assert ib.qualify_calls == 1
    for variant in (
        _opt(strike=750.0),
        _opt(right="P"),
        _opt(expiry="20260731"),
        _opt(symbol="SPX"),
    ):
        before = ib.qualify_calls
        r = await qualified_option_contract(ib, variant, timeout_sec=8.0)
        assert r.cached is False
        assert ib.qualify_calls == before + 1
    # ...and every one of them is now individually cached.
    calls = ib.qualify_calls
    for variant in (_opt(), _opt(strike=750.0), _opt(right="P"),
                    _opt(expiry="20260731"), _opt(symbol="SPX")):
        assert (await qualified_option_contract(
            ib, variant, timeout_sec=8.0)).cached is True
    assert ib.qualify_calls == calls


async def test_raised_qualify_is_not_cached_as_a_negative() -> None:
    """A transient CDS outage must not permanently poison a strike."""
    ib = _FakeIB(fail_times=1)
    with pytest.raises(RuntimeError):
        await qualified_option_contract(ib, _opt(), timeout_sec=8.0)
    assert ib.qualify_calls == 1

    retry = await qualified_option_contract(ib, _opt(), timeout_sec=8.0)
    assert ib.qualify_calls == 2          # RETRIED, not served from a negative
    assert retry.cached is False
    assert retry.contract.conId > 0
    assert (await qualified_option_contract(
        ib, _opt(), timeout_sec=8.0)).cached is True
    assert ib.qualify_calls == 2


async def test_timed_out_qualify_is_not_cached_and_retries() -> None:
    """The exact shape of the live incident: the qualify blows its budget."""
    ib = _FakeIB(delay_sec=5.0)
    with pytest.raises(asyncio.TimeoutError):
        await qualified_option_contract(ib, _opt(), timeout_sec=0.05)
    assert ib.qualify_calls == 1

    ib.delay_sec = 0.0
    retry = await qualified_option_contract(ib, _opt(), timeout_sec=8.0)
    assert ib.qualify_calls == 2
    assert retry.cached is False
    assert retry.contract.conId > 0


async def test_unknown_contract_is_returned_but_never_cached() -> None:
    """qualifyContractsAsync does not raise on an unknown/ambiguous contract —
    it returns a None slot and leaves conId 0. That is a FAILURE and must not
    be cached, while the contract is still returned.

    NOTE (review MINOR): this fake does NOT model what production does next.
    Real ib_async reqMktData -> startTicker keys by hash(contract) == conId and
    RAISES ValueError at conId 0, so the subscribe never happens; the mirror's
    blanket except then routes to the snapshot fallback. Behaviour is identical
    to main either way, so this is a fidelity gap in the fake, not a regression
    — but do not read this test as documenting a live subscribe-then-refuse
    path, because there isn't one."""
    ib = _FakeIB(unknown=True)
    r = await qualified_option_contract(ib, _opt(), timeout_sec=8.0)
    assert r.cached is False
    assert int(getattr(r.contract, "conId", 0) or 0) == 0

    ib.unknown = False
    again = await qualified_option_contract(ib, _opt(), timeout_sec=8.0)
    assert ib.qualify_calls == 2          # retried, not poisoned
    assert again.contract.conId > 0


async def test_malformed_option_raises_the_same_error_and_caches_nothing() -> None:
    """An option missing its strike must still raise _to_ib_contract's
    ValueError — the cache key may never turn it into a different exception."""
    ib = _FakeIB()
    bad = Contract(symbol="SPY", kind=ContractKind.OPTION, exchange="SMART",
                   expiry="20260730", strike=None, right="C")
    with pytest.raises(ValueError):
        await qualified_option_contract(ib, bad, timeout_sec=8.0)
    assert ib.qualify_calls == 0


async def test_non_option_contracts_are_never_cached() -> None:
    """A ContFuture qualifies to the CURRENT front month, which ROLLS —
    caching it would serve a dead contract after the roll."""
    ib = _FakeIB()
    fut = Contract(symbol="ES", kind=ContractKind.FUTURE, exchange="CME")
    for _ in range(3):
        r = await qualified_option_contract(ib, fut, timeout_sec=8.0)
        assert r.cached is False
    assert ib.qualify_calls == 3


# ===========================================================================
# B. The budget
# ===========================================================================


def test_qualify_timeout_default_is_8s_and_still_env_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """8.0s matches engine/live_trading (FEF_ENTRY_QUALIFY_TIMEOUT_SEC) and
    engine/spx_target_watcher (FEF_WATCHER_QUALIFY_TIMEOUT_SEC). 3.0s was the
    tightest qualify budget in the repo and became the binding constraint on the
    mirror once the 7/29 fresh-or-skip spot race was fixed (2026-07-30: 09:45
    stream_wait_ms=3000 TimeoutError -> skip; 10:48 stream_wait_ms=141 -> placed)."""
    assert qs.quote_stream_qualify_timeout_sec() == pytest.approx(8.0)
    monkeypatch.setenv("FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC", "1.5")
    assert qs.quote_stream_qualify_timeout_sec() == pytest.approx(1.5)
    # The 0.5s floor survives.
    monkeypatch.setenv("FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC", "0.01")
    assert qs.quote_stream_qualify_timeout_sec() == pytest.approx(0.5)
    # Garbage never crashes the fire path — back to the default.
    monkeypatch.setenv("FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC", "garbage")
    assert qs.quote_stream_qualify_timeout_sec() == pytest.approx(8.0)


def test_qualify_timeout_is_clamped_at_15s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review MINOR: the knob had a 0.5s FLOOR but NO CEILING, so
    FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC=600 would hold the SPX firing lock for
    ten minutes. The sibling fire-hook knob (spy_mirror._mirror_spot_wait_sec)
    was clamped to 10.0s by the previous commit's reviewer for exactly this
    reason; this knob sits on the SAME hook with a 2.7x larger default."""
    monkeypatch.setenv("FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC", "600")
    assert qs.quote_stream_qualify_timeout_sec() == pytest.approx(15.0)
    monkeypatch.setenv("FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC", "15.0")
    assert qs.quote_stream_qualify_timeout_sec() == pytest.approx(15.0)
    # Just under the ceiling passes through untouched.
    monkeypatch.setenv("FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC", "14.9")
    assert qs.quote_stream_qualify_timeout_sec() == pytest.approx(14.9)
    # inf must not defeat the clamp.
    monkeypatch.setenv("FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC", "inf")
    assert qs.quote_stream_qualify_timeout_sec() <= 15.0


# ===========================================================================
# C. Provider wiring (open_option_quote_stream)
# ===========================================================================


async def test_open_stream_uses_the_cache_and_stamps_telemetry() -> None:
    ib = _FakeIB()
    p = _provider(ib)
    s1 = await p.open_option_quote_stream(_opt())
    assert ib.qualify_calls == 1
    assert s1.qualify_cached is False
    assert s1.qualify_ms is not None and s1.qualify_ms >= 0

    s2 = await p.open_option_quote_stream(_opt())
    assert ib.qualify_calls == 1          # served from cache
    assert s2.qualify_cached is True
    assert s2.qualify_ms == 0
    # Both subscriptions still happened on their own contract objects.
    assert len(ib.subscribed) == 2
    assert ib.subscribed[0].conId == ib.subscribed[1].conId
    s1.close()
    s2.close()
    assert len(ib.cancelled) == 2


async def test_prequalified_contract_still_does_zero_round_trips() -> None:
    """The SPX chase path hands in its already-qualified contract — it must
    neither qualify nor touch the cache."""
    ib = _FakeIB()
    p = _provider(ib)
    pre = SimpleNamespace(conId=987654, symbol="SPX")
    s = await p.open_option_quote_stream(_opt(symbol="SPX"), ib_contract=pre)
    assert ib.qualify_calls == 0
    assert ib.subscribed == [pre]
    assert s.qualify_cached is None       # "this transport never qualified"
    assert s.qualify_ms is None


async def test_disconnected_provider_raises_promptly_and_never_qualifies() -> None:
    """Fail-soft contract preserved: no reconnect backoff at fire time, and
    the caller's fallback gets control immediately."""
    ib = _FakeIB(connected=False)
    p = _provider(ib)
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        await p.open_option_quote_stream(_opt())
    assert time.monotonic() - t0 < 0.5
    assert ib.qualify_calls == 0
    assert ib.subscribed == []

    p_none = LiveIBKRProvider()           # _ib is None (never connected)
    with pytest.raises(ConnectionError):
        await p_none.open_option_quote_stream(_opt())


async def test_slow_qualify_under_the_new_budget_now_succeeds() -> None:
    """THE REGRESSION that reproduces the 7/30 incident. A 4.0s qualify — well
    within the CDS farm's queued-behind-the-entry latency — raised TimeoutError
    under the old 3.0s default, so stream_opened was false and the mirror
    starved. Under 8.0s it succeeds.

    Deliberately a REAL sleep: the number under test IS a wall-clock budget and
    a mocked clock would prove nothing about it. 4.0s sits ~1s above the old
    budget (measured against main: TimeoutError at ~3.3s, Windows timer slack
    included) and 4s below the new one, so neither verdict is marginal."""
    ib = _FakeIB(delay_sec=4.0)
    p = _provider(ib)
    s = await p.open_option_quote_stream(_opt())
    assert s is not None
    assert s.qualify_cached is False
    assert s.qualify_ms is not None and s.qualify_ms >= 3_500
    assert len(ib.subscribed) == 1


async def test_warm_cache_serves_a_budget_that_would_time_out_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache is the second, independent line of defence: once a strike is
    qualified, a budget far TIGHTER than the round-trip still serves it."""
    ib = _FakeIB(delay_sec=0.3)
    p = _provider(ib)
    await p.open_option_quote_stream(_opt())          # warms the cache (0.3s)
    monkeypatch.setenv("FEF_QUOTE_STREAM_QUALIFY_TIMEOUT_SEC", "0.05")
    t0 = time.monotonic()
    s = await p.open_option_quote_stream(_opt())      # would time out cold
    assert time.monotonic() - t0 < 0.05
    assert s.qualify_cached is True
    assert ib.qualify_calls == 1


# ===========================================================================
# D. SPY mirror — telemetry lands, refusal semantics unchanged
# ===========================================================================
#
# Harness mirrors tests/test_streaming_quotes.py section C (same config/expiry
# patches, same state shape) so the two files can be read side by side.


def _patch_config(monkeypatch: pytest.MonkeyPatch, **kw):
    import fef_sidecar.config_store as cs
    cfg = UIConfig(**kw)
    monkeypatch.setattr(cs, "load_config", lambda: cfg)
    return cfg


def _patch_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    import fef_sidecar.engine.expiry_resolver as er

    async def fake_resolve(state, symbol):
        return _TODAY

    monkeypatch.setattr(er, "resolve_nearest_expiry", fake_resolve)
    monkeypatch.setattr(er, "_today_et_ymd", lambda: _TODAY)
    monkeypatch.setattr(sm, "_now_et_hhmm_ymd", lambda: ("10:00", _TODAY))


def _mirror_state(**kw) -> SimpleNamespace:
    d = dict(
        auto_trade_count_by_symbol={},
        auto_trade_last_at_ms_by_symbol={},
        auto_trade_count_session=0,
        auto_trade_max_per_symbol=2,
        active_symbol="SPX",
        last_live_tick_spx=7500.0,
        last_spx_price=7500.0,
        exit_dollar_t1=1000.0, exit_dollar_t2=2500.0,
        exit_dollar_t3=5000.0, exit_dollar_stop=-1000.0,
        ibkr=SimpleNamespace(),
    )
    d.update(kw)
    s = SimpleNamespace(**d)
    s._watcher_stock_spot_cache = {"SPY": (int(time.time() * 1000), _SPY_SPOT)}
    s._watcher_stock_spot_refreshing = set()
    s._watcher_stock_spot_tasks = set()
    return s


def _mirror_kwargs(**kw) -> dict:
    d = dict(
        direction="LONG_CALLS", spx_strike=7540.0, spx_qty=3,
        spx_parent_id=42, source="auto_scanner", strategy="spx_points",
        spx_t1=7515.0, spx_t2=7525.0, spx_t3=7550.0, spx_stop=7490.0,
        entry_timeout_sec=45.0, spx_spot_ref=7500.0, spx_entry_ask=5.1,
    )
    d.update(kw)
    return d


async def _mirror_audit_rows() -> list[AuditLog]:
    async with get_session() as session:
        rows = (await session.exec(
            select(AuditLog).where(AuditLog.category == "spy_mirror"))).all()
    return list(rows)


async def _live_orders() -> list[LiveOrder]:
    async with get_session() as session:
        return list((await session.exec(select(LiveOrder))).all())


async def test_mirror_records_qualify_telemetry_on_a_placed_fire(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """ask_diag carries the qualify outcome on the SUCCESS row too — without a
    base rate the next incident is blind again (the reason this defect went
    unseen from 7/21 to 7/29)."""
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)
    handle = MockOptionQuoteStream()
    handle.push_quote(0.50, 0.55)
    handle.qualify_cached = True
    handle.qualify_ms = 7

    async def opener(contract, *, ib_contract=None, qualify_timeout_sec=None):
        return handle

    state = _mirror_state(ibkr=SimpleNamespace(open_option_quote_stream=opener))
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["ok"] is True
    placed = [a for a in await _mirror_audit_rows()
              if "PLACED" in a.summary]
    assert len(placed) == 1
    diag = placed[0].details["ask_diag"]
    assert diag["qualify_cached"] is True
    assert diag["qualify_ms"] == 7
    assert diag["resolved_source"] == "stream"


async def test_mirror_still_skips_with_no_order_when_both_legs_fail(
    monkeypatch: pytest.MonkeyPatch, _db,
) -> None:
    """UNCHANGED refusal: stream dead AND fallback dead -> no_fresh_quote SKIP,
    no order on a stale/guessed quote — with the qualify telemetry recorded on
    the skip row."""
    _patch_config(monkeypatch)
    _patch_expiry(monkeypatch)

    async def dead_opener(contract, *, ib_contract=None, qualify_timeout_sec=None):
        raise asyncio.TimeoutError()

    async def dead_quote(contract):
        return SimpleNamespace(bid=None, ask=None, last=None)

    state = _mirror_state(ibkr=SimpleNamespace(
        open_option_quote_stream=dead_opener, get_option_quote=dead_quote))
    r = await maybe_place_spy_mirror(state, **_mirror_kwargs())
    assert r["skipped"] == "no_fresh_quote"
    assert await _live_orders() == []
    skips = [a for a in await _mirror_audit_rows() if "SKIPPED" in a.summary]
    assert len(skips) == 1
    diag = skips[0].details["ask_diag"]
    # The qualify never completed -> telemetry is present but None, and the
    # stream error is what names the failure.
    assert diag["qualify_cached"] is None
    assert diag["qualify_ms"] is None
    assert diag["stream_opened"] is False
    assert diag["fallback_ran"] is True
