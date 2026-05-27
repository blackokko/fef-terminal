"""Comprehensive FEF research dump for a date range.

One-stop analysis script. Given a session-date window, pulls everything
persisted in signal_log.db that we use to evaluate strategy performance
and produces a structured markdown report.

Sections (default = all):
    overview      — daily_review + daily_session rollups
    signals       — signal_iter volume / direction / confidence breakdown
    features      — feature-W/L bucket analysis (delta_price/drift, fe_ratio,
                    pa_score, walls, aggression, IVR) using lookahead outcomes
    hft           — HFT microstructure (book_imbalance, stuffing, cancel-to-trade,
                    vol_regime) bucketed by W/L using extras.hft_at_fire
    micro         — microstructure direction (OFI, AVI, microprice dev) bucketed
                    by W/L using extras.microstructure_at_fire
    lookahead     — forward MFE/MAE/T1-hit/stop-hit at 5/15/30/60min by
                    direction × confidence
    execution     — live_order + ibkr_execution audit, BE-ladder activations,
                    exit triggers, dollar slippage
    patterns      — pattern_match counts and outcomes per pattern
    tournaments   — tournament_run + tournament_run_result, walk_forward_run
    engine        — engine_change records (what shipped, measured impact)
    audit         — audit_log block reasons / kill switches / overrides
    vix           — VIX sample summary
    calendar      — calendar_event in window
    ablation      — call ablation_backtest.run_ablation()

CLI:
    python research_dump.py 2026-05-18 2026-05-20
    python research_dump.py 2026-05-18 2026-05-20 --sections signals,features,hft
    python research_dump.py 2026-05-18 2026-05-20 --brief --out report.md

Programmatic:
    from research_dump import run_research_dump
    md = run_research_dump("2026-05-18", "2026-05-20")
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Local import — ablation backtest + canonical actionable helper
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sidecar" / "src"))
from ablation_backtest import DOLLARS_PER_R, format_report as format_ablation_report, run_ablation
from fef_sidecar.db.repository import build_actionable_signals_sql  # noqa: E402

DEFAULT_DB = str(Path.home() / ".fef-terminal" / "signal_log.db")
SECTIONS_ALL = [
    "overview", "signals", "features", "hft", "micro", "lookahead",
    "execution", "patterns", "tournaments", "engine", "audit",
    "vix", "calendar", "ablation",
]

# ----------- Shared utilities -----------

def _parse_extras(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _stats(vals: list[float | None]) -> dict | None:
    clean = [float(v) for v in vals if v is not None and not (isinstance(v, float) and v != v)]
    if not clean:
        return None
    return {
        "n": len(clean),
        "mean": round(statistics.mean(clean), 4),
        "median": round(statistics.median(clean), 4),
        "min": round(min(clean), 4),
        "max": round(max(clean), 4),
    }


def _md_table(headers: list[str], rows: list[list[Any]], aligns: list[str] | None = None) -> list[str]:
    aligns = aligns or ["left"] * len(headers)
    sep = []
    for a in aligns:
        if a == "right":
            sep.append("---:")
        elif a == "center":
            sep.append(":---:")
        else:
            sep.append(":---")
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(sep) + " |"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return out


def _bucket_for(direction: str, hit_t1: bool, hit_stop: bool) -> str:
    """Map (direction, lookahead outcome) → W/L bucket label."""
    side = "C" if direction == "LONG_CALLS" else ("P" if direction == "LONG_PUTS" else None)
    if side is None:
        return "WAIT"
    if hit_t1 and not hit_stop:
        return f"{side}_WIN"
    if hit_stop and not hit_t1:
        return f"{side}_LOSS"
    if hit_t1 and hit_stop:
        return f"{side}_MIXED"
    return f"{side}_NEUTRAL"


def _row_account_class(d: dict) -> str:
    """Read account_class from extras (added 2026-05-21 commit 2e2f795).
    Falls back to 'unknown' for rows written before the tag existed."""
    extras = d.get("extras_parsed")
    if not extras:
        return "unknown"
    return str(extras.get("account_class") or "unknown")


def _account_class_match(d: dict, accept: set[str]) -> bool:
    """True if the row's account_class is in the accept set. 'unknown' is
    treated as paper-equivalent ONLY when paper is in the accept set
    (since 100% of pre-tag data is from the paper account DUM098107)."""
    cls = _row_account_class(d)
    if cls in accept:
        return True
    if cls == "unknown" and "paper" in accept:
        return True
    return False


def _load_fired_with_outcomes(
    cur: sqlite3.Cursor, date_from: str, date_to: str,
    accept_classes: set[str] | None = None,
) -> list[dict]:
    """Join SignalIter (fired only) with SignalLookaheadOutcome @ 60min.
    Returns one row per fired signal with unpacked extras.

    accept_classes filters by extras.account_class. Default = the global
    _ACCOUNT_CLASS_ACCEPTED (set by run_research_dump from the CLI flag).

    Uses the canonical `build_actionable_signals_sql` helper for the
    actionable filter (Chat audit Priority 10 — 2026-05-22). Signals here
    are ALL actionable rows with optional outcomes attached (LEFT JOIN);
    the post-query filter applies the broader account_class accept set
    when not strictly paper-only."""
    accept = accept_classes or globals().get("_ACCOUNT_CLASS_ACCEPTED", {"paper"})
    # The canonical builder handles direction != WAIT + price band + paper
    # filter. We add the lookahead LEFT JOIN ourselves since this caller
    # WANTS rows without outcomes (require_lookahead=False) but with the
    # outcome columns LEFT-joined in. Build the base subquery via the
    # helper, then wrap with the LEFT JOIN.
    paper_only = accept == {"paper"} or accept == {"paper", "unknown"}
    base_sql, params = build_actionable_signals_sql(
        date_from=date_from, date_to=date_to,
        paper_only=paper_only,
        price_band=(1000.0, 10000.0),
        require_lookahead=False,
        select_clause=(
            "s.id, s.session_date, s.timestamp, s.direction, s.confidence, "
            "s.spx_price, s.drift_cum_net_m, s.drift_bias, "
            "s.aggression_call_pct, s.aggression_put_pct, "
            "s.iv_rank_call_pct, s.iv_rank_put_pct, s.extras"
        ),
    )
    # Wrap the canonical query in a CTE so we can LEFT JOIN the lookahead
    # outcome columns onto it without duplicating the WHERE clause.
    sql = (
        f"WITH actionable AS ({base_sql}) "
        "SELECT a.id, a.session_date, a.timestamp, a.direction, a.confidence, "
        "       a.spx_price, a.drift_cum_net_m, a.drift_bias, "
        "       a.aggression_call_pct, a.aggression_put_pct, "
        "       a.iv_rank_call_pct, a.iv_rank_put_pct, a.extras, "
        "       o.would_have_hit_t1, o.would_have_hit_stop, "
        "       o.spx_mfe_pts, o.spx_mae_pts, o.spx_close_at_window_end "
        "FROM actionable a "
        "LEFT JOIN signal_lookahead_outcome o "
        "  ON o.signal_iter_id = a.id AND o.lookahead_minutes = 60"
    )
    rows = cur.execute(sql, params).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["extras_parsed"] = _parse_extras(d.get("extras"))
        # When paper_only fired in SQL, the account_class post-filter is
        # redundant; for the broader accept-set case the helper passed
        # paper_only=False and we filter here.
        if not paper_only and not _account_class_match(d, accept):
            continue
        d["bucket"] = _bucket_for(
            d["direction"],
            bool(d.get("would_have_hit_t1")),
            bool(d.get("would_have_hit_stop")),
        )
        out.append(d)
    return out


# ----------- Sections -----------

def section_overview(cur: sqlite3.Cursor, date_from: str, date_to: str) -> list[str]:
    lines = ["## Overview — daily_review + daily_session"]
    dr = cur.execute(
        """SELECT session_date, total_signals, actionable_signals, wins, stops,
                  timeouts, win_rate_pct, expectancy_r, actual_trades_taken,
                  actual_total_R, dedup_actionable, dedup_wins, dedup_stops,
                  dedup_win_rate_pct, dedup_expectancy_r, sharpe_ratio,
                  max_drawdown_R, profit_factor
           FROM daily_review WHERE session_date BETWEEN ? AND ?
           ORDER BY session_date""",
        (date_from, date_to),
    ).fetchall()
    if not dr:
        lines.append("_No daily_review rows in window._")
    else:
        rows = [[r["session_date"], r["total_signals"], r["actionable_signals"],
                 r["wins"], r["stops"], r["timeouts"],
                 f"{r['win_rate_pct']:.1f}%" if r["win_rate_pct"] is not None else "-",
                 f"{r['expectancy_r']:+.2f}" if r["expectancy_r"] is not None else "-",
                 r["actual_trades_taken"],
                 f"{r['actual_total_R']:+.2f}" if r["actual_total_R"] is not None else "-",
                 f"{r['dedup_expectancy_r']:+.3f}" if r["dedup_expectancy_r"] is not None else "-",
                 f"{r['sharpe_ratio']:.2f}" if r["sharpe_ratio"] is not None else "-",
                 f"{r['max_drawdown_R']:+.2f}" if r["max_drawdown_R"] is not None else "-",
                 f"{r['profit_factor']:.2f}" if r["profit_factor"] is not None else "-",
                 ] for r in dr]
        lines.extend(_md_table(
            ["date","total","actionable","wins","stops","timeouts","WR","expR","trades","actualR","dedupExpR","sharpe","maxDD","PF"],
            rows,
            ["left","right","right","right","right","right","right","right","right","right","right","right","right","right"],
        ))

    ds = cur.execute(
        """SELECT session_date, total_iters, actionable_signals, trades_taken,
                  gross_pnl, net_pnl, win_count, loss_count,
                  best_trade_pnl, worst_trade_pnl
           FROM daily_session WHERE session_date BETWEEN ? AND ?
           ORDER BY session_date""",
        (date_from, date_to),
    ).fetchall()
    if ds:
        lines.append("")
        lines.append("### daily_session (operator-tracked)")
        lines.append("")
        rows = [[r["session_date"], r["total_iters"], r["trades_taken"],
                 f"${r['gross_pnl']:+,.0f}" if r["gross_pnl"] is not None else "-",
                 f"${r['net_pnl']:+,.0f}" if r["net_pnl"] is not None else "-",
                 r["win_count"], r["loss_count"],
                 f"${r['best_trade_pnl']:+,.0f}" if r["best_trade_pnl"] is not None else "-",
                 f"${r['worst_trade_pnl']:+,.0f}" if r["worst_trade_pnl"] is not None else "-",
                 ] for r in ds]
        lines.extend(_md_table(
            ["date","iters","trades","gross$","net$","W","L","best","worst"],
            rows,
            ["left","right","right","right","right","right","right","right","right"],
        ))
    return lines


def section_signals(cur: sqlite3.Cursor, date_from: str, date_to: str) -> list[str]:
    lines = ["## Signal generation — signal_iter"]
    by_date_dir = cur.execute(
        """SELECT session_date, direction, confidence, COUNT(*) n
           FROM signal_iter WHERE session_date BETWEEN ? AND ?
           GROUP BY session_date, direction, confidence
           ORDER BY session_date, direction, confidence""",
        (date_from, date_to),
    ).fetchall()
    if not by_date_dir:
        lines.append("_No signal_iter rows in window._")
        return lines
    pivot: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    totals_by_date: dict[str, int] = defaultdict(int)
    for r in by_date_dir:
        pivot[r["session_date"]][r["direction"]][r["confidence"]] += r["n"]
        totals_by_date[r["session_date"]] += r["n"]
    rows = []
    for date in sorted(pivot.keys()):
        for d in ("LONG_CALLS","LONG_PUTS","WAIT"):
            bucket = pivot[date].get(d, {})
            if not bucket:
                continue
            rows.append([date, d, sum(bucket.values()),
                         bucket.get("HIGH", 0), bucket.get("MEDIUM", 0), bucket.get("LOW", 0)])
    lines.extend(_md_table(
        ["date","direction","total","HIGH","MEDIUM","LOW"],
        rows,
        ["left","left","right","right","right","right"],
    ))

    lines.append("")
    lines.append("### drift_bias distribution by date")
    lines.append("")
    drift = cur.execute(
        """SELECT session_date, drift_bias, COUNT(*) n
           FROM signal_iter WHERE session_date BETWEEN ? AND ?
           GROUP BY session_date, drift_bias ORDER BY session_date, drift_bias""",
        (date_from, date_to),
    ).fetchall()
    rows = [[r["session_date"], r["drift_bias"] or "(null)", r["n"]] for r in drift]
    lines.extend(_md_table(["date","drift_bias","n"], rows, ["left","left","right"]))

    # data-quality flag: signals with broken spx_price
    bad = cur.execute(
        "SELECT session_date, COUNT(*) n FROM signal_iter WHERE session_date BETWEEN ? AND ? AND (spx_price < 1000 OR spx_price > 10000) GROUP BY session_date",
        (date_from, date_to),
    ).fetchall()
    if bad:
        lines.append("")
        lines.append("### Data quality — SignalIter rows with invalid spx_price")
        lines.append("")
        rows = [[r["session_date"], r["n"]] for r in bad]
        lines.extend(_md_table(["date","invalid_rows"], rows, ["left","right"]))
        lines.append("")
        lines.append("_(filter `spx_price BETWEEN 1000 AND 10000` excludes these from downstream analysis)_")
    return lines


# ----- Feature bucket analysis helpers -----

NUMERIC_FEATURE_KEYS = [
    ("delta_price", "extras.delta_price"),
    ("delta_drift", "extras.delta_drift_m"),
    ("fe_ratio", "extras.fe_ratio"),
    ("drift_cum_net_m", "row.drift_cum_net_m"),
    ("pa_score", "extras.secondary_scores.price_action"),
    ("mp_gravity", "extras.secondary_scores.mp_gravity"),
    ("drift_score", "extras.secondary_scores.drift"),
    ("gex_walls", "extras.secondary_scores.gex_walls"),
    ("dex_floor", "extras.secondary_scores.dex_floor"),
    ("agg_score", "extras.secondary_scores.aggression"),
    ("nearest_wall_distance", "extras.nearest_wall_distance"),
    ("aggression_call_pct", "row.aggression_call_pct"),
    ("aggression_put_pct", "row.aggression_put_pct"),
    ("iv_rank_call_pct", "row.iv_rank_call_pct"),
    ("iv_rank_put_pct", "row.iv_rank_put_pct"),
]

HFT_FIELDS = [
    ("vol_regime", "categorical"),
    ("vol_ratio", "numeric"),
    ("short_vol_pts", "numeric"),
    ("long_vol_pts", "numeric"),
    ("cancel_to_trade_ratio", "numeric"),
    ("quote_intensity_per_sec", "numeric"),
    ("stuffing_severity", "categorical"),
    ("book_imbalance", "numeric"),
    ("book_imbalance_5lvl", "numeric"),
    ("book_direction", "categorical"),
    ("book_confidence", "categorical"),
    ("composite_direction", "categorical"),
    ("book_spread", "numeric"),
]

MICRO_FIELDS = [
    ("ofi_window", "numeric"),
    ("avi_pct", "numeric"),
    ("aggressive_buy_volume", "numeric"),
    ("aggressive_sell_volume", "numeric"),
    ("microprice_deviation_bp", "numeric"),
    ("direction", "categorical"),
    ("confidence", "categorical"),
]


def _pluck(row: dict, key: str):
    """Resolve dotted feature path like 'extras.secondary_scores.price_action'."""
    if key.startswith("row."):
        return row.get(key[4:])
    if key.startswith("extras."):
        path = key[7:].split(".")
        val = row.get("extras_parsed", {})
        for p in path:
            if not isinstance(val, dict):
                return None
            val = val.get(p)
        return val
    return None


def _bucketed_feature_analysis(
    fired: list[dict],
    field_specs: list[tuple[str, str]],
    extract: callable,
    title: str,
) -> list[str]:
    """Returns markdown lines bucketing each field by C_WIN/C_LOSS/P_WIN/P_LOSS."""
    lines = [f"## {title}"]
    if not fired:
        lines.append("_No fired signals with outcome data in window._")
        return lines

    # group by bucket
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for r in fired:
        by_bucket[r["bucket"]].append(r)
    bucket_order = ["C_WIN","C_LOSS","C_MIXED","C_NEUTRAL","P_WIN","P_LOSS","P_MIXED","P_NEUTRAL"]
    active_buckets = [b for b in bucket_order if b in by_bucket]
    summary_row = [["bucket", "n"]] + [[b, len(by_bucket[b])] for b in active_buckets]
    lines.append(f"_Buckets present: {', '.join(f'{b}={len(by_bucket[b])}' for b in active_buckets)}._")
    lines.append("")

    numeric_fields = [(name, kind) for name, kind in field_specs if kind == "numeric"]
    if numeric_fields:
        # rows = field; cols = each bucket's median (with n)
        header = ["field"] + active_buckets
        rows = []
        for name, _ in numeric_fields:
            row = [name]
            for b in active_buckets:
                vals = [extract(r, name) for r in by_bucket[b]]
                s = _stats(vals)
                row.append(f"{s['median']:+.3f} (n={s['n']})" if s else "-")
            rows.append(row)
        lines.append("### Numeric features — median per bucket")
        lines.append("")
        lines.extend(_md_table(header, rows, ["left"] + ["right"] * len(active_buckets)))

    categorical_fields = [(name, kind) for name, kind in field_specs if kind == "categorical"]
    if categorical_fields:
        lines.append("")
        lines.append("### Categorical features — counts per bucket")
        lines.append("")
        for name, _ in categorical_fields:
            lines.append(f"**{name}**")
            lines.append("")
            counts_by_bucket: dict[str, Counter] = {}
            all_values: set[str] = set()
            for b in active_buckets:
                vals = [extract(r, name) for r in by_bucket[b]]
                ctr = Counter(str(v) for v in vals if v is not None)
                counts_by_bucket[b] = ctr
                all_values.update(ctr.keys())
            if not all_values:
                lines.append("_No values._")
                lines.append("")
                continue
            header = ["value"] + active_buckets
            rows = [[v] + [counts_by_bucket[b].get(v, 0) for b in active_buckets]
                    for v in sorted(all_values)]
            lines.extend(_md_table(header, rows, ["left"] + ["right"] * len(active_buckets)))
            lines.append("")
    return lines


def section_features(cur: sqlite3.Cursor, date_from: str, date_to: str) -> list[str]:
    fired = _load_fired_with_outcomes(cur, date_from, date_to)
    def extract(row, name):
        for k, path in NUMERIC_FEATURE_KEYS:
            if k == name:
                v = _pluck(row, path)
                # treat nan as None
                if isinstance(v, float) and v != v:
                    return None
                return v
        return None
    return _bucketed_feature_analysis(
        fired,
        [(name, "numeric") for name, _ in NUMERIC_FEATURE_KEYS] +
        [("confidence", "categorical"), ("drift_bias", "categorical")],
        lambda r, name: r.get(name) if name in ("confidence","drift_bias") else extract(r, name),
        "Signal features — W/L bucket analysis (60min lookahead outcomes)",
    )


def section_hft(cur: sqlite3.Cursor, date_from: str, date_to: str) -> list[str]:
    fired = _load_fired_with_outcomes(cur, date_from, date_to)
    n_with_hft = sum(1 for r in fired if r["extras_parsed"].get("hft_at_fire"))
    def extract(row, name):
        hft = row["extras_parsed"].get("hft_at_fire") or {}
        return hft.get(name)
    out = _bucketed_feature_analysis(
        fired, HFT_FIELDS, extract,
        f"HFT microstructure — W/L bucket analysis (n_with_hft_snapshot={n_with_hft}/{len(fired)})",
    )
    return out


def section_micro(cur: sqlite3.Cursor, date_from: str, date_to: str) -> list[str]:
    fired = _load_fired_with_outcomes(cur, date_from, date_to)
    n_with_micro = sum(1 for r in fired if r["extras_parsed"].get("microstructure_at_fire"))
    def extract(row, name):
        m = row["extras_parsed"].get("microstructure_at_fire") or {}
        return m.get(name)
    return _bucketed_feature_analysis(
        fired, MICRO_FIELDS, extract,
        f"Microstructure direction — W/L bucket analysis (n_with_micro_snapshot={n_with_micro}/{len(fired)})",
    )


def section_lookahead(cur: sqlite3.Cursor, date_from: str, date_to: str) -> list[str]:
    lines = ["## Forward outcomes (signal_lookahead_outcome)"]
    agg = cur.execute(
        """SELECT session_date, direction_at_signal AS dir, lookahead_minutes AS win,
                  COUNT(*) n,
                  ROUND(AVG(spx_mfe_pts), 2) AS mfe,
                  ROUND(AVG(spx_mae_pts), 2) AS mae,
                  ROUND(AVG(spx_close_at_window_end - spx_at_signal), 2) AS close_chg,
                  ROUND(100.0 * AVG(CASE WHEN would_have_hit_t1 THEN 1.0 ELSE 0.0 END), 1) AS t1_pct,
                  ROUND(100.0 * AVG(CASE WHEN would_have_hit_stop THEN 1.0 ELSE 0.0 END), 1) AS stop_pct
           FROM signal_lookahead_outcome
           WHERE session_date BETWEEN ? AND ?
             AND spx_at_signal BETWEEN 1000 AND 10000
           GROUP BY session_date, direction_at_signal, lookahead_minutes
           ORDER BY session_date, direction_at_signal, lookahead_minutes""",
        (date_from, date_to),
    ).fetchall()
    if not agg:
        lines.append("_No signal_lookahead_outcome rows in window. Did the worker run?_")
        return lines
    rows = [[r["session_date"], r["dir"], r["win"], r["n"], r["mfe"], r["mae"],
             r["close_chg"], f"{r['t1_pct']}%", f"{r['stop_pct']}%"] for r in agg]
    lines.extend(_md_table(
        ["date","direction","win","n","mfe","mae","close_chg","T1%","stop%"],
        rows,
        ["left","left","right","right","right","right","right","right","right"],
    ))
    return lines


def section_execution(cur: sqlite3.Cursor, date_from: str, date_to: str) -> list[str]:
    lines = ["## Execution audit — live_order + ibkr_execution"]
    parents = cur.execute(
        """SELECT id, submitted_at, symbol, strike, right, action, qty, status,
                  filled_qty, avg_fill_price, commission, exit_strategy,
                  entry_spx_price, spx_target_t1, spx_target_t2, spx_target_t3,
                  spx_target_stop, spx_target_t1_fired, spx_target_t2_fired,
                  spx_target_t3_fired, dollar_loss_cap, dynamic_stop_level,
                  be_ladder_max_step_R, dry_run
           FROM live_order
           WHERE bracket_role = 'parent'
             AND DATE(submitted_at) BETWEEN ? AND ?
           ORDER BY submitted_at""",
        (date_from, date_to),
    ).fetchall()
    if not parents:
        lines.append("_No parent live_orders in window._")
        return lines

    # Per-day summary
    by_date: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n":0, "filled":0, "closed":0, "stopped":0, "open":0, "dry":0,
                 "be_activated":0, "t1_fires":0, "t2_fires":0, "t3_fires":0}
    )
    for p in parents:
        d = str(p["submitted_at"])[:10]
        b = by_date[d]
        b["n"] += 1
        if p["filled_qty"] and p["filled_qty"] > 0:
            b["filled"] += 1
        if p["status"] == "CLOSED":
            b["closed"] += 1
        elif p["status"] == "STOPPED":
            b["stopped"] += 1
        elif p["status"] in ("WORKING","FILLED","SUBMITTED","ACCEPTED"):
            b["open"] += 1
        if p["dry_run"]:
            b["dry"] += 1
        if p["be_ladder_max_step_R"] and p["be_ladder_max_step_R"] > 0:
            b["be_activated"] += 1
        if p["spx_target_t1_fired"]:
            b["t1_fires"] += 1
        if p["spx_target_t2_fired"]:
            b["t2_fires"] += 1
        if p["spx_target_t3_fired"]:
            b["t3_fires"] += 1
    rows = [[d, b["n"], b["filled"], b["closed"], b["stopped"], b["open"], b["dry"],
             b["t1_fires"], b["t2_fires"], b["t3_fires"], b["be_activated"]]
            for d, b in sorted(by_date.items())]
    lines.extend(_md_table(
        ["date","parents","filled","closed","stopped","open","dry","t1","t2","t3","BE_act"],
        rows,
        ["left","right","right","right","right","right","right","right","right","right","right"],
    ))

    # BE-ladder kill rate (stopped trades where BE was activated)
    be_kills = sum(1 for p in parents if p["status"] == "STOPPED" and p["be_ladder_max_step_R"] and p["be_ladder_max_step_R"] > 0)
    total_stops = sum(1 for p in parents if p["status"] == "STOPPED")
    lines.append("")
    if total_stops:
        lines.append(f"**BE-ladder kill rate**: {be_kills}/{total_stops} stops had BE ladder pre-activated ({100*be_kills/total_stops:.0f}%) — this is the cohort hit by the SPX-vs-option-price exit mismatch.")
    else:
        lines.append("_No stopped trades in window._")

    # IBKR executions count
    execs = cur.execute(
        "SELECT session_date, COUNT(*) n FROM ibkr_execution WHERE session_date BETWEEN ? AND ? GROUP BY session_date ORDER BY session_date",
        (date_from, date_to),
    ).fetchall()
    if execs:
        lines.append("")
        lines.append("### IBKR fills per session")
        lines.append("")
        lines.extend(_md_table(["date","fills"], [[r["session_date"], r["n"]] for r in execs], ["left","right"]))
    return lines


def section_patterns(cur: sqlite3.Cursor, date_from: str, date_to: str) -> list[str]:
    lines = ["## Pattern matches"]
    matches = cur.execute(
        """SELECT p.name, p.enabled, DATE(pm.matched_at) AS d, COUNT(*) n
           FROM pattern_match pm JOIN pattern p ON p.id = pm.pattern_id
           WHERE DATE(pm.matched_at) BETWEEN ? AND ?
           GROUP BY p.name, d ORDER BY p.name, d""",
        (date_from, date_to),
    ).fetchall()
    if not matches:
        lines.append("_No pattern matches in window._")
        return lines
    rows = [[r["name"], "yes" if r["enabled"] else "no", r["d"], r["n"]] for r in matches]
    lines.extend(_md_table(["pattern","enabled","date","matches"], rows, ["left","left","left","right"]))
    return lines


def section_tournaments(cur: sqlite3.Cursor, date_from: str, date_to: str) -> list[str]:
    lines = ["## Tournaments + walk-forward"]
    tr = cur.execute(
        """SELECT id, run_at, session_date_from, session_date_to, preset_count,
                  apply_be_ladder, dominant_regime, notes
           FROM tournament_run
           WHERE DATE(run_at) BETWEEN ? AND ? OR
                 session_date_from BETWEEN ? AND ? OR
                 session_date_to BETWEEN ? AND ?
           ORDER BY run_at DESC""",
        (date_from, date_to, date_from, date_to, date_from, date_to),
    ).fetchall()
    if tr:
        lines.append("### tournament_run")
        lines.append("")
        rows = [[r["id"], str(r["run_at"])[:19], r["session_date_from"], r["session_date_to"],
                 r["preset_count"], "yes" if r["apply_be_ladder"] else "no",
                 r["dominant_regime"] or "-", (r["notes"] or "")[:60]] for r in tr]
        lines.extend(_md_table(
            ["id","run_at","from","to","presets","BE","regime","notes"],
            rows,
            ["right","left","left","left","right","left","left","left"],
        ))
        run_ids = [r["id"] for r in tr]
        placeholders = ",".join("?" * len(run_ids))
        results = cur.execute(
            f"""SELECT tournament_run_id, preset_name, rank, trade_count,
                       win_rate_pct, expectancy_r, sharpe_ratio, profit_factor,
                       cumulative_r
                FROM tournament_run_result
                WHERE tournament_run_id IN ({placeholders})
                ORDER BY tournament_run_id, rank""",
            run_ids,
        ).fetchall()
        if results:
            lines.append("")
            lines.append("### tournament_run_result")
            lines.append("")
            rows = [[r["tournament_run_id"], r["rank"], r["preset_name"], r["trade_count"],
                     f"{r['win_rate_pct']:.1f}%" if r["win_rate_pct"] is not None else "-",
                     f"{r['expectancy_r']:+.3f}" if r["expectancy_r"] is not None else "-",
                     f"{r['sharpe_ratio']:.2f}" if r["sharpe_ratio"] is not None else "-",
                     f"{r['profit_factor']:.2f}" if r["profit_factor"] is not None else "-",
                     f"{r['cumulative_r']:+.2f}" if r["cumulative_r"] is not None else "-",
                     ] for r in results]
            lines.extend(_md_table(
                ["run","rank","preset","trades","WR","exp_R","sharpe","PF","cum_R"],
                rows,
                ["right","right","left","right","right","right","right","right","right"],
            ))
    else:
        lines.append("_No tournament_run in window._")
    wf = cur.execute(
        """SELECT id, run_at, window_days, optimal_sharpe, optimal_total_pnl,
                  optimal_win_rate, drift_score, notes
           FROM walk_forward_run
           WHERE DATE(run_at) BETWEEN ? AND ?
           ORDER BY run_at DESC""",
        (date_from, date_to),
    ).fetchall()
    if wf:
        lines.append("")
        lines.append("### walk_forward_run")
        lines.append("")
        rows = [[r["id"], str(r["run_at"])[:19], r["window_days"],
                 f"{r['optimal_sharpe']:.2f}" if r["optimal_sharpe"] is not None else "-",
                 f"{r['optimal_total_pnl']:+.2f}" if r["optimal_total_pnl"] is not None else "-",
                 f"{r['optimal_win_rate']:.1f}%" if r["optimal_win_rate"] is not None else "-",
                 f"{r['drift_score']:.3f}" if r["drift_score"] is not None else "-",
                 (r["notes"] or "")[:50]] for r in wf]
        lines.extend(_md_table(
            ["id","run_at","days","opt_sharpe","opt_pnl","opt_WR","drift","notes"],
            rows,
            ["right","left","right","right","right","right","right","left"],
        ))
    return lines


def section_engine(cur: sqlite3.Cursor, date_from: str, date_to: str) -> list[str]:
    lines = ["## Engine changes (engine_change)"]
    rows = cur.execute(
        """SELECT applied_at, name, category, expected_impact, measured_impact_r,
                  rollback_condition, git_commit_sha
           FROM engine_change WHERE DATE(applied_at) BETWEEN ? AND ?
           ORDER BY applied_at DESC""",
        (date_from, date_to),
    ).fetchall()
    if not rows:
        lines.append("_No engine_change rows in window._")
        return lines
    md_rows = [[str(r["applied_at"])[:19], r["name"], r["category"] or "-",
                (r["expected_impact"] or "-")[:50],
                f"{r['measured_impact_r']:+.2f}" if r["measured_impact_r"] is not None else "-",
                (r["git_commit_sha"] or "")[:10]] for r in rows]
    lines.extend(_md_table(
        ["applied","name","category","expected","measured_R","sha"],
        md_rows,
        ["left","left","left","left","right","left"],
    ))
    return lines


def section_audit(cur: sqlite3.Cursor, date_from: str, date_to: str) -> list[str]:
    lines = ["## Audit log (block reasons / kill switches / overrides)"]
    by_cat = cur.execute(
        """SELECT DATE(timestamp) d, category, block_reason_code, COUNT(*) n
           FROM audit_log WHERE DATE(timestamp) BETWEEN ? AND ?
           GROUP BY d, category, block_reason_code
           ORDER BY d, category, n DESC""",
        (date_from, date_to),
    ).fetchall()
    if not by_cat:
        lines.append("_No audit_log rows in window._")
        return lines
    rows = [[r["d"], r["category"], r["block_reason_code"] or "-", r["n"]] for r in by_cat]
    lines.extend(_md_table(["date","category","block_reason","n"], rows, ["left","left","left","right"]))
    return lines


def section_vix(cur: sqlite3.Cursor, date_from: str, date_to: str) -> list[str]:
    lines = ["## VIX context (vix_sample)"]
    rows = cur.execute(
        """SELECT DATE(datetime(timestamp_ms/1000, 'unixepoch','-4 hours')) AS d,
                  ROUND(MIN(value), 2) AS lo, ROUND(MAX(value), 2) AS hi,
                  ROUND(AVG(value), 2) AS avg_, COUNT(*) AS n
           FROM vix_sample
           WHERE timestamp_ms BETWEEN strftime('%s', ?) * 1000 AND strftime('%s', ?) * 1000 + 86400000
           GROUP BY d ORDER BY d""",
        (date_from, date_to),
    ).fetchall()
    if not rows:
        lines.append("_No VIX samples in window._")
        return lines
    md_rows = [[r["d"], r["lo"], r["hi"], r["avg_"], r["n"]] for r in rows]
    lines.extend(_md_table(["date","min","max","avg","samples"], md_rows, ["left","right","right","right","right"]))
    return lines


def section_calendar(cur: sqlite3.Cursor, date_from: str, date_to: str) -> list[str]:
    lines = ["## Calendar events in window"]
    rows = cur.execute(
        """SELECT event_date, event_time_et, category, title, impact,
                  auto_pause_minutes_before, auto_pause_minutes_after
           FROM calendar_event WHERE event_date BETWEEN ? AND ?
           ORDER BY event_date, event_time_et""",
        (date_from, date_to),
    ).fetchall()
    if not rows:
        lines.append("_No calendar events in window._")
        return lines
    md_rows = [[r["event_date"], r["event_time_et"] or "-", r["category"] or "-",
                (r["title"] or "")[:50], r["impact"] or "-",
                r["auto_pause_minutes_before"], r["auto_pause_minutes_after"]] for r in rows]
    lines.extend(_md_table(
        ["date","time_ET","category","title","impact","pause_before","pause_after"],
        md_rows,
        ["left","left","left","left","left","right","right"],
    ))
    return lines


def section_ablation(cur: sqlite3.Cursor, date_from: str, date_to: str, db_path: str) -> list[str]:
    result = run_ablation(date_from, date_to, db_path)
    return format_ablation_report(result, DOLLARS_PER_R).split("\n")


# ----------- Driver -----------

SECTION_FNS = {
    "overview": section_overview,
    "signals": section_signals,
    "features": section_features,
    "hft": section_hft,
    "micro": section_micro,
    "lookahead": section_lookahead,
    "execution": section_execution,
    "patterns": section_patterns,
    "tournaments": section_tournaments,
    "engine": section_engine,
    "audit": section_audit,
    "vix": section_vix,
    "calendar": section_calendar,
}


_ACCOUNT_CLASS_DEFAULT: set[str] = {"paper"}


def _parse_account_class_arg(raw: str) -> set[str]:
    """Parse --account-class CLI value into a set. 'all' = no filter."""
    if not raw:
        return {"paper"}
    raw = raw.strip().lower()
    if raw == "all":
        return {"paper", "live", "mock", "unknown"}
    return {x.strip() for x in raw.split(",") if x.strip()}


def run_research_dump(
    date_from: str,
    date_to: str,
    db_path: str = DEFAULT_DB,
    sections: list[str] | None = None,
    accept_classes: set[str] | None = None,
) -> str:
    sections = sections or SECTIONS_ALL
    accept = accept_classes or set(_ACCOUNT_CLASS_DEFAULT)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    # Mutate the global default so section functions (which read extras
    # on their own queries) pick up the same filter. Cleaner than threading
    # the set through 14 section signatures.
    global _ACCOUNT_CLASS_ACCEPTED
    _ACCOUNT_CLASS_ACCEPTED = accept

    header = [
        f"# FEF Research Dump",
        f"",
        f"- **Window**: {date_from} to {date_to}",
        f"- **Account classes**: {sorted(accept)}",
        f"- **Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- **DB**: `{db_path}`",
        f"- **Sections**: {', '.join(sections)}",
        f"",
    ]

    body: list[str] = []
    for s in sections:
        body.append("\n---\n")
        try:
            if s == "ablation":
                body.extend(section_ablation(cur, date_from, date_to, db_path))
            elif s in SECTION_FNS:
                body.extend(SECTION_FNS[s](cur, date_from, date_to))
            else:
                body.append(f"## Unknown section: {s}")
        except Exception as e:
            body.append(f"## Section `{s}` failed")
            body.append(f"")
            body.append(f"```")
            body.append(f"{type(e).__name__}: {e}")
            body.append(f"```")
        body.append("")
    return "\n".join(header + body)


def main() -> int:
    ap = argparse.ArgumentParser(description="Comprehensive FEF research dump.")
    ap.add_argument("date_from", help="YYYY-MM-DD")
    ap.add_argument("date_to", help="YYYY-MM-DD")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"DB path (default {DEFAULT_DB})")
    ap.add_argument("--sections", default=",".join(SECTIONS_ALL),
                    help=f"Comma-separated sections to include. Available: {','.join(SECTIONS_ALL)}")
    ap.add_argument("--account-class", default="paper",
                    help="Filter by extras.account_class. 'paper' (default), 'live', 'mock', "
                         "'unknown', 'all', or comma-separated. 'unknown' is treated as paper.")
    ap.add_argument("--out", help="Write markdown to this file instead of stdout")
    args = ap.parse_args()

    sections = [s.strip() for s in args.sections.split(",") if s.strip()]
    accept = _parse_account_class_arg(args.account_class)
    md = run_research_dump(args.date_from, args.date_to, args.db, sections, accept)

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
