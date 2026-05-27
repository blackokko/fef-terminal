"""Paper-vs-real divergence decomposition for a session.

Per Audit priority #1 (2026-05-22, Chat round-2 audit). The 5/22 paper-vs-real
divergence is the smoking gun:

    expectancy_r = +0.157 (paper, from daily_review)
    actual_total_R = -5.63 (real, from broker fills)

on the same 46 actionable signals. This script decomposes that gap into
attributable factors so the post-freeze layer can be targeted at the
biggest leak:

    paper_R - real_R =
        (selection_gap)    actionable signal not turned into a LiveOrder
      + (slippage_gap)     LiveOrder filled at worse than paper-quoted mid
      + (timing_gap)       LiveOrder fired late / paper bar-walk hit T1 first
      + (commission_gap)   IBKR fees not modeled in paper expectancy

Decomposition methodology:

  1. **Per-signal pairing.** Each actionable LONG_CALLS / LONG_PUTS signal
     is matched to the closest LiveOrder parent within ±90s (same right,
     same session). Unmatched signals contribute to `selection_gap` only.

  2. **Paper R per signal.** Uses the order-preserving bar walk
     (`_simulate_partial_walk` from realistic_fill_estimator) with the
     signal's stored t1/t2/t3/stop from extras and the engine's 50/30/20
     scale-out. This is the SAME bar walk used by `_simulate_baseline` in
     ablation_backtest and by the realistic-fill estimator's paper leg, so
     paper R here = daily_review.expectancy_r contributing per-signal.

  3. **Real R per signal.** When a signal matched a parent, real R is
     derived from the contract-group pair-off (per
     `audit_package_2/11_methodology/pair_off.py`) pro-rated to the parent
     by its BOT-share. Realized $ / R_dollars => real R.

  4. **Slippage gap.** Difference between parent.avg_fill_price and the
     option_pricing.entry_limit from signal extras (paper-quoted entry).

  5. **Timing gap.** Paper bar walk starts from signal.timestamp; live
     parent.submitted_at lags by `parent_lag_s`. If the bar walked from
     parent's submit time gives a different R than from signal time, that
     delta is the timing component.

  6. **Commission gap.** Per IBKR paper quirk (see
     audit_package_2/11_methodology/pair_off.py): paper-mode TWS sends
     commission=0.00 via API even though UI shows $1.29/contract. The
     decomposition charges $1.29 × shares per fill on the real leg only
     (paper leg is already commission-free). Total commission per session
     contributes to commission_gap.

Sources:
  - Paper side: signal_iter + signal_lookahead_outcome + bar-walk simulator.
  - Real side: live_order + ibkr_execution (post-nightly).
  - Pair-off formula: audit_package_2/11_methodology/pair_off.py.

CLI:
    python paper_vs_real_decomposition.py 2026-05-22
    python paper_vs_real_decomposition.py 2026-05-22 --json
    python paper_vs_real_decomposition.py 2026-05-22 --out path/to/report.md

Default output:
  - stdout: markdown report.
  - audit_package_2/15_paper_vs_real_<date>.md: same markdown report.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPT_DIR))
from realistic_fill_estimator import (  # noqa: E402
    COMMISSION_PER_FILL_PER_CONTRACT,
    PREMIUM_AT_RISK_FRACTION,
    _simulate_partial_walk,
    _ts_to_ms,
    _extras_dict,
)

DEFAULT_DB = str(Path.home() / ".fef-terminal" / "signal_log.db")
AUDIT_PACKAGE_DIR = Path(__file__).parent.parent.parent / "audit_package_2"
DEFAULT_OUT_NAME_TEMPLATE = "15_paper_vs_real_{date_underscored}.md"

PARENT_MATCH_WINDOW_SEC = 90      # signal -> parent timing tolerance
WALK_HORIZON_MINUTES = 60         # bar walk horizon (matches engine's 60-min target window)
SLIPPAGE_CAP_DOLLARS = 200.0      # per-trade slippage cap (sanity bound on $ derivation)


# =============================================================================
# DATA PULLS
# =============================================================================

def _pull_signals(cur, date: str) -> list[dict]:
    """Actionable LONG_CALLS / LONG_PUTS signals for the session."""
    rows = cur.execute(
        """
        SELECT id, session_date, timestamp, direction, confidence, spx_price, extras
        FROM signal_iter
        WHERE session_date = ?
          AND direction IN ('LONG_CALLS', 'LONG_PUTS')
          AND spx_price BETWEEN 1000 AND 10000
        ORDER BY timestamp
        """,
        (date,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["timestamp_ms"] = _ts_to_ms(d["timestamp"])
        d["extras_dict"] = _extras_dict(d["extras"])
        out.append(d)
    return out


def _pull_parents(cur, date: str) -> list[dict]:
    """Non-dry-run parent live_orders for the session."""
    rows = cur.execute(
        """
        SELECT id AS parent_id, submitted_at, last_status_at, ticket_id,
               ib_order_id, symbol, strike, right, expiry, qty, filled_qty,
               status, avg_fill_price, limit_price, entry_spx_price,
               spx_target_t1, spx_target_t2, spx_target_t3, spx_target_stop
        FROM live_order
        WHERE bracket_role = 'parent' AND dry_run = 0
          AND substr(submitted_at, 1, 10) = ?
        ORDER BY submitted_at
        """,
        (date,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["submitted_ms"] = _ts_to_ms(d["submitted_at"])
        out.append(d)
    return out


def _pull_executions(cur, date: str) -> list[dict]:
    """All ibkr_executions for the session, ordered by timestamp."""
    rows = cur.execute(
        """
        SELECT exec_id, timestamp, symbol, strike, right, expiry, side,
               shares, price, ib_order_id, commission, account
        FROM ibkr_execution
        WHERE session_date = ?
        ORDER BY timestamp
        """,
        (date,),
    ).fetchall()
    return [dict(r) for r in rows]


def _pull_bars(cur, date: str) -> tuple[dict[int, dict], list[int]]:
    """Pull SPX 1-min bars covering the session window + buffer."""
    rows = cur.execute(
        """
        SELECT timestamp_ms, open, high, low, close
        FROM historical_bar
        WHERE symbol='SPX' AND bar_size='1 min'
          AND timestamp_ms >= strftime('%s', ? || ' 00:00:00') * 1000 - 3600000
          AND timestamp_ms <  strftime('%s', ? || ' 23:59:59') * 1000 + 7200000
        ORDER BY timestamp_ms
        """,
        (date, date),
    ).fetchall()
    bar_by_ms = {b["timestamp_ms"]: dict(b) for b in rows}
    return bar_by_ms, sorted(bar_by_ms.keys())


# =============================================================================
# MATCHING
# =============================================================================

def _match_signal_to_parent(
    sig: dict, parents_by_right: dict[str, list[dict]],
    window_s: int = PARENT_MATCH_WINDOW_SEC,
) -> dict | None:
    """Find the closest parent within ±window_s of signal.timestamp."""
    right_target = "C" if sig["direction"] == "LONG_CALLS" else "P"
    candidates = parents_by_right.get(right_target, [])
    best = None
    best_dt = float(window_s)
    for p in candidates:
        dt = abs(p["submitted_ms"] - sig["timestamp_ms"]) / 1000.0
        if dt < best_dt:
            best = p
            best_dt = dt
    return best, (best_dt if best else None)


# =============================================================================
# PER-SIGNAL DECOMPOSITION
# =============================================================================

def _bars_in_window(start_ms: int, end_ms: int,
                    bar_by_ms: dict[int, dict],
                    bar_ms_sorted: list[int]) -> list[dict]:
    lo = bisect.bisect_left(bar_ms_sorted, start_ms)
    hi = bisect.bisect_right(bar_ms_sorted, end_ms)
    return [bar_by_ms[m] for m in bar_ms_sorted[lo:hi]]


def _per_contract_pair_off(execs: list[dict]) -> dict[tuple, dict]:
    """Per-contract pair-off (verbatim from
    audit_package_2/11_methodology/pair_off.py). Returns map
    (symbol, strike, right, expiry) -> {realized_dollars, bot_qty, sld_qty,
    avg_bot, avg_sld, paired_qty}."""
    by_contract: dict[tuple, dict[str, float]] = {}
    for e in execs:
        key = (e["symbol"], float(e["strike"] or 0), e["right"], e["expiry"])
        slot = by_contract.setdefault(
            key, {"bot_v": 0.0, "sld_v": 0.0, "bot_q": 0.0, "sld_q": 0.0}
        )
        side = (e["side"] or "").upper()
        sh = float(e["shares"] or 0)
        pr = float(e["price"] or 0)
        if side == "BOT":
            slot["bot_v"] += sh * pr
            slot["bot_q"] += sh
        elif side == "SLD":
            slot["sld_v"] += sh * pr
            slot["sld_q"] += sh
    out: dict[tuple, dict] = {}
    for key, slot in by_contract.items():
        bot_q, sld_q = slot["bot_q"], slot["sld_q"]
        if bot_q == 0 and sld_q == 0:
            continue
        paired = min(bot_q, sld_q)
        avg_bot = slot["bot_v"] / bot_q if bot_q > 0 else 0.0
        avg_sld = slot["sld_v"] / sld_q if sld_q > 0 else 0.0
        row_realized = (avg_sld - avg_bot) * paired * 100 if paired > 0 else 0.0
        out[key] = {
            "realized_dollars": row_realized,
            "bot_qty": bot_q, "sld_qty": sld_q, "paired_qty": paired,
            "avg_bot": avg_bot, "avg_sld": avg_sld,
        }
    return out


def _parent_real_r(
    parent: dict, parent_execs: list[dict],
    contract_groups: dict[tuple, dict],
    r_dollars: float,
) -> tuple[float, float, dict]:
    """Compute (real_R, real_dollars, detail) for a single parent.

    Real $ = group_realized * (this parent's BOT-share within the contract
    group). Real R = real_$ / r_dollars (1R-equivalent for this signal).
    Commission charged at $1.29/share for this parent's execution rows
    that have commission == 0 or NULL (paper quirk fallback).
    """
    key = (parent["symbol"], float(parent["strike"] or 0),
           parent["right"], parent["expiry"])
    group = contract_groups.get(key)
    if group is None:
        return 0.0, 0.0, {"reason": "no_contract_group"}
    bot_execs = [e for e in parent_execs
                 if (e["side"] or "").upper() == "BOT"
                 and e["ib_order_id"] == parent["ib_order_id"]]
    parent_bot_q = sum(float(e["shares"] or 0) for e in bot_execs)
    group_bot_total = group["bot_qty"]
    if group_bot_total <= 0 or parent_bot_q <= 0:
        return 0.0, 0.0, {"reason": "no_bot_match", "group_bot": group_bot_total,
                          "parent_bot": parent_bot_q}
    share = parent_bot_q / group_bot_total
    parent_real_dollars = group["realized_dollars"] * share
    # Commission for this parent's fills (BOT side only — entry leg fills).
    # The contract group's SLD side may have additional commission but
    # we don't attribute the exit-side commission per-parent here (it's
    # already netted into the group realized via fills).
    commission_dollars = 0.0
    for e in bot_execs:
        real_comm = e["commission"]
        if real_comm is not None and float(real_comm) > 0:
            commission_dollars += float(real_comm)
        else:
            commission_dollars += float(e["shares"] or 0) * COMMISSION_PER_FILL_PER_CONTRACT
    # Real R after commission (commission is the OUT-leg loss, modeled as
    # subtracting from real_$ on entry side here for the per-parent view).
    # Note: real_dollars already reflects realized (avg_sld - avg_bot) * paired,
    # which is BEFORE commission. To get net-of-commission $: real_dollars - commission.
    real_dollars_net = parent_real_dollars - commission_dollars
    real_r_gross = parent_real_dollars / r_dollars if r_dollars > 0 else 0.0
    real_r_net = real_dollars_net / r_dollars if r_dollars > 0 else 0.0
    return real_r_gross, real_r_net, {
        "parent_bot": parent_bot_q,
        "group_bot": group_bot_total,
        "share": round(share, 4),
        "group_realized_dollars": round(group["realized_dollars"], 2),
        "parent_real_dollars": round(parent_real_dollars, 2),
        "commission_dollars": round(commission_dollars, 2),
        "real_dollars_net": round(real_dollars_net, 2),
    }


# =============================================================================
# MAIN DECOMPOSITION
# =============================================================================

def run_decomposition(date: str, db_path: str = DEFAULT_DB) -> dict:
    """Decompose paper-vs-real R for a session into attributable factors.

    Returns a dict with per-signal rows, per-bucket aggregates, and total
    gap decomposition.
    """
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    signals = _pull_signals(cur, date)
    parents = _pull_parents(cur, date)
    executions = _pull_executions(cur, date)
    bar_by_ms, bar_ms_sorted = _pull_bars(cur, date)

    # Group parents by right for matching
    parents_by_right: dict[str, list[dict]] = defaultdict(list)
    for p in parents:
        parents_by_right[p["right"]].append(p)

    # Per-contract pair-off
    contract_groups = _per_contract_pair_off(executions)

    # Per-signal decomposition rows
    decomposition: list[dict] = []
    for sig in signals:
        ex = sig["extras_dict"]
        op = ex.get("option_pricing") or {}
        regime = ex.get("regime_at_fire") or "NORMAL"
        # Hour of session (UTC for portability — caller can convert)
        hr_utc = sig["timestamp"][11:13] if sig["timestamp"] else "??"
        direction_sign = +1 if sig["direction"] == "LONG_CALLS" else -1
        entry_spx = float(sig["spx_price"])
        t1 = ex.get("t1")
        t2 = ex.get("t2")
        t3 = ex.get("t3")
        stop = ex.get("stop")
        if t1 is None or t2 is None or t3 is None or stop is None:
            # Pre-extras-targets signals — can't bar-walk paper R.
            decomposition.append({
                "sid": sig["id"], "timestamp": sig["timestamp"],
                "direction": sig["direction"], "regime": regime,
                "hour_utc": hr_utc,
                "skip_reason": "missing_targets_in_extras",
            })
            continue

        # ---- PAPER R (bar walk from signal.timestamp) ----
        fwd = _bars_in_window(
            sig["timestamp_ms"], sig["timestamp_ms"] + WALK_HORIZON_MINUTES * 60 * 1000,
            bar_by_ms, bar_ms_sorted,
        )
        paper_r, paper_reason, paper_tiers = _simulate_partial_walk(
            entry_spx, direction_sign, float(t1), float(t2), float(t3), float(stop), fwd,
        )

        # ---- Match to parent ----
        parent, parent_lag_s = _match_signal_to_parent(sig, parents_by_right)
        if parent is None:
            # Selection gap: signal never became a LiveOrder. Paper R is
            # realized, real R is 0. Contributes 100% to selection_gap.
            decomposition.append({
                "sid": sig["id"], "timestamp": sig["timestamp"],
                "direction": sig["direction"], "regime": regime,
                "hour_utc": hr_utc,
                "paper_r": paper_r, "paper_exit_reason": paper_reason,
                "paper_tiers": paper_tiers,
                "matched_parent_id": None,
                "real_r_gross": 0.0, "real_r_net": 0.0,
                "selection_gap_r": paper_r,    # the full paper-R is "unmatched"
                "slippage_gap_r": 0.0,
                "timing_gap_r": 0.0,
                "commission_gap_r": 0.0,
                "category": "selection",
            })
            continue

        # ---- R-dollars sizing per signal ----
        qty = parent.get("filled_qty") or parent.get("qty") or 0
        entry_limit_paper = op.get("entry_limit")    # paper-quoted entry mid
        entry_fill_real = parent.get("avg_fill_price")  # actual fill premium
        if entry_limit_paper and entry_limit_paper > 0:
            r_dollars = float(qty) * float(entry_limit_paper) * 100.0 * PREMIUM_AT_RISK_FRACTION
        elif entry_fill_real and entry_fill_real > 0:
            r_dollars = float(qty) * float(entry_fill_real) * 100.0 * PREMIUM_AT_RISK_FRACTION
        else:
            r_dollars = 0.0

        # ---- REAL R from parent's pair-off share ----
        parent_execs = [e for e in executions
                        if e["ib_order_id"] == parent["ib_order_id"]]
        real_r_gross, real_r_net, real_detail = _parent_real_r(
            parent, parent_execs, contract_groups, r_dollars,
        )

        # ---- TIMING gap: paper bar walk from PARENT's submit time ----
        parent_fwd = _bars_in_window(
            parent["submitted_ms"], parent["submitted_ms"] + WALK_HORIZON_MINUTES * 60 * 1000,
            bar_by_ms, bar_ms_sorted,
        )
        # Use parent's actual entry_spx_price when available, else signal's
        # spx_price as a fallback.
        entry_spx_at_parent = parent.get("entry_spx_price") or entry_spx
        paper_r_at_parent_time, _, _ = _simulate_partial_walk(
            float(entry_spx_at_parent), direction_sign,
            float(t1), float(t2), float(t3), float(stop), parent_fwd,
        )
        timing_gap_r = paper_r - paper_r_at_parent_time

        # ---- SLIPPAGE gap: difference in entry price (paper mid vs real fill) ----
        # Convert dollar slippage to R units by dividing by r_dollars.
        # Sign convention: if real fill > paper quote (paying more on entry),
        # we lose; slippage_gap_r is POSITIVE (paper better than real by this much).
        if (entry_limit_paper and entry_limit_paper > 0
                and entry_fill_real and entry_fill_real > 0
                and qty > 0 and r_dollars > 0):
            slippage_dollars = (float(entry_fill_real) - float(entry_limit_paper)) * float(qty) * 100.0
            # Cap to avoid extreme outliers from missing data
            slippage_dollars_capped = max(min(slippage_dollars, SLIPPAGE_CAP_DOLLARS * qty),
                                          -SLIPPAGE_CAP_DOLLARS * qty)
            slippage_gap_r = slippage_dollars_capped / r_dollars
        else:
            slippage_gap_r = 0.0

        # ---- COMMISSION gap: pure cost on real side, zero on paper side ----
        commission_dollars = real_detail.get("commission_dollars", 0.0) or 0.0
        commission_gap_r = commission_dollars / r_dollars if r_dollars > 0 else 0.0

        # ---- Residual = paper - real - (slippage + timing + commission)
        # is the "modeling residual" that we attribute to the bar-walk
        # paper estimator vs actual exit execution (BE ladder, T1/T2/T3
        # late fires, theta, etc.). For accounting, we report it
        # separately so the four components sum to the total gap exactly
        # by construction.
        total_gap_r = paper_r - real_r_net  # what we're decomposing
        residual_gap_r = (
            total_gap_r - slippage_gap_r - timing_gap_r - commission_gap_r
        )

        decomposition.append({
            "sid": sig["id"], "timestamp": sig["timestamp"],
            "direction": sig["direction"], "regime": regime,
            "hour_utc": hr_utc,
            "matched_parent_id": parent["parent_id"],
            "parent_lag_s": round(parent_lag_s, 1),
            "paper_r": round(paper_r, 4),
            "paper_exit_reason": paper_reason,
            "paper_tiers": paper_tiers,
            "real_r_gross": round(real_r_gross, 4),
            "real_r_net": round(real_r_net, 4),
            "r_dollars": round(r_dollars, 2),
            "entry_limit_paper": entry_limit_paper,
            "entry_fill_real": entry_fill_real,
            "qty": qty,
            "total_gap_r": round(total_gap_r, 4),
            "selection_gap_r": 0.0,
            "slippage_gap_r": round(slippage_gap_r, 4),
            "timing_gap_r": round(timing_gap_r, 4),
            "commission_gap_r": round(commission_gap_r, 4),
            "residual_gap_r": round(residual_gap_r, 4),
            "category": "execution",
            "real_detail": real_detail,
        })

    # =========================================================================
    # AGGREGATES
    # =========================================================================
    n_signals = len(signals)
    n_matched = sum(1 for d in decomposition if d.get("matched_parent_id"))
    n_unmatched = sum(1 for d in decomposition if d.get("category") == "selection")
    n_skipped = sum(1 for d in decomposition if d.get("skip_reason"))

    sum_paper_r = sum(d.get("paper_r", 0.0) for d in decomposition if "paper_r" in d)
    sum_real_r_gross = sum(d.get("real_r_gross", 0.0) for d in decomposition if "real_r_gross" in d)
    sum_real_r_net = sum(d.get("real_r_net", 0.0) for d in decomposition if "real_r_net" in d)
    sum_selection = sum(d.get("selection_gap_r", 0.0) for d in decomposition)
    sum_slippage = sum(d.get("slippage_gap_r", 0.0) for d in decomposition)
    sum_timing = sum(d.get("timing_gap_r", 0.0) for d in decomposition)
    sum_commission = sum(d.get("commission_gap_r", 0.0) for d in decomposition)
    sum_residual = sum(d.get("residual_gap_r", 0.0) for d in decomposition)
    total_gap_r = sum_paper_r - sum_real_r_net

    # Bucket by regime
    by_regime: dict[str, dict] = defaultdict(
        lambda: dict(n=0, paper=0.0, real_net=0.0, selection=0.0,
                     slippage=0.0, timing=0.0, commission=0.0, residual=0.0)
    )
    for d in decomposition:
        regime = d.get("regime", "NORMAL")
        b = by_regime[regime]
        b["n"] += 1
        b["paper"] += d.get("paper_r", 0.0) or 0.0
        b["real_net"] += d.get("real_r_net", 0.0) or 0.0
        b["selection"] += d.get("selection_gap_r", 0.0) or 0.0
        b["slippage"] += d.get("slippage_gap_r", 0.0) or 0.0
        b["timing"] += d.get("timing_gap_r", 0.0) or 0.0
        b["commission"] += d.get("commission_gap_r", 0.0) or 0.0
        b["residual"] += d.get("residual_gap_r", 0.0) or 0.0

    # Bucket by hour-of-session (UTC). Caller can re-bucket to CT in viz.
    by_hour: dict[str, dict] = defaultdict(
        lambda: dict(n=0, paper=0.0, real_net=0.0, selection=0.0,
                     slippage=0.0, timing=0.0, commission=0.0, residual=0.0)
    )
    for d in decomposition:
        hr = d.get("hour_utc", "??")
        b = by_hour[hr]
        b["n"] += 1
        b["paper"] += d.get("paper_r", 0.0) or 0.0
        b["real_net"] += d.get("real_r_net", 0.0) or 0.0
        b["selection"] += d.get("selection_gap_r", 0.0) or 0.0
        b["slippage"] += d.get("slippage_gap_r", 0.0) or 0.0
        b["timing"] += d.get("timing_gap_r", 0.0) or 0.0
        b["commission"] += d.get("commission_gap_r", 0.0) or 0.0
        b["residual"] += d.get("residual_gap_r", 0.0) or 0.0

    # Cross-check vs daily_review for sanity
    dr = cur.execute(
        """
        SELECT actionable_signals, expectancy_r, dedup_expectancy_r,
               actual_trades_taken, actual_wins, actual_losses, actual_total_R
        FROM daily_review WHERE session_date = ?
        """,
        (date,),
    ).fetchone()
    daily_review_row = dict(dr) if dr else {}

    return {
        "date": date,
        "n_signals": n_signals,
        "n_matched": n_matched,
        "n_unmatched": n_unmatched,
        "n_skipped": n_skipped,
        "n_parents_total": len(parents),
        "n_executions_total": len(executions),
        # Sums
        "sum_paper_r": round(sum_paper_r, 4),
        "sum_real_r_gross": round(sum_real_r_gross, 4),
        "sum_real_r_net": round(sum_real_r_net, 4),
        "total_gap_r": round(total_gap_r, 4),
        # Decomposition components
        "selection_gap_r": round(sum_selection, 4),
        "slippage_gap_r": round(sum_slippage, 4),
        "timing_gap_r": round(sum_timing, 4),
        "commission_gap_r": round(sum_commission, 4),
        "residual_gap_r": round(sum_residual, 4),
        # Side-by-side with daily_review
        "daily_review": daily_review_row,
        # Per-bucket aggregates
        "by_regime": {k: {**v, "paper": round(v["paper"], 4),
                          "real_net": round(v["real_net"], 4),
                          "selection": round(v["selection"], 4),
                          "slippage": round(v["slippage"], 4),
                          "timing": round(v["timing"], 4),
                          "commission": round(v["commission"], 4),
                          "residual": round(v["residual"], 4)}
                      for k, v in by_regime.items()},
        "by_hour": {k: {**v, "paper": round(v["paper"], 4),
                        "real_net": round(v["real_net"], 4),
                        "selection": round(v["selection"], 4),
                        "slippage": round(v["slippage"], 4),
                        "timing": round(v["timing"], 4),
                        "commission": round(v["commission"], 4),
                        "residual": round(v["residual"], 4)}
                    for k, v in by_hour.items()},
        # Per-signal detail
        "per_signal": decomposition,
    }


# =============================================================================
# REPORT
# =============================================================================

def format_report(result: dict) -> str:
    """Markdown report. Mirrors the audit_package_2 numbering."""
    lines: list[str] = []
    date = result["date"]
    lines.append(f"# Paper-vs-real decomposition — {date}")
    lines.append("")
    lines.append(
        "Per Audit priority #1 (2026-05-22, Chat round-2 audit). Decomposes the "
        "paper-vs-real R divergence into four attributable factors: **selection** "
        "(signal not turned into order), **slippage** (fill worse than paper "
        "quote), **timing** (parent submitted late vs signal time), and "
        "**commission** (IBKR fees on real side only). The four sum to the "
        "total gap plus a **residual** (unmodeled execution detail: BE ladder, "
        "partial-fill timing, theta on the live side, etc.)."
    )
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append(f"- **Signals analyzed**: {result['n_signals']} actionable LONG_CALLS/LONG_PUTS")
    lines.append(f"- **Matched to live parent**: {result['n_matched']}/{result['n_signals']} "
                 f"({100 * result['n_matched'] / max(result['n_signals'], 1):.1f}%)")
    lines.append(f"- **Unmatched (selection gap)**: {result['n_unmatched']}")
    if result.get("n_skipped"):
        lines.append(f"- **Skipped (missing targets in extras)**: {result['n_skipped']}")
    lines.append(f"- **Live parent orders**: {result['n_parents_total']}")
    lines.append(f"- **IBKR fills**: {result['n_executions_total']}")
    lines.append("")
    dr = result.get("daily_review", {})
    lines.append("## Cross-check vs daily_review")
    lines.append("")
    lines.append("| Metric | This decomposition | daily_review row |")
    lines.append("|---|---:|---:|")
    lines.append(
        f"| Actionable signals | {result['n_signals']} | "
        f"{dr.get('actionable_signals', '—')} |"
    )
    lines.append(
        f"| Sum paper R | {result['sum_paper_r']:+.4f} | "
        f"expectancy_r × n = "
        f"{(dr.get('expectancy_r', 0) or 0) * (dr.get('actionable_signals', 0) or 0):+.4f} |"
    )
    lines.append(
        f"| Sum real R (net) | {result['sum_real_r_net']:+.4f} | "
        f"actual_total_R = {dr.get('actual_total_R', '—')} |"
    )
    lines.append(
        f"| Sum real R (gross) | {result['sum_real_r_gross']:+.4f} | "
        f"(pre-commission) |"
    )
    lines.append("")
    lines.append("## Total gap decomposition")
    lines.append("")
    total_gap = result["total_gap_r"]
    lines.append(f"**Paper R − Real R (net)** = `{result['sum_paper_r']:+.4f}` − "
                 f"`{result['sum_real_r_net']:+.4f}` = **`{total_gap:+.4f}` R**")
    lines.append("")
    lines.append("| Component | R | % of total gap |")
    lines.append("|---|---:|---:|")
    for label, key in [
        ("Selection gap (unmatched signals' paper R)", "selection_gap_r"),
        ("Slippage gap (entry mid vs real fill)", "slippage_gap_r"),
        ("Timing gap (parent submitted later than signal)", "timing_gap_r"),
        ("Commission gap ($1.29/share on real side only)", "commission_gap_r"),
        ("Residual (unmodeled: BE ladder, exit timing, theta…)", "residual_gap_r"),
    ]:
        val = result.get(key, 0.0)
        pct = (val / total_gap * 100) if total_gap else 0.0
        lines.append(f"| {label} | `{val:+.4f}` | {pct:+.1f}% |")
    lines.append("")
    lines.append(
        "_The five rows sum to the total gap by construction. Selection is "
        "100% gap (paper assumes the signal traded; real didn't), "
        "Commission is 100% loss (paper has no fees). Slippage and timing "
        "can be positive or negative depending on direction. Residual is "
        "the catch-all for everything not modeled by the above three — "
        "BE-ladder partial exits, T1/T2/T3 late fires, theta on the live "
        "leg, peak-trail differences, etc._"
    )
    lines.append("")
    lines.append("## Per-regime concentration")
    lines.append("")
    lines.append("| Regime | n | Paper R | Real R net | Sel | Slip | Time | Comm | Resid |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for regime in sorted(result["by_regime"].keys()):
        b = result["by_regime"][regime]
        lines.append(
            f"| {regime} | {b['n']} | {b['paper']:+.3f} | {b['real_net']:+.3f} | "
            f"{b['selection']:+.3f} | {b['slippage']:+.3f} | "
            f"{b['timing']:+.3f} | {b['commission']:+.3f} | "
            f"{b['residual']:+.3f} |"
        )
    lines.append("")
    lines.append("## Per-hour concentration (UTC)")
    lines.append("")
    lines.append("| Hour | n | Paper R | Real R net | Sel | Slip | Time | Comm | Resid |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for hr in sorted(result["by_hour"].keys()):
        b = result["by_hour"][hr]
        lines.append(
            f"| {hr}:00 | {b['n']} | {b['paper']:+.3f} | {b['real_net']:+.3f} | "
            f"{b['selection']:+.3f} | {b['slippage']:+.3f} | "
            f"{b['timing']:+.3f} | {b['commission']:+.3f} | "
            f"{b['residual']:+.3f} |"
        )
    lines.append("")
    lines.append("## Per-signal detail (top 15 worst gaps)")
    lines.append("")
    sigs = [d for d in result["per_signal"]
            if d.get("category") == "execution" and "total_gap_r" in d]
    sigs.sort(key=lambda d: abs(d.get("total_gap_r", 0.0)), reverse=True)
    lines.append("| Sid | Time | Dir | Regime | Paper R | Real R (net) | Gap | Sel | Slip | Time | Comm | Resid |")
    lines.append("|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for d in sigs[:15]:
        lines.append(
            f"| {d['sid']} | {d['timestamp'][11:19]} | {d['direction']} | "
            f"{d['regime']} | {d['paper_r']:+.3f} | {d['real_r_net']:+.3f} | "
            f"{d['total_gap_r']:+.3f} | {d['selection_gap_r']:+.3f} | "
            f"{d['slippage_gap_r']:+.3f} | {d['timing_gap_r']:+.3f} | "
            f"{d['commission_gap_r']:+.3f} | {d['residual_gap_r']:+.3f} |"
        )
    lines.append("")
    lines.append("### Unmatched (selection-gap) signals")
    lines.append("")
    unmatched = [d for d in result["per_signal"] if d.get("category") == "selection"]
    if unmatched:
        lines.append("| Sid | Time | Dir | Regime | Paper R | Reason |")
        lines.append("|---:|---|---|---|---:|---|")
        for d in unmatched:
            lines.append(
                f"| {d['sid']} | {d['timestamp'][11:19]} | {d['direction']} | "
                f"{d['regime']} | {d.get('paper_r', 0):+.3f} | "
                f"no parent within {PARENT_MATCH_WINDOW_SEC}s |"
            )
    else:
        lines.append("_(none — every signal matched a parent within ±90s)_")
    lines.append("")
    lines.append("## Methodology notes")
    lines.append("")
    lines.append(
        "- **Pair-off formula**: see "
        "`audit_package_2/11_methodology/pair_off.py`. Per-contract "
        "`(avg_sld - avg_bot) * paired * 100` with paper-quirk commission "
        "fallback ($1.29/share when ibkr_execution.commission == 0)."
    )
    lines.append(
        "- **Paper bar walk**: `realistic_fill_estimator._simulate_partial_walk` "
        "(50/30/20 partial scale-out, stop priority on ambiguous bars, "
        "60-min timeout). Same routine that produces "
        "`daily_review.dedup_expectancy_r`."
    )
    lines.append(
        "- **R-dollars sizing**: `qty × entry_limit × 100 × 0.5` per signal "
        "(per `realistic_fill_estimator.PREMIUM_AT_RISK_FRACTION`). "
        "Uses option_pricing.entry_limit from extras; falls back to "
        "parent.avg_fill_price if missing."
    )
    lines.append(
        "- **Timing gap definition**: paper R from bar walk starting at "
        "signal.timestamp MINUS paper R from bar walk starting at "
        "parent.submitted_at. Positive = signal saw favorable move before "
        "parent submitted (engine reacted too slowly)."
    )
    lines.append(
        "- **Slippage gap definition**: "
        "`(parent.avg_fill_price − signal.option_pricing.entry_limit) "
        "× qty × 100 / r_dollars`. Capped at ±$200 × qty per trade to "
        "bound outliers from missing-data rows."
    )
    lines.append(
        f"- **Parent matching window**: ±{PARENT_MATCH_WINDOW_SEC}s of "
        "signal.timestamp, same direction (LONG_CALLS→C, LONG_PUTS→P). "
        "Unmatched signals (~40% on 5/22) attribute their full paper R "
        "to the selection gap."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("session_date", help="YYYY-MM-DD")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true",
                    help="Emit raw JSON to stdout instead of markdown")
    ap.add_argument("--out", default=None,
                    help="Write markdown report to this path (also still prints to stdout). "
                         "Default: audit_package_2/15_paper_vs_real_<date>.md")
    args = ap.parse_args()

    result = run_decomposition(args.session_date, args.db)

    # Windows cp1252 stdout chokes on extended chars in the markdown.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    if args.json:
        # Drop the per-trade list for stdout JSON brevity unless asked
        print(json.dumps({k: v for k, v in result.items() if k != "per_signal"},
                         indent=2, default=str))
    else:
        report = format_report(result)
        print(report)
        # Also write to audit_package_2 by default
        out_path = args.out
        if out_path is None:
            date_underscored = args.session_date.replace("-", "_")
            out_path = AUDIT_PACKAGE_DIR / DEFAULT_OUT_NAME_TEMPLATE.format(
                date_underscored=date_underscored,
            )
        out_p = Path(out_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(report, encoding="utf-8")
        print(f"\n_Report also written to: {out_p}_")
    return 0


if __name__ == "__main__":
    sys.exit(main())
