"""Broker-message capture on entry stand-down + the broker-measured commission.

Both land 2026-07-29 from the FOMC-session incident: two HIGH fires went MKT,
sat, hit the 45s timeout, and left NO recoverable reason anywhere in our data.
ib_async had the reason in `trade.log` the whole time.
"""
from dataclasses import dataclass

import pytest

from fef_sidecar.engine.entry_timeout import _broker_messages, _broker_reason_tag


@dataclass
class _Entry:
    """Mirrors ib_async.objects.TradeLogEntry (time, status, message, errorCode)."""
    status: str = ""
    message: str = ""
    errorCode: int = 0


class _Trade:
    def __init__(self, log):
        self.log = log


def test_plain_status_transitions_are_ignored():
    """errorCode==0 entries are ordinary status history, not reasons."""
    t = _Trade([_Entry("PendingSubmit", "", 0), _Entry("Submitted", "", 0)])
    assert _broker_messages(t) == []
    assert _broker_reason_tag([]) == ""


def test_precaution_warning_is_captured_with_validation_error_status():
    """THE incident case: a TWS precaution arrives as a warning that also sets
    orderStatus.status='ValidationError' (which is in ib_async ActiveStates, so
    the order looks merely unfilled)."""
    t = _Trade([
        _Entry("PendingSubmit", "", 0),
        _Entry("ValidationError",
               "Warning 2109, reqId 42: Order Event Warning: price-based "
               "volatility risk exceeds the configured threshold", 2109),
    ])
    msgs = _broker_messages(t)
    assert len(msgs) == 1
    assert msgs[0]["errorCode"] == 2109
    assert msgs[0]["status"] == "ValidationError"
    assert "volatility" in msgs[0]["message"]
    assert any(m["status"] == "ValidationError" for m in msgs)
    tag = _broker_reason_tag(msgs)
    assert tag.startswith(" | broker: ValidationError 2109:")


def test_hard_rejection_is_captured():
    t = _Trade([_Entry("Cancelled", "Error 201, reqId 7: Order rejected - "
                       "reason: precautionary constraints", 201)])
    msgs = _broker_messages(t)
    assert msgs[0]["errorCode"] == 201
    assert "rejected" in msgs[0]["message"]


def test_message_list_is_bounded_AFTER_filtering():
    """Review MAJOR-2a: the bound must apply to MESSAGES, not raw entries.
    Slicing the raw log first let ~25 trailing status transitions push the only
    real reason out of the window entirely."""
    log = [_Entry("ValidationError", f"warn {i}", 2100 + i) for i in range(40)]
    msgs = _broker_messages(_Trade(log))
    assert len(msgs) == 20
    assert msgs[-1]["errorCode"] == 2139       # newest kept

    # THE regression: one real reason buried under 25 no-code transitions.
    buried = ([_Entry("ValidationError", "THE REASON", 2109)]
              + [_Entry("Submitted", "Fill 1@2.00", 0) for _ in range(25)])
    msgs2 = _broker_messages(_Trade(buried))
    assert [m["errorCode"] for m in msgs2] == [2109], (
        "the only diagnostic message was dropped by the bound")


def test_our_own_cancel_ack_never_masquerades_as_the_reason():
    """Review MAJOR-1, the defect that defeated this feature's purpose.

    TWS acks the cancel THIS module requests with error 202, which ib_async
    appends to trade.log. Because the cancel-confirm loop spins until the status
    is terminal and the 202 handler is what makes it terminal, a 202 is present
    on essentially EVERY stand-down and is always the NEWEST entry. Taking
    msgs[-1] therefore reported our own cancel instead of the precaution."""
    from fef_sidecar.engine.entry_timeout import (
        _broker_summary_clause, _primary_broker_reason,
    )
    incident = _Trade([
        _Entry("PendingSubmit", "", 0),
        _Entry("Submitted", "", 0),
        _Entry("ValidationError", "Warning 2109: price-based volatility risk", 2109),
        _Entry("PendingCancel", "", 0),
        _Entry("Cancelled", "Error 202: Order Canceled - Reason:", 202),
    ])
    msgs = _broker_messages(incident)
    assert [m["errorCode"] for m in msgs] == [2109, 202]
    assert _primary_broker_reason(msgs)["errorCode"] == 2109
    assert "2109" in _broker_reason_tag(msgs)
    assert "202" not in _broker_reason_tag(msgs)
    assert "volatility" in _broker_summary_clause(msgs)


def test_cancel_ack_only_is_reported_as_no_diagnosis_not_as_a_reason():
    """A bare 202 must NOT be dressed up as a broker reason — that put a useless
    '| broker: Cancelled 202' on every stood-down parent row."""
    from fef_sidecar.engine.entry_timeout import (
        _broker_summary_clause, _primary_broker_reason,
    )
    only_ack = _Trade([_Entry("Cancelled", "Error 202: Order Canceled", 202)])
    msgs = _broker_messages(only_ack)
    assert msgs and _primary_broker_reason(msgs) is None
    assert _broker_reason_tag(msgs) == ""
    clause = _broker_summary_clause(msgs)
    assert "our own cancel ack" in clause and "202" in clause


def test_empty_log_summary_does_NOT_claim_the_order_was_accepted():
    """Review MAJOR-2c/d: reqId=-1 connectivity and market-data-farm errors never
    attach to a Trade, and a TWS-side precaution dialog can block transmission
    with no API error at all. An empty log means WE SAW NOTHING."""
    from fef_sidecar.engine.entry_timeout import _broker_summary_clause
    clause = _broker_summary_clause([])
    assert "does NOT prove" in clause
    assert "accepted and simply did not fill" not in clause
    assert "reqId=-1" in clause


def test_one_bad_entry_does_not_discard_the_others():
    """Review MINOR-1: the loop-level try returned [] on a single unparseable
    entry, which then made the caller assert 'no broker message'."""
    class _Hostile:
        status = "ValidationError"
        message = "boom"

        @property
        def errorCode(self):
            raise RuntimeError("ib_async internals changed")

    t = _Trade([_Entry("ValidationError", "THE REASON", 2109), _Hostile()])
    msgs = _broker_messages(t)
    assert [m["errorCode"] for m in msgs] == [2109]


def test_never_raises_on_hostile_input():
    """Fail-soft: a broken/absent log must yield [] and never propagate."""
    class _Bad:
        @property
        def log(self):
            raise RuntimeError("ib_async internals changed")

    assert _broker_messages(_Bad()) == []
    assert _broker_messages(_Trade(None)) == []
    assert _broker_messages(object()) == []
    assert _broker_messages(_Trade([object()])) == []   # entry lacks attributes


def test_message_is_truncated():
    t = _Trade([_Entry("Cancelled", "x" * 900, 201)])
    assert len(_broker_messages(t)[0]["message"]) == 300


def test_commission_matches_broker_measured_tiered_cost():
    """319 real ibkr_execution rows: sum(commission)/sum(shares) = $1.2794 per
    contract per SIDE on IBKR Tiered -> $2.58 round trip. The old $1.30 treated
    a (wrong, Fixed-rate) per-side $0.65 as the whole round trip."""
    from fef_sidecar.engine.counterfactual import FrictionModel
    from fef_sidecar.engine.realistic_fill import RealisticFillConfig

    # 2 x $1.28028 = $2.56056. The exact modal rate, size-invariant from 6 to
    # 720 contracts (review corrected an earlier 2.58 rounding).
    assert FrictionModel().commission_per_contract_rt == 2.56
    assert RealisticFillConfig().commission_per_contract_rt == 2.56
    assert (FrictionModel().commission_per_contract_rt
            == RealisticFillConfig().commission_per_contract_rt)


def test_commission_flows_into_the_friction_math():
    """A round trip on 10 contracts must cost 10 x 2.58, not 10 x 1.30."""
    from fef_sidecar.engine.counterfactual import FrictionModel, _apply_friction

    fm = FrictionModel()
    out = _apply_friction(0.0, n_trades=5, avg_qty_per_trade=2.0,
                          is_bcd=False, fm=fm)
    assert out["best_live"] == pytest.approx(-25.6)   # 10 contracts x $2.56
