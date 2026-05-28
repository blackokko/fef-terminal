"""Backfill the FULL per-strike OI chain + raw aggression buckets from QuantData.

Background
----------
Report 36 (quantdata_api_gap_audit) confirmed two more QuantData streams we
fetch but truncate at the flow_snapshot persistence layer:

  - OI chain (get_oi_by_strike): returns ~238 strikes; flow_snapshot collapses
    it to ntm_max_pain / raw_max_pain scalars. The full per-strike OI is what
    pinning / range-regime detection needs (where the largest OI walls sit
    relative to spot, and how that map evolves).
  - Raw aggression (get_trade_side_stats): returns 10 buckets (AA/A/M/B/BB ×
    call/put); flow_snapshot collapses to a single aggression_call_pct /
    aggression_put_pct, losing the capitulation-vs-chase detail (BB extreme-
    passive vs M neutral vs AA extreme-aggressive).

This mirrors backfill_exposure_chains.py exactly (same gap, same fix). It
backfills past sessions so research isn't blocked waiting for the new
live_poll capture to accumulate.

Mechanics
---------
Both endpoints are scoped by the page-filter's session_date (they take no
per-call date arg — they read the page filter state). Setting the page filter
to a past trading day and fetching returns that day's data.

Intraday resolution: like exposure_by_strike, these endpoints do NOT honour an
intraday time hint — the API serves the single latest (EOD/current) snapshot
per session_date. So this backfill captures ONE OI chain + ONE aggression
snapshot per session, anchored 16:00 ET (RTH close). That's fine for research.
OI and aggression aren't greek-typed, so each is pulled exactly once per session.

Idempotency
-----------
  - OiChainPoint keys on (timestamp_ms, strike) via idx_oi_chain_pt_unique.
  - AggressionDetailPoint keys on (timestamp_ms, symbol) via
    idx_aggression_detail_pt_unique.
The per-session timestamp is the deterministic 16:00 ET anchor so reruns dedup
cleanly. source='backfill'.

Usage
-----

.. code-block:: bash

    python scripts/maintenance/backfill_oi_and_aggression.py --dates 2026-05-18 2026-05-19 ...
    python scripts/maintenance/backfill_oi_and_aggression.py --from 2026-05-18 --to 2026-05-27
    python scripts/maintenance/backfill_oi_and_aggression.py --from 2026-05-18 --to 2026-05-27 --ticker SPX
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure the sidecar `src` package is importable when run as a bare script.
_SIDECAR_ROOT = Path(__file__).resolve().parents[2]
_SRC = _SIDECAR_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

ET = timezone(timedelta(hours=-4))

# US market holidays in the backfill window. The QuantData fetch returns
# empty/garbage for a closed day and we skip it at fetch time anyway, but
# listing them keeps the run log honest.
KNOWN_HOLIDAYS = {"2026-05-25"}  # Memorial Day 2026


def _is_weekend(d: datetime) -> bool:
    return d.weekday() >= 5  # Sat=5, Sun=6


def _expand_date_range(from_d: str, to_d: str) -> list[str]:
    """All weekday dates in [from_d, to_d] inclusive (YYYY-MM-DD), minus known
    holidays. Closed days that slip through still get skipped at fetch time."""
    start = datetime.strptime(from_d, "%Y-%m-%d")
    end = datetime.strptime(to_d, "%Y-%m-%d")
    out: list[str] = []
    cur = start
    while cur <= end:
        ds = cur.strftime("%Y-%m-%d")
        if not _is_weekend(cur) and ds not in KNOWN_HOLIDAYS:
            out.append(ds)
        cur += timedelta(days=1)
    return out


async def _spot_from_exposure_chain(session_date: str, ticker: str) -> float:
    """Look up the EOD spot for a session from the already-backfilled
    exposure_chain_point rows (same 16:00 ET anchor, source='backfill').

    The historical OI + aggression endpoints return no stockPriceInCents, so
    spot_at_capture would otherwise be 0. The exposure-chain backfill captures
    the same session's spot, so we reuse it to keep these rows queryable
    against spot. Returns 0.0 if no exposure-chain row exists for the session.
    """
    from sqlalchemy import text

    from fef_sidecar.db.engine import get_session

    async with get_session() as session:
        res = await session.exec(  # type: ignore[call-overload]
            text(
                "SELECT spot_at_capture FROM exposure_chain_point "
                "WHERE session_date = :d AND symbol = :s AND spot_at_capture > 0 "
                "ORDER BY captured_at DESC LIMIT 1"
            ).bindparams(d=session_date, s=ticker)
        )
        row = res.first()
    if row is None:
        return 0.0
    return float(row[0])


def _session_anchor_ms(session_date: str) -> int:
    """Deterministic per-session timestamp: 16:00 ET (RTH close) of the date.

    Used as timestamp_ms so backfill reruns dedup against the unique indexes.
    The endpoints return the EOD/latest snapshot, so anchoring at the close is
    the honest representation of when the data reflects.
    """
    dt = datetime.strptime(session_date, "%Y-%m-%d").replace(
        hour=16, minute=0, second=0, microsecond=0, tzinfo=ET
    )
    return int(dt.timestamp() * 1000)


async def _backfill_session(client, session_date: str, ticker: str) -> dict:
    """Pull OI chain + aggression for one session and persist rows.

    Returns a stats dict: {session_date, spot, oi_strikes, oi_min_strike,
    oi_max_strike, oi_persisted, oi_skipped, agg_persisted, agg_skipped}.
    """
    from fef_sidecar.db.models import AggressionDetailPoint, OiChainPoint

    await client.set_page_filter(session_date=session_date, ticker=ticker)
    ts = _session_anchor_ms(session_date)

    # Historical OI + aggression endpoints return no stockPriceInCents — reuse
    # the EOD spot the exposure-chain backfill already captured for the session.
    fallback_spot = await _spot_from_exposure_chain(session_date, ticker)

    spot = 0.0
    oi_rows: list = []
    oi_min_strike: float | None = None
    oi_max_strike: float | None = None

    # ---- OI chain (full, ~238 strikes) ----
    try:
        oi_snap = await client.get_oi_by_strike()
    except Exception as e:  # noqa: BLE001
        print(f"    OI: fetch failed — {type(e).__name__}: {str(e)[:80]}")
        oi_snap = None

    if oi_snap is not None and oi_snap.rows:
        # Endpoint spot if present (live re-runs), else exposure-chain fallback.
        spot = oi_snap.spx_price if oi_snap.spx_price > 0 else fallback_spot
        sorted_rows = sorted(oi_snap.rows, key=lambda r: r.strike)
        oi_min_strike = sorted_rows[0].strike
        oi_max_strike = sorted_rows[-1].strike
        for r in oi_snap.rows:
            oi_rows.append(OiChainPoint(
                timestamp_ms=ts,
                session_date=session_date,
                symbol=ticker,
                strike=float(r.strike),
                call_oi=int(r.call_oi),
                put_oi=int(r.put_oi),
                spot_at_capture=float(spot),
                source="backfill",
            ))
    else:
        print("    OI: empty/closed-day chain — skipped")

    # ---- Aggression (full AA/A/M/B/BB buckets, one row) ----
    agg_rows: list = []
    try:
        agg = await client.get_trade_side_stats()
    except Exception as e:  # noqa: BLE001
        print(f"    aggression: fetch failed — {type(e).__name__}: {str(e)[:80]}")
        agg = None

    if agg is not None and agg.call is not None and agg.put is not None:
        c, p = agg.call, agg.put
        # Aggression endpoint has no spot — use whatever spot OI/exposure gave.
        agg_spot = spot if spot > 0 else fallback_spot
        # On a genuinely closed day there's no flow and no spot; skip those.
        has_flow = (c.total + p.total) > 0
        if agg_spot > 0 or has_flow:
            agg_rows.append(AggressionDetailPoint(
                timestamp_ms=ts,
                session_date=session_date,
                symbol=ticker,
                spot_at_capture=float(agg_spot),
                call_aa=float(c.aa),
                call_a=float(c.a),
                call_mid=float(c.mid),
                call_b=float(c.b),
                call_bb=float(c.bb),
                put_aa=float(p.aa),
                put_a=float(p.a),
                put_mid=float(p.mid),
                put_b=float(p.b),
                put_bb=float(p.bb),
                source="backfill",
            ))
        else:
            print("    aggression: no flow + no spot (closed day) — skipped")
    else:
        print("    aggression: empty — skipped")

    oi_persisted, oi_skipped = await _persist(oi_rows)
    agg_persisted, agg_skipped = await _persist(agg_rows)

    return {
        "session_date": session_date,
        "spot": spot,
        "oi_strikes": len(oi_rows),
        "oi_min_strike": oi_min_strike,
        "oi_max_strike": oi_max_strike,
        "oi_persisted": oi_persisted,
        "oi_skipped": oi_skipped,
        "agg_persisted": agg_persisted,
        "agg_skipped": agg_skipped,
    }


async def _persist(rows: list) -> tuple[int, int]:
    """Idempotent insert. Returns (persisted, skipped_as_duplicate)."""
    if not rows:
        return 0, 0
    from sqlalchemy.exc import IntegrityError

    from fef_sidecar.db.engine import get_session

    # Try bulk first; on any IntegrityError fall back to per-row to count dups.
    try:
        async with get_session() as session:
            session.add_all(rows)
            await session.flush()
        return len(rows), 0
    except IntegrityError:
        pass

    persisted = 0
    skipped = 0
    for r in rows:
        try:
            async with get_session() as session:
                session.add(r)
                await session.flush()
            persisted += 1
        except IntegrityError:
            skipped += 1
    return persisted, skipped


async def _run(dates: list[str], ticker: str, db_path: Path) -> int:
    os.environ.setdefault(
        "FEF_DB_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}"
    )

    from fef_sidecar.db.engine import init_db
    from fef_sidecar.quantdata.client import QuantDataClient

    await init_db()  # ensure tables + unique indexes exist

    print(f"DB: {db_path}")
    print(f"Ticker: {ticker}")
    print(f"Sessions: {', '.join(dates)}\n")

    total_oi = 0
    total_agg = 0
    async with QuantDataClient() as client:
        for d in dates:
            print(f"--- {d} ---")
            stats = await _backfill_session(client, d, ticker)
            total_oi += stats["oi_persisted"]
            total_agg += stats["agg_persisted"]
            coverage = ""
            if stats["oi_min_strike"] is not None:
                coverage = (
                    f"  range=[{stats['oi_min_strike']:.0f}, "
                    f"{stats['oi_max_strike']:.0f}]"
                )
            print(f"  spot={stats['spot']:.2f}  OI strikes={stats['oi_strikes']}{coverage}")
            print(
                f"  OI persisted={stats['oi_persisted']} skipped(dup)={stats['oi_skipped']}"
                f"  |  aggression persisted={stats['agg_persisted']} "
                f"skipped(dup)={stats['agg_skipped']}"
            )
            print()

    print(f"TOTAL persisted: OI={total_oi} rows, aggression={total_agg} rows")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dates", nargs="+", help="Explicit YYYY-MM-DD dates")
    ap.add_argument("--from", dest="from_d", help="Range start YYYY-MM-DD")
    ap.add_argument("--to", dest="to_d", help="Range end YYYY-MM-DD")
    ap.add_argument("--ticker", default="SPX")
    ap.add_argument(
        "--db",
        default=str(Path.home() / ".fef-terminal" / "signal_log.db"),
        help="Path to signal_log.db",
    )
    args = ap.parse_args()

    if args.dates:
        dates = args.dates
    elif args.from_d and args.to_d:
        dates = _expand_date_range(args.from_d, args.to_d)
    else:
        ap.error("provide either --dates or both --from and --to")

    return asyncio.run(_run(dates, args.ticker, Path(args.db)))


if __name__ == "__main__":
    raise SystemExit(main())
