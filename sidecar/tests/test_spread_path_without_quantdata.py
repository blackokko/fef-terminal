"""The credit-spread path survives the QuantData removal — end to end.

[REMOVED 2026-08-25] QuantData subscription CANCELLED. THIS IS THE TEST THAT
GUARDS THE ONE THING THE OPERATOR KEPT.

Before the removal the credit-spread recommendation and the credit-spread
auto-trade both executed INSIDE `loop._run_one_iter`, purely by position —
lines 1366-1440 and 1485-1525 of a function that began with

    snap = await state.quantdata.fetch_market_snapshot()
    inputs = snapshot_to_inputs(snap)          # <-- raises on None

Neither block had a single QuantData import; only their enclosing function did.
So deleting the vendor fetch naively would have killed the spread bot at the
`snapshot_to_inputs` line, silently, without a failing test anywhere — the
directional bot and the credit-spread bot would have died together.

They are now `loop._build_spread_recommendation` / `loop._run_spread_auto_trade`,
driven by `loop._run_spread_iter` on its own schedule. This module pins that the
whole path runs with:

  * NO QuantData client anywhere in the process (`fef_sidecar.quantdata` does not
    import; `AppState` has no `quantdata` attribute),
  * NO market snapshot (`AppState` has no `last_snapshot` attribute),
  * NO FEF verdict and NO engine push,

and still (a) produces a real PCS recommendation off IBKR bars + option quotes,
and (b) hands exactly that recommendation to `maybe_auto_trade_spread`.
"""

from __future__ import annotations

import importlib
import os
import time
from datetime import datetime, timedelta

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["FEF_PROVIDER_IBKR"] = "mock"

from fef_sidecar.db import dispose_db, init_db
from fef_sidecar.engine import spread_auto_trader, strike_picker
from fef_sidecar.ibkr.models import Bar, Contract, OptionQuote
from fef_sidecar.loop import _run_spread_iter, _spread_spot
from fef_sidecar.state import AppState

SPOT = 7700.0

# The picker starts 5 points below spot and steps by `width` (10), so the PCS
# ladder is 7695, 7685, 7675 ... With delta_max 0.15 and delta_floor_frac 0.5
# the accept band is |delta| in [0.075, 0.15]: 7695 is the NEAREST qualifying
# short strike and 7685 is its wing.
_DELTAS = {7695.0: 0.140, 7685.0: 0.085, 7675.0: 0.040, 7665.0: 0.020}
_MIDS = {7695.0: 3.10, 7685.0: 1.90, 7675.0: 0.90, 7665.0: 0.40}


@pytest.fixture(autouse=True)
async def _db():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


@pytest.fixture(autouse=True)
def _clear_strike_cache():
    strike_picker.clear_strike_cache()
    yield
    strike_picker.clear_strike_cache()


class _FakeIBKR:
    """Serves 30-min bars and a put delta ladder. Nothing else — this is the
    ONLY market-data provider left in the process."""

    account_class = "paper"
    account_id = "DU000000"

    def __init__(self) -> None:
        self.bar_calls: list[tuple[str, str, str]] = []
        self.quote_strikes: list[float] = []

    async def fetch_historical_bars(self, contract, *, bar_size: str, duration: str) -> list[Bar]:
        self.bar_calls.append((contract.symbol, bar_size, duration))
        t0 = datetime(2026, 8, 25, 10, 0)
        # A clean uptrend, so the PCS trigger has something coherent to report.
        return [
            Bar(timestamp=t0 + timedelta(minutes=30 * i),
                open=7660.0 + 4 * i, high=7666.0 + 4 * i,
                low=7656.0 + 4 * i, close=7664.0 + 4 * i, volume=1000)
            for i in range(40)
        ]

    async def get_option_quote(self, contract: Contract) -> OptionQuote | None:
        strike = float(contract.strike)
        self.quote_strikes.append(strike)
        d = _DELTAS.get(strike)
        if d is None:
            return None
        mid = _MIDS[strike]
        return OptionQuote(
            timestamp=datetime.now(), contract=contract, last=mid,
            bid=mid - 0.05, ask=mid + 0.05,
            delta=-d if contract.right == "P" else d,
        )


def _state() -> AppState:
    state = AppState.build()
    state.ibkr = _FakeIBKR()  # type: ignore[assignment]
    state.active_symbol = "SPX"
    # The IBKR tick is the only spot source left (loop._spread_spot).
    state.last_live_tick_spx = SPOT
    state.last_live_tick_ms = int(time.time() * 1000)
    state.last_live_tick_symbol = "SPX"
    return state


# ---------------------------------------------------------------------------
# 1. The vendor really is gone
# ---------------------------------------------------------------------------

def test_quantdata_package_does_not_import() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("fef_sidecar.quantdata")


def test_appstate_carries_no_quantdata_client_and_no_snapshot() -> None:
    state = _state()
    assert not hasattr(state, "quantdata"), "AppState still holds a QuantData client"
    assert not hasattr(state, "last_snapshot"), "AppState still caches a MarketSnapshot"
    assert not hasattr(state, "quantdata_poll_task")


# ---------------------------------------------------------------------------
# 2. THE DELIVERABLE: the spread path runs end to end without any of it
# ---------------------------------------------------------------------------

async def test_spread_iter_recommends_and_reaches_auto_trader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_spread_iter -> recommendation -> maybe_auto_trade_spread.

    The auto-trader is SPY-WRAPPED, not stubbed: the real gate stack still
    runs. We assert only that it was reached, and with the exact dict the
    recommender built.
    """
    state = _state()

    seen: list[dict | None] = []
    real = spread_auto_trader.maybe_auto_trade_spread

    async def _spy(st, rec, **kw):
        seen.append(rec)
        return await real(st, rec, **kw)

    monkeypatch.setattr(spread_auto_trader, "maybe_auto_trade_spread", _spy)

    await _run_spread_iter(state)

    # -- the recommendation was actually built, off IBKR data only ----------
    rec = state.last_spread_recommendation
    assert rec is not None, "no recommendation produced"
    assert rec["kind"] == "PCS", f"expected a put credit spread, got {rec['kind']}: {rec['reasoning']}"
    assert rec["short_strike"] == 7695.0
    assert rec["long_strike"] == 7685.0
    assert rec["spread_width"] == 10.0
    assert rec["estimated_credit_per_contract"] > 0
    assert rec["symbol"] == "SPX"
    # Provenance stamps the auto-trader's staleness gates depend on.
    assert rec["session_date"] and rec["as_of_ms"] > 0

    # -- it came from bars + option quotes, i.e. IBKR ----------------------
    assert state.ibkr.bar_calls == [("SPX", "30 mins", "5 D")]  # type: ignore[attr-defined]
    assert 7695.0 in state.ibkr.quote_strikes  # type: ignore[attr-defined]

    # -- and it reached the auto-trader, unchanged -------------------------
    assert len(seen) == 1, "maybe_auto_trade_spread was not reached"
    assert seen[0] is rec, "the auto-trader saw a different object than the recommender built"

    # The real gate stack ran and recorded its decision (default state is
    # disabled, so it stops at gate 1 — what matters is that it got there).
    assert state.spread_auto_trade_last_reason_code == "disabled"
    assert state.spread_auto_trade_last_eval_at_ms > 0


async def test_recommendation_passes_every_structural_gate_of_the_auto_trader() -> None:
    """With the master switch ON, the built recommendation must not be refused
    by any gate that judges the STRUCTURE itself.

    Gate 1 (disabled) is what stops the default path, so it would hide a
    malformed recommendation. Here we turn the switch on and assert the denial
    (if any) is a session/clock/broker-policy gate, never a structural one —
    the class of failure that would mean the extraction broke the payload.
    """
    state = _state()
    state.spread_auto_trade_enabled = True

    await _run_spread_iter(state)
    rec = state.last_spread_recommendation
    assert rec is not None and rec["kind"] == "PCS"

    decision = await spread_auto_trader.maybe_auto_trade_spread(
        state, rec, source="test",
    )
    structural = {
        "no_rec", "rec_none", "rec_incomplete", "calls_disabled",
        "no_width", "limit_out_of_range", "limit_implausibly_small",
        "rec_stale", "rec_symbol_mismatch", "rec_too_old",
    }
    assert decision.reason_code not in structural, (
        f"structural gate refused the extracted recommendation: "
        f"{decision.reason_code} — {decision.reason}"
    )


async def test_spread_iter_survives_a_broken_recommendation() -> None:
    """The two halves keep independent try/except: a recommender fault must not
    stop the auto-trade evaluation, and neither may escape to the driver."""
    state = _state()

    async def _boom(*a, **kw):
        raise RuntimeError("bar fetch exploded")

    state.ibkr.fetch_historical_bars = _boom  # type: ignore[attr-defined]

    await _run_spread_iter(state)  # must not raise

    # _get_cached_bars swallows the fetch fault and returns [], so the
    # recommender reports the no-history case rather than crashing.
    assert state.last_spread_recommendation is None or (
        state.last_spread_recommendation.get("kind") == "NONE"
    )
    # The auto-trader still ran and still recorded a decision.
    assert state.spread_auto_trade_last_eval_at_ms > 0


async def test_no_bars_caches_no_recommendation() -> None:
    """With no bar history there is nothing to recommend, and — importantly —
    `last_spread_recommendation` is NOT refreshed, so a stale structure from an
    earlier session can never be traded (review finding B2)."""
    state = _state()

    async def _no_bars(*a, **kw):
        return []

    state.ibkr.fetch_historical_bars = _no_bars  # type: ignore[attr-defined]

    await _run_spread_iter(state)
    assert state.last_spread_recommendation is None


# ---------------------------------------------------------------------------
# 3. _spread_spot — the replacement for the QuantData snapshot price
# ---------------------------------------------------------------------------

def _bars(*closes: float) -> list[Bar]:
    t0 = datetime(2026, 8, 25, 10, 0)
    return [
        Bar(timestamp=t0 + timedelta(minutes=30 * i), open=c, high=c, low=c,
            close=c, volume=1)
        for i, c in enumerate(closes)
    ]


def test_spread_spot_prefers_a_fresh_same_symbol_tick() -> None:
    state = _state()
    assert _spread_spot(state, _bars(1.0, 2.0)) == pytest.approx(SPOT)


def test_spread_spot_ignores_a_stale_tick() -> None:
    state = _state()
    state.last_live_tick_ms = int(time.time() * 1000) - 61_000
    assert _spread_spot(state, _bars(7601.0, 7602.5)) == pytest.approx(7602.5)


def test_spread_spot_ignores_a_tick_stamped_by_another_symbol() -> None:
    """The 2026-07-23 MAJOR-2 guard: after a /config/symbol flip the cached
    tick belongs to the PREVIOUS symbol. Pricing a QQQ spread off a frozen SPX
    print is exactly how a wrong-symbol level became a real strike."""
    state = _state()
    state.active_symbol = "QQQ"
    state.last_live_tick_symbol = "SPX"
    assert _spread_spot(state, _bars(730.0, 731.25)) == pytest.approx(731.25)


def test_spread_spot_returns_zero_when_nothing_is_available() -> None:
    state = _state()
    state.last_live_tick_spx = 0.0
    state.last_live_tick_ms = 0
    state.last_spx_price = 0.0
    assert _spread_spot(state, []) == 0.0
