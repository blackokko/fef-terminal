"""Signal lookahead worker — populate SignalLookaheadOutcome rows so we
can answer the question "what did SPX do in the N min after each signal?"

This closes the "non-fire blind spot": the engine logs trade outcomes for
signals that FIRED, but we have no way to tell whether WAIT verdicts
correctly skipped chop or quietly missed real moves. By walking forward
through SignalIter rows once they've aged past each lookahead window
(5, 15, 30, 60 min) and recording MFE/MAE from the corresponding SPX
1-min bars, we get a fair signal-strength metric independent of exit
logic.

Pure read/analytics. Doesn't touch verdict generation. Freeze-safe.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import HistoricalBar, SignalIter, SignalLookaheadOutcome

logger = logging.getLogger(__name__)


LOOKAHEAD_MINUTES: tuple[int, ...] = (5, 15, 30, 60)


def _compute_t1_hit_probability(
    bars: list[HistoricalBar],
    t1_spx: float | None,
    stop_spx: float | None,
    direction: str,
) -> float:
    """Estimate P(T1 fired before stop) using within-bar geometry.

    Audit priority #2 (Claude Chat methodology review 2026-05-21): the
    boolean would_have_hit_t1 / would_have_hit_stop fields treat 1-min
    bars as atomic — a bar whose low touched stop AND high touched T1
    records BOTH True even though only one fired first in real ticks.
    This systematically overstates T1 win rates.

    Heuristic (Brownian-bridge–style proxy; see Glasserman, "Monte Carlo
    Methods in Financial Engineering" (2004) Ch 6 for the academic
    treatment): scan bars in time order, return the first bar where
    EITHER T1 or stop was touched. If only one was touched in that bar,
    the answer is deterministic (1.0 or 0.0). If BOTH were touched in
    the same bar, use the open-relative excursion split as the proxy for
    buy/sell-pressure dominance:

        p_t1_first = upward_excursion / (upward_excursion + downward_excursion)

    For LONG_CALLS, upward_excursion = high - open (favors T1).
    For LONG_PUTS, downward_excursion = open - low (favors T1, since T1
    is below entry for puts), so we invert the ratio.

    Returns 0.0 if T1 was never touched, 1.0 if T1 touched and stop
    never did across the window, fractional in the ambiguous case.
    """
    if not t1_spx:
        return 0.0
    t1 = float(t1_spx)
    stop = float(stop_spx) if stop_spx else None

    for b in bars:
        if direction == "LONG_CALLS":
            t1_touched = b.high >= t1
            stop_touched = stop is not None and b.low <= stop
        elif direction == "LONG_PUTS":
            t1_touched = b.low <= t1
            stop_touched = stop is not None and b.high >= stop
        else:
            return 0.0

        if not t1_touched and not stop_touched:
            continue
        if t1_touched and not stop_touched:
            return 1.0
        if stop_touched and not t1_touched:
            return 0.0
        # Both touched in this bar — geometry proxy off the open.
        up = max(0.0, b.high - b.open)
        down = max(0.0, b.open - b.low)
        total = up + down
        if total <= 0.0:
            return 0.5  # degenerate flat bar; uninformative
        if direction == "LONG_CALLS":
            return up / total
        else:  # LONG_PUTS — downward excursion favors T1
            return down / total

    # T1 never touched across the window
    return 0.0


async def _populate_one(signal: SignalIter, lookahead_min: int) -> SignalLookaheadOutcome | None:
    """Compute outcome for ONE signal at ONE lookahead window.

    Returns None if SPX bars aren't yet available for the full window
    (signal too recent, or bars not yet persisted).

    NOTE on within-bar ambiguity: the boolean `would_have_hit_t1` and
    `would_have_hit_stop` fields are computed independently per bar and
    will BOTH be True on any bar that straddled both levels. Callers
    doing win-rate statistics should prefer `t1_hit_probability` (a
    geometry-weighted P(T1 first | both touched) ∈ [0,1]) for
    statistically honest counts. The booleans are retained for
    backwards compatibility with existing analyses.
    """
    start_ms = int(signal.timestamp.replace(tzinfo=timezone.utc).timestamp() * 1000) \
        if signal.timestamp.tzinfo is None \
        else int(signal.timestamp.timestamp() * 1000)
    end_ms = start_ms + lookahead_min * 60 * 1000

    async with get_session() as session:
        bars = list((await session.exec(
            select(HistoricalBar)
            .where(HistoricalBar.symbol == "SPX")
            .where(HistoricalBar.bar_size == "1 min")
            .where(HistoricalBar.timestamp_ms >= start_ms)
            .where(HistoricalBar.timestamp_ms <= end_ms)
            .order_by(HistoricalBar.timestamp_ms.asc())  # type: ignore[attr-defined]
        )).all())

    if not bars or len(bars) < max(2, lookahead_min // 5):
        return None  # not enough data yet

    spx_at_signal = float(signal.spx_price)
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    max_up = max(highs) - spx_at_signal
    max_down = spx_at_signal - min(lows)
    close_end = float(bars[-1].close)

    direction = signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction)
    extras = signal.extras or {}
    opt_pricing = extras.get("option_pricing") or {}
    t1_spx = opt_pricing.get("t1") if opt_pricing else extras.get("t1")
    t2_spx = opt_pricing.get("t2") if opt_pricing else extras.get("t2")
    t3_spx = opt_pricing.get("t3") if opt_pricing else extras.get("t3")
    stop_spx = opt_pricing.get("stop") if opt_pricing else extras.get("stop")

    if direction == "LONG_CALLS":
        mfe_pts = max_up
        mae_pts = -max_down
        hit_t1 = bool(t1_spx and max(highs) >= float(t1_spx))
        hit_t2 = bool(t2_spx and max(highs) >= float(t2_spx))
        hit_t3 = bool(t3_spx and max(highs) >= float(t3_spx))
        hit_stop = bool(stop_spx and min(lows) <= float(stop_spx))
    elif direction == "LONG_PUTS":
        mfe_pts = max_down
        mae_pts = -max_up
        hit_t1 = bool(t1_spx and min(lows) <= float(t1_spx))
        hit_t2 = bool(t2_spx and min(lows) <= float(t2_spx))
        hit_t3 = bool(t3_spx and min(lows) <= float(t3_spx))
        hit_stop = bool(stop_spx and max(highs) >= float(stop_spx))
    else:  # WAIT — no directional bias; report unsigned ranges
        mfe_pts = max(max_up, max_down)
        mae_pts = -min(max_up, max_down)
        hit_t1 = False
        hit_t2 = False
        hit_t3 = False
        hit_stop = False

    # Within-bar order probability — fast paths for the unambiguous cases,
    # geometry proxy for the both-touched case. See _compute_t1_hit_probability.
    if direction in ("LONG_CALLS", "LONG_PUTS"):
        if not hit_t1:
            t1_prob = 0.0
        elif hit_t1 and not hit_stop:
            t1_prob = 1.0
        else:  # both flagged across the window — walk bars to find first-touch
            t1_prob = _compute_t1_hit_probability(bars, t1_spx, stop_spx, direction)
    else:
        t1_prob = 0.0

    confidence = signal.confidence.value if hasattr(signal.confidence, "value") else str(signal.confidence)

    return SignalLookaheadOutcome(
        signal_iter_id=signal.id,  # type: ignore[arg-type]
        session_date=signal.session_date,
        lookahead_minutes=lookahead_min,
        spx_at_signal=spx_at_signal,
        spx_mfe_pts=float(mfe_pts),
        spx_mae_pts=float(mae_pts),
        spx_close_at_window_end=close_end,
        would_have_hit_t1=hit_t1,
        would_have_hit_t2=hit_t2,
        would_have_hit_t3=hit_t3,
        would_have_hit_stop=hit_stop,
        t1_hit_probability=float(t1_prob),
        direction_at_signal=direction,
        confidence_at_signal=confidence,
    )


async def populate_for_signal(signal_iter_id: int) -> int:
    """Populate outcome rows for one specific signal across all lookahead
    windows. Returns the number of rows actually written (skips windows
    that lack enough forward bars yet).
    """
    async with get_session() as session:
        sig = (await session.exec(
            select(SignalIter).where(SignalIter.id == signal_iter_id)  # type: ignore[attr-defined]
        )).first()
        if sig is None:
            return 0
        existing = list((await session.exec(
            select(SignalLookaheadOutcome)
            .where(SignalLookaheadOutcome.signal_iter_id == signal_iter_id)  # type: ignore[attr-defined]
        )).all())
        done_windows = {r.lookahead_minutes for r in existing}

    written = 0
    rows_to_add: list[SignalLookaheadOutcome] = []
    for window in LOOKAHEAD_MINUTES:
        if window in done_windows:
            continue
        row = await _populate_one(sig, window)
        if row is not None:
            rows_to_add.append(row)
    if rows_to_add:
        async with get_session() as session:
            for r in rows_to_add:
                session.add(r)
            written = len(rows_to_add)
    return written


async def backfill_session(session_date: str) -> dict:
    """Backfill all signals from one session_date. Returns summary stats."""
    async with get_session() as session:
        signals = list((await session.exec(
            select(SignalIter)
            .where(SignalIter.session_date == session_date)  # type: ignore[attr-defined]
        )).all())

    total_written = 0
    signals_touched = 0
    for sig in signals:
        if sig.id is None:
            continue
        n = await populate_for_signal(sig.id)
        if n > 0:
            signals_touched += 1
            total_written += n
    return {
        "session_date": session_date,
        "signals_scanned": len(signals),
        "signals_touched": signals_touched,
        "rows_written": total_written,
    }


async def lookahead_worker(state) -> None:
    """Background loop: every 5 minutes, walk signals from today + yesterday
    that don't yet have all 4 lookahead windows filled, and backfill those
    whose lookahead windows are now in the past.

    Idempotent — `populate_for_signal` skips windows already populated.
    """
    while True:
        try:
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            today_et = _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")
            yesterday_et = (_dt.now(_tz(_td(hours=-4))) - _td(days=1)).strftime("%Y-%m-%d")
            for date in (yesterday_et, today_et):
                try:
                    result = await backfill_session(date)
                    if result["rows_written"] > 0:
                        logger.info(
                            "Signal lookahead backfill %s: scanned=%d touched=%d wrote=%d",
                            date, result["signals_scanned"],
                            result["signals_touched"], result["rows_written"],
                        )
                except Exception:
                    logger.exception("Lookahead backfill failed for %s", date)
            await asyncio.sleep(300)  # 5 min cadence
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("lookahead_worker iteration failed")
            await asyncio.sleep(60)
