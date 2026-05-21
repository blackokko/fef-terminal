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


def run_ablation(date_from: str, date_to: str, db_path: str = DEFAULT_DB) -> dict:
    """Returns a structured dict with per-trade rows, per-variant aggregates,
    and per-day breakdown."""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    signals = cur.execute(
        """
        SELECT id, session_date, timestamp, direction, spx_price
        FROM signal_iter
        WHERE session_date BETWEEN ? AND ?
          AND direction IN ('LONG_CALLS','LONG_PUTS')
          AND spx_price BETWEEN 1000 AND 10000
        ORDER BY session_date, timestamp
        """,
        (date_from, date_to),
    ).fetchall()

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
            "entry_spx": entry_spx, "spx_30m_change": round(spx_30m_change, 2),
            "baseline_r": baseline_r,
            "direction_filter_r": 0.0 if blocked else baseline_r,
            "bcd_r": bcd_r,
            "combined_r": 0.0 if blocked else bcd_r,
            "blocked_by_dir_filter": blocked,
            "bcd_reason": bcd_reason,
        })

    def summary(rows: list[dict], key: str) -> dict:
        totals: dict[str, dict] = defaultdict(
            lambda: {"n": 0, "sum_r": 0.0, "wins": 0, "losses": 0, "flat": 0}
        )
        for r in rows:
            v = r[key]
            for bucket in (r["date"], "ALL"):
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

    return {
        "date_from": date_from,
        "date_to": date_to,
        "n_signals": len(results),
        "n_blocked_by_dir_filter": sum(1 for r in results if r["blocked_by_dir_filter"]),
        "bcd_exit_reasons": dict(Counter(r["bcd_reason"] for r in results)),
        "aggregates": aggregates,
        "trades": results,
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
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Ablation backtest for FEF post-freeze candidates.")
    ap.add_argument("date_from", help="YYYY-MM-DD")
    ap.add_argument("date_to", help="YYYY-MM-DD")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true", help="Emit raw JSON instead of markdown")
    ap.add_argument("--dollars-per-r", type=float, default=DOLLARS_PER_R)
    args = ap.parse_args()

    result = run_ablation(args.date_from, args.date_to, args.db)
    if args.json:
        # Drop the per-trade list for stdout JSON brevity unless asked
        print(json.dumps({k: v for k, v in result.items() if k != "trades"}, indent=2, default=str))
    else:
        print(format_report(result, args.dollars_per_r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
