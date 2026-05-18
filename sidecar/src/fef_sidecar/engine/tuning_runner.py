"""Glue: pull DailyReviews + EngineChanges from DB, run tuning_recommender,
persist results to TuningRecommendation table.

Two entrypoints:
  run_tuning_recommendations() -- production nightly run, all-time window
  run_tuning_recommendations_in_window(start, end) -- manual recompute
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import DailyReview, EngineChange, TuningRecommendation
from fef_sidecar.engine.tuning_recommender import evaluate

logger = logging.getLogger(__name__)


async def _load(start: Optional[str] = None, end: Optional[str] = None):
    async with get_session() as session:
        rstmt = select(DailyReview).order_by(DailyReview.session_date.asc())  # type: ignore
        if start: rstmt = rstmt.where(DailyReview.session_date >= start)
        if end: rstmt = rstmt.where(DailyReview.session_date <= end)
        reviews = list((await session.exec(rstmt)).all())
        changes = list((await session.exec(
            select(EngineChange).order_by(EngineChange.applied_at.asc())  # type: ignore
        )).all())
    return reviews, changes


async def run_tuning_recommendations(start: Optional[str] = None,
                                     end: Optional[str] = None) -> dict:
    """Compute and persist a fresh batch of TuningRecommendation rows.

    Existing OPEN recs are SUPERSEDED (status flips) so the UI only shows
    the latest analysis. APPLIED/DISMISSED rows are preserved.
    """
    reviews, changes = await _load(start, end)
    recs = evaluate(reviews, changes, now=datetime.now(timezone.utc))

    async with get_session() as session:
        # Supersede existing OPEN recs
        prior_open = list((await session.exec(
            select(TuningRecommendation).where(TuningRecommendation.status == "OPEN")
        )).all())
        for p in prior_open:
            p.status = "SUPERSEDED"
            session.add(p)
        for r in recs:
            session.add(r)
        await session.commit()

    logger.info("Tuning recommendations: persisted %d new (superseded %d open) "
                "from %d reviews + %d changes",
                len(recs), len(prior_open), len(reviews), len(changes))
    return {"persisted": len(recs), "superseded_prior": len(prior_open),
            "reviews_window": len(reviews), "engine_changes": len(changes)}
