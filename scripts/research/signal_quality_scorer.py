"""Per-signal feature-importance ranking.

You're already curating at fire-time — 131 signals fired but only ~84%
became live parents, and the curated set has positive edge while the
all-signals universe is mildly negative. This script asks: WHICH
features at fire time correlate with the outcome?

For each fired signal, this script:
  1. Extracts ALL numeric + categorical features from signal_iter.extras
     (drift_bias, fe_ratio, pa_score, walls, aggression, IV ranks,
      regime_at_fire, alt_direction_30m, hft_at_fire.*,
      microstructure_at_fire.*, mp_gravity, drift_score, etc.)
  2. Computes outcome R via order-preserving bar walk over forward
     SPX 1-min bars (same methodology as ablation/shadow).
  3. For each numeric feature: Pearson + Spearman correlation with R,
     AUC for binary "win > 0.05R" classification.
  4. For each categorical feature: per-category win rate + mean R.
  5. Top-quartile-filter sim: if we'd only fired signals in the top
     quartile by feature X, what's the aggregate sum_r vs all-signals?

Output: feature-importance markdown report ranked by abs(AUC - 0.5).

Read-only — no DB writes. Goal is to identify which features carry
information so post-freeze we can use them as a fire-time gate.

CLI:
    python signal_quality_scorer.py 2026-05-19 2026-05-21 --account-class paper
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPT_DIR))
from shadow_exit_replay import simulate_variant, _variant_by_label  # noqa: E402

DEFAULT_DB = str(Path.home() / ".fef-terminal" / "signal_log.db")


def _ts_to_ms(ts_str: str) -> int:
    dt = datetime.fromisoformat(ts_str.replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _parse_account_class_arg(raw: str) -> set[str]:
    if not raw:
        return {"paper"}
    raw = raw.strip().lower()
    if raw == "all":
        return {"paper", "live", "mock", "unknown"}
    return {x.strip() for x in raw.split(",") if x.strip()}


def _passes_account_filter(extras_raw, accept: set[str]) -> bool:
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


def _flatten_extras(ex: dict, prefix: str = "") -> dict:
    """Recursively flatten the extras JSON into dotted keys for feature
    extraction. Keeps numeric + categorical values; drops lists/dicts
    that don't reduce to scalars."""
    out: dict[str, object] = {}
    for k, v in (ex or {}).items():
        key = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
        if isinstance(v, (int, float, str, bool)) or v is None:
            out[key] = v
        elif isinstance(v, dict):
            out.update(_flatten_extras(v, key))
        # lists are dropped — most are wall arrays etc.
    return out


def _pearson(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    if n < 5:
        return None
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    dx = math.sqrt(sum((a - mx) ** 2 for a in x))
    dy = math.sqrt(sum((b - my) ** 2 for b in y))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _spearman(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    if n < 5:
        return None

    def rank(vals: list[float]) -> list[float]:
        indexed = sorted(range(n), key=lambda i: vals[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[indexed[j + 1]] == vals[indexed[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[indexed[k]] = avg
            i = j + 1
        return ranks

    return _pearson(rank(x), rank(y))


def _auc(scores: list[float], labels: list[int]) -> float | None:
    """ROC AUC for binary labels (1=win, 0=loss). O(n log n) via Mann-Whitney."""
    n = len(scores)
    if n < 10 or sum(labels) == 0 or sum(labels) == n:
        return None
    indexed = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[indexed[j + 1]] == scores[indexed[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg
        i = j + 1
    pos = sum(1 for lab in labels if lab == 1)
    neg = n - pos
    sum_ranks_pos = sum(ranks[i] for i in range(n) if labels[i] == 1)
    u = sum_ranks_pos - pos * (pos + 1) / 2
    return u / (pos * neg)


def run_scorer(
    date_from: str, date_to: str, db_path: str = DEFAULT_DB,
    accept_classes: set[str] | None = None,
) -> dict:
    accept = accept_classes or {"paper"}
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    sigs = cur.execute(
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
    sigs = [s for s in sigs if _passes_account_filter(s["extras"], accept)]

    bars = cur.execute(
        """
        SELECT timestamp_ms, open, high, low, close
        FROM historical_bar
        WHERE symbol='SPX' AND bar_size='1 min'
          AND timestamp_ms >= strftime('%s', ? || ' 00:00:00') * 1000 - 3600000
          AND timestamp_ms <  strftime('%s', ? || ' 23:59:59') * 1000 + 7200000
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

    baseline_be = _variant_by_label("baseline_partial_with_be_ladder")

    rows: list[dict] = []
    for s in sigs:
        sig_ms = _ts_to_ms(s["timestamp"])
        end_ms = sig_ms + 60 * 60 * 1000
        fwd = bars_in_window(sig_ms, end_ms)
        direction_sign = +1 if s["direction"] == "LONG_CALLS" else -1
        entry_spx = float(s["spx_price"])
        out = simulate_variant(entry_spx, direction_sign, fwd, baseline_be)
        try:
            ex = json.loads(s["extras"]) if s["extras"] else {}
        except json.JSONDecodeError:
            ex = {}
        features = _flatten_extras(ex)
        rows.append({
            "sig_id": s["id"], "session_date": s["session_date"],
            "direction": s["direction"], "entry_spx": entry_spx,
            "realized_r": out["realized_r"],
            "exit_reason": out["exit_reason"],
            "features": features,
        })

    # Build features index — set of all feature keys observed
    all_keys: set[str] = set()
    for r in rows:
        all_keys.update(r["features"].keys())

    # Per-feature analysis
    numeric_results: list[dict] = []
    categorical_results: list[dict] = []
    all_r = [r["realized_r"] for r in rows]
    all_win = [1 if r["realized_r"] > 0.05 else 0 for r in rows]
    aggregate_sum_r = sum(all_r)
    aggregate_n = len(all_r)

    for key in sorted(all_keys):
        vals_raw = [r["features"].get(key) for r in rows]
        # Try numeric path first
        numeric = []
        win_lab = []
        r_vals = []
        for i, v in enumerate(vals_raw):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                numeric.append(float(v))
                r_vals.append(rows[i]["realized_r"])
                win_lab.append(all_win[i])
        if len(numeric) >= 10:
            pearson = _pearson(numeric, r_vals)
            spearman = _spearman(numeric, r_vals)
            auc = _auc(numeric, win_lab)
            # Top quartile carve
            sorted_pairs = sorted(zip(numeric, r_vals), reverse=True)
            top_q = sorted_pairs[:max(1, len(sorted_pairs) // 4)]
            top_q_sum_r = sum(r for _, r in top_q)
            top_q_n = len(top_q)
            numeric_results.append({
                "feature": key,
                "n_observed": len(numeric),
                "pearson_r": pearson,
                "spearman_r": spearman,
                "auc": auc,
                "auc_signed_distance": (abs(auc - 0.5) if auc is not None else 0),
                "top_q_n": top_q_n,
                "top_q_sum_r": round(top_q_sum_r, 3),
                "top_q_mean_r": round(top_q_sum_r / top_q_n, 4) if top_q_n else 0,
                "all_mean_r": round(sum(r_vals) / len(r_vals), 4),
            })
        else:
            # Categorical path
            by_cat: dict[str, dict] = defaultdict(
                lambda: {"n": 0, "sum_r": 0.0, "wins": 0, "losses": 0}
            )
            for i, v in enumerate(vals_raw):
                if v is None:
                    cat = "(null)"
                else:
                    cat = str(v)[:32]
                by_cat[cat]["n"] += 1
                by_cat[cat]["sum_r"] += rows[i]["realized_r"]
                if rows[i]["realized_r"] > 0.05:
                    by_cat[cat]["wins"] += 1
                elif rows[i]["realized_r"] < -0.05:
                    by_cat[cat]["losses"] += 1
            if len(by_cat) <= 1:
                continue
            cats_list = []
            for cat, c in by_cat.items():
                if c["n"] < 5:
                    continue
                wl = c["wins"] + c["losses"]
                cats_list.append({
                    "value": cat, "n": c["n"],
                    "mean_r": round(c["sum_r"] / c["n"], 4),
                    "wr": round((c["wins"] / wl * 100) if wl else 0, 1),
                })
            if len(cats_list) >= 2:
                # Spread = max mean_r - min mean_r (proxy for category importance)
                spread = max(c["mean_r"] for c in cats_list) - min(c["mean_r"] for c in cats_list)
                categorical_results.append({
                    "feature": key,
                    "categories": cats_list,
                    "spread_mean_r": round(spread, 4),
                })

    # Sort numeric by abs AUC distance from 0.5
    numeric_results.sort(
        key=lambda x: (x["auc"] is not None, x["auc_signed_distance"]), reverse=True
    )
    categorical_results.sort(key=lambda x: x["spread_mean_r"], reverse=True)

    db.close()
    return {
        "date_from": date_from, "date_to": date_to,
        "n_signals": len(rows),
        "aggregate_mean_r": round(aggregate_sum_r / aggregate_n, 4) if aggregate_n else 0,
        "aggregate_sum_r": round(aggregate_sum_r, 2),
        "aggregate_win_rate_pct": round(sum(all_win) / len(all_win) * 100, 1) if all_win else 0,
        "numeric_features": numeric_results,
        "categorical_features": categorical_results,
    }


def format_report(result: dict) -> str:
    lines: list[str] = []
    lines.append(f"## Signal Quality Scorer: {result['date_from']} to {result['date_to']}")
    lines.append("")
    lines.append(
        f"- Fired signals analyzed: **{result['n_signals']}**, "
        f"aggregate sum R: {result['aggregate_sum_r']:+.2f}, "
        f"mean R: {result['aggregate_mean_r']:+.3f}, "
        f"WR: {result['aggregate_win_rate_pct']}%"
    )
    lines.append("")
    lines.append("### Numeric features ranked by predictive value")
    lines.append("")
    lines.append(
        "AUC = ROC AUC for binary 'win (>0.05R) vs loss (<-0.05R OR <=0.05R)'. "
        "AUC=0.5 means no predictive value; AUC>0.6 is meaningful. Top-quartile = "
        "if we kept only the top 25% of signals ranked by this feature, the "
        "sum_r and mean_r tells you how much edge that subset has vs the average."
    )
    lines.append("")
    lines.append("| Feature | n | Pearson | Spearman | AUC | Top-25% n | Top-25% sum R | Top-25% mean R | All mean R |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in result["numeric_features"][:25]:
        def _fmt(v: float | None, p: int = 3) -> str:
            return f"{v:+.{p}f}" if v is not None else "—"
        lines.append(
            f"| {r['feature']} | {r['n_observed']} | "
            f"{_fmt(r['pearson_r'])} | {_fmt(r['spearman_r'])} | "
            f"{_fmt(r['auc'])} | {r['top_q_n']} | "
            f"{r['top_q_sum_r']:+.2f} | {r['top_q_mean_r']:+.4f} | "
            f"{r['all_mean_r']:+.4f} |"
        )
    lines.append("")
    lines.append("### Categorical features ranked by mean-R spread across categories")
    lines.append("")
    for r in result["categorical_features"][:15]:
        lines.append(f"#### `{r['feature']}` (spread {r['spread_mean_r']:+.3f})")
        lines.append("")
        lines.append("| Category | n | mean R | WR |")
        lines.append("|---|---:|---:|---:|")
        for c in sorted(r["categories"], key=lambda x: x["mean_r"], reverse=True):
            lines.append(f"| {c['value']} | {c['n']} | {c['mean_r']:+.3f} | {c['wr']}% |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("date_from")
    ap.add_argument("date_to")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--account-class", default="paper")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    accept = _parse_account_class_arg(args.account_class)
    result = run_scorer(args.date_from, args.date_to, args.db, accept)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
