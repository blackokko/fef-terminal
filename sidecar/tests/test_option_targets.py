"""Tests for option-price target estimation."""

from __future__ import annotations

from datetime import datetime

from fef_sidecar.engine.option_targets import build_option_targets
from fef_sidecar.ibkr.models import Contract, ContractKind, OptionQuote


def _call_quote(strike: float, last: float, delta: float, spx: float = 7430.0) -> OptionQuote:
    return OptionQuote(
        timestamp=datetime.now(),
        contract=Contract(
            symbol="SPX",
            kind=ContractKind.OPTION,
            exchange="CBOE",
            expiry="20260515",
            strike=strike,
            right="C",
        ),
        last=last,
        bid=last - 0.25,
        ask=last + 0.25,
        delta=delta,
        underlying_price=spx,
    )


def test_long_calls_t1_higher_than_entry() -> None:
    quote = _call_quote(strike=7450.0, last=8.0, delta=0.4, spx=7430.0)
    t = build_option_targets(
        quote=quote,
        current_spx=7430.0,
        spx_t1=7440.0,
        spx_t2=7450.0,
        spx_t3=7465.0,
        spx_stop=7420.0,
    )
    # Entry = ask = 8.25; delta 0.4
    # T1 at SPX +10pts -> entry + 0.4*10 = 12.25
    assert t.entry_limit == 8.25
    assert t.t1 == 12.25
    assert t.t2 > t.t1
    assert t.t3 > t.t2
    assert t.stop < t.entry_limit


def test_long_puts_targets_invert() -> None:
    # Put quote with negative delta
    quote = OptionQuote(
        timestamp=datetime.now(),
        contract=Contract(
            symbol="SPX",
            kind=ContractKind.OPTION,
            exchange="CBOE",
            expiry="20260515",
            strike=7400.0,
            right="P",
        ),
        last=6.0,
        bid=5.75,
        ask=6.25,
        delta=-0.35,
        underlying_price=7430.0,
    )

    t = build_option_targets(
        quote=quote,
        current_spx=7430.0,
        spx_t1=7420.0,   # SPX -10 -> profit
        spx_t2=7410.0,
        spx_t3=7395.0,
        spx_stop=7440.0,  # SPX +10 -> loss
    )

    # Entry 6.25, delta -0.35
    # T1 at SPX -10pts -> entry + (-0.35)*(-10) = 9.75
    assert t.entry_limit == 6.25
    assert t.t1 > t.entry_limit  # profitable target
    assert t.stop < t.entry_limit  # losing stop


def test_no_delta_returns_entry_for_all_targets() -> None:
    quote = OptionQuote(
        timestamp=datetime.now(),
        contract=Contract(
            symbol="SPX",
            kind=ContractKind.OPTION,
            exchange="CBOE",
            expiry="20260515",
            strike=7450.0,
            right="C",
        ),
        last=8.0,
        bid=7.75,
        ask=8.25,
        delta=None,
        underlying_price=7430.0,
    )
    t = build_option_targets(
        quote=quote,
        current_spx=7430.0,
        spx_t1=7440.0,
        spx_t2=7450.0,
        spx_t3=7465.0,
        spx_stop=7420.0,
    )
    assert t.t1 == t.t2 == t.t3 == t.stop == t.entry_limit


def test_floor_at_5_cents() -> None:
    # Big adverse move should clamp to $0.05 not go negative
    quote = _call_quote(strike=7500.0, last=0.50, delta=0.10, spx=7430.0)
    t = build_option_targets(
        quote=quote,
        current_spx=7430.0,
        spx_t1=7440.0,
        spx_t2=7450.0,
        spx_t3=7465.0,
        spx_stop=7000.0,  # huge stop way below
    )
    assert t.stop == 0.05
