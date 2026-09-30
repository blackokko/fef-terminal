"""Option chain capture loop.

Snapshots the ATM ± offset option chain via IBKR snapshot market data
every 2 min during RTH. Powers shadow variants that need alternate-strike
replay (e.g., 'what would the ATM call have done on this signal').

DEFENSIVE DESIGN
----------------
- Runs as an isolated async task — any exception is caught at the iteration
  boundary; failure NEVER crashes the sidecar or affects other loops.
- Snapshot mode (no streaming subscription, no line cost) — uses the same
  reqMktData(snapshot=True) call path that get_option_quote uses but without
  any genericTickList so OPRA isn't asked for stuff paper accounts can't see.
- Timeout per contract (5s) — a frozen request can't accumulate state or
  block the next iteration.
- Idempotent persist (UNIQUE INDEX on timestamp_ms+strike+right+expiry) —
  re-runs of the same minute silently no-op via IntegrityError trap.
- FEF_CHAIN_CAPTURE_ENABLED env var to disable on demand.
- Does NOT touch signal_gen, auto_trader, or watcher code paths. The only
  IBKR resource shared with other loops is `state.ibkr._ib`, which already
  serializes via the asyncio event loop.

Storage rough estimate
----------------------
22 contracts × 1 row each × every 2 min × 6.5h RTH ≈ 4,290 rows/day.
At ~80 B/row in SQLite that's ~340 KB/day, ~90 MB/year. The unique
index halves dedup risk overhead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from datetime import UTC, datetime, timedelta

from fef_sidecar import et_time

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults — overridable via env. Numbers tuned for SPX 0DTE workflow.
# ---------------------------------------------------------------------------

DEFAULT_INTERVAL_SEC = 120     # match fef_iter cadence
DEFAULT_OFFSET_PTS = 25.0      # ATM ± 25pt → 11 strikes per side
DEFAULT_STEP_PTS = 5.0         # SPX strike grid
DEFAULT_TIMEOUT_SEC = 5.0      # per-contract reqMktData wait

# RTH window, ET wall clock: 09:30-16:00 (8:30am-3:00pm CT), whatever the DST
# state (the user trades 0DTE SPX which expires at market close anyway). The
# fef_iter loop fires through this window so chain capture should too. This
# was 13:30-20:00 UTC, which is the same window only in EDT: in EST it ran
# 08:30-15:00 ET and missed the last hour.
RTH_START_ET_HM = (9, 30)
RTH_END_ET_HM = (16, 0)


def _enabled() -> bool:
    return os.environ.get("FEF_CHAIN_CAPTURE_ENABLED", "true").lower() in (
        "true", "1", "yes", "on",
    )


def _within_rth(now: datetime | None = None) -> bool:
    """True if `now` (default: current time) is within 09:30-16:00 ET.
    A naive `now` is taken as UTC."""
    et_now = et_time.to_et(now or datetime.now(UTC))
    hm = (et_now.hour, et_now.minute)
    return RTH_START_ET_HM <= hm <= RTH_END_ET_HM


def _today_session_date_et() -> str:
    """Return YYYY-MM-DD in ET so it matches signal_iter.session_date."""
    return et_time.today_et()


def _today_expiry_yyyymmdd() -> str:
    """SPX 0DTE expiry — today in ET in YYYYMMDD form (matches IBKR contract
    lastTradeDateOrContractMonth). For weekends/holidays this naively returns
    today's date; capture will fail-gracefully on no-quote for non-trading
    days, no special-casing needed."""
    return et_time.now_et().strftime("%Y%m%d")


def _build_strike_list(spot: float, offset_pts: float, step_pts: float) -> list[float]:
    """Round spot to nearest step, then build ± offset list.

    Example: spot=7414.2, offset=25, step=5 → [7390, 7395, ..., 7440] (11 strikes).
    """
    if spot <= 0 or step_pts <= 0:
        return []
    atm = round(spot / step_pts) * step_pts
    # Number of strikes on each side (inclusive of ATM on the negative side)
    n_each_side = int(round(offset_pts / step_pts))
    strikes = [atm + i * step_pts for i in range(-n_each_side, n_each_side + 1)]
    return [round(s, 4) for s in strikes]


# ---------------------------------------------------------------------------
# Capture loop
# ---------------------------------------------------------------------------

async def option_chain_capture_loop(state) -> None:
    """Main background task. Runs until cancelled."""
    if not _enabled():
        logger.info("Option chain capture disabled via FEF_CHAIN_CAPTURE_ENABLED")
        return

    interval = float(os.environ.get(
        "FEF_CHAIN_CAPTURE_INTERVAL_SEC", str(DEFAULT_INTERVAL_SEC),
    ))
    offset = float(os.environ.get(
        "FEF_CHAIN_CAPTURE_OFFSET_PTS", str(DEFAULT_OFFSET_PTS),
    ))
    step = float(os.environ.get(
        "FEF_CHAIN_CAPTURE_STEP_PTS", str(DEFAULT_STEP_PTS),
    ))
    timeout = float(os.environ.get(
        "FEF_CHAIN_CAPTURE_TIMEOUT_SEC", str(DEFAULT_TIMEOUT_SEC),
    ))

    logger.info(
        "Option chain capture loop starting "
        "(interval=%.0fs, offset=%.1fpt, step=%.1fpt, timeout=%.1fs)",
        interval, offset, step, timeout,
    )

    while True:
        try:
            n = await _capture_once(
                state, offset_pts=offset, step=step, timeout=timeout,
            )
            if n > 0:
                logger.info("Chain capture: persisted %d rows", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Chain capture iteration failed (isolated, will retry)")
        await asyncio.sleep(interval)


async def _capture_once(
    state,
    *,
    offset_pts: float = DEFAULT_OFFSET_PTS,
    step: float = DEFAULT_STEP_PTS,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    now: datetime | None = None,
) -> int:
    """Run one capture iteration. Returns number of rows persisted.

    `now` parameter is for tests; production callers leave it None.
    """
    # Gate: RTH only. Outside that window we early-exit before any IBKR call.
    now = now or datetime.now(UTC)
    if not _within_rth(now):
        return 0

    # Pull live SPX spot from the freshest IBKR tick.
    #
    # [REMOVED 2026-08-25] QuantData subscription CANCELLED. `last_spx_price`
    # used to be refreshed by the QuantData poll loop, so it was a genuine
    # fallback when the tick stream went down. Nothing writes it now except
    # the spread iter, so in practice this is tick-or-nothing: with no tick,
    # spot is 0 and the capture skips the iteration (return 0 below) rather
    # than writing rows against a stale price.
    spot = float(getattr(state, "last_live_tick_spx", 0.0)
                 or getattr(state, "last_spx_price", 0.0) or 0.0)
    if spot <= 0:
        return 0

    # Skip if no IBKR provider, or it's the mock (mock doesn't have real
    # option chain data — would produce noise rows).
    ibkr = getattr(state, "ibkr", None)
    if ibkr is None:
        return 0
    is_live = type(ibkr).__name__ == "LiveIBKRProvider"
    if not is_live:
        return 0
    if not getattr(ibkr, "is_connected", False):
        return 0

    strikes = _build_strike_list(spot, offset_pts, step)
    if not strikes:
        return 0

    expiry = _today_expiry_yyyymmdd()
    session_date = _today_session_date_et()
    symbol = getattr(state, "active_symbol", "SPX")

    # Find the nearest signal_iter by time so the chain rows can be joined
    # back to whichever iter they live "alongside". Best-effort — None is fine.
    signal_iter_id = await _find_nearest_signal_iter_id(now)

    # Qualify all (strike, right) contracts in parallel. Each contract is
    # cached on state after first qualification so re-runs are free.
    contracts = await _qualify_chain_contracts(
        state, symbol=symbol, strikes=strikes, expiry=expiry,
    )
    if not contracts:
        return 0

    # Snapshot every (strike, right) in parallel with per-contract timeout.
    timestamp_ms = int(now.timestamp() * 1000)
    snapshot_tasks = [
        asyncio.wait_for(_snapshot_one(state, ic), timeout=timeout)
        for (_strike, _right, ic) in contracts
    ]
    results = await asyncio.gather(*snapshot_tasks, return_exceptions=True)

    # Build rows. Failed snapshots (timeout / exception) produce a row with
    # null prices/greeks so the absence is recorded too.
    from fef_sidecar.db.models import HistoricalOptionChainPoint
    rows: list[HistoricalOptionChainPoint] = []
    for (strike, right, _ic), result in zip(contracts, results, strict=False):
        if isinstance(result, BaseException):
            # Persist a row even on timeout/error — so downstream queries
            # can see "we tried this strike at this minute" vs "we never
            # captured here". All price fields stay None.
            rows.append(HistoricalOptionChainPoint(
                timestamp_ms=timestamp_ms,
                session_date=session_date,
                signal_iter_id=signal_iter_id,
                symbol=symbol,
                strike=strike,
                right=right,
                expiry=expiry,
                spot_at_capture=spot,
                source="ibkr_snapshot",
            ))
            continue
        bid = result.get("bid")
        ask = result.get("ask")
        last = result.get("last")
        mid = ((bid + ask) / 2.0) if (bid is not None and ask is not None) else None
        rows.append(HistoricalOptionChainPoint(
            timestamp_ms=timestamp_ms,
            session_date=session_date,
            signal_iter_id=signal_iter_id,
            symbol=symbol,
            strike=strike,
            right=right,
            expiry=expiry,
            spot_at_capture=spot,
            bid=bid,
            ask=ask,
            last=last,
            mid=mid,
            volume=result.get("volume"),
            open_interest=result.get("open_interest"),
            iv=result.get("iv"),
            delta=result.get("delta"),
            gamma=result.get("gamma"),
            theta=result.get("theta"),
            vega=result.get("vega"),
            source="ibkr_snapshot",
        ))

    return await _persist_rows(rows)


async def _qualify_chain_contracts(
    state,
    *,
    symbol: str,
    strikes: list[float],
    expiry: str,
) -> list[tuple[float, str, object]]:
    """Build + qualify IBKR Option contracts for each (strike, C/P) pair.

    Uses a per-session cache keyed by (symbol, strike, right, expiry) so we
    only round-trip qualifyContractsAsync once per contract per process
    lifetime. Pattern mirrors spx_target_watcher._qualified_ib_contract.
    """
    from fef_sidecar.ibkr.live import _to_ib_contract
    from fef_sidecar.ibkr.models import Contract, ContractKind

    if not hasattr(state, "_chain_capture_qualified"):
        state._chain_capture_qualified = {}  # type: ignore[attr-defined]
    cache: dict = state._chain_capture_qualified  # type: ignore[attr-defined]

    out: list[tuple[float, str, object]] = []
    to_qualify: list[tuple[float, str, object]] = []
    for strike in strikes:
        for right in ("C", "P"):
            key = (symbol, float(strike), right, expiry)
            cached = cache.get(key)
            if cached is not None:
                out.append((float(strike), right, cached))
                continue
            opt = Contract(
                symbol=symbol, kind=ContractKind.OPTION,
                exchange="CBOE" if symbol == "SPX" else "SMART",
                expiry=expiry, strike=float(strike), right=right,
            )
            ic = _to_ib_contract(opt)
            to_qualify.append((float(strike), right, ic))

    if to_qualify:
        try:
            ib = state.ibkr._ib  # type: ignore[attr-defined]
            await ib.qualifyContractsAsync(*[t[2] for t in to_qualify])
            for strike, right, ic in to_qualify:
                cache[(symbol, strike, right, expiry)] = ic
                out.append((strike, right, ic))
        except Exception:
            logger.exception(
                "Chain capture: qualifyContractsAsync failed for %d contracts",
                len(to_qualify),
            )
            # Skip un-qualified ones; partially-cached ones still flow.

    return out


async def _snapshot_one(state, ib_contract) -> dict:
    """Fetch a single snapshot. Returns dict of populated fields.

    NaN-tolerant — IBKR sometimes returns ticker.bid as NaN when there's no
    bid; the _good() filter converts NaN -> None. modelGreeks may be None
    if IBKR didn't compute them; that's also fine, rows persist as null.
    """
    ib = state.ibkr._ib  # type: ignore[attr-defined]

    # Enable delayed market data globally (paper accounts) once. Same flag
    # the option-quote path uses; this is just defense in depth.
    if not getattr(state.ibkr, "_market_data_type_set", False):
        try:
            ib.reqMarketDataType(3)
            state.ibkr._market_data_type_set = True  # type: ignore[attr-defined]
        except Exception:
            pass

    # Snapshot mode: IBKR returns one populated ticker then auto-cancels.
    # We don't pass genericTickList — saves OPRA permission issues on paper.
    ticker = ib.reqMktData(ib_contract, "", True, False)

    # Wait up to ~3s for any of bid/ask/last/greeks. The outer timeout in
    # _capture_once will bail at 5s if this hangs.
    for _ in range(30):
        await asyncio.sleep(0.1)
        has_price = (
            (ticker.bid is not None and ticker.bid > 0 and ticker.bid == ticker.bid)
            or (ticker.ask is not None and ticker.ask > 0 and ticker.ask == ticker.ask)
            or (ticker.last is not None and ticker.last > 0 and ticker.last == ticker.last)
        )
        if has_price or ticker.modelGreeks is not None:
            break

    def _good(v) -> bool:
        try:
            return v is not None and v == v and float(v) > 0
        except (TypeError, ValueError):
            return False

    def _pick(*candidates) -> float | None:
        for c in candidates:
            try:
                f = float(c) if c is not None else None
            except (TypeError, ValueError):
                continue
            if f is not None and f == f:
                # accept 0.0 (legitimate for OTM near expiry) only on last;
                # bid/ask of 0 means "no quote", treat as None upstream.
                return f
        return None

    bid = _pick(ticker.bid, getattr(ticker, "delayedBid", None))
    ask = _pick(ticker.ask, getattr(ticker, "delayedAsk", None))
    last = _pick(ticker.last, getattr(ticker, "delayedLast", None), ticker.close)
    # Drop zero quotes — they mean "no market" rather than "$0 mid".
    if bid is not None and bid <= 0:
        bid = None
    if ask is not None and ask <= 0:
        ask = None
    if last is not None and last < 0:
        last = None

    mg = ticker.modelGreeks
    delta = float(mg.delta) if mg and _good(mg.delta) else None
    gamma = float(mg.gamma) if mg and _good(mg.gamma) else None
    theta = float(mg.theta) if mg and mg.theta is not None and mg.theta == mg.theta else None
    vega = float(mg.vega) if mg and _good(mg.vega) else None
    iv = float(mg.impliedVol) if mg and _good(mg.impliedVol) else None

    volume = None
    try:
        v = ticker.volume
        if v is not None and v == v and v >= 0:
            volume = int(v)
    except Exception:
        pass

    return {
        "bid": bid, "ask": ask, "last": last,
        "iv": iv, "delta": delta, "gamma": gamma,
        "theta": theta, "vega": vega,
        "volume": volume,
        "open_interest": None,  # IBKR snapshot doesn't reliably include OI
    }


async def _find_nearest_signal_iter_id(now: datetime) -> int | None:
    """Return the id of the SignalIter closest in time to `now`. None if
    no rows in the last 5 minutes (avoid associating with stale data)."""
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import SignalIter

    five_min_ago = now - timedelta(minutes=5)
    try:
        async with get_session() as session:
            stmt = (
                select(SignalIter)
                .where(SignalIter.timestamp >= five_min_ago)  # type: ignore[attr-defined]
                .order_by(SignalIter.timestamp.desc())  # type: ignore[attr-defined]
                .limit(1)
            )
            row = (await session.exec(stmt)).first()
            return row.id if row is not None else None
    except Exception:
        # Best-effort — never block capture on this lookup
        return None


async def _persist_rows(rows: list) -> int:
    """Bulk insert. On unique-constraint conflict (same minute re-capture),
    silently skip — sqlite's IntegrityError on (timestamp_ms, strike, right,
    expiry). Returns count of rows successfully inserted."""
    if not rows:
        return 0

    from sqlalchemy.exc import IntegrityError
    from fef_sidecar.db.engine import get_session

    # Try the bulk path first. If any row conflicts the whole transaction
    # rolls back, then fall back to per-row inserts (idempotent).
    try:
        async with get_session() as session:
            session.add_all(rows)
            await session.flush()
        return len(rows)
    except IntegrityError:
        pass
    except Exception:
        logger.exception("Chain capture bulk persist failed; trying per-row")

    # Per-row fallback for partial dedup. Each row gets its own session so
    # one IntegrityError doesn't poison the rest.
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
                "Chain capture per-row persist failed for %s %s %s",
                getattr(r, "strike", "?"),
                getattr(r, "right", "?"),
                getattr(r, "expiry", "?"),
            )
    return n
