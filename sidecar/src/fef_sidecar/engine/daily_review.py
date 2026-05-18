"""Daily session report card.

Pulls every signal from a given session_date, walks SPX bars forward to
score outcomes (T1/T2/T3/STOP/TIMEOUT), and writes a DailyReview row.
Run either nightly (via auto_walk_forward scheduler) or on-demand via
POST /admin/daily_review.

The R-multiple calculation mirrors the analysis I did interactively:
  risk = |spx_at_signal - stop|
  realized_R = -1 if STOP, weighted-mean(R-tier × tier-share) if a tier
  hit, 0 if timeout. Uses the same 50/30/20 split as the auto-trader.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import DailyReview, HistoricalBar, SignalIter
from fef_sidecar.db.repository import SignalIterRepository

logger = logging.getLogger(__name__)


def _trace_outcome(start_ms: int, spx_entry: float, t1: float, t2: float,
                   t3: float, stop: float, is_long_calls: bool,
                   bars: list, window_min: int = 60) -> dict:
    end_ms = start_ms + window_min * 60_000
    out = {"t1": None, "t2": None, "t3": None, "stop": None, "mfe": 0.0}
    for b in bars:
        if b.timestamp_ms < start_ms:
            continue
        if b.timestamp_ms > end_ms:
            break
        elapsed = (b.timestamp_ms - start_ms) / 60_000
        hi, lo = b.high, b.low
        if is_long_calls:
            out["mfe"] = max(out["mfe"], hi - spx_entry)
            if out["stop"] is None and lo <= stop:
                out["stop"] = elapsed; break
            if out["t1"] is None and hi >= t1: out["t1"] = elapsed
            if out["t2"] is None and hi >= t2: out["t2"] = elapsed
            if out["t3"] is None and hi >= t3: out["t3"] = elapsed
        else:
            out["mfe"] = max(out["mfe"], spx_entry - lo)
            if out["stop"] is None and hi >= stop:
                out["stop"] = elapsed; break
            if out["t1"] is None and lo <= t1: out["t1"] = elapsed
            if out["t2"] is None and lo <= t2: out["t2"] = elapsed
            if out["t3"] is None and lo <= t3: out["t3"] = elapsed
    return out


async def compute_daily_review(session_date: str) -> DailyReview:
    """Compute (or recompute) the DailyReview row for the given session_date.
    Overwrites any existing row for that date."""
    async with get_session() as session:
        sigs = list((await session.exec(
            select(SignalIter).where(SignalIter.session_date == session_date)
        )).all())
        bars = list((await session.exec(
            select(HistoricalBar)
            .where(HistoricalBar.symbol == "SPX")
            .where(HistoricalBar.bar_size == "1 min")
        )).all())

    bars.sort(key=lambda b: b.timestamp_ms)
    today_actionable = [r for r in sigs if r.direction.value in ("LONG_CALLS", "LONG_PUTS")]

    by_trigger = {"confluence": [], "aligned_only": [], "wall_only": []}
    by_confidence = {"HIGH": [], "MEDIUM": [], "LOW": []}
    by_hour_ct: dict[str, list] = {}
    tier_counts = Counter()
    realized_total = 0.0

    for r in today_actionable:
        ex = r.extras or {}
        t1 = ex.get("t1"); t2 = ex.get("t2"); t3 = ex.get("t3"); stop = ex.get("stop")
        if not all([t1, t2, t3, stop]):
            continue
        is_lc = r.direction.value == "LONG_CALLS"
        ts = r.timestamp
        if ts.tzinfo is None: ts = ts.replace(tzinfo=UTC)
        start_ms = int(ts.timestamp() * 1000)
        spx = r.spx_price
        out = _trace_outcome(start_ms, spx, t1, t2, t3, stop, is_lc, bars)

        risk = abs(spx - stop) or 1.0
        if out["stop"] is not None:
            realized_R = -1.0
            tier_counts["STOP"] += 1
            outcome = "STOP"
        elif out["t1"] is not None:
            r_t1 = abs(t1 - spx) / risk
            r_t2 = abs(t2 - spx) / risk if out["t2"] is not None else r_t1
            r_t3 = abs(t3 - spx) / risk if out["t3"] is not None else (
                abs(t2 - spx) / risk if out["t2"] is not None else r_t1)
            realized_R = 0.50 * r_t1 + 0.30 * r_t2 + 0.20 * r_t3
            if out["t3"] is not None:
                tier_counts["T3"] += 1; outcome = "T3"
            elif out["t2"] is not None:
                tier_counts["T2"] += 1; outcome = "T2"
            else:
                tier_counts["T1"] += 1; outcome = "T1"
        else:
            realized_R = 0.0; tier_counts["TIMEOUT"] += 1; outcome = "TIMEOUT"

        realized_total += realized_R

        wall = bool(ex.get("wall_collapse_trigger"))
        aligned = bool(ex.get("aligned"))
        if wall and aligned:
            by_trigger["confluence"].append(realized_R)
        elif aligned:
            by_trigger["aligned_only"].append(realized_R)
        elif wall:
            by_trigger["wall_only"].append(realized_R)
        by_confidence[r.confidence.value if hasattr(r.confidence, "value") else str(r.confidence)] = (
            by_confidence.get(r.confidence.value if hasattr(r.confidence, "value") else str(r.confidence), []))
        try:
            by_confidence[r.confidence.value if hasattr(r.confidence, "value") else str(r.confidence)].append(realized_R)
        except KeyError:
            pass
        # CT hour bucketing
        ct = ts.astimezone(timezone(timedelta(hours=-5)))
        hour_key = f"{ct.hour:02d}"
        by_hour_ct.setdefault(hour_key, []).append(realized_R)

    n_actionable = len(today_actionable)
    wins = tier_counts.get("T1", 0) + tier_counts.get("T2", 0) + tier_counts.get("T3", 0)
    stops = tier_counts.get("STOP", 0)
    timeouts = tier_counts.get("TIMEOUT", 0)

    def _summary(rs: list) -> dict:
        n = len(rs)
        wn = sum(1 for r in rs if r > 0)
        return {"n": n, "wins": wn, "win_pct": round(wn / n * 100, 1) if n else 0,
                "expectancy_r": round(sum(rs) / n, 3) if n else 0}

    review = DailyReview(
        session_date=session_date,
        computed_at=datetime.now(UTC),
        total_signals=len(sigs),
        actionable_signals=n_actionable,
        wins=wins, stops=stops, timeouts=timeouts,
        win_rate_pct=round(wins / n_actionable * 100, 1) if n_actionable else 0,
        expectancy_r=round(realized_total / n_actionable, 3) if n_actionable else 0,
        t1_hits=tier_counts.get("T1", 0),
        t2_hits=tier_counts.get("T2", 0),
        t3_hits=tier_counts.get("T3", 0),
        by_trigger={k: _summary(v) for k, v in by_trigger.items()},
        by_confidence={k: _summary(v) for k, v in by_confidence.items()},
        by_hour_ct={k: _summary(v) for k, v in by_hour_ct.items()},
    )

    # Upsert: delete any existing row for this date first
    async with get_session() as session:
        existing = (await session.exec(
            select(DailyReview).where(DailyReview.session_date == session_date)
        )).first()
        if existing is not None:
            await session.delete(existing)
            await session.commit()
        session.add(review)
        await session.commit()
        await session.refresh(review)

    await SignalIterRepository.write_audit_log(
        category="daily_review", actor="system",
        summary=f"DailyReview {session_date}: {n_actionable} fires, {wins} won "
                f"({review.win_rate_pct}%), expectancy {review.expectancy_r:+.3f}R",
    )
    logger.info("DailyReview computed for %s: %d signals %.1f%% wins %.3fR exp",
                session_date, n_actionable, review.win_rate_pct, review.expectancy_r)
    return review
