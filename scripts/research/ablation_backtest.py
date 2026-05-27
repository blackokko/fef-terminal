"""Ablation backtest: Baseline vs Direction-filter vs B+C+D vs Both vs Conjunction_req_2.

Replays fired LONG_CALLS / LONG_PUTS signals against forward SPX 1-min
bars to estimate which post-freeze candidate change has the largest
realistic-fill P/L lift.

Variants (P1 unification 2026-05-21: all variants now share order-
preserving 1-min bar walk + 50/30/20 partial scale-out; only the
trail-exit logic differs between Baseline and B+C+D).
  - Baseline: bar walk with stop at -1R, partials at T1/T2/T3 (+1R/2R/3R),
    timeout at 60min. No peak-trail. Methodology-consistent with
    daily_review dedup_expectancy_r + per_regime_sweep v2 + v2
    realistic-fill estimator.
  - Direction filter (v1): block PUTS when 30m SPX change > +3pt;
    block CALLS when 30m SPX change < -3pt. Otherwise = baseline.
  - B+C+D: baseline + peak-trail exit at 15% pullback from peak (after
    peak >= 0.3R floor) on the residual position. Uses same 50/30/20
    partial scale-out as baseline — trail is the ONLY knob being
    measured against baseline.
  - Both: direction filter + B+C+D.
  - Conjunction_req_2 (only): baseline exit + counter-bias verdicts demoted
    to WAIT unless >=2 of {alt_30m, alt_4h, microstructure, aggression}
    agree with verdict direction. Graduated 2026-05-26 from
    shadow_signal_replay (+54R top result over 5/19-5/26).
  - Conjunction_req_2 + B+C+D: B+C+D exit + conjunction_req_2 verdict
    filter. The most likely live-deployment composite.

Limitations:
  - 1R = per-signal |entry - stop| in SPX points (canonical post-2026-05-22
    per Chat audit Priority 3). Matches daily_review + realistic_fill_estimator.
    Falls back to 10pt only for legacy rows with missing extras.stop.
  - All variants use SPX moves as proxy for option-price moves (linear, no
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
DOLLARS_PER_R = 500.0  # 10 contracts × $50/pt × 1R (10pt SPX) = $500/R/contract

# Canonical R-unit helper (Chat audit Priority 3 — 2026-05-22).
# R is the per-signal SPX-point distance from entry to stop, NOT a hardcoded
# 10pt. Matches daily_review.py and realistic_fill_estimator's convention.
# Falls back to 10pt only when entry/stop are missing on a legacy row.
import sys as _sys  # noqa: E402 (intentional for path injection)
from pathlib import Path as _Path  # noqa: E402

_sys.path.insert(0, str(_Path(__file__).parent))
from _r_unit import compute_r_unit  # noqa: E402

# Canonical actionable-signals filter (Chat audit Priority 10 — 2026-05-22).
# Single source of truth for direction != WAIT + paper-only + price band.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "sidecar" / "src"))
from fef_sidecar.db.repository import build_actionable_signals_sql  # noqa: E402

# Conjunction_req_2 verdict filter (graduated 2026-05-26 from
# shadow_signal_replay.py per report 21). Top shadow result was +54R across
# 5/19-5/26; we now subject it to the full DSR / PBO / CPCV / sequential
# bootstrap methodology stack. Reuse the canonical helpers from the shadow
# script rather than re-implementing — keeps a single source of truth.
from shadow_signal_replay import (  # noqa: E402
    _conjunction_req_2_re_verdict,
)


def _ts_to_ms(ts_str: str) -> int:
    dt = datetime.fromisoformat(ts_str.replace(" ", "T"))
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _simulate_baseline(
    entry_spx: float, direction_sign: int, fwd_bars: list[dict],
    risk_pts: float = 10.0,
) -> tuple[float, str]:
    """Order-preserving 1-min bar walk WITH 50/30/20 partial scale-out at
    T1/T2/T3 (±1R/2R/3R from entry). Stops residual at −1R. No peak-trail
    (that's B+C+D's variant).

    `risk_pts` is the per-signal R-unit in SPX points (= |entry - stop|).
    Canonical post-2026-05-22 (Chat audit Priority 3): pass the engine's
    actual risk per signal so R numbers match daily_review and
    realistic_fill_estimator. Defaults to 10pt only for callers that
    haven't been migrated.

    This is the methodology-consistent baseline (matches v2 realistic-fill
    estimator + per_regime_sweep v2). Replaces the previous lookahead-flag
    scoring (T1-anywhere-in-60min = +1R) which overcounted wins on fast-
    reversal bars and produced a +30R methodology gap vs daily_review's
    bar-walk-based dedup_expectancy_r.

    Returns (realized_R, exit_reason)."""
    if not fwd_bars:
        return 0.0, "no_data"
    realized_r = 0.0
    remaining_share = 1.0
    tiers_fired: set[str] = set()
    T1_SHARE, T2_SHARE, T3_SHARE = 0.5, 0.3, 0.2
    for b in fwd_bars:
        adverse_pts = b["low"] if direction_sign > 0 else b["high"]
        favor_pts = b["high"] if direction_sign > 0 else b["low"]
        adverse_r = direction_sign * (adverse_pts - entry_spx) / risk_pts
        favor_r = direction_sign * (favor_pts - entry_spx) / risk_pts
        # Stop priority on ambiguous bars (conservative — same convention
        # as per_regime_sweep v2 and v2 realistic-fill estimator).
        if adverse_r <= -1.0 and remaining_share > 0:
            realized_r += remaining_share * -1.0
            return realized_r, "stop"
        # Tier checks in order
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
            return realized_r, "t1t2t3"
    # Timeout — exit residual at last close
    if remaining_share > 0:
        final_r = direction_sign * (fwd_bars[-1]["close"] - entry_spx) / risk_pts
        realized_r += remaining_share * final_r
    return realized_r, "timeout"


def _simulate_bcd(
    entry_spx: float, direction_sign: int, fwd_bars: list[dict],
    risk_pts: float = 10.0,
) -> tuple[float, str]:
    """B+C+D variant of baseline: same bar walk + 50/30/20 partial scale-out
    (identical to baseline), PLUS peak-trail exit at 15% pullback from peak
    (after peak >= 0.3R) on the residual position. This isolates the trail
    as the ONLY difference vs baseline — previous version also changed T1
    from partial to hard exit, confounding the two variables.

    `risk_pts` is the per-signal R-unit in SPX points (Chat audit Priority 3
    — 2026-05-22). Pass |entry - stop| so R numbers match daily_review.

    Returns (realized_R, exit_reason)."""
    if not fwd_bars:
        return 0.0, "no_data"
    realized_r = 0.0
    remaining_share = 1.0
    tiers_fired: set[str] = set()
    T1_SHARE, T2_SHARE, T3_SHARE = 0.5, 0.3, 0.2
    peak_r = 0.0
    for b in fwd_bars:
        adverse_pts = b["low"] if direction_sign > 0 else b["high"]
        favor_pts = b["high"] if direction_sign > 0 else b["low"]
        adverse_r = direction_sign * (adverse_pts - entry_spx) / risk_pts
        favor_r = direction_sign * (favor_pts - entry_spx) / risk_pts
        # Stop priority on ambiguous bars (conservative — same as baseline)
        if adverse_r <= -1.0 and remaining_share > 0:
            realized_r += remaining_share * -1.0
            return realized_r, "stop"
        # Tier checks in order (identical to baseline)
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
            return realized_r, "t1t2t3"
        # Peak-trail on residual position (the ONLY knob vs baseline)
        if favor_r > peak_r:
            peak_r = favor_r
        if peak_r >= 0.3 and remaining_share > 0:
            current_r = direction_sign * (b["close"] - entry_spx) / risk_pts
            if current_r <= peak_r - 0.15:
                realized_r += remaining_share * (peak_r - 0.15)
                return realized_r, "trail"
    # Timeout — exit residual at last close
    if remaining_share > 0:
        final_r = direction_sign * (fwd_bars[-1]["close"] - entry_spx) / risk_pts
        realized_r += remaining_share * final_r
    return realized_r, "timeout"


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

    # Canonical actionable filter (Chat audit Priority 10 — 2026-05-22).
    # paper_only flag only fires the SQL filter when the CLI requested
    # paper-only; otherwise the post-query Python filter handles 'live',
    # 'mock', 'all' or comma-separated mixes (the helper doesn't yet
    # support arbitrary class sets — paper-or-unknown is the default canonical).
    paper_only = accept == {"paper"} or accept == {"paper", "unknown"}
    sql, params = build_actionable_signals_sql(
        date_from=date_from, date_to=date_to,
        paper_only=paper_only,
        price_band=(1000.0, 10000.0),
        require_lookahead=False,  # this script applies its own lookahead join below
        # Conjunction_req_2 needs drift_bias, confidence, aggression_call_pct,
        # aggression_put_pct in addition to the prior baseline columns. Pull
        # all of them so the per-signal verdict-re-evaluation can run.
        select_clause=(
            "s.id, s.session_date, s.timestamp, s.direction, s.spx_price, "
            "s.drift_bias, s.confidence, s.aggression_call_pct, "
            "s.aggression_put_pct, s.extras"
        ),
    )
    raw_signals = cur.execute(sql, params).fetchall()
    if paper_only:
        signals = list(raw_signals)
    else:
        signals = [s for s in raw_signals if _passes_account_filter(s["extras"], accept)]

    # Cross-check the canonical count vs daily_review.actionable_signals
    # for the same range. Used as a sanity assertion at end of run_ablation
    # (Chat audit Priority 10 — 2026-05-22). Note: daily_review counts ALL
    # actionable signals (no price-band filter), so this helper's filtered
    # count can be lower if any spx_price falls outside the band.
    canonical_count = len(signals)
    daily_review_count = cur.execute(
        "SELECT COALESCE(SUM(actionable_signals), 0) FROM daily_review "
        "WHERE session_date BETWEEN ? AND ?",
        (date_from, date_to),
    ).fetchone()[0]
    daily_session_count = cur.execute(
        "SELECT COALESCE(SUM(actionable_signals), 0) FROM daily_session "
        "WHERE session_date BETWEEN ? AND ?",
        (date_from, date_to),
    ).fetchone()[0]

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

    # Optional pre/post-migration sanity check (Chat audit Priority 3 —
    # 2026-05-22). Captures first 5 signals' (old_10pt_R, new_per_signal_R)
    # so callers can SEE the migration didn't silently change all numbers.
    migration_sample: list[dict] = []

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

        # Per-signal R unit (|entry - stop|). Canonical post-2026-05-22 per
        # Chat audit Priority 3. The engine's actual risk per signal — not
        # a hardcoded 10pt. Falls back to 10pt only if extras.stop is missing.
        try:
            ex = json.loads(s["extras"]) if s["extras"] else {}
        except (json.JSONDecodeError, TypeError):
            ex = {}
        stop_spx = ex.get("stop") if isinstance(ex, dict) else None
        risk_pts = compute_r_unit(entry_spx, stop_spx, default_pts=10.0)

        prior = bars_before(sig_ms, 30)
        spx_30m_change = (prior[-1]["close"] - prior[0]["close"]) if len(prior) >= 5 else 0.0
        blocked = (direction_sign > 0 and spx_30m_change < -3.0) or (
            direction_sign < 0 and spx_30m_change > 3.0
        )

        # Conjunction_req_2 verdict filter (graduated 2026-05-26 from
        # shadow_signal_replay.py per report 21). Counter-bias verdicts get
        # demoted to WAIT unless >=2 confirming gates from {alt_30m, alt_4h,
        # microstructure, aggression} agree with verdict direction. Aligned
        # verdicts are always kept. The helper expects a signal_meta dict
        # with direction / drift_bias / aggression_*_pct keys, plus a
        # separate `extras` dict for alt_direction_* and microstructure_*.
        signal_meta = {
            "direction": s["direction"],
            "drift_bias": s["drift_bias"],
            "confidence": s["confidence"],
            "aggression_call_pct": s["aggression_call_pct"],
            "aggression_put_pct": s["aggression_put_pct"],
        }
        try:
            conj_demoted = (
                _conjunction_req_2_re_verdict(ex, signal_meta) == "WAIT"
            )
        except Exception:
            # Defensive: bad extras / missing fields shouldn't break the
            # whole ablation. Treat as "not demoted" (i.e., variant matches
            # baseline for this row). Matches shadow_signal_replay.apply_variant
            # error handling — see line ~609.
            conj_demoted = False

        # P1 (2026-05-21): switched baseline + direction-filter from
        # lookahead-flag scoring (T1-anywhere-in-60min = +1R) to
        # order-preserving bar walk + 50/30/20 partial scale-out. Lookahead
        # overcounted T1 hits on fast-reversal bars (intra-bar dip to stop
        # then rip through T1) and produced a +30R aggregate gap vs
        # daily_review's bar-walk-based dedup_expectancy_r. The two
        # variants below now share methodology with B+C+D / per_regime_sweep
        # v2 / realistic_fill_estimator v2 — only the trail-exit logic
        # differs between baseline and B+C+D.
        baseline_r, baseline_reason = _simulate_baseline(
            entry_spx, direction_sign, fwd, risk_pts=risk_pts,
        )
        bcd_r, bcd_reason = _simulate_bcd(
            entry_spx, direction_sign, fwd, risk_pts=risk_pts,
        )

        # Capture first 5 signals' before/after for sanity output (Chat
        # audit Priority 3 — 2026-05-22).
        if len(migration_sample) < 5:
            legacy_baseline_r, _ = _simulate_baseline(
                entry_spx, direction_sign, fwd, risk_pts=10.0,
            )
            legacy_bcd_r, _ = _simulate_bcd(
                entry_spx, direction_sign, fwd, risk_pts=10.0,
            )
            migration_sample.append({
                "sid": sid, "risk_pts_new": round(risk_pts, 2),
                "risk_pts_legacy": 10.0,
                "baseline_r_legacy": round(legacy_baseline_r, 3),
                "baseline_r_new": round(baseline_r, 3),
                "bcd_r_legacy": round(legacy_bcd_r, 3),
                "bcd_r_new": round(bcd_r, 3),
            })

        results.append({
            "sid": sid, "date": s["session_date"], "dir": s["direction"],
            "timestamp_ms": sig_ms,
            "regime": _regime_from_extras(s["extras"]),
            "entry_spx": entry_spx, "spx_30m_change": round(spx_30m_change, 2),
            "risk_pts": round(risk_pts, 2),
            "baseline_r": baseline_r,
            "direction_filter_r": 0.0 if blocked else baseline_r,
            "bcd_r": bcd_r,
            "combined_r": 0.0 if blocked else bcd_r,
            # Conjunction_req_2 (only) = baseline exit + verdict filter.
            # Isolates the direction-fix's marginal contribution.
            "conj_only_r": 0.0 if conj_demoted else baseline_r,
            # Conjunction_req_2 + B+C+D = B+C+D exit + verdict filter.
            # Most likely live-deployment composite.
            "conj_bcd_r": 0.0 if conj_demoted else bcd_r,
            "blocked_by_dir_filter": blocked,
            "blocked_by_conjunction_req_2": conj_demoted,
            "baseline_reason": baseline_reason,
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
        # Graduated 2026-05-26 from shadow_signal_replay.py per report 21.
        # See VARIANT_LEDGER.md → direction_filter family + composite family.
        ("Conjunction_req_2 only", "conj_only_r"),
        ("Conjunction_req_2 + B+C+D", "conj_bcd_r"),
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
        "n_blocked_by_conjunction_req_2": sum(
            1 for r in results if r["blocked_by_conjunction_req_2"]
        ),
        "baseline_exit_reasons": dict(Counter(r["baseline_reason"] for r in results)),
        "bcd_exit_reasons": dict(Counter(r["bcd_reason"] for r in results)),
        "aggregates": aggregates,
        "aggregates_by_regime": aggregates_by_regime,
        "methodology_stats": methodology_stats,
        "r_unit_migration_sample": migration_sample,
        "actionable_count_check": {
            "canonical_with_price_band": canonical_count,
            "daily_review_sum": int(daily_review_count or 0),
            "daily_session_sum": int(daily_session_count or 0),
            "lookahead_join_n": len(results),
        },
        "trades": results,
    }


def _methodology_stats(results: list[dict], variants: list[tuple[str, str]]) -> dict:
    """Compute DSR + per-fold Sharpe + PBO across the ablation variants.
    Returns a dict ready to format in the markdown report."""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).parent))
    from _stats import (
        _median_trade_duration_min,
        cpcv_paths,
        deflated_sharpe_ratio,
        dsr_at_effective_n,
        get_ledger_variant_count,
        kfold_sharpes,
        probability_of_backtest_overfitting,
        sharpe_distribution_sequential,
        sharpe_ratio,
        stratified_pbo_by_regime,
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

    # DSR at multiple N_trials counts to surface survivorship bias:
    #   1. N_trials = N_variants in this ablation (4) — the "as-tested" view.
    #      Honest only for the immediate comparison; ignores prior trials.
    #   2. N_trials = SQL `variant_ledger` row count (12 today) — covers
    #      registered shadow variants only. Still survivorship-biased
    #      because DROPPED / SUPERSEDED variants never got a SQL row.
    #   3. N_trials = honest project-wide count from
    #      `scripts/research/VARIANT_LEDGER.md` (58 today). Per Harvey &
    #      Liu 2015: this is the only valid number for any project-level
    #      claim about strategy edge. Includes every ACTIVE / SHADOW /
    #      DROPPED / SUPERSEDED variant the project has ever tested.
    ledger_counts = get_ledger_variant_count(family="exit_logic")
    # SQL-derived counts (legacy — registered shadow variants only).
    ledger_n_project = ledger_counts.get("in_project") or 20
    ledger_n_family = ledger_counts.get("in_family") or len(variants)
    # Honest counts from VARIANT_LEDGER.md (covers DROPPED + SUPERSEDED
    # variants that never made it into the SQL table). Falls back to the
    # legacy SQL count when the markdown ledger isn't available.
    honest_n_project = ledger_counts.get("in_project_honest") or ledger_n_project
    honest_n_family = ledger_counts.get("in_family_honest") or ledger_n_family
    dsr_4 = deflated_sharpe_ratio(winner_returns, n_trials=len(variants),
                                  variance_of_sr_trials=var_sr)
    dsr_honest = deflated_sharpe_ratio(winner_returns, n_trials=ledger_n_project,
                                       variance_of_sr_trials=var_sr)
    dsr_family = deflated_sharpe_ratio(winner_returns, n_trials=ledger_n_family,
                                       variance_of_sr_trials=var_sr)
    dsr_honest_project = deflated_sharpe_ratio(
        winner_returns, n_trials=honest_n_project, variance_of_sr_trials=var_sr,
    )
    dsr_honest_family = deflated_sharpe_ratio(
        winner_returns, n_trials=honest_n_family, variance_of_sr_trials=var_sr,
    )

    pbo = probability_of_backtest_overfitting(per_variant_per_fold)

    # CPCV — combinatorial paths per variant. With n_groups=6, test=2 we
    # get C(6,2)=15 paths per variant. Each path's Sharpe is independent
    # so we can report mean ± std + how often it's positive.
    cpcv_by_variant: dict[str, dict] = {}
    for name, key in variants:
        returns_v = [r[key] for r in sorted_results]
        cpcv_by_variant[name] = cpcv_paths(returns_v, timestamps,
                                           n_groups=6, test_groups=2)

    # Stratified CPCV (per-regime) — addresses the regime-imbalance failure
    # mode that the aggregate CPCV above suffers from (Audit priority #3,
    # 2026-05-22). When one regime dominates the sample (TREND_DAY=143/268)
    # and another tiny subset (POWER_HOUR=12) is the only profitable one,
    # aggregate CPCV shows "100% positive paths" because POWER_HOUR trades
    # leak into every path via autocorrelation. Stratifying CPCV within
    # each regime gives regime-conditional Sharpe distributions you can
    # actually trust.
    #
    # Rules:
    #   - n >= 20 within regime: run CPCV (flag n < 30 as borderline)
    #   - n < 20: SKIP and emit an undersampled-flag entry
    # Aggregate CPCV is kept (with a warning) for backwards compatibility.
    stratified_cpcv: dict[str, dict] = _stratified_cpcv_by_regime(
        sorted_results, variants, cpcv_paths_fn=cpcv_paths,
    )

    # Sequential Bootstrap (López de Prado AFML ch. 4) — for the winning
    # variant. Our signals have overlapping 60-min outcome windows; classical
    # bootstrap treats them as independent and underestimates SE. Sequential
    # bootstrap downweights samples whose label windows overlap with already-
    # drawn samples, preserving effective sample size. We expect a larger
    # bootstrap SR std than classical (the inflation factor surfaces the
    # gap directly).
    #
    # Audit priority #7 (2026-05-22): hardcoded label_horizon_min=60 was a
    # residual research-script default from when 60-min was the lookahead
    # window. The honest horizon is the median observed trade duration,
    # since that's the window over which one signal's outcome competes
    # with the next signal's outcome for the same minute-bars. We now
    # ship BOTH the legacy 60-min version (for backwards comparison) AND
    # the calibrated median-duration version (the honest one).
    seq_boot = sharpe_distribution_sequential(
        winner_returns,
        signal_start_times_ms=timestamps,
        n_bootstraps=500,  # AFML default = 1000; 500 keeps runtime reasonable.
        label_horizon_minutes=60,
        seed=42,
    )

    # Calibrated horizon: median observed trade duration from live_order.
    # Falls back to 60 if no live_order data (e.g., fresh install, all-paper).
    median_trade_min = _median_trade_duration_min(sorted_results)
    if median_trade_min is not None and median_trade_min >= 1:
        calibrated_horizon = int(round(median_trade_min))
    else:
        calibrated_horizon = 60
    if calibrated_horizon != 60:
        seq_boot_calibrated = sharpe_distribution_sequential(
            winner_returns,
            signal_start_times_ms=timestamps,
            n_bootstraps=500,
            label_horizon_min=calibrated_horizon,
            seed=42,
        )
    else:
        # Skip the redundant second call when calibrated == 60.
        seq_boot_calibrated = {
            **seq_boot,
            "note": "calibrated horizon = 60 (same as legacy); no second run",
        }

    # Stratified PBO (per-regime) — Audit priority #6 (2026-05-22). The
    # aggregate PBO above pools variant ranks across folds that may be
    # regime-imbalanced (one fold all TREND_DAY, another all CHOP). The
    # stratified version runs PBO separately within each regime where
    # n >= 20, surfacing whether the winner survives cross-period within
    # the same regime. Same threshold pattern as stratified CPCV.
    stratified_pbo: dict[str, dict] = stratified_pbo_by_regime(
        sorted_results,
        variants,
        kfold_sharpes_fn=kfold_sharpes,
        pbo_fn=probability_of_backtest_overfitting,
        min_n=STRATIFIED_CPCV_MIN_N,
        borderline_n=STRATIFIED_CPCV_BORDERLINE_N,
        k_folds=6,
    )

    # Correlation-aware DSR (Audit priority #9, 2026-05-22). Harvey & Liu
    # 2015 §5 — N_trials must be adjusted for non-independence of the
    # variant trials. The variants share the same underlying signal stream
    # and exit logic, so their per-trade returns are highly correlated.
    # N_eff = N * (1 - mean_off_diagonal_correlation) is the effective
    # independent count of trials. Pass that to deflated_sharpe_ratio in
    # place of the raw N_honest_project (58).
    per_variant_returns_seq = {
        name: [r[key] for r in sorted_results] for name, key in variants
    }
    dsr_effective = dsr_at_effective_n(
        winner_returns=winner_returns,
        per_variant_returns=per_variant_returns_seq,
        n_honest=honest_n_project,
        variance_of_sr_trials=var_sr,
    )

    # Conjunction_req_2 spotlight (graduated 2026-05-26 from
    # shadow_signal_replay.py per report 21). The auto-winner pipeline above
    # runs DSR / sequential bootstrap on whichever variant has the highest
    # sum R. For this graduation we ALSO want the answers specifically for
    # the conjunction_req_2 variants (both standalone and composite with
    # B+C+D) so the report can speak directly to the dispositive questions
    # report 22 frames: DSR @ N_eff, stratified PBO, CPCV % positive, and
    # sequential bootstrap CI. The variant-spotlight dict below feeds the
    # TLDR section of format_report.
    spotlight_names = ["Conjunction_req_2 only", "Conjunction_req_2 + B+C+D"]
    variant_spotlights: dict[str, dict] = {}
    for spot_name in spotlight_names:
        if spot_name not in per_variant_returns_seq:
            continue
        spot_returns = per_variant_returns_seq[spot_name]
        # DSR ladder for THIS variant (not the auto-winner). Use the same
        # variance_of_sr_trials so the null threshold is consistent across
        # spotlights and the headline DSR.
        spot_dsr_n_eff = dsr_at_effective_n(
            winner_returns=spot_returns,
            per_variant_returns=per_variant_returns_seq,
            n_honest=honest_n_project,
            variance_of_sr_trials=var_sr,
        )
        # Sequential bootstrap at calibrated horizon (median observed trade
        # duration). When calibrated == 60 we reuse the legacy result, same
        # as the auto-winner path above.
        if calibrated_horizon != 60:
            spot_seq = sharpe_distribution_sequential(
                spot_returns,
                signal_start_times_ms=timestamps,
                n_bootstraps=500,
                label_horizon_min=calibrated_horizon,
                seed=42,
            )
        else:
            spot_seq = sharpe_distribution_sequential(
                spot_returns,
                signal_start_times_ms=timestamps,
                n_bootstraps=500,
                label_horizon_minutes=60,
                seed=42,
            )
        # Aggregate CPCV (15 paths) for this variant — pull from the same
        # cpcv_by_variant dict already populated above.
        spot_cpcv = cpcv_by_variant.get(spot_name) or {}
        # Stratified CPCV % positive per regime (n >= 20) for this variant.
        spot_strat_cpcv: dict[str, dict] = {}
        for regime, entry in stratified_cpcv.items():
            if entry.get("skipped"):
                continue
            by_var = entry.get("by_variant") or {}
            if spot_name not in by_var:
                continue
            v_data = by_var[spot_name]
            spot_strat_cpcv[regime] = {
                "n": entry.get("n"),
                "sharpe_mean": v_data.get("sharpe_mean"),
                "sharpe_std": v_data.get("sharpe_std"),
                "sharpe_positive_pct": v_data.get("sharpe_positive_pct"),
                "borderline": entry.get("borderline"),
            }
        # Stratified PBO per regime (n >= 20) for this variant — extract
        # the variant's per-fold rank from the regime's per_variant_per_fold
        # if available (this requires running PBO per-variant... but the
        # current stratified_pbo aggregates across all variants. The PBO
        # per-regime value applies to the full variant set, so it doesn't
        # change per-variant. Surface the variant's per-fold Sharpe within
        # each regime instead.)
        per_variant_per_fold_in_regime: dict[str, list[float | None]] = {}
        for regime, entry in stratified_pbo.items():
            if entry.get("skipped"):
                continue
            # The current stratified_pbo doesn't expose the
            # per_variant_per_fold ranks it computed. We re-derive them
            # cheaply by re-running kfold_sharpes for this variant only.
            rows_in_regime = [
                r for r in sorted_results if r["regime"] == regime
            ]
            if not rows_in_regime:
                continue
            ts_regime = [r["timestamp_ms"] for r in rows_in_regime]
            spot_key = dict(variants).get(spot_name)
            if not spot_key:
                continue
            returns_in_regime = [r[spot_key] for r in rows_in_regime]
            folds = kfold_sharpes(returns_in_regime, ts_regime, k=6)
            per_variant_per_fold_in_regime[regime] = folds
        variant_spotlights[spot_name] = {
            "sum_r": per_variant[spot_name]["sum_r"],
            "sharpe": per_variant[spot_name]["sharpe"],
            "n_trades": per_variant[spot_name]["n_trades"],
            "dsr_at_n_eff": spot_dsr_n_eff,
            "sequential_bootstrap_calibrated": spot_seq,
            "aggregate_cpcv": spot_cpcv,
            "stratified_cpcv": spot_strat_cpcv,
            "per_regime_per_fold_sharpe": per_variant_per_fold_in_regime,
        }

    return {
        "per_variant_sharpe": per_variant,
        "per_variant_per_fold_sharpes": {
            name: [round(s, 3) if s is not None else None for s in folds]
            for name, folds in per_variant_per_fold.items()
        },
        "winner": winner_name,
        "ledger_counts": ledger_counts,
        "dsr_at_n_variants": dsr_4,
        "dsr_at_ledger_family": dsr_family,
        "dsr_at_ledger_project": dsr_honest,
        # Honest (DROPPED + SUPERSEDED included) — the answer that
        # passes Harvey & Liu 2015 muster.
        "dsr_at_honest_family": dsr_honest_family,
        "dsr_at_honest_project": dsr_honest_project,
        # Correlation-aware effective N (Harvey & Liu 2015 §5).
        # Audit priority #9, 2026-05-22.
        "dsr_at_effective_n": dsr_effective,
        "pbo": pbo,
        "stratified_pbo": stratified_pbo,
        "cpcv": cpcv_by_variant,
        "stratified_cpcv": stratified_cpcv,
        "sequential_bootstrap": seq_boot,
        # Calibrated sequential bootstrap (Audit priority #7, 2026-05-22) —
        # median observed trade duration replaces the hardcoded 60-min.
        "sequential_bootstrap_calibrated": seq_boot_calibrated,
        "calibrated_label_horizon_min": calibrated_horizon,
        # Variant spotlight (graduated 2026-05-26 per report 22) — DSR /
        # sequential bootstrap / CPCV breakouts for specific named variants
        # (typically conjunction_req_2 only / + B+C+D) so the TLDR can
        # answer the report's dispositive questions directly even when the
        # auto-winner isn't the variant under evaluation.
        "variant_spotlights": variant_spotlights,
    }


# Stratified-CPCV thresholds. Centralised so the format_report can render
# the same flags / skips that _methodology_stats used.
STRATIFIED_CPCV_MIN_N = 20      # run CPCV only when within-regime n >= 20
STRATIFIED_CPCV_BORDERLINE_N = 30  # flag n < 30 as borderline


def _stratified_cpcv_by_regime(
    sorted_results: list[dict],
    variants: list[tuple[str, str]],
    cpcv_paths_fn,
) -> dict[str, dict]:
    """Per-regime CPCV: group sorted_results by regime_at_fire, then run
    CPCV separately on each regime with n >= STRATIFIED_CPCV_MIN_N. Returns
    a dict keyed by regime label with the per-variant CPCV stats inside.

    Schema:
        {
            "<regime>": {
                "n": int,
                "skipped": bool,
                "borderline": bool,
                "note": str | None,
                "by_variant": {
                    "<variant name>": {... cpcv_paths(...) output ...},
                    ...
                } | None,        # None when skipped
            },
            ...
        }
    """
    # Group sorted_results by regime, preserving chronological order within
    # each regime (sorted_results is already timestamp-sorted; bucket-sort
    # keeps order).
    by_regime: dict[str, list[dict]] = defaultdict(list)
    for r in sorted_results:
        by_regime[r["regime"]].append(r)

    out: dict[str, dict] = {}
    for regime, rows in sorted(by_regime.items()):
        n = len(rows)
        entry: dict = {
            "n": n,
            "skipped": False,
            "borderline": False,
            "note": None,
            "by_variant": None,
        }
        if n < STRATIFIED_CPCV_MIN_N:
            entry["skipped"] = True
            entry["note"] = (
                f"n={n} < {STRATIFIED_CPCV_MIN_N} — undersampled, CPCV skipped"
            )
            out[regime] = entry
            continue

        if n < STRATIFIED_CPCV_BORDERLINE_N:
            entry["borderline"] = True
            entry["note"] = (
                f"n={n} < {STRATIFIED_CPCV_BORDERLINE_N} — borderline; "
                f"per-path Sharpes will be noisy"
            )

        ts_regime = [r["timestamp_ms"] for r in rows]
        per_variant_cpcv: dict[str, dict] = {}
        for name, key in variants:
            returns_v = [r[key] for r in rows]
            per_variant_cpcv[name] = cpcv_paths_fn(
                returns_v, ts_regime, n_groups=6, test_groups=2,
            )
        entry["by_variant"] = per_variant_cpcv
        out[regime] = entry
    return out


def _append_conjunction_tldr(
    lines: list[str], result: dict, dollars_per_r: float
) -> None:
    """Emit the conjunction_req_2 TLDR block (5 dispositive questions per
    report 22). Pulls per-variant DSR, sequential bootstrap CI, aggregate
    CPCV % positive, stratified CPCV per regime, and marginal contribution
    vs. B+C+D-only. Safe no-op when variant_spotlights is empty."""
    ms = result.get("methodology_stats") or {}
    spotlights = ms.get("variant_spotlights") or {}
    if not spotlights:
        return

    aggregates = result.get("aggregates") or {}
    spot_only = spotlights.get("Conjunction_req_2 only") or {}
    spot_bcd = spotlights.get("Conjunction_req_2 + B+C+D") or {}
    bcd_only_ALL = (aggregates.get("B+C+D only") or {}).get("ALL") or {
        "sum_r": 0.0, "n": 0,
    }
    spot_only_sum_r = spot_only.get("sum_r", 0.0)
    spot_bcd_sum_r = spot_bcd.get("sum_r", 0.0)
    bcd_sum_r = bcd_only_ALL.get("sum_r", 0.0)

    lines.append("## TLDR — conjunction_req_2 dispositive answers")
    lines.append("")
    lines.append(
        "Per report 22 framing. The shadow_signal_replay top result was "
        "**+54R** for `conjunction_req_2` across 5/19-5/26 (binary T1/stop "
        "scoring). Below are the same variant's numbers under the "
        "ablation_backtest methodology stack (50/30/20 bar-walk + per-signal "
        "R-unit) — the honest answer to whether the edge survives selection-"
        "bias correction."
    )
    lines.append("")

    # ---- Q1: DSR @ N_eff for conjunction_req_2 (both standalone and composite)
    dsr_only = spot_only.get("dsr_at_n_eff") or {}
    dsr_bcd = spot_bcd.get("dsr_at_n_eff") or {}
    lines.append(
        f"**Q1. DSR @ N_eff for `conjunction_req_2`:**"
    )
    if dsr_only.get("deflated_sharpe_prob") is not None:
        n_eff = dsr_only.get("n_effective")
        rho = dsr_only.get("mean_off_diagonal_correlation")
        obs_sr = dsr_only.get("observed_sharpe")
        null_thr = dsr_only.get("sr_zero_threshold")
        dsr_val = dsr_only["deflated_sharpe_prob"]
        verdict = (
            "edge likely real after selection-bias correction"
            if dsr_val > 0.50 else "borderline, more sessions needed"
        )
        lines.append(
            f"- Standalone (Baseline exit + verdict filter), DSR @ "
            f"N_eff={n_eff}: **`{dsr_val}`** "
            f"(observed SR={obs_sr}, null threshold={null_thr}, "
            f"rho_bar={rho}) — _{verdict}._"
        )
        lines.append(
            f"  Sum R = `{spot_only_sum_r:+.2f}` "
            f"({spot_only.get('n_trades', 0)} non-zero trades)"
        )
    else:
        lines.append(
            f"- DSR could not be computed for `conjunction_req_2` standalone "
            f"(observed SR or variance is degenerate)."
        )
    if dsr_bcd.get("deflated_sharpe_prob") is not None:
        n_eff = dsr_bcd.get("n_effective")
        rho = dsr_bcd.get("mean_off_diagonal_correlation")
        obs_sr = dsr_bcd.get("observed_sharpe")
        null_thr = dsr_bcd.get("sr_zero_threshold")
        dsr_val = dsr_bcd["deflated_sharpe_prob"]
        verdict = (
            "edge likely real after selection-bias correction"
            if dsr_val > 0.50 else "borderline, more sessions needed"
        )
        lines.append(
            f"- Composite (+ B+C+D exit), DSR @ N_eff={n_eff}: "
            f"**`{dsr_val}`** (observed SR={obs_sr}, null "
            f"threshold={null_thr}, rho_bar={rho}) — _{verdict}._"
        )
        lines.append(
            f"  Sum R = `{spot_bcd_sum_r:+.2f}` "
            f"({spot_bcd.get('n_trades', 0)} non-zero trades)"
        )
    lines.append("")

    # ---- Q2: Stratified PBO — particularly TREND_DAY
    lines.append(
        "**Q2. Stratified PBO for `conjunction_req_2` (especially TREND_DAY):**"
    )
    strat_pbo = ms.get("stratified_pbo") or {}
    if strat_pbo:
        lines.append(
            "  - The full Stratified PBO table below shows PBO across all "
            "variants in each regime (PBO is a set-level diagnostic — it "
            "tells you whether ANY variant's apparent ranking on one slice "
            "survives the next slice). Below: per-regime PBO values."
        )
        for regime, entry in sorted(strat_pbo.items()):
            if entry.get("skipped"):
                continue
            p = entry.get("pbo") or {}
            pbo_v = p.get("pbo")
            interp = p.get("interpretation")
            interp_text = interp[0] if isinstance(interp, tuple) else (interp or "")
            n = entry.get("n", 0)
            flag = " (borderline)" if entry.get("borderline") else ""
            if pbo_v is not None:
                lines.append(
                    f"  - `{regime}` (n={n}{flag}): PBO=`{pbo_v:.4f}` — "
                    f"{interp_text}"
                )
            else:
                lines.append(
                    f"  - `{regime}` (n={n}{flag}): PBO insufficient data"
                )
    lines.append(
        "  - **Verdict**: TREND_DAY baseline-era PBO was 0.533 (high "
        "overfit). The above number is the post-direction-filter result; "
        "lower is better."
    )
    lines.append("")

    # ---- Q3: CPCV % positive paths (aggregate AND per-regime) for conj_req_2
    lines.append(
        "**Q3. CPCV % positive paths for `conjunction_req_2` "
        "(aggregate AND per-regime):**"
    )
    agg_cpcv_only = spot_only.get("aggregate_cpcv") or {}
    agg_cpcv_bcd = spot_bcd.get("aggregate_cpcv") or {}
    if agg_cpcv_only.get("sharpe_positive_pct") is not None:
        n_p = agg_cpcv_only.get("n_paths", 0)
        sr_m = agg_cpcv_only.get("sharpe_mean")
        sr_s = agg_cpcv_only.get("sharpe_std")
        pct = agg_cpcv_only.get("sharpe_positive_pct")
        lines.append(
            f"- Standalone (aggregate, {n_p} paths): mean Sharpe `{sr_m}`, "
            f"std `{sr_s}`, **% positive `{pct}%`**"
        )
    if agg_cpcv_bcd.get("sharpe_positive_pct") is not None:
        n_p = agg_cpcv_bcd.get("n_paths", 0)
        sr_m = agg_cpcv_bcd.get("sharpe_mean")
        sr_s = agg_cpcv_bcd.get("sharpe_std")
        pct = agg_cpcv_bcd.get("sharpe_positive_pct")
        lines.append(
            f"- + B+C+D (aggregate, {n_p} paths): mean Sharpe `{sr_m}`, "
            f"std `{sr_s}`, **% positive `{pct}%`**"
        )
    # Per-regime stratified CPCV for the standalone variant
    strat_cpcv_only = spot_only.get("stratified_cpcv") or {}
    if strat_cpcv_only:
        lines.append("- Per-regime stratified CPCV (standalone, n>=20):")
        for regime in sorted(strat_cpcv_only.keys()):
            r = strat_cpcv_only[regime]
            flag = " (borderline)" if r.get("borderline") else ""
            lines.append(
                f"  - `{regime}` (n={r.get('n')}{flag}): mean SR "
                f"`{r.get('sharpe_mean')}`, % positive "
                f"`{r.get('sharpe_positive_pct')}%`"
            )
    strat_cpcv_bcd = spot_bcd.get("stratified_cpcv") or {}
    if strat_cpcv_bcd:
        lines.append("- Per-regime stratified CPCV (+ B+C+D, n>=20):")
        for regime in sorted(strat_cpcv_bcd.keys()):
            r = strat_cpcv_bcd[regime]
            flag = " (borderline)" if r.get("borderline") else ""
            lines.append(
                f"  - `{regime}` (n={r.get('n')}{flag}): mean SR "
                f"`{r.get('sharpe_mean')}`, % positive "
                f"`{r.get('sharpe_positive_pct')}%`"
            )
    lines.append("")

    # ---- Q4: Marginal contribution of conjunction_req_2 + B+C+D vs B+C+D only
    delta_bcd_vs_conj_bcd = spot_bcd_sum_r - bcd_sum_r
    lines.append(
        "**Q4. Marginal contribution of `conjunction_req_2 + B+C+D` "
        "ON TOP of `B+C+D alone`:**"
    )
    lines.append(
        f"- B+C+D only sum R: `{bcd_sum_r:+.2f}` "
        f"(`{bcd_sum_r * dollars_per_r:+,.0f}` $)"
    )
    lines.append(
        f"- conjunction_req_2 + B+C+D sum R: `{spot_bcd_sum_r:+.2f}` "
        f"(`{spot_bcd_sum_r * dollars_per_r:+,.0f}` $)"
    )
    lines.append(
        f"- **Delta**: `{delta_bcd_vs_conj_bcd:+.2f}R` "
        f"(`{delta_bcd_vs_conj_bcd * dollars_per_r:+,.0f}` $) — "
        f"{'positive — direction filter is synergistic with the exit logic' if delta_bcd_vs_conj_bcd > 0 else 'negative — direction filter is DEDUCTIVE on top of B+C+D (dropped fires that B+C+D could have profited from)'}"
    )
    lines.append("")

    # ---- Q5: Sequential bootstrap CI at calibrated horizon for conj_req_2
    cal_horizon = ms.get("calibrated_label_horizon_min", 60)
    lines.append(
        f"**Q5. Sequential bootstrap 95% CI at calibrated "
        f"({cal_horizon}-min) horizon for `conjunction_req_2`:**"
    )
    sb_only = spot_only.get("sequential_bootstrap_calibrated") or {}
    sb_bcd = spot_bcd.get("sequential_bootstrap_calibrated") or {}
    if sb_only.get("sharpe_ci_95"):
        ci = sb_only["sharpe_ci_95"]
        sr_m = sb_only.get("sharpe_mean")
        sr_s = sb_only.get("sharpe_std")
        p_neg = sb_only.get("sharpe_p_negative")
        if ci[0] is not None and ci[0] > 0:
            verdict = "lower bound > 0 — keep"
        elif ci[0] is not None and ci[0] > -0.05:
            verdict = "lower bound near 0 — fragile"
        else:
            verdict = "lower bound below 0 — likely noise"
        lines.append(
            f"- Standalone: SR `{sr_m} ± {sr_s}`, "
            f"**95% CI `[{ci[0]:+.4f}, {ci[1]:+.4f}]`**, P(SR<=0)=`{p_neg}` — "
            f"_{verdict}._"
        )
    if sb_bcd.get("sharpe_ci_95"):
        ci = sb_bcd["sharpe_ci_95"]
        sr_m = sb_bcd.get("sharpe_mean")
        sr_s = sb_bcd.get("sharpe_std")
        p_neg = sb_bcd.get("sharpe_p_negative")
        if ci[0] is not None and ci[0] > 0:
            verdict = "lower bound > 0 — keep"
        elif ci[0] is not None and ci[0] > -0.05:
            verdict = "lower bound near 0 — fragile"
        else:
            verdict = "lower bound below 0 — likely noise"
        lines.append(
            f"- + B+C+D: SR `{sr_m} ± {sr_s}`, "
            f"**95% CI `[{ci[0]:+.4f}, {ci[1]:+.4f}]`**, P(SR<=0)=`{p_neg}` — "
            f"_{verdict}._"
        )
    lines.append("")

    # ---- Summary recommendation
    lines.append("### Recommendation (post-freeze deployment readiness)")
    lines.append("")
    # Pull DSR + sequential CI for the standalone and composite to drive
    # the recommendation.
    dsr_only_v = (dsr_only.get("deflated_sharpe_prob") or 0.0)
    dsr_bcd_v = (dsr_bcd.get("deflated_sharpe_prob") or 0.0)
    sb_only_ci = (sb_only.get("sharpe_ci_95") or (None, None))
    sb_bcd_ci = (sb_bcd.get("sharpe_ci_95") or (None, None))
    if (dsr_only_v > 0.50 and sb_only_ci[0] is not None and sb_only_ci[0] > 0):
        lines.append(
            "- **`conjunction_req_2` standalone**: PASSES — DSR @ N_eff "
            "above 0.50 AND sequential bootstrap lower bound > 0. Ready "
            "for post-freeze live deployment."
        )
    elif dsr_only_v > 0.50:
        lines.append(
            "- **`conjunction_req_2` standalone**: DSR @ N_eff passes but "
            "sequential bootstrap CI straddles zero — needs more "
            "sessions of validation."
        )
    else:
        # Comment on direction (composite vs standalone) when standalone fails
        # because that's the key research insight from this graduation —
        # the binary T1/stop scoring in shadow_signal_replay overstated the
        # standalone's edge.
        lines.append(
            "- **`conjunction_req_2` standalone**: DOES NOT PASS DSR @ "
            f"N_eff threshold (`{dsr_only_v}` < 0.50). The standalone "
            "direction-fix's marginal contribution under the bar-walk "
            "methodology is much smaller than the shadow-replay's binary "
            "T1/stop scoring suggested (+54R shadow → "
            f"`{spot_only_sum_r:+.2f}R` ablation). Needs more sessions "
            "of validation OR composite deployment with B+C+D."
        )
    # Composite: nuanced thresholds since 0.49 is meaningfully different
    # from 0.20 even though both are < 0.50.
    composite_borderline = 0.45 < dsr_bcd_v < 0.50
    composite_pass = dsr_bcd_v >= 0.50
    seq_pass = sb_bcd_ci[0] is not None and sb_bcd_ci[0] > 0
    if composite_pass and seq_pass:
        lines.append(
            "- **`conjunction_req_2 + B+C+D` composite**: PASSES — DSR @ "
            "N_eff above 0.50 AND sequential bootstrap lower bound > 0. "
            "This is the recommended live-deployment shape."
        )
    elif composite_borderline and seq_pass:
        lines.append(
            f"- **`conjunction_req_2 + B+C+D` composite**: BORDERLINE — "
            f"DSR @ N_eff (`{dsr_bcd_v}`) is just below the 0.50 "
            f"threshold AND sequential bootstrap lower bound > 0 "
            f"(`{sb_bcd_ci[0]:+.4f}`). The composite shows real edge "
            f"signal but at honest N_eff the headline doesn't pass the "
            f"selection-bias correction. **Marginal Δ vs B+C+D alone is "
            f"`{spot_bcd_sum_r - bcd_sum_r:+.2f}R`** — the direction "
            f"filter does not add net edge ON TOP of B+C+D's exit "
            f"improvement on this dataset. Recommend: validate B+C+D-only "
            f"first; revisit conjunction_req_2 once 2-3 more sessions of "
            f"data exist."
        )
    elif composite_pass:
        lines.append(
            "- **`conjunction_req_2 + B+C+D` composite**: DSR @ N_eff "
            "passes but sequential bootstrap CI straddles zero — needs "
            "more sessions of validation."
        )
    else:
        lines.append(
            f"- **`conjunction_req_2 + B+C+D` composite**: DOES NOT PASS "
            f"DSR @ N_eff threshold (`{dsr_bcd_v}` < 0.50). The composite "
            f"doesn't meaningfully improve over B+C+D alone (marginal Δ = "
            f"`{spot_bcd_sum_r - bcd_sum_r:+.2f}R`) — needs more sessions."
        )
    lines.append("")


def format_report(result: dict, dollars_per_r: float = DOLLARS_PER_R) -> str:
    """Markdown report string."""
    lines: list[str] = []
    lines.append(f"## Ablation Backtest: {result['date_from']} to {result['date_to']}")
    lines.append("")
    lines.append(f"- Fired signals analyzed: **{result['n_signals']}**")
    lines.append(f"- Blocked by direction filter: {result['n_blocked_by_dir_filter']}")
    lines.append(
        f"- Blocked by conjunction_req_2: "
        f"{result.get('n_blocked_by_conjunction_req_2', 0)}"
    )
    lines.append(f"- Baseline exit reasons: {result.get('baseline_exit_reasons', {})}")
    lines.append(f"- B+C+D exit reasons: {result['bcd_exit_reasons']}")
    lines.append("")

    # TLDR — conjunction_req_2 dispositive answers (report 22 framing).
    # The methodology pipeline below runs DSR / sequential bootstrap on the
    # auto-selected winner. This TLDR pulls the same per-variant numbers for
    # conjunction_req_2 (standalone and composite with B+C+D) so the report
    # can answer the dispositive questions directly even when the auto-
    # winner is a different variant.
    _append_conjunction_tldr(lines, result, dollars_per_r)
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
        dsr_family = ms.get("dsr_at_ledger_family") or {}
        dsr_project = ms.get("dsr_at_ledger_project") or {}
        dsr_honest_family = ms.get("dsr_at_honest_family") or {}
        dsr_honest_project = ms.get("dsr_at_honest_project") or {}
        ledger_counts = ms.get("ledger_counts") or {}
        pbo = ms.get("pbo") or {}
        lines.append(f"- **Winning variant**: `{winner}`")
        if dsr4:
            lines.append(
                f"- **DSR @ N_trials={dsr4.get('n_trials', '?')}** "
                f"(this ablation only): `{dsr4.get('deflated_sharpe_prob', 'n/a')}`  "
                f"(observed SR={dsr4.get('observed_sharpe', '?')}, "
                f"null threshold={dsr4.get('sr_zero_threshold', '?')})"
            )
        if dsr_family:
            family_n = dsr_family.get("n_trials", "?")
            lines.append(
                f"- **DSR @ N_trials={family_n}** (SQL ledger — `exit_logic` family, "
                f"registered variants only): `{dsr_family.get('deflated_sharpe_prob', 'n/a')}`"
            )
        if dsr_project:
            project_n = dsr_project.get("n_trials", "?")
            lines.append(
                f"- **DSR @ N_trials={project_n}** (SQL ledger — all registered variants): "
                f"`{dsr_project.get('deflated_sharpe_prob', 'n/a')}`"
            )
        # Honest counts (DROPPED + SUPERSEDED included) — the survivorship-
        # bias-corrected DSR per Harvey & Liu 2015.
        if dsr_honest_family and dsr_honest_family.get("n_trials"):
            family_hn = dsr_honest_family.get("n_trials", "?")
            lines.append(
                f"- **DSR @ N_trials={family_hn}** "
                f"(honest VARIANT_LEDGER.md — `exit_logic` family, includes DROPPED + SUPERSEDED): "
                f"`{dsr_honest_family.get('deflated_sharpe_prob', 'n/a')}`"
            )
        if dsr_honest_project and dsr_honest_project.get("n_trials"):
            project_hn = dsr_honest_project.get("n_trials", "?")
            lines.append(
                f"- **DSR @ N_trials={project_hn}** "
                f"(honest VARIANT_LEDGER.md — project-wide, every variant ever tested): "
                f"`{dsr_honest_project.get('deflated_sharpe_prob', 'n/a')}`"
            )
        if ledger_counts:
            in_fam_h = ledger_counts.get("in_family_honest") or 0
            in_proj_h = ledger_counts.get("in_project_honest") or 0
            lines.append(
                f"- _Variant ledger counts: SQL in_family=`{ledger_counts.get('in_family', '?')}`, "
                f"SQL in_project=`{ledger_counts.get('in_project', '?')}`, "
                f"honest in_family=`{in_fam_h}`, honest in_project=`{in_proj_h}` "
                f"(per scripts/research/VARIANT_LEDGER.md; "
                f"honest counts include DROPPED + SUPERSEDED — Harvey & Liu 2015 compliant)_"
            )
        if pbo and pbo.get("pbo") is not None:
            interp = pbo.get("interpretation")
            interp_text = interp[0] if isinstance(interp, tuple) else interp
            lines.append(
                f"- **PBO (AGGREGATE — see Stratified PBO section)**: `{pbo['pbo']}` across "
                f"{pbo.get('n_pairs_evaluated', '?')} cross-period pairs "
                f"({pbo.get('k_folds', '?')} folds × {pbo.get('n_variants', '?')} variants) "
                f"— {interp_text}"
            )

        # Correlation-aware DSR (Audit priority #9, 2026-05-22). Harvey &
        # Liu 2015 §5 — N_trials must be adjusted for non-independence
        # of the variant trials.
        dsr_eff = ms.get("dsr_at_effective_n") or {}
        if dsr_eff and dsr_eff.get("n_effective"):
            rho_bar = dsr_eff.get("mean_off_diagonal_correlation")
            lines.append(
                f"- **DSR @ N_eff={dsr_eff.get('n_effective', '?')}** "
                f"(correlation-aware, Harvey & Liu 2015 §5): "
                f"`{dsr_eff.get('deflated_sharpe_prob', 'n/a')}` "
                f"_(shrunk from N_honest={dsr_eff.get('n_honest', '?')} via "
                f"rho_bar={rho_bar})_"
            )
            lines.append(
                f"  _N_eff = N_honest × (1 − rho_bar), where rho_bar is the mean "
                f"off-diagonal Pearson correlation across the variant × time "
                f"return matrix. When all variants share the same underlying signal "
                f"stream their per-trade returns are highly correlated, so honest "
                f"N over-counts independent trials. N_eff is the corrected count._"
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

        # CPCV — combinatorial paths per variant
        cpcv = ms.get("cpcv") or {}
        if cpcv:
            any_paths = next(iter(cpcv.values())) if cpcv else {}
            n_paths = any_paths.get("n_paths", 0) if isinstance(any_paths, dict) else 0
            if n_paths > 0:
                lines.append("")
                lines.append("#### CPCV (Combinatorial Purged Cross-Validation) — AGGREGATE")
                lines.append("")
                lines.append(
                    "> **WARNING — regime-imbalanced.** The aggregate CPCV below pools "
                    "trades across all regimes, so the % positive figure is dominated "
                    "by whichever regime contributes the most signals. When one "
                    "small subset (e.g., POWER_HOUR, n=12) is the only profitable "
                    "stratum and a large one (TREND_DAY) is nil-edge, autocorrelation "
                    "makes the small subset's trades show up in every CPCV path and "
                    "inflate the apparent stability. **See the Stratified CPCV section "
                    "below for regime-conditional results — those are the ones to trust.**"
                )
                lines.append("")
                lines.append(
                    f"Each variant evaluated on **{n_paths} independent train/test paths** "
                    f"(C(6,2)=15 combinations of test-groups, purged with embargo). "
                    "Per-path Sharpe distribution shows whether the variant's edge is "
                    "stable across many train/test routes through the time series, "
                    "not just the one walk-forward path. Wider distributions = less "
                    "reliable edge."
                )
                lines.append("")
                lines.append("| Variant | n_paths | Sharpe mean | Sharpe std | % positive |")
                lines.append("|---|---:|---:|---:|---:|")
                for vname, vdata in cpcv.items():
                    if not isinstance(vdata, dict):
                        continue
                    sr_m = vdata.get("sharpe_mean")
                    sr_s = vdata.get("sharpe_std")
                    pos_pct = vdata.get("sharpe_positive_pct")
                    lines.append(
                        f"| {vname} | {vdata.get('n_paths', 0)} | "
                        f"{f'{sr_m:+.3f}' if sr_m is not None else '—'} | "
                        f"{f'{sr_s:.3f}' if sr_s is not None else '—'} | "
                        f"{f'{pos_pct:.0f}%' if pos_pct is not None else '—'} |"
                    )
                lines.append("")
                lines.append(
                    "_Interpretation: a variant with consistently positive Sharpe across "
                    "all 15 CPCV paths is statistically robust. A variant with mean > 0 "
                    "but std comparable to mean has unstable edge. % positive < 60% on "
                    "15 paths = the apparent edge depends heavily on which time slice "
                    "you train/test on. At n=123 trades / 6 groups = ~20 trades per "
                    "group, per-path Sharpes are still noisy; CPCV becomes much more "
                    "informative as data accumulates post-freeze._"
                )

        # Stratified CPCV (per-regime) — Audit priority #3, 2026-05-22.
        # Aggregate CPCV pools all regimes together; this section runs the
        # same C(K,k) path enumeration WITHIN each regime that has enough
        # samples, surfacing regime-conditional Sharpe stability that the
        # aggregate masks.
        stratified = ms.get("stratified_cpcv") or {}
        if stratified:
            lines.append("")
            lines.append("#### Stratified CPCV (per-regime)")
            lines.append("")
            lines.append(
                f"Same CPCV machinery, but trades are grouped by "
                f"`regime_at_fire` first. CPCV runs separately within each "
                f"regime where n >= {STRATIFIED_CPCV_MIN_N}. Regimes with "
                f"{STRATIFIED_CPCV_MIN_N} <= n < {STRATIFIED_CPCV_BORDERLINE_N} "
                f"are reported but flagged as borderline (per-path Sharpes "
                f"are very noisy at that sample size, since CPCV needs "
                f"n_groups=6 sub-blocks within the regime). Regimes with "
                f"n < {STRATIFIED_CPCV_MIN_N} are skipped and listed "
                f"separately as undersampled."
            )
            lines.append("")
            included = [(reg, e) for reg, e in stratified.items() if not e.get("skipped")]
            skipped = [(reg, e) for reg, e in stratified.items() if e.get("skipped")]
            if included:
                lines.append("| Regime | n | Variant | n_paths | Sharpe mean | Sharpe std | % positive | Flag |")
                lines.append("|---|---:|---|---:|---:|---:|---:|---|")
                for regime, entry in included:
                    n = entry.get("n", 0)
                    flag = "borderline" if entry.get("borderline") else ""
                    by_var = entry.get("by_variant") or {}
                    for vname, vdata in by_var.items():
                        if not isinstance(vdata, dict):
                            continue
                        n_paths = vdata.get("n_paths", 0)
                        sr_m = vdata.get("sharpe_mean")
                        sr_s = vdata.get("sharpe_std")
                        pos_pct = vdata.get("sharpe_positive_pct")
                        note = vdata.get("note") or ""
                        # If cpcv_paths returned a 0-paths skip (n too small
                        # after the n>=20 cut for some other reason — e.g.,
                        # CPCV's internal n_groups*3 floor), prefer its note.
                        cell_flag = flag
                        if n_paths == 0 and note:
                            cell_flag = f"{flag} | {note}".strip(" |")
                        lines.append(
                            f"| {regime} | {n} | {vname} | {n_paths} | "
                            f"{f'{sr_m:+.3f}' if sr_m is not None else '—'} | "
                            f"{f'{sr_s:.3f}' if sr_s is not None else '—'} | "
                            f"{f'{pos_pct:.0f}%' if pos_pct is not None else '—'} | "
                            f"{cell_flag} |"
                        )
                lines.append("")
            if skipped:
                lines.append("**Undersampled regimes (CPCV skipped):**")
                lines.append("")
                for regime, entry in skipped:
                    n = entry.get("n", 0)
                    note = entry.get("note") or f"n={n}"
                    lines.append(f"- `{regime}`: {note}")
                lines.append("")
            lines.append(
                "_Read this section before believing the aggregate CPCV %-positive "
                "figures. If the winning variant's edge is concentrated in one "
                "or two regimes with high % positive (and the dominant regime "
                "shows low or negative Sharpe mean), the headline aggregate "
                "result is regime-imbalance artifact, not stable cross-time edge. "
                "Conversely, a variant whose Sharpe mean is positive across "
                "every well-sampled regime is genuinely robust._"
            )

        # Stratified PBO (per-regime) — Audit priority #6, 2026-05-22.
        # Aggregate PBO above warned with "see Stratified PBO section";
        # this is that section. Same threshold pattern as Stratified CPCV:
        # n < 20 skipped, 20 <= n < 30 borderline.
        strat_pbo = ms.get("stratified_pbo") or {}
        if strat_pbo:
            lines.append("")
            lines.append("#### Stratified PBO (per-regime)")
            lines.append("")
            lines.append(
                f"Same PBO machinery (Bailey & López de Prado 2014 — best-on-fold-i "
                f"rank on fold-j), but trades are grouped by `regime_at_fire` first "
                f"and PBO runs separately within each regime where n >= "
                f"{STRATIFIED_CPCV_MIN_N}. Regimes with {STRATIFIED_CPCV_MIN_N} <= "
                f"n < {STRATIFIED_CPCV_BORDERLINE_N} are flagged borderline. "
                f"Regimes with n < {STRATIFIED_CPCV_MIN_N} are skipped. Aggregate "
                f"PBO can be misleading when one regime dominates the fold "
                f"composition; this stratification surfaces whether the winner "
                f"survives cross-period WITHIN a regime."
            )
            lines.append("")
            included_pbo = [(reg, e) for reg, e in strat_pbo.items() if not e.get("skipped")]
            skipped_pbo = [(reg, e) for reg, e in strat_pbo.items() if e.get("skipped")]
            if included_pbo:
                lines.append("| Regime | n | PBO | n_pairs | k_folds | n_variants | Interpretation | Flag |")
                lines.append("|---|---:|---:|---:|---:|---:|---|---|")
                for regime, entry in included_pbo:
                    n = entry.get("n", 0)
                    flag = "borderline" if entry.get("borderline") else ""
                    p = entry.get("pbo") or {}
                    pbo_val = p.get("pbo")
                    interp = p.get("interpretation")
                    interp_text = interp[0] if isinstance(interp, tuple) else (interp or "")
                    lines.append(
                        f"| {regime} | {n} | "
                        f"{f'{pbo_val:.4f}' if pbo_val is not None else '—'} | "
                        f"{p.get('n_pairs_evaluated', '—')} | "
                        f"{p.get('k_folds', '—')} | "
                        f"{p.get('n_variants', '—')} | "
                        f"{interp_text} | "
                        f"{flag} |"
                    )
                lines.append("")
            if skipped_pbo:
                lines.append("**Undersampled regimes (PBO skipped):**")
                lines.append("")
                for regime, entry in skipped_pbo:
                    n = entry.get("n", 0)
                    note = entry.get("note") or f"n={n}"
                    lines.append(f"- `{regime}`: {note}")
                lines.append("")
            lines.append(
                "_PBO < 0.25 within a regime = winners on one cross-section of "
                "that regime stay winners on another (good — stable edge). PBO "
                "between 0.25 and 0.5 = moderate overfit. PBO > 0.5 = systematic "
                "anti-correlation; the variant that wins on one slice loses on "
                "the next, classic noise-fitting. A winner with low aggregate PBO "
                "but high PBO within the dominant regime is a regime-imbalance "
                "artifact, not real edge._"
            )

        # Sequential Bootstrap — LdP AFML ch. 4. Corrects for the SE
        # underestimation that classical bootstrap suffers when label windows
        # overlap (here: 60-min lookahead horizons that pile up on
        # consecutive minutes of bars). Side-by-side with a classical
        # bootstrap so the inflation factor is visible.
        seq_boot = ms.get("sequential_bootstrap") or {}
        if seq_boot and seq_boot.get("sharpe_mean") is not None:
            lines.append("")
            lines.append("#### Sequential Bootstrap Sharpe distribution (winning variant)")
            lines.append("")
            lines.append(
                "López de Prado AFML ch. 4 (Algorithm 4.1). Standard bootstrap "
                "draws samples uniformly with replacement, treating overlapping-"
                "window labels as independent. When our signal labels share "
                "outcome bars (60-min lookahead horizons piled up on consecutive "
                "minute bars), classical bootstrap CIs are **mis-calibrated** "
                "— the resampled distribution doesn't reflect the effective "
                "sample size. Sequential Bootstrap samples each observation "
                "with probability ∝ its **uniqueness** (the fraction of its "
                "outcome bars not yet represented in the drawn sample), "
                "yielding bootstrap distributions that better approximate "
                "the true sampling distribution of the Sharpe estimator."
            )
            lines.append("")
            ci = seq_boot.get("sharpe_ci_95") or (None, None)
            classical = seq_boot.get("classical_bootstrap_comparison") or {}
            lines.append(
                f"- **Winning variant**: `{ms.get('winner', '?')}`  "
                f"(label horizon = {seq_boot.get('label_horizon_minutes', '?')} min, "
                f"{seq_boot.get('n_bootstraps', '?')} bootstrap reps)"
            )
            lines.append(
                f"- **Sequential SR mean ± std**: "
                f"`{seq_boot.get('sharpe_mean', 'n/a')} ± {seq_boot.get('sharpe_std', 'n/a')}`"
            )
            if ci[0] is not None and ci[1] is not None:
                lines.append(
                    f"- **95% CI**: `[{ci[0]:+.4f}, {ci[1]:+.4f}]`"
                )
            lines.append(
                f"- **P(SR ≤ 0)**: `{seq_boot.get('sharpe_p_negative', 'n/a')}`  "
                "(fraction of bootstrap reps whose Sharpe is non-positive)"
            )
            if classical.get("sharpe_std") is not None:
                lines.append(
                    f"- **Classical bootstrap SR std (for comparison)**: "
                    f"`{classical.get('sharpe_std', 'n/a')}`  "
                    f"(mean `{classical.get('sharpe_mean', 'n/a')}`)"
                )
            if seq_boot.get("se_inflation_factor") is not None:
                factor = seq_boot["se_inflation_factor"]
                if factor > 1.05:
                    fdir = (
                        "> 1 = sequential bootstrap inflates SE vs classical "
                        "(classical was understating uncertainty)"
                    )
                elif factor < 0.95:
                    fdir = (
                        "< 1 = sequential bootstrap tightens SE vs classical "
                        "(more diverse resampling reduces variance; "
                        "uniqueness-weighting picks more-representative draws)"
                    )
                else:
                    fdir = "≈ 1 = label overlap doesn't materially change SE here"
                lines.append(
                    f"- **SE ratio (seq / classical)**: `{factor}x`  ({fdir})"
                )
            lines.append("")
            lines.append(
                "_Interpretation: the sequential-bootstrap CI is the methodologically-"
                "correct one — read 95% CI and P(SR ≤ 0) directly. If 95% CI "
                "straddles zero or P(SR ≤ 0) > 0.1, the apparent edge is fragile. "
                "The SE-ratio diagnostic tells you whether classical bootstrap "
                "was lying about uncertainty: ratio > 1 means classical understated "
                "SE (the common AFML claim); ratio < 1 means classical overstated "
                "SE because uniqueness-weighted draws happen to be more "
                "representative on this sample. Either way, the sequential numbers "
                "are the ones to trust._"
            )

        # Calibrated sequential bootstrap (Audit priority #7, 2026-05-22).
        # The 60-min label horizon above is preserved for backwards
        # comparison. This calibrated run uses the median observed trade
        # duration (typically much shorter — 5/22 median was ~21 min),
        # which is the honest horizon since it reflects the window over
        # which one signal's outcome actually competes with the next
        # signal's outcome for the same minute-bars.
        seq_boot_cal = ms.get("sequential_bootstrap_calibrated") or {}
        cal_horizon = ms.get("calibrated_label_horizon_min")
        if seq_boot_cal and seq_boot_cal.get("sharpe_mean") is not None:
            lines.append("")
            lines.append("#### Sequential Bootstrap — CALIBRATED label horizon")
            lines.append("")
            lines.append(
                f"Same machinery as the 60-min version above, but the label "
                f"horizon is set to the **median observed trade duration** "
                f"(`{cal_horizon}` min) from `live_order.last_status_at - "
                f"submitted_at` across CLOSED + STOPPED parents in the same "
                f"date range. The legacy 60-min version is preserved for "
                f"backwards comparison; this calibrated version is the "
                f"honest one because uniqueness weighting depends on how "
                f"much real overlap there is between signals' outcome "
                f"windows. Two signals fired 25 minutes apart with median "
                f"trade duration ~20 min are effectively independent — the "
                f"60-min horizon was overstating their overlap and "
                f"downweighting both unnecessarily."
            )
            lines.append("")
            ci_c = seq_boot_cal.get("sharpe_ci_95") or (None, None)
            cls_c = seq_boot_cal.get("classical_bootstrap_comparison") or {}
            lines.append(
                f"- **Calibrated horizon**: `{cal_horizon} min` "
                f"(legacy was 60 min — was {round(60 / max(cal_horizon, 1), 1)}x longer "
                f"than median observed trade duration)"
            )
            lines.append(
                f"- **Sequential SR mean ± std (calibrated)**: "
                f"`{seq_boot_cal.get('sharpe_mean', 'n/a')} ± "
                f"{seq_boot_cal.get('sharpe_std', 'n/a')}`"
            )
            if ci_c[0] is not None and ci_c[1] is not None:
                lines.append(
                    f"- **95% CI (calibrated)**: `[{ci_c[0]:+.4f}, {ci_c[1]:+.4f}]`"
                )
            lines.append(
                f"- **P(SR ≤ 0) (calibrated)**: "
                f"`{seq_boot_cal.get('sharpe_p_negative', 'n/a')}`"
            )
            if cls_c.get("sharpe_std") is not None:
                lines.append(
                    f"- **Classical bootstrap SR std (calibrated comparison)**: "
                    f"`{cls_c.get('sharpe_std', 'n/a')}` "
                    f"(mean `{cls_c.get('sharpe_mean', 'n/a')}`)"
                )
            if seq_boot_cal.get("se_inflation_factor") is not None:
                lines.append(
                    f"- **SE ratio (seq / classical, calibrated)**: "
                    f"`{seq_boot_cal['se_inflation_factor']}x`"
                )
            lines.append("")
            lines.append(
                "_If the calibrated CI is materially tighter than the 60-min CI, "
                "the 60-min version was overstating SE by treating non-overlapping "
                "signals as overlapping. If the calibrated CI is wider, the 60-min "
                "version was missing real overlap. Either way, the calibrated "
                "numbers reflect the actual statistical situation in the data._"
            )
        elif cal_horizon is not None and seq_boot_cal.get("note"):
            lines.append("")
            lines.append("#### Sequential Bootstrap — CALIBRATED label horizon")
            lines.append("")
            lines.append(
                f"- Calibrated horizon = `{cal_horizon} min` "
                f"({seq_boot_cal.get('note', '')})"
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
        # Pre/post R-unit migration sanity output (Chat audit Priority 3 —
        # 2026-05-22). Prints first 5 signals' (legacy 10pt-R, per-signal R).
        sample = result.get("r_unit_migration_sample") or []
        if sample:
            print()
            print("### R-unit migration sanity (first 5 signals)")
            print()
            print("Legacy = old hardcoded 10pt-R. New = per-signal |entry-stop|.")
            print("Rows where stop wasn't +/-10pt from entry will differ.")
            print()
            print("| sid | risk_pts | baseline_R legacy | baseline_R new | B+C+D_R legacy | B+C+D_R new |")
            print("|---:|---:|---:|---:|---:|---:|")
            for s in sample:
                print(
                    f"| {s['sid']} | {s['risk_pts_new']} | "
                    f"{s['baseline_r_legacy']:+.3f} | {s['baseline_r_new']:+.3f} | "
                    f"{s['bcd_r_legacy']:+.3f} | {s['bcd_r_new']:+.3f} |"
                )
        # Actionable-count cross-check (Chat audit Priority 10 — 2026-05-22).
        # Verify canonical actionable count vs daily_review/daily_session sums.
        check = result.get("actionable_count_check") or {}
        if check:
            print()
            print("### Actionable-signal count cross-check")
            print()
            print(f"- canonical (paper, price-band, no-lookahead) : {check.get('canonical_with_price_band')}")
            print(f"- daily_review.actionable_signals sum         : {check.get('daily_review_sum')}")
            print(f"- daily_session.actionable_signals sum        : {check.get('daily_session_sum')}")
            print(f"- after require_lookahead=True (n_signals)    : {check.get('lookahead_join_n')}")
            # Expected: canonical <= daily_review_sum (price band filters out
            # test/mock rows with spx_price outside [1000, 10000]). If
            # canonical > daily_review_sum, the helper or counters drifted.
            canonical = check.get('canonical_with_price_band', 0)
            dr = check.get('daily_review_sum', 0)
            if canonical > dr:
                print(
                    f"  WARNING: canonical ({canonical}) > daily_review ({dr}). "
                    "One of the counters has drifted — investigate."
                )
            elif dr > canonical:
                print(
                    f"  Note: daily_review ({dr}) > canonical ({canonical}) — "
                    "expected when test rows fall outside the price band."
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
