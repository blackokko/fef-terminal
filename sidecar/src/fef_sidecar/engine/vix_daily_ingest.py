"""VIX 1-day ingest — keeps the historical_bar VIX daily series current.

The lo-VIX day classifier (engine/lo_vix_day.py) ranks against the
historical_bar (symbol='VIX', bar_size='1 day') series, which was
previously extended only by the MANUAL maintenance script
(scripts/maintenance/backfill_vix.py) — it silently lapsed (last row
2026-07-01 while 07-07 traded) and the classifier would have ranked a
4-session-old close, confidently (review MAJOR-3). lo_vix_day's staleness
guard now fails that open; this loop fixes the supply side: one pass at
boot plus one pass per session after the close.

Mechanics: fetch recent VIX daily bars from IBKR
(state.ibkr.fetch_historical_bars — the dedicated historical connection)
and INSERT the missing COMPLETED sessions. Additive-only and idempotent
(existing timestamps are skipped; uq_bar backstops). Today's still-forming
bar is NEVER inserted pre-close — its open is final but a frozen partial
CLOSE would poison every future session's prior-close pool.

Timestamps anchor at 16:00 America/New_York (DST-aware), byte-matching the
backfill script's convention so both writers land IDENTICAL rows —
vix_daily_bar_ts_ms here is the single definition and the script imports
it.

SAME-SERIES RULE (see lo_vix_day docstring): rows must come from the same
IBKR CBOE VIX index the backfill wrote. LiveIBKRProvider ONLY — a mock
provider's synthetic bars must never enter the real series.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time as _time
from typing import Any, Iterable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET_ZONE = ZoneInfo("America/New_York")
VIX_SYMBOL = "VIX"
VIX_BAR_SIZE = "1 day"
CLOSE_MINUTE_ET = 16 * 60           # a session's daily bar completes at 16:00 ET
INGEST_AFTER_MINUTE_ET = 16 * 60 + 15  # daily pass runs at/after 16:15 ET
CHECK_INTERVAL_SEC = 15 * 60        # loop wake cadence (heartbeat + close check)
FETCH_DURATION = "2 M"              # covers lo_vix_day's 45-day lookback + margin


def vix_daily_bar_ts_ms(session_date: dt.date) -> int:
    """Anchor a VIX daily bar at the 16:00 ET close — the EXACT convention
    scripts/maintenance/backfill_vix.py wrote the series with (DST-aware
    zoneinfo, NOT the app's fixed-offset _ET). Both writers must land
    identical timestamps or the (symbol, bar_size, ts) dedup breaks."""
    return int(dt.datetime(
        session_date.year, session_date.month, session_date.day, 16, 0,
        tzinfo=ET_ZONE,
    ).timestamp() * 1000)


def _bar_session_date(bar: Any) -> dt.date:
    """ET session date of one fetched daily bar. IBKR daily bars arrive
    date-anchored (naive midnight after Bar-model coercion); a tz-aware
    timestamp is converted to ET first."""
    ts = bar.timestamp
    if ts.tzinfo is not None:
        return ts.astimezone(ET_ZONE).date()
    return ts.date()


def completed_daily_rows(
    bars: Iterable[Any], now_ms: int,
) -> list[tuple[int, float, float, float, float, float]]:
    """(ts_ms, o, h, l, c, v) rows for COMPLETED sessions only — a bar
    dated today is included only once the ET clock has passed the close;
    future-dated or open<=0 bars are dropped."""
    now_et = dt.datetime.fromtimestamp(now_ms / 1000, ET_ZONE)
    today = now_et.date()
    past_close = (now_et.hour * 60 + now_et.minute) >= CLOSE_MINUTE_ET
    out: list[tuple[int, float, float, float, float, float]] = []
    for b in bars or []:
        d = _bar_session_date(b)
        if d > today or (d == today and not past_close):
            continue
        if not b.open or b.open <= 0:
            continue
        out.append((
            vix_daily_bar_ts_ms(d), float(b.open), float(b.high),
            float(b.low), float(b.close), float(b.volume or 0.0),
        ))
    return out


async def upsert_vix_daily(
    rows: list[tuple[int, float, float, float, float, float]],
) -> int:
    """INSERT the missing (VIX, '1 day', timestamp_ms) rows — additive
    only, idempotent (existing timestamps skipped; uq_bar backstops).
    Returns the number of rows inserted."""
    if not rows:
        return 0
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import HistoricalBar
    min_ts = min(r[0] for r in rows)
    inserted = 0
    async with get_session() as session:
        existing = {
            b.timestamp_ms for b in (await session.exec(
                select(HistoricalBar)
                .where(HistoricalBar.symbol == VIX_SYMBOL)
                .where(HistoricalBar.bar_size == VIX_BAR_SIZE)
                .where(HistoricalBar.timestamp_ms >= min_ts)
            )).all()
        }
        for ts, o, h, lo, c, v in rows:
            if ts in existing:
                continue
            session.add(HistoricalBar(
                symbol=VIX_SYMBOL, bar_size=VIX_BAR_SIZE, timestamp_ms=ts,
                open=o, high=h, low=lo, close=c, volume=v,
            ))
            existing.add(ts)
            inserted += 1
    return inserted


def _log_skip_once(state: Any, msg: str) -> None:
    if getattr(state, "_vix_ingest_skip_logged", None) == msg:
        return
    try:
        state._vix_ingest_skip_logged = msg
    except Exception:
        pass
    logger.info("VIX daily ingest: %s", msg)


async def run_vix_daily_ingest_once(state: Any, *, now_ms: int | None = None) -> int | None:
    """One fetch+upsert pass. Returns the inserted count, or None when
    skipped (disabled / non-live provider) or failed (retried on the next
    scheduled pass). Never raises."""
    enabled = os.environ.get(
        "FEF_VIX_DAILY_INGEST_ENABLED", "true"
    ).lower() in ("true", "1", "yes", "on")
    if not enabled:
        _log_skip_once(state, "disabled via FEF_VIX_DAILY_INGEST_ENABLED")
        return None
    try:
        from fef_sidecar.ibkr.live import LiveIBKRProvider
        if not isinstance(getattr(state, "ibkr", None), LiveIBKRProvider):
            # SAME-SERIES RULE: never write a mock provider's synthetic
            # bars into the real VIX series.
            _log_skip_once(state, "non-live IBKR provider — skipped (same-series rule)")
            return None
        from fef_sidecar.ibkr.models import Contract
        bars = await state.ibkr.fetch_historical_bars(
            Contract.vix_index(), bar_size=VIX_BAR_SIZE,
            duration=FETCH_DURATION, use_rth=True, timeout_seconds=30.0,
        )
        now = int(now_ms if now_ms is not None else _time.time() * 1000)
        rows = completed_daily_rows(bars, now)
        inserted = await upsert_vix_daily(rows)
        logger.info(
            "VIX daily ingest: fetched=%d completed=%d inserted=%d",
            len(bars or []), len(rows), inserted,
        )
        if inserted:
            from fef_sidecar.db.repository import SignalIterRepository
            await SignalIterRepository.write_audit_log(
                category="vix_daily_ingest", actor="system",
                summary=(
                    f"VIX daily ingest: inserted {inserted} missing session "
                    f"row(s) into historical_bar (fetched {len(bars or [])} bars)"
                ),
                details={"inserted": inserted, "fetched": len(bars or [])},
            )
        return inserted
    except Exception:
        logger.exception(
            "VIX daily ingest pass failed — retried on the next scheduled pass"
        )
        return None


async def vix_daily_ingest_loop(state: Any) -> None:
    """Supervised loop: one pass at boot (fills any gap the manual backfill
    left), then one pass per session at/after 16:15 ET. Wakes every
    CHECK_INTERVAL_SEC for the watchdog heartbeat; a failed close-pass is
    retried on the next wake (the session is only marked done on success)."""
    from fef_sidecar.engine.loop_watchdog import heartbeat as _wd_beat
    await asyncio.sleep(20)  # let the IBKR provider connect first
    _wd_beat(state, "vix-daily-ingest")
    await run_vix_daily_ingest_once(state)
    last_close_run: str | None = None
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SEC)
        _wd_beat(state, "vix-daily-ingest")
        try:
            now_et = dt.datetime.now(ET_ZONE)
            sess = now_et.strftime("%Y-%m-%d")
            past_ingest_time = (
                now_et.hour * 60 + now_et.minute
            ) >= INGEST_AFTER_MINUTE_ET
            if past_ingest_time and last_close_run != sess:
                if await run_vix_daily_ingest_once(state) is not None:
                    last_close_run = sess
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("VIX daily ingest loop iteration failed")
