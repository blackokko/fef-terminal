#!/usr/bin/env python
r"""Standalone, continuous options-microstructure / HFT capture for SPX research.

WHY THIS EXISTS
---------------
The production microstructure + HFT sampling lives INSIDE the trading engine
loop (src/fef_sidecar/loop.py :: hft_loop / microstructure_loop). It therefore
only runs while the user is actively trading the sidecar. Result: a month-long
gap (all of June 2026 empty), wildly inconsistent per-day volume, and muddled
symbol labelling (HFTSample tagged by active_symbol='SPX' but the book is really
ES; MicrostructureSample tagged by the ES proxy). See capture_health.py output.

This script is a self-contained daemon that captures the SAME features using the
SAME compute code (imported, not reimplemented) on an ISOLATED IBKR client so it
can run EVERY session, decoupled from the trading engine, for forward validation.

WHAT IT CAPTURES (per 1s during RTH 09:30-16:00 ET, tag = the real symbol)
    ES  (E-mini S&P future, the S&P microstructure lead per [[futures-lead-cash]]):
        - MicrostructureSample: OFI / AVI / microprice / microprice-deviation
          (fed by real tick-by-tick BidAsk + AllLast).
        - HFTSample: realized-vol regime (fed by quote-mid tick series),
          quote-stuffing / cancel-to-trade (real tick counts), L2 book imbalance
          (real reqMktDepth), composite direction.
    SPX (cash index -- has NO order book and NO trade tape):
        - HFTSample: realized-vol only, fed by the index last-price stream.
          Book / stuffing columns are written with honest 'empty' source badges.
        - No MicrostructureSample (there is no bid/ask book to compute OFI from).
          This is deliberate: we do NOT fabricate SPX order-flow that the index
          does not have. For SPX order-flow microstructure, ES is the proxy.

    (Pass --symbols to capture others. FUTURE/STOCK kinds get the full ES-style
     treatment; INDEX kinds get vol-only.)

SAFETY (this daemon must NEVER disturb the live trading sidecar)
    * ISOLATED clientId (default 151). The sidecar uses 1 (+101 historical).
      TWS supports many simultaneous API clients; a 151 connection cannot see or
      touch the sidecar's client-1 orders/subscriptions.
    * The IBKR connection is opened with readonly=True -> TWS rejects ANY order
      placement/modification/cancel from this client at the API level. This
      daemon only calls market-data subscription APIs (reqTickByTickData /
      reqMktDepth / reqMktData). It has no order code path at all.
    * DB writes go to the same signal_log.db but SQLite is in WAL mode with a
      30s busy_timeout (see db/engine.py) -- the sidecar already runs several
      concurrent writers; one more 1Hz writer is safe and non-blocking.
    * Schema: we only call init_db() (idempotent, no-op on the existing DB) if
      the sample tables are missing. Normal runs take ZERO schema locks.

RUN
    # Full session (waits for RTH open if launched early, auto-exits after close)
    .\.venv\Scripts\python.exe scripts\maintenance\capture_microstructure_standalone.py

    # Brief isolated connectivity test (writes a few rows then stops cleanly)
    ... capture_microstructure_standalone.py --test-seconds 8

    # Options
    --symbols ES,SPX      symbols to capture (default ES,SPX)
    --client-id 151       isolated IBKR clientId (MUST differ from the sidecar's)
    --host 127.0.0.1  --port 7497
    --persist-sec 1.0     write cadence
    --test-seconds N      run N seconds ignoring the RTH gate, then stop (0=off)
    --no-rth-gate         capture continuously (incl. overnight ES), no 09:30-16:00 gate
    --end-buffer-min 5    keep capturing this many minutes past 16:00 before exit

DEPLOYMENT (Windows Task Scheduler) -- see scripts/maintenance/CAPTURE_DEPLOYMENT.md
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# --- compute engines: IMPORTED from the sidecar, NOT reimplemented -----------
from fef_sidecar.engine.microstructure import (
    MicrostructureState,
    QuoteUpdate,
    TradePrint,
    assess as micro_assess,
    push_quote as micro_push_quote,
    push_trade as micro_push_trade,
)
from fef_sidecar.engine.quote_stuffing import (
    StuffingState,
    assess as stuff_assess,
    push_quote as stuff_push_quote,
    push_trade as stuff_push_trade,
)
from fef_sidecar.engine.realized_vol import (
    RealizedVolState,
    assess as vol_assess,
    push_tick as vol_push,
)
from fef_sidecar.engine.depth import (
    DepthState,
    assess as depth_assess,
    update_book,
)
from fef_sidecar.ibkr.models import Contract, ContractKind
from fef_sidecar.ibkr.live import LiveIBKRProvider
from fef_sidecar.db.engine import get_session, init_db
from fef_sidecar.db.models import HFTSample, MicrostructureSample

ET = ZoneInfo("America/New_York")

# ASCII-only console output (Windows consoles default to cp1252).
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("micro_capture")
# ib_async is chatty at INFO; keep it to warnings so our log stays readable.
logging.getLogger("ib_async").setLevel(logging.WARNING)
logging.getLogger("ib_insync").setLevel(logging.WARNING)


@dataclass
class SymbolCapture:
    """Per-symbol rolling state + live subscription bookkeeping."""

    symbol: str
    kind: ContractKind
    vol: RealizedVolState = field(default_factory=RealizedVolState)
    stuff: StuffingState = field(default_factory=StuffingState)
    depth: DepthState = field(default_factory=DepthState)
    micro: MicrostructureState = field(default_factory=MicrostructureState)

    quote_count: int = 0
    trade_count: int = 0
    depth_count: int = 0
    lastprice_count: int = 0

    # what this symbol's contract kind supports
    has_ticks: bool = False   # tick-by-tick BidAsk+AllLast (book + tape)
    has_depth: bool = False   # reqMktDepth L2
    write_micro: bool = False  # persist MicrostructureSample (needs a book)

    cancels: list = field(default_factory=list)   # async cancel handles
    tasks: list = field(default_factory=list)     # asyncio tasks (last-price)


def _composite_direction(stuff_sev: str, book_dir: str, vol_regime: str) -> str:
    """Replicates loop.py::hft_loop composite logic exactly."""
    if stuff_sev == "STUFFING":
        return "FADE"
    if book_dir == "BID_HEAVY" and vol_regime != "EXPANSION":
        return "BULLISH"
    if book_dir == "ASK_HEAVY" and vol_regime != "EXPANSION":
        return "BEARISH"
    if vol_regime == "EXPANSION":
        return "VOLATILE"
    return "NEUTRAL"


def _rth_bounds(now_et: datetime) -> tuple[datetime, datetime]:
    d = now_et.date()
    start = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    end = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)
    return start, end


async def _setup_symbol(provider: LiveIBKRProvider, sym: str) -> SymbolCapture:
    """Subscribe to whatever the symbol's contract kind supports."""
    contract = Contract.for_symbol(sym)
    cap = SymbolCapture(symbol=sym, kind=contract.kind)

    if contract.kind in (ContractKind.FUTURE, ContractKind.STOCK):
        # Full microstructure: real book + tape + depth. Vol fed from quote mids.
        cap.has_ticks = True
        cap.has_depth = True
        cap.write_micro = True

        def on_quote(ts: int, bid: float, ask: float, bs: float, as_: float,
                     _cap: SymbolCapture = cap) -> None:
            micro_push_quote(_cap.micro, QuoteUpdate(
                timestamp_ms=ts, bid=bid, ask=ask, bid_size=bs, ask_size=as_))
            stuff_push_quote(_cap.stuff, ts)
            _cap.quote_count += 1
            if bid > 0 and ask > 0:
                vol_push(_cap.vol, ts, (bid + ask) / 2.0)

        def on_trade(ts: int, price: float, size: float, agg: str,
                     _cap: SymbolCapture = cap) -> None:
            micro_push_trade(_cap.micro, TradePrint(
                timestamp_ms=ts, price=price, size=size, aggressor=agg))  # type: ignore[arg-type]
            stuff_push_trade(_cap.stuff, ts)
            _cap.trade_count += 1

        cancel_ticks = await provider.subscribe_tick_by_tick(contract, on_quote, on_trade)
        cap.cancels.append(cancel_ticks)
        logger.info("%s: subscribed tick-by-tick (BidAsk+AllLast)", sym)

        try:
            def on_book(ts: int, bids, asks, _cap: SymbolCapture = cap) -> None:
                update_book(_cap.depth, bids, asks, ts)
                _cap.depth_count += 1

            cancel_depth = await provider.subscribe_depth(contract, on_book)
            cap.cancels.append(cancel_depth)
            logger.info("%s: subscribed L2 depth (reqMktDepth)", sym)
        except Exception:
            cap.has_depth = False
            logger.warning("%s: depth subscription failed -- book will be 'empty'", sym)

    elif contract.kind == ContractKind.INDEX:
        # Index: no book, no tape. Only a last-price stream feeds realized vol.
        cap.has_ticks = False
        cap.has_depth = False
        cap.write_micro = False

        async def vol_stream(_cap: SymbolCapture = cap, _c: Contract = contract) -> None:
            try:
                async for q in provider.stream_last_price(_c):
                    px = float(q.last) if q.last else 0.0
                    if px > 0:
                        vol_push(_cap.vol, int(time.time() * 1000), px)
                        _cap.lastprice_count += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s: last-price stream error", _cap.symbol)

        cap.tasks.append(asyncio.create_task(vol_stream(), name=f"vol-{sym}"))
        logger.info("%s: subscribed index last-price stream (realized-vol only)", sym)
    else:
        logger.warning("%s: unsupported kind %s -- skipping", sym, contract.kind)

    return cap


def _build_rows(cap: SymbolCapture, now_ms: int) -> tuple[HFTSample, MicrostructureSample | None]:
    vol = vol_assess(cap.vol, now_ms)
    stuff = stuff_assess(cap.stuff, now_ms)
    depth = depth_assess(cap.depth, now_ms)

    vol_source = "live_sampled" if (cap.quote_count or cap.lastprice_count) else "synth"
    if cap.quote_count > 0 and cap.trade_count > 0:
        stuffing_source = "live"
    elif cap.quote_count > 0:
        stuffing_source = "empty_trades"
    else:
        stuffing_source = "empty"
    depth_source = "live" if cap.depth_count > 0 else "empty"

    composite = _composite_direction(stuff.severity, depth.direction, vol.regime)

    hft = HFTSample(
        symbol=cap.symbol,
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

    micro = None
    if cap.write_micro:
        sig = micro_assess(cap.micro, now_ms)
        micro = MicrostructureSample(
            symbol=cap.symbol, timestamp_ms=now_ms,
            ofi_window=sig.ofi_window, avi_pct=sig.avi_pct,
            aggressive_buy_volume=sig.aggressive_buy_volume,
            aggressive_sell_volume=sig.aggressive_sell_volume,
            microprice=sig.microprice, last_trade_price=sig.last_trade_price,
            microprice_deviation_bp=sig.microprice_deviation_bp,
            direction=sig.direction, confidence=sig.confidence,
        )
    return hft, micro


async def _ensure_schema() -> None:
    """Call init_db() ONLY if the sample tables are missing (avoid schema locks
    on the live DB in the normal case where the sidecar already created them)."""
    from sqlalchemy import text
    from fef_sidecar.db.engine import _get_engine  # type: ignore

    engine = _get_engine()
    async with engine.connect() as conn:
        res = await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('microstructure_sample','hft_sample')"
        )
        have = {r[0] for r in res.fetchall()}
    if {"microstructure_sample", "hft_sample"} - have:
        logger.info("Sample tables missing -> running idempotent init_db()")
        await init_db()
    else:
        logger.info("Sample tables present -> skipping schema init (zero schema locks)")


async def run(args: argparse.Namespace) -> int:
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    now_et = datetime.now(ET)
    logger.info("=" * 72)
    logger.info("STANDALONE MICROSTRUCTURE CAPTURE (research, read-only w.r.t. trading)")
    logger.info("  clientId=%d (isolated)  host=%s port=%d  readonly=True",
                args.client_id, args.host, args.port)
    logger.info("  symbols=%s  persist=%.1fs  now=%s ET",
                symbols, args.persist_sec, now_et.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("  NOTE: this process places NO orders and cannot touch the sidecar.")
    logger.info("=" * 72)

    if args.client_id in (1, 101):
        logger.error("REFUSING to run: clientId %d collides with the live sidecar. "
                     "Use an isolated id (e.g. 151).", args.client_id)
        return 2

    test_mode = args.test_seconds and args.test_seconds > 0
    gate = (not args.no_rth_gate) and (not test_mode)

    # --- RTH gate at startup ---------------------------------------------
    if gate:
        if now_et.weekday() >= 5:
            logger.info("Weekend (%s) -- market closed, nothing to capture. Exiting.",
                        now_et.strftime("%A"))
            return 0
        rth_start, rth_end = _rth_bounds(now_et)
        if now_et > rth_end + timedelta(minutes=args.end_buffer_min):
            logger.info("Past today's RTH close (%s). Exiting.", rth_end.strftime("%H:%M"))
            return 0
        if now_et < rth_start:
            wait_s = (rth_start - now_et).total_seconds()
            logger.info("Before RTH open. Sleeping %.0fs until 09:30 ET...", wait_s)
            await asyncio.sleep(wait_s)

    await _ensure_schema()

    # --- connect an ISOLATED, READ-ONLY IBKR client ----------------------
    # We build the provider (so we can reuse its subscribe_* helpers) but hand
    # it a connection we opened ourselves with readonly=True. provider.connect()
    # then early-returns because it is already connected -> the readonly conn is
    # what every subscribe_* call uses.
    provider = LiveIBKRProvider(host=args.host, port=args.port, client_id=args.client_id)
    from ib_async import IB
    ib = IB()
    await ib.connectAsync(args.host, args.port, clientId=args.client_id, readonly=True)
    provider._ib = ib  # type: ignore[attr-defined]
    acct = ib.managedAccounts()
    logger.info("Connected (readonly). account(s)=%s", acct or "?")

    caps: list[SymbolCapture] = []
    try:
        for sym in symbols:
            try:
                caps.append(await _setup_symbol(provider, sym))
            except Exception:
                logger.exception("Setup failed for %s -- skipping it", sym)

        if not caps:
            logger.error("No symbols set up -- exiting.")
            return 1

        # brief warmup so the rolling windows have some data before first write
        logger.info("Warming up (3s)...")
        await asyncio.sleep(3.0)

        start_wall = time.time()
        written = 0
        last_log = 0.0
        logger.info("Capturing. Ctrl-C to stop.")
        while True:
            now_ms = int(time.time() * 1000)
            now_et = datetime.now(ET)

            in_window = True
            if gate:
                rth_start, rth_end = _rth_bounds(now_et)
                in_window = rth_start <= now_et <= rth_end

            if in_window:
                try:
                    async with get_session() as s:
                        for cap in caps:
                            hft, micro = _build_rows(cap, now_ms)
                            s.add(hft)
                            if micro is not None:
                                s.add(micro)
                    written += 1
                except Exception:
                    logger.exception("persist tick failed (continuing)")

            # periodic heartbeat (~every 30s)
            if time.time() - last_log >= 30:
                last_log = time.time()
                parts = []
                for cap in caps:
                    parts.append(
                        f"{cap.symbol}[q={cap.quote_count} t={cap.trade_count} "
                        f"d={cap.depth_count} lp={cap.lastprice_count}]")
                logger.info("hb %s ET | writes=%d | %s",
                            now_et.strftime("%H:%M:%S"), written, " ".join(parts))

            # stop conditions
            if test_mode and (time.time() - start_wall) >= args.test_seconds:
                logger.info("Test window (%ds) elapsed -> stopping.", args.test_seconds)
                break
            if gate:
                rth_start, rth_end = _rth_bounds(now_et)
                if now_et > rth_end + timedelta(minutes=args.end_buffer_min):
                    logger.info("Past RTH close + buffer -> stopping for the day.")
                    break

            await asyncio.sleep(args.persist_sec)

        logger.info("Capture finished. total 1s ticks written=%d", written)
        for cap in caps:
            logger.info("  %s totals: quotes=%d trades=%d depth=%d lastprice=%d write_micro=%s",
                        cap.symbol, cap.quote_count, cap.trade_count,
                        cap.depth_count, cap.lastprice_count, cap.write_micro)
        return 0
    finally:
        logger.info("Cleaning up subscriptions...")
        for cap in caps:
            for cancel in cap.cancels:
                try:
                    await cancel()
                except Exception:
                    logger.warning("cancel failed for %s", cap.symbol)
            for task in cap.tasks:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        try:
            ib.disconnect()
        except Exception:
            pass
        logger.info("Disconnected (client %d).", args.client_id)


def main() -> None:
    ap = argparse.ArgumentParser(description="Standalone SPX/ES microstructure capture daemon")
    ap.add_argument("--symbols", default="ES,SPX")
    ap.add_argument("--client-id", type=int, default=int(os.environ.get("FEF_CAPTURE_CLIENT_ID", "151")))
    ap.add_argument("--host", default=os.environ.get("FEF_IB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("FEF_IB_PORT", "7497")))
    ap.add_argument("--persist-sec", type=float, default=1.0)
    ap.add_argument("--test-seconds", type=int, default=0,
                    help="Run this many seconds ignoring the RTH gate, then stop (0=off).")
    ap.add_argument("--no-rth-gate", action="store_true",
                    help="Capture continuously with no 09:30-16:00 ET gate (incl. overnight ES).")
    ap.add_argument("--end-buffer-min", type=int, default=5)
    args = ap.parse_args()

    try:
        rc = asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("Interrupted -- shutting down.")
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    main()
