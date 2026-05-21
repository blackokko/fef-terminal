"""Ablation backtest: Baseline vs Direction-filter vs B+C+D vs Both.

Replays fired LONG_CALLS / LONG_PUTS signals against forward SPX 1-min
bars to estimate which post-freeze candidate change has the largest
realistic-fill P/L lift.

Variants:
  - Baseline: current SPX BE ladder + SPX-target T1/stop. Uses
    SignalLookaheadOutcome.would_have_hit_t1 / stop at the 60-min horizon.
  - Direction filter (v1): block PUTS when 30m SPX change > +3pt;
    block CALLS when 30m SPX change < -3pt. Otherwise = baseline.
  - B+C+D: option-price-proxy exit logic. Walk fwd 1-min bars; T1 at
    +1R, peak-trail at 15% pullback from peak (peak >= 0.3R floor),
    -1R hard stop, timeout at 60min.
  - Both: direction filter + B+C+D.

Limitations:
  - 1R = 10 SPX points (T1 distance = stop distance from extras.t1/stop).
  - B+C+D uses SPX moves as proxy for option-price moves (linear, no
    gamma asymmetry or theta — modest overstatement on 60min windows).
  - Direction filter uses raw 30m SPX change, not EMA stack. EMA version
    would block roughly the same fires on 1-min bars.
  - Filters were designed FROM 5/18-5/20 data — out-of-sample lift will
    be smaller. Treat as upper-bound estimate.

CLI:
    python ablation_backtest.py 2026-05-18 2026-05-20
    python ablation_backtest.py 2026-05-18 2026-05-20 --json

Programmatic:
    from ablation_backtest import run_ablation
    result = run_ablation("2026-05-18", "2026-05-20")
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = str(Path.home() / ".fef-terminal" / "signal_log.db")
DOLLARS_PER_R = 400.0  # 5 contracts × $2000 risk per trade


def _ts_to_ms(ts_str: str) -> int:
    dt = datetime.fromisoformat(ts_str.replace(" ", "T"))
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _simulate_bcd(entry_spx: float, direction_sign: int, fwd_bars: list[dict]) -> tuple[float, str]:
    """Walk forward 1-min bars; return (realized_R, exit_reason)."""
    if not fwd_bars:
        return 0.0, "no_data"
    peak_r = 0.0
    for b in fwd_bars:
        adverse_pts = b["low"] if direction_sign > 0 else b["high"]
        favor_pts = b["high"] if direction_sign > 0 else b["low"]
        adverse_r = direction_sign * (adverse_pts - entry_spx) / 10.0
        favor_r = direction_sign * (favor_pts - entry_spx) / 10.0
        if adverse_r <= -1.0:
            return -1.0, "stop"
        if favor_r >= 1.0:
            return +1.0, "t1"
        if favor_r > peak_r:
            peak_r = favor_r
        if peak_r >= 0.3:
            current_r = direction_sign * (b["close"] - entry_spx) / 10.0
            if current_r <= peak_r - 0.15:
                return peak_r - 0.15, "trail"
    final_r = direction_sign * (fwd_bars[-1]["close"] - entry_spx) / 10.0
    return final_r, "timeout"


def _parse_account_class_arg(raw: str) -> set[str]:
    if not raw:
        return {"paper"}
    raw = raw.strip().lower()
    if raw == "all":
        return {"paper", "live", "mock", "unknown"}
    return {x.strip() for x in raw.split(",") if x.strip()}


def _passes_account_filter(extras_raw, accept: set[str]) -> bool:
    """Return True if the row's extras.account_class is in accept.
    'unknown' (pre-tag rows) is treated as paper-equivalent when paper accepted."""
    if not extras_raw:
        return ("paper" in accept) or ("unknown" in accept)
    try:
        ex = json.loads(extras_raw) if isinstance(extras_raw, str) else extras_raw
    except json.JSONDecodeError:
        return ("paper" in accept) or ("unknown" in accept)
    cls = (ex.get("account_class") or "unknown") if isinstance(ex, dict) else "unknown"
    if cls in accept:
        return True
    if cls == "unknown" and "paper" in accept:
        return True
    return False


def run_ablation(
    date_from: str, date_to: str, db_path: str = DEFAULT_DB,
    accept_classes: set[str] | None = None,
) -> dict:
    """Returns a structured dict with per-trade rows, per-variant aggregates,
    and per-day breakdown. accept_classes filters by extras.account_class
    (default = {'paper'})."""
    accept = accept_classes or {"paper"}
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    raw_signals = cur.execute(
        """
        SELECT id, session_date, timestamp, direction, spx_price, extras
        FROM signal_iter
        WHERE session_date BETWEEN ? AND ?
          AND direction IN ('LONG_CALLS','LONG_PUTS')
          AND spx_price BETWEEN 1000 AND 10000
        ORDER BY session_date, timestamp
        """,
        (date_from, date_to),
    ).fetchall()
    signals = [s for s in raw_signals if _passes_account_filter(s["extras"], accept)]

    # Pull SPX 1-min bars covering window + buffer for 30min prior context
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
    bar_ms_sorted = sorted(bar_by_ms.keys())

    def bars_in_window(start_ms: int, end_ms: int) -> list[dict]:
        lo = bisect.bisect_left(bar_ms_sorted, start_ms)
        hi = bisect.bisect_right(bar_ms_sorted, end_ms)
        return [bar_by_ms[m] for m in bar_ms_sorted[lo:hi]]

    def bars_before(ms: int, n: int) -> list[dict]:
        idx = bisect.bisect_left(bar_ms_sorted, ms)
        return [bar_by_ms[m] for m in bar_ms_sorted[max(0, idx - n) : idx]]

    look = {
        r["signal_iter_id"]: dict(r)
        for r in cur.execute(
            """
            SELECT signal_iter_id, would_have_hit_t1, would_have_hit_stop
            FROM signal_lookahead_outcome
            WHERE lookahead_minutes = 60
              AND session_date BETWEEN ? AND ?
            """,
            (date_from, date_to),
        )
    }

    def _regime_from_extras(extras_raw) -> str:
        """Mirror compute_daily_review's precedence: regime_at_fire >
        regime_demoted.regime > NORMAL."""
        if not extras_raw:
            return "NORMAL"
        try:
            ex = json.loads(extras_raw) if isinstance(extras_raw, str) else extras_raw
        except json.JSONDecodeError:
            return "NORMAL"
        if not isinstance(ex, dict):
            return "NORMAL"
        return ex.get("regime_at_fire") or (
            (ex.get("regime_demoted") or {}).get("regime") or "NORMAL"
        )

    results: list[dict] = []
    for s in signals:
        sid = s["id"]
        if sid not in look:
            continue
        sig_ms = _ts_to_ms(s["timestamp"])
        end_ms = sig_ms + 60 * 60 * 1000
        fwd = bars_in_window(sig_ms, end_ms)
        direction_sign = +1 if s["direction"] == "LONG_CALLS" else -1
        entry_spx = float(s["spx_price"])

        prior = bars_before(sig_ms, 30)
        spx_30m_change = (prior[-1]["close"] - prior[0]["close"]) if len(prior) >= 5 else 0.0
        blocked = (direction_sign > 0 and spx_30m_change < -3.0) or (
            direction_sign < 0 and spx_30m_change > 3.0
        )

        hit_t1 = bool(look[sid]["would_have_hit_t1"])
        hit_stop = bool(look[sid]["would_have_hit_stop"])
        if hit_t1 and hit_stop:
            baseline_r = -0.15  # BE ladder gives back to slight-negative on roundtrip
        elif hit_t1:
            baseline_r = +1.0
        elif hit_stop:
            baseline_r = -1.0
        else:
            baseline_r = 0.0

        bcd_r, bcd_reason = _simulate_bcd(entry_spx, direction_sign, fwd)

        results.append({
            "sid": sid, "date": s["session_date"], "dir": s["direction"],
            "timestamp_ms": sig_ms,
            "regime": _regime_from_extras(s["extras"]),
            "entry_spx": entry_spx, "spx_30m_change": round(spx_30m_change, 2),
            "baseline_r": baseline_r,
            "direction_filter_r": 0.0 if blocked else baseline_r,
            "bcd_r": bcd_r,
            "combined_r": 0.0 if blocked else bcd_r,
            "blocked_by_dir_filter": blocked,
            "bcd_reason": bcd_reason,
        })

    def summary(rows: list[dict], key: str, group_key: str = "date") -> dict:
        totals: dict[str, dict] = defaultdict(
            lambda: {"n": 0, "sum_r": 0.0, "wins": 0, "losses": 0, "flat": 0}
        )
        for r in rows:
            v = r[key]
            for bucket in (r[group_key], "ALL"):
                totals[bucket]["n"] += 1
                totals[bucket]["sum_r"] += v
                if v > 0.05:
                    totals[bucket]["wins"] += 1
                elif v < -0.05:
                    totals[bucket]["losses"] += 1
                else:
                    totals[bucket]["flat"] += 1
        return dict(totals)

    variants = [
        ("Baseline", "baseline_r"),
        ("Direction-filter only", "direction_filter_r"),
        ("B+C+D only", "bcd_r"),
        ("Both (dir + B+C+D)", "combined_r"),
    ]
    aggregates = {name: summary(results, key) for name, key in variants}
    # Per-regime breakouts so we can see if B+C+D wins in one regime and
    # direction filter wins in another (tonight's 5/21 vs 5/18-5/20 flip).
    aggregates_by_regime = {name: summary(results, key, group_key="regime")
                            for name, key in variants}

    # Methodology stats (DSR + purged K-fold + PBO) — added 2026-05-21 per
    # Claude Chat's institutional-methodology gap analysis. DSR discounts
    # the winning variant's apparent Sharpe for the selection-bias of
    # having tested N variants. K-fold + PBO check whether the winner's
    # edge survives across multiple time slices.
    methodology_stats = _methodology_stats(results, variants)

    return {
        "date_from": date_from,
        "date_to": date_to,
        "n_signals": len(results),
        "n_blocked_by_dir_filter": sum(1 for r in results if r["blocked_by_dir_filter"]),
        "bcd_exit_reasons": dict(Counter(r["bcd_reason"] for r in results)),
        "aggregates": aggregates,
        "aggregates_by_regime": aggregates_by_regime,
        "methodology_stats": methodology_stats,
        "trades": results,
    }


def _methodology_stats(results: list[dict], variants: list[tuple[str, str]]) -> dict:
    """Compute DSR + per-fold Sharpe + PBO across the ablation variants.
    Returns a dict ready to format in the markdown report."""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).parent))
    from _stats import (
        deflated_sharpe_ratio,
        kfold_sharpes,
        probability_of_backtest_overfitting,
        sharpe_ratio,
    )

    # Order results by timestamp for chronological splitting
    sorted_results = sorted(results, key=lambda r: r["timestamp_ms"])
    timestamps = [r["timestamp_ms"] for r in sorted_results]

    per_variant: dict[str, dict] = {}
    per_variant_per_fold: dict[str, list[float | None]] = {}
    for name, key in variants:
        returns = [r[key] for r in sorted_results]
        per_variant[name] = {
            "sharpe": sharpe_ratio(returns),
            "n_trades": sum(1 for x in returns if abs(x) > 1e-9),
            "sum_r": sum(returns),
        }
        per_variant_per_fold[name] = kfold_sharpes(returns, timestamps, k=6)

    # Pick the winning variant by total R (matches headline P/L)
    winner_name = max(variants, key=lambda v: per_variant[v[0]]["sum_r"])[0]
    winner_key = dict(variants)[winner_name]
    winner_returns = [r[winner_key] for r in sorted_results]
    # Variance of the per-variant SRs across the N trials, for the DSR's
    # null-threshold computation. Tighter than the conservative default of 1.0.
    trial_srs = [v["sharpe"] for v in per_variant.values() if v["sharpe"] is not None]
    if len(trial_srs) >= 2:
        m = sum(trial_srs) / len(trial_srs)
        var_sr = sum((s - m) ** 2 for s in trial_srs) / (len(trial_srs) - 1)
    else:
        var_sr = 1.0

    # DSR at N_trials = N_variants (default) and at a more honest larger N
    # to show how the metric tightens as you account for more comparisons.
    dsr_4 = deflated_sharpe_ratio(winner_returns, n_trials=len(variants),
                                  variance_of_sr_trials=var_sr)
    dsr_honest = deflated_sharpe_ratio(winner_returns, n_trials=20,
                                       variance_of_sr_trials=var_sr)

    pbo = probability_of_backtest_overfitting(per_variant_per_fold)

    return {
        "per_variant_sharpe": per_variant,
        "per_variant_per_fold_sharpes": {
            name: [round(s, 3) if s is not None else None for s in folds]
            for name, folds in per_variant_per_fold.items()
        },
        "winner": winner_name,
        "dsr_at_n_variants": dsr_4,
        "dsr_at_20_trials": dsr_honest,
        "pbo": pbo,
    }


def format_report(result: dict, dollars_per_r: float = DOLLARS_PER_R) -> str:
    """Markdown report string."""
    lines: list[str] = []
    lines.append(f"## Ablation Backtest: {result['date_from']} to {result['date_to']}")
    lines.append("")
    lines.append(f"- Fired signals analyzed: **{result['n_signals']}**")
    lines.append(f"- Blocked by direction filter: {result['n_blocked_by_dir_filter']}")
    lines.append(f"- B+C+D exit reasons: {result['bcd_exit_reasons']}")
    lines.append("")
    lines.append("### Aggregate R-unit P/L by variant")
    lines.append("")
    lines.append("| Variant | Date | n | sum R | W | L | flat | WR | avg R |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for name, agg in result["aggregates"].items():
        for date in sorted(agg.keys()):
            v = agg[date]
            wr = (v["wins"] / (v["wins"] + v["losses"]) * 100) if (v["wins"] + v["losses"]) else 0
            avg = v["sum_r"] / v["n"] if v["n"] else 0
            lines.append(
                f"| {name} | {date} | {v['n']} | {v['sum_r']:+.2f} | {v['wins']} | {v['losses']} | {v['flat']} | {wr:.1f}% | {avg:+.3f} |"
            )
    lines.append("")
    lines.append(f"### Dollar P/L (${dollars_per_r:.0f}/R, ALL trades)")
    lines.append("")
    lines.append("| Variant | Sum R | $ Total | $/trade |")
    lines.append("|---|---:|---:|---:|")
    for name, agg in result["aggregates"].items():
        v = agg.get("ALL", {"n": 0, "sum_r": 0.0})
        per_trade = v["sum_r"] * dollars_per_r / v["n"] if v["n"] else 0
        lines.append(
            f"| {name} | {v['sum_r']:+.2f} | {v['sum_r'] * dollars_per_r:+,.0f} | {per_trade:+,.2f} |"
        )

    # Per-regime breakout — shows whether different variants win in different
    # regime conditions (e.g., B+C+D wins in CHOP but loses in TREND_DAY).
    by_regime = result.get("aggregates_by_regime") or {}
    if by_regime:
        # Collect all regime labels present across the variants
        all_regimes: set[str] = set()
        for agg in by_regime.values():
            all_regimes.update(k for k in agg.keys() if k != "ALL")
        if all_regimes:
            lines.append("")
            lines.append("### Per-regime breakout")
            lines.append("")
            lines.append(
                "Same configs evaluated separately per regime_at_fire. Watch for "
                "the same variant winning in one regime and losing in another — "
                "that's evidence the right answer is regime-conditional gating, "
                "not a single universal exit/entry filter."
            )
            lines.append("")
            lines.append("| Variant | Regime | n | sum R | WR | avg R |")
            lines.append("|---|---|---:|---:|---:|---:|")
            for name, agg in by_regime.items():
                for regime in sorted(all_regimes):
                    v = agg.get(regime)
                    if not v or v["n"] == 0:
                        continue
                    wr = (v["wins"] / (v["wins"] + v["losses"]) * 100) if (v["wins"] + v["losses"]) else 0
                    avg = v["sum_r"] / v["n"] if v["n"] else 0
                    lines.append(
                        f"| {name} | {regime} | {v['n']} | {v['sum_r']:+.2f} | {wr:.1f}% | {avg:+.3f} |"
                    )

    # Methodology stats — DSR + purged K-fold Sharpe + PBO. The honest
    # answer to "is the apparent winner real edge or selection bias?"
    ms = result.get("methodology_stats") or {}
    if ms:
        lines.append("")
        lines.append("### Methodology stats (DSR + purged K-fold + PBO)")
        lines.append("")
        lines.append(
            "Tools from López de Prado's *Advances in Financial Machine Learning*. "
            "**DSR** = Deflated Sharpe Ratio: probability the winner's edge is real "
            "(not selection bias from testing N variants). **K-fold Sharpe** = "
            "per-variant Sharpe in each chronological fold (purged with 60-min "
            "embargo to prevent label leakage). **PBO** = Probability of Backtest "
            "Overfitting (Bailey & López de Prado 2014): fraction of cross-period "
            "pairs where the best-on-period-A variant ranks in the bottom half "
            "on period B. PBO close to 0 = winners survive cross-period; PBO ≥ 0.5 "
            "= apparent winners don't generalize."
        )
        lines.append("")
        winner = ms.get("winner")
        dsr4 = ms.get("dsr_at_n_variants") or {}
        dsr20 = ms.get("dsr_at_20_trials") or {}
        pbo = ms.get("pbo") or {}
        lines.append(f"- **Winning variant**: `{winner}`")
        if dsr4:
            lines.append(
                f"- **DSR @ N_trials={dsr4.get('n_trials', '?')}** "
                f"(this ablation only): `{dsr4.get('deflated_sharpe_prob', 'n/a')}`  "
                f"(observed SR={dsr4.get('observed_sharpe', '?')}, "
                f"null threshold={dsr4.get('sr_zero_threshold', '?')})"
            )
        if dsr20:
            lines.append(
                f"- **DSR @ N_trials=20** (honest count including adjacent "
                f"analyses): `{dsr20.get('deflated_sharpe_prob', 'n/a')}`"
            )
        if pbo and pbo.get("pbo") is not None:
            interp = pbo.get("interpretation")
            interp_text = interp[0] if isinstance(interp, tuple) else interp
            lines.append(
                f"- **PBO**: `{pbo['pbo']}` across "
                f"{pbo.get('n_pairs_evaluated', '?')} cross-period pairs "
                f"({pbo.get('k_folds', '?')} folds × {pbo.get('n_variants', '?')} variants) "
                f"— {interp_text}"
            )
        lines.append("")
        lines.append("#### Per-variant per-fold Sharpe (chronological, purged 60-min embargo)")
        lines.append("")
        per_fold = ms.get("per_variant_per_fold_sharpes") or {}
        if per_fold:
            n_folds = max((len(v) for v in per_fold.values()), default=0)
            header = ["Variant"] + [f"Fold {i+1}" for i in range(n_folds)]
            lines.append("| " + " | ".join(header) + " |")
            lines.append("|---|" + "|".join(["---:"] * n_folds) + "|")
            for vname, folds in per_fold.items():
                cells = [vname]
                for s in folds:
                    cells.append(f"{s:+.3f}" if s is not None else "—")
                lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
        lines.append(
            "_DSR < 0.5 = apparent edge is probably selection bias. "
            "DSR > 0.95 = passes formal hypothesis test as real edge. "
            "K-fold Sharpes that consistently agree on sign = stable variant; "
            "wildly varying signs = noise-fit._"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Ablation backtest for FEF post-freeze candidates.")
    ap.add_argument("date_from", help="YYYY-MM-DD")
    ap.add_argument("date_to", help="YYYY-MM-DD")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--account-class", default="paper",
                    help="Filter by extras.account_class. 'paper' (default), 'live', 'mock', "
                         "'unknown', 'all', or comma-separated.")
    ap.add_argument("--json", action="store_true", help="Emit raw JSON instead of markdown")
    ap.add_argument("--dollars-per-r", type=float, default=DOLLARS_PER_R)
    args = ap.parse_args()

    accept = _parse_account_class_arg(args.account_class)
    result = run_ablation(args.date_from, args.date_to, args.db, accept)
    # Windows cp1252 stdout chokes on extended chars in the markdown.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    if args.json:
        # Drop the per-trade list for stdout JSON brevity unless asked
        print(json.dumps({k: v for k, v in result.items() if k != "trades"}, indent=2, default=str))
    else:
        print(format_report(result, args.dollars_per_r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
