"""Credit-spread recommender — TA-driven entries with delta-targeted strikes.

Independent of the FEF directional model: spreads have their own entry
playbook (see engine/spread_entry.py for the rule set). When entry
conditions trigger on the 30-min timeframe, this module scans the option
chain for the first short strike with |delta| <= 0.15 AND mid premium >=
$0.70, and assembles a 5-point wide spread recommendation.

Returns ``NONE`` whenever:
  * Not enough bar history to evaluate
  * Technical entry conditions not met
  * No strike in the chain satisfies BOTH delta and premium targets
    (per the 'show no spread' fallback choice)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fef_sidecar.engine.spread_entry import (
    SpreadEntryTrigger,
    check_ccs_entry,
    check_pcs_entry,
)
from fef_sidecar.engine.strike_picker import (
    StrikePick,
    find_ccs_strike,
    find_pcs_strike,
)
from fef_sidecar.ibkr.models import Bar
from fef_sidecar.ibkr.provider import IBKRProvider


SpreadKind = Literal["LONG_CALL", "LONG_PUT", "PCS", "CCS", "NONE"]


@dataclass(frozen=True)
class SpreadRecommendation:
    """Stable schema consumed by the verdict + OrderTicket panel.

    Fields preserved from the prior heuristic-based recommender so the
    frontend continues to render without modification.
    """

    kind: SpreadKind
    reasoning: str
    short_strike: float | None = None
    long_strike: float | None = None
    spread_width: float | None = None
    estimated_credit_per_contract: float | None = None  # in cents (* 100)
    max_profit_per_contract: float | None = None
    max_loss_per_contract: float | None = None
    breakeven: float | None = None
    # New diagnostic fields the panel can surface in tooltip/expanded view
    trigger_bar_close_ms: int | None = None
    trigger_signature: str = ""
    short_delta: float | None = None
    fail_reasons: list[str] | None = None


def _build_from_trigger_and_pick(
    trigger: SpreadEntryTrigger, pick: StrikePick, kind: SpreadKind
) -> SpreadRecommendation:
    """Assemble the SpreadRecommendation given a fired trigger + valid pick."""
    credit_per_contract_cents = pick.estimated_credit * 100
    width = pick.width
    max_loss = (width - pick.estimated_credit) * 100
    breakeven = (
        pick.short_strike - pick.estimated_credit
        if kind == "PCS"
        else pick.short_strike + pick.estimated_credit
    )

    reasoning = " · ".join([
        f"{kind} trigger:",
        *trigger.reasons,
        f"short {pick.short_strike:.0f} delta {pick.short_delta:+.2f}",
        f"mid ${pick.estimated_credit:.2f}",
    ])

    return SpreadRecommendation(
        kind=kind,
        reasoning=reasoning,
        short_strike=pick.short_strike,
        long_strike=pick.long_strike,
        spread_width=width,
        estimated_credit_per_contract=credit_per_contract_cents,
        max_profit_per_contract=credit_per_contract_cents,
        max_loss_per_contract=max_loss,
        breakeven=breakeven,
        trigger_bar_close_ms=trigger.trigger_bar_close_ms,
        trigger_signature=trigger.signature,
        short_delta=pick.short_delta,
    )


async def recommend_from_bars(
    bars_30m: list[Bar],
    *,
    ibkr: IBKRProvider,
    symbol: str,
    expiry: str,
    spot: float,
    delta_max: float = 0.15,
    premium_min: float = 0.70,
    width: float = 5.0,
) -> SpreadRecommendation:
    """Top-level entry point — checks both PCS and CCS triggers against the
    30-min bar series, and returns the recommendation for whichever fires
    (or NONE if neither does or no qualifying strike exists).

    Both triggers can't fire simultaneously (PCS needs uptrend + bullish
    engulfing, CCS needs downtrend + bearish engulfing) so the order of
    checks doesn't matter — but PCS is checked first for symmetry.
    """
    pcs_trigger = check_pcs_entry(bars_30m)
    if pcs_trigger.triggered:
        pick = await find_pcs_strike(
            ibkr,
            symbol=symbol,
            expiry=expiry,
            spot=spot,
            width=width,
            delta_max=delta_max,
            premium_min=premium_min,
        )
        if pick is not None:
            return _build_from_trigger_and_pick(pcs_trigger, pick, "PCS")
        return SpreadRecommendation(
            kind="NONE",
            reasoning=(
                "PCS entry triggered but no strike in scan window had "
                f"|delta| <= {delta_max} AND mid >= ${premium_min:.2f}"
            ),
            fail_reasons=pcs_trigger.reasons,
        )

    ccs_trigger = check_ccs_entry(bars_30m)
    if ccs_trigger.triggered:
        pick = await find_ccs_strike(
            ibkr,
            symbol=symbol,
            expiry=expiry,
            spot=spot,
            width=width,
            delta_max=delta_max,
            premium_min=premium_min,
        )
        if pick is not None:
            return _build_from_trigger_and_pick(ccs_trigger, pick, "CCS")
        return SpreadRecommendation(
            kind="NONE",
            reasoning=(
                "CCS entry triggered but no strike in scan window had "
                f"|delta| <= {delta_max} AND mid >= ${premium_min:.2f}"
            ),
            fail_reasons=ccs_trigger.reasons,
        )

    # Neither fired — surface the closer-to-firing rule's reasons so the
    # panel can show what's pending. Prefer whichever rule has more
    # criteria already satisfied (= fewer reasons).
    if len(pcs_trigger.reasons) <= len(ccs_trigger.reasons):
        pending = pcs_trigger
        side = "PCS"
    else:
        pending = ccs_trigger
        side = "CCS"
    return SpreadRecommendation(
        kind="NONE",
        reasoning=f"No spread trigger — closest side ({side}): {pending.reasons[0] if pending.reasons else 'unknown'}",
        fail_reasons=pending.reasons,
    )
