"""Daily session report card.

Pulls every signal from a given session_date, walks SPX bars forward to
score outcomes (T1/T2/T3/STOP/TIMEOUT), and writes a DailyReview row.
Run either nightly (via auto_walk_forward scheduler) or on-demand via
POST /admin/daily_review.

The R-multiple calculation mirrors the analysis I did interactively:
  risk = |spx_at_signal - stop|
  realized_R = -1 if STOP, weighted-mean(R-tier × tier-share) if a tier
  hit, 0 if timeout. Uses the same 50/30/20 split as the auto-trader.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import DailyReview, HistoricalBar, SignalIter
from fef_sidecar.db.repository import SignalIterRepository

logger = logging.getLogger(__name__)


def _trace_outcome(start_ms: int, spx_entry: float, t1: float, t2: float,
                   t3: float, stop: float, is_long_calls: bool,
                   bars: list, window_min: int = 60) -> dict:
    """Walk forward 1-min bars and trace where SPX touched targets/stops.

    Tracks DYNAMIC STOP modeling the CURRENT engine's BE ladder:
      - At +0.5R MFE: stop moves to entry SPX (locked 0R)
      - At +1.0R MFE: stop moves to entry + 0.5R (locked +0.5R)
      - At +1.5R MFE: stop moves to entry + 1.0R (locked +1.0R)
    Stop hits register against the CURRENT dynamic stop, not the original.

    `out["dyn_stop_at_hit"]` is the dynamic-stop level when stop fired.
    Use this in the caller to compute realized R as locked amount instead
    of always -1R.
    """
    end_ms = start_ms + window_min * 60_000
    out = {"t1": None, "t2": None, "t3": None, "stop": None, "mfe": 0.0,
           "dyn_stop_at_hit": None}
    risk = abs(spx_entry - stop) or 1.0
    BE_LADDER = [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)]
    dyn_stop = stop
    mfe_r_max = 0.0

    for b in bars:
        if b.timestamp_ms < start_ms:
            continue
        if b.timestamp_ms > end_ms:
            break
        elapsed = (b.timestamp_ms - start_ms) / 60_000
        hi, lo = b.high, b.low

        if is_long_calls:
            # Update MFE first, then advance BE ladder, then check stop
            mfe_pts = hi - spx_entry
            if mfe_pts > out["mfe"]:
                out["mfe"] = mfe_pts
            mfe_r_max = max(mfe_r_max, mfe_pts / risk)
            for trigger_r, lock_r in BE_LADDER:
                if mfe_r_max >= trigger_r:
                    new_stop = spx_entry + lock_r * risk
                    if new_stop > dyn_stop:
                        dyn_stop = new_stop
            if out["stop"] is None and lo <= dyn_stop:
                out["stop"] = elapsed
                out["dyn_stop_at_hit"] = dyn_stop
                break
            if out["t1"] is None and hi >= t1: out["t1"] = elapsed
            if out["t2"] is None and hi >= t2: out["t2"] = elapsed
            if out["t3"] is None and hi >= t3: out["t3"] = elapsed
        else:  # LONG_PUTS
            mfe_pts = spx_entry - lo
            if mfe_pts > out["mfe"]:
                out["mfe"] = mfe_pts
            mfe_r_max = max(mfe_r_max, mfe_pts / risk)
            for trigger_r, lock_r in BE_LADDER:
                if mfe_r_max >= trigger_r:
                    new_stop = spx_entry - lock_r * risk
                    if new_stop < dyn_stop:
                        dyn_stop = new_stop
            if out["stop"] is None and hi >= dyn_stop:
                out["stop"] = elapsed
                out["dyn_stop_at_hit"] = dyn_stop
                break
            if out["t1"] is None and lo <= t1: out["t1"] = elapsed
            if out["t2"] is None and lo <= t2: out["t2"] = elapsed
            if out["t3"] is None and lo <= t3: out["t3"] = elapsed
    return out


def _estimate_parent_r(p) -> float:
    """Estimate realized R for one LiveOrder parent from SPX-target firing
    flags + dynamic stop level. **SECONDARY fallback** — only used when
    there are no ``ibkr_execution`` rows for the session (e.g. shadow /
    paper-only days). For real fills, ``compute_parent_wl_from_fills``
    in ``engine/exec_pair_off`` is the canonical FIFO pair-off source of
    truth.

    Accounts for the 50/30/20 partial scale-out so a STOPPED parent that
    fired T1+T2+T3 before its residual stopped gets credited as a winner
    (+1.1R to +1.5R), not -1R.

    Approximation: assumes partial fills happened at the SPX target levels
    (T1 = +1R, T2 = +2R, T3 = +3R). Actual option-price slippage may
    deviate, but this is much closer to reality than the prior status-only
    +1.1/-1R math.

    Open/working positions return 0 (not yet resolved)."""
    status = (p.status or "").upper()
    if status not in ("CLOSED", "STOPPED", "PARTIAL"):
        return 0.0
    qty = int(p.qty or 0)
    if qty <= 0:
        return 0.0
    t1_qty = int(p.spx_target_t1_qty or 0)
    t2_qty = int(p.spx_target_t2_qty or 0)
    t3_qty = int(p.spx_target_t3_qty or 0)
    if t1_qty + t2_qty + t3_qty == 0:
        # Fallback to canonical 50/30/20 if qty splits weren't recorded
        t1_qty = int(qty * 0.5)
        t2_qty = int(qty * 0.3)
        t3_qty = qty - t1_qty - t2_qty
    t1_share = t1_qty / qty
    t2_share = t2_qty / qty
    t3_share = t3_qty / qty
    r = 0.0
    residual_share = 1.0
    if p.spx_target_t1_fired:
        r += t1_share * 1.0
    residual_share -= t1_share if p.spx_target_t1_fired else 0.0
    if p.spx_target_t2_fired:
        r += t2_share * 2.0
    residual_share -= t2_share if p.spx_target_t2_fired else 0.0
    if p.spx_target_t3_fired:
        r += t3_share * 3.0
    residual_share -= t3_share if p.spx_target_t3_fired else 0.0
    # Settle the residual share (qty that never partial-fired)
    if residual_share > 0.001:
        if status == "STOPPED":
            entry = p.entry_spx_price
            stop = p.spx_target_stop
            dyn = p.dynamic_stop_level or stop
            if entry and stop and dyn:
                risk = abs(entry - stop) or 1.0
                is_lc = (p.right or "").upper() == "C"
                stop_r = (dyn - entry) / risk if is_lc else (entry - dyn) / risk
                r += residual_share * stop_r
            else:
                r += residual_share * (-1.0)
        elif status == "CLOSED":
            # Closed with no remaining qty implies the residual exited
            # somewhere — approximate as T1-equivalent (+1R) for the
            # untracked portion. Conservative for clean wins.
            r += residual_share * 1.0
        # PARTIAL: still open, don't settle residual
    return r


async def compute_daily_review(session_date: str) -> DailyReview:
    """Compute (or recompute) the DailyReview row for the given session_date.
    Overwrites any existing row for that date."""
    from fef_sidecar.db.models import AuditLog, LiveOrder
    async with get_session() as session:
        sigs = list((await session.exec(
            select(SignalIter).where(SignalIter.session_date == session_date)
        )).all())
        bars = list((await session.exec(
            select(HistoricalBar)
            .where(HistoricalBar.symbol == "SPX")
            .where(HistoricalBar.bar_size == "1 min")
        )).all())
        # Real trades from live_order parent rows whose submitted_at falls in session.
        # SPY MIRRORS EXCLUDED (2026-07-17 adversarial review Finding 5): a
        # mirror is the SAME fire at a second venue at 1/10 scale — counting
        # it here double-counts every mirrored fire in wins/R/expectancy.
        # The mirror book is its own ledger: mirror_of IS NOT NULL.
        live_parents = list((await session.exec(
            select(LiveOrder)
            .where(LiveOrder.bracket_role == "parent")
            .where(LiveOrder.dry_run == False)  # noqa: E712
            .where(LiveOrder.mirror_of.is_(None))  # type: ignore[union-attr]
        )).all())
        live_parents = [p for p in live_parents
                        if p.submitted_at.strftime("%Y-%m-%d") == session_date]
        # Audit blocks for block_reason summary.
        audit = list((await session.exec(
            select(AuditLog).where(AuditLog.category == "auto_trade_block")
        )).all())
        audit = [a for a in audit if a.timestamp.strftime("%Y-%m-%d") == session_date]

    bars.sort(key=lambda b: b.timestamp_ms)
    today_actionable = [r for r in sigs if r.direction.value in ("LONG_CALLS", "LONG_PUTS")]

    by_trigger = {"confluence": [], "aligned_only": [], "wall_only": []}
    by_confidence = {"HIGH": [], "MEDIUM": [], "LOW": []}
    by_hour_ct: dict[str, list] = {}
    by_direction = {"LONG_CALLS": [], "LONG_PUTS": []}
    by_regime: dict[str, list] = {}
    by_wall_distance = {"<5pt": [], "5-15pt": [], "15-30pt": [], ">30pt or none": []}
    by_fe_ratio = {"<1": [], "1-3": [], "3-5": [], "5-10": [], ">=10": []}
    tier_counts = Counter()
    realized_total = 0.0

    # Per-signal records for deduplication. Each entry: (epoch_ms,
    # contract_key, outcome, realized_R). After the main loop we group
    # by contract_key + time window and keep only the FIRST signal per
    # group — that gives per-INDEPENDENT-SETUP stats vs the inflated
    # per-signal stats when stacking happens.
    per_signal_records: list[tuple[int, str, str, float]] = []
    DEDUP_WINDOW_MIN = 5

    for r in today_actionable:
        ex = r.extras or {}
        t1 = ex.get("t1"); t2 = ex.get("t2"); t3 = ex.get("t3"); stop = ex.get("stop")
        if not all([t1, t2, t3, stop]):
            continue
        is_lc = r.direction.value == "LONG_CALLS"
        ts = r.timestamp
        if ts.tzinfo is None: ts = ts.replace(tzinfo=UTC)
        start_ms = int(ts.timestamp() * 1000)
        spx = r.spx_price
        out = _trace_outcome(start_ms, spx, t1, t2, t3, stop, is_lc, bars)

        risk = abs(spx - stop) or 1.0
        # R-multiples for each target level
        r_t1 = abs(t1 - spx) / risk
        r_t2 = abs(t2 - spx) / risk
        r_t3 = abs(t3 - spx) / risk
        # Per-tier qty share — the 50/30/20 scale-out split. Tiers that
        # were never hit have their share roll into the residual.
        T1_SHARE, T2_SHARE, T3_SHARE = 0.5, 0.3, 0.2

        # Outcome classification accounts for partials banked BEFORE a
        # stop. The old logic checked `out["stop"] is not None` first
        # and credited only the dynamic-stop R, ignoring T1/T2/T3 that
        # had already fired in earlier bars. On a 50/30/20 scale-out
        # day where T1+T2+T3 all hit before the residual stopped,
        # that produced -1R losses on what were actually +1.5R winners.
        realized_R = 0.0
        residual_share = 1.0
        # Bank partial profits for each tier that fired before stop
        if out["t1"] is not None:
            realized_R += T1_SHARE * r_t1
            residual_share -= T1_SHARE
        if out["t2"] is not None:
            realized_R += T2_SHARE * r_t2
            residual_share -= T2_SHARE
        if out["t3"] is not None:
            realized_R += T3_SHARE * r_t3
            residual_share -= T3_SHARE
        # Settle residual (anything that didn't take a partial)
        if out["stop"] is not None and residual_share > 0:
            dyn = out.get("dyn_stop_at_hit") or stop
            stop_r = (dyn - spx) / risk if is_lc else (spx - dyn) / risk
            realized_R += residual_share * stop_r
        # Outcome label = highest tier reached, else STOP if hit with no
        # tier, else TIMEOUT. tier_counts["T2"] now means "reached T2
        # (and may or may not have also reached T3)" so we count by
        # highest tier to preserve the bucket structure downstream.
        if out["t3"] is not None:
            tier_counts["T3"] += 1; outcome = "T3"
        elif out["t2"] is not None:
            tier_counts["T2"] += 1; outcome = "T2"
        elif out["t1"] is not None:
            tier_counts["T1"] += 1; outcome = "T1"
        elif out["stop"] is not None:
            tier_counts["STOP"] += 1; outcome = "STOP"
        else:
            tier_counts["TIMEOUT"] += 1; outcome = "TIMEOUT"

        realized_total += realized_R

        # Record for deduplication. Contract key matches what the engine
        # produces — strike_rec from extras (the actual contract that
        # would be bought) + direction + today's expiry.
        strike = ex.get("strike_rec")
        contract_key = f"{r.direction.value}|{strike}"
        per_signal_records.append((start_ms, contract_key, outcome, realized_R))

        wall = bool(ex.get("wall_collapse_trigger"))
        aligned = bool(ex.get("aligned"))
        if wall and aligned:
            by_trigger["confluence"].append(realized_R)
        elif aligned:
            by_trigger["aligned_only"].append(realized_R)
        elif wall:
            by_trigger["wall_only"].append(realized_R)
        by_confidence[r.confidence.value if hasattr(r.confidence, "value") else str(r.confidence)] = (
            by_confidence.get(r.confidence.value if hasattr(r.confidence, "value") else str(r.confidence), []))
        try:
            by_confidence[r.confidence.value if hasattr(r.confidence, "value") else str(r.confidence)].append(realized_R)
        except KeyError:
            pass
        ct = ts.astimezone(timezone(timedelta(hours=-5)))
        hour_key = f"{ct.hour:02d}"
        by_hour_ct.setdefault(hour_key, []).append(realized_R)
        # By direction
        by_direction[r.direction.value].append(realized_R)
        # By regime (from extras if signal was demoted, else 'normal')
        regime_at_fire = ex.get("regime_at_fire") or (
            (ex.get("regime_demoted") or {}).get("regime") or "NORMAL"
        )
        by_regime.setdefault(regime_at_fire, []).append(realized_R)
        # By wall distance (proximity to nearest dominant wall at entry)
        nwd = ex.get("nearest_wall_distance")
        if nwd is None: bucket = ">30pt or none"
        elif nwd < 5: bucket = "<5pt"
        elif nwd < 15: bucket = "5-15pt"
        elif nwd < 30: bucket = "15-30pt"
        else: bucket = ">30pt or none"
        by_wall_distance[bucket].append(realized_R)
        # By FE ratio
        fe = ex.get("fe_ratio")
        if fe is None: pass
        elif fe < 1: by_fe_ratio["<1"].append(realized_R)
        elif fe < 3: by_fe_ratio["1-3"].append(realized_R)
        elif fe < 5: by_fe_ratio["3-5"].append(realized_R)
        elif fe < 10: by_fe_ratio["5-10"].append(realized_R)
        else: by_fe_ratio[">=10"].append(realized_R)

    n_actionable = len(today_actionable)
    # wins = signals with positive realized R (matches sub-aggregate
    # _summary definition so top-level and per-direction/hour/regime
    # buckets agree). Includes BE-ladder-locked stops where the
    # residual exits above entry, which the strict tier-count misses.
    wins = sum(1 for _ts, _ck, _out, r in per_signal_records if r > 0)
    stops = tier_counts.get("STOP", 0)
    timeouts = tier_counts.get("TIMEOUT", 0)

    # Dedup pass: delegates to engine.dedup so the rule stays identical
    # across daily review + counterfactual + any future caller.
    # contract_key matches the canonical "{direction}|{strike}" format —
    # we adapt per_signal_records into signal dicts the util consumes.
    from fef_sidecar.engine.dedup import deduplicate_signals
    per_signal_records.sort(key=lambda x: x[0])
    _signals_for_dedup = [
        {"timestamp_ms": ts_ms, "_outcome_and_R": (outcome, r_realized),
         # Reconstruct direction+strike from contract_key "DIR|STRIKE"
         "direction": ck.split("|")[0] if "|" in ck else ck,
         "strike": float(ck.split("|")[1]) if "|" in ck and ck.split("|")[1]
                   .replace(".", "").replace("-", "").isdigit() else 0.0}
        for ts_ms, ck, outcome, r_realized in per_signal_records
    ]
    _kept, _stats = deduplicate_signals(_signals_for_dedup,
                                         window_min=DEDUP_WINDOW_MIN)
    dedup_records: list[tuple[str, float]] = [s["_outcome_and_R"] for s in _kept]
    dedup_outcomes = Counter(o for o, _ in dedup_records)
    dedup_n = len(dedup_records)
    dedup_wins_n = (dedup_outcomes.get("T1", 0) + dedup_outcomes.get("T2", 0)
                    + dedup_outcomes.get("T3", 0))
    dedup_stops_n = dedup_outcomes.get("STOP", 0)
    dedup_timeouts_n = dedup_outcomes.get("TIMEOUT", 0)
    dedup_realized_total = sum(r for _, r in dedup_records)

    # Risk-adjusted performance metrics — computed from the dedup R series
    # since stacking-on-same-contract correlation inflates raw per-signal
    # variance. Dedup gives one R per setup (closer to portfolio-realistic).
    all_R = [r for _, r in dedup_records]
    sharpe_ratio = None
    sortino_ratio = None
    max_drawdown_R = None
    profit_factor = None
    ulcer_index = None
    if len(all_R) >= 2:
        import statistics as _stat
        import math as _math
        mean_R = _stat.mean(all_R)
        std_R = _stat.stdev(all_R)
        if std_R > 0:
            # Sharpe — not annualized; this is per-signal Sharpe. Caller can
            # multiply by sqrt(N_per_year) if they want annualization.
            sharpe_ratio = round(mean_R / std_R, 3)
        downside = [r for r in all_R if r < 0]
        if len(downside) >= 2:
            ds_std = _stat.stdev(downside)
            if ds_std > 0:
                sortino_ratio = round(mean_R / ds_std, 3)
        # Max drawdown on cumulative-R equity curve
        cum = 0.0; peak = 0.0; max_dd = 0.0
        for r in all_R:
            cum += r
            if cum > peak: peak = cum
            dd = peak - cum
            if dd > max_dd: max_dd = dd
        max_drawdown_R = round(max_dd, 3)
        # Profit factor — gross wins / abs(gross losses)
        gross_win = sum(r for r in all_R if r > 0)
        gross_loss = abs(sum(r for r in all_R if r < 0))
        if gross_loss > 0:
            profit_factor = round(gross_win / gross_loss, 3)
        # Ulcer Index — RMS of pct drawdown, penalises long drawdowns
        if peak > 0:
            cum2 = 0.0; peak2 = 0.0
            dd_pcts_sq = []
            for r in all_R:
                cum2 += r
                if cum2 > peak2: peak2 = cum2
                if peak2 > 0:
                    dd_pct = (peak2 - cum2) / peak2 * 100
                    dd_pcts_sq.append(dd_pct ** 2)
            if dd_pcts_sq:
                ulcer_index = round(_math.sqrt(sum(dd_pcts_sq) / len(dd_pcts_sq)), 3)

    def _summary(rs: list) -> dict:
        n = len(rs)
        wn = sum(1 for r in rs if r > 0)
        return {"n": n, "wins": wn, "win_pct": round(wn / n * 100, 1) if n else 0,
                "expectancy_r": round(sum(rs) / n, 3) if n else 0}

    # Block reason aggregation — prefers the structured reason_code from
    # AutoTradeDecision (BlockReason enum). Falls back to fuzzy string
    # match on the human reason for older rows pre-enum refactor.
    block_reasons = Counter()
    for a in audit:
        details = a.details or {}
        code = details.get("reason_code")
        if code:
            block_reasons[code] += 1
            continue
        reason = details.get("reason", "unknown")
        if "cool-down" in reason: reason = "COOL_DOWN"
        elif "below floor" in reason: reason = "BELOW_FLOOR"
        elif "no option pricing" in reason: reason = "NO_OPTION_PRICING"
        elif "invalid option_pricing" in reason: reason = "INVALID_PRICING"
        elif "calendar pause" in reason: reason = "CALENDAR_PAUSE"
        elif "global session cap" in reason: reason = "GLOBAL_CAP"
        elif "sub-cap" in reason: reason = "SYMBOL_CAP"
        elif "WAIT" in reason: reason = "VERDICT_WAIT"
        elif "no strike_rec" in reason: reason = "NO_STRIKE"
        elif "place_bracket" in reason: reason = "PLACE_ERROR"
        elif "FEF_LIVE_TRADING" in reason: reason = "ENV_NOT_SET"
        elif "disabled" in reason: reason = "DISABLED"
        else: reason = "UNKNOWN"
        block_reasons[reason] += 1

    # Actual trades (real fills). Source of truth = broker-confirmed
    # ``ibkr_execution`` rows, FIFO-paired-off per (symbol, strike, right,
    # expiry) and attributed back to parents via ``parent_id``. The
    # canonical helper lives in ``engine/exec_pair_off`` so daily_review
    # and ``recompute_daily_session_wl.py`` produce bit-for-bit identical
    # numbers.
    #
    #   - ``actual_total_R``  = SESSION-LEVEL realized $ from FIFO pair-off
    #     divided by per-R dollar assumption (FEF_R_DOLLARS env, default
    #     400 = 5 contracts × $2000 risk).
    #   - ``actual_wins`` / ``actual_losses`` = PER-PARENT W/L from FIFO
    #     attribution. A partially-scaled parent counts as W/L on the
    #     realized portion (residual still long counts as "open" but
    #     doesn't preempt classification).
    #
    # Fallback: when there are NO execs for the session (paper-only /
    # shadow days), fall back to ``_estimate_parent_r`` so dry-run /
    # mock sessions still get a signed W/L count from the SPX-target
    # flag estimator. That path is intentionally secondary now.
    #
    # FIFO method (vs the previous avg-price approximation): on partial
    # scale-out days the avg-price approach drifts by a few percent from
    # the true FIFO sum (e.g. 5/22 differs by $70). FIFO matches
    # ``daily_session.gross_pnl`` to the penny on 5/19, 5/20, 5/21.
    import os as _os
    from fef_sidecar.engine.exec_pair_off import compute_parent_wl_from_fills
    R_DOLLARS = float(_os.environ.get("FEF_R_DOLLARS", "400"))
    fill_summary = await compute_parent_wl_from_fills(session_date)
    actual_trades = len(live_parents)
    if fill_summary.n_execs > 0:
        # Primary path: FIFO from broker fills
        realized_dollars = fill_summary.total_realized_dollars
        actual_total_R = (
            round(realized_dollars / R_DOLLARS, 2) if R_DOLLARS > 0 else 0.0
        )
        actual_wins = fill_summary.wins
        actual_losses = fill_summary.losses
        best_pnl = fill_summary.best_pnl
        worst_pnl = fill_summary.worst_pnl
    else:
        # Fallback path: no broker fills (paper / shadow / dry-run only).
        # Use the legacy SPX-target flag estimator so dry-run sessions
        # still get a signed W/L count.
        realized_dollars = 0.0
        actual_total_R = 0.0
        actual_wins = 0
        actual_losses = 0
        for p in live_parents:
            r_est = _estimate_parent_r(p)
            if r_est > 0.05:
                actual_wins += 1
            elif r_est < -0.05:
                actual_losses += 1
        best_pnl = 0.0
        worst_pnl = 0.0

    review = DailyReview(
        session_date=session_date,
        computed_at=datetime.now(UTC),
        total_signals=len(sigs),
        actionable_signals=n_actionable,
        wins=wins, stops=stops, timeouts=timeouts,
        win_rate_pct=round(wins / (wins + stops) * 100, 1) if (wins + stops) else 0,
        expectancy_r=round(realized_total / n_actionable, 3) if n_actionable else 0,
        t1_hits=tier_counts.get("T1", 0),
        t2_hits=tier_counts.get("T2", 0),
        t3_hits=tier_counts.get("T3", 0),
        by_trigger={k: _summary(v) for k, v in by_trigger.items()},
        by_confidence={k: _summary(v) for k, v in by_confidence.items()},
        by_hour_ct={k: _summary(v) for k, v in by_hour_ct.items()},
        by_direction={k: _summary(v) for k, v in by_direction.items()},
        by_regime={k: _summary(v) for k, v in by_regime.items()},
        by_wall_distance={k: _summary(v) for k, v in by_wall_distance.items()},
        by_fe_ratio={k: _summary(v) for k, v in by_fe_ratio.items()},
        by_block_reason=dict(block_reasons),
        actual_trades_taken=actual_trades,
        actual_wins=actual_wins,
        actual_losses=actual_losses,
        actual_total_R=round(actual_total_R, 2),
        dedup_actionable=dedup_n,
        dedup_wins=dedup_wins_n,
        dedup_stops=dedup_stops_n,
        dedup_timeouts=dedup_timeouts_n,
        dedup_win_rate_pct=round(dedup_wins_n / (dedup_wins_n + dedup_stops_n) * 100, 1) if (dedup_wins_n + dedup_stops_n) else 0.0,
        dedup_expectancy_r=round(dedup_realized_total / dedup_n, 3) if dedup_n else 0.0,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        max_drawdown_R=max_drawdown_R,
        profit_factor=profit_factor,
        ulcer_index=ulcer_index,
    )

    # Upsert: delete + insert in a single transaction to avoid data loss
    # if the process crashes between the two operations.
    async with get_session() as session:
        existing = (await session.exec(
            select(DailyReview).where(DailyReview.session_date == session_date)
        )).first()
        if existing is not None:
            await session.delete(existing)
            await session.flush()
        session.add(review)
        await session.commit()
        await session.refresh(review)

    # Cascade: keep DailySession in sync from the SAME source of truth
    # (FIFO pair-off, via compute_parent_wl_from_fills above). Net = gross
    # for now (commission data isn't reliably captured on ibkr_execution
    # — paper accounts report $0 commissions even with realistic fees).
    # `best_pnl` / `worst_pnl` are PER-PARENT extremes from the FIFO
    # summary — the prior sign-based per-contract aggregate was confused
    # by partial scale-outs and produced misleading best/worst values.
    from fef_sidecar.db.models import DailySession
    async with get_session() as session:
        existing_ds = (await session.exec(
            select(DailySession).where(DailySession.session_date == session_date)
        )).first()
        if existing_ds is not None:
            existing_ds.total_iters = len(sigs)
            existing_ds.actionable_signals = n_actionable
            existing_ds.trades_taken = actual_trades
            existing_ds.gross_pnl = round(realized_dollars, 2)
            existing_ds.net_pnl = round(realized_dollars, 2)
            existing_ds.win_count = actual_wins
            existing_ds.loss_count = actual_losses
            existing_ds.best_trade_pnl = round(best_pnl, 2)
            existing_ds.worst_trade_pnl = round(worst_pnl, 2)
            existing_ds.updated_at = datetime.now(UTC)
            session.add(existing_ds)
        else:
            session.add(DailySession(
                session_date=session_date,
                total_iters=len(sigs),
                actionable_signals=n_actionable,
                trades_taken=actual_trades,
                gross_pnl=round(realized_dollars, 2),
                net_pnl=round(realized_dollars, 2),
                win_count=actual_wins,
                loss_count=actual_losses,
                best_trade_pnl=round(best_pnl, 2),
                worst_trade_pnl=round(worst_pnl, 2),
            ))
        await session.commit()

    await SignalIterRepository.write_audit_log(
        category="daily_review", actor="system",
        summary=f"DailyReview {session_date}: {n_actionable} fires, {wins} won "
                f"({review.win_rate_pct}%), expectancy {review.expectancy_r:+.3f}R",
    )
    logger.info("DailyReview computed for %s: %d signals %.1f%% wins %.3fR exp",
                session_date, n_actionable, review.win_rate_pct, review.expectancy_r)
    return review
