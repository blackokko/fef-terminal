"""Spread recommender — naked vs credit-spread suggestion for the FEF verdict.

Decides whether the user should take a naked long call/put (high-conviction
directional play) or a credit spread (defined-risk premium-collecting play
that profits from time decay + the directional bias being correct enough).

Heuristics drawn from common 0DTE practitioner playbook:
- High IV rank (>60) → premium-rich environment → spreads more attractive
- Late in session (after 13:30 ET) → theta accelerating → spreads benefit
- Distance to NTM max pain >25pts → expect mean reversion → spread that
  profits from price NOT reaching → CCS above for bullish, PCS below for bearish
- LOW IVR (<30) + aligned HIGH verdict → naked is cheap relative to spreads
- WAIT verdict → no recommendation

Output describes a 1-leg-wide spread (e.g., 7400/7395 PCS) anchored on the
nearest dominant wall in the relevant direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SpreadKind = Literal["LONG_CALL", "LONG_PUT", "PCS", "CCS", "NONE"]


@dataclass(frozen=True)
class SpreadRecommendation:
    kind: SpreadKind
    reasoning: str
    short_strike: float | None = None
    long_strike: float | None = None
    spread_width: float | None = None  # in points
    estimated_credit_per_contract: float | None = None  # for spreads, in dollars
    max_profit_per_contract: float | None = None
    max_loss_per_contract: float | None = None
    breakeven: float | None = None


def recommend(
    direction: str,           # "LONG_CALLS" / "LONG_PUTS" / "WAIT"
    confidence: str,          # "HIGH" / "MEDIUM" / "LOW" / "NONE"
    spx_price: float,
    ntm_max_pain: float,
    top_dex_call_strike: float | None,
    top_dex_put_strike: float | None,
    iv_rank_call_pct: float | None,
    iv_rank_put_pct: float | None,
    minutes_since_open: int,
) -> SpreadRecommendation:
    """Pick the trade structure for the current verdict + market context."""

    if direction == "WAIT":
        return SpreadRecommendation(kind="NONE", reasoning="No actionable signal — stand down")

    iv_rank = iv_rank_call_pct if direction == "LONG_CALLS" else iv_rank_put_pct
    is_late_session = minutes_since_open >= 240  # after ~13:30 ET
    mp_distance = abs(spx_price - ntm_max_pain)

    # ---- LONG_CALLS branch ----
    if direction == "LONG_CALLS":
        # Naked when IV is cheap + high conviction
        if (iv_rank is not None and iv_rank < 30) and confidence == "HIGH":
            return SpreadRecommendation(
                kind="LONG_CALL",
                reasoning=f"HIGH conviction + IVR {iv_rank:.0f}% (cheap calls) — naked long",
            )

        # Late session + spot near a defended put wall → PCS (sell put spread, bullish)
        if is_late_session and top_dex_put_strike is not None and spx_price > top_dex_put_strike:
            short = round(top_dex_put_strike / 5) * 5
            long_strike = short - 5
            credit = 0.70  # typical 5-wide PCS credit at 0DTE
            width = short - long_strike
            return SpreadRecommendation(
                kind="PCS",
                reasoning=f"Late-session bullish + spot above DEX put wall {short:.0f} — collect premium with PCS",
                short_strike=short,
                long_strike=long_strike,
                spread_width=width,
                estimated_credit_per_contract=credit * 100,
                max_profit_per_contract=credit * 100,
                max_loss_per_contract=(width - credit) * 100,
                breakeven=short - credit,
            )

        # High IVR (premium-rich) → favor PCS over naked
        if iv_rank is not None and iv_rank > 60:
            # Anchor short strike at NTM MP (or nearest 5-pt below spot)
            anchor = ntm_max_pain if ntm_max_pain < spx_price else (spx_price - 10)
            short = round(anchor / 5) * 5
            long_strike = short - 5
            credit = 0.85
            width = 5.0
            return SpreadRecommendation(
                kind="PCS",
                reasoning=f"IVR {iv_rank:.0f}% (rich premium) — sell PCS instead of buying naked",
                short_strike=short,
                long_strike=long_strike,
                spread_width=width,
                estimated_credit_per_contract=credit * 100,
                max_profit_per_contract=credit * 100,
                max_loss_per_contract=(width - credit) * 100,
                breakeven=short - credit,
            )

        # Default: naked long call
        return SpreadRecommendation(
            kind="LONG_CALL",
            reasoning="Directional bull move — naked long call for max delta",
        )

    # ---- LONG_PUTS branch ----
    if direction == "LONG_PUTS":
        if (iv_rank is not None and iv_rank < 30) and confidence == "HIGH":
            return SpreadRecommendation(
                kind="LONG_PUT",
                reasoning=f"HIGH conviction + IVR {iv_rank:.0f}% (cheap puts) — naked long",
            )

        if is_late_session and top_dex_call_strike is not None and spx_price < top_dex_call_strike:
            short = round(top_dex_call_strike / 5) * 5
            long_strike = short + 5
            credit = 0.70
            width = long_strike - short
            return SpreadRecommendation(
                kind="CCS",
                reasoning=f"Late-session bearish + spot below DEX call wall {short:.0f} — collect premium with CCS",
                short_strike=short,
                long_strike=long_strike,
                spread_width=width,
                estimated_credit_per_contract=credit * 100,
                max_profit_per_contract=credit * 100,
                max_loss_per_contract=(width - credit) * 100,
                breakeven=short + credit,
            )

        if iv_rank is not None and iv_rank > 60:
            anchor = ntm_max_pain if ntm_max_pain > spx_price else (spx_price + 10)
            short = round(anchor / 5) * 5
            long_strike = short + 5
            credit = 0.85
            width = 5.0
            return SpreadRecommendation(
                kind="CCS",
                reasoning=f"IVR {iv_rank:.0f}% (rich premium) — sell CCS instead of buying naked",
                short_strike=short,
                long_strike=long_strike,
                spread_width=width,
                estimated_credit_per_contract=credit * 100,
                max_profit_per_contract=credit * 100,
                max_loss_per_contract=(width - credit) * 100,
                breakeven=short + credit,
            )

        return SpreadRecommendation(
            kind="LONG_PUT",
            reasoning="Directional bear move — naked long put for max negative delta",
        )

    return SpreadRecommendation(kind="NONE", reasoning="Unrecognized direction")
