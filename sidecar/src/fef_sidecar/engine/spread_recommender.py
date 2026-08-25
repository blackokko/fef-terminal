"""Credit-spread recommender — TA-driven entries with delta-targeted strikes.

Independent of the FEF directional model: spreads have their own entry
playbook (see engine/spread_entry.py for the rule set). This module asks
engine/strike_picker.py for the short strike whose |delta| is NEAREST the
configured target (0.15) — not the first one under it, which is what produced
the 0.0329-delta pick on 2026-08-12 — and assembles the spread recommendation
around it.

Returns ``NONE`` whenever:
  * Not enough bar history to evaluate
  * Technical entry conditions not met AND gating is on (it is off by default)
  * No strike in the chain lands in the delta band, clears the net-credit floor,
    or the scan could not quote enough of the window to make a "nearest" claim
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
    DEFAULT_DELTA_FLOOR_FRAC,
    DEFAULT_MIN_CREDIT_WIDTH_RATIO,
    DEFAULT_MIN_NET_CREDIT,
    DEFAULT_MIN_QUOTE_COVERAGE,
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
    """Assemble the SpreadRecommendation given a pick and the trigger state.

    ⚠ THE LABEL IS LOAD-BEARING FOR THE AUDIT TRAIL. `trigger.reasons` holds the
    conditions that FAILED when the trigger did not fire. This function used to
    head that list with "{kind} trigger:" unconditionally, and because the
    recommender runs UNGATED by default (`require_gate=False`, deliberate — 0 of
    236 entry gates beat "sell every session", `vrpsig`), the common case wrote
    lines like

        "PCS trigger: EMA8 not above EMA21 · no golden cross · ..."

    into the reasoning that the audit log, the UI and every post-hoc "why did the
    bot fire?" query then read as the reasons it FIRED. It fired because gating
    is off; those were the reasons it would NOT have. Only the wording is fixed
    here — the ungated firing is correct and the gating behaviour is untouched.
    """
    credit_per_contract_cents = pick.estimated_credit * 100
    width = pick.width
    max_loss = (width - pick.estimated_credit) * 100
    breakeven = (
        pick.short_strike - pick.estimated_credit
        if kind == "PCS"
        else pick.short_strike + pick.estimated_credit
    )

    if trigger.triggered:
        header = f"{kind} trigger:"
    else:
        header = (
            f"{kind} UNGATED (playbook did NOT trigger; "
            f"the following are the conditions it FAILED):"
        )

    reasoning = " · ".join([
        header,
        *trigger.reasons,
        f"short {pick.short_strike:.0f} delta {pick.short_delta:+.2f}",
        f"net credit ${pick.estimated_credit:.2f}",
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
        # Machine-readable counterpart to the header above: populated ONLY when
        # the playbook did not fire, so a consumer never has to parse prose to
        # tell a gated fire from an ungated one.
        fail_reasons=None if trigger.triggered else list(trigger.reasons),
    )


async def recommend_from_bars(
    bars_30m: list[Bar],
    *,
    ibkr: IBKRProvider,
    symbol: str,
    expiry: str,
    spot: float,
    delta_max: float = 0.15,
    premium_min: float = 0.0,
    width: float = 10.0,
    puts_only: bool = True,
    require_gate: bool = False,
    delta_floor_frac: float = DEFAULT_DELTA_FLOOR_FRAC,
    min_quote_coverage: float = DEFAULT_MIN_QUOTE_COVERAGE,
    min_credit_width_ratio: float = DEFAULT_MIN_CREDIT_WIDTH_RATIO,
    min_net_credit: float = DEFAULT_MIN_NET_CREDIT,
) -> SpreadRecommendation:
    """Top-level entry point — checks both PCS and CCS triggers against the
    30-min bar series, and returns the recommendation for whichever fires
    (or NONE if neither does or no qualifying strike exists).

    Both triggers can't fire simultaneously (PCS needs uptrend + bullish
    engulfing, CCS needs downtrend + bearish engulfing) so the order of
    checks doesn't matter — but PCS is checked first for symmetry.

    ``puts_only`` (default True) suppresses the CCS side: call spreads measured
    NEGATIVE even at MID fills in both eras (`tds_sweep` §5), and 1DTE calls
    mis-calibrate the wrong way (+1.18pp, `touchsell`). Set False to restore.

    ``require_gate`` (default False) controls whether the TA entry conditions
    must fire. **0 of 236 entry gates beat "sell every session"** (`vrpsig`),
    G3 passed at 2.5% — BELOW the 5% chance rate — and a gate trading fraction
    r forfeits sqrt(r) of Sharpe automatically (measured corr +0.983 to theory).
    With require_gate=False the structure is offered every iteration and the
    trigger is still evaluated and reported, so the UI keeps showing WHY the
    playbook would or would not have fired. Set True to restore gating.

    The four ``*_frac`` / ``min_*`` arguments are the strike-selection guards
    added after the 2026-08-12 0.0329-delta pick; see engine/strike_picker.py
    for what each one refuses and why. They are threaded through from AppState
    so every axis stays tunable without a code change.
    """
    # One dict, three call sites — the picker's guards must not be able to
    # differ between the ungated PCS path, the gated PCS path and the CCS path.
    picker_kwargs = {
        "symbol": symbol,
        "expiry": expiry,
        "spot": spot,
        "width": width,
        "delta_max": delta_max,
        "premium_min": premium_min,
        "delta_floor_frac": delta_floor_frac,
        "min_quote_coverage": min_quote_coverage,
        "min_credit_width_ratio": min_credit_width_ratio,
        "min_net_credit": min_net_credit,
    }
    # Shared tail for the "nothing qualified" messages, so the UI reports the
    # band actually enforced rather than the old "<= delta_max" that is no
    # longer the rule.
    criteria = (
        f"|delta| within [{delta_max * delta_floor_frac:.3f}, {delta_max:.3f}] "
        f"(nearest {delta_max:.2f}), net credit >= "
        f"{min_credit_width_ratio:.0%} of width and >= ${min_net_credit:.2f}, "
        f"with >= {min_quote_coverage:.0%} of scanned strikes quotable"
        + (f", short mid >= ${premium_min:.2f}" if premium_min > 0 else "")
    )

    pcs_trigger = check_pcs_entry(bars_30m)
    if not require_gate:
        # Ungated: always attempt the put spread; keep the trigger for display.
        pick = await find_pcs_strike(ibkr, **picker_kwargs)
        if pick is not None:
            return _build_from_trigger_and_pick(pcs_trigger, pick, "PCS")
        return SpreadRecommendation(
            kind="NONE",
            reasoning=f"ungated PCS: no strike met {criteria}",
        )

    if pcs_trigger.triggered:
        pick = await find_pcs_strike(ibkr, **picker_kwargs)
        if pick is not None:
            return _build_from_trigger_and_pick(pcs_trigger, pick, "PCS")
        return SpreadRecommendation(
            kind="NONE",
            reasoning=f"PCS entry triggered but no strike met {criteria}",
            fail_reasons=pcs_trigger.reasons,
        )

    ccs_trigger = check_ccs_entry(bars_30m)
    if ccs_trigger.triggered and not puts_only:
        pick = await find_ccs_strike(ibkr, **picker_kwargs)
        if pick is not None:
            return _build_from_trigger_and_pick(ccs_trigger, pick, "CCS")
        return SpreadRecommendation(
            kind="NONE",
            reasoning=f"CCS entry triggered but no strike met {criteria}",
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
