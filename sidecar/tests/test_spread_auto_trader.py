"""Spread auto-trader — session dedupe and audit-trail labelling.

`spread_auto_trader.py` shipped with ZERO test coverage, which is how the
2026-08-12 defects survived to production. Two of the four are pinned here (the
strike-selection pair live in test_strike_picker.py):

  * THE STATUS-BLIND DEDUPE. `spread_parents_since` counted every parent in
    every status. Parent 927 was refused by our OWN FEF_ALLOW_CREDIT_SPREADS
    gate — marked REJECTED, never transmitted, no position, no exposure — and
    still consumed the one-per-session budget, producing 104 `already_today`
    blocks for the rest of the day. DRY_RUN must keep counting (a rehearsal has
    to exercise the same guard the live path does, the 2026-08-11 finding); a
    self-refusal must not.

  * THE MISLABELLED REASONING. The recommender runs UNGATED by default, so
    `trigger.reasons` holds the conditions that FAILED. Heading that list with
    "PCS trigger:" wrote the reasons it would NOT have fired into the audit log
    as the reasons it DID.
"""

from __future__ import annotations

import datetime
import os
import time
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.db.models import LiveOrder
from fef_sidecar.engine import spread_auto_trader
from fef_sidecar.engine.spread_auto_trader import maybe_auto_trade_spread
from fef_sidecar.engine.spread_entry import SpreadEntryTrigger
from fef_sidecar.engine.spread_recommender import _build_from_trigger_and_pick
from fef_sidecar.engine.strike_picker import StrikePick


ET = spread_auto_trader.ET
# The incident date; a real NYSE trading day, noon ET (inside the entry window).
INCIDENT_ET = datetime.datetime(2026, 8, 12, 12, 0, tzinfo=ET)
# Noon ET = 16:00 UTC. Rows are stored naive-UTC, so this is safely after the
# ET-midnight boundary the dedupe queries from.
INCIDENT_UTC_NAIVE = datetime.datetime(2026, 8, 12, 16, 0)


@pytest.fixture(autouse=True)
async def _db():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


async def _write_parent(status: str | None, *, ib_order_id: int | None = None,
                        ib_perm_id: int | None = None) -> LiveOrder:
    """A credit-spread PARENT row for the incident day in the given status.

    `ib_order_id` / `ib_perm_id` mark an order that actually reached IBKR.
    """
    return await SignalIterRepository.write_live_order(LiveOrder(
        symbol="SPX", strike=7660.0, right="P", expiry="20260812",
        action="SELL", qty=1, order_type="LMT", limit_price=1.50,
        bracket_role="parent", dry_run=(status == "DRY_RUN"), status=status,
        exit_strategy="spread_premium", spread_kind="PCS",
        spread_short_strike=7660.0, spread_long_strike=7650.0,
        spread_initial_credit=1.50, submitted_at=INCIDENT_UTC_NAIVE,
        ib_order_id=ib_order_id, ib_perm_id=ib_perm_id,
    ))


async def _parents_today() -> list[LiveOrder]:
    return await SignalIterRepository.spread_parents_since(
        datetime.datetime(2026, 8, 12, 4, 0)  # ET midnight in naive UTC
    )


# ---------------------------------------------------------------------------
# 1. The dedupe predicate itself
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejected_parent_does_not_consume_the_session() -> None:
    """THE REGRESSION. Parent 927 never reached the broker; it must not count."""
    await _write_parent("REJECTED")
    assert await _parents_today() == []


@pytest.mark.asyncio
async def test_cancelled_parent_does_not_consume_the_session() -> None:
    """Same reasoning as REJECTED: pulled before it could establish anything.

    Both British and American spellings, because IBKR says 'Cancelled' and
    plenty of our own call sites say 'CANCELED'.
    """
    await _write_parent("CANCELLED")
    assert await _parents_today() == []
    await _write_parent("CANCELED")
    assert await _parents_today() == []


@pytest.mark.asyncio
async def test_dry_run_parent_still_consumes_the_session() -> None:
    """Non-negotiable: a rehearsal must exercise the SAME guard the live path
    does, or the rehearsal is not testing the live path (2026-08-11 finding)."""
    await _write_parent("DRY_RUN")
    assert len(await _parents_today()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["SUBMITTED", "FILLED", "PARTIAL", "CLOSED"])
async def test_live_statuses_consume_the_session(status: str) -> None:
    await _write_parent(status)
    assert len(await _parents_today()) == 1, f"{status} must block a second spread"


@pytest.mark.asyncio
async def test_unknown_status_still_counts() -> None:
    """The exclusion is a DENY-list, so a status nobody anticipated blocks
    rather than slips through. Fail closed."""
    await _write_parent("SOME_FUTURE_STATUS")
    assert len(await _parents_today()) == 1


@pytest.mark.asyncio
async def test_null_status_still_counts() -> None:
    """`NOT IN (...)` evaluates NULL as NULL, which SQL drops from the result —
    that would fail OPEN. The predicate has to special-case it."""
    await _write_parent(None)
    assert len(await _parents_today()) == 1


@pytest.mark.asyncio
async def test_rejected_alongside_a_real_parent_still_blocks() -> None:
    """Excluding refusals must not weaken the guard when a real order exists."""
    await _write_parent("REJECTED")
    await _write_parent("SUBMITTED")
    assert len(await _parents_today()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [
    {"ib_order_id": 4242},
    {"ib_perm_id": 987654321},
    {"ib_order_id": 4242, "ib_perm_id": 987654321},
])
async def test_rejected_but_broker_acknowledged_still_blocks(ids: dict) -> None:
    """THE HOLE THAT EXCLUDING 'REJECTED' WOULD OTHERWISE OPEN.

    `place_credit_spread` wraps `ib.placeOrder` AND the DB write that follows
    it in one try/except and marks the parent REJECTED on any exception. So a
    submission that SUCCEEDED but whose bookkeeping failed is stored as
    REJECTED while the order is live at IBKR. A status-only exclusion would
    reopen the session and stack a second spread on a real position.

    Anything carrying a broker id keeps consuming the budget, whatever its
    status says.
    """
    await _write_parent("REJECTED", **ids)
    assert len(await _parents_today()) == 1


@pytest.mark.asyncio
async def test_gate_refused_order_has_no_broker_id_and_is_still_excluded() -> None:
    """The 927 shape specifically: our gate refuses BEFORE `placeOrder`, so
    there is no broker id and the exclusion still applies."""
    await _write_parent("REJECTED", ib_order_id=None, ib_perm_id=None)
    assert await _parents_today() == []


# ---------------------------------------------------------------------------
# 2. End to end — the symptom the operator actually saw
# ---------------------------------------------------------------------------

def _state() -> SimpleNamespace:
    return SimpleNamespace(
        spread_auto_trade_enabled=True,
        spread_auto_trade_observe_only=True,   # nothing is ever sent from a test
        spread_auto_trade_last_date="",
        spread_auto_trade_qty=1,
        spread_puts_only=True,
        spread_require_gate=False,
        spread_rec_max_age_sec=300.0,
        spread_stop_multiplier=1e9,
        spread_tp_close_debit=0.0,
        active_symbol="SPX",
        ibkr=None,
    )


def _rec() -> dict:
    return {
        "kind": "PCS",
        "short_strike": 7660.0,
        "long_strike": 7650.0,
        "spread_width": 10.0,
        "estimated_credit_per_contract": 150.0,   # -> $1.50/share limit
        "session_date": "20260812",
        "symbol": "SPX",
        "as_of_ms": int(time.time() * 1000),
        "reasoning": "test",
    }


@pytest.fixture
def _at_incident(monkeypatch):
    """Freeze the auto-trader's clock inside the incident day's entry window."""
    monkeypatch.setattr(spread_auto_trader, "_now_et", lambda: INCIDENT_ET)


@pytest.mark.asyncio
async def test_self_refused_order_does_not_block_the_rest_of_the_day(
    _at_incident,
) -> None:
    """104 `already_today` blocks came from ONE order our own gate refused.

    The decision after the dedupe is irrelevant here (no ibkr on the test
    state, so it stops later); what matters is that it is no longer the
    dedupe that stops it.
    """
    await _write_parent("REJECTED")
    decision = await maybe_auto_trade_spread(_state(), _rec())
    assert decision.reason_code != "already_today_db", decision.reason


@pytest.mark.asyncio
async def test_a_real_parent_still_blocks_a_second_spread(_at_incident) -> None:
    """The other half of the pin — the guard must still do its job."""
    await _write_parent("SUBMITTED")
    decision = await maybe_auto_trade_spread(_state(), _rec())
    assert decision.fired is False
    assert decision.reason_code == "already_today_db"


@pytest.mark.asyncio
async def test_dry_run_parent_blocks_end_to_end(_at_incident) -> None:
    await _write_parent("DRY_RUN")
    decision = await maybe_auto_trade_spread(_state(), _rec())
    assert decision.reason_code == "already_today_db"


# ---------------------------------------------------------------------------
# 3. The audit-trail label
# ---------------------------------------------------------------------------

FAIL_REASONS = ["EMA8 not above EMA21", "no golden cross", "no bullish engulfing"]


def _pick() -> StrikePick:
    return StrikePick(
        short_strike=7660.0, long_strike=7650.0, short_delta=-0.142,
        short_mid=2.00, short_bid=1.95, short_ask=2.05, width=10.0,
        estimated_credit=1.20, long_mid=0.80,
    )


def test_ungated_reasoning_is_not_labelled_as_a_trigger() -> None:
    """THE REGRESSION. Ungated + not triggered means `reasons` are FAIL
    reasons, and the log read 'PCS trigger: EMA8 not above EMA21 · ...' as
    though those were why it fired."""
    rec = _build_from_trigger_and_pick(
        SpreadEntryTrigger(kind="NONE", reasons=list(FAIL_REASONS)), _pick(), "PCS",
    )
    assert not rec.reasoning.startswith("PCS trigger:")
    assert "UNGATED" in rec.reasoning
    assert "did NOT trigger" in rec.reasoning
    # The reasons are still shown — they are useful, they were just mislabelled.
    assert "EMA8 not above EMA21" in rec.reasoning


def test_a_genuine_trigger_is_still_labelled_a_trigger() -> None:
    """Only the label changed; gating behaviour is untouched."""
    rec = _build_from_trigger_and_pick(
        SpreadEntryTrigger(kind="PCS", reasons=["EMA8 above EMA21", "golden cross"]),
        _pick(), "PCS",
    )
    assert rec.reasoning.startswith("PCS trigger:")
    assert "UNGATED" not in rec.reasoning


def test_fail_reasons_are_machine_readable_only_when_not_triggered() -> None:
    """So post-hoc analysis never has to parse prose to tell a gated fire from
    an ungated one."""
    ungated = _build_from_trigger_and_pick(
        SpreadEntryTrigger(kind="NONE", reasons=list(FAIL_REASONS)), _pick(), "PCS",
    )
    assert ungated.fail_reasons == FAIL_REASONS

    fired = _build_from_trigger_and_pick(
        SpreadEntryTrigger(kind="PCS", reasons=["EMA8 above EMA21"]), _pick(), "PCS",
    )
    assert fired.fail_reasons is None


def test_reasoning_reports_the_net_credit_not_a_mid() -> None:
    """`estimated_credit` has been the NET of both legs since the A1/A2 fix;
    the string still called it 'mid'."""
    rec = _build_from_trigger_and_pick(
        SpreadEntryTrigger(kind="PCS", reasons=["ok"]), _pick(), "PCS",
    )
    assert "net credit $1.20" in rec.reasoning
