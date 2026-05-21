"""Realistic-fill P/L estimator — paper theoretical vs IBKR-fill ground truth.

Measures the empirical paper-to-real haircut per session. Tonight's 5/21
audit surfaced an 80% lookahead-WR vs 24% execution-WR gap. Claude Chat's
2026-05-20 review warned that execution-based edges typically degrade
50-80% in the paper-to-live transition vs 10-20% for signal-based edges.
This estimator quantifies that delta on whatever sessions are already in
the DB.

Spec: DEFERRED_CHANGES.md, section
  "2026-05-20 — Realistic-fill estimator [FREEZE-SAFE — PRIORITY #0 for
   sessions 4-10]"

v2 changes (2026-05-21, methodology corrections vs v1):

  1) Lookahead overcounted T1 wins on fast-reversal days. v1 used
     `signal_lookahead_outcome.would_have_hit_t1` which marks True if SPX
     touched T1 anywhere in the 60-min window, EVEN IF stop fired first.
     v2 replaces that with an order-preserving 1-min bar walk per signal
     (mirrors ablation_backtest.py:_simulate_bcd, minus peak-trail).

  2) Partials were ignored. v1 credited +1R per T1 win regardless of T2/T3.
     v2 implements 50/30/20 scale-out using each tier's actual distance
     from entry (so banked R = 0.5*tier1_R + 0.3*tier2_R + 0.2*tier3_R).
     If targets sit at exactly +1R/+2R/+3R, total = +1.7R; but the
     engine places targets at wall clusters so spacing varies. Stops
     after partials exit residual at the original stop level. This
     matches the live spx_target_watcher flow.

  3) Fixed $400/R was wrong for sessions with non-default sizing. v2
     derives R_dollars per signal from the matched live parent's qty ×
     entry_limit × 100 × 0.5 (~50% of premium ≈ stop-distance in option
     price terms). When the signal can't be matched to a parent, falls
     back to the session's median R_dollars from matched parents. If a
     session has zero parents, signals are skipped (no fabricated $).

  4) Commissions on real $. v1 didn't subtract IBKR fees. v2 subtracts
     $1.75 × shares for every ibkr_execution row and reports the
     aggregate as commission_dollars.

  5) Unrealized on open positions. v1 reported open_cost as carry-over
     (purchase cost only). v2 adds a crude unrealized estimate using
     SPX_last × 0.4 (rough delta proxy for 0DTE deep-OTM near close).
     Reports as unrealized_dollars. Net real $ = realized − commissions
     + unrealized.

What this v2 does NOT do (still phase A of the original spec — deferred):
  - Does not capture exit-side L1 bid/ask (needs runtime additions to
    spx_target_watcher / BE-ladder code paths).
  - Does not persist DailyReview.realistic_fill_dollars (schema change).

Known data-quality caveats:
  - Bracket child orders not persisted with parent_id linkage. Per-parent
    real-$ is pro-rated by paired-BOT-qty within each contract group.
  - signal_iter is not directly linked to live_order. v2 matches by
    (session_date, direction, timestamp proximity, strike_rec ≈ parent
    strike). When option_pricing.entry_limit is missing (~10% on
    pre-5/20 sessions), the parent's avg_fill_price is used as proxy.
  - Unrealized uses a fixed 0.4 delta proxy (crude — actual deep-OTM
    0DTE delta near close is regime-dependent).
  - Commission model is $1.75 per execution row (per spec). Round-trip
    1-lot trades may slightly over-charge; bulk fills are charged once
    per fill row.

CLI:
    python realistic_fill_estimator.py 2026-05-19 2026-05-21
    python realistic_fill_estimator.py 2026-05-19 2026-05-21 --verbose
    python realistic_fill_estimator.py 2026-05-19 2026-05-21 --json
    python realistic_fill_estimator.py 2026-05-19 2026-05-21 --out fills.md

Programmatic:
    from realistic_fill_estimator import run_realistic_fill_estimator
    result = run_realistic_fill_estimator("2026-05-19", "2026-05-21")
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

DEFAULT_DB = str(Path.home() / ".fef-terminal" / "signal_log.db")

# v1 fallback (used only when v2 sizing cannot be derived for a session).
DOLLARS_PER_R = 400.0  # 5 contracts × $2000 risk per trade

# v2 constants — see methodology block in module docstring
COMMISSION_PER_FILL_PER_CONTRACT = 1.75   # $/share, per ibkr_execution row
PREMIUM_AT_RISK_FRACTION = 0.5            # heuristic: ~50% of entry premium = R
UNREALIZED_DELTA_PROXY = 0.4              # heuristic: deep-OTM 0DTE near close
SIGNAL_TO_PARENT_MATCH_WINDOW_S = 30      # secs between signal.timestamp and parent.submitted_at

# Bar walk
PARTIAL_T1_FRACTION = 0.5
PARTIAL_T2_FRACTION = 0.3
PARTIAL_T3_FRACTION = 0.2
WALK_HORIZON_MINUTES = 60


# ----------------------- account-class filter -----------------------

def _parse_account_class_arg(raw: str) -> set[str]:
    if not raw:
        return {"paper"}
    raw = raw.strip().lower()
    if raw == "all":
        return {"paper", "live", "mock", "unknown"}
    return {x.strip() for x in raw.split(",") if x.strip()}


def _passes_account_filter(extras_raw, accept: set[str]) -> bool:
    """Return True if the row's extras.account_class is in accept.
    'unknown' (pre-tag rows) is treated as paper-equivalent when paper
    accepted. Matches the convention from ablation_backtest.py."""
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


# ----------------------- helpers -----------------------

def _session_date_of(submitted_at_raw: str) -> str:
    """Extract YYYY-MM-DD from a SQLite-stored datetime string. live_order
    has no session_date column — daily_review derives it from submitted_at
    (see daily_review.py line 181), so we mirror that."""
    if not submitted_at_raw:
        return ""
    return submitted_at_raw[:10]


def _ts_to_ms(ts_str: str) -> int:
    """Parse a SQLite-stored datetime ('YYYY-MM-DD HH:MM:SS[.f]' or ISO) into UTC ms."""
    if not ts_str:
        return 0
    s = ts_str.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # tolerate microsecond overflow / odd formats
        try:
            dt = datetime.fromisoformat(s[:26])
        except ValueError:
            return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _extras_dict(extras_raw) -> dict:
    if not extras_raw:
        return {}
    try:
        ex = json.loads(extras_raw) if isinstance(extras_raw, str) else extras_raw
    except json.JSONDecodeError:
        return {}
    return ex if isinstance(ex, dict) else {}


# ----------------------- v2 order-preserving bar walk -----------------------

def _simulate_partial_walk(
    entry_spx: float,
    direction_sign: int,
    t1: float, t2: float, t3: float, stop: float,
    fwd_bars: list[dict],
) -> tuple[float, str, list[str]]:
    """Order-preserving 1-min bar walk with 50/30/20 partial scale-out.

    Returns (realized_R, exit_reason, fired_tiers).

    Logic per bar:
      - LONG_CALLS (direction_sign=+1): adverse = bar.low, favor = bar.high.
      - LONG_PUTS  (direction_sign=-1): adverse = bar.high, favor = bar.low.
      - Order within a bar: stop check first (conservative — if both stop
        and a tier are touched in the same 1-min bar, assume stop fired
        first unless the entire bar is to the favorable side of stop).
      - Stop fires:
          * Before any tier: realized = -1R (full position out).
          * After partials: residual unrealized = (stop - entry)/risk × remaining_share.
            Total = banked partials + residual at stop.
      - Tiers (fired in strict order, T1 then T2 then T3):
          * T1: banks 0.5 × +1R = +0.5R.
          * T2: banks 0.3 × +2R = +0.6R (T2 distance from entry = 2× risk).
          * T3: banks 0.2 × +3R = +0.6R (T3 distance = 3× risk). Halt — all sold.
      - Timeout (no terminal exit): residual exits at last bar close,
        realized = banked + remaining_share × direction_sign × (close-entry)/risk.
    """
    if not fwd_bars:
        return 0.0, "no_data", []

    risk = abs(entry_spx - stop)
    if risk <= 0:
        return 0.0, "invalid_risk", []

    banked_r = 0.0
    remaining_share = 1.0
    t1_fired = False
    t2_fired = False
    t3_fired = False
    fired_tiers: list[str] = []

    for b in fwd_bars:
        hi, lo, cl = float(b["high"]), float(b["low"]), float(b["close"])
        if direction_sign > 0:
            adverse_touched = (lo <= stop)
            t1_touched = (hi >= t1)
            t2_touched = (hi >= t2)
            t3_touched = (hi >= t3)
        else:
            adverse_touched = (hi >= stop)
            t1_touched = (lo <= t1)
            t2_touched = (lo <= t2)
            t3_touched = (lo <= t3)

        # Within-bar order: check stop first (conservative — penalizes ambiguous bars).
        if adverse_touched:
            # Stop fires. Residual exits at stop level.
            residual_r = direction_sign * (stop - entry_spx) / risk  # negative = -1.0
            total_r = banked_r + remaining_share * residual_r
            reason = "stop" if not fired_tiers else "stop_after_partial"
            return total_r, reason, fired_tiers

        # Tier checks (strict order: T1 must fire before T2, T2 before T3).
        if not t1_fired and t1_touched:
            tier_r = direction_sign * (t1 - entry_spx) / risk  # = +1R
            banked_r += PARTIAL_T1_FRACTION * tier_r
            remaining_share -= PARTIAL_T1_FRACTION
            t1_fired = True
            fired_tiers.append("T1")
        if t1_fired and not t2_fired and t2_touched:
            tier_r = direction_sign * (t2 - entry_spx) / risk  # = +2R
            banked_r += PARTIAL_T2_FRACTION * tier_r
            remaining_share -= PARTIAL_T2_FRACTION
            t2_fired = True
            fired_tiers.append("T2")
        if t2_fired and not t3_fired and t3_touched:
            tier_r = direction_sign * (t3 - entry_spx) / risk  # = +3R
            banked_r += PARTIAL_T3_FRACTION * tier_r
            remaining_share -= PARTIAL_T3_FRACTION
            t3_fired = True
            fired_tiers.append("T3")
            # All scaled out
            return banked_r, "t3_complete", fired_tiers

    # Timeout — exit residual at last bar close.
    last_close = float(fwd_bars[-1]["close"])
    residual_r = direction_sign * (last_close - entry_spx) / risk
    total_r = banked_r + remaining_share * residual_r
    reason = "timeout" if not fired_tiers else "timeout_after_partial"
    return total_r, reason, fired_tiers


# ----------------------- v2 signal→parent matching for R-sizing -----------------------

def _match_signal_to_parent(
    sig: dict,
    parents_by_date_dir: dict,
) -> dict | None:
    """Find the parent that fired from this signal.

    Heuristic: same session_date, same direction, parent.submitted_at
    within SIGNAL_TO_PARENT_MATCH_WINDOW_S of signal.timestamp, parent
    strike close to extras.strike_rec.

    Returns the parent row dict or None.
    """
    date = sig["session_date"]
    direction = sig["direction"]
    sig_ms = _ts_to_ms(sig["timestamp"])
    if sig_ms == 0:
        return None
    right_target = "C" if direction == "LONG_CALLS" else "P"
    sig_extras = _extras_dict(sig["extras"])
    strike_rec = sig_extras.get("strike_rec")

    candidates = parents_by_date_dir.get((date, right_target), [])
    best = None
    best_dt = None
    for p in candidates:
        dt = abs(p["_submitted_ms"] - sig_ms) / 1000.0
        if dt > SIGNAL_TO_PARENT_MATCH_WINDOW_S:
            continue
        # Optional strike tightening — prefer parents whose strike matches strike_rec
        if strike_rec is not None and p["strike"] is not None:
            strike_close = abs(float(p["strike"]) - float(strike_rec)) <= 5.0
        else:
            strike_close = True
        if not strike_close:
            continue
        if best is None or dt < best_dt:
            best = p
            best_dt = dt
    return best


def _r_dollars_for_signal(
    sig: dict, parent: dict | None, session_median_r_dollars: float | None,
) -> tuple[float | None, str]:
    """Compute R_dollars for a single signal.

    Priority:
      1) parent.qty × entry_limit × 100 × PREMIUM_AT_RISK_FRACTION
         (entry_limit from signal.extras.option_pricing.entry_limit;
          falls back to parent.avg_fill_price when missing).
      2) session-median R_dollars from successfully matched parents.
      3) None (caller skips the signal).

    Returns (r_dollars, source_label).
    """
    if parent is not None:
        qty = parent.get("qty") or 0
        if qty > 0:
            sig_extras = _extras_dict(sig["extras"])
            op = sig_extras.get("option_pricing") or {}
            entry_limit = op.get("entry_limit") if isinstance(op, dict) else None
            if entry_limit is None or entry_limit <= 0:
                entry_limit = parent.get("avg_fill_price")
            if entry_limit and entry_limit > 0:
                r_dollars = float(qty) * float(entry_limit) * 100.0 * PREMIUM_AT_RISK_FRACTION
                return r_dollars, "parent_match"
    if session_median_r_dollars is not None and session_median_r_dollars > 0:
        return session_median_r_dollars, "session_median"
    return None, "no_basis"


# ----------------------- v2 unrealized estimate -----------------------

def _estimate_unrealized(
    contract_rows: list[dict], spx_last: float | None,
) -> tuple[float, list[dict]]:
    """Estimate $ value of still-open positions vs avg purchase cost.

    Only applied to SPX-symbol contracts — other underlyings (QQQ, etc.)
    would need their own last-spot to be meaningful. Non-SPX open
    positions are recorded with unrealized_dollars=None so they don't
    pollute the session total.

    For each SPX contract group with open_qty > 0:
      LONG_CALL: option_value ≈ max(0, spx_last - strike) × delta_proxy
      LONG_PUT:  option_value ≈ max(0, strike - spx_last) × delta_proxy
      unrealized = (option_value - avg_bot) × open_qty × 100

    Returns (total_unrealized, per-contract detail list).
    """
    total = 0.0
    detail: list[dict] = []
    for c in contract_rows:
        open_q = c.get("open_qty", 0.0) or 0.0
        if open_q <= 0:
            detail.append({**c})
            continue
        symbol = (c.get("symbol") or "").upper()
        if symbol != "SPX":
            # Non-SPX open positions need their own last-spot to estimate.
            # Mark them None rather than contaminate the total with stale
            # SPX-priced intrinsic on, e.g., a QQQ contract.
            detail.append({**c, "unrealized_dollars": None, "option_value_est": None,
                           "note": "non_spx_underlying_skipped"})
            continue
        if spx_last is None:
            detail.append({**c, "unrealized_dollars": None, "option_value_est": None,
                           "note": "no_spx_close_available"})
            continue
        strike = float(c["strike"] or 0)
        avg_bot = float(c["avg_bot"] or 0)
        right = (c["right"] or "").upper()
        if right == "C":
            intrinsic = max(0.0, spx_last - strike)
        elif right == "P":
            intrinsic = max(0.0, strike - spx_last)
        else:
            intrinsic = 0.0
        option_value = intrinsic * UNREALIZED_DELTA_PROXY
        unreal = (option_value - avg_bot) * open_q * 100.0
        total += unreal
        detail.append({
            **c,
            "option_value_est": round(option_value, 3),
            "unrealized_dollars": round(unreal, 2),
        })
    return total, detail


# ----------------------- v1 (legacy) lookahead-based R helper ------------------

def _paper_r_from_lookahead(hit_t1: bool, hit_stop: bool) -> float:
    """v1 scoring (kept for the v1-vs-v2 comparison table)."""
    if hit_t1 and not hit_stop:
        return +1.0
    if hit_stop and not hit_t1:
        return -1.0
    return 0.0


# ----------------------- main entry -----------------------

def run_realistic_fill_estimator(
    date_from: str,
    date_to: str,
    db_path: str = DEFAULT_DB,
    accept_classes: set[str] | None = None,
    dollars_per_r: float = DOLLARS_PER_R,
) -> dict:
    """Compute paper-theoretical vs IBKR-realized P/L per session (v2).

    Returns a dict with:
      - sessions: per-day rows with v1 + v2 columns side-by-side
      - totals: cross-session aggregate
      - methodology: text block explaining the v2 corrections
    """
    accept = accept_classes or {"paper"}
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    # ----------- Pull signals + lookahead (used for v1 comparison) -----------
    raw_signals = cur.execute(
        """
        SELECT id, session_date, timestamp, spx_price, direction, extras
        FROM signal_iter
        WHERE session_date BETWEEN ? AND ?
          AND direction IN ('LONG_CALLS','LONG_PUTS')
          AND spx_price BETWEEN 1000 AND 10000
        ORDER BY session_date, timestamp
        """,
        (date_from, date_to),
    ).fetchall()
    signals = [dict(s) for s in raw_signals if _passes_account_filter(s["extras"], accept)]
    sig_ids = [s["id"] for s in signals]

    look_by_sid: dict[int, dict] = {}
    if sig_ids:
        for i in range(0, len(sig_ids), 900):
            chunk = sig_ids[i : i + 900]
            placeholders = ",".join("?" * len(chunk))
            for r in cur.execute(
                f"""
                SELECT signal_iter_id, would_have_hit_t1, would_have_hit_stop
                FROM signal_lookahead_outcome
                WHERE lookahead_minutes = 60
                  AND signal_iter_id IN ({placeholders})
                """,
                chunk,
            ):
                look_by_sid[r["signal_iter_id"]] = dict(r)

    # ----------- Pull SPX 1-min bars covering window + buffer -----------
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

    # SPX close per session (for unrealized estimate)
    spx_last_by_date: dict[str, float] = {}
    for date in _date_range(date_from, date_to):
        day_start_ms = _ts_to_ms(f"{date} 00:00:00")
        day_end_ms = _ts_to_ms(f"{date} 23:59:59")
        day_bars = bars_in_window(day_start_ms, day_end_ms)
        if day_bars:
            spx_last_by_date[date] = float(day_bars[-1]["close"])

    # ----------- Pull live parents for matching -----------
    parents_raw = cur.execute(
        """
        SELECT lo.id AS parent_id, lo.submitted_at, lo.ib_order_id,
               lo.ticket_id, lo.symbol, lo.strike, lo.right, lo.expiry,
               lo.qty, lo.filled_qty, lo.status, lo.avg_fill_price,
               lo.entry_spx_price, lo.spx_target_t1, lo.spx_target_stop,
               tt.signal_iter_id
        FROM live_order lo
        LEFT JOIN trade_ticket tt ON tt.id = lo.ticket_id
        WHERE lo.bracket_role = 'parent'
          AND lo.dry_run = 0
        ORDER BY lo.submitted_at
        """
    ).fetchall()
    parents = [dict(p) for p in parents_raw
               if date_from <= _session_date_of(p["submitted_at"]) <= date_to]
    # Filter to parents with filled_qty > 0 OR avg_fill_price set — unfilled
    # parents (cancelled, qty=0 fills) can't supply meaningful R sizing.
    for p in parents:
        p["_submitted_ms"] = _ts_to_ms(p["submitted_at"])
        p["_session_date"] = _session_date_of(p["submitted_at"])
    parents_filled = [p for p in parents
                      if (p.get("filled_qty") or 0) > 0
                      and (p.get("avg_fill_price") or 0) > 0]

    parents_by_date_dir: dict[tuple, list[dict]] = defaultdict(list)
    for p in parents_filled:
        parents_by_date_dir[(p["_session_date"], p["right"])].append(p)

    # ----------- Per-session median R_dollars (used as fallback) -----------
    session_r_dollars_samples: dict[str, list[float]] = defaultdict(list)
    for p in parents_filled:
        date = p["_session_date"]
        qty = p.get("filled_qty") or 0
        entry = p.get("avg_fill_price") or 0
        if qty > 0 and entry > 0:
            session_r_dollars_samples[date].append(
                float(qty) * float(entry) * 100.0 * PREMIUM_AT_RISK_FRACTION
            )
    session_median_r_dollars: dict[str, float | None] = {
        d: (median(vals) if vals else None)
        for d, vals in session_r_dollars_samples.items()
    }

    # ----------- v2 paper $ via order-preserving bar walk -----------
    paper_v2_dollars_by_date: dict[str, float] = defaultdict(float)
    paper_v2_r_sum_by_date: dict[str, float] = defaultdict(float)
    paper_v2_wins_by_date: dict[str, int] = defaultdict(int)
    paper_v2_losses_by_date: dict[str, int] = defaultdict(int)
    paper_v2_flats_by_date: dict[str, int] = defaultdict(int)
    paper_v2_skipped_by_date: dict[str, int] = defaultdict(int)
    paper_v2_exit_reason_by_date: dict[str, defaultdict] = defaultdict(lambda: defaultdict(int))
    paper_v2_signal_detail: dict[str, list[dict]] = defaultdict(list)
    paper_v2_direction_signs_by_date: dict[str, list[float]] = defaultdict(list)

    # v1 paper $ (legacy lookahead-based) — kept for comparison only
    paper_v1_dollars_by_date: dict[str, float] = defaultdict(float)
    paper_v1_r_sum_by_date: dict[str, float] = defaultdict(float)
    paper_v1_wins_by_date: dict[str, int] = defaultdict(int)
    paper_v1_losses_by_date: dict[str, int] = defaultdict(int)
    paper_v1_flats_by_date: dict[str, int] = defaultdict(int)
    paper_n_by_date: dict[str, int] = defaultdict(int)

    for s in signals:
        date = s["session_date"]
        paper_n_by_date[date] += 1

        # ---- v1: lookahead scoring × fixed dollars_per_r ----
        look = look_by_sid.get(s["id"])
        if look:
            r1 = _paper_r_from_lookahead(bool(look["would_have_hit_t1"]),
                                         bool(look["would_have_hit_stop"]))
        else:
            r1 = 0.0
        paper_v1_r_sum_by_date[date] += r1
        paper_v1_dollars_by_date[date] += r1 * dollars_per_r
        if r1 > 0.05:
            paper_v1_wins_by_date[date] += 1
        elif r1 < -0.05:
            paper_v1_losses_by_date[date] += 1
        else:
            paper_v1_flats_by_date[date] += 1

        # ---- v2: order-preserving walk × dynamic R-sizing ----
        ex = _extras_dict(s["extras"])
        t1, t2, t3, stop = ex.get("t1"), ex.get("t2"), ex.get("t3"), ex.get("stop")
        if t1 is None or t2 is None or t3 is None or stop is None:
            paper_v2_skipped_by_date[date] += 1
            paper_v2_exit_reason_by_date[date]["skip_missing_targets"] += 1
            continue

        parent = _match_signal_to_parent(s, parents_by_date_dir)
        r_dollars, r_source = _r_dollars_for_signal(
            s, parent, session_median_r_dollars.get(date),
        )
        if r_dollars is None:
            paper_v2_skipped_by_date[date] += 1
            paper_v2_exit_reason_by_date[date]["skip_no_sizing_basis"] += 1
            continue

        direction_sign = +1 if s["direction"] == "LONG_CALLS" else -1
        entry_spx = float(s["spx_price"])
        sig_ms = _ts_to_ms(s["timestamp"])
        fwd = bars_in_window(sig_ms, sig_ms + WALK_HORIZON_MINUTES * 60 * 1000)
        r2, reason, tiers = _simulate_partial_walk(
            entry_spx, direction_sign, float(t1), float(t2), float(t3), float(stop), fwd,
        )
        dollars = r2 * r_dollars
        paper_v2_r_sum_by_date[date] += r2
        paper_v2_dollars_by_date[date] += dollars
        paper_v2_direction_signs_by_date[date].append(dollars)
        paper_v2_exit_reason_by_date[date][reason] += 1
        if r2 > 0.05:
            paper_v2_wins_by_date[date] += 1
        elif r2 < -0.05:
            paper_v2_losses_by_date[date] += 1
        else:
            paper_v2_flats_by_date[date] += 1

        paper_v2_signal_detail[date].append({
            "signal_id": s["id"],
            "timestamp": s["timestamp"],
            "direction": s["direction"],
            "entry_spx": entry_spx,
            "t1": float(t1), "t2": float(t2), "t3": float(t3), "stop": float(stop),
            "r": round(r2, 3),
            "exit_reason": reason,
            "fired_tiers": tiers,
            "r_dollars": round(r_dollars, 2),
            "r_source": r_source,
            "paper_dollars": round(dollars, 2),
            "matched_parent_id": (parent["parent_id"] if parent else None),
        })

    # ----------- Real $ leg: pair-off by contract group + commissions -----------
    real_by_date: dict[str, dict] = {}
    for date in _date_range(date_from, date_to):
        execs = cur.execute(
            """
            SELECT symbol, strike, right, expiry, side, shares, price, ib_order_id
            FROM ibkr_execution
            WHERE session_date = ?
            ORDER BY timestamp
            """,
            (date,),
        ).fetchall()
        by_contract: dict[tuple, dict[str, float]] = {}
        commission_dollars = 0.0
        n_fills = 0
        total_shares_filled = 0.0
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
            # Commissions: per spec, charge $1.75 × shares per execution row.
            commission_dollars += sh * COMMISSION_PER_FILL_PER_CONTRACT
            n_fills += 1
            total_shares_filled += sh

        realized = 0.0
        open_cost = 0.0
        contract_rows: list[dict] = []
        for key, slot in by_contract.items():
            bot_q, sld_q = slot["bot_q"], slot["sld_q"]
            if bot_q == 0 and sld_q == 0:
                continue
            paired = min(bot_q, sld_q)
            open_q = bot_q - sld_q
            avg_bot = slot["bot_v"] / bot_q if bot_q > 0 else 0.0
            avg_sld = slot["sld_v"] / sld_q if sld_q > 0 else 0.0
            row_realized = (avg_sld - avg_bot) * paired * 100 if paired > 0 else 0.0
            row_open_cost = avg_bot * open_q * 100 if open_q > 0 else 0.0
            realized += row_realized
            open_cost += row_open_cost
            contract_rows.append({
                "symbol": key[0], "strike": key[1], "right": key[2], "expiry": key[3],
                "bot_qty": bot_q, "sld_qty": sld_q, "paired_qty": paired,
                "avg_bot": round(avg_bot, 3), "avg_sld": round(avg_sld, 3),
                "realized_dollars": round(row_realized, 2),
                "open_qty": max(0.0, open_q),
                "open_cost_dollars": round(row_open_cost, 2),
            })

        # v2: unrealized estimate on still-open positions
        spx_last = spx_last_by_date.get(date)
        unrealized, contract_rows_with_unreal = _estimate_unrealized(contract_rows, spx_last)

        real_by_date[date] = {
            "realized_dollars": realized,
            "open_cost_dollars": open_cost,
            "commission_dollars": commission_dollars,
            "unrealized_dollars": unrealized,
            "spx_last": spx_last,
            "n_fills": n_fills,
            "total_shares_filled": total_shares_filled,
            "contracts": contract_rows_with_unreal,
        }

    # ----------- Per-parent breakdown (verbose) -----------
    # Pro-rate group realized by parent's BOT-share within its contract group.
    parents_by_date: dict[str, list[dict]] = defaultdict(list)
    for p in parents:
        date = p["_session_date"]
        # Pull this parent's entry executions
        entry_execs = cur.execute(
            """
            SELECT shares, price, side
            FROM ibkr_execution
            WHERE ib_order_id = ? AND session_date = ? AND side = 'BOT'
            """,
            (p["ib_order_id"], date),
        ).fetchall()
        parent_bot_q = sum(float(e["shares"] or 0) for e in entry_execs)
        parent_bot_v = sum(float(e["shares"] or 0) * float(e["price"] or 0)
                           for e in entry_execs)
        parent_avg_bot = parent_bot_v / parent_bot_q if parent_bot_q > 0 else None

        key = (p["symbol"], float(p["strike"] or 0), p["right"], p["expiry"])
        group_realized = 0.0
        group_bot_total = 0.0
        for c in real_by_date.get(date, {}).get("contracts", []):
            if (c["symbol"], c["strike"], c["right"], c["expiry"]) == key:
                group_realized = c["realized_dollars"]
                group_bot_total = c["bot_qty"]
                break

        if group_bot_total > 0 and parent_bot_q > 0:
            share = parent_bot_q / group_bot_total
            parent_real = group_realized * share
        else:
            parent_real = 0.0

        # Paper-theoretical (v2): use the matched signal if found. Prefer
        # the trade_ticket.signal_iter_id link when present; otherwise
        # fall back to timestamp+direction+strike proximity (same
        # heuristic as _match_signal_to_parent in reverse).
        parent_paper = 0.0
        paper_label = "unmatched"
        matched_sid = None
        if p["signal_iter_id"] is not None:
            for det in paper_v2_signal_detail.get(date, []):
                if det["signal_id"] == p["signal_iter_id"]:
                    matched_sid = det["signal_id"]
                    parent_paper = det["paper_dollars"]
                    paper_label = det["exit_reason"]
                    break
        if matched_sid is None:
            parent_dir = "LONG_CALLS" if p["right"] == "C" else "LONG_PUTS"
            parent_ms = p["_submitted_ms"]
            parent_strike = float(p["strike"] or 0)
            best_det = None
            best_dt = None
            for det in paper_v2_signal_detail.get(date, []):
                if det["direction"] != parent_dir:
                    continue
                det_ms = _ts_to_ms(det["timestamp"])
                if det_ms == 0:
                    continue
                dt = abs(det_ms - parent_ms) / 1000.0
                if dt > SIGNAL_TO_PARENT_MATCH_WINDOW_S:
                    continue
                if best_det is None or dt < best_dt:
                    best_det = det
                    best_dt = dt
            if best_det is not None:
                parent_paper = best_det["paper_dollars"]
                paper_label = best_det["exit_reason"]

        parents_by_date[date].append({
            "parent_id": p["parent_id"],
            "submitted_at": p["submitted_at"],
            "ib_order_id": p["ib_order_id"],
            "signal_iter_id": p["signal_iter_id"],
            "contract": f"{p['symbol']} {int(p['strike'] or 0)} {p['right']} {p['expiry']}",
            "qty": int(p["qty"] or 0),
            "filled_qty": int(p["filled_qty"] or 0),
            "status": p["status"],
            "parent_bot_qty": parent_bot_q,
            "parent_avg_bot": round(parent_avg_bot, 3) if parent_avg_bot else None,
            "paper_theoretical_dollars": round(parent_paper, 2),
            "paper_label": paper_label,
            "real_realized_dollars": round(parent_real, 2),
            "haircut_dollars": round(parent_paper - parent_real, 2),
        })

    # ----------- Roll up per-session summary -----------
    sessions: list[dict] = []
    for date in _date_range(date_from, date_to):
        # v1 paper
        v1_paper = paper_v1_dollars_by_date.get(date, 0.0)
        v1_sum_r = paper_v1_r_sum_by_date.get(date, 0.0)

        # v2 paper
        v2_paper = paper_v2_dollars_by_date.get(date, 0.0)
        v2_sum_r = paper_v2_r_sum_by_date.get(date, 0.0)

        # Real
        rl = real_by_date.get(date, {})
        realized = rl.get("realized_dollars", 0.0)
        commissions = rl.get("commission_dollars", 0.0)
        unrealized = rl.get("unrealized_dollars", 0.0)
        open_cost = rl.get("open_cost_dollars", 0.0)
        real_net = realized - commissions + unrealized  # v2 net real

        # v1 real (just realized, no commissions / unrealized — matches old report)
        v1_real = realized

        # v2 haircuts
        haircut_v2 = v2_paper - real_net
        haircut_v2_pct = (haircut_v2 / v2_paper * 100) if v2_paper else None

        # Direction agreement: sign of v2_paper vs sign of real_net
        if v2_paper == 0 and real_net == 0:
            direction_agree = "n/a"
        elif (v2_paper > 0) == (real_net > 0):
            direction_agree = "yes"
        else:
            direction_agree = "no"

        # Median R-dollars for the session (for transparency)
        median_r = session_median_r_dollars.get(date)

        sessions.append({
            "date": date,
            "n_signals": paper_n_by_date.get(date, 0),
            "n_parents": len(parents_by_date.get(date, [])),
            "spx_last": rl.get("spx_last"),
            "session_median_r_dollars": round(median_r, 2) if median_r else None,

            # v2 paper
            "paper_v2_sum_r": round(v2_sum_r, 2),
            "paper_v2_dollars": round(v2_paper, 2),
            "paper_v2_wins": paper_v2_wins_by_date.get(date, 0),
            "paper_v2_losses": paper_v2_losses_by_date.get(date, 0),
            "paper_v2_flats": paper_v2_flats_by_date.get(date, 0),
            "paper_v2_skipped": paper_v2_skipped_by_date.get(date, 0),
            "paper_v2_exit_reasons": dict(paper_v2_exit_reason_by_date.get(date, {})),

            # v1 paper (for comparison)
            "paper_v1_sum_r": round(v1_sum_r, 2),
            "paper_v1_dollars": round(v1_paper, 2),
            "paper_v1_wins": paper_v1_wins_by_date.get(date, 0),
            "paper_v1_losses": paper_v1_losses_by_date.get(date, 0),
            "paper_v1_flats": paper_v1_flats_by_date.get(date, 0),

            # Real
            "real_realized_dollars": round(realized, 2),
            "commission_dollars": round(commissions, 2),
            "unrealized_dollars": round(unrealized, 2),
            "real_net_dollars": round(real_net, 2),
            "real_v1_dollars": round(v1_real, 2),  # legacy (no commission, no unrealized)
            "open_cost_dollars": round(open_cost, 2),

            # Haircut (v2)
            "haircut_dollars": round(haircut_v2, 2),
            "haircut_pct": round(haircut_v2_pct, 1) if haircut_v2_pct is not None else None,
            "direction_agree": direction_agree,

            # Detail
            "contracts": rl.get("contracts", []),
            "parents": parents_by_date.get(date, []),
            "signal_details": paper_v2_signal_detail.get(date, []),
        })

    # ----------- Cross-session totals -----------
    total_v1_paper = sum(s["paper_v1_dollars"] for s in sessions)
    total_v2_paper = sum(s["paper_v2_dollars"] for s in sessions)
    total_realized = sum(s["real_realized_dollars"] for s in sessions)
    total_commissions = sum(s["commission_dollars"] for s in sessions)
    total_unrealized = sum(s["unrealized_dollars"] for s in sessions)
    total_real_net = sum(s["real_net_dollars"] for s in sessions)
    total_real_v1 = sum(s["real_v1_dollars"] for s in sessions)
    total_haircut = total_v2_paper - total_real_net

    totals = {
        "n_signals": sum(s["n_signals"] for s in sessions),
        "n_parents": sum(s["n_parents"] for s in sessions),
        "paper_v1_dollars": round(total_v1_paper, 2),
        "paper_v2_dollars": round(total_v2_paper, 2),
        "real_realized_dollars": round(total_realized, 2),
        "commission_dollars": round(total_commissions, 2),
        "unrealized_dollars": round(total_unrealized, 2),
        "real_net_dollars": round(total_real_net, 2),
        "real_v1_dollars": round(total_real_v1, 2),
        "haircut_dollars": round(total_haircut, 2),
        "haircut_pct": round(total_haircut / total_v2_paper * 100, 1) if total_v2_paper else None,
        "open_cost_dollars": round(sum(s["open_cost_dollars"] for s in sessions), 2),
    }

    return {
        "date_from": date_from,
        "date_to": date_to,
        "dollars_per_r_fallback": dollars_per_r,
        "accept_classes": sorted(accept),
        "version": "v2",
        "sessions": sessions,
        "totals": totals,
    }


def _date_range(date_from: str, date_to: str) -> list[str]:
    """Inclusive [date_from, date_to] iterator as YYYY-MM-DD strings."""
    d0 = datetime.fromisoformat(date_from)
    d1 = datetime.fromisoformat(date_to)
    out: list[str] = []
    cur = d0
    while cur <= d1:
        out.append(cur.strftime("%Y-%m-%d"))
        cur = cur + timedelta(days=1)
    return out


# ----------------------- markdown report -----------------------

def format_report(result: dict, verbose: bool = False) -> str:
    """Markdown report string. verbose=True adds per-parent and
    per-signal-detail breakdowns under each session."""
    lines: list[str] = []
    lines.append(
        f"## Realistic-Fill Estimator v2: {result['date_from']} to {result['date_to']}"
    )
    lines.append("")
    lines.append(
        f"Account-class filter: `{','.join(result['accept_classes'])}` | "
        f"fallback $/R: ${result['dollars_per_r_fallback']:.0f}"
    )
    lines.append("")

    # ----- Methodology section -----
    lines.append("### v2 methodology (5 corrections vs v1)")
    lines.append("")
    lines.append(
        "1. **Order-preserving bar walk** (replaces lookahead flags). "
        "Walks SPX 1-min bars forward from `signal.timestamp` and checks "
        "stop/T1/T2/T3 in order. Bars where stop is touched halt the "
        "trade — even if T1 was also touched in the same bar, stop "
        "wins (conservative). v1 used `signal_lookahead_outcome` which "
        "incorrectly marked T1 wins on stop-then-reversal bars."
    )
    lines.append("")
    lines.append(
        "2. **50/30/20 partial scale-out**. Each tier banks "
        "(fraction × tier_R), where tier_R = (tier_level − entry) / risk. "
        "If T1/T2/T3 sit at exactly +1R/+2R/+3R from entry, totals are "
        "0.5+0.6+0.6 = +1.7R. The engine actually places targets at wall "
        "clusters so spacing varies — a wider T3 = larger banked R on full "
        "scale-out. v1 credited a flat +1R per T1 win regardless of T2/T3."
    )
    lines.append("")
    lines.append(
        "3. **Dynamic R-sizing**. Each signal's R_dollars = matched "
        "parent's qty × signal.extras.option_pricing.entry_limit × 100 "
        f"× {PREMIUM_AT_RISK_FRACTION:.2f} (50% of premium ≈ stop "
        "distance in option price terms). Falls back to per-session "
        "median R_dollars if no parent matches; signals are skipped if "
        "the session has zero parents."
    )
    lines.append("")
    lines.append(
        f"4. **Commissions on real $**. Subtracts ${COMMISSION_PER_FILL_PER_CONTRACT:.2f} "
        "× shares per ibkr_execution row. Reported as commission_dollars."
    )
    lines.append("")
    lines.append(
        "5. **Crude unrealized on open positions**. "
        f"`option_value ≈ max(0, intrinsic) × {UNREALIZED_DELTA_PROXY:.1f}` "
        "(delta proxy for deep-OTM 0DTE near close). "
        "unrealized = (option_value − avg_bot) × open_qty × 100. "
        "Net real $ = realized − commissions + unrealized."
    )
    lines.append("")

    # ----- Per-session v2 table -----
    lines.append("### Per-session summary (v2)")
    lines.append("")
    lines.append(
        "| Date | n sigs | n parents | paper W/L/flat | paper $ (v2) | "
        "real $ (post-comm) | unrealized $ | net real $ | haircut $ | "
        "dir agree? |"
    )
    lines.append(
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|:---:|"
    )
    for s in result["sessions"]:
        wlf = f"{s['paper_v2_wins']}/{s['paper_v2_losses']}/{s['paper_v2_flats']}"
        real_post_comm = s["real_realized_dollars"] - s["commission_dollars"]
        lines.append(
            f"| {s['date']} | {s['n_signals']} | {s['n_parents']} | {wlf} | "
            f"{s['paper_v2_dollars']:+,.0f} | "
            f"{real_post_comm:+,.0f} | "
            f"{s['unrealized_dollars']:+,.0f} | "
            f"{s['real_net_dollars']:+,.0f} | "
            f"{s['haircut_dollars']:+,.0f} | "
            f"{s['direction_agree']} |"
        )
    lines.append("")

    # ----- v1 vs v2 comparison -----
    lines.append("### v1 vs v2 comparison")
    lines.append("")
    lines.append(
        "| Date | v1 paper $ | v2 paper $ | v1 real $ | v2 real $ (net) | dir agree? |"
    )
    lines.append(
        "|---|---:|---:|---:|---:|:---:|"
    )
    for s in result["sessions"]:
        lines.append(
            f"| {s['date']} | {s['paper_v1_dollars']:+,.0f} | "
            f"{s['paper_v2_dollars']:+,.0f} | "
            f"{s['real_v1_dollars']:+,.0f} | "
            f"{s['real_net_dollars']:+,.0f} | "
            f"{s['direction_agree']} |"
        )
    lines.append("")

    # ----- Totals -----
    t = result["totals"]
    lines.append("### Totals")
    lines.append("")
    lines.append(f"- Fired signals (paper-class): **{t['n_signals']}**")
    lines.append(f"- Live parent orders: **{t['n_parents']}**")
    lines.append(f"- Paper P/L v2: **${t['paper_v2_dollars']:+,.0f}**  (v1 was ${t['paper_v1_dollars']:+,.0f})")
    lines.append(f"- Real realized $: ${t['real_realized_dollars']:+,.0f}")
    lines.append(f"- Commissions $: −${t['commission_dollars']:,.0f}")
    lines.append(f"- Unrealized (open positions, crude): ${t['unrealized_dollars']:+,.0f}")
    lines.append(f"- **Real $ (net)**: **${t['real_net_dollars']:+,.0f}**  (v1 reported ${t['real_v1_dollars']:+,.0f})")
    lines.append(
        f"- Haircut v2: **${t['haircut_dollars']:+,.0f}** "
        f"({(str(t['haircut_pct']) + '%') if t['haircut_pct'] is not None else 'n/a'} of v2 paper)"
    )
    lines.append(f"- Open-position cost (carried): ${t['open_cost_dollars']:,.0f}")
    lines.append("")

    if not verbose:
        return "\n".join(lines)

    # ----- v2 signal-detail breakdown -----
    lines.append("### v2 signal-walk detail (verbose)")
    lines.append("")
    for s in result["sessions"]:
        if not s["signal_details"]:
            continue
        lines.append(
            f"#### {s['date']} — {len(s['signal_details'])} walked signals "
            f"(skipped: {s['paper_v2_skipped']})"
        )
        lines.append("")
        lines.append(f"Exit reason counts: `{s['paper_v2_exit_reasons']}`")
        lines.append("")
        lines.append(
            "| sid | time | dir | entry | t1/t2/t3/stop | R | tiers | "
            "exit | $/R | paper $ | source |"
        )
        lines.append(
            "|---:|---|---|---:|---|---:|---|---|---:|---:|---|"
        )
        for d in s["signal_details"]:
            tiers_str = ",".join(d["fired_tiers"]) or "—"
            dir_short = "C" if d["direction"] == "LONG_CALLS" else "P"
            lines.append(
                f"| {d['signal_id']} | {d['timestamp'][11:19]} | {dir_short} | "
                f"{d['entry_spx']:.2f} | "
                f"{d['t1']:.1f}/{d['t2']:.1f}/{d['t3']:.1f}/{d['stop']:.1f} | "
                f"{d['r']:+.2f} | {tiers_str} | {d['exit_reason']} | "
                f"{d['r_dollars']:,.0f} | {d['paper_dollars']:+,.0f} | "
                f"{d['r_source']} |"
            )
        lines.append("")

    # ----- Per-parent breakdown -----
    lines.append("### Per-parent breakdown (verbose)")
    lines.append("")
    lines.append(
        "Real-$ is pro-rated by parent's BOT-qty share within its "
        "contract group (exact when one parent per contract per session, "
        "approximate otherwise — bracket children aren't persisted with "
        "parent_id linkage)."
    )
    lines.append("")
    for s in result["sessions"]:
        if not s["parents"]:
            continue
        lines.append(f"#### {s['date']} — {len(s['parents'])} parents")
        lines.append("")
        lines.append(
            "| parent_id | contract | qty | status | paper $ | "
            "paper label | real $ | haircut $ |"
        )
        lines.append("|---:|---|---:|---|---:|---|---:|---:|")
        for p in s["parents"]:
            lines.append(
                f"| {p['parent_id']} | {p['contract']} | {p['qty']} | "
                f"{p['status']} | {p['paper_theoretical_dollars']:+,.0f} | "
                f"{p['paper_label']} | {p['real_realized_dollars']:+,.0f} | "
                f"{p['haircut_dollars']:+,.0f} |"
            )
        lines.append("")

    # ----- Per-contract pair-off detail (with unrealized) -----
    lines.append("### Per-contract pair-off + unrealized (verbose)")
    lines.append("")
    for s in result["sessions"]:
        if not s["contracts"]:
            continue
        spx_last = s.get("spx_last")
        spx_txt = f"{spx_last:.2f}" if spx_last is not None else "n/a"
        lines.append(
            f"#### {s['date']} — {len(s['contracts'])} contracts "
            f"(SPX last: {spx_txt})"
        )
        lines.append("")
        lines.append(
            "| contract | BOT qty | SLD qty | paired | avg BOT | "
            "avg SLD | realized $ | open qty | open cost $ | unrealized $ |"
        )
        lines.append(
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
        )
        for c in s["contracts"]:
            contract = f"{c['symbol']} {int(c['strike'])} {c['right']} {c['expiry']}"
            unreal = c.get("unrealized_dollars")
            unreal_txt = f"{unreal:+,.0f}" if isinstance(unreal, (int, float)) else "n/a"
            lines.append(
                f"| {contract} | {c['bot_qty']:.0f} | {c['sld_qty']:.0f} | "
                f"{c['paired_qty']:.0f} | {c['avg_bot']:.2f} | "
                f"{c['avg_sld']:.2f} | {c['realized_dollars']:+,.0f} | "
                f"{c['open_qty']:.0f} | {c['open_cost_dollars']:,.0f} | "
                f"{unreal_txt} |"
            )
        lines.append("")

    return "\n".join(lines)


# ----------------------- CLI -----------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Realistic-fill estimator v2: order-preserving paper walk + "
                    "dynamic R sizing + commissions + crude unrealized."
    )
    ap.add_argument("date_from", help="YYYY-MM-DD")
    ap.add_argument("date_to", help="YYYY-MM-DD")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--account-class", default="paper",
                    help="Filter by extras.account_class. 'paper' (default), 'live', "
                         "'mock', 'unknown', 'all', or comma-separated.")
    ap.add_argument("--dollars-per-r", type=float, default=DOLLARS_PER_R,
                    help=f"Legacy fallback $/R for v1 column (default {DOLLARS_PER_R:.0f}). "
                         "v2 derives R-dollars per signal from parent qty × entry premium.")
    ap.add_argument("--json", action="store_true", help="Emit raw JSON instead of markdown.")
    ap.add_argument("--verbose", action="store_true",
                    help="Include per-signal walk + per-parent + per-contract detail.")
    ap.add_argument("--out", default=None, help="Write report to file instead of stdout.")
    args = ap.parse_args()

    accept = _parse_account_class_arg(args.account_class)
    result = run_realistic_fill_estimator(
        args.date_from, args.date_to, args.db, accept, args.dollars_per_r,
    )
    if args.json:
        body = json.dumps(result, indent=2, default=str)
    else:
        body = format_report(result, verbose=args.verbose)

    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(f"Wrote {args.out} ({len(body)} bytes)")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
