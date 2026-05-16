"""Near-the-money max pain calculation.

Per [[ntm-max-pain]] memory: the chain-wide max pain reported by QuantData is
polluted by far-OTM macro hedge OI. For signaling we want the max pain
computed only over strikes near current spot — that's where dealer hedging
actually matters intraday.

This module is a pure function — no I/O, no client dependency. Test it with
a frozen OI list.
"""

from __future__ import annotations

from dataclasses import dataclass

from fef_sidecar.quantdata.models import OIRow


@dataclass(frozen=True)
class NTMMaxPainResult:
    max_pain: float
    spx_price: float
    distance_pts: float
    """Signed: positive = spot is above max pain (bullish-stretch), negative = below."""

    distance_pct: float
    total_writer_pain_at_mp: float
    """Total dealer pain at the max-pain strike. Lower = tighter pin."""

    strikes_considered: int


def compute_ntm_max_pain(
    oi_rows: list[OIRow],
    spx_price: float,
    window: float = 50.0,
) -> NTMMaxPainResult:
    """Compute near-the-money max pain.

    Args:
        oi_rows: Open interest rows. May span the full chain — we'll filter.
        spx_price: Current SPX price (for window centering and distance calc).
        window: Half-width of NTM filter. Default $50 matches qd_get_oi_by_strike
            default behavior. Strikes outside [spot-window, spot+window] are ignored.

    Returns:
        NTMMaxPainResult with the strike (within the NTM window) that minimizes
        total option-writer pain at expiration.
    """
    ntm_rows = [r for r in oi_rows if abs(r.strike - spx_price) <= window]
    if not ntm_rows:
        return NTMMaxPainResult(
            max_pain=spx_price,
            spx_price=spx_price,
            distance_pts=0.0,
            distance_pct=0.0,
            total_writer_pain_at_mp=0.0,
            strikes_considered=0,
        )

    candidates = sorted({r.strike for r in ntm_rows})

    best_strike = candidates[0]
    best_pain = float("inf")

    for s in candidates:
        pain = 0.0
        for r in ntm_rows:
            if s > r.strike:
                pain += (s - r.strike) * r.call_oi
            if s < r.strike:
                pain += (r.strike - s) * r.put_oi
        if pain < best_pain:
            best_pain = pain
            best_strike = s

    distance_pts = spx_price - best_strike
    distance_pct = (distance_pts / spx_price * 100) if spx_price else 0.0

    return NTMMaxPainResult(
        max_pain=best_strike,
        spx_price=spx_price,
        distance_pts=distance_pts,
        distance_pct=distance_pct,
        total_writer_pain_at_mp=best_pain,
        strikes_considered=len(ntm_rows),
    )
