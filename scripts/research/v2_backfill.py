"""v2 engine recompute backfill.

Walks every signal_iter row in signal_log_v2.db and re-derives the
engine's verdict + flags using the v2-corrected formulas:

  1. fe_ratio: requires |delta_price| >= 0.5pt; sub-0.5pt produces
     fe_ratio = 0 (v1 clamped denominator at 0.1, inflating fe_ratio
     on tiny price moves — likely mechanical cause of structural
     BULLISH drift_bias)
  2. drift_bias: threshold raised from $1M to $3M absolute (v1's $1M
     was arbitrarily small relative to typical session flow)
  3. absorption: re-evaluated using v2 fe_ratio
  4. verdict: re-evaluated using v2 flags

Read-only on input columns; updates `direction`, `confidence`, `drift_bias`,
`reasoning`, and inserts v1 backups under `extras.v1_*` keys for
side-by-side comparison.

v1 DB at ~/.fef-terminal/signal_log.db remains untouched.
v2 DB at ~/.fef-terminal/signal_log_v2.db gets the recomputed columns.

CLI:
    python v2_backfill.py
    python v2_backfill.py --date-from 2026-05-19 --date-to 2026-05-21
    python v2_backfill.py --dry-run  # report changes without writing

The script does NOT recompute wall_collapse_trigger, wall_proximity_skip,
or the HFT / microstructure features — those depend on rolling state
or raw L2 events we don't fully reconstruct here. Stored v1 values are
used. Documented limitation.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

V2_DB = str(Path.home() / ".fef-terminal" / "signal_log_v2.db")

# v2 thresholds
MIN_DELTA_PRICE_FOR_FE = 0.5      # pt — gates fe_ratio computation
ABSORPTION_FE_MIN = 5.0           # default — overrides per-row if extras has it
DRIFT_BIAS_THRESHOLD_M = 3.0      # absolute net premium $M for BULLISH/BEARISH
ALIGNED_MIN = 1.0                 # pt — magnitude floor for aligned flag


def _v2_fe_ratio(delta_price: float, delta_drift_m: float) -> float:
    if abs(delta_price) < MIN_DELTA_PRICE_FOR_FE:
        return 0.0
    return abs(delta_drift_m) / abs(delta_price)


def _v2_aligned(delta_price: float, delta_drift_m: float) -> bool:
    same_sign = (
        (delta_price > 0 and delta_drift_m > 0)
        or (delta_price < 0 and delta_drift_m < 0)
    )
    return same_sign and abs(delta_price) >= ALIGNED_MIN and abs(delta_drift_m) >= ALIGNED_MIN


def _v2_absorption(fe_ratio: float, delta_price: float, delta_drift_m: float,
                   absorption_fe_min: float = ABSORPTION_FE_MIN) -> bool:
    same_sign = (
        (delta_price > 0 and delta_drift_m > 0)
        or (delta_price < 0 and delta_drift_m < 0)
    )
    return (
        (fe_ratio >= absorption_fe_min and not same_sign and abs(delta_drift_m) >= 3.0)
        or (fe_ratio >= 10.0 and abs(delta_drift_m) >= 5.0)
    )


def _v2_drift_bias(drift_cum_net_m: float) -> str:
    if drift_cum_net_m > DRIFT_BIAS_THRESHOLD_M:
        return "BULLISH"
    if drift_cum_net_m < -DRIFT_BIAS_THRESHOLD_M:
        return "BEARISH"
    return "NEUTRAL"


def _classify_confidence_v2(delta_price: float, delta_drift_m: float, fe_ratio: float) -> str:
    """Mirrors v1 confidence classification (no change in v2). Bands:
       HIGH:    |delta_price| >= 2.0 AND |delta_drift_m| >= 3.0
       MEDIUM:  |delta_price| >= 1.5 AND |delta_drift_m| >= 2.0
       LOW:     otherwise (aligned still fires)
    """
    if abs(delta_price) >= 2.0 and abs(delta_drift_m) >= 3.0:
        return "HIGH"
    if abs(delta_price) >= 1.5 and abs(delta_drift_m) >= 2.0:
        return "MEDIUM"
    return "LOW"


def _v2_verdict(
    *,
    delta_price: float,
    delta_drift_m: float,
    fe_ratio_v2: float,
    aligned_v2: bool,
    absorption_v2: bool,
    wall_collapse_trigger_v1: bool,
    wall_proximity_skip_v1: bool,
    drift_bias_v2: str,
) -> tuple[str, str, str]:
    """Returns (direction, confidence, reasoning). Mirrors v1 verdict
    precedence (wall_collapse → wall_proximity_skip → aligned →
    absorption → WAIT) with v2 flags substituted."""
    if wall_collapse_trigger_v1 and aligned_v2:
        direction = "LONG_CALLS" if delta_price > 0 else "LONG_PUTS"
        return direction, "HIGH", (
            f"v2: WALL COLLAPSE + ALIGNED {direction} — ΔPrice {delta_price:+.2f}pts, "
            f"ΔDrift {delta_drift_m:+.2f}M, FE {fe_ratio_v2:.2f}."
        )
    if wall_proximity_skip_v1:
        return "WAIT", "NONE", "v2: WALL PROXIMITY SKIP (unchanged from v1)."
    if aligned_v2:
        direction = "LONG_CALLS" if delta_price > 0 else "LONG_PUTS"
        confidence = _classify_confidence_v2(delta_price, delta_drift_m, fe_ratio_v2)
        wants_bull = direction == "LONG_CALLS"
        bias_disagrees = (
            (wants_bull and drift_bias_v2 == "BEARISH")
            or (not wants_bull and drift_bias_v2 == "BULLISH")
        )
        if bias_disagrees:
            confidence = {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}[confidence]
        return direction, confidence, (
            f"v2: ALIGNED {direction} — ΔPrice {delta_price:+.2f}pts, "
            f"ΔDrift {delta_drift_m:+.2f}M, FE {fe_ratio_v2:.2f}, "
            f"bias={drift_bias_v2}"
            + (" (bias-disagrees, demoted)" if bias_disagrees else "")
            + "."
        )
    if absorption_v2:
        return "WAIT", "LOW", (
            f"v2: ABSORPTION — drift {delta_drift_m:+.2f}M, price {delta_price:+.2f}pts, "
            f"FE {fe_ratio_v2:.2f}. Waiting for confirmation."
        )
    return "WAIT", "NONE", "v2: no aligned confluence, no absorption."


def backfill(
    db_path: str = V2_DB,
    date_from: str | None = None,
    date_to: str | None = None,
    dry_run: bool = False,
) -> dict:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    where = ""
    params: tuple = ()
    if date_from and date_to:
        where = "WHERE session_date BETWEEN ? AND ?"
        params = (date_from, date_to)
    elif date_from:
        where = "WHERE session_date >= ?"
        params = (date_from,)
    elif date_to:
        where = "WHERE session_date <= ?"
        params = (date_to,)

    rows = cur.execute(
        f"""
        SELECT id, session_date, iter_num, timestamp, spx_price, direction,
               confidence, reasoning, drift_cum_net_m, drift_bias, extras
        FROM signal_iter
        {where}
        ORDER BY session_date, iter_num
        """,
        params,
    ).fetchall()

    direction_flips = defaultdict(int)  # (v1, v2) -> count
    fe_ratio_zeroed = 0
    drift_bias_changes = defaultdict(int)  # (v1, v2) -> count
    n_updated = 0
    n_total = 0

    updates: list[tuple] = []
    for r in rows:
        n_total += 1
        try:
            ex = json.loads(r["extras"]) if r["extras"] else {}
        except json.JSONDecodeError:
            ex = {}
        delta_price = ex.get("delta_price")
        delta_drift_m = ex.get("delta_drift_m")
        wall_proximity_skip = bool(ex.get("wall_proximity_skip"))
        wall_collapse_trigger = bool(ex.get("wall_collapse_trigger"))
        absorption_fe_min = ex.get("absorption_fe_min", ABSORPTION_FE_MIN)

        # Some early iters have no delta_price / delta_drift_m
        # (need ≥ history_window iters before first eval). Leave them as-is.
        if delta_price is None or delta_drift_m is None:
            continue

        fe_ratio_v2 = _v2_fe_ratio(delta_price, delta_drift_m)
        aligned_v2 = _v2_aligned(delta_price, delta_drift_m)
        absorption_v2 = _v2_absorption(
            fe_ratio_v2, delta_price, delta_drift_m, absorption_fe_min
        )
        drift_bias_v2 = _v2_drift_bias(r["drift_cum_net_m"])

        if fe_ratio_v2 == 0.0 and ex.get("fe_ratio", 0) > 0:
            fe_ratio_zeroed += 1

        direction_v2, confidence_v2, reasoning_v2 = _v2_verdict(
            delta_price=delta_price,
            delta_drift_m=delta_drift_m,
            fe_ratio_v2=fe_ratio_v2,
            aligned_v2=aligned_v2,
            absorption_v2=absorption_v2,
            wall_collapse_trigger_v1=wall_collapse_trigger,
            wall_proximity_skip_v1=wall_proximity_skip,
            drift_bias_v2=drift_bias_v2,
        )

        direction_flips[(r["direction"], direction_v2)] += 1
        drift_bias_changes[(r["drift_bias"], drift_bias_v2)] += 1

        # Preserve v1 values in extras under v1_* keys
        new_extras = dict(ex)
        for k in ("fe_ratio", "aligned", "absorption"):
            new_extras[f"v1_{k}"] = ex.get(k)
        new_extras["fe_ratio"] = round(fe_ratio_v2, 4)
        new_extras["aligned"] = aligned_v2
        new_extras["absorption"] = absorption_v2
        new_extras["v2_recomputed"] = True
        new_extras["v2_recomputed_at"] = datetime.utcnow().isoformat()

        updates.append((
            direction_v2, confidence_v2, reasoning_v2, drift_bias_v2,
            json.dumps(new_extras), r["id"],
        ))
        n_updated += 1

    if not dry_run and updates:
        cur.executemany(
            """
            UPDATE signal_iter
            SET direction = ?, confidence = ?, reasoning = ?,
                drift_bias = ?, extras = ?
            WHERE id = ?
            """,
            updates,
        )
        db.commit()
    db.close()

    return {
        "n_total": n_total,
        "n_updated": n_updated,
        "fe_ratio_zeroed_count": fe_ratio_zeroed,
        "direction_flips": dict(direction_flips),
        "drift_bias_changes": dict(drift_bias_changes),
        "dry_run": dry_run,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=V2_DB)
    ap.add_argument("--date-from", default=None)
    ap.add_argument("--date-to", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    result = backfill(args.db, args.date_from, args.date_to, args.dry_run)
    print(f"## v2 Engine Recompute — {'DRY RUN' if args.dry_run else 'APPLIED'}")
    print(f"- Total signal_iter rows: {result['n_total']}")
    print(f"- Updated: {result['n_updated']}")
    print(f"- fe_ratio zeroed (was non-zero in v1): {result['fe_ratio_zeroed_count']}")
    print()
    print("### Direction flips (v1 → v2)")
    print()
    print("| v1 → v2 | count |")
    print("|---|---:|")
    for (v1d, v2d), cnt in sorted(result["direction_flips"].items(), key=lambda x: -x[1]):
        marker = " " if v1d == v2d else " ← CHANGED"
        print(f"| {v1d} → {v2d}{marker} | {cnt} |")
    print()
    print("### drift_bias changes")
    print()
    print("| v1 → v2 | count |")
    print("|---|---:|")
    for (v1b, v2b), cnt in sorted(result["drift_bias_changes"].items(), key=lambda x: -x[1]):
        marker = " " if v1b == v2b else " ← CHANGED"
        print(f"| {v1b} → {v2b}{marker} | {cnt} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
