"""Unified signal dedup methodology.

Used by `daily_review.py`, counterfactual analytics, and any future
analytics that needs to answer "what would the WR look like if I only
fired ONCE per setup instead of stacking?"

Methodology — match what `daily_review.compute_daily_review()` has been
using (so historical numbers stay comparable):

  1. Sort signals chronologically by timestamp.
  2. For each signal, compute `contract_key = f"{direction}|{strike}"`.
     - direction: "LONG_CALLS" / "LONG_PUTS" (WAIT is excluded earlier).
     - strike: the recommended option strike (numeric, no rounding).
  3. Keep a signal IF no prior signal with the SAME contract_key fired
     within `window_min` minutes.
  4. Skipped signals are "duplicates" — they were stacks on the same
     setup, useful for sizing but not for measuring signal quality.

The output dedup list is one entry per "unique setup," which is what
you'd have placed if you only fired one trade per signal.

Default window is 5 minutes. Tunable per-caller in case some analyses
want a tighter or looser definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_DEDUP_WINDOW_MIN = 5


@dataclass(frozen=True)
class DedupResult:
    """Per-call dedup outcome."""

    total_signals: int          # input count
    dedup_count: int            # unique setups kept
    dropped_dupes: int          # total_signals - dedup_count
    window_min: int             # the window used


def deduplicate_signals(
    signals: list[dict[str, Any]],
    *,
    window_min: int = DEFAULT_DEDUP_WINDOW_MIN,
) -> tuple[list[dict[str, Any]], DedupResult]:
    """Filter a list of signals down to unique setups.

    Each input signal must have:
      - `timestamp_ms` (int) — millisecond UTC timestamp
      - `direction` (str) — "LONG_CALLS" / "LONG_PUTS"
      - `strike` (float) — recommended strike (no rounding)

    Returns `(kept_signals, stats)` where `kept_signals` is a subset
    preserving the input dict shape (sorted ascending by timestamp_ms).
    """
    if not signals:
        return [], DedupResult(0, 0, 0, window_min)

    # Sort ascending so the first occurrence wins
    sorted_sigs = sorted(signals, key=lambda s: s.get("timestamp_ms", 0))

    last_fire_ms: dict[str, int] = {}
    kept: list[dict[str, Any]] = []
    window_ms = window_min * 60_000

    for sig in sorted_sigs:
        direction = sig.get("direction")
        strike = sig.get("strike")
        if direction is None or strike is None:
            continue
        key = f"{direction}|{strike}"
        ts_ms = int(sig.get("timestamp_ms", 0))
        prev = last_fire_ms.get(key)
        if prev is not None and (ts_ms - prev) < window_ms:
            continue  # duplicate within window — drop
        last_fire_ms[key] = ts_ms
        kept.append(sig)

    return kept, DedupResult(
        total_signals=len(signals),
        dedup_count=len(kept),
        dropped_dupes=len(signals) - len(kept),
        window_min=window_min,
    )


def deduplicate_parents(parents: list[Any], *, window_min: int = DEFAULT_DEDUP_WINDOW_MIN):
    """Convenience: dedup a list of LiveOrder parent rows directly.

    Each parent must have `submitted_at` (datetime), `right` (str),
    `strike` (float). Returns `(kept_parents, stats)`.

    Maps `right` → direction: "C" → LONG_CALLS, "P" → LONG_PUTS.
    """
    signals = []
    for p in parents:
        right = getattr(p, "right", None)
        if right not in ("C", "P"):
            continue
        direction = "LONG_CALLS" if right == "C" else "LONG_PUTS"
        submitted = getattr(p, "submitted_at", None)
        if submitted is None:
            continue
        ts_ms = int(submitted.timestamp() * 1000)
        signals.append({
            "timestamp_ms": ts_ms,
            "direction": direction,
            "strike": getattr(p, "strike", None),
            "_parent_ref": p,
        })

    kept_signals, stats = deduplicate_signals(signals, window_min=window_min)
    kept_parents = [s["_parent_ref"] for s in kept_signals]
    return kept_parents, stats
