"""Canonical R-unit helper for research scripts.

Background (Chat round-2 audit, Priority 3 — 2026-05-22):
  Different research scripts disagreed on the R unit. `ablation_backtest`,
  `shadow_exit_replay`, and `mid_session_snapshot` hardcoded `1R = 10 SPX pts`.
  `realistic_fill_estimator` and `daily_review` use per-signal
  `abs(entry - stop)`. When the engine places a stop at -7pt instead of
  -10pt, the first three record a stop hit as -1R while the last two record
  it as -0.7R. The "B+C+D +15.87R" reported by ablation was NOT the same R
  unit as "+15.87R" reported by daily_review.

Canonical choice: per-signal `abs(entry - stop)`. This matches what the
live engine actually risks per signal and matches `daily_review` (the
user-facing source of truth).

Use this helper in any research script that computes R-units from SPX
points. Pass the SPX entry price and SPX stop price extracted from one of:
  - LiveOrder.entry_spx_price / spx_target_stop
  - SignalIter.extras["stop"]   (the SPX stop the engine recommended)
  - SignalIter.extras["option_pricing"]["stop"]  (option-price stop —
    don't pass this; it's a $/option value, not an SPX price)

`default_pts` is the legacy 10-pt fallback used only when entry/stop is
missing or zero (e.g., a signal that pre-dates the option-pricing field).
"""
from __future__ import annotations


def compute_r_unit(
    entry_spx: float | None,
    stop_spx: float | None,
    default_pts: float = 10.0,
) -> float:
    """Canonical R unit: distance from entry to stop in SPX points.

    Falls back to default_pts when entry or stop is missing/zero.
    This is the engine's actual risk per signal and matches daily_review's
    `abs(entry_spx_price - spx_target_stop)` computation.

    Args:
        entry_spx: SPX price at signal entry (or LiveOrder.entry_spx_price).
        stop_spx: SPX stop price (or LiveOrder.spx_target_stop, or
            SignalIter.extras["stop"]).
        default_pts: Fallback when entry/stop is None/0 (default 10.0 — the
            legacy hardcoded value).

    Returns: |entry - stop| in SPX points, or default_pts on missing data.
    """
    if not entry_spx or not stop_spx:
        return default_pts
    diff = abs(float(entry_spx) - float(stop_spx))
    return diff if diff > 0 else default_pts


def r_unit_from_extras(
    extras: dict | None,
    entry_spx: float | None,
    default_pts: float = 10.0,
) -> float:
    """Convenience: pull the SPX stop from a SignalIter.extras dict and
    compute R-unit relative to entry_spx. Use this when you have the
    signal's extras JSON loaded.

    Looks at extras["stop"] (the SPX-price stop level the engine chose,
    NOT extras["option_pricing"]["stop"] which is a $/option value).
    """
    if not isinstance(extras, dict):
        return default_pts
    stop_spx = extras.get("stop")
    return compute_r_unit(entry_spx, stop_spx, default_pts)
