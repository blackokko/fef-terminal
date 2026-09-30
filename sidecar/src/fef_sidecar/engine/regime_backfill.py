"""Backfill regime_at_fire on historical signal_iter rows.

For each row missing extras.regime_at_fire: pull a rolling window of
prior rows (60 by default), feed them through a fresh RegimeDetector,
take the resulting assessment, write it into the row's extras.

Idempotent: skips rows that already have regime_at_fire set.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import select

from fef_sidecar import et_time
from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import SignalIter
from fef_sidecar.engine.regime import RegimeDetector, RegimeSample

logger = logging.getLogger(__name__)


def _to_sample(r: SignalIter) -> RegimeSample:
    ex = r.extras or {}
    ts = r.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    et = et_time.to_et(ts)
    mins = max(0, (et.hour - 9) * 60 + (et.minute - 30))
    return RegimeSample(
        spx_price=r.spx_price,
        drift_cum_net_m=r.drift_cum_net_m,
        drift_bias=r.drift_bias,
        wall_proximity_pts=ex.get("nearest_wall_distance"),
        aligned=bool(ex.get("aligned", False)),
        absorption=bool(ex.get("absorption", False)),
        direction=r.direction.value if hasattr(r.direction, "value") else str(r.direction),
        confidence=r.confidence.value if hasattr(r.confidence, "value") else str(r.confidence),
        fe_ratio=ex.get("fe_ratio"),
        minutes_since_open=mins,
        vix=None,
    )


async def backfill_regime_at_fire(session_date: str | None = None) -> dict:
    """Walk SignalIter rows for the given session_date (or all sessions
    if None) and fill in extras['regime_at_fire'] using a fresh
    RegimeDetector seeded with prior context."""

    async with get_session() as session:
        stmt = select(SignalIter).order_by(SignalIter.id.asc())  # type: ignore[attr-defined]
        if session_date:
            stmt = stmt.where(SignalIter.session_date == session_date)
        rows = list((await session.exec(stmt)).all())

        backfilled = 0
        skipped = 0
        detector = RegimeDetector(window_size=60)
        # Group rows by session_date so each session starts with a fresh detector
        by_date: dict[str, list[SignalIter]] = {}
        for r in rows:
            by_date.setdefault(r.session_date, []).append(r)

        for date, day_rows in by_date.items():
            detector = RegimeDetector(window_size=60)
            for r in day_rows:
                ex = r.extras or {}
                if ex.get("regime_at_fire"):
                    skipped += 1
                else:
                    # Push prior sample BEFORE assessing for this row, so the
                    # detector reflects context-leading-up-to. Then push this
                    # row's sample after assessing.
                    if detector.n >= 3:
                        assessment = detector.assess()
                        # Merge into existing extras to preserve keys written
                        # by other backfill passes (fire_order, etc.)
                        r.extras = {**(r.extras or {}), "regime_at_fire": assessment.regime}
                        session.add(r)
                        backfilled += 1
                detector.push(_to_sample(r))
        await session.commit()
    logger.info("regime_at_fire backfill: %d filled, %d skipped", backfilled, skipped)
    return {"backfilled": backfilled, "skipped": skipped}
