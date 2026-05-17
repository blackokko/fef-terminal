"""Estimate option-price targets from SPX-price targets via delta.

First-order approximation: option_price_at_target ≈ current_option + delta × (target_spx − current_spx).

Adequate for the typical 10-35pt targets in the FEF model; breaks down on
huge moves where gamma matters. v1.5 can add a gamma term.
"""

from __future__ import annotations

from dataclasses import dataclass

from fef_sidecar.ibkr.models import OptionQuote


@dataclass(frozen=True)
class OptionTargets:
    contract_strike: float
    right: str  # "C" or "P"
    entry_limit: float  # option ask (or last if no ask)
    delta: float | None
    underlying_price_at_quote: float | None
    t1: float
    t2: float
    t3: float
    stop: float


def _est(current_option: float, delta: float | None, current_spx: float, target_spx: float) -> float:
    if delta is None:
        return round(current_option, 2)
    move = target_spx - current_spx
    return round(max(0.05, current_option + delta * move), 2)


def build_option_targets(
    quote: OptionQuote,
    current_spx: float,
    spx_t1: float,
    spx_t2: float,
    spx_t3: float,
    spx_stop: float,
) -> OptionTargets:
    if quote.contract.strike is None or quote.contract.right is None:
        raise ValueError("OptionQuote.contract must have strike + right")

    # Always anchor on the same SPX value that built the SPX targets (the FEF
    # iter's snapshot price). The quote's underlying_price can lag or — in mock
    # mode — diverge from the QuantData provider's SPX, breaking target ordering.
    spx = current_spx
    entry = quote.ask if quote.ask and quote.ask > 0 else quote.last

    return OptionTargets(
        contract_strike=quote.contract.strike,
        right=quote.contract.right,
        entry_limit=round(entry, 2),
        delta=quote.delta,
        underlying_price_at_quote=quote.underlying_price,
        t1=_est(entry, quote.delta, spx, spx_t1),
        t2=_est(entry, quote.delta, spx, spx_t2),
        t3=_est(entry, quote.delta, spx, spx_t3),
        stop=_est(entry, quote.delta, spx, spx_stop),
    )
