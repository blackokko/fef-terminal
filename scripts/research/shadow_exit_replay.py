"""Shadow exit-logic replay over live_order parents.

Post-hoc counterfactual: for each parent order placed during the date
window, walk forward through SPX 1-min bars from the parent's entry_spx
+ submitted_at and compute what each registered exit variant would have
realized. Persisted into `shadow_exit_outcome` (one row per parent ×
variant) for downstream analysis without ever touching the live engine's
exit code path.

Built 2026-05-21 after Chat recommended observational shadow mode for
B+C+D during the remaining freeze sessions. Generalized from the
original B+C+D-specific replay so any new exit variant — tighter
trail, different partial shares, time-based exits — can be tested
without a schema change. Variants register themselves in REGISTERED_VARIANTS.

Methodology matches scripts/research/ablation_backtest.py (so deltas
between this replay and ablation results reflect entry universe — actual
live parents vs all fired signals — not methodology drift).

Adding a new variant (the only thing you should ever edit):
    REGISTERED_VARIANTS = [
        ...,
        ExitVariant(
            label="bcd_tight_trail",
            stop_r=-1.0,
            targets=[(1.0, 1.0)],
            trail_peak_floor_r=0.4,        # stricter activation
            trail_pullback_r=0.10,         # tighter pullback
        ),
    ]

CLI:
    python shadow_exit_replay.py 2026-05-18 2026-05-21
    python shadow_exit_replay.py 2026-05-18 2026-05-21 --variants bcd_trail_15pct,baseline_partial
    python shadow_exit_replay.py 2026-05-18 2026-05-21 --account-class paper --json

Programmatic:
    from shadow_exit_replay import run_replay, REGISTERED_VARIANTS, ExitVariant
    result = run_replay("2026-05-18", "2026-05-21")
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = str(Path.home() / ".fef-terminal" / "signal_log.db")
DOLLARS_PER_R_PER_CONTRACT = 500.0  # 10pt SPX × $50/pt × 1 contract


# ---------------------------------------------------------------------------
# Variant definition + registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExitVariant:
    """A named exit-logic configuration. All R-values are in R-units
    where 1R = 10 SPX points (FEF's 0DTE convention).

    Fields:
        label: stable identifier used as the row key in shadow_exit_outcome.
        stop_r: adverse R-level that closes the residual share. Negative.
        targets: ordered list of (favor_r_level, share_fraction). Share
            fractions should sum to ≤ 1.0; whatever is left when targets
            are exhausted exits either at trail or at timeout. Set to
            [(1.0, 1.0)] for a single hard +1R target (B+C+D's style).
            Set to [(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)] for the standard
            50/30/20 partial scale-out.
        trail_peak_floor_r: peak_r must reach this level before the trail
            arms. None = trail disabled entirely.
        trail_pullback_r: once armed, trail exits residual when the bar's
            close drops to (peak_r - trail_pullback_r). In R-units.
        timeout_minutes: total forward window for the walk. Residual at
            the last bar exits at its close.
        regime_routes: optional regime → variant_label mapping. When set,
            this variant becomes a *meta-variant*: for each parent,
            look up the parent's regime_at_fire and dispatch to the routed
            leaf variant. The leaf's stop/targets/trail fields are used —
            this variant's own fields are ignored. Use default_route for
            regimes not in the dict.
        default_route: fallback variant_label when regime is missing or
            not present in regime_routes. Required if regime_routes set.
    """
    label: str
    stop_r: float = -1.0
    targets: list[tuple[float, float]] = field(default_factory=list)
    trail_peak_floor_r: float | None = None
    trail_pullback_r: float | None = None
    timeout_minutes: int = 60
    regime_routes: dict[str, str] | None = None
    default_route: str | None = None
    # Optional entry-side gate. None = always trade. When set, the
    # variant evaluates the gate at parent entry time; if it blocks,
    # the row is persisted with realized_r=0 (representing "would have
    # skipped this fire"). Composes cleanly with any exit logic so we
    # can isolate the entry filter as the only knob changed vs baseline.
    # Supported kinds: 'direction_30m' (params: threshold_pts).
    entry_filter: dict | None = None
    # Break-even ladder — ordered list of (peak_r_trigger, new_stop_r).
    # Once peak_r crosses a trigger, the dynamic stop ratchets UP to
    # new_stop_r (only upward, never down). Captures the live engine's
    # runner-protection behavior. spx_target_watcher.py uses
    # [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)]: hit +0.5R → stop to BE;
    # hit +1.0R → lock +0.5R; hit +1.5R → lock +1R. The variants
    # without be_ladder set keep the original -1R fixed stop (a
    # pessimistic-vs-live model — useful for measuring the BE ladder's
    # incremental contribution).
    be_ladder: list[tuple[float, float]] | None = None

    @property
    def is_meta(self) -> bool:
        return self.regime_routes is not None

    def to_dict(self) -> dict:
        d = asdict(self)
        # tuple → list for JSON storage
        d["targets"] = [list(t) for t in self.targets]
        return d


# Canonical variants. Append to this list to register new ones; the
# replay loop picks them up automatically.
REGISTERED_VARIANTS: list[ExitVariant] = [
    ExitVariant(
        # The current live exit logic shape: 50/30/20 partial scale-out
        # with -1R stop, no trail. Matches ablation_backtest._simulate_baseline.
        label="baseline_partial_50_30_20",
        stop_r=-1.0,
        targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
    ),
    ExitVariant(
        # B+C+D as evaluated in ablation_backtest._simulate_bcd: 100%
        # hard exit at T1 with peak-trail at 15% pullback after peak >= 0.3R.
        # This is the "headline winner" from the P1 ablation.
        label="bcd_trail_15pct",
        stop_r=-1.0,
        targets=[(1.0, 1.0)],
        trail_peak_floor_r=0.3,
        trail_pullback_r=0.15,
    ),
    ExitVariant(
        # Regime-conditional B+C+D — added 2026-05-21 per Chat's
        # recommendation after the shadow replay surfaced the
        # trending-vs-chop split (B+C+D costs $5k on 5/19's trend day,
        # saves $32k on 5/20+5/21 chop/red days).
        # Routes:
        #   TREND_DAY / DRIFT_BULL → baseline (timeouts ride the trend)
        #   everything else        → bcd_trail_15pct (trail saves stops)
        # All fields below (stop_r, targets, ...) are IGNORED — the
        # routed leaf variant's params are used at simulate time.
        label="bcd_only_in_chop",
        regime_routes={
            "TREND_DAY": "baseline_partial_50_30_20",
            "DRIFT_BULL": "baseline_partial_50_30_20",
        },
        default_route="bcd_trail_15pct",
    ),
    # Trail-pullback parameter sweep — registered 2026-05-21 per Chat's
    # "register variants proactively so you have data by session 10" plan.
    # Identical to bcd_trail_15pct except the pullback knob, so the 3-way
    # comparison (10% / 15% / 25%) isolates the trail tightness as the
    # only variable. Tests whether 15% is at a noise-influenced local
    # optimum or actually the right pullback distance.
    ExitVariant(
        # Tighter trail — exits residual faster after MFE pullback.
        # Hypothesis: helps on fast-reversal days like 5/21 where the
        # current 15% lets too much MFE leak before triggering.
        label="bcd_trail_10pct",
        stop_r=-1.0,
        targets=[(1.0, 1.0)],
        trail_peak_floor_r=0.3,
        trail_pullback_r=0.10,
    ),
    ExitVariant(
        # Looser trail — lets winners run longer before pullback exit.
        # Hypothesis: helps on trending days like 5/19 where the current
        # 15% triggered too early on small intra-bar dips and missed
        # subsequent runs (cost $5k vs baseline on that session).
        label="bcd_trail_25pct",
        stop_r=-1.0,
        targets=[(1.0, 1.0)],
        trail_peak_floor_r=0.3,
        trail_pullback_r=0.25,
    ),
    # ----- Other deferred-change tests (added 2026-05-21) -----
    # Each variant below differs from the current live baseline by
    # exactly one knob, so the comparison isolates that knob's effect.
    ExitVariant(
        # Chat-suggested: pure trail, no hard T1 target. Tests whether
        # the +1R hard exit is adding value or whether the trail alone
        # is doing all the work. Same stop / trail config as
        # bcd_trail_15pct but with targets=[] (no scale-out).
        label="pure_trail_15pct",
        stop_r=-1.0,
        targets=[],
        trail_peak_floor_r=0.3,
        trail_pullback_r=0.15,
    ),
    ExitVariant(
        # Chat-suggested: baseline structure with longer timeout window.
        # Tests whether 60min is cutting trending-day residual too early.
        label="baseline_timeout_90min",
        stop_r=-1.0,
        targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
        timeout_minutes=90,
    ),
    ExitVariant(
        # Deferred change: direction filter at entry (the ablation
        # variant we discussed but didn't ship). Filter logic identical
        # to ablation_backtest's direction filter — block PUTS when 30m
        # SPX > +3pt; block CALLS when 30m SPX < -3pt. Pairs with
        # baseline exit so the filter is the only knob changed vs the
        # current live logic (NOT mixed with B+C+D exit, per isolation
        # requirement).
        label="entry_dir_filter_then_baseline",
        stop_r=-1.0,
        targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
        entry_filter={"kind": "direction_30m", "params": {"threshold_pts": 3.0}},
    ),
    # ----- BE-ladder variants -----
    # Critical discovery 2026-05-21: spx_target_watcher.py applies a BE
    # ladder [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)] to all parents. The
    # variants above (no be_ladder set) are pessimistic vs actual live
    # behavior — they show stop-outs at -1R that the live engine would
    # have ratcheted to BE before adverse moves hit. The two variants
    # below model the ladder explicitly so we can measure (a) accurate
    # current-live behavior, and (b) the 2b+B+C+D combination the user
    # asked about (runner protection from BE ladder + B+C+D's trail).
    ExitVariant(
        # Accurate model of current live exit logic: partials + the
        # live BE ladder. The right reference point for measuring the
        # B+C+D shadow's incremental edge vs what's actually running.
        label="baseline_partial_with_be_ladder",
        stop_r=-1.0,
        targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
        be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
    ),
    ExitVariant(
        # The "Option 2b + B+C+D" combination: BE ladder protection on
        # runners (stop ratchets up as MFE increases) + B+C+D's
        # peak-trail (exit on pullback from peak). Two protection
        # mechanisms that compose rather than conflict — ladder
        # prevents giving back ALL of MFE on a deep reversal, trail
        # exits earlier when intra-trade pullback exceeds threshold.
        label="bcd_trail_15pct_with_be_ladder",
        stop_r=-1.0,
        targets=[(1.0, 1.0)],
        trail_peak_floor_r=0.3,
        trail_pullback_r=0.15,
        be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
    ),
    ExitVariant(
        # And: pure-trail + BE ladder. Tonight's surprise headline
        # winner (pure_trail_15pct, +$32k vs baseline) PLUS the BE
        # ladder runner protection. If trail-alone is doing the work
        # and BE ladder protects further-out runners, this should
        # combine the best of both.
        label="pure_trail_15pct_with_be_ladder",
        stop_r=-1.0,
        targets=[],
        trail_peak_floor_r=0.3,
        trail_pullback_r=0.15,
        be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
    ),
]


def _variant_by_label(label: str) -> ExitVariant | None:
    return next((v for v in REGISTERED_VARIANTS if v.label == label), None)


def _evaluate_entry_filter(
    entry_filter: dict,
    direction_sign: int,
    prior_bars: list[dict],
) -> tuple[bool, str]:
    """Returns (passes, reason). `passes=False` means the trade is
    blocked at entry and the variant's row should be persisted with
    realized_r=0. Pure read-only — adding new filter kinds is just a
    new elif branch here."""
    kind = entry_filter.get("kind")
    params = entry_filter.get("params") or {}
    if kind == "direction_30m":
        # Block PUTS when SPX moved up ≥ threshold pts over the prior
        # 30 1-min bars; block CALLS when SPX moved down. Same logic as
        # ablation_backtest's direction filter — moved into the shadow
        # framework so we can isolate the filter's effect on real entries.
        threshold = float(params.get("threshold_pts", 3.0))
        if len(prior_bars) < 5:
            return True, "no_prior_data"
        spx_30m_change = prior_bars[-1]["close"] - prior_bars[0]["close"]
        if direction_sign > 0 and spx_30m_change < -threshold:
            return False, f"calls_blocked_spx_30m_{spx_30m_change:+.1f}pt"
        if direction_sign < 0 and spx_30m_change > threshold:
            return False, f"puts_blocked_spx_30m_{spx_30m_change:+.1f}pt"
        return True, "passed"
    return True, f"unknown_kind:{kind}"


# ---------------------------------------------------------------------------
# Parametric bar-walk simulator
# ---------------------------------------------------------------------------

def simulate_variant(
    entry_spx: float,
    direction_sign: int,
    fwd_bars: list[dict],
    variant: ExitVariant,
) -> dict:
    """Walk fwd_bars in order, applying the variant's stop / targets /
    trail rules. Stop-priority on ambiguous bars (matches the rest of the
    research stack — conservative). Returns:
        {realized_r, exit_reason, peak_r, exit_ms}

    exit_reason ∈ {'no_data', 'stop', 'targets', 'trail', 'timeout'}.
    """
    if not fwd_bars:
        return {"realized_r": 0.0, "exit_reason": "no_data",
                "peak_r": 0.0, "exit_ms": None}

    realized_r = 0.0
    remaining_share = 1.0
    tiers_fired: set[int] = set()
    peak_r = 0.0
    # Dynamic stop — starts at variant.stop_r, ratchets via be_ladder.
    # Conservative ordering: a bar's adverse_r is checked against the
    # PREVIOUS bar's dynamic_stop_r (i.e., ratchet only takes effect
    # on the NEXT bar). Matches the live engine where the watcher
    # updates state after observing the bar, so adverse-move-this-bar
    # uses the stop in effect at bar start.
    dynamic_stop_r = variant.stop_r
    ladder = sorted(variant.be_ladder or [], key=lambda x: x[0])
    trail_enabled = (
        variant.trail_peak_floor_r is not None
        and variant.trail_pullback_r is not None
    )

    for b in fwd_bars:
        adverse_pts = b["low"] if direction_sign > 0 else b["high"]
        favor_pts = b["high"] if direction_sign > 0 else b["low"]
        adverse_r = direction_sign * (adverse_pts - entry_spx) / 10.0
        favor_r = direction_sign * (favor_pts - entry_spx) / 10.0

        # 1. Stop (priority on ambiguous bars). Uses the dynamic stop
        # as ratcheted by prior bars — this bar's peak update hasn't
        # happened yet.
        if adverse_r <= dynamic_stop_r and remaining_share > 0:
            realized_r += remaining_share * dynamic_stop_r
            return {"realized_r": realized_r,
                    "exit_reason": "stop" if dynamic_stop_r <= variant.stop_r + 1e-9
                                   else "be_stop",
                    "peak_r": peak_r, "exit_ms": b["timestamp_ms"]}

        # Update peak now that we know the bar's high didn't precede
        # the stop. (For partials/trail we check vs this bar's
        # favorable extreme below.)
        if favor_r > peak_r:
            peak_r = favor_r

        # BE ladder ratchet — only after peak update + stop check, so
        # the new (higher) stop applies starting next bar.
        for trig, new_stop in ladder:
            if peak_r >= trig and new_stop > dynamic_stop_r:
                dynamic_stop_r = new_stop

        # 2. Targets in order — fire any not yet hit
        for idx, (r_level, share) in enumerate(variant.targets):
            if idx in tiers_fired:
                continue
            if favor_r >= r_level and remaining_share > 0:
                actual_share = min(share, remaining_share)
                realized_r += actual_share * r_level
                remaining_share -= actual_share
                tiers_fired.add(idx)

        # 3. All targets exhausted → exit
        if remaining_share <= 0.001:
            return {"realized_r": realized_r, "exit_reason": "targets",
                    "peak_r": peak_r, "exit_ms": b["timestamp_ms"]}

        # 4. Trail exit on residual (after target fires this bar)
        if trail_enabled and peak_r >= (variant.trail_peak_floor_r or 0):
            current_r = direction_sign * (b["close"] - entry_spx) / 10.0
            trail_level = peak_r - (variant.trail_pullback_r or 0)
            if current_r <= trail_level and remaining_share > 0:
                realized_r += remaining_share * trail_level
                return {"realized_r": realized_r, "exit_reason": "trail",
                        "peak_r": peak_r, "exit_ms": b["timestamp_ms"]}

    # Timeout: residual exits at last close
    if remaining_share > 0:
        final_r = direction_sign * (fwd_bars[-1]["close"] - entry_spx) / 10.0
        realized_r += remaining_share * final_r
    return {"realized_r": realized_r, "exit_reason": "timeout",
            "peak_r": peak_r, "exit_ms": fwd_bars[-1]["timestamp_ms"]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts_to_ms(ts_str: str) -> int:
    dt = datetime.fromisoformat(ts_str.replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _resolve_account_class(notes: str | None, accept: set[str]) -> bool:
    """live_order.notes encodes account_class as 'account_class=paper' etc."""
    if not notes:
        return ("paper" in accept) or ("unknown" in accept)
    cls = "unknown"
    for part in notes.split(";"):
        part = part.strip()
        if part.startswith("account_class="):
            cls = part.split("=", 1)[1].strip().lower() or "unknown"
            break
    if cls in accept:
        return True
    if cls == "unknown" and "paper" in accept:
        return True
    return False


def _parse_account_class_arg(raw: str) -> set[str]:
    if not raw:
        return {"paper"}
    raw = raw.strip().lower()
    if raw == "all":
        return {"paper", "live", "mock", "unknown"}
    return {x.strip() for x in raw.split(",") if x.strip()}


# ---------------------------------------------------------------------------
# Replay entry point
# ---------------------------------------------------------------------------

def run_replay(
    date_from: str,
    date_to: str,
    db_path: str = DEFAULT_DB,
    accept_classes: set[str] | None = None,
    variants: list[ExitVariant] | None = None,
    persist: bool = True,
) -> dict:
    """Replay every parent live_order in the window through every variant.

    Args:
        date_from / date_to: YYYY-MM-DD inclusive.
        db_path: sqlite path. Defaults to ~/.fef-terminal/signal_log.db.
        accept_classes: account_class filter (default {'paper'}).
        variants: list of ExitVariant to test. Defaults to
            REGISTERED_VARIANTS so adding a variant to the registry
            makes it appear in the next run automatically.
        persist: whether to upsert into shadow_exit_outcome.
    """
    accept = accept_classes or {"paper"}
    variants = variants or REGISTERED_VARIANTS
    if not variants:
        return {"date_from": date_from, "date_to": date_to,
                "n_parents": 0, "rows": [], "by_variant": {}}

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    # Include STOPPED parents — those are exactly the failure-mode cases
    # B+C+D / trail-variants are designed to test against (would they have
    # trail-exited with profit before the bracket stop hit?). Skip
    # CANCELLED / REJECTED / SUBMITTED-only parents — those never resulted
    # in real positions.
    parents = cur.execute(
        """
        SELECT id, submitted_at, symbol, strike, right, expiry, qty,
               avg_fill_price, entry_spx_price, status, notes
        FROM live_order
        WHERE bracket_role = 'parent'
          AND status IN ('FILLED', 'CLOSED', 'PARTIAL', 'STOPPED')
          AND entry_spx_price IS NOT NULL
          AND substr(submitted_at, 1, 10) BETWEEN ? AND ?
        ORDER BY submitted_at
        """,
        (date_from, date_to),
    ).fetchall()
    parents = [p for p in parents if _resolve_account_class(p["notes"], accept)]

    # Pull SPX 1-min bars with 2h forward buffer (last fire of day needs
    # full lookahead window).
    bars = cur.execute(
        """
        SELECT timestamp_ms, open, high, low, close
        FROM historical_bar
        WHERE symbol = 'SPX' AND bar_size = '1 min'
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

    def bars_before(ms: int, n: int) -> list[dict]:
        """Up to n 1-min bars strictly prior to ms, in chronological order."""
        idx = bisect.bisect_left(bar_ms_sorted, ms)
        return [bar_by_ms[m] for m in bar_ms_sorted[max(0, idx - n):idx]]

    # Build parent_id → regime_at_fire lookup. Match each parent to the
    # closest signal_iter row in the 5-minute window preceding its
    # submitted_at; parse extras JSON for regime_at_fire. Used by
    # meta-variants (regime_routes) — leaf variants ignore it.
    parent_regimes: dict[int, str] = {}
    if any(v.is_meta for v in variants) and parents:
        parent_ids = [p["id"] for p in parents]
        sig_rows = cur.execute(
            f"""
            SELECT id, timestamp, extras, session_date
            FROM signal_iter
            WHERE session_date BETWEEN ? AND ?
            ORDER BY timestamp
            """,
            (date_from, date_to),
        ).fetchall()
        sigs_by_session: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for s in sig_rows:
            sigs_by_session[s["session_date"]].append((s["timestamp"], s["extras"]))
        for p in parents:
            parent_ms = _ts_to_ms(p["submitted_at"])
            session_date = p["submitted_at"][:10]
            best_regime = "UNKNOWN"
            best_diff = 5 * 60 * 1000  # 5min window
            for sig_ts, sig_extras in sigs_by_session.get(session_date, []):
                sig_ms = _ts_to_ms(sig_ts)
                diff = parent_ms - sig_ms
                if 0 <= diff <= best_diff:
                    best_diff = diff
                    try:
                        ex = json.loads(sig_extras) if sig_extras else {}
                        regime = (
                            ex.get("regime_at_fire")
                            or (ex.get("regime_demoted") or {}).get("regime")
                            or "NORMAL"
                        )
                        best_regime = regime
                    except (json.JSONDecodeError, AttributeError):
                        pass
            parent_regimes[p["id"]] = best_regime

    rows: list[dict] = []
    for p in parents:
        entry_spx = float(p["entry_spx_price"])
        direction = "LONG_CALLS" if p["right"] == "C" else "LONG_PUTS"
        direction_sign = +1 if direction == "LONG_CALLS" else -1
        entry_ms = _ts_to_ms(p["submitted_at"])
        qty = int(p["qty"] or 0)
        session_date = p["submitted_at"][:10]

        for v in variants:
            # Resolve meta-variants to a leaf variant for this parent's
            # regime. Leaf variants simulate directly.
            if v.is_meta:
                regime = parent_regimes.get(p["id"], "UNKNOWN")
                routed_label = (v.regime_routes or {}).get(regime, v.default_route)
                leaf = _variant_by_label(routed_label) if routed_label else None
                if leaf is None or leaf.is_meta:
                    # Skip — misconfigured meta-variant; persist nothing.
                    continue
                sim_variant = leaf
                params_snapshot = {
                    "meta": v.to_dict(),
                    "resolved_regime": regime,
                    "resolved_leaf": leaf.to_dict(),
                }
            else:
                sim_variant = v
                params_snapshot = v.to_dict()

            # Entry filter — gate evaluated before the bar walk. When the
            # filter blocks the trade, persist realized_r=0 so portfolio
            # aggregates correctly treat this as "did not trade." Filter
            # is stored on the *meta or leaf* whichever exposed it; we
            # check both so a meta-variant can specify a filter and route
            # to leaf exit logic independently.
            applicable_filter = v.entry_filter or sim_variant.entry_filter
            if applicable_filter is not None:
                prior_bars = bars_before(entry_ms, 30)
                passes, filter_reason = _evaluate_entry_filter(
                    applicable_filter, direction_sign, prior_bars
                )
                if not passes:
                    rows.append({
                        "parent_order_id": p["id"],
                        "variant_label": v.label,
                        "variant_params": {
                            **params_snapshot,
                            "entry_filter_eval": filter_reason,
                        },
                        "session_date": session_date,
                        "entry_spx": entry_spx,
                        "direction": direction,
                        "qty": qty,
                        "realized_r": 0.0,
                        "exit_reason": f"blocked:{filter_reason}",
                        "peak_r": 0.0,
                        "exit_ms": None,
                        "dollars": 0.0,
                    })
                    continue

            end_ms = entry_ms + sim_variant.timeout_minutes * 60 * 1000
            fwd = bars_in_window(entry_ms, end_ms)
            out = simulate_variant(entry_spx, direction_sign, fwd, sim_variant)
            rows.append({
                "parent_order_id": p["id"],
                "variant_label": v.label,  # meta label kept for grouping
                "variant_params": params_snapshot,
                "session_date": session_date,
                "entry_spx": entry_spx,
                "direction": direction,
                "qty": qty,
                "realized_r": round(out["realized_r"], 4),
                "exit_reason": out["exit_reason"],
                "peak_r": round(out["peak_r"], 4),
                "exit_ms": out["exit_ms"],
                "dollars": round(
                    out["realized_r"] * DOLLARS_PER_R_PER_CONTRACT * qty, 2
                ),
            })

    if persist and rows:
        now_iso = datetime.now(timezone.utc).isoformat()
        cur.executemany(
            """
            INSERT OR REPLACE INTO shadow_exit_outcome
                (parent_order_id, variant_label, session_date, computed_at,
                 entry_spx, direction, qty, variant_params,
                 realized_r, exit_reason, peak_r, exit_ms,
                 actual_realized_pnl, actual_realized_r)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            [
                (
                    r["parent_order_id"], r["variant_label"], r["session_date"],
                    now_iso, r["entry_spx"], r["direction"], r["qty"],
                    json.dumps(r["variant_params"]),
                    r["realized_r"], r["exit_reason"], r["peak_r"], r["exit_ms"],
                )
                for r in rows
            ],
        )
        db.commit()
    db.close()

    # Aggregate by variant × session
    by_variant: dict[str, dict] = {}
    for v in variants:
        v_rows = [r for r in rows if r["variant_label"] == v.label]
        by_session: dict[str, dict] = defaultdict(
            lambda: {"n": 0, "qty_total": 0, "sum_r": 0.0, "sum_dollars": 0.0,
                     "wins": 0, "losses": 0, "exit_reasons": defaultdict(int)}
        )
        for r in v_rows:
            b = by_session[r["session_date"]]
            b["n"] += 1
            b["qty_total"] += r["qty"]
            b["sum_r"] += r["realized_r"]
            b["sum_dollars"] += r["dollars"]
            if r["realized_r"] > 0.05:
                b["wins"] += 1
            elif r["realized_r"] < -0.05:
                b["losses"] += 1
            b["exit_reasons"][r["exit_reason"]] += 1
        for s in by_session.values():
            s["exit_reasons"] = dict(s["exit_reasons"])
        by_variant[v.label] = {
            "params": v.to_dict(),
            "by_session": dict(by_session),
            "aggregate": {
                "n": len(v_rows),
                "sum_r": round(sum(r["realized_r"] for r in v_rows), 4),
                "sum_dollars": round(sum(r["dollars"] for r in v_rows), 2),
                "wins": sum(1 for r in v_rows if r["realized_r"] > 0.05),
                "losses": sum(1 for r in v_rows if r["realized_r"] < -0.05),
                "exit_reasons": dict(defaultdict(int, {
                    er: sum(1 for r in v_rows if r["exit_reason"] == er)
                    for er in {r["exit_reason"] for r in v_rows}
                })),
            },
        }

    return {
        "date_from": date_from,
        "date_to": date_to,
        "account_classes": sorted(accept),
        "variants": [v.label for v in variants],
        "n_parents": len({r["parent_order_id"] for r in rows}),
        "n_outcome_rows": len(rows),
        "by_variant": by_variant,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_report(result: dict) -> str:
    lines: list[str] = []
    lines.append(f"## Shadow Exit Replay: {result['date_from']} to {result['date_to']}")
    lines.append("")
    lines.append(f"- Account classes: {', '.join(result['account_classes'])}")
    lines.append(f"- Variants: {', '.join(result['variants'])}")
    lines.append(
        f"- Parent orders replayed: **{result['n_parents']}** "
        f"({result['n_outcome_rows']} outcome rows)"
    )
    lines.append("")
    lines.append("### Aggregate by variant")
    lines.append("")
    lines.append("| Variant | n | Sum R | Sum $ | W | L | WR | avg R |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for label, v in result["by_variant"].items():
        agg = v["aggregate"]
        wl = agg["wins"] + agg["losses"]
        wr = (agg["wins"] / wl * 100) if wl else 0
        avg_r = agg["sum_r"] / agg["n"] if agg["n"] else 0
        lines.append(
            f"| {label} | {agg['n']} | {agg['sum_r']:+.2f} | "
            f"{agg['sum_dollars']:+,.0f} | {agg['wins']} | {agg['losses']} | "
            f"{wr:.1f}% | {avg_r:+.3f} |"
        )

    lines.append("")
    lines.append("### Per-session × variant breakdown")
    lines.append("")
    lines.append("| Variant | Session | n | qty | Sum R | Sum $ | W/L | exit mix |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|")
    for label, v in result["by_variant"].items():
        for date in sorted(v["by_session"].keys()):
            b = v["by_session"][date]
            exit_summary = ", ".join(
                f"{k}:{v}" for k, v in sorted(b["exit_reasons"].items())
            )
            lines.append(
                f"| {label} | {date} | {b['n']} | {b['qty_total']} | "
                f"{b['sum_r']:+.2f} | {b['sum_dollars']:+,.0f} | "
                f"{b['wins']}/{b['losses']} | {exit_summary} |"
            )

    # Pair-wise deltas (each non-first variant vs the first)
    if len(result["by_variant"]) >= 2:
        labels = list(result["by_variant"].keys())
        ref_label = labels[0]
        lines.append("")
        lines.append(f"### Variant deltas vs `{ref_label}`")
        lines.append("")
        lines.append("| Variant | Δ Sum R | Δ $ | Δ avg R |")
        lines.append("|---|---:|---:|---:|")
        ref = result["by_variant"][ref_label]["aggregate"]
        for label in labels[1:]:
            cur = result["by_variant"][label]["aggregate"]
            dR = cur["sum_r"] - ref["sum_r"]
            d_dollars = cur["sum_dollars"] - ref["sum_dollars"]
            avg_ref = ref["sum_r"] / ref["n"] if ref["n"] else 0
            avg_cur = cur["sum_r"] / cur["n"] if cur["n"] else 0
            lines.append(
                f"| {label} | {dR:+.2f} | {d_dollars:+,.0f} | "
                f"{(avg_cur - avg_ref):+.3f} |"
            )

    return "\n".join(lines)


def _filter_variants(arg: str | None) -> list[ExitVariant]:
    if not arg:
        return REGISTERED_VARIANTS
    requested = {x.strip() for x in arg.split(",") if x.strip()}
    selected = [v for v in REGISTERED_VARIANTS if v.label in requested]
    unknown = requested - {v.label for v in REGISTERED_VARIANTS}
    if unknown:
        raise SystemExit(
            f"Unknown variant(s): {sorted(unknown)}. "
            f"Available: {[v.label for v in REGISTERED_VARIANTS]}"
        )
    return selected


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Shadow exit-logic replay over live_order parents."
    )
    ap.add_argument("date_from", help="YYYY-MM-DD")
    ap.add_argument("date_to", help="YYYY-MM-DD")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument(
        "--account-class", default="paper",
        help="Filter by live_order.notes account_class. 'paper' (default), 'live', "
             "'mock', 'unknown', 'all', or comma-separated.",
    )
    ap.add_argument(
        "--variants", default="",
        help="Comma-separated variant labels to test. Default: all registered.",
    )
    ap.add_argument("--no-persist", action="store_true",
                    help="Skip writing shadow_exit_outcome rows (dry run).")
    ap.add_argument("--json", action="store_true",
                    help="Emit raw JSON instead of markdown.")
    args = ap.parse_args()

    accept = _parse_account_class_arg(args.account_class)
    variants = _filter_variants(args.variants)
    result = run_replay(
        args.date_from, args.date_to, args.db, accept,
        variants=variants, persist=not args.no_persist,
    )
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    if args.json:
        print(json.dumps(
            {k: v for k, v in result.items() if k != "rows"},
            indent=2, default=str,
        ))
    else:
        print(format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
