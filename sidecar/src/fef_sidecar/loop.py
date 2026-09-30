"""Background loops:

- spread_tick_loop     — every FEF_LOOP_INTERVAL_SEC (default 120s): builds the
                         credit-spread recommendation from cached 30-min IBKR
                         bars and offers it to the spread auto-trader
- bar_stream_loop      — continuously streams IBKR 2-min bars, broadcasts TickEvent
                         on each close
- tick_stream_loop     — sub-second last-price stream (live: ib_async updateEvent;
                         mock: synthetic ticks every FEF_TICK_INTERVAL_SEC). Broadcasts
                         TickEvent without a bar payload.

[REMOVED 2026-08-25] QuantData subscription CANCELLED.
  * `quantdata_poll_loop` (the ~15s MarketSnapshot fetch that cached the
    snapshot on AppState, persisted flow_snapshot / exposure-chain / OI-chain /
    aggression-detail / flow-series rows, fed the phase detector and broadcast
    SnapshotEvent) is gone — every one of those writes had exactly one source.
  * `_run_one_iter` (the FEF iteration) is gone. Its input came from
    `snapshot_to_inputs(MarketSnapshot)`, which had no other producer, so the
    0DTE DIRECTIONAL bot is retired along with the vendor. Operator-confirmed.
  * The CREDIT-SPREAD path that used to execute INSIDE `_run_one_iter`, purely
    by position, was extracted first — see `_build_spread_recommendation` /
    `_run_spread_auto_trade` / `_run_spread_iter` below. It never had a
    QuantData import; only its enclosing function did. It now runs on its own
    schedule off IBKR quotes + cached 30-min bars.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from fef_sidecar import et_time
from fef_sidecar.db import SignalIterRepository
from fef_sidecar.engine.option_targets import OptionTargets, build_option_targets
from fef_sidecar.events import IterEvent, TickEvent
from fef_sidecar.ibkr import Contract, ContractKind

if TYPE_CHECKING:
    from fef_sidecar.state import AppState

logger = logging.getLogger(__name__)


def _market_hours_et() -> tuple[int, int]:
    return 9 * 60 + 30, 16 * 60


def _in_market_hours_et() -> bool:
    now = et_time.now_et()
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    open_m, close_m = _market_hours_et()
    return open_m <= minute <= close_m


def _today_et() -> str:
    return et_time.today_et()


def _walls_json(walls: Any) -> list[dict[str, Any]]:
    return [
        {"strike": w.strike, "net_m": round(w.net_m, 2), "side": w.side}
        for w in walls
    ]


# [REMOVED 2026-08-25] QuantData subscription CANCELLED. The full-chain
# capture layer lived here and had exactly one producer — the QuantData
# snapshot. Deleted with it:
#   _expiry_rows_json / _orders_json      (MarketSnapshot -> JSON serialisers)
#   EXPOSURE_CHAIN_WINDOW_PTS, OI_CHAIN_WINDOW_PTS, OI_CHAIN_MIN_INTERVAL_SEC
#   _last_oi_capture_ms, _last_flow_series_ms   (per-poll cadence gates)
#   _build_exposure_chain_rows / _persist_exposure_chain_rows
#   _build_oi_chain_rows / _build_aggression_detail_row
#   _build_flow_series_rows / _persist_rows_idempotent
# The DB tables they wrote (exposure_chain_point, oi_chain_point,
# aggression_detail, flow_series_point, flow_snapshot) are NOT dropped —
# historical rows stay queryable; nothing writes new ones.


def _et_date_from_ms(ts_ms: int) -> str:
    """ET session date YYYY-MM-DD for an epoch-ms timestamp (DST-correct)."""
    return et_time.session_date_et(ts_ms)


async def refresh_es_vwap(ib: Any, duration: str = "1 D") -> int:
    """Compute ES session-anchored VWAP from IBKR 1-min TRADES bars and upsert
    into es_vwap_point. Shared by the live loop and the diag/backfill endpoint
    so both use identical methodology. Idempotent; returns rows inserted.

    VWAP = Σ(bar_vwap × volume)/Σ(volume) cumulative from RTH open, per ET
    session. bar_vwap uses IBKR's per-bar `average` (true bar VWAP), falling
    back to typical price (H+L+C)/3 if absent.
    """
    from fef_sidecar.ibkr.live import _to_ib_contract
    from fef_sidecar.ibkr.models import Contract
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import EsVwapPoint, HistoricalBar
    from sqlmodel import select as _s
    from sqlalchemy.exc import IntegrityError

    ib_contract = _to_ib_contract(Contract.for_symbol("ES"))
    await ib.qualifyContractsAsync(ib_contract)
    bars = await ib.reqHistoricalDataAsync(
        ib_contract, endDateTime="", durationStr=duration,
        barSizeSetting="1 min", whatToShow="TRADES", useRTH=True,
        formatDate=2, keepUpToDate=False,
    )

    cum_pv: dict[str, float] = {}
    cum_v: dict[str, float] = {}
    rows: list[Any] = []
    # Also store ES RTH OHLCV under a CLEAN key ('ES'/'1 min RTH') so the VWAP
    # shadow can detect on the right (RTH-anchored) bars. The legacy 'ES'/'1 min'
    # rows are the OVERNIGHT session and must not be used for RTH VWAP.
    hist_rows: list[Any] = []
    for b in bars:
        try:
            ts_ms = int(b.date.timestamp() * 1000) if hasattr(b.date, "timestamp") else int(b.date * 1000)
        except Exception:
            continue
        sd = _et_date_from_ms(ts_ms)
        vol = float(b.volume or 0)
        bar_vwap = float(getattr(b, "average", 0) or 0) or (float(b.high) + float(b.low) + float(b.close)) / 3
        cum_pv[sd] = cum_pv.get(sd, 0.0) + bar_vwap * vol
        cum_v[sd] = cum_v.get(sd, 0.0) + vol
        sess_vwap = (cum_pv[sd] / cum_v[sd]) if cum_v[sd] > 0 else bar_vwap
        rows.append(EsVwapPoint(
            timestamp_ms=ts_ms, session_date=sd, symbol="ES",
            close=float(b.close), bar_vwap=bar_vwap, bar_volume=vol,
            session_vwap=sess_vwap, cum_volume=cum_v[sd], source="live_loop",
        ))
        hist_rows.append(HistoricalBar(
            symbol="ES", bar_size="1 min RTH", timestamp_ms=ts_ms,
            open=float(b.open), high=float(b.high), low=float(b.low),
            close=float(b.close), volume=vol,
        ))

    if not rows:
        return 0
    added = 0
    async with get_session() as session:
        existing_ts = {
            r.timestamp_ms for r in (await session.exec(
                _s(EsVwapPoint).where(EsVwapPoint.symbol == "ES")
            )).all()
        }
        for r in rows:
            if r.timestamp_ms in existing_ts:
                continue
            session.add(r)
            added += 1
        if added:
            try:
                await session.commit()
            except IntegrityError:
                added = 0
    # ES RTH OHLCV — separate best-effort idempotent upsert (unique on
    # symbol+bar_size+timestamp_ms). Never fail the VWAP path on a bar dup.
    try:
        async with get_session() as session:
            have = {
                r.timestamp_ms for r in (await session.exec(
                    _s(HistoricalBar).where(HistoricalBar.symbol == "ES")
                    .where(HistoricalBar.bar_size == "1 min RTH")
                )).all()
            }
            new_bars = [h for h in hist_rows if h.timestamp_ms not in have]
            if new_bars:
                session.add_all(new_bars)
                await session.commit()
    except IntegrityError:
        pass
    except Exception:
        logger.exception("ES RTH OHLCV persist failed")
    return added


async def es_vwap_loop(state: Any) -> None:
    """Refresh ES session-anchored VWAP every FEF_ES_VWAP_INTERVAL_SEC (default
    60s) from IBKR 1-min TRADES bars. Independent best-effort loop — never
    affects trading. Gated OFF unless FEF_CAPTURE_ENABLED is set, like the
    other capture paths."""
    interval = float(os.environ.get("FEF_ES_VWAP_INTERVAL_SEC", "60"))
    _capture_on = os.environ.get("FEF_CAPTURE_ENABLED", "true").lower() in ("true", "1", "yes", "on")
    if not _capture_on:
        logger.info("ES VWAP loop disabled (FEF_CAPTURE_ENABLED is off)")
        return
    from fef_sidecar.ibkr.live import LiveIBKRProvider
    logger.info("Starting ES VWAP loop (interval=%.0fs)", interval)
    while True:
        try:
            if isinstance(state.ibkr, LiveIBKRProvider):
                ib = getattr(state.ibkr, "_ib", None)
                if ib is not None and ib.isConnected():
                    n = await refresh_es_vwap(ib, duration="1 D")
                    if n:
                        logger.debug("ES VWAP: +%d rows", n)
        except asyncio.CancelledError:
            logger.info("ES VWAP loop cancelled — exiting")
            raise
        except Exception:
            logger.exception("ES VWAP refresh failed")
        await asyncio.sleep(interval)


# [REMOVED 2026-08-25] QuantData subscription CANCELLED. Deleted here:
#   _orders_json          — MarketSnapshot order-flow -> JSON
#   _snapshot_plausible   — per-symbol price-bounds guard on a QD snapshot
#   _qd_scope_lock        — the set_page_filter/fetch atomicity lock
#   quantdata_poll_loop   — the ~15s poll: cached the snapshot on AppState,
#                           wrote flow_snapshot + the four chain-capture
#                           tables, fed the PHASE DETECTOR (advisory
#                           RIDE/FADE/WAIT, state.phase_last_verdict) and
#                           broadcast SnapshotEvent.
# The phase detector goes with it: its samples (spx, drift_bias, aggression
# call/put %, max pain) were ALL snapshot fields. state.phase_last_verdict now
# stays None, which auto_trader already handles as "no phase read".


# Historical-bar cache TTL — a freshly closed bar is picked up within this
# window without hammering IBKR. Shared by the spread recommender + chart.
_BARS_CACHE_TTL_MS = 25 * 60 * 1000


async def _get_cached_bars(
    state: AppState,
    symbol: str,
    bar_size: str,
    duration: str,
    *,
    max_age_ms: int = _BARS_CACHE_TTL_MS,
) -> list[Any]:
    """Fetch + cache historical bars keyed by (symbol, bar_size, duration).

    Re-fetches when the cached entry is missing/empty OR older than
    max_age_ms; otherwise serves the cache so repeated callers (the spread
    recommender's 30-min/5-D TA fetch + the chart regression overlay) don't
    each hit IBKR. Best-effort: returns [] if the fetch fails, and an empty
    result is NOT served from cache (keeps retrying — matches the original
    spread-recommender behaviour where an empty list fell through).

    IBKR serves any bar_size/duration on demand — no new data source. For
    mock, fetch_historical_bars synthesizes whatever bar size is requested.

    Read-only DISPLAY/TA data — NOT part of the auto-trade / scanner /
    watcher / exit path.
    """
    import time as _t
    from fef_sidecar.ibkr.models import Contract

    now_ms = int(_t.time() * 1000)
    key = (symbol, bar_size, duration)
    cached = state.bars_cache.get(key)
    if cached is not None:
        fetched_at, bars = cached
        if bars and (now_ms - fetched_at) < max_age_ms:
            return bars  # type: ignore[return-value]

    try:
        bars = await state.ibkr.fetch_historical_bars(  # type: ignore[attr-defined]
            Contract.for_symbol(symbol),
            bar_size=bar_size,
            duration=duration,
        )
    except Exception:
        logger.exception(
            "fetch_historical_bars failed for %s (%s / %s)", symbol, bar_size, duration
        )
        bars = []

    state.bars_cache[key] = (now_ms, bars)
    return bars  # type: ignore[return-value]


async def _get_spread_bars_30m(state: AppState) -> list[Any]:
    """Cached 30-min / 5-D bars for the active symbol — feeds the credit-
    spread recommender's TA entry detector (EMA 8/21 cross + StdDev channel
    + engulfing). Thin wrapper over the generalized _get_cached_bars; the
    25-min TTL and best-effort [] semantics are unchanged.
    """
    return await _get_cached_bars(state, state.active_symbol, "30 mins", "5 D")


# [REMOVED 2026-08-25] QuantData subscription CANCELLED — `_ntm_max_pain(snap)`
# read snapshot OI rows. The pure calculation it wrapped survives at
# engine/max_pain.py::compute_ntm_max_pain for the ThetaData research path.


# ---------------------------------------------------------------------------
# Spread tick loop - drives the credit-spread recommendation + auto-trade
# ---------------------------------------------------------------------------
#
# [REMOVED 2026-08-25] QuantData subscription CANCELLED. This was
# `fef_tick_loop`, which called `_run_one_iter` (QuantData fetch ->
# snapshot_to_inputs -> FEF verdict -> directional auto-trade). The credit
# spread path executed inside that function purely by POSITION, so a naive
# deletion of the QuantData fetch would have taken the spread bot down with
# the directional bot at the `snapshot_to_inputs(None)` line. It is now its
# own driver, with no snapshot, no verdict and no engine in the path.
# ---------------------------------------------------------------------------


async def spread_tick_loop(state: AppState) -> None:
    interval = int(os.environ.get("FEF_LOOP_INTERVAL_SEC", "120"))
    market_hours_only = os.environ.get("FEF_LOOP_MARKET_HOURS_ONLY", "false").lower() in (
        "true",
        "1",
        "yes",
        "on",
    )
    logger.info(
        "Starting spread tick loop (interval=%ds, market_hours_only=%s)",
        interval,
        market_hours_only,
    )

    while True:
        try:
            # Session-boundary bookkeeping. The engine buffer reset that used
            # to happen here went with the FEF iteration; the spread path's
            # own once-per-session guarantee lives in spread_auto_trader
            # (spread_auto_trade_last_date + a DB check), so all this needs to
            # do is keep state.session_date current for the audit rows.
            current_date = _today_et()
            if state.session_date != current_date:
                if state.session_date != "":
                    logger.info(
                        "New session detected (%s -> %s)",
                        state.session_date,
                        current_date,
                    )
                state.session_date = current_date

            if market_hours_only and not _in_market_hours_et():
                logger.debug("Outside market hours - sleeping 60s")
                await asyncio.sleep(60)
                continue

            await _run_spread_iter(state)
        except asyncio.CancelledError:
            logger.info("Spread tick loop cancelled - exiting")
            raise
        except Exception:
            logger.exception("Spread tick loop iteration failed")

        await asyncio.sleep(interval)


def _current_provider_mode(state: AppState) -> str:
    """'live' / 'mock' — tag for the SignalIter.provider_mode column.
    Captures whether IBKR is real or mocked. Independent of the IBKR account
    class (paper vs live) — see _current_account_class below.

    [REMOVED 2026-08-25] QuantData subscription CANCELLED. This used to be a
    two-provider check (QD x IBKR) that could return 'mixed'. With one provider
    left there is no mixed state; 'mixed' is never emitted again. Historical
    rows still carry it."""
    ibkr_mock = type(state.ibkr).__name__.startswith("Mock")
    return "mock" if ibkr_mock else "live"


# [REMOVED 2026-08-25] QuantData subscription CANCELLED - `_ema`,
# `_alt_direction_signal`, `_alt_direction_30m_signal` and
# `_alt_direction_4h_signal` deleted. They were observational SHADOW signals
# whose only consumer was SignalIter.extras["alt_direction_30m" / "_4h"],
# written by the (now deleted) FEF iteration. They existed to be compared
# against drift_bias, which is also gone.


def _current_account_class(state: AppState) -> str:
    """'paper' / 'live' / 'mock' / 'unknown' — tag for which IBKR account
    class is connected. Stamped into SignalIter.extras and LiveOrder.notes
    so research scripts can keep paper and live data strictly separated
    once both flow through the same DB.

    Reads from the IBKR provider's account_class property which is
    populated on connect from managedAccounts(). Both LiveIBKRProvider
    and MockIBKRProvider expose this property uniformly."""
    return getattr(state.ibkr, "account_class", "unknown") or "unknown"


# ---------------------------------------------------------------------------
# Credit-spread iteration - EXTRACTED from _run_one_iter (2026-08-25)
# ---------------------------------------------------------------------------
#
# [REMOVED 2026-08-25] QuantData subscription CANCELLED. `_run_one_iter` was
# the FEF iteration: fetch MarketSnapshot -> snapshot_to_inputs -> engine.push
# -> persist SignalIter -> broadcast IterEvent -> directional auto_trade ->
# pattern eval -> tournament -> regime detector. Every one of those steps was
# fed, directly or transitively, by the vendor snapshot, and snapshot_to_inputs
# had no other producer. It is deleted whole.
#
# The two CREDIT-SPREAD blocks it contained had ZERO QuantData imports - they
# ran on IBKR quotes and cached 30-min bars and were nested in the FEF iter
# only by position. They are extracted verbatim below, with two changes:
#   1. the recommendation is returned as a LOCAL dict instead of being written
#      into event.verdict["spread_recommendation"] (the FEF verdict object no
#      longer exists);
#   2. the underlying spot comes from `_spread_spot` (the IBKR tick) instead of
#      `inputs.spx_price` (the QuantData snapshot price). That path already
#      preferred the live tick - `_run_one_iter` overlaid it onto the snapshot
#      before the recommender ran - so this is the same number by a shorter
#      route, not a new data source.
# ---------------------------------------------------------------------------


def _spread_spot(state: AppState, bars_30m: list[Any]) -> float:
    """Underlying spot for the credit-spread recommender.

    Order of preference:
      1. the live IBKR tick, if it is <60s old AND was stamped by the symbol
         we are recommending for (the 2026-07-23 MAJOR-2 cross-symbol guard -
         after a /config/symbol flip the cached tick belongs to the PREVIOUS
         symbol);
      2. the close of the most recent 30-min bar we already fetched;
      3. state.last_spx_price.

    Returns 0.0 when nothing is available; the caller treats that as "no
    recommendation this iter" rather than passing a bogus spot to strike
    selection.
    """
    import time as _t

    if (
        state.last_live_tick_spx > 0
        and state.last_live_tick_ms > 0
        and (int(_t.time() * 1000) - state.last_live_tick_ms) < 60_000
        # FAILS CLOSED on an unstamped tick. `x or active == active` would be
        # True when the stamp is None, trusting a tick whose symbol we cannot
        # confirm - which is exactly the cross-symbol contamination the
        # 2026-07-23 MAJOR-2 guard exists to stop. Falling through to the bar
        # close costs at most 30 minutes of staleness; guessing costs a strike.
        and state.last_live_tick_symbol == state.active_symbol
    ):
        return float(state.last_live_tick_spx)

    for bar in reversed(bars_30m or []):
        close = getattr(bar, "close", None)
        if close is None and isinstance(bar, dict):
            close = bar.get("close")
        if close:
            return float(close)

    return float(state.last_spx_price or 0.0)


async def _build_spread_recommendation(state: AppState) -> dict[str, Any] | None:
    """Credit-spread recommendation - TA-driven entries (EMA 8/21 cross +
    pullback engulfing on StdDev channel) with delta-targeted strikes (short
    strike NEAREST spread_delta_max, net credit above the credit/width floor).

    Reads cached 30-min bars; refreshes them at most every 25 minutes per
    symbol. Sets state.last_spread_recommendation and returns the dict, or
    None if the block faulted.
    """
    import time as _time

    try:
        from fef_sidecar.engine.spread_recommender import recommend_from_bars
        from fef_sidecar.engine.spread_entry import SpreadEntryTrigger  # noqa: F401

        bars_30m = await _get_spread_bars_30m(state)
        if not bars_30m:
            return {
                "kind": "NONE", "reasoning": "30-min bar history not yet available",
                "short_strike": None, "long_strike": None, "spread_width": None,
                "estimated_credit_per_contract": None,
                "max_profit_per_contract": None,
                "max_loss_per_contract": None,
                "breakeven": None,
            }

        spot = _spread_spot(state, bars_30m)
        if spot <= 0:
            # No trustworthy underlying price. Do NOT hand strike selection a
            # zero/stale spot - that is how a wrong-symbol print becomes a
            # real short strike. Report it instead of guessing.
            return {
                "kind": "NONE",
                "reasoning": "no fresh underlying price for the active symbol",
                "short_strike": None, "long_strike": None, "spread_width": None,
                "estimated_credit_per_contract": None,
                "max_profit_per_contract": None,
                "max_loss_per_contract": None,
                "breakeven": None,
            }

        today_ymd = _today_et().replace("-", "")
        spread_rec = await recommend_from_bars(
            bars_30m,
            ibkr=state.ibkr,
            symbol=state.active_symbol,
            expiry=today_ymd,
            spot=spot,
            # Structure comes from AppState so every axis is tunable without a
            # code change. Defaults are the measured-best values -- see state.py
            # for what each one is worth and which study measured it.
            width=state.spread_width,
            delta_max=state.spread_delta_max,
            premium_min=state.spread_premium_min,
            puts_only=state.spread_puts_only,
            require_gate=state.spread_require_gate,
            # Strike-selection guards added after the 2026-08-12
            # 0.0329-delta pick. See state.py for what each refuses.
            delta_floor_frac=state.spread_delta_floor_frac,
            min_quote_coverage=state.spread_min_quote_coverage,
            min_credit_width_ratio=state.spread_min_credit_width_ratio,
            min_net_credit=state.spread_min_net_credit,
        )
        rec = {
            "kind": spread_rec.kind,
            "reasoning": spread_rec.reasoning,
            "short_strike": spread_rec.short_strike,
            "long_strike": spread_rec.long_strike,
            "spread_width": spread_rec.spread_width,
            "estimated_credit_per_contract": spread_rec.estimated_credit_per_contract,
            "max_profit_per_contract": spread_rec.max_profit_per_contract,
            "max_loss_per_contract": spread_rec.max_loss_per_contract,
            "breakeven": spread_rec.breakeven,
            "trigger_bar_close_ms": spread_rec.trigger_bar_close_ms,
            "trigger_signature": spread_rec.trigger_signature,
            "short_delta": spread_rec.short_delta,
            "fail_reasons": spread_rec.fail_reasons,
            "fresh_trigger": bool(
                spread_rec.trigger_signature
                and spread_rec.trigger_signature != state.last_spread_trigger_signature
            ),
            # Provenance, so a consumer can tell a live recommendation from a
            # stale one. `last_spread_recommendation` is NOT updated when the
            # bar fetch returns empty (documented best-effort []) and is never
            # session-reset, so without these a Monday PCS could be sold on
            # Tuesday after a gap - at max loss on entry. Review finding B2.
            "session_date": today_ymd,
            "as_of_ms": int(_time.time() * 1000),
            "symbol": state.active_symbol,
        }
        if spread_rec.trigger_signature:
            state.last_spread_trigger_signature = spread_rec.trigger_signature
        state.last_spread_recommendation = rec
        return rec
    except Exception:
        logger.exception("Spread recommendation failed")
        return None


async def _run_spread_auto_trade(state: AppState) -> None:
    """Spread auto-trade - the wire from recommendation to order. Entirely
    independent of the (now retired) directional auto-trader: different
    structure, different exit path (spread_exit_watcher), its own enable flag,
    its own once-per-session dedupe. Defensive - an auto-trade fault must never
    break the iteration pipeline.
    """
    import time as _time

    try:
        from fef_sidecar.engine.spread_auto_trader import maybe_auto_trade_spread
        spread_decision = await maybe_auto_trade_spread(
            state, state.last_spread_recommendation, source="auto_main",
        )
        # Record every decision so GET /spread_auto_trade/status can answer
        # "why didn't it fire?" without hand-querying signal_log.db (2026-08-17).
        # Diagnostics only - no gate reads these back.
        state.spread_auto_trade_last_reason = spread_decision.reason
        state.spread_auto_trade_last_reason_code = spread_decision.reason_code
        state.spread_auto_trade_last_eval_at_ms = int(_time.time() * 1000)
        if spread_decision.fired:
            state.spread_auto_trade_count_session += 1
            logger.info("spread_auto_trade fired: %s", spread_decision.reason)
        elif (
            state.spread_auto_trade_enabled
            and spread_decision.reason_code not in ("disabled", "rec_none", "no_rec")
        ):
            # Enabled and there WAS something to trade, but a gate blocked it.
            try:
                await SignalIterRepository.write_audit_log(
                    category="spread_auto_trade_block", actor="system",
                    summary=f"BLOCKED spread: {spread_decision.reason}",
                    details={
                        "source": "auto_main",
                        "reason": spread_decision.reason,
                        "reason_code": spread_decision.reason_code,
                        "recommendation": state.last_spread_recommendation,
                    },
                    block_reason_code=spread_decision.reason_code,
                )
            except Exception:
                pass
    except Exception:
        logger.exception("Spread auto-trade evaluation failed")


async def _run_spread_iter(state: AppState) -> None:
    """One credit-spread iteration: recommend, then offer to the auto-trader.

    Runs with NO market snapshot, NO FEF verdict and NO engine in the path.
    Both halves keep their own try/except so neither can break the driver.
    """
    import time as _time

    t_start = _time.perf_counter()
    rec = await _build_spread_recommendation(state)
    await _run_spread_auto_trade(state)

    # Keep the recommendation on the existing WS wire. The frontend's
    # OrderTicketPanel reads `IterEvent.verdict.spread_recommendation`; the FEF
    # verdict that used to carry it is gone, so we publish a WAIT-shaped
    # verdict whose only real payload is the spread block. Everything else on
    # it is a constant - there is no direction engine behind it any more.
    if rec is not None:
        try:
            state.last_iter_num += 1
            verdict: dict[str, Any] = {
                "direction": "WAIT",
                "confidence": "NONE",
                "reasoning": "directional engine retired 2026-08-25 (QuantData cancelled)",
                "strike_rec": None,
                "t1": None, "t2": None, "t3": None, "stop": None,
                "spread_recommendation": rec,
            }
            state.last_verdict = verdict
            await state.broadcaster.publish(
                IterEvent(
                    iter_num=state.last_iter_num,
                    session_date=_today_et(),
                    spx_price=_spread_spot(state, []),
                    verdict=verdict,
                    snapshot={},
                )
            )
        except Exception:
            logger.exception("Spread iter broadcast failed")

    state.last_iter_perf_ms = {
        "total_ms": round((_time.perf_counter() - t_start) * 1000, 2)
    }


# [REMOVED 2026-08-25] QuantData subscription CANCELLED - `_evaluate_patterns`
# deleted. It scored user-defined patterns against FEFInputs + FEFVerdict, both
# of which only existed inside the FEF iteration. Live pattern trading is
# unaffected: engine/pattern_trigger.py runs off confirmed IBKR bars and has
# its own loop.


async def _fetch_option_quote_with_retry(
    fetch_fn,
    *,
    max_retries: int = 3,
    backoff_ms: int = 200,
) -> tuple[Any | None, list[tuple[str, str]]]:
    """Retry an option-chain quote fetch on transient failures.

    EOD strikes (last ~10min of session) sometimes return empty IBKR option
    chain data: either get_option_quote raises ValidationError (last=None
    fails pydantic), or returns a quote with all-NaN/0 pricing. Both are
    typically resolved by retrying after a short backoff while the chain
    populates.

    Returns (quote, attempts_log) where attempts_log is a list of
    (error_type, error_msg) tuples for each failed attempt. attempts_log is
    empty on first-try success. Does NOT change strike selection — only
    whether the chain fetch itself is retried.
    """
    attempts: list[tuple[str, str]] = []
    for attempt in range(max_retries):
        try:
            quote = await fetch_fn()
            if quote is not None:
                return quote, attempts
            attempts.append(("None", "fetch returned None"))
        except Exception as e:
            attempts.append((type(e).__name__, str(e)))
        # Linear backoff: 200ms, 400ms, 600ms
        if attempt < max_retries - 1:
            await asyncio.sleep(backoff_ms / 1000 * (attempt + 1))
    return None, attempts


# [REMOVED 2026-08-25] QuantData subscription CANCELLED - the verdict-gated
# wrapper `_maybe_fetch_option_targets(state, verdict, inputs)` is deleted with
# the FEF verdict. `fetch_option_targets` below is unchanged and still live:
# engine/pattern_trigger.py calls it directly.


async def fetch_option_targets(
    state: AppState,
    *,
    direction_value: str,
    strike_rec: float,
    t1: float,
    t2: float,
    t3: float,
    stop: float,
    spx_price: float,
) -> OptionTargets | None:
    """Fetch the 0DTE option quote for a strike and build option-price targets.

    Shared by the FEF iter pipeline (_maybe_fetch_option_targets) and the
    pattern-trigger loop so both produce identical option_pricing payloads."""
    expiry = _today_et().replace("-", "")  # YYYYMMDD for IBKR
    right = "C" if direction_value == "LONG_CALLS" else "P"

    contract = Contract(
        symbol="SPX",
        kind=ContractKind.OPTION,
        exchange="CBOE",  # SPX options are CBOE-listed
        expiry=expiry,
        strike=strike_rec,
        right=right,
    )

    quote, attempts = await _fetch_option_quote_with_retry(
        lambda: state.ibkr.get_option_quote(contract),
        max_retries=3,
        backoff_ms=200,
    )
    if quote is None:
        # All retries exhausted — log the structured failure and skip.
        last_err_type, last_err_msg = (attempts[-1] if attempts else ("Unknown", "no attempts recorded"))
        if attempts:
            logger.warning(
                "Could not fetch option quote for %s after %d retries: last error %s: %s",
                contract, len(attempts), last_err_type, last_err_msg,
            )
        try:
            from fef_sidecar.engine.auto_trader import BlockReason as _BR_oqf
            await SignalIterRepository.write_audit_log(
                category="option_quote_fail", actor="system",
                summary=(
                    f"get_option_quote failed after {len(attempts)} retries: "
                    f"SPX {expiry} {strike_rec}{right}: {last_err_type}: {last_err_msg}"
                ),
                details={"strike": strike_rec, "right": right, "expiry": expiry,
                         "error_type": last_err_type, "error": last_err_msg,
                         "retry_attempts": len(attempts),
                         "all_errors": [{"type": t, "msg": m} for t, m in attempts]},
                block_reason_code=_BR_oqf.OPTION_QUOTE_FAIL,
            )
        except Exception:
            pass
        return None

    targets = build_option_targets(
        quote=quote, current_spx=spx_price,
        spx_t1=t1, spx_t2=t2, spx_t3=t3, spx_stop=stop,
    )
    if targets is None:
        try:
            from fef_sidecar.engine.auto_trader import BlockReason as _BR_oqf2
            await SignalIterRepository.write_audit_log(
                category="option_quote_fail", actor="system",
                summary=(
                    f"option quote unusable for SPX {expiry} {strike_rec}{right}: "
                    f"last={quote.last} bid={quote.bid} ask={quote.ask} delta={quote.delta} "
                    f"(NaN/0 — IBKR didn't return valid pricing for this contract)"
                ),
                details={"strike": strike_rec, "right": right, "expiry": expiry,
                         "last": quote.last, "bid": quote.bid, "ask": quote.ask,
                         "delta": quote.delta,
                         "retry_attempts": len(attempts)},
                block_reason_code=_BR_oqf2.OPTION_QUOTE_FAIL,
            )
        except Exception:
            pass
    return targets


def _option_pricing_dict(targets: OptionTargets | None) -> dict[str, Any] | None:
    if targets is None:
        return None
    return {
        "contract_strike": targets.contract_strike,
        "right": targets.right,
        "entry_limit": targets.entry_limit,
        "delta": targets.delta,
        "underlying_price_at_quote": targets.underlying_price_at_quote,
        "t1": targets.t1,
        "t2": targets.t2,
        "t3": targets.t3,
        "stop": targets.stop,
        # Full greeks + bid/ask now captured at fire time so backtester can
        # model theta drag + real slippage. Forward-only (expired contracts
        # can't be backfilled).
        "gamma": targets.gamma,
        "vega": targets.vega,
        "theta": targets.theta,
        "iv": targets.iv,
        "bid": targets.bid,
        "ask": targets.ask,
        # Quote-snapshot age drives the marketable-limit entry's freshness
        # check (auto_trader gate 9.5 / entry_pricing.resolve_entry_ask).
        "quote_ts_ms": targets.quote_ts_ms,
    }


async def bar_stream_loop(state: AppState) -> None:
    """Stream + persist completed bars for the ACTIVE symbol.

    2026-07-07 incident hardening: the session wrote ZERO SPX bars after the
    ES->SPX chart switch and the bar writer died silently at ~13:31Z. Two
    holes fixed here:
      (a) the subscription was keyed to the SPAWN-time symbol — if the
          /config/symbol cancel+respawn misses (or a future caller forgets),
          the loop streamed the old symbol forever. It now re-reads
          state.active_symbol per (re)subscribe AND per bar, and resubscribes
          itself on a switch — respawn or no respawn.
      (b) `async for` on a wedged keepUpToDate subscription blocks forever
          with no exception (an IBKR reconnect silently drops the update
          feed). The wait is now bounded (poll timeout) so the loop stamps
          its watchdog heartbeat every wakeup, and a no-bar gap beyond
          FEF_BAR_STALL_THRESHOLD_SEC during RTH forces a resubscribe.
    """
    import time as _t
    from fef_sidecar.engine.loop_watchdog import heartbeat as _wd_beat
    bar_size = os.environ.get("FEF_BAR_SIZE", "1 min")
    stall_threshold = float(os.environ.get("FEF_BAR_STALL_THRESHOLD_SEC", "180"))
    poll_timeout = float(os.environ.get("FEF_BAR_POLL_TIMEOUT_SEC", "15"))
    logger.info(
        "Starting bar stream loop for %s (%s, stall watchdog=%.0fs)",
        state.active_symbol, bar_size, stall_threshold,
    )
    while True:
        symbol = state.active_symbol
        resub_delay = 0.0
        try:
            await state.ibkr.connect()
            # CRITICAL: pass bar_size through. Historically this call used the
            # provider's default ("2 mins") while the persist below TAGGED rows
            # with FEF_BAR_SIZE ("1 min") — so live capture wrote 2-minute bars
            # labeled '1 min' into historical_bar. It went unnoticed while
            # backfills (/diag/backfill_spx_bars, research pulls) kept filling
            # true 1-min history; from 2026-07-01 the live stream was the only
            # writer and the 2-min cadence surfaced. Same subscription cost.
            agen = state.ibkr.stream_bars(
                Contract.for_symbol(symbol), bar_size=bar_size,
            )
            last_bar_mono = _t.monotonic()
            # The next-bar wait is a PERSISTENT task polled with asyncio.wait
            # — wait_for would CANCEL __anext__ on every poll timeout, which
            # closes the generator (its finally unsubscribes) between 1-min
            # bars. asyncio.wait leaves the pending task running.
            pending: asyncio.Task | None = None
            try:
                while True:
                    _wd_beat(state, "bar-stream")
                    if state.active_symbol != symbol:
                        logger.info(
                            "Bar stream: active symbol %s -> %s — resubscribing",
                            symbol, state.active_symbol,
                        )
                        break
                    if pending is None:
                        pending = asyncio.ensure_future(agen.__anext__())
                    done, _p = await asyncio.wait({pending}, timeout=poll_timeout)
                    if not done:
                        # No completed bar this wakeup — normal off-cadence /
                        # closed market. Only a gap beyond the stall threshold
                        # DURING RTH means the subscription is wedged.
                        if (_t.monotonic() - last_bar_mono > stall_threshold
                                and _in_market_hours_et()):
                            logger.warning(
                                "Bar stream stalled >%.0fs on %s during RTH — resubscribing",
                                stall_threshold, symbol,
                            )
                            resub_delay = 1.0
                            break
                        continue
                    try:
                        bar = pending.result()
                    except StopAsyncIteration:
                        pending = None
                        logger.info("Bar stream generator ended — resubscribing")
                        resub_delay = 5.0
                        break
                    pending = None
                    last_bar_mono = _t.monotonic()
                    state.last_spx_price = bar.close
                    # Provenance stamp (2026-07-23 review MAJOR-2) — bar
                    # closes write last_spx_price too; stamp the source
                    # symbol so active-tick consumers can verify it.
                    state.last_live_tick_symbol = symbol
                    # Persist bar for historical chart + backtest replay
                    try:
                        await SignalIterRepository.write_bar(
                            symbol=symbol, bar_size=bar_size, timestamp_ms=bar.epoch_ms,
                            open_=bar.open, high=bar.high, low=bar.low, close=bar.close,
                            volume=bar.volume,
                        )
                    except Exception:
                        logger.exception("Failed to persist bar")
                    event = TickEvent(
                        timestamp_ms=bar.epoch_ms,
                        spx_price=bar.close,
                        bar={
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                        },
                        # 2026-07-23 incident B: an UNLABELED bar TickEvent
                        # for the active (ES) chart was consumed by the SPX
                        # stop tick-watcher as an SPX tick (its old default
                        # treated missing symbol as "SPX"). Always label.
                        symbol=symbol,
                        prior_close=state.prior_close_by_symbol.get(symbol),
                    )
                    await state.broadcaster.publish(event)
            finally:
                own_cancelled = False
                if pending is not None and not pending.done():
                    # Await the cancelled waiter BEFORE closing the generator
                    # — aclose() on a generator with a running __anext__
                    # raises RuntimeError and can leak the IBKR subscription
                    # slot (review MINOR-9).
                    pending.cancel()
                    try:
                        await pending
                    except asyncio.CancelledError:
                        # Distinguish the child's cancellation from our OWN
                        # (re-review N5): swallowing our own here would make
                        # the loop survive a shutdown/symbol-switch cancel and
                        # briefly duplicate against its supervised replacement.
                        cur = asyncio.current_task()
                        own_cancelled = bool(
                            cur is not None
                            and getattr(cur, "cancelling", lambda: 0)()
                        )
                    except Exception:
                        pass
                try:
                    await agen.aclose()
                except Exception:
                    pass
                if own_cancelled:
                    raise asyncio.CancelledError()
            if resub_delay:
                await asyncio.sleep(resub_delay)
        except asyncio.CancelledError:
            logger.info("Bar stream cancelled — exiting")
            raise
        except Exception:
            logger.exception("Bar stream failed — reconnecting in 5s")
            await asyncio.sleep(5)


async def _bar_capture_health_report(state: AppState, *, tag: str) -> str:
    """Compute + audit one capture-health line: historical_bar rows written
    per (symbol, bar_size) over the trailing 60 minutes. A dead/wedged bar
    writer (2026-07-07: zero SPX rows all session, writer died 13:31Z) shows
    up as active-symbol=0 within the hour instead of at the EOD audit.
    Returns the summary for tests/callers."""
    from sqlalchemy import func
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import HistoricalBar
    import time as _t

    cutoff_ms = int(_t.time() * 1000) - 3600_000
    async with get_session() as session:
        rows = list((await session.exec(
            select(HistoricalBar.symbol, HistoricalBar.bar_size, func.count())
            .where(HistoricalBar.timestamp_ms >= cutoff_ms)
            .group_by(HistoricalBar.symbol, HistoricalBar.bar_size)
        )).all())
    counts = {f"{sym} ({bs})": int(n) for sym, bs, n in rows}
    active = state.active_symbol
    active_rows = sum(
        n for (sym, bs, n) in rows
        if sym == active and bs == os.environ.get("FEF_BAR_SIZE", "1 min")
    )
    body = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    warn = (
        "WARNING: 0 rows for the active symbol in the last hour — bar capture is dead. "
        if active_rows == 0 else ""
    )
    summary = (
        f"BAR CAPTURE health [{tag}]: {warn}last 60min: {body} (active={active})"
    )
    await SignalIterRepository.write_audit_log(
        category="capture_health", actor="system", summary=summary,
        details={"tag": tag, "counts": counts, "active_symbol": active,
                 "active_rows_last_hour": active_rows},
    )
    return summary


async def bar_capture_health_loop(state: AppState) -> None:
    """Capture-health reporter: one audit line at the open (~09:31 ET) and at
    each top-of-hour during RTH (see _bar_capture_health_report). Checks
    every 60s; dedupes by (session, slot)."""
    from fef_sidecar.engine.loop_watchdog import heartbeat as _wd_beat
    fired: set[str] = set()
    logger.info("Starting bar-capture health loop (open + hourly during RTH)")
    while True:
        try:
            _wd_beat(state, "bar-capture-health")
            if _in_market_hours_et():
                now = et_time.now_et()  # DST-correct (review MINOR-10)
                slot = None
                if now.hour == 9 and now.minute in (31, 32):
                    slot = "open"
                elif now.minute in (0, 1) and now.hour > 9:
                    slot = f"h{now.hour:02d}"
                if slot is not None:
                    key = f"{now.strftime('%Y-%m-%d')}:{slot}"
                    if key not in fired:
                        fired.add(key)
                        await _bar_capture_health_report(state, tag=slot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("bar-capture health iteration failed")
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Tick stream — sub-second last price for the top-bar SPX banner
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# VIX poll loop — periodic VIX index quote for the top-bar IV regime indicator
# ---------------------------------------------------------------------------


async def vix_poll_loop(state: AppState) -> None:
    """IMPROVEMENTS #M — refresh VIX every FEF_VIX_POLL_INTERVAL_SEC (default 30s).

    Source: IBKR ^VIX index quote. When it has no value, last_vix stays at its
    prior reading.

    [REMOVED 2026-08-25] QuantData subscription CANCELLED. The fallback source
    (last_snapshot.iv_rank.vix_proxy — SPX ~30-day ATM IV, same scale as VIX
    within ~1-2 points) is gone, so an unavailable IBKR VIX subscription now
    means a STALE last_vix instead of a proxied one. This is visible, not
    silent: /status and the top bar keep showing the last good reading, and
    write_vix stops persisting new samples.
    """
    from fef_sidecar.events import StatusEvent
    from fef_sidecar.engine.loop_watchdog import heartbeat as _wd_beat
    import time as _time

    interval = float(os.environ.get("FEF_VIX_POLL_INTERVAL_SEC", "30"))
    logger.info("Starting VIX poll loop (interval=%.1fs)", interval)
    while True:
        _wd_beat(state, "vix-poll")
        try:
            vix_value: float | None = None
            vix_ts_ms: int | None = None
            source: str | None = None

            quote = await state.ibkr.get_last_quote(Contract.vix_index())
            if quote and quote.last and quote.last > 0:
                vix_value = float(quote.last)
                vix_ts_ms = int(quote.timestamp.timestamp() * 1000)
                source = "ibkr"

            if vix_value is not None:
                state.last_vix = vix_value
                try:
                    await SignalIterRepository.write_vix(
                        timestamp_ms=vix_ts_ms or int(_time.time() * 1000),
                        value=vix_value,
                    )
                except Exception:
                    logger.exception("Failed to persist VIX sample")
                # Broadcast updated status with VIX value
                await state.broadcaster.publish(
                    StatusEvent(
                        ibkr=state.ibkr_status if state.ibkr_status != "unknown" else "connected",
                        engine="running" if state.loop_task is not None else "stopped",
                        history_size=state.engine.history_size,
                        vix=state.last_vix,
                    )
                )
                logger.debug("VIX from %s: %.2f", source, vix_value)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("VIX poll iteration failed")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Account poll loop — live broker scorecard (realized + unrealized P/L)
# ---------------------------------------------------------------------------


async def account_poll_loop(state: AppState) -> None:
    """Poll IBKR account summary every FEF_ACCOUNT_POLL_INTERVAL_SEC (default 15s).

    IBKR is the source of truth for P/L — no need to reconcile from our
    TradeTicket records. Broadcasts AccountEvent on every poll.
    """
    from fef_sidecar.events import AccountEvent

    interval = float(os.environ.get("FEF_ACCOUNT_POLL_INTERVAL_SEC", "15"))
    logger.info("Starting account poll loop (interval=%.1fs)", interval)
    while True:
        try:
            summary = await state.ibkr.get_account_summary()
            if summary is None:
                await asyncio.sleep(interval)
                continue
            state.last_account = summary
            await state.broadcaster.publish(
                AccountEvent(
                    timestamp_ms=summary.timestamp_ms,
                    account=summary.account,
                    net_liquidation=summary.net_liquidation,
                    buying_power=summary.buying_power,
                    available_funds=summary.available_funds,
                    daily_pnl=summary.daily_pnl,
                    realized_pnl=summary.realized_pnl,
                    unrealized_pnl=summary.unrealized_pnl,
                    gross_position_value=summary.gross_position_value,
                    open_positions_count=summary.open_positions_count,
                    open_options_count=summary.open_options_count,
                    currency=summary.currency,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Account poll iteration failed")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Microstructure loop — sub-second OFI / AVI / Microprice signal
# ---------------------------------------------------------------------------


async def hft_loop(state: AppState) -> None:
    """Combined HFT signal loop — realized vol + quote stuffing + L2 depth.

    Runs at HFT_LOOP_INTERVAL_SEC (default 0.25s = 4Hz). Event-driven on top
    of that: if vol_regime, stuffing_severity, or book_direction flips
    between polls, fires immediately instead of waiting.
    """
    import asyncio as _asyncio
    import random as _random
    import time as _time

    from fef_sidecar.engine.depth import BookLevel, DepthState, assess as depth_assess, update_book
    from fef_sidecar.engine.quote_stuffing import StuffingState, assess as stuffing_assess, push_quote as stuffing_push_quote, push_trade as stuffing_push_trade
    from fef_sidecar.engine.realized_vol import RealizedVolState, assess as vol_assess, push_tick as vol_push
    from fef_sidecar.events import HFTEvent
    from fef_sidecar.ibkr.live import LiveIBKRProvider

    interval = float(os.environ.get("HFT_LOOP_INTERVAL_SEC", "0.25"))
    persist_interval = float(os.environ.get("FEF_HFT_PERSIST_SEC", "1.0"))
    last_persist_ms = [0]  # mutable closure ref so inner block can update
    rng = _random.Random()

    state.vol_state = RealizedVolState()
    state.stuffing_state = StuffingState()
    state.depth_state = DepthState()

    cancel_depth = None
    if isinstance(state.ibkr, LiveIBKRProvider):
        # Live: subscribe to L2 depth on the active symbol (or proxy)
        from fef_sidecar.ibkr.models import Contract
        depth_symbol = {"SPX": "ES", "SPY": "ES", "QQQ": "NQ", "IWM": "RTY"}.get(
            state.active_symbol, state.active_symbol)
        try:
            await state.ibkr.connect()

            def _on_book(ts, b, a):
                update_book(state.depth_state, b, a, ts)
                state.depth_update_count_live += 1

            cancel_depth = await state.ibkr.subscribe_depth(
                Contract.for_symbol(depth_symbol), _on_book,
            )
            logger.info("HFT loop subscribed to LIVE depth on %s", depth_symbol)

            # Watchdog: if no depth events arrive within 30s, your account
            # likely lacks the CME depth data package. Log a clear warning
            # so you know the panel showing 0/BALANCED is data-permission,
            # not a code bug.
            async def _depth_subscription_watchdog():
                await asyncio.sleep(30)
                if state.depth_update_count_live == 0:
                    logger.warning(
                        "L2 depth subscription on %s delivered 0 events in 30s. "
                        "Likely missing CME depth data subscription on this IBKR "
                        "account. HFT panel L2 Depth will show 0/BALANCED until "
                        "the subscription is added.", depth_symbol,
                    )
                else:
                    logger.info(
                        "L2 depth subscription on %s healthy: %d events in 30s",
                        depth_symbol, state.depth_update_count_live,
                    )
            asyncio.create_task(_depth_subscription_watchdog(), name="depth-watchdog")
        except Exception:
            logger.exception("Live depth subscription failed — using synth")
            cancel_depth = None
    else:
        logger.info("HFT loop in MOCK mode (synth vol/stuffing/depth, interval=%.2fs)", interval)

    # Track last direction strings for event-driven fire detection
    last_vol_regime = "NORMAL"
    last_stuffing_sev = "NORMAL"
    last_book_dir = "BALANCED"

    try:
        while True:
            try:
                now_ms = int(_time.time() * 1000)
                spx = state.last_spx_price or 7400.0

                # Mock: synthesize new tick + quote + book
                if cancel_depth is None:
                    # Tick: random walk
                    tick_price = round(spx + rng.uniform(-0.5, 0.5), 2)
                    vol_push(state.vol_state, now_ms, tick_price)
                    # Quote: 70% of polls add a quote update, 20% add a trade
                    stuffing_push_quote(state.stuffing_state, now_ms)
                    if rng.random() < 0.2:
                        stuffing_push_trade(state.stuffing_state, now_ms)
                    # Synth book — 10 levels each side, sizes biased by recent direction
                    bids = [BookLevel(price=round(spx - i * 0.25, 2),
                                      size=float(rng.randint(50, 500)))
                            for i in range(1, 11)]
                    asks = [BookLevel(price=round(spx + i * 0.25, 2),
                                      size=float(rng.randint(50, 500)))
                            for i in range(1, 11)]
                    update_book(state.depth_state, bids, asks, now_ms)
                else:
                    # Live: push current spx for vol calc. Stuffing state is
                    # already being fed from microstructure_loop's tick
                    # subscription (real quotes + real trades) — DON'T push
                    # synthetic quote ticks here or we'd double-count.
                    vol_push(state.vol_state, now_ms, spx)

                vol = vol_assess(state.vol_state, now_ms)
                stuff = stuffing_assess(state.stuffing_state, now_ms)
                depth = depth_assess(state.depth_state, now_ms)

                # Event-driven: fire on direction change OR poll interval
                flips = []
                if vol.regime != last_vol_regime:
                    flips.append(f"vol_regime:{last_vol_regime}→{vol.regime}")
                    last_vol_regime = vol.regime
                if stuff.severity != last_stuffing_sev:
                    flips.append(f"stuffing:{last_stuffing_sev}→{stuff.severity}")
                    last_stuffing_sev = stuff.severity
                if depth.direction != last_book_dir:
                    flips.append(f"book:{last_book_dir}→{depth.direction}")
                    last_book_dir = depth.direction

                # Composite direction: book trumps vol unless stuffing is high
                if stuff.severity == "STUFFING":
                    composite = "FADE"  # stuffing often precedes reversion
                elif depth.direction == "BID_HEAVY" and vol.regime != "EXPANSION":
                    composite = "BULLISH"
                elif depth.direction == "ASK_HEAVY" and vol.regime != "EXPANSION":
                    composite = "BEARISH"
                elif vol.regime == "EXPANSION":
                    composite = "VOLATILE"
                else:
                    composite = "NEUTRAL"

                fired_on = ",".join(flips) if flips else "poll"

                # Data source determination for the HFT panel badges:
                is_live = isinstance(state.ibkr, LiveIBKRProvider)
                vol_source = "live_sampled" if is_live else "synth"
                if is_live:
                    # If the tick subscription is delivering quotes + trades,
                    # stuffing is live. If quotes but no trades, EMPTY
                    # (tick-by-tick subscription missing for trades).
                    if state.tick_quote_count_live > 0 and state.tick_trade_count_live > 0:
                        stuffing_source = "live"
                    elif state.tick_quote_count_live > 0:
                        stuffing_source = "empty_trades"
                    else:
                        stuffing_source = "empty"
                    if state.depth_update_count_live > 0:
                        depth_source = "live"
                    else:
                        depth_source = "empty"
                else:
                    stuffing_source = "synth"
                    depth_source = "synth"

                await state.broadcaster.publish(HFTEvent(
                    timestamp_ms=now_ms,
                    short_vol_pts=vol.short_vol_pts, long_vol_pts=vol.long_vol_pts,
                    vol_ratio=vol.ratio, vol_regime=vol.regime,
                    cancel_to_trade_ratio=stuff.cancel_to_trade_ratio,
                    quote_intensity_per_sec=stuff.quote_intensity_per_sec,
                    stuffing_severity=stuff.severity,
                    book_imbalance=depth.imbalance, book_imbalance_5lvl=depth.imbalance_5lvl,
                    book_direction=depth.direction, book_confidence=depth.confidence,
                    weighted_mid_price=depth.weighted_mid_price, book_spread=depth.spread,
                    composite_direction=composite, fired_on=fired_on,
                    vol_source=vol_source, stuffing_source=stuffing_source,
                    depth_source=depth_source,
                ))

                # Cache latest state on AppState so SignalIter can snapshot
                # at fire time (correlates HFT to each signal).
                state.last_hft_state = {
                    "timestamp_ms": now_ms,
                    "short_vol_pts": vol.short_vol_pts, "long_vol_pts": vol.long_vol_pts,
                    "vol_ratio": vol.ratio, "vol_regime": vol.regime,
                    "cancel_to_trade_ratio": stuff.cancel_to_trade_ratio,
                    "quote_intensity_per_sec": stuff.quote_intensity_per_sec,
                    "stuffing_severity": stuff.severity,
                    "book_imbalance": depth.imbalance,
                    "book_imbalance_5lvl": depth.imbalance_5lvl,
                    "book_direction": depth.direction,
                    "book_confidence": depth.confidence,
                    "weighted_mid_price": depth.weighted_mid_price,
                    "book_spread": depth.spread,
                    "composite_direction": composite,
                }

                # Persist HFTSample at FEF_HFT_PERSIST_SEC cadence (default 1s)
                if (now_ms - last_persist_ms[0]) >= persist_interval * 1000:
                    last_persist_ms[0] = now_ms
                    try:
                        from fef_sidecar.db.models import HFTSample
                        from fef_sidecar.db.engine import get_session as _gs
                        sample = HFTSample(
                            symbol=state.active_symbol,
                            timestamp_ms=now_ms,
                            short_vol_pts=vol.short_vol_pts, long_vol_pts=vol.long_vol_pts,
                            vol_ratio=vol.ratio, vol_regime=vol.regime,
                            cancel_to_trade_ratio=stuff.cancel_to_trade_ratio,
                            quote_intensity_per_sec=stuff.quote_intensity_per_sec,
                            stuffing_severity=stuff.severity,
                            book_imbalance=depth.imbalance,
                            book_imbalance_5lvl=depth.imbalance_5lvl,
                            book_direction=depth.direction,
                            book_confidence=depth.confidence,
                            weighted_mid_price=depth.weighted_mid_price,
                            book_spread=depth.spread,
                            composite_direction=composite,
                            vol_source=vol_source,
                            stuffing_source=stuffing_source,
                            depth_source=depth_source,
                        )
                        async with _gs() as _s:
                            _s.add(sample)
                            await _s.commit()
                    except Exception:
                        logger.exception("HFTSample persist failed")
            except _asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("HFT iteration failed")
            await _asyncio.sleep(interval)
    finally:
        if cancel_depth is not None:
            try:
                await cancel_depth()
            except Exception:
                logger.exception("Depth cancel failed")


async def microstructure_loop(state: AppState) -> None:
    """Continuously broadcast a microstructure signal every
    FEF_MICRO_INTERVAL_SEC (default 0.5s).

    Live mode (LiveIBKRProvider): subscribes to reqTickByTickData on the
    MICRO_PROXY of the active symbol (SPX/SPY -> ES futures, QQQ -> NQ,
    IWM -> RTY — futures lead cash; SPX itself is an index with no tape;
    see the proxy table below). Real bid/ask/trade events feed the
    OFI/AVI/microprice calculations. (Docstring corrected 2026-07-28 —
    it previously said "uses SPY", contradicting the proxy map fifty
    lines down; the exact stale-doc class the adjudication fixed.)

    Mock mode: synthesizes plausible quote + trade events around the
    current price anchor so the UI populates without a live feed.
    """
    import asyncio as _asyncio
    import random as _random
    import time as _time

    from fef_sidecar.engine.microstructure import (
        QuoteUpdate,
        TradePrint,
        assess,
        classify_trade,
        push_quote,
        push_trade,
    )
    from fef_sidecar.events import MicrostructureEvent
    from fef_sidecar.ibkr.live import LiveIBKRProvider

    interval = float(os.environ.get("FEF_MICRO_INTERVAL_SEC", "0.5"))
    persist_every_ms = int(float(os.environ.get("FEF_MICRO_PERSIST_SEC", "1.0")) * 1000)
    last_persisted_ms = 0
    rng = _random.Random()

    # Both callbacks ALSO feed the stuffing state (single tick stream,
    # multiple consumers). HFT loop reads state.stuffing_state for its
    # cancel-to-trade ratio — without this, live stuffing detection was
    # broken (only quotes, never trades).
    from fef_sidecar.engine.quote_stuffing import (
        push_quote as _stuff_push_quote,
        push_trade as _stuff_push_trade,
    )

    def _on_quote(ts_ms: int, bid: float, ask: float, bs: float, as_: float) -> None:
        push_quote(state.microstructure_state, QuoteUpdate(
            timestamp_ms=ts_ms, bid=bid, ask=ask, bid_size=bs, ask_size=as_,
        ))
        if state.stuffing_state is not None:
            _stuff_push_quote(state.stuffing_state, ts_ms)
        state.tick_quote_count_live += 1

    # Microstructure proxy mapping — futures lead their cash/ETF counterparts.
    # ES is the source of truth for S&P 500 order flow (24h tape, consolidated
    # CME book, professional liquidity); SPY is fragmented retail tape across
    # ~15 exchanges. Same logic for NQ vs QQQ and RTY vs IWM. SPX is an index
    # with no tape so we MUST use a tradable proxy.
    MICRO_PROXY = {
        "SPX": "ES",
        "SPY": "ES",
        "QQQ": "NQ",
        "IWM": "RTY",
    }
    micro_symbol = MICRO_PROXY.get(state.active_symbol, state.active_symbol)

    def _on_trade(ts_ms: int, price: float, size: float, aggressor: str) -> None:
        push_trade(state.microstructure_state, TradePrint(
            timestamp_ms=ts_ms, price=price, size=size, aggressor=aggressor,  # type: ignore[arg-type]
        ))
        if state.stuffing_state is not None:
            _stuff_push_trade(state.stuffing_state, ts_ms)
        state.tick_trade_count_live += 1
        # Feed the SAME trade prints into the tick-candle aggregators
        # (1000T / 2000T chart resolutions). Piggybacks on this existing
        # AllLast subscription — no extra IBKR tick-by-tick slot used
        # (IBKR default cap: 3 simultaneous tick-by-tick subscriptions).
        try:
            state.tick_candles.feed(micro_symbol, ts_ms, price, size, source="live")
        except Exception:
            pass

    cancel_live = None
    if isinstance(state.ibkr, LiveIBKRProvider):
        try:
            await state.ibkr.connect()
            cancel_live = await state.ibkr.subscribe_tick_by_tick(
                Contract.for_symbol(micro_symbol), _on_quote, _on_trade,
            )
            logger.info("Microstructure loop subscribed to LIVE %s ticks (interval=%.2fs)", micro_symbol, interval)
        except Exception:
            logger.exception("Live microstructure subscription failed — falling back to synth")
            cancel_live = None
    else:
        logger.info("Microstructure loop in MOCK mode (synthesizing ticks, interval=%.2fs)", interval)

    try:
        while True:
            try:
                if cancel_live is None:
                    # MOCK: synthesize a quote + a trade per tick interval
                    spx = state.last_spx_price or 7400.0
                    now_ms = int(_time.time() * 1000)
                    spread = round(rng.uniform(0.05, 0.25), 2)
                    bid = round(spx - spread / 2, 2)
                    ask = round(spx + spread / 2, 2)
                    bid_size = float(rng.randint(50, 500))
                    ask_size = float(rng.randint(50, 500))
                    push_quote(state.microstructure_state, QuoteUpdate(
                        timestamp_ms=now_ms, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size,
                    ))
                    trade_price = round(spx + rng.uniform(-spread / 2, spread / 2), 2)
                    trade_size = float(rng.randint(1, 50))
                    agg = classify_trade(trade_price, bid, ask)
                    push_trade(state.microstructure_state, TradePrint(
                        timestamp_ms=now_ms, price=trade_price, size=trade_size, aggressor=agg,
                    ))
                    # Mock tick-candle feed under the proxy symbol so the
                    # 1000T/2000T chart works in dev mode (tagged 'mock').
                    try:
                        state.tick_candles.feed(
                            micro_symbol, now_ms, trade_price, trade_size, source="mock",
                        )
                    except Exception:
                        pass

                now_ms = int(_time.time() * 1000)
                sig = assess(state.microstructure_state, now_ms)
                # Cache for SignalIter snapshot at fire time
                state.last_microstructure_state = {
                    "timestamp_ms": sig.timestamp_ms,
                    "ofi_window": sig.ofi_window,
                    "avi_pct": sig.avi_pct,
                    "microprice": sig.microprice,
                    "microprice_deviation_bp": sig.microprice_deviation_bp,
                    "direction": sig.direction,
                    "confidence": sig.confidence,
                }
                await state.broadcaster.publish(MicrostructureEvent(
                    timestamp_ms=sig.timestamp_ms,
                    ofi_window=sig.ofi_window,
                    avi_pct=sig.avi_pct,
                    aggressive_buy_volume=sig.aggressive_buy_volume,
                    aggressive_sell_volume=sig.aggressive_sell_volume,
                    microprice=sig.microprice,
                    last_trade_price=sig.last_trade_price,
                    microprice_deviation_bp=sig.microprice_deviation_bp,
                    direction=sig.direction,
                    confidence=sig.confidence,
                    reasoning=sig.reasoning,
                    quote_count=sig.quote_count,
                    trade_count=sig.trade_count,
                ))
                # Persist sampled (every FEF_MICRO_PERSIST_SEC, default 1s)
                if now_ms - last_persisted_ms >= persist_every_ms:
                    try:
                        proxy = MICRO_PROXY.get(state.active_symbol, state.active_symbol) if cancel_live else state.active_symbol
                        await SignalIterRepository.write_microstructure(
                            symbol=proxy, timestamp_ms=sig.timestamp_ms,
                            ofi_window=sig.ofi_window, avi_pct=sig.avi_pct,
                            aggressive_buy_volume=sig.aggressive_buy_volume,
                            aggressive_sell_volume=sig.aggressive_sell_volume,
                            microprice=sig.microprice,
                            last_trade_price=sig.last_trade_price,
                            microprice_deviation_bp=sig.microprice_deviation_bp,
                            direction=sig.direction, confidence=sig.confidence,
                        )
                        last_persisted_ms = now_ms
                    except Exception:
                        logger.exception("Failed to persist microstructure sample")
            except _asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Microstructure iteration failed")
            await _asyncio.sleep(interval)
    finally:
        if cancel_live is not None:
            try:
                await cancel_live()
            except Exception:
                logger.exception("Microstructure live cancel failed")


async def tick_stream_loop(state: AppState) -> None:
    """Stream last-price ticks. Watchdog: if no tick arrives within
    TICK_STALL_THRESHOLD_SEC we treat the IBKR subscription as wedged
    and force a reconnect — silent stalls used to leave the Live Quote
    panel frozen until manual app restart.
    """
    import time as _t
    from fef_sidecar.engine.loop_watchdog import heartbeat as _wd_beat
    stall_threshold = int(os.environ.get("FEF_TICK_STALL_THRESHOLD_SEC", "30"))
    logger.info(
        "Starting tick stream loop for %s (stall watchdog=%ds)",
        state.active_symbol, stall_threshold,
    )
    while True:
        # Re-read the active symbol per (re)subscribe and follow switches
        # below — the subscription must never stay keyed to the spawn-time
        # symbol (2026-07-07: bar/tick capture lost the ES->SPX switch).
        symbol = state.active_symbol
        try:
            await state.ibkr.connect()
            async def _stream():
                async for q in state.ibkr.stream_last_price(Contract.for_symbol(symbol)):
                    yield q
            agen = _stream()
            try:
                while True:
                    _wd_beat(state, "tick-stream")
                    if state.active_symbol != symbol:
                        logger.info(
                            "Tick stream: active symbol %s -> %s — resubscribing",
                            symbol, state.active_symbol,
                        )
                        break
                    try:
                        quote = await asyncio.wait_for(
                            agen.__anext__(), timeout=stall_threshold,
                        )
                    except (asyncio.TimeoutError, TimeoutError):
                        logger.warning(
                            "Tick stream stalled for >%ds on %s — forcing reconnect",
                            stall_threshold, symbol,
                        )
                        raise
                    except StopAsyncIteration:
                        logger.info("Tick stream generator ended — restarting")
                        break
                    state.last_spx_price = quote.last
                    state.last_live_tick_spx = quote.last
                    state.last_live_tick_ms = int(quote.timestamp.timestamp() * 1000)
                    # Provenance stamp (2026-07-23 review MAJOR-2): consumers
                    # gating on the "active tick" must be able to verify WHICH
                    # symbol's feed wrote it (a post-flip stale value belongs
                    # to the previous symbol).
                    state.last_live_tick_symbol = symbol
                    event = TickEvent(
                        timestamp_ms=int(quote.timestamp.timestamp() * 1000),
                        spx_price=quote.last, bar=None,
                        bid=quote.bid, ask=quote.ask,
                        bid_size=quote.bid_size, ask_size=quote.ask_size,
                        symbol=symbol,
                        prior_close=state.prior_close_by_symbol.get(symbol),
                    )
                    await state.broadcaster.publish(event)
            finally:
                try:
                    await agen.aclose()
                except Exception:
                    pass
        except asyncio.CancelledError:
            logger.info("Tick stream cancelled — exiting")
            raise
        except Exception:
            logger.exception("Tick stream failed — reconnecting in 5s")
            # Force a fresh main IBKR connection so the next stream_last_price
            # subscription gets a clean reqMktData slot.
            try:
                if hasattr(state.ibkr, "_ib") and state.ibkr._ib is not None:  # type: ignore[attr-defined]
                    state.ibkr._ib.disconnect()  # type: ignore[attr-defined]
            except Exception:
                pass
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# IBKR health check — pings every 30s, broadcasts StatusEvent on state change
# ---------------------------------------------------------------------------


async def ibkr_health_check_loop(state: AppState) -> None:
    """Ping IBKR every FEF_IBKR_HEALTHCHECK_SEC seconds. On state change,
    broadcast a StatusEvent so the UI dot updates without waiting for an
    iter or a manual refresh. On transition to disconnected, force a
    reconnect attempt — the streams will then catch their broken
    subscriptions and re-subscribe.
    """
    from fef_sidecar.events import StatusEvent

    interval = float(os.environ.get("FEF_IBKR_HEALTHCHECK_SEC", "30"))
    logger.info("Starting IBKR health check loop (interval=%.1fs)", interval)

    # Wait for the initial connect race (bar/tick loops all calling
    # connect() simultaneously) to settle before starting health checks.
    # Without this, ping fires while ib_async is mid-connect and returns
    # False → forces a disconnect that fights with the in-flight connect,
    # cascading into a never-stable state.
    await asyncio.sleep(interval)

    last_status = state.ibkr_status

    async def _publish(current: str) -> None:
        event = StatusEvent(
            ibkr=current,
            engine="running" if state.loop_task is not None else "stopped",
            history_size=state.engine.history_size,
        )
        await state.broadcaster.publish(event)

    while True:
        try:
            healthy = await state.ibkr.ping()
            current = "connected" if healthy else "disconnected"
            state.ibkr_status = current
            # Keep state.account_class / account_id in sync with the provider.
            # The provider populates them on connect; mirroring on state makes
            # them accessible to read paths (UI / audit) without a provider ref.
            state.account_class = getattr(state.ibkr, "account_class", "unknown") or "unknown"
            state.account_id = getattr(state.ibkr, "account_id", "") or ""

            if current != last_status:
                logger.info("IBKR status change: %s -> %s", last_status, current)
                await _publish(current)
                last_status = current

                if current == "disconnected":
                    # Force reconnect — streams will re-subscribe on next iter
                    logger.warning("IBKR ping failed; forcing reconnect")
                    state.ibkr_status = "reconnecting"
                    await _publish("reconnecting")
                    try:
                        await state.ibkr.disconnect()
                    except Exception:
                        pass
                    try:
                        await state.ibkr.connect()
                        if await state.ibkr.ping():
                            state.ibkr_status = "connected"
                            last_status = "connected"
                            await _publish("connected")
                            logger.info("IBKR reconnect succeeded")
                        else:
                            logger.warning("Reconnect did not restore health")
                    except Exception:
                        logger.exception("Forced reconnect failed; streams will retry")
        except asyncio.CancelledError:
            logger.info("IBKR health check cancelled — exiting")
            raise
        except Exception:
            logger.exception("IBKR health check iteration failed")

        await asyncio.sleep(interval)
