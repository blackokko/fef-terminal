"""Fire-order labeling.

For each actionable signal, compute `fire_order` = number of preceding
actionable signals on the same (direction, strike) within the last
DEDUP_WINDOW_MIN minutes. fire_order=1 means it's the FIRST signal in
the cluster; fire_order=2 means second; etc.

Used to answer "is the bot's edge in first-fires or in stacks?" — by
bucketing realized outcomes by fire_order, we can tell whether stacks
add value (signal quality clarifies after first fire) or hurt
(over-commitment to a decaying setup).

Backfillable from existing SignalIter rows; forward-applied at fire
time by loop.py.
"""

from __future__ import annotations

import logging
from datetime import timezone

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import SignalIter

logger = logging.getLogger(__name__)


DEDUP_WINDOW_MIN = 5


def _utc_ms(dt) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _signal_cluster_key(direction_value: str, strike_rec) -> str | None:
    if direction_value == "WAIT" or strike_rec is None:
        return None
    return f"{direction_value}|{strike_rec}"


async def backfill_fire_order(session_date: str | None = None) -> dict:
    """Walk SignalIters chronologically and stamp fire_order into extras.

    If session_date is None, processes all rows. Otherwise only the
    given session.
    """
    written = 0
    scanned = 0
    async with get_session() as session:
        stmt = select(SignalIter)
        if session_date:
            stmt = stmt.where(SignalIter.session_date == session_date)
        stmt = stmt.order_by(SignalIter.timestamp.asc())  # type: ignore[attr-defined]
        sigs = list((await session.exec(stmt)).all())

        last_fire_ms_by_key: dict[str, list[int]] = {}
        window_ms = DEDUP_WINDOW_MIN * 60_000
        for sig in sigs:
            scanned += 1
            direction = sig.direction.value if hasattr(sig.direction, "value") else str(sig.direction)
            strike = (sig.extras or {}).get("strike_rec")
            key = _signal_cluster_key(direction, strike)
            ts_ms = _utc_ms(sig.timestamp)
            if key is None:
                fire_order = 0  # WAIT or unknown strike — not part of any cluster
            else:
                prior = last_fire_ms_by_key.get(key, [])
                # Remove fires outside the window
                prior = [t for t in prior if (ts_ms - t) < window_ms]
                fire_order = len(prior) + 1
                prior.append(ts_ms)
                last_fire_ms_by_key[key] = prior
            # Stamp into extras — merge to preserve keys from other backfills
            if (sig.extras or {}).get("fire_order") != fire_order:
                sig.extras = {**(sig.extras or {}), "fire_order": fire_order}
                session.add(sig)
                written += 1

    return {
        "session_date": session_date,
        "scanned": scanned,
        "rows_updated": written,
    }


def compute_fire_order_live(
    state,
    direction_value: str,
    strike_rec: float | None,
) -> int:
    """Compute fire_order for a brand-new signal about to be written,
    using state.recent_signal_fires (a small in-memory cache).

    state.recent_signal_fires: dict[str, list[int_ms]] keyed by
    "{direction}|{strike}", pruned to last 10 min.
    """
    from time import time as _now
    now_ms = int(_now() * 1000)
    cache = getattr(state, "recent_signal_fires", None)
    if cache is None:
        cache = {}
        state.recent_signal_fires = cache  # type: ignore[attr-defined]

    key = _signal_cluster_key(direction_value, strike_rec)
    if key is None:
        return 0

    window_ms = DEDUP_WINDOW_MIN * 60_000
    prior = cache.get(key, [])
    prior = [t for t in prior if (now_ms - t) < window_ms]
    fire_order = len(prior) + 1
    prior.append(now_ms)
    cache[key] = prior

    # Prune stale keys periodically (every 100 calls): drop keys whose
    # most recent fire is older than 30 min
    total_keys = len(cache)
    if total_keys > 50:
        stale_cutoff = now_ms - 30 * 60_000
        stale_keys = [k for k, v in cache.items() if not v or max(v) < stale_cutoff]
        for k in stale_keys:
            cache.pop(k, None)

    return fire_order
