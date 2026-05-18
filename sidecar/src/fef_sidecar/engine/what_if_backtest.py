"""'What would have fired' backtest.

Replays today's (or any window's) signal_iter rows through a proposed
filter function and reports: how many signals would be removed, what
the expectancy delta would be, etc.

Used by the TuningPanel's 'Apply' button to show the user what a
proposed gate would have actually done.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import HistoricalBar, SignalIter

logger = logging.getLogger(__name__)


def _outcome_R(start_ms: int, spx_entry: float, t1: float, t2: float, t3: float,
               stop: float, is_long_calls: bool, bars: list,
               window_min: int = 60) -> float:
    """Same 50/30/20 weighted-R outcome we use in daily_review."""
    end_ms = start_ms + window_min * 60_000
    hit_t1 = hit_t2 = hit_t3 = hit_stop = None
    for b in bars:
        if b.timestamp_ms < start_ms: continue
        if b.timestamp_ms > end_ms: break
        if is_long_calls:
            if hit_stop is None and b.low <= stop: hit_stop = True; break
            if hit_t1 is None and b.high >= t1: hit_t1 = True
            if hit_t2 is None and b.high >= t2: hit_t2 = True
            if hit_t3 is None and b.high >= t3: hit_t3 = True
        else:
            if hit_stop is None and b.high >= stop: hit_stop = True; break
            if hit_t1 is None and b.low <= t1: hit_t1 = True
            if hit_t2 is None and b.low <= t2: hit_t2 = True
            if hit_t3 is None and b.low <= t3: hit_t3 = True
    if hit_stop: return -1.0
    if hit_t1 is None: return 0.0
    risk = abs(spx_entry - stop) or 1.0
    r1 = abs(t1 - spx_entry) / risk
    r2 = abs(t2 - spx_entry) / risk if hit_t2 else r1
    r3 = abs(t3 - spx_entry) / risk if hit_t3 else (abs(t2 - spx_entry) / risk if hit_t2 else r1)
    return 0.50 * r1 + 0.30 * r2 + 0.20 * r3


# Built-in filter functions. Each takes a signal_iter row + its extras
# and returns True if the proposed gate would ALLOW the signal.
def _filter_confluence_only(row: SignalIter, ex: dict) -> bool:
    return bool(ex.get("aligned")) and bool(ex.get("wall_collapse_trigger"))


def _filter_aligned_only(row: SignalIter, ex: dict) -> bool:
    return bool(ex.get("aligned"))


def _filter_wall_proximity_5_15(row: SignalIter, ex: dict) -> bool:
    nwd = ex.get("nearest_wall_distance")
    return nwd is not None and 5 <= nwd <= 15


def _filter_fe_band_1_3(row: SignalIter, ex: dict) -> bool:
    fe = ex.get("fe_ratio")
    return fe is not None and 1.0 <= fe <= 3.0


def _filter_market_hours_only(row: SignalIter, ex: dict) -> bool:
    from datetime import timezone, timedelta
    ts = row.timestamp
    if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
    ct = ts.astimezone(timezone(timedelta(hours=-5)))
    return 8 <= ct.hour <= 14  # rough RTH window in CT


FILTERS: dict[str, Callable] = {
    "confluence_only": _filter_confluence_only,
    "aligned_only": _filter_aligned_only,
    "wall_proximity_5_15": _filter_wall_proximity_5_15,
    "fe_band_1_3": _filter_fe_band_1_3,
    "market_hours_only": _filter_market_hours_only,
}


async def what_if(filter_names: list[str],
                  session_date: str | None = None) -> dict[str, Any]:
    """Replay signals through the given filter combo (AND of all filters).

    Returns a comparison: baseline (no filter) vs filtered, with expectancy
    + win-rate + counts for both.
    """
    async with get_session() as session:
        sigs_stmt = select(SignalIter)
        if session_date:
            sigs_stmt = sigs_stmt.where(SignalIter.session_date == session_date)
        sigs = list((await session.exec(sigs_stmt)).all())
        bars = list((await session.exec(
            select(HistoricalBar)
            .where(HistoricalBar.symbol == "SPX")
            .where(HistoricalBar.bar_size == "1 min")
        )).all())

    bars.sort(key=lambda b: b.timestamp_ms)
    today_actionable = [r for r in sigs if r.direction.value in ("LONG_CALLS", "LONG_PUTS")]

    # Resolve filter functions
    fns = []
    for name in filter_names:
        if name not in FILTERS:
            raise ValueError(f"unknown filter: {name}. Available: {list(FILTERS)}")
        fns.append(FILTERS[name])

    baseline_outcomes = []
    filtered_outcomes = []
    skipped_count = 0
    for r in today_actionable:
        ex = r.extras or {}
        t1 = ex.get("t1"); t2 = ex.get("t2"); t3 = ex.get("t3"); stop = ex.get("stop")
        if not all([t1, t2, t3, stop]): continue
        ts = r.timestamp
        if ts.tzinfo is None:
            from datetime import timezone as _tz
            ts = ts.replace(tzinfo=_tz.utc)
        start_ms = int(ts.timestamp() * 1000)
        is_lc = r.direction.value == "LONG_CALLS"
        outcome_r = _outcome_R(start_ms, r.spx_price, t1, t2, t3, stop, is_lc, bars)
        baseline_outcomes.append(outcome_r)
        if all(fn(r, ex) for fn in fns):
            filtered_outcomes.append(outcome_r)
        else:
            skipped_count += 1

    def _stats(outs: list) -> dict:
        n = len(outs)
        wins = sum(1 for r in outs if r > 0)
        return {
            "n": n, "wins": wins,
            "win_pct": round(wins / n * 100, 1) if n else 0,
            "expectancy_r": round(sum(outs) / n, 3) if n else 0,
            "total_r": round(sum(outs), 2),
        }

    baseline = _stats(baseline_outcomes)
    filtered = _stats(filtered_outcomes)
    return {
        "filters_applied": filter_names,
        "session_date": session_date or "all",
        "baseline": baseline,
        "filtered": filtered,
        "skipped_by_filter": skipped_count,
        "expectancy_delta_r": round(filtered["expectancy_r"] - baseline["expectancy_r"], 3),
        "total_r_delta": round(filtered["total_r"] - baseline["total_r"], 2),
    }
