"""Per-regime parameter sweep — Option 4 diagnostic.

Runs the FEF threshold-knob sweep SEPARATELY per regime label
(PIN_DAY / TREND_DAY / CHOP / WALL_TEST / OPEN_DRIVE / NORMAL) and
checks whether the per-regime sub-optima are stable across two halves
of the window.

Answers the question: is `walk_forward_run.drift_score` high because
the regimes themselves are real (each has a different optimal config,
mixed-regime sweep is confused) OR because parameter tuning is noise-
fitting (per-regime sub-optima also drift)?

Reading the output
------------------
- For each regime: top-5 configs from the FULL-window sweep + cross-
  half drift check.
- "Cross-half drift" = how different the first-half optimal is from
  the second-half optimal. Stable per-regime sub-optima → regimes are
  real → presets are the right architecture, just need a better
  classifier. Drift inside each regime → 4-knob design is noise-fit.

CLI
---
  python per_regime_sweep.py 2026-05-16 2026-05-21
  python per_regime_sweep.py 2026-05-16 2026-05-21 --out report.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "sidecar" / "src"))

DEFAULT_DB = str(Path.home() / ".fef-terminal" / "signal_log.db")

# Grid — keep it modest. 4 × 4 × 3 × 4 = 192 configs per regime.
PROXIMITY_VALUES = [3.0, 5.0, 8.0, 12.0]
COLLAPSE_PCT_VALUES = [0.10, 0.15, 0.20, 0.30]
COLLAPSE_FLOOR_VALUES = [3.0, 5.0, 8.0]
ABSORPTION_FE_VALUES = [2.0, 3.0, 5.0, 7.0]
LOOKAHEAD_ITERS = 3
MIN_ITERS_FOR_SWEEP = 30  # below this the sweep is too noisy to trust


def _parse_account_class_arg(raw: str) -> set[str]:
    if not raw:
        return {"paper"}
    raw = raw.strip().lower()
    if raw == "all":
        return {"paper", "live", "mock", "unknown"}
    return {x.strip() for x in raw.split(",") if x.strip()}


def _passes_account_filter(extras_dict: dict, accept: set[str]) -> bool:
    cls = (extras_dict.get("account_class") or "unknown") if isinstance(extras_dict, dict) else "unknown"
    if cls in accept:
        return True
    if cls == "unknown" and "paper" in accept:
        return True
    return False


def _load_iters_by_regime(
    date_from: str, date_to: str, db_path: str,
    accept_classes: set[str] | None = None,
) -> dict[str, list[dict]]:
    accept = accept_classes or {"paper"}
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()
    rows = cur.execute(
        """
        SELECT timestamp, spx_price, drift_cum_net_m, drift_bias,
               ntm_max_pain, raw_max_pain, top_gex_walls, top_dex_walls,
               aggression_call_pct, aggression_put_pct, extras
        FROM signal_iter
        WHERE session_date BETWEEN ? AND ?
          AND spx_price BETWEEN 1000 AND 10000
        ORDER BY timestamp
        """,
        (date_from, date_to),
    ).fetchall()

    by_regime: dict[str, list[dict]] = defaultdict(list)

    def _parse_walls(raw):
        if raw is None:
            return []
        if isinstance(raw, (list, dict)):
            return raw if isinstance(raw, list) else []
        try:
            return json.loads(raw) if isinstance(raw, str) else []
        except json.JSONDecodeError:
            return []

    for r in rows:
        try:
            ex = json.loads(r["extras"]) if r["extras"] else {}
        except (TypeError, json.JSONDecodeError):
            ex = {}
        # Account-class filter (default paper; treat unknown as paper)
        if not _passes_account_filter(ex, accept):
            continue
        # Prefer regime_at_fire; fall back to regime_demoted.regime if present,
        # else NORMAL — same precedence as daily_review.compute_daily_review.
        regime = ex.get("regime_at_fire") or (
            (ex.get("regime_demoted") or {}).get("regime") or "NORMAL"
        )
        by_regime[regime].append({
            "spx_price": r["spx_price"],
            "drift_cum_net_m": r["drift_cum_net_m"],
            "drift_bias": r["drift_bias"] or "NEUTRAL",
            "ntm_max_pain": r["ntm_max_pain"] or r["spx_price"],
            "raw_max_pain": r["raw_max_pain"] or r["spx_price"],
            "top_gex_walls": _parse_walls(r["top_gex_walls"]),
            "top_dex_walls": _parse_walls(r["top_dex_walls"]),
            "aggression_call_pct": r["aggression_call_pct"],
            "aggression_put_pct": r["aggression_put_pct"],
        })
    return dict(by_regime)


def _run_sweep(iters: list[dict]):
    from fef_sidecar.engine.optimization import SweepConfig, run_sweep
    cfg = SweepConfig(
        proximity_values=PROXIMITY_VALUES,
        collapse_pct_values=COLLAPSE_PCT_VALUES,
        collapse_floor_values=COLLAPSE_FLOOR_VALUES,
        absorption_fe_values=ABSORPTION_FE_VALUES,
        lookahead_iters=LOOKAHEAD_ITERS,
    )
    return run_sweep(iters, cfg)


def _top1_params(results) -> dict | None:
    if not results:
        return None
    r = results[0]
    return {
        "wall_proximity_pts": r.wall_proximity_pts,
        "wall_collapse_pct": r.wall_collapse_pct,
        "wall_collapse_floor_m": r.wall_collapse_floor_m,
        "absorption_fe_ratio_min": r.absorption_fe_ratio_min,
        "test_sharpe": r.test_sharpe,
        "test_total_pnl": r.test_total_pnl,
        "test_trades": r.test_trades,
    }


def _param_drift(p1: dict | None, p2: dict | None) -> float:
    """Normalized distance between two parameter sets. ~0 = same; >0.3 = drift."""
    if not p1 or not p2:
        return float("nan")
    norm = (
        abs(p1["wall_proximity_pts"] - p2["wall_proximity_pts"]) / 10.0
        + abs(p1["wall_collapse_pct"] - p2["wall_collapse_pct"]) / 0.3
        + abs(p1["wall_collapse_floor_m"] - p2["wall_collapse_floor_m"]) / 8.0
        + abs(p1["absorption_fe_ratio_min"] - p2["absorption_fe_ratio_min"]) / 7.0
    ) / 4
    return round(norm, 3)


def _fmt_param_row(label: str, p: dict | None) -> str:
    if not p:
        return f"| {label} | n/a | n/a | n/a | n/a | n/a | n/a |"
    return (
        f"| {label} | {p['wall_proximity_pts']} | {p['wall_collapse_pct']} "
        f"| {p['wall_collapse_floor_m']} | {p['absorption_fe_ratio_min']} "
        f"| {p['test_sharpe'] if p['test_sharpe'] is not None else 'n/a'} "
        f"| {p['test_total_pnl']:+.0f} |"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("date_from")
    ap.add_argument("date_to")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--account-class", default="paper",
                    help="Filter by extras.account_class. 'paper' (default), 'live', 'mock', "
                         "'unknown', 'all', or comma-separated.")
    ap.add_argument("--out", help="markdown report path (defaults to stdout)")
    args = ap.parse_args()

    accept = _parse_account_class_arg(args.account_class)
    by_regime = _load_iters_by_regime(args.date_from, args.date_to, args.db, accept)
    if not by_regime:
        print("No signal_iter rows in window", file=sys.stderr)
        return 1

    lines: list[str] = []
    lines.append("# Per-Regime Parameter Sweep")
    lines.append("")
    lines.append(f"- **Window**: {args.date_from} to {args.date_to}")
    n_combos = (
        len(PROXIMITY_VALUES) * len(COLLAPSE_PCT_VALUES)
        * len(COLLAPSE_FLOOR_VALUES) * len(ABSORPTION_FE_VALUES)
    )
    lines.append(f"- **Sweep grid**: {n_combos} configs per regime")
    lines.append(f"- **Lookahead**: {LOOKAHEAD_ITERS} iters")
    lines.append("")
    lines.append("## Regime distribution")
    lines.append("")
    lines.append("| Regime | n_iters | swept? |")
    lines.append("|---|---:|:---:|")
    for regime, items in sorted(by_regime.items(), key=lambda x: -len(x[1])):
        swept = "yes" if len(items) >= MIN_ITERS_FOR_SWEEP else f"no (need >={MIN_ITERS_FOR_SWEEP})"
        lines.append(f"| {regime} | {len(items)} | {swept} |")
    lines.append("")

    # Run sweeps per regime + cross-half drift check
    per_regime_summary: list[dict] = []
    for regime, items in sorted(by_regime.items(), key=lambda x: -len(x[1])):
        if len(items) < MIN_ITERS_FOR_SWEEP:
            continue

        full_results = _run_sweep(items)
        # Cross-half stability: split items in half, sweep each half
        mid = len(items) // 2
        first_half_results = _run_sweep(items[:mid]) if mid >= MIN_ITERS_FOR_SWEEP else []
        second_half_results = _run_sweep(items[mid:]) if (len(items) - mid) >= MIN_ITERS_FOR_SWEEP else []

        full_top1 = _top1_params(full_results)
        h1_top1 = _top1_params(first_half_results)
        h2_top1 = _top1_params(second_half_results)
        cross_half_drift = _param_drift(h1_top1, h2_top1)

        per_regime_summary.append({
            "regime": regime, "n": len(items),
            "full_top1": full_top1, "h1_top1": h1_top1, "h2_top1": h2_top1,
            "cross_half_drift": cross_half_drift,
            "top5": full_results[:5],
        })

        lines.append(f"## {regime} ({len(items)} iters)")
        lines.append("")
        lines.append("### Top 5 configs (full window)")
        lines.append("")
        lines.append("| rank | wall_prox | collapse_pct | collapse_floor | abs_fe | train_sharpe | test_sharpe | train_$ | test_$ | train_trades | test_trades |")
        lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for i, r in enumerate(full_results[:5]):
            lines.append(
                f"| {i+1} | {r.wall_proximity_pts} | {r.wall_collapse_pct} "
                f"| {r.wall_collapse_floor_m} | {r.absorption_fe_ratio_min} "
                f"| {r.train_sharpe if r.train_sharpe is not None else '-'} "
                f"| {r.test_sharpe if r.test_sharpe is not None else '-'} "
                f"| {r.train_total_pnl:+.0f} | {r.test_total_pnl:+.0f} "
                f"| {r.train_trades} | {r.test_trades} |"
            )
        lines.append("")
        lines.append("### Cross-half stability")
        lines.append("")
        lines.append("| split | wall_prox | collapse_pct | collapse_floor | abs_fe | test_sharpe | test_$ |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        lines.append(_fmt_param_row("full", full_top1))
        lines.append(_fmt_param_row("first half", h1_top1))
        lines.append(_fmt_param_row("second half", h2_top1))
        if cross_half_drift == cross_half_drift:  # not NaN
            verdict = (
                "STABLE (regime knobs may be real)" if cross_half_drift <= 0.15
                else "DRIFTING (regime sub-optimum is unstable)"
            )
            lines.append("")
            lines.append(f"**Cross-half drift**: {cross_half_drift:.3f} — {verdict}")
        else:
            lines.append("")
            lines.append("**Cross-half drift**: insufficient data in one half")
        lines.append("")

    # Final summary: are top-1 configs similar across regimes?
    if per_regime_summary:
        lines.append("---")
        lines.append("")
        lines.append("## Cross-regime comparison (top-1 per regime, full window)")
        lines.append("")
        lines.append("If wall_prox / collapse_pct / abs_fe optimal values are SIMILAR across regimes")
        lines.append("→ regimes are not behaviorally distinct (current knob design is regime-agnostic).")
        lines.append("If they DIFFER and each regime is internally STABLE (cross-half drift low)")
        lines.append("→ regimes are real, presets are the right architecture, classifier is the bottleneck.")
        lines.append("If each regime is also internally UNSTABLE")
        lines.append("→ knobs are noise-fitting; redesign the gate layer (Option 1) or accept noise (Option 3).")
        lines.append("")
        lines.append("| regime | n | wall_prox | collapse_pct | collapse_floor | abs_fe | test_sharpe | test_$ | cross_half_drift |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for s in per_regime_summary:
            p = s["full_top1"]
            if not p:
                continue
            lines.append(
                f"| {s['regime']} | {s['n']} | {p['wall_proximity_pts']} "
                f"| {p['wall_collapse_pct']} | {p['wall_collapse_floor_m']} "
                f"| {p['absorption_fe_ratio_min']} "
                f"| {p['test_sharpe'] if p['test_sharpe'] is not None else '-'} "
                f"| {p['test_total_pnl']:+.0f} "
                f"| {s['cross_half_drift'] if s['cross_half_drift'] == s['cross_half_drift'] else '-'} |"
            )
        lines.append("")

    md = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"Wrote {len(md.splitlines())} lines to {args.out}", file=sys.stderr)
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
