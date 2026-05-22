"""Per-regime parameter sweep — Option 4 diagnostic (v2 methodology).

Runs the FEF threshold-knob sweep SEPARATELY per regime label
(PIN_DAY / TREND_DAY / CHOP / WALL_TEST / OPEN_DRIVE / NORMAL) and
checks whether the per-regime sub-optima are stable across two halves
of the window.

Answers the question: is `walk_forward_run.drift_score` high because
the regimes themselves are real (each has a different optimal config,
mixed-regime sweep is confused) OR because parameter tuning is noise-
fitting (per-regime sub-optima also drift)?

v2 methodology (2026-05-22, post Chat scrutiny)
-----------------------------------------------
v1 used optimization.py's `_proxy_pnl` = `(spx_future - spx_now) × $50 × qty`
with a fixed 3-iter (~6 min) lookahead and a hard −$10/pt stop. That's
crude. v1 reported TREND_DAY test_$ at −$25,856 — but the realistic-fill
estimator v2 showed real broker fills produced +$9k on the same signals.
The −$25k was a simulator artifact.

v2 replaces `_proxy_pnl` with an order-preserving 1-min bar walk +
50/30/20 partial scale-out, using T1/T2/T3 at ±1R/2R/3R and stop at
−1R from entry (R=10pt SPX, matches FEF's standard 0DTE targeting).
Same logic as ablation_backtest.py::_simulate_bcd minus peak-trail.

Compare to v1 by checking git history (commit 7af326e).

CLI
---
  python per_regime_sweep.py 2026-05-16 2026-05-21
  python per_regime_sweep.py 2026-05-16 2026-05-21 --out report.md
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "sidecar" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for ablation_backtest imports

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


def _ts_to_ms(ts_str: str) -> int:
    """Parse a SignalIter.timestamp string to UTC ms epoch.
    Mirrors ablation_backtest._ts_to_ms — DB stores naive UTC timestamps."""
    dt = datetime.fromisoformat(ts_str.replace(" ", "T"))
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _load_spx_bars(date_from: str, date_to: str, db_path: str) -> tuple[dict, list[int]]:
    """Load 1-min SPX bars covering the window + 60min buffer at each end.
    Returns (bar_by_ms dict, sorted list of timestamp_ms keys)."""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()
    bars = cur.execute(
        """
        SELECT timestamp_ms, open, high, low, close
        FROM historical_bar
        WHERE symbol='SPX' AND bar_size='1 min'
          AND timestamp_ms >= strftime('%s', ? || ' 00:00:00') * 1000 - 3600000
          AND timestamp_ms <  strftime('%s', ? || ' 23:59:59') * 1000 + 3600000
        ORDER BY timestamp_ms
        """,
        (date_from, date_to),
    ).fetchall()
    bar_by_ms = {b["timestamp_ms"]: dict(b) for b in bars}
    return bar_by_ms, sorted(bar_by_ms.keys())


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
               top_cex_walls, top_vex_walls,
               top_gex_by_expiry, top_dex_by_expiry,
               zero_dte_gex_net_m, zero_dte_dex_net_m,
               recent_whale_count_30m, recent_golden_sweep_count_30m,
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
            "timestamp_ms": _ts_to_ms(r["timestamp"]),
            "spx_price": r["spx_price"],
            "drift_cum_net_m": r["drift_cum_net_m"],
            "drift_bias": r["drift_bias"] or "NEUTRAL",
            "ntm_max_pain": r["ntm_max_pain"] or r["spx_price"],
            "raw_max_pain": r["raw_max_pain"] or r["spx_price"],
            "top_gex_walls": _parse_walls(r["top_gex_walls"]),
            "top_dex_walls": _parse_walls(r["top_dex_walls"]),
            "top_cex_walls": _parse_walls(r["top_cex_walls"]) if "top_cex_walls" in r.keys() else [],
            "top_vex_walls": _parse_walls(r["top_vex_walls"]) if "top_vex_walls" in r.keys() else [],
            "top_gex_by_expiry": _parse_walls(r["top_gex_by_expiry"]) if "top_gex_by_expiry" in r.keys() else [],
            "top_dex_by_expiry": _parse_walls(r["top_dex_by_expiry"]) if "top_dex_by_expiry" in r.keys() else [],
            "zero_dte_gex_net_m": r["zero_dte_gex_net_m"] if "zero_dte_gex_net_m" in r.keys() else None,
            "zero_dte_dex_net_m": r["zero_dte_dex_net_m"] if "zero_dte_dex_net_m" in r.keys() else None,
            "recent_whale_count_30m": int(r["recent_whale_count_30m"] or 0) if "recent_whale_count_30m" in r.keys() else 0,
            "recent_golden_sweep_count_30m": int(r["recent_golden_sweep_count_30m"] or 0) if "recent_golden_sweep_count_30m" in r.keys() else 0,
            "aggression_call_pct": r["aggression_call_pct"],
            "aggression_put_pct": r["aggression_put_pct"],
        })
    return dict(by_regime)


# v2 sweep parameters (mirror auto-trader's standard 0DTE targeting)
RISK_PTS = 10.0          # T1=±10pt SPX from entry, stop=∓10pt
T1_SHARE, T2_SHARE, T3_SHARE = 0.5, 0.3, 0.2  # 50/30/20 scale-out


def _simulate_partial_walk_v2(
    direction: str, entry_spx: float, fwd_bars: list[dict],
    risk_pts: float = RISK_PTS,
) -> float:
    """Order-preserving 1-min bar walk with 50/30/20 partial scale-out.

    Same shape as ablation_backtest.py::_simulate_bcd minus the peak-
    trail. Returns realized R as a float (positive = win, negative =
    loss). Conservative ordering: within a single 1-min bar, stop has
    priority if both stop and a tier level are crossed.

    T1 = entry ± 1R; T2 = ± 2R; T3 = ± 3R; stop = ∓ 1R.
    R = `risk_pts` SPX (default 10).
    Scale-out: 50% at T1, 30% at T2, 20% at T3. After partials, residual
    sells at the (still original) stop if it hits. If 60 min elapses
    without full exit, the residual exits at the last bar's close."""
    if not fwd_bars:
        return 0.0
    is_lc = direction == "LONG_CALLS"
    sign = 1 if is_lc else -1
    realized_r = 0.0
    remaining_share = 1.0
    tiers_fired: set[str] = set()
    for b in fwd_bars:
        adverse_pts = b["low"] if is_lc else b["high"]
        favor_pts = b["high"] if is_lc else b["low"]
        adverse_r = sign * (adverse_pts - entry_spx) / risk_pts
        favor_r = sign * (favor_pts - entry_spx) / risk_pts
        # Stop priority on ambiguous bars (conservative)
        if adverse_r <= -1.0 and remaining_share > 0:
            realized_r += remaining_share * -1.0
            return realized_r
        # Tier checks in order (T1 → T2 → T3)
        if favor_r >= 1.0 and "T1" not in tiers_fired:
            realized_r += T1_SHARE * 1.0
            remaining_share -= T1_SHARE
            tiers_fired.add("T1")
        if favor_r >= 2.0 and "T2" not in tiers_fired:
            realized_r += T2_SHARE * 2.0
            remaining_share -= T2_SHARE
            tiers_fired.add("T2")
        if favor_r >= 3.0 and "T3" not in tiers_fired:
            realized_r += T3_SHARE * 3.0
            remaining_share -= T3_SHARE
            tiers_fired.add("T3")
        if remaining_share <= 0.001:
            return realized_r
    # Timeout — exit residual at last close
    if remaining_share > 0 and fwd_bars:
        final_r = sign * (fwd_bars[-1]["close"] - entry_spx) / risk_pts
        realized_r += remaining_share * final_r
    return realized_r


def _run_sweep_v2(iters: list[dict], bar_by_ms: dict, bar_ms_sorted: list[int]):
    """v2 sweep: replays FEFEngine over candidate threshold configs to
    generate verdicts, then walks 1-min SPX bars per non-WAIT verdict
    with 50/30/20 partial scale-out. Replaces optimization.py's
    `_proxy_pnl` linear approximation.

    Returns a list of SweepResult-shaped dicts sorted by test_sharpe DESC."""
    import math
    from dataclasses import dataclass

    from fef_sidecar.engine.fef import FEFEngine
    from fef_sidecar.engine.types import FEFInputs
    from fef_sidecar.quantdata.models import ExposureRow

    if len(iters) < 10:
        return []
    split = int(len(iters) * 0.6)
    # Purge window: skip 3 iters between train and test to avoid
    # information leakage from overlapping 60-min forward windows.
    PURGE_ITERS = 3
    train_iters = iters[:split]
    test_iters = iters[split + PURGE_ITERS:]
    if len(test_iters) < 5:
        return []

    def bars_in_window(start_ms: int, end_ms: int) -> list[dict]:
        lo = bisect.bisect_left(bar_ms_sorted, start_ms)
        hi = bisect.bisect_right(bar_ms_sorted, end_ms)
        return [bar_by_ms[m] for m in bar_ms_sorted[lo:hi]]

    def _eval(slice_iters: list[dict], engine: FEFEngine):
        engine.reset()
        returns_R: list[float] = []
        wins = losses = 0
        for it in slice_iters:
            gex_rows = [
                ExposureRow(strike=w["strike"], call_m=max(0, w["net_m"]),
                            put_m=min(0, w["net_m"]), net_m=w["net_m"])
                for w in (it.get("top_gex_walls") or [])
            ]
            dex_rows = [
                ExposureRow(strike=w["strike"], call_m=max(0, w["net_m"]),
                            put_m=min(0, w["net_m"]), net_m=w["net_m"])
                for w in (it.get("top_dex_walls") or [])
            ]
            inputs = FEFInputs(
                timestamp_ms=it.get("timestamp_ms", 0),
                spx_price=it["spx_price"],
                drift_cum_net_m=it["drift_cum_net_m"],
                drift_bias=it.get("drift_bias", "NEUTRAL"),
                top_gex_walls=gex_rows,
                top_dex_walls=dex_rows,
                ntm_max_pain=it.get("ntm_max_pain", it["spx_price"]),
                raw_max_pain=it.get("raw_max_pain", it["spx_price"]),
                aggression_call_pct=it.get("aggression_call_pct"),
                aggression_put_pct=it.get("aggression_put_pct"),
            )
            verdict = engine.push(inputs)
            direction = verdict.direction.value if hasattr(verdict.direction, "value") else str(verdict.direction)
            if direction == "WAIT":
                continue
            sig_ms = it.get("timestamp_ms")
            if not sig_ms:
                continue
            fwd = bars_in_window(sig_ms, sig_ms + 60 * 60 * 1000)
            if not fwd:
                continue
            r = _simulate_partial_walk_v2(direction, it["spx_price"], fwd)
            returns_R.append(r)
            if r > 0.05:
                wins += 1
            elif r < -0.05:
                losses += 1
        return returns_R, wins, losses

    def _sharpe(returns):
        """Per-trade Sharpe (mean/std of trade returns). Not annualized —
        returns are per-trade, not daily, so sqrt(252) is incorrect."""
        if len(returns) < 5:
            return None
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        if var <= 0:
            return None
        return round(mean / math.sqrt(var), 2)

    # R-units convert to $ via the standard per-R constant
    from ablation_backtest import DOLLARS_PER_R
    R_DOLLARS = DOLLARS_PER_R

    results = []
    for prox in PROXIMITY_VALUES:
        for cpct in COLLAPSE_PCT_VALUES:
            for cfloor in COLLAPSE_FLOOR_VALUES:
                for afe in ABSORPTION_FE_VALUES:
                    engine = FEFEngine(
                        wall_proximity_pts=prox,
                        wall_collapse_pct=cpct,
                        wall_collapse_floor_m=cfloor,
                        absorption_fe_ratio_min=afe,
                    )
                    train_r, train_w, train_l = _eval(train_iters, engine)
                    test_r, test_w, test_l = _eval(test_iters, engine)
                    results.append({
                        "wall_proximity_pts": prox,
                        "wall_collapse_pct": cpct,
                        "wall_collapse_floor_m": cfloor,
                        "absorption_fe_ratio_min": afe,
                        "train_sharpe": _sharpe(train_r),
                        "test_sharpe": _sharpe(test_r),
                        "train_trades": len(train_r),
                        "test_trades": len(test_r),
                        "train_win_rate_pct": round(train_w / len(train_r) * 100, 1) if train_r else 0.0,
                        "test_win_rate_pct": round(test_w / len(test_r) * 100, 1) if test_r else 0.0,
                        "train_total_pnl": round(sum(train_r) * R_DOLLARS, 0),
                        "test_total_pnl": round(sum(test_r) * R_DOLLARS, 0),
                    })
    # Sort by test_sharpe DESC then test_total_pnl DESC
    results.sort(
        key=lambda r: (r["test_sharpe"] if r["test_sharpe"] is not None else -999,
                       r["test_total_pnl"]),
        reverse=True,
    )
    return results


# v2 sweep entry point — replaces the old optimization.run_sweep call.
# Bound to module-level _BARS_CACHE so caller doesn't have to thread bars
# through every invocation (set once at start of main()).
_BARS_CACHE: tuple[dict, list[int]] | None = None


def _run_sweep(iters: list[dict]):
    """v2 sweep using order-preserving bar walk. Requires _BARS_CACHE to be
    populated by main() before calling. Returns list of dict results
    (NOT the SweepResult dataclass — shape changed in v2)."""
    if _BARS_CACHE is None:
        raise RuntimeError("_BARS_CACHE not initialized; call _load_spx_bars first")
    bar_by_ms, bar_ms_sorted = _BARS_CACHE
    return _run_sweep_v2(iters, bar_by_ms, bar_ms_sorted)


def _top1_params(results) -> dict | None:
    if not results:
        return None
    r = results[0]
    # v2 results are dicts (not SweepResult dataclass)
    return {
        "wall_proximity_pts": r["wall_proximity_pts"],
        "wall_collapse_pct": r["wall_collapse_pct"],
        "wall_collapse_floor_m": r["wall_collapse_floor_m"],
        "absorption_fe_ratio_min": r["absorption_fe_ratio_min"],
        "test_sharpe": r["test_sharpe"],
        "test_total_pnl": r["test_total_pnl"],
        "test_trades": r["test_trades"],
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

    # Load 1-min SPX bars once and bind to module-level cache so the v2
    # sweep function can read them without thread-through arguments.
    bar_by_ms, bar_ms_sorted = _load_spx_bars(args.date_from, args.date_to, args.db)
    if not bar_by_ms:
        print(f"No SPX 1-min bars in window {args.date_from}..{args.date_to}", file=sys.stderr)
        return 1
    global _BARS_CACHE
    _BARS_CACHE = (bar_by_ms, bar_ms_sorted)

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
                f"| {i+1} | {r['wall_proximity_pts']} | {r['wall_collapse_pct']} "
                f"| {r['wall_collapse_floor_m']} | {r['absorption_fe_ratio_min']} "
                f"| {r['train_sharpe'] if r['train_sharpe'] is not None else '-'} "
                f"| {r['test_sharpe'] if r['test_sharpe'] is not None else '-'} "
                f"| {r['train_total_pnl']:+.0f} | {r['test_total_pnl']:+.0f} "
                f"| {r['train_trades']} | {r['test_trades']} |"
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
