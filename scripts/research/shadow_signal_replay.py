"""Shadow signal-replay for VERDICT decisions (not exit logic).

Sibling to `shadow_exit_replay.py` — that one re-walks `live_order` parents
through alternative EXIT logic. This one re-evaluates `signal_iter` rows
through alternative VERDICT logic (direction filters + strike selection).
Both write to the variant_ledger; both leave the live engine untouched.

Built 2026-05-26 in response to two findings:
  - `audit_package_2/19_direction_confusion_decomposition.md` — 35% of
    actionable signals across 5/19-5/26 are counter-bias (verdict direction
    opposite of drift_bias), realizing -0.65R/fire vs +0.91R/fire for
    aligned. Engine treats `delta_price` (last 1-min SPX tick) as the
    direction selector, overriding cumulative drift_bias. Top
    recommended fix: counter-bias verdicts must have >=2 of
    {alt_30m, alt_4h, microstructure, aggression} agreeing with the
    verdict direction.
  - `audit_package_2/20_strike_too_far_otm_5_26.md` — hardcoded
    `offset_pts=35.0` at engine/strikes.py:24-30 is structurally too far
    on low-vol days. 5/26: 0 of 23 parents had SPX reach strike (0/142
    30-min windows had >=35pt move). Recommended fix: vol-adaptive
    offset (`VIX * 1.5`).

A `VerdictVariant` registers two optional callables:
  - `re_verdict_fn(extras, signal_meta) -> str | None` — returns new
    direction ('LONG_CALLS' / 'LONG_PUTS' / 'WAIT') or None to keep the
    original verdict.
  - `strike_offset_fn(spot, vix, signal_meta) -> float` — returns the
    OTM offset pts a vol-adaptive strike picker would use. Does NOT
    change the verdict direction — purely descriptive.

The replay loads `signal_iter` rows joined with `signal_lookahead_outcome`
(60-min window preferred, else longest available), applies each variant,
and computes outcome metrics (T1 hit rate, expected-R, regime breakdown).

CAVEAT: P&L impact of vol-adaptive strikes cannot be measured without
chain data (historical_option_chain_point only started populating
2026-05-26). The strike-offset variant therefore produces a DESCRIPTIVE
analysis (offset distribution) rather than a P&L counterfactual.

CLI:
    python shadow_signal_replay.py 2026-05-19 2026-05-26
    python shadow_signal_replay.py 2026-05-19 2026-05-26 --variants conjunction_req_2
    python shadow_signal_replay.py 2026-05-19 2026-05-26 --out report.md

Programmatic:
    from shadow_signal_replay import run_replay, REGISTERED_VARIANTS
    result = run_replay("2026-05-19", "2026-05-26")
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_DB = str(Path.home() / ".fef-terminal" / "signal_log.db")

# Legacy binary-scoring constants — kept for the no-bars fallback path only
# (e.g., unit tests that pass synthetic signals without an attached
# historical_bar table). The canonical metrics path now uses bar-walk +
# 50/30/20 partial scale-outs via `_simulate_realized_r` (lazy-imports
# `_simulate_baseline` from `ablation_backtest.py`) — see the methodology
# bug fix shipped 2026-05-26 in response to the conjunction_req_2 graduation
# (+54R binary shadow vs −1.16R ablation bar-walk).
R_T1 = 1.5
R_STOP = -1.0

# 60-min forward window for bar walk (matches `signal_lookahead_outcome` and
# `ablation_backtest.run_ablation`).
FWD_WINDOW_MS = 60 * 60 * 1000


# ---------------------------------------------------------------------------
# Variant definition
# ---------------------------------------------------------------------------

ReVerdictFn = Callable[[dict, dict], str | None]
StrikeOffsetFn = Callable[[float, float | None, dict], float]


@dataclass(frozen=True)
class VerdictVariant:
    """A named verdict-logic configuration.

    Fields:
      name: stable identifier (column key, ledger variant_id).
      description: human-readable summary; written to variant_ledger.
      re_verdict_fn: (extras, signal_meta) -> direction or None.
        Returning None keeps the original verdict; returning a string
        replaces it ('LONG_CALLS', 'LONG_PUTS', 'WAIT'). signal_meta is
        the row dict with direction/drift_bias/confidence/regime fields.
      strike_offset_fn: (spot, current_vix, signal_meta) -> offset_pts.
        None = use the engine's default (35pt). Does NOT change verdict
        direction — purely descriptive.
      family: ledger family ('direction_filter', 'strike_selection',
        'composite', or '' for the baseline validation variant).
      sub_family: optional sub-classification.
      hypothesis: forward-looking — what the variant tests.
    """

    name: str
    description: str
    re_verdict_fn: ReVerdictFn | None = None
    strike_offset_fn: StrikeOffsetFn | None = None
    family: str = ""
    sub_family: str | None = None
    hypothesis: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "family": self.family,
            "sub_family": self.sub_family,
            "hypothesis": self.hypothesis,
            "has_re_verdict": self.re_verdict_fn is not None,
            "has_strike_offset": self.strike_offset_fn is not None,
        }


# ---------------------------------------------------------------------------
# Helpers: signal direction / drift / counter-bias
# ---------------------------------------------------------------------------

def _direction_to_directional(direction: str) -> str | None:
    """LONG_CALLS -> 'BULLISH', LONG_PUTS -> 'BEARISH', else None."""
    if direction == "LONG_CALLS":
        return "BULLISH"
    if direction == "LONG_PUTS":
        return "BEARISH"
    return None


def _is_counter_bias(direction: str, drift_bias: str) -> bool:
    """LONG_PUTS on BULLISH drift or LONG_CALLS on BEARISH drift."""
    return (direction == "LONG_PUTS" and drift_bias == "BULLISH") or (
        direction == "LONG_CALLS" and drift_bias == "BEARISH"
    )


def _parse_alt_dir(extras: dict, key: str) -> str | None:
    """alt_direction_30m / alt_direction_4h are dicts with a 'signal' key
    that is 'BULLISH' / 'BEARISH' / etc. Returns the signal string or None."""
    v = extras.get(key)
    if isinstance(v, dict):
        sig = v.get("signal")
        if sig in ("BULLISH", "BEARISH"):
            return sig
    elif isinstance(v, str) and v in ("BULLISH", "BEARISH"):
        return v
    return None


def _microstructure_dir(extras: dict) -> str | None:
    """microstructure_at_fire.direction in
    {'BULLISH_PRESSURE', 'BEARISH_PRESSURE', 'BALANCED'}.
    Returns 'BULLISH' / 'BEARISH' / None."""
    ms = extras.get("microstructure_at_fire") or {}
    if not isinstance(ms, dict):
        return None
    d = ms.get("direction")
    if d == "BULLISH_PRESSURE":
        return "BULLISH"
    if d == "BEARISH_PRESSURE":
        return "BEARISH"
    return None


def _aggression_dir(signal_meta: dict, threshold: float = 10.0) -> str | None:
    """Aggression imbalance vote.

    If aggression_call_pct > aggression_put_pct + threshold ⇒ BULLISH;
    inverse for BEARISH. threshold defaults to 10pp per the report-19
    Section 3 recommendation."""
    ac = signal_meta.get("aggression_call_pct")
    ap = signal_meta.get("aggression_put_pct")
    if not isinstance(ac, (int, float)) or not isinstance(ap, (int, float)):
        return None
    if ac - ap > threshold:
        return "BULLISH"
    if ap - ac > threshold:
        return "BEARISH"
    return None


def _count_confirming_gates(
    extras: dict,
    signal_meta: dict,
    verdict_direction: str,  # 'BULLISH' or 'BEARISH'
    aggression_threshold: float = 10.0,
) -> int:
    """Count how many of {alt_30m, alt_4h, microstructure, aggression}
    emit the same direction as `verdict_direction`. Used by the
    conjunction-requirement variants from report 19, Section 3c."""
    votes = [
        _parse_alt_dir(extras, "alt_direction_30m"),
        _parse_alt_dir(extras, "alt_direction_4h"),
        _microstructure_dir(extras),
        _aggression_dir(signal_meta, threshold=aggression_threshold),
    ]
    return sum(1 for v in votes if v == verdict_direction)


# ---------------------------------------------------------------------------
# Variant re_verdict callables
# ---------------------------------------------------------------------------

def _conjunction_req_2_re_verdict(extras: dict, signal_meta: dict) -> str | None:
    """Top recommended fix from report 19, Section 6 (Option E).

    For each fire:
      - Aligned verdicts are kept (return None).
      - Counter-bias verdicts must have >=2 of
        {alt_30m, alt_4h, microstructure, aggression} agreeing
        with the verdict direction; else demoted to WAIT.
    """
    direction = signal_meta.get("direction") or ""
    drift = signal_meta.get("drift_bias") or ""
    if not _is_counter_bias(direction, drift):
        return None
    verdict_dir = _direction_to_directional(direction)
    if verdict_dir is None:
        return None
    n_gates = _count_confirming_gates(extras, signal_meta, verdict_dir)
    if n_gates < 2:
        return "WAIT"
    return None


def _conjunction_req_1_re_verdict(extras: dict, signal_meta: dict) -> str | None:
    """Looser variant: counter-bias requires >=1 confirming gate."""
    direction = signal_meta.get("direction") or ""
    drift = signal_meta.get("drift_bias") or ""
    if not _is_counter_bias(direction, drift):
        return None
    verdict_dir = _direction_to_directional(direction)
    if verdict_dir is None:
        return None
    n_gates = _count_confirming_gates(extras, signal_meta, verdict_dir)
    if n_gates < 1:
        return "WAIT"
    return None


def _drop_low_counter_bias_re_verdict(
    extras: dict, signal_meta: dict
) -> str | None:
    """Simpler fallback: LOW confidence + counter-bias ⇒ WAIT.

    Empirically all counter-bias fires in the 5/19-5/26 window were
    LOW confidence (per report 19, Section 5 confidence table), so this
    has identical coverage to "drop all counter-bias" on this dataset
    but is policy-safer: leaves the door open for MEDIUM/HIGH counter-
    bias verdicts to fire when they actually appear in production."""
    direction = signal_meta.get("direction") or ""
    drift = signal_meta.get("drift_bias") or ""
    confidence = signal_meta.get("confidence") or ""
    if _is_counter_bias(direction, drift) and confidence == "LOW":
        return "WAIT"
    return None


# ---------------------------------------------------------------------------
# Strike offset callable
# ---------------------------------------------------------------------------

def _vol_adaptive_offset(
    spot: float, current_vix: float | None, signal_meta: dict
) -> float:
    """Vol-adaptive strike offset from report 20, Section 6.

    Returns offset_pts = clamp(VIX * 1.5, 5, 40). When VIX is unavailable
    or zero, falls back to 20.0 (median of the live distribution).
    spot and signal_meta are accepted for interface compatibility — the
    rule itself only uses VIX in this simple form."""
    if current_vix is None or current_vix <= 0:
        return 20.0
    return max(5.0, min(40.0, current_vix * 1.5))


# ---------------------------------------------------------------------------
# Registered variants
# ---------------------------------------------------------------------------

REGISTERED_VARIANTS: list[VerdictVariant] = [
    VerdictVariant(
        name="baseline",
        description=(
            "Baseline validation variant — keeps every original verdict, "
            "no strike-offset change. Used to verify the replay framework "
            "matches historical outcomes. Does NOT count toward family "
            "N_trials."
        ),
        family="",
        sub_family=None,
        hypothesis=(
            "Baseline outputs should equal the historical outcomes. Any "
            "delta vs reality indicates a framework bug, not a strategy "
            "improvement."
        ),
    ),
    VerdictVariant(
        name="conjunction_req_2",
        description=(
            "Counter-bias verdicts (direction opposite of drift_bias) "
            "demoted to WAIT unless >=2 of "
            "{alt_30m, alt_4h, microstructure, aggression} agree with "
            "the verdict direction. Top recommended fix from report 19 "
            "Section 6 (Option E)."
        ),
        re_verdict_fn=_conjunction_req_2_re_verdict,
        family="direction_filter",
        sub_family="conjunction_req",
        hypothesis=(
            "Per report 19 Section 3c independent-gates table, 56/79 "
            "counter-bias fires have 0 confirming gates and realize "
            "-0.84R; 4/79 have 2+ confirming gates and realize +0.50R. "
            "A 2-gate filter drops 75 losers while keeping 4 winners. "
            "Estimated +50R aggregate across 5-session window."
        ),
    ),
    VerdictVariant(
        name="conjunction_req_1",
        description=(
            "Looser version: counter-bias requires >=1 of "
            "{alt_30m, alt_4h, microstructure, aggression} agreeing. "
            "Drops fewer fires than conjunction_req_2 but keeps the "
            "marginal n=19 counter-bias-with-1-gate cohort (T1 hit-rate "
            "26.3%, -0.34R per fire — modest negative edge)."
        ),
        re_verdict_fn=_conjunction_req_1_re_verdict,
        family="direction_filter",
        sub_family="conjunction_req",
        hypothesis=(
            "Tests whether the 1-gate cohort is salvageable. If its "
            "realized R approaches break-even on broader data, the looser "
            "filter is preferable. Otherwise, conjunction_req_2 wins."
        ),
    ),
    VerdictVariant(
        name="drop_low_counter_bias",
        description=(
            "LOW confidence + counter-bias ⇒ WAIT. Simpler fallback per "
            "report 19, Section 5: 100% of counter-bias fires in the "
            "5/19-5/26 window are LOW confidence, so this drops all 79 "
            "counter-bias fires while leaving MEDIUM/HIGH untouched."
        ),
        re_verdict_fn=_drop_low_counter_bias_re_verdict,
        family="direction_filter",
        sub_family="confidence_gate",
        hypothesis=(
            "If counter-bias and LOW are highly correlated in production "
            "(true on 5/19-5/26), this gives the same result as the "
            "conjunction filter but is simpler and policy-safer for "
            "future MEDIUM/HIGH counter-bias signals."
        ),
    ),
    VerdictVariant(
        name="strike_vol_adaptive",
        description=(
            "Vol-adaptive strike offset: offset_pts = clamp(VIX * 1.5, 5, 40). "
            "Replaces the hardcoded 35pt OTM with a VIX-scaled offset. "
            "Descriptive only — full P&L impact requires chain capture data "
            "which only began populating 2026-05-26."
        ),
        strike_offset_fn=_vol_adaptive_offset,
        family="strike_selection",
        sub_family="vol_adaptive",
        hypothesis=(
            "Report 20: 5/26 max 30-min SPX range was 19.51pt vs 35pt "
            "required to bring the strike ATM. VIX*1.5 = 14*1.5 = 21pt "
            "would have produced reachable strikes. Tests whether the "
            "vol-adaptive rule produces meaningfully different offsets "
            "across the 5-session window."
        ),
    ),
    VerdictVariant(
        name="conjunction_req_2_AND_vol_adaptive",
        description=(
            "Combined variant: applies the conjunction_req_2 direction "
            "filter AND the vol-adaptive strike offset. Lets us measure "
            "interaction effects between the two fixes."
        ),
        re_verdict_fn=_conjunction_req_2_re_verdict,
        strike_offset_fn=_vol_adaptive_offset,
        family="composite",
        sub_family=None,
        hypothesis=(
            "If both fixes are net-positive independently, the "
            "combination should be additive on signal count drops "
            "(direction filter) and orthogonally improve strike "
            "selection. Tests the no-interaction null."
        ),
    ),
]


def _variant_by_name(name: str) -> VerdictVariant | None:
    return next((v for v in REGISTERED_VARIANTS if v.name == name), None)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _ts_to_ms(ts_str: str) -> int:
    dt = datetime.fromisoformat(ts_str.replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def load_signals(
    date_from: str, date_to: str, db_path: str = DEFAULT_DB,
) -> list[dict]:
    """Load signal_iter rows joined with the LONGEST AVAILABLE
    signal_lookahead_outcome window (prefers 60min, falls back to 30/15/5).

    Returns a list of dicts with keys:
      id, session_date, timestamp, spx_price, direction, drift_bias,
      confidence, aggression_call_pct, aggression_put_pct,
      iv_rank_call_pct, iv_rank_put_pct, engine_version, extras (dict),
      regime, lookahead_minutes, would_have_hit_t1, would_have_hit_stop,
      would_have_hit_t2, would_have_hit_t3, spx_mfe_pts, spx_mae_pts,
      t1_hit_probability.

    Includes WAIT rows — variants that promote WAIT into actionable
    would need them — but the current variant set only ever demotes,
    so WAIT rows are kept for completeness and dropped from the
    actionable-cohort metrics.
    """
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()
    sigs = cur.execute(
        """
        SELECT s.id, s.session_date, s.timestamp, s.spx_price,
               s.direction, s.drift_bias, s.confidence,
               s.aggression_call_pct, s.aggression_put_pct,
               s.iv_rank_call_pct, s.iv_rank_put_pct,
               s.engine_version, s.extras
        FROM signal_iter s
        WHERE s.session_date BETWEEN ? AND ?
          AND s.direction IN ('LONG_CALLS', 'LONG_PUTS', 'WAIT')
        ORDER BY s.id
        """,
        (date_from, date_to),
    ).fetchall()

    # For each signal id, find the longest-available lookahead window.
    # The query below returns one row per (signal_id, lookahead_minutes);
    # we group by signal_id and pick max(lookahead_minutes).
    lo_rows = cur.execute(
        """
        SELECT signal_iter_id, lookahead_minutes,
               would_have_hit_t1, would_have_hit_t2, would_have_hit_t3,
               would_have_hit_stop, t1_hit_probability,
               spx_mfe_pts, spx_mae_pts, spx_at_signal,
               spx_close_at_window_end
        FROM signal_lookahead_outcome
        WHERE session_date BETWEEN ? AND ?
        ORDER BY signal_iter_id, lookahead_minutes DESC
        """,
        (date_from, date_to),
    ).fetchall()
    lo_by_id: dict[int, dict] = {}
    for r in lo_rows:
        sid = r["signal_iter_id"]
        # first row per signal_id is the longest window (ORDER BY DESC)
        if sid not in lo_by_id:
            lo_by_id[sid] = dict(r)
    db.close()

    out: list[dict] = []
    for s in sigs:
        try:
            extras = json.loads(s["extras"]) if s["extras"] else {}
        except (json.JSONDecodeError, TypeError):
            extras = {}
        regime = (
            extras.get("regime_at_fire")
            or (extras.get("regime_demoted") or {}).get("regime")
            or "UNKNOWN"
        )
        rec: dict[str, Any] = {
            "id": s["id"],
            "session_date": s["session_date"],
            "timestamp": s["timestamp"],
            "spx_price": s["spx_price"],
            "direction": s["direction"],
            "drift_bias": s["drift_bias"],
            "confidence": s["confidence"],
            "aggression_call_pct": s["aggression_call_pct"],
            "aggression_put_pct": s["aggression_put_pct"],
            "iv_rank_call_pct": s["iv_rank_call_pct"],
            "iv_rank_put_pct": s["iv_rank_put_pct"],
            "engine_version": s["engine_version"],
            "extras": extras,
            "regime": regime,
            "vix_at_fire": extras.get("vix_at_fire"),
        }
        lo = lo_by_id.get(s["id"])
        if lo:
            rec["lookahead_minutes"] = lo["lookahead_minutes"]
            rec["would_have_hit_t1"] = lo["would_have_hit_t1"]
            rec["would_have_hit_t2"] = lo["would_have_hit_t2"]
            rec["would_have_hit_t3"] = lo["would_have_hit_t3"]
            rec["would_have_hit_stop"] = lo["would_have_hit_stop"]
            rec["t1_hit_probability"] = lo["t1_hit_probability"]
            rec["spx_mfe_pts"] = lo["spx_mfe_pts"]
            rec["spx_mae_pts"] = lo["spx_mae_pts"]
            rec["spx_at_signal"] = lo["spx_at_signal"]
            rec["spx_close_at_window_end"] = lo["spx_close_at_window_end"]
        else:
            rec["lookahead_minutes"] = None
            rec["would_have_hit_t1"] = None
            rec["would_have_hit_t2"] = None
            rec["would_have_hit_t3"] = None
            rec["would_have_hit_stop"] = None
            rec["t1_hit_probability"] = None
            rec["spx_mfe_pts"] = None
            rec["spx_mae_pts"] = None
            rec["spx_at_signal"] = None
            rec["spx_close_at_window_end"] = None
        out.append(rec)
    return out


def _load_vix_table(db_path: str) -> list[tuple[int, float]]:
    """Pull (timestamp_ms, value) from vix_sample sorted by timestamp.
    Used by the strike-offset variants to resolve VIX at signal time."""
    db = sqlite3.connect(db_path)
    cur = db.cursor()
    rows = cur.execute(
        "SELECT timestamp_ms, value FROM vix_sample ORDER BY timestamp_ms"
    ).fetchall()
    db.close()
    return [(int(r[0]), float(r[1])) for r in rows]


# ---------------------------------------------------------------------------
# Bar-walk data loading and realized-R simulation
# (methodology fix shipped 2026-05-26 — replaces binary T1/stop scoring per
# the conjunction_req_2 graduation gap: shadow said +54R, ablation said
# -1.16R. Per-signal bar walk + 50/30/20 partial scale-out is now the
# canonical methodology, matching ablation_backtest._simulate_baseline,
# daily_review, per_regime_sweep v2, and realistic_fill_estimator v2.)
# ---------------------------------------------------------------------------


def _load_spx_bars(
    db_path: str, date_from: str, date_to: str,
) -> tuple[dict[int, dict], list[int]]:
    """Load SPX 1-min bars for the date window plus a 60-min buffer either
    side (to cover signals near session boundaries). Returns
    (bar_by_ms, bar_ms_sorted) so callers can do bisect lookups for any
    signal timestamp.

    Schema and selection are intentionally identical to
    `ablation_backtest.run_ablation`'s bar load so bar-walk results match
    on the same input row."""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()
    try:
        rows = cur.execute(
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
    except sqlite3.OperationalError:
        # Test DBs without historical_bar — fall through to no-bars fallback.
        rows = []
    db.close()
    bar_by_ms: dict[int, dict] = {int(r["timestamp_ms"]): dict(r) for r in rows}
    bar_ms_sorted: list[int] = sorted(bar_by_ms.keys())
    return bar_by_ms, bar_ms_sorted


def _bars_in_window(
    bar_by_ms: dict[int, dict], bar_ms_sorted: list[int],
    start_ms: int, end_ms: int,
) -> list[dict]:
    """Return all bars with timestamp_ms in [start_ms, end_ms]. Matches
    `ablation_backtest.run_ablation:bars_in_window`."""
    if not bar_ms_sorted:
        return []
    lo = bisect.bisect_left(bar_ms_sorted, start_ms)
    hi = bisect.bisect_right(bar_ms_sorted, end_ms)
    return [bar_by_ms[m] for m in bar_ms_sorted[lo:hi]]


def _direction_sign(direction: str) -> int | None:
    """Map LONG_CALLS -> +1, LONG_PUTS -> -1, anything else -> None."""
    if direction == "LONG_CALLS":
        return +1
    if direction == "LONG_PUTS":
        return -1
    return None


def _risk_pts_for_signal(signal_meta: dict, default_pts: float = 10.0) -> float:
    """Per-signal R-unit = |entry - stop| in SPX points. Falls back to
    `default_pts` (10) when extras.stop is missing — matches the convention
    used by `ablation_backtest.compute_r_unit` / `_r_unit.py`."""
    entry = signal_meta.get("spx_price") or 0.0
    extras = signal_meta.get("extras") or {}
    stop_spx = extras.get("stop") if isinstance(extras, dict) else None
    if not entry or not stop_spx:
        return default_pts
    diff = abs(float(entry) - float(stop_spx))
    return diff if diff > 0 else default_pts


# Lazily-imported handle to the canonical bar-walk simulator. Lives in
# `ablation_backtest.py:_simulate_baseline`. We use a module-level cache so
# the import cost is paid once. Lazy because `ablation_backtest` imports
# `_conjunction_req_2_re_verdict` from THIS module, so an eager
# `from ablation_backtest import ...` at module load would create a
# circular-import failure during test collection.
_BAR_WALK_BASELINE: Callable | None = None


def _get_bar_walk_baseline():
    """Lazy-import `_simulate_baseline` from `ablation_backtest`. Returns
    None if the import fails (then the no-bars fallback handles things)."""
    global _BAR_WALK_BASELINE
    if _BAR_WALK_BASELINE is not None:
        return _BAR_WALK_BASELINE
    # Ensure ablation_backtest is importable. Both files live in this same
    # `scripts/research/` directory, so adding the parent to sys.path is
    # how the package gets resolved when shadow_signal_replay is imported
    # as a top-level module from tests/CLI.
    research_dir = str(Path(__file__).resolve().parent)
    if research_dir not in sys.path:
        sys.path.insert(0, research_dir)
    try:
        from ablation_backtest import _simulate_baseline  # noqa: WPS433
        _BAR_WALK_BASELINE = _simulate_baseline
    except ImportError:
        _BAR_WALK_BASELINE = None
    return _BAR_WALK_BASELINE


def _simulate_realized_r(
    signal_meta: dict, fwd_bars: list[dict] | None,
) -> float:
    """Bar-walk realized R for a single signal, using the variant's
    (post-replacement) `new_direction`. Returns NaN when bar data is
    unavailable (callers handle NaN via the same filter as
    `_expected_r`) so the function degrades gracefully on synthetic
    test inputs that don't seed `historical_bar`.

    Direction handling:
      - WAIT / unknown -> NaN (signal is not actionable)
      - LONG_CALLS -> +1, LONG_PUTS -> -1

    R-unit:
      - per-signal `|entry - stop|` from extras.stop, falling back to 10pt
        when the row pre-dates option-pricing extras.

    Methodology is intentionally identical to
    `ablation_backtest._simulate_baseline`:
      - Order-preserving 1-min bar walk
      - 50/30/20 partial scale-out at T1/T2/T3 (+1R/2R/3R)
      - Stop priority on ambiguous bars at -1R
      - Timeout at the 60-min window's last close
    """
    direction = signal_meta.get("new_direction") or signal_meta.get("direction")
    sign = _direction_sign(direction)
    if sign is None:
        return float("nan")
    if not fwd_bars:
        return float("nan")
    entry = signal_meta.get("spx_price") or 0.0
    if not entry:
        return float("nan")
    risk_pts = _risk_pts_for_signal(signal_meta)
    fn = _get_bar_walk_baseline()
    if fn is None:
        return float("nan")
    realized_r, _reason = fn(float(entry), sign, fwd_bars, risk_pts=risk_pts)
    return float(realized_r)


def _attach_forward_bars(
    signals: list[dict],
    bar_by_ms: dict[int, dict],
    bar_ms_sorted: list[int],
) -> None:
    """Mutate every signal dict in `signals` to add a `fwd_bars` key — the
    list of 1-min bars in [signal_ts, signal_ts + FWD_WINDOW_MS]. When the
    bar table is empty the field is set to None (no-bars fallback path).

    Done once per replay (before apply_variant fan-out) so the variant
    iteration doesn't repeat the bisect lookup K times for K variants."""
    for s in signals:
        ts = s.get("timestamp")
        if not ts:
            s["fwd_bars"] = None
            continue
        try:
            sig_ms = _ts_to_ms(ts)
        except (ValueError, TypeError):
            s["fwd_bars"] = None
            continue
        end_ms = sig_ms + FWD_WINDOW_MS
        bars = _bars_in_window(bar_by_ms, bar_ms_sorted, sig_ms, end_ms)
        s["fwd_bars"] = bars if bars else None


def _resolve_vix_at_signal(
    signal_meta: dict, vix_table: list[tuple[int, float]],
    max_offset_ms: int = 30 * 60 * 1000,  # 30 min — VIX moves slowly
) -> float | None:
    """Resolve VIX at signal time.

    Tries in order:
      1. extras.vix_at_fire — most reliable when present
      2. nearest vix_sample within max_offset_ms of the signal timestamp
      3. extras.iv_rank.last_iv — only as a heuristic fallback
      4. None — caller's strike_offset_fn must accept None
    """
    extras = signal_meta.get("extras") or {}
    v = extras.get("vix_at_fire")
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    ts = signal_meta.get("timestamp")
    if ts and vix_table:
        sig_ms = _ts_to_ms(ts)
        # binary search for nearest
        ms_list = [r[0] for r in vix_table]
        idx = bisect.bisect_left(ms_list, sig_ms)
        candidates = []
        if idx < len(vix_table):
            candidates.append(vix_table[idx])
        if idx > 0:
            candidates.append(vix_table[idx - 1])
        best = None
        best_diff = max_offset_ms + 1
        for tm, val in candidates:
            d = abs(tm - sig_ms)
            if d < best_diff:
                best_diff = d
                best = val
        if best is not None and best_diff <= max_offset_ms:
            return best
    iv = extras.get("iv_rank")
    if isinstance(iv, dict):
        last_iv = iv.get("last_iv")
        if isinstance(last_iv, (int, float)) and last_iv > 0:
            return float(last_iv)
    return None


# ---------------------------------------------------------------------------
# Variant application
# ---------------------------------------------------------------------------

def apply_variant(
    signals: list[dict], variant: VerdictVariant,
    vix_table: list[tuple[int, float]] | None = None,
) -> list[dict]:
    """Apply re_verdict_fn + strike_offset_fn to every signal.

    For each signal, computes:
      original_direction, new_direction, changed (bool),
      new_offset_pts (None if variant has no strike_offset_fn),
      original_offset_pts (35.0 — engine default).

    Returns a NEW list of dicts (does NOT mutate inputs).
    """
    if vix_table is None:
        vix_table = []
    out: list[dict] = []
    for s in signals:
        original_dir = s["direction"]
        # Apply re_verdict_fn
        if variant.re_verdict_fn is not None:
            try:
                new_dir = variant.re_verdict_fn(s["extras"], s)
            except Exception as exc:  # broad: bad extras shouldn't kill replay
                new_dir = None
                # Optional: log; for now silently keep original
                _ = exc
            if new_dir is None:
                new_dir = original_dir
        else:
            new_dir = original_dir
        changed = new_dir != original_dir

        # Apply strike_offset_fn (descriptive)
        new_offset: float | None = None
        if variant.strike_offset_fn is not None:
            spot = s.get("spx_price") or 0.0
            vix = _resolve_vix_at_signal(s, vix_table)
            try:
                new_offset = variant.strike_offset_fn(spot, vix, s)
            except Exception:
                new_offset = None

        out_rec = dict(s)
        out_rec["original_direction"] = original_dir
        out_rec["new_direction"] = new_dir
        out_rec["changed"] = changed
        out_rec["new_offset_pts"] = new_offset
        out_rec["original_offset_pts"] = 35.0  # current hardcoded engine value
        out.append(out_rec)
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _expected_r(rec: dict) -> float:
    """LEGACY binary R-multiple proxy — fallback ONLY when bar data is
    unavailable (synthetic test DBs without `historical_bar`).

    METHODOLOGY BUG (fixed 2026-05-27): this binary T1-or-stop scoring was
    the live metrics path through 2026-05-26 and overstated counter-bias
    direction filters ~15× (conjunction_req_2 showed +54R here vs −1.16R
    under ablation_backtest's bar walk). The binary rule counts any 60-min
    T1 touch as a full +1.5R win, but the partial-scale-out bar walk reveals
    many of those "T1 hits" are fast-reversal stop-outs (intra-bar dip to
    −1R, then rip through T1) that realize a net loss. The canonical R path
    is now `_score_r` → `_simulate_realized_r` (bar walk + 50/30/20 partials),
    matching ablation_backtest._simulate_baseline / daily_review /
    per_regime_sweep v2. This function survives only for the no-bars path.

    Uses the binary would_have_hit_t1 / would_have_hit_stop flags
    (per report 19 caveat that `t1_hit_probability` is effectively binary
    for 99% of rows and is 0.0 for many older rows where it was never
    computed):
      T1 hit ⇒ +1.5R; stop hit ⇒ -1R; neither ⇒ 0R.
    Returns NaN when both T1 and stop flags are missing.

    The `t1_hit_probability` column is preferred ONLY when it is
    strictly between 0 and 1 (excluding the default-0 sentinel) — i.e.
    when it has been populated by signal_quality_scorer.py. That gives
    a forward-compatible path to a real probability-weighted expectation
    when newer rows arrive.
    """
    t1 = rec.get("would_have_hit_t1")
    stop = rec.get("would_have_hit_stop")
    if t1 is None and stop is None:
        return float("nan")
    prob = rec.get("t1_hit_probability")
    # Only use prob if it's strictly inside (0, 1) — sentinel-zero rows
    # use the binary fallback instead.
    if isinstance(prob, (int, float)) and 0.0 < float(prob) < 1.0:
        return float(prob) * R_T1 + (1.0 - float(prob)) * R_STOP
    if t1:
        return R_T1
    if stop:
        return R_STOP
    return 0.0


def _score_r(rec: dict) -> float:
    """Canonical per-signal realized R for the metrics layer.

    Prefers the bar-walk + 50/30/20 partial-scale-out simulation
    (`_simulate_realized_r`, which delegates to
    ablation_backtest._simulate_baseline) when forward 1-min bars are
    attached to the record (`fwd_bars`). Falls back to the LEGACY binary
    T1/stop scoring (`_expected_r`) only when bars are unavailable — i.e.
    synthetic test DBs without a `historical_bar` table, or signals whose
    timestamp lies outside the loaded bar window.

    Returns NaN when neither path can score the signal (caller filters
    NaN out of the R aggregation, identical to the prior `_expected_r`
    NaN handling).

    This is the methodology fix shipped 2026-05-27: the prior live path
    used `_expected_r` directly, which overstated counter-bias direction
    filters ~15× vs the bar walk (see `_expected_r` docstring + the
    `_simulate_realized_r` block above).
    """
    fwd_bars = rec.get("fwd_bars")
    if fwd_bars:
        r = _simulate_realized_r(rec, fwd_bars)
        if r == r:  # not NaN — bar walk succeeded
            return r
    # No bars (synthetic test DB) or bar walk returned NaN → legacy binary.
    return _expected_r(rec)


def compute_outcome_metrics(applied_signals: list[dict]) -> dict:
    """Per-variant cohort outcome metrics.

    Returns a dict with:
      n_total: rows in input
      n_actionable: rows where new_direction != WAIT
      n_dropped: rows where original was actionable and new is WAIT
      n_changed: rows where new != original (drops only in current variant set)
      n_counter_bias_in_actionable: actionable rows that are counter-bias
        relative to drift_bias
      counter_bias_pct: % of actionable that are counter-bias
      mean_expected_r: average expected R-multiple across actionable rows
        that have lookahead coverage
      sum_expected_r: aggregate sum of expected R
      t1_hit_rate / stop_hit_rate: % of actionable with lookahead coverage
      by_regime / by_confidence / by_hour: stratified breakdowns
      offset_stats: when strike_offset_fn present, distribution stats
    """
    n_total = len(applied_signals)
    actionable = [s for s in applied_signals if s["new_direction"] != "WAIT"]
    n_actionable = len(actionable)

    # Counter-bias on the NEW direction
    cb_actionable = [
        s for s in actionable
        if _is_counter_bias(s["new_direction"], s["drift_bias"] or "")
    ]
    n_cb_actionable = len(cb_actionable)

    # Dropped = original direction was actionable, new is WAIT.
    n_dropped = sum(
        1 for s in applied_signals
        if s["original_direction"] in ("LONG_CALLS", "LONG_PUTS")
        and s["new_direction"] == "WAIT"
    )
    n_changed = sum(1 for s in applied_signals if s["changed"])

    # Aligned vs counter-bias R aggregation. R is the canonical bar-walk
    # realized R (50/30/20 partials) via _score_r; the n_t1/n_stop counters
    # remain the descriptive lookahead-flag hit rates (independent of the R
    # methodology — kept for the T1%/stop% report columns).
    aligned_r: list[float] = []
    cb_r: list[float] = []
    overall_r: list[float] = []
    n_t1 = 0
    n_stop = 0
    n_eval = 0
    for s in actionable:
        er = _score_r(s)
        if er != er:  # NaN
            continue
        overall_r.append(er)
        n_eval += 1
        if s.get("would_have_hit_t1"):
            n_t1 += 1
        if s.get("would_have_hit_stop"):
            n_stop += 1
        if _is_counter_bias(s["new_direction"], s["drift_bias"] or ""):
            cb_r.append(er)
        else:
            aligned_r.append(er)

    def _mean(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    # Stratify
    by_regime: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "sum_r": 0.0, "n_eval": 0, "n_t1": 0, "n_stop": 0}
    )
    by_confidence: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "sum_r": 0.0, "n_eval": 0, "n_t1": 0, "n_stop": 0}
    )
    by_hour: dict[int, dict] = defaultdict(
        lambda: {"n": 0, "sum_r": 0.0, "n_eval": 0, "n_t1": 0, "n_stop": 0}
    )
    by_session: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "n_cb": 0, "sum_r": 0.0, "n_eval": 0,
                 "n_t1": 0, "n_stop": 0}
    )
    for s in actionable:
        regime = s.get("regime") or "UNKNOWN"
        conf = s.get("confidence") or "UNKNOWN"
        session = s.get("session_date") or "UNKNOWN"
        try:
            ct = (
                datetime.fromisoformat(s["timestamp"].replace(" ", "T"))
                + timedelta(hours=-5)
            )
            hour = ct.hour
        except (KeyError, ValueError, TypeError):
            hour = -1
        er = _score_r(s)
        is_finite = er == er
        is_cb = _is_counter_bias(s["new_direction"], s["drift_bias"] or "")
        for d in (by_regime[regime], by_confidence[conf], by_hour[hour]):
            d["n"] += 1
            if is_finite:
                d["sum_r"] += er
                d["n_eval"] += 1
                if s.get("would_have_hit_t1"):
                    d["n_t1"] += 1
                if s.get("would_have_hit_stop"):
                    d["n_stop"] += 1
        # by_session also tracks counter-bias counts
        sd = by_session[session]
        sd["n"] += 1
        if is_cb:
            sd["n_cb"] += 1
        if is_finite:
            sd["sum_r"] += er
            sd["n_eval"] += 1
            if s.get("would_have_hit_t1"):
                sd["n_t1"] += 1
            if s.get("would_have_hit_stop"):
                sd["n_stop"] += 1

    # Strike offset distribution (only when variant defines it).
    new_offsets = [s["new_offset_pts"] for s in applied_signals
                   if s.get("new_offset_pts") is not None]
    offset_stats: dict | None = None
    if new_offsets:
        sorted_off = sorted(new_offsets)
        n = len(sorted_off)
        offset_stats = {
            "n": n,
            "min": round(min(sorted_off), 2),
            "max": round(max(sorted_off), 2),
            "mean": round(sum(sorted_off) / n, 2),
            "median": round(sorted_off[n // 2], 2),
            "p25": round(sorted_off[n // 4], 2),
            "p75": round(sorted_off[3 * n // 4], 2),
            "baseline_default_pts": 35.0,
            "n_below_baseline": sum(1 for o in sorted_off if o < 35.0),
            "n_at_floor": sum(1 for o in sorted_off if o <= 5.01),
            "n_at_ceiling": sum(1 for o in sorted_off if o >= 39.99),
        }

    return {
        "n_total": n_total,
        "n_actionable": n_actionable,
        "n_dropped": n_dropped,
        "n_changed": n_changed,
        "n_counter_bias_in_actionable": n_cb_actionable,
        "counter_bias_pct": (
            round(100.0 * n_cb_actionable / n_actionable, 2)
            if n_actionable else 0.0
        ),
        "n_with_lookahead": n_eval,
        "mean_expected_r": round(_mean(overall_r), 4) if overall_r else None,
        "sum_expected_r": round(sum(overall_r), 4) if overall_r else 0.0,
        "mean_aligned_r": round(_mean(aligned_r), 4) if aligned_r else None,
        "mean_counter_bias_r": round(_mean(cb_r), 4) if cb_r else None,
        "t1_hit_rate": (
            round(100.0 * n_t1 / n_eval, 2) if n_eval else None
        ),
        "stop_hit_rate": (
            round(100.0 * n_stop / n_eval, 2) if n_eval else None
        ),
        "by_regime": {
            k: {
                **v,
                "mean_r": (
                    round(v["sum_r"] / v["n_eval"], 4) if v["n_eval"] else None
                ),
                "t1_hit_rate": (
                    round(100.0 * v["n_t1"] / v["n_eval"], 2)
                    if v["n_eval"] else None
                ),
            }
            for k, v in by_regime.items()
        },
        "by_confidence": {
            k: {
                **v,
                "mean_r": (
                    round(v["sum_r"] / v["n_eval"], 4) if v["n_eval"] else None
                ),
            }
            for k, v in by_confidence.items()
        },
        "by_hour": {
            k: {
                **v,
                "mean_r": (
                    round(v["sum_r"] / v["n_eval"], 4) if v["n_eval"] else None
                ),
            }
            for k, v in by_hour.items()
        },
        "by_session": {
            k: {
                **v,
                "cb_pct": (
                    round(100.0 * v["n_cb"] / v["n"], 2) if v["n"] else 0.0
                ),
                "mean_r": (
                    round(v["sum_r"] / v["n_eval"], 4) if v["n_eval"] else None
                ),
                "t1_hit_rate": (
                    round(100.0 * v["n_t1"] / v["n_eval"], 2)
                    if v["n_eval"] else None
                ),
            }
            for k, v in by_session.items()
        },
        "offset_stats": offset_stats,
    }


def compare_to_baseline(
    variant_metrics: dict, baseline_metrics: dict
) -> dict:
    """Delta tables: variant - baseline on the key magnitudes."""

    def _diff(a, b):
        if a is None or b is None:
            return None
        return round(a - b, 4)

    return {
        "delta_n_actionable": (
            variant_metrics["n_actionable"] - baseline_metrics["n_actionable"]
        ),
        "delta_n_counter_bias": (
            variant_metrics["n_counter_bias_in_actionable"]
            - baseline_metrics["n_counter_bias_in_actionable"]
        ),
        "delta_counter_bias_pct": round(
            variant_metrics["counter_bias_pct"]
            - baseline_metrics["counter_bias_pct"],
            2,
        ),
        "delta_sum_expected_r": _diff(
            variant_metrics["sum_expected_r"],
            baseline_metrics["sum_expected_r"],
        ),
        "delta_mean_expected_r": _diff(
            variant_metrics["mean_expected_r"],
            baseline_metrics["mean_expected_r"],
        ),
        "delta_t1_hit_rate": _diff(
            variant_metrics["t1_hit_rate"], baseline_metrics["t1_hit_rate"]
        ),
        "delta_stop_hit_rate": _diff(
            variant_metrics["stop_hit_rate"], baseline_metrics["stop_hit_rate"]
        ),
    }


# ---------------------------------------------------------------------------
# Variant ledger upsert
# ---------------------------------------------------------------------------

def upsert_variant_ledger(
    db_path: str, variants: list[VerdictVariant], sample_window: str,
) -> int:
    """Idempotent upsert. Skips the empty-family baseline (validation-only)."""
    db = sqlite3.connect(db_path)
    cur = db.cursor()
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    try:
        for v in variants:
            if not v.family:
                continue
            params = json.dumps(v.to_dict())
            cur.execute(
                """
                INSERT INTO variant_ledger
                    (variant_id, family, sub_family, description, hypothesis,
                     registered_at, last_seen_at, tested_in, sample_window,
                     outcome_summary, still_active, variant_params)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(variant_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    sample_window = excluded.sample_window,
                    variant_params = excluded.variant_params,
                    family = excluded.family,
                    sub_family = excluded.sub_family,
                    description = excluded.description,
                    hypothesis = excluded.hypothesis,
                    tested_in = excluded.tested_in
                """,
                (
                    v.name, v.family, v.sub_family,
                    v.description, v.hypothesis,
                    now, now, "shadow_signal_replay", sample_window,
                    "pending", params,
                ),
            )
            n += 1
        db.commit()
    except sqlite3.OperationalError as e:
        print(f"[warn] variant_ledger not available: {e}", file=sys.stderr)
    finally:
        db.close()
    return n


# ---------------------------------------------------------------------------
# Replay pipeline
# ---------------------------------------------------------------------------

def run_replay(
    date_from: str,
    date_to: str,
    db_path: str = DEFAULT_DB,
    variants: list[VerdictVariant] | None = None,
    *,
    persist_ledger: bool = True,
) -> dict:
    """Full pipeline: load signals, apply each variant, compute metrics,
    compare to baseline.

    Returns:
        {
          "date_from", "date_to", "n_signals", "n_actionable_original",
          "variants": [name, ...],
          "metrics": {variant_name: {...metrics}},
          "deltas_vs_baseline": {variant_name: {...deltas}},
        }
    """
    variants = variants or REGISTERED_VARIANTS
    if persist_ledger:
        upsert_variant_ledger(
            db_path, variants, sample_window=f"{date_from}_{date_to}"
        )
    signals = load_signals(date_from, date_to, db_path)
    vix_table = _load_vix_table(db_path)

    # Attach forward 1-min bars to every signal so the canonical bar-walk
    # realized-R (50/30/20 partial scale-out, matching
    # ablation_backtest._simulate_baseline) can be computed in
    # compute_outcome_metrics. Done ONCE here so the K-variant fan-out below
    # reuses the same fwd_bars list instead of re-running the bisect lookup
    # per variant. When historical_bar is absent (synthetic test DBs) the
    # field is None and compute_outcome_metrics falls back to binary scoring.
    bar_by_ms, bar_ms_sorted = _load_spx_bars(db_path, date_from, date_to)
    _attach_forward_bars(signals, bar_by_ms, bar_ms_sorted)

    # Apply each variant, compute metrics.
    applied: dict[str, list[dict]] = {}
    metrics: dict[str, dict] = {}
    for v in variants:
        rows = apply_variant(signals, v, vix_table)
        applied[v.name] = rows
        metrics[v.name] = compute_outcome_metrics(rows)

    # Pick the baseline variant (by name='baseline'). If missing, use
    # the variant_metrics that have n_actionable closest to the
    # original actionable count.
    baseline_name = "baseline"
    if baseline_name not in metrics:
        baseline_name = next(iter(metrics.keys()), None)
    baseline_metrics = metrics.get(baseline_name, {}) if baseline_name else {}
    deltas: dict[str, dict] = {}
    if baseline_metrics:
        for name, m in metrics.items():
            if name == baseline_name:
                continue
            deltas[name] = compare_to_baseline(m, baseline_metrics)

    n_actionable_original = sum(
        1 for s in signals if s["direction"] in ("LONG_CALLS", "LONG_PUTS")
    )

    return {
        "date_from": date_from,
        "date_to": date_to,
        "n_signals": len(signals),
        "n_actionable_original": n_actionable_original,
        "variants": [v.name for v in variants],
        "baseline_name": baseline_name,
        "metrics": metrics,
        "deltas_vs_baseline": deltas,
        # raw applied rows omitted from the result dict for compactness —
        # uncomment the next line for full-fidelity outputs.
        # "applied": applied,
    }


# ---------------------------------------------------------------------------
# Reporting (markdown)
# ---------------------------------------------------------------------------

def format_report(result: dict) -> str:
    """Markdown report.

    Lead with TLDR (which variant delta-baselines best), then per-variant
    detail, regime stratification, offset distribution, caveats."""
    out: list[str] = []
    out.append(
        f"# Shadow Signal Replay: {result['date_from']} to {result['date_to']}"
    )
    out.append("")
    out.append(
        f"Signals loaded: {result['n_signals']} "
        f"({result['n_actionable_original']} actionable in original)."
    )
    out.append(f"Variants tested: {', '.join(result['variants'])}")
    out.append("")

    # TLDR — rank by delta_sum_expected_r descending.
    deltas = result.get("deltas_vs_baseline") or {}
    metrics = result.get("metrics") or {}
    if deltas:
        ranked = sorted(
            deltas.items(),
            key=lambda kv: kv[1].get("delta_sum_expected_r") or 0.0,
            reverse=True,
        )
        top_name, top_d = ranked[0]
        baseline_m = metrics.get(result.get("baseline_name") or "baseline", {})
        out.append("## TLDR")
        out.append("")
        out.append(
            f"**Baseline** sum expected-R = "
            f"{baseline_m.get('sum_expected_r')}R "
            f"({baseline_m.get('n_actionable')} actionable, "
            f"{baseline_m.get('counter_bias_pct')}% counter-bias, "
            f"T1 hit-rate {baseline_m.get('t1_hit_rate')}%)."
        )
        out.append("")
        out.append(
            f"**Best variant** by Δ sum expected-R: **`{top_name}`** "
            f"with Δ sum R = **+{top_d.get('delta_sum_expected_r')}R** "
            f"(Δ counter-bias % = {top_d.get('delta_counter_bias_pct')}pp, "
            f"signals dropped = "
            f"{-1 * (top_d.get('delta_n_actionable') or 0)})."
        )
        # 5/26 spotlight — report 19 emphasized this session.
        spotlight_date = "2026-05-26"
        bs = baseline_m.get("by_session", {}).get(spotlight_date)
        ts_top = metrics.get(top_name, {}).get(
            "by_session", {}
        ).get(spotlight_date)
        if bs and ts_top:
            out.append("")
            out.append(
                f"**5/26 spotlight**: baseline {bs['n']} actionable "
                f"({bs['cb_pct']}% counter-bias, T1 hit-rate {bs['t1_hit_rate']}%, "
                f"sum R = {round(bs['sum_r'], 2)}); "
                f"`{top_name}` {ts_top['n']} actionable "
                f"({ts_top['cb_pct']}% counter-bias, "
                f"T1 hit-rate {ts_top['t1_hit_rate']}%, "
                f"sum R = {round(ts_top['sum_r'], 2)}). "
                f"Net: dropping the {bs['n_cb']} counter-bias fires shifts the "
                f"session from {round(bs['sum_r'], 2)}R to "
                f"{round(ts_top['sum_r'], 2)}R."
            )
        out.append("")
        out.append("| Variant | Δ N actionable | Δ N counter-bias | Δ CB% | Δ sum R | Δ mean R | Δ T1 hit% | Δ stop hit% |")
        out.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for name, d in ranked:
            out.append(
                f"| {name} | {d.get('delta_n_actionable')} | "
                f"{d.get('delta_n_counter_bias')} | "
                f"{d.get('delta_counter_bias_pct')} | "
                f"{d.get('delta_sum_expected_r')} | "
                f"{d.get('delta_mean_expected_r')} | "
                f"{d.get('delta_t1_hit_rate')} | "
                f"{d.get('delta_stop_hit_rate')} |"
            )
        out.append("")

    # Per-variant metrics table
    out.append("## Per-variant aggregate")
    out.append("")
    out.append("| Variant | n actionable | dropped | CB% | mean R | sum R | T1% | stop% |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for name, m in metrics.items():
        out.append(
            f"| {name} | {m['n_actionable']} | {m['n_dropped']} | "
            f"{m['counter_bias_pct']} | "
            f"{m['mean_expected_r']} | {m['sum_expected_r']} | "
            f"{m['t1_hit_rate']} | {m['stop_hit_rate']} |"
        )
    out.append("")
    out.append(
        "Aligned-vs-counter-bias mean R per variant (where lookahead exists):"
    )
    out.append("")
    out.append("| Variant | mean aligned R | mean counter-bias R |")
    out.append("|---|---:|---:|")
    for name, m in metrics.items():
        out.append(
            f"| {name} | {m['mean_aligned_r']} | {m['mean_counter_bias_r']} |"
        )
    out.append("")

    # Per-session breakdown (report 19 organizes by session — mirror it)
    out.append("## Per-session breakdown (per variant)")
    out.append("")
    out.append("| Variant | Session | n | n_cb | cb% | T1% | mean R | sum R |")
    out.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for name, m in metrics.items():
        for date in sorted(m["by_session"].keys()):
            r = m["by_session"][date]
            out.append(
                f"| {name} | {date} | {r['n']} | {r['n_cb']} | "
                f"{r['cb_pct']} | {r['t1_hit_rate']} | "
                f"{r['mean_r']} | {round(r['sum_r'], 2)} |"
            )
    out.append("")

    # Strike offset distribution
    out.append("## Strike offset distribution (vol-adaptive variants)")
    out.append("")
    out.append("| Variant | n | min | p25 | median | mean | p75 | max | below 35pt baseline |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    any_offsets = False
    for name, m in metrics.items():
        os_ = m.get("offset_stats")
        if not os_:
            continue
        any_offsets = True
        out.append(
            f"| {name} | {os_['n']} | {os_['min']} | {os_['p25']} | "
            f"{os_['median']} | {os_['mean']} | {os_['p75']} | "
            f"{os_['max']} | {os_['n_below_baseline']} |"
        )
    if not any_offsets:
        out.append("_(no variants applied a strike offset)_")
    out.append("")

    # Regime stratification
    out.append("## Per-variant × regime breakdown")
    out.append("")
    out.append("| Variant | Regime | n | n_eval | mean R | T1% |")
    out.append("|---|---|---:|---:|---:|---:|")
    for name, m in metrics.items():
        for regime in sorted(m["by_regime"].keys()):
            r = m["by_regime"][regime]
            out.append(
                f"| {name} | {regime} | {r['n']} | {r['n_eval']} | "
                f"{r['mean_r']} | {r['t1_hit_rate']} |"
            )
    out.append("")

    # Recommendation
    out.append("## Recommended next step")
    out.append("")
    if deltas:
        top_name = next(iter(ranked))[0]
        out.append(
            f"Graduate **`{top_name}`** to `ablation_backtest.py` for "
            f"proper DSR / PBO / CPCV evaluation. Treat this replay's "
            f"+R estimate as an upper bound — variants share the same "
            f"signal stream, so honest N_trials for the family applies."
        )
    out.append("")

    # Caveats
    out.append("## Caveats")
    out.append("")
    out.append(
        "- Sample size n=226 actionable across 5 sessions is small; "
        "variant deltas have wide CIs."
    )
    out.append(
        "- Variants share the input signal stream — they are not "
        "independent. DSR family correction applies."
    )
    out.append(
        "- Realized-R is the canonical bar-walk + 50/30/20 partial "
        "scale-out (matching ablation_backtest._simulate_baseline), NOT "
        "the legacy binary T1-or-stop scoring. The binary path overstated "
        "counter-bias direction filters ~15x (methodology fix 2026-05-27) "
        "and now survives only as a no-bars fallback for synthetic test "
        "DBs. The T1%/stop% columns remain the descriptive lookahead-flag "
        "hit rates and are independent of the R methodology."
    )
    out.append(
        "- Vol-adaptive strike P&L impact is NOT measured — chain "
        "data only began populating 2026-05-26 (`historical_option_chain_point`). "
        "Offset distribution is descriptive."
    )
    out.append(
        "- 5/19-5/21 are engine v1 only; 5/22 + 5/26 are v2. Mixed-"
        "engine cohort. Stratify by engine_version for in-engine-only "
        "claims."
    )
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _filter_variants(arg: str | None) -> list[VerdictVariant]:
    if not arg:
        return REGISTERED_VARIANTS
    requested = {x.strip() for x in arg.split(",") if x.strip()}
    selected = [v for v in REGISTERED_VARIANTS if v.name in requested]
    unknown = requested - {v.name for v in REGISTERED_VARIANTS}
    if unknown:
        raise SystemExit(
            f"Unknown variant(s): {sorted(unknown)}. "
            f"Available: {[v.name for v in REGISTERED_VARIANTS]}"
        )
    # If baseline isn't explicitly requested, always include it so the
    # delta-vs-baseline table works.
    if not any(v.name == "baseline" for v in selected):
        bv = _variant_by_name("baseline")
        if bv:
            selected = [bv] + selected
    return selected


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Shadow signal-replay for VERDICT decisions.",
    )
    ap.add_argument("date_from", help="YYYY-MM-DD")
    ap.add_argument("date_to", help="YYYY-MM-DD")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument(
        "--variants", default="",
        help="Comma-separated variant names to test. Default: all registered.",
    )
    ap.add_argument("--out", default="", help="Write report to this path (markdown).")
    ap.add_argument("--json", action="store_true", help="Emit raw JSON instead of markdown.")
    ap.add_argument(
        "--no-ledger", action="store_true",
        help="Skip variant_ledger upsert (dry run).",
    )
    args = ap.parse_args()

    variants = _filter_variants(args.variants)
    result = run_replay(
        args.date_from, args.date_to, args.db,
        variants=variants, persist_ledger=not args.no_ledger,
    )
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    if args.json:
        text = json.dumps(result, indent=2, default=str)
    else:
        text = format_report(result)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
