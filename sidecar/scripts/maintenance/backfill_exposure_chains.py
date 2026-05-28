"""Backfill the FULL per-strike exposure chain (all 4 greeks) from QuantData.

Background
----------
Report 36 (quantdata_api_gap_audit) confirmed QuantData's exposure_by_strike
returns ~200 strikes for GAMMA/DELTA/CHARM/VANNA every poll, but the live
flow_snapshot path persists only ``snap.gex.top(5)`` (truncation at
loop.py). The discarded full chain is exactly what a cumulative-gamma /
gamma-flip regime classifier needs. This script backfills the full chain for
past sessions so the Phase 0 gamma-flip regime test is unblocked WITHOUT
waiting 10-15 live sessions for the new live_poll capture to accumulate.

Mechanics
---------
QuantData's exposure_by_strike is scoped by the page-filter's session_date.
Setting the page filter to a past trading day and fetching returns that day's
chain (verified across 5/18-5/27 — each date returns a distinct spot + chain).

Intraday resolution: the endpoint does NOT honour any intraday time hint
(verified 2026-05-28 — query params + metadata ``time`` fields are all
ignored; the endpoint always returns the single latest/EOD chain per
session_date). So this backfill captures ONE full-chain snapshot per session
(per greek), not open/mid/close. That single snapshot still spans the spot/
flip zone and is sufficient to locate the gamma flip for regime classification
— which top-5 truncation could not do.

Idempotency
-----------
Each row keys on (timestamp_ms, greek_type, strike) via the UNIQUE index
``idx_exposure_chain_pt_unique`` created by init_db. The per-session
timestamp is a deterministic anchor (16:00 ET of the session) so reruns
dedup cleanly. source='backfill'.

Usage
-----

.. code-block:: bash

    python scripts/maintenance/backfill_exposure_chains.py --dates 2026-05-18 2026-05-19 ...
    python scripts/maintenance/backfill_exposure_chains.py --from 2026-05-18 --to 2026-05-27
    python scripts/maintenance/backfill_exposure_chains.py --from 2026-05-18 --to 2026-05-27 --ticker SPX
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
GREEKS = ("GAMMA", "DELTA", "CHARM", "VANNA")


def _is_weekend(d: datetime) -> bool:
    return d.weekday() >= 5  # Sat=5, Sun=6


def _expand_date_range(from_d: str, to_d: str) -> list[str]:
    """All weekday dates in [from_d, to_d] inclusive (YYYY-MM-DD).

    Weekends are dropped. Holidays (e.g. Memorial Day 2026-05-25) aren't in a
    calendar here — the QuantData fetch simply returns no/garbage data for a
    closed day and we skip it at fetch time.
    """
    start = datetime.strptime(from_d, "%Y-%m-%d")
    end = datetime.strptime(to_d, "%Y-%m-%d")
    out: list[str] = []
    cur = start
    while cur <= end:
        if not _is_weekend(cur):
            out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def _session_anchor_ms(session_date: str) -> int:
    """Deterministic per-session timestamp: 16:00 ET (RTH close) of the date.

    Used as timestamp_ms so backfill reruns dedup against the unique index.
    The exposure endpoint returns the EOD/latest chain, so anchoring at the
    close is the honest representation of when the snapshot reflects.
    """
    dt = datetime.strptime(session_date, "%Y-%m-%d").replace(
        hour=16, minute=0, second=0, microsecond=0, tzinfo=ET
    )
    return int(dt.timestamp() * 1000)


async def _backfill_session(client, session_date: str, ticker: str) -> dict:
    """Pull all 4 greeks for one session and persist ExposureChainPoint rows.

    Returns a stats dict: {session_date, spot, strikes_per_greek, persisted,
    skipped, flip_strike, flip_ok}.
    """
    from fef_sidecar.quantdata.models import GreekType
    from fef_sidecar.db.models import ExposureChainPoint

    await client.set_page_filter(session_date=session_date, ticker=ticker)
    ts = _session_anchor_ms(session_date)

    rows: list = []
    spot = 0.0
    strikes_per_greek: dict[str, int] = {}
    gamma_rows: list = []

    for greek_name in GREEKS:
        try:
            snap = await client.get_exposure_by_strike(GreekType(greek_name))
        except Exception as e:  # noqa: BLE001
            print(f"    {greek_name}: fetch failed — {type(e).__name__}: {str(e)[:80]}")
            strikes_per_greek[greek_name] = 0
            continue

        if not snap.rows or snap.spx_price <= 0:
            print(f"    {greek_name}: empty/closed-day chain — skipped")
            strikes_per_greek[greek_name] = 0
            continue

        spot = snap.spx_price
        strikes_per_greek[greek_name] = len(snap.rows)
        if greek_name == "GAMMA":
            gamma_rows = sorted(snap.rows, key=lambda r: r.strike)

        for r in snap.rows:
            rows.append(ExposureChainPoint(
                timestamp_ms=ts,
                session_date=session_date,
                symbol=ticker,
                greek_type=greek_name,
                strike=float(r.strike),
                net_m=round(float(r.net_m), 4),
                call_m=round(float(r.call_m), 4),
                put_m=round(float(r.put_m), 4),
                spot_at_capture=float(spot),
                source="backfill",
            ))

    persisted, skipped = await _persist(rows)

    flip_strike, flip_ok = _locate_gamma_flip(gamma_rows, spot)

    return {
        "session_date": session_date,
        "spot": spot,
        "strikes_per_greek": strikes_per_greek,
        "persisted": persisted,
        "skipped": skipped,
        "flip_strike": flip_strike,
        "flip_ok": flip_ok,
    }


def _locate_gamma_flip(gamma_rows: list, spot: float) -> tuple[float | None, bool]:
    """Find the dealer gamma-flip ("zero gamma") strike nearest spot.

    Definition: the gamma-flip level is the strike at which per-strike net
    gamma changes sign — below it dealers are net short gamma (volatility-
    amplifying regime), above it net long gamma (volatility-dampening). In the
    backfilled SPX chains net_m is strongly negative around/below spot and
    turns positive just above, so the flip sits right around the spot strike.

    We scan adjacent (strike, net_m) pairs for a sign change and pick the
    crossing whose midpoint is closest to spot, then linearly interpolate the
    zero. Returns (flip_strike, near_spot) where near_spot is True if the flip
    is within 150pt of spot — the thing top-5 wall truncation could NOT
    compute (it kept only 5 strikes, never the full profile around spot).
    """
    if not gamma_rows or spot <= 0:
        return None, False
    best_flip: float | None = None
    best_dist = float("inf")
    for prev, cur in zip(gamma_rows, gamma_rows[1:], strict=False):
        a, b = prev.net_m, cur.net_m
        # Sign change between two consecutive non-zero strikes.
        if (a < 0 < b) or (a > 0 > b):
            span = b - a
            frac = (0.0 - a) / span if span else 0.0
            flip = prev.strike + frac * (cur.strike - prev.strike)
            dist = abs(flip - spot)
            if dist < best_dist:
                best_dist, best_flip = dist, flip
    if best_flip is None:
        return None, False
    return round(best_flip, 2), best_dist <= 150.0


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

    await init_db()  # ensure table + unique index exist

    print(f"DB: {db_path}")
    print(f"Ticker: {ticker}")
    print(f"Sessions: {', '.join(dates)}\n")

    total = 0
    async with QuantDataClient() as client:
        for d in dates:
            print(f"--- {d} ---")
            stats = await _backfill_session(client, d, ticker)
            total += stats["persisted"]
            spg = stats["strikes_per_greek"]
            spg_str = ", ".join(f"{g}={n}" for g, n in spg.items())
            print(f"  spot={stats['spot']:.2f}  strikes/greek: {spg_str}")
            print(f"  persisted={stats['persisted']}  skipped(dup)={stats['skipped']}")
            if stats["flip_strike"] is not None:
                flag = "near spot" if stats["flip_ok"] else "FAR from spot"
                print(f"  gamma flip @ {stats['flip_strike']}  ({flag}, spot={stats['spot']:.2f})")
            else:
                print("  gamma flip: NOT FOUND (no sign change in cumulative GEX)")
            print()

    print(f"TOTAL persisted: {total} rows")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
