"""EOD QuantData backfill for option chain history.

For each signal_iter in the day, find the unique (strike, right, expiry)
contracts that were near the ATM window, pull the QuantData intraday
price path for each, and write one HistoricalOptionChainPoint per
(timestamp_ms, strike, right, expiry) sample.

Source tag: 'quantdata_backfill'. No greeks (QuantData's contract_price_time
endpoint only returns price+volume) — those stay null. The live capture loop
fills greeks via IBKR snapshot mode.

Idempotent: writes go through the same UNIQUE index on
(timestamp_ms, strike, right, expiry) as the live capture path. If a
backfill runs twice or overlaps with already-captured IBKR rows, the
duplicates silently no-op.

Wired into auto_walk_forward.nightly_scheduler AFTER daily_review runs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta, timezone

logger = logging.getLogger(__name__)


# Strike grid for SPX 0DTE. Keep narrow by default — QuantData calls cost
# real API budget, and the ATM window is the only zone shadow variants need.
DEFAULT_OFFSET_PTS = 25.0
DEFAULT_STEP_PTS = 5.0


def _today_expiry_yyyymmdd(session_date: str) -> str:
    """Translate session_date 'YYYY-MM-DD' into IBKR-style 'YYYYMMDD'."""
    return session_date.replace("-", "")


def _signal_iter_strikes(
    spx_price: float,
    offset_pts: float = DEFAULT_OFFSET_PTS,
    step: float = DEFAULT_STEP_PTS,
) -> list[float]:
    """ATM ± offset strike grid for a given SPX spot. Same builder as
    option_chain_capture._build_strike_list — duplicated here to avoid an
    import cycle (capture module imports from this is fine; reverse is not)."""
    if spx_price <= 0 or step <= 0:
        return []
    atm = round(spx_price / step) * step
    n = int(round(offset_pts / step))
    return [round(atm + i * step, 4) for i in range(-n, n + 1)]


async def backfill_session_chains_quantdata(
    state,
    session_date: str,
    *,
    offset_pts: float = DEFAULT_OFFSET_PTS,
    step: float = DEFAULT_STEP_PTS,
) -> int:
    """Backfill historical_option_chain_point rows from QuantData for one session.

    Strategy:
      1. Pull all SignalIter rows for session_date.
      2. Union their ATM ± offset strike grids → unique (strike, right) set.
      3. For each unique contract, fetch QuantData contract_price_time.
      4. For each minute sample, persist one HistoricalOptionChainPoint
         tagged source='quantdata_backfill'. spot_at_capture comes from
         the nearest signal_iter by time.

    Returns total row count persisted (after dedup against existing rows).
    """
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import SignalIter

    # 1. Collect today's iters.
    async with get_session() as session:
        iters_stmt = (
            select(SignalIter)
            .where(SignalIter.session_date == session_date)  # type: ignore[attr-defined]
            .order_by(SignalIter.timestamp.asc())  # type: ignore[attr-defined]
        )
        iters = list((await session.exec(iters_stmt)).all())

    if not iters:
        logger.info("Backfill: no signal_iter rows for %s", session_date)
        return 0

    # 2. Union strike sets across the day's spot trajectory. SPX 0DTE
    # walks through 30-100pt during a session so the union ends up being
    # the widest coverage zone any iter cared about.
    unique_contracts: set[tuple[float, str]] = set()
    for it in iters:
        for strike in _signal_iter_strikes(it.spx_price, offset_pts, step):
            unique_contracts.add((strike, "C"))
            unique_contracts.add((strike, "P"))

    expiry_yyyymmdd = _today_expiry_yyyymmdd(session_date)
    # QuantData's expiration param expects YYYY-MM-DD (no dashes stripped).
    expiry_iso = session_date

    quantdata = getattr(state, "quantdata", None)
    if quantdata is None:
        logger.warning("Backfill: no quantdata client on state, skipping")
        return 0

    symbol = getattr(state, "active_symbol", "SPX")
    total_persisted = 0

    # 3. Fetch each contract's intraday path. Sequential to be polite to
    # the API; ~50 contracts × ~1s/call = ~50s per session backfill.
    for strike, right in sorted(unique_contracts):
        try:
            snap = await quantdata.get_contract_price_time(
                strike=strike, right=right, expiration=expiry_iso, ticker=symbol,
            )
        except Exception as e:
            # Log but continue — one bad strike shouldn't kill the whole run.
            logger.debug(
                "Backfill: get_contract_price_time(%.1f %s) failed: %s",
                strike, right, e,
            )
            continue

        if not getattr(snap, "points", None):
            continue

        rows = _build_rows_from_snapshot(
            snap, iters=iters, symbol=symbol,
            session_date=session_date, expiry_yyyymmdd=expiry_yyyymmdd,
        )
        if rows:
            n = await _persist_rows(rows)
            total_persisted += n

    logger.info(
        "Backfill: %s → %d quantdata rows persisted across %d unique contracts",
        session_date, total_persisted, len(unique_contracts),
    )
    return total_persisted


def _build_rows_from_snapshot(
    snap,
    *,
    iters: list,
    symbol: str,
    session_date: str,
    expiry_yyyymmdd: str,
) -> list:
    """Convert one ContractPriceTimeSnapshot into HistoricalOptionChainPoint rows.

    spot_at_capture is interpolated from the nearest signal_iter by time.
    """
    from fef_sidecar.db.models import HistoricalOptionChainPoint

    if not iters:
        return []

    # Pre-build timestamp index from iters for nearest-by-time lookup.
    iter_index: list[tuple[int, float, int | None]] = []
    for it in iters:
        ts = it.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        iter_index.append((int(ts.timestamp() * 1000), it.spx_price, it.id))
    iter_index.sort(key=lambda x: x[0])
    iter_ts = [x[0] for x in iter_index]
    import bisect

    rows: list = []
    for pt in snap.points:
        # Find nearest signal_iter by timestamp_ms.
        idx = bisect.bisect_left(iter_ts, pt.timestamp_ms)
        candidates = []
        if idx < len(iter_index):
            candidates.append(iter_index[idx])
        if idx > 0:
            candidates.append(iter_index[idx - 1])
        nearest = min(
            candidates,
            key=lambda c: abs(c[0] - pt.timestamp_ms),
        ) if candidates else None
        spot = nearest[1] if nearest else 0.0
        signal_iter_id = nearest[2] if nearest else None

        rows.append(HistoricalOptionChainPoint(
            timestamp_ms=int(pt.timestamp_ms),
            session_date=session_date,
            signal_iter_id=signal_iter_id,
            symbol=symbol,
            strike=float(snap.strike),
            right=str(snap.right).upper()[:1],
            expiry=expiry_yyyymmdd,
            spot_at_capture=float(spot),
            last=float(pt.price) if pt.price is not None else None,
            mid=float(pt.price) if pt.price is not None else None,
            volume=int(pt.volume) if getattr(pt, "volume", None) else None,
            source="quantdata_backfill",
        ))
    return rows


async def _persist_rows(rows: list) -> int:
    """Bulk insert with IntegrityError-on-duplicate-no-op fallback. Same
    pattern as option_chain_capture._persist_rows."""
    if not rows:
        return 0

    from sqlalchemy.exc import IntegrityError
    from fef_sidecar.db.engine import get_session

    try:
        async with get_session() as session:
            session.add_all(rows)
            await session.flush()
        return len(rows)
    except IntegrityError:
        pass
    except Exception:
        logger.exception("Backfill bulk persist failed; trying per-row")

    n = 0
    for r in rows:
        try:
            async with get_session() as session:
                session.add(r)
                await session.flush()
            n += 1
        except IntegrityError:
            continue
        except Exception:
            logger.exception(
                "Backfill per-row persist failed for %s %s %s",
                getattr(r, "strike", "?"),
                getattr(r, "right", "?"),
                getattr(r, "expiry", "?"),
            )
    return n
