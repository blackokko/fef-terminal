# Canonical "Actionable Signal" Definition

**Shipped:** 2026-05-22 (Chat audit Priority 10)
**Source of truth:** `sidecar/src/fef_sidecar/db/repository.py`

## The canonical

> An **actionable signal** is a `SignalIter` row where `direction != WAIT`.

That's it. It matches the live engine's intent (every non-WAIT verdict is
something the engine wanted to trade) and the DailySession /
daily_review `actionable_signals` counters.

## The three axes of variation

Different research scripts produced different actionable counts over the
5/18-5/22 window (268, 303, 307) — not because the canonical was unclear,
but because each script set three filter axes inconsistently:

| Axis | Values | What it does |
|---|---|---|
| **Date range** | `session_date` (single) or `date_from`+`date_to` (range) | Trivial — just be explicit which one |
| **Account-class filter** | `paper_only=True` (default) / `False` | When True: keeps `extras.account_class IN ('paper', NULL)`. NULL is treated paper-equivalent because 100% of pre-2026-05-21 data is from DUM098107. |
| **Price band guard** | `(1000.0, 10000.0)` (default) or `None` | Drops rows where `spx_price` is outside the band (catches test/mock rows with sentinel prices like 67.20). |
| **Lookahead requirement** | `require_lookahead=True` / `False` (default) | When True: only rows that have a `SignalLookaheadOutcome` @ 60min. Use this for any analysis that needs forward outcomes. |

## Why counts diverge

5/18-5/22 example numbers:

| Filter set | Count | Notes |
|---|---:|---|
| paper, price-band, no-lookahead | **303** | `build_actionable_signals_sql` canonical default |
| paper, no price band | 307 | matches `SUM(daily_review.actionable_signals)` |
| all account classes, no price band | 307 | same as above for this window |
| paper, price-band, require_lookahead | 268 | matches `ablation_backtest.n_signals` |

The 4-row gap between 303 and 307 is test/mock rows with `spx_price`
outside [1000, 10000]. The 35-row gap between 303 and 268 is signals
that don't have a 60-min lookahead outcome row yet (most recent
signals; the worker hasn't processed them).

## How to use the helper

**Async / sqlmodel callers** (engine, WS, sidecar code):
```python
from fef_sidecar.db.repository import fetch_actionable_signals

rows = await fetch_actionable_signals(
    session, date_from="2026-05-18", date_to="2026-05-22",
    paper_only=True, price_band=(1000.0, 10000.0),
    require_lookahead=False,
)
```

**SQLite3 callers** (research scripts):
```python
from fef_sidecar.db.repository import build_actionable_signals_sql

sql, params = build_actionable_signals_sql(
    date_from="2026-05-18", date_to="2026-05-22",
    paper_only=True, price_band=(1000.0, 10000.0),
)
rows = cur.execute(sql, params).fetchall()
```

The SQL builder accepts a custom `select_clause` (defaults to `s.*`) so
callers can hand-pick columns or add joins downstream (see
`research_dump._load_fired_with_outcomes` for the LEFT JOIN onto
`signal_lookahead_outcome` pattern).

## Post-migration script inventory

| Script | Uses helper? | Filter axes set |
|---|---|---|
| `ablation_backtest.py` | yes | paper_only (from CLI), price_band=(1000,10000), require_lookahead=False (joins lookahead in a second query) |
| `research_dump.py::_load_fired_with_outcomes` | yes | paper_only (from CLI), price_band=(1000,10000), require_lookahead=False (LEFT JOIN'd) |
| `shadow_exit_replay.py` | n/a | Operates on `live_order` parents, not `signal_iter` rows. The signal_iter loadout for regime lookup intentionally includes WAIT rows. |
| `per_regime_sweep.py` | n/a | REPLAYS the engine under candidate configs, so it loads ALL signals (including WAIT) and lets each replay re-decide direction. Documented inline. |
| `realistic_fill_estimator.py` | not yet migrated | Has its own `direction IN ('LONG_CALLS','LONG_PUTS')` query — equivalent to `direction != WAIT`. Counts already match daily_review. Migration is optional. |
| `signal_quality_scorer.py` | not yet migrated | Same pattern as realistic_fill_estimator. |
| `mid_session_snapshot_2026_05_22.py` | n/a | Scoped to today; uses `direction != 'WAIT'` directly in one query (Section 4). The `actionable_rows` count it reports is per-session. |

## Cross-check on every run

Where the helper is in use, each script emits a console block at the end
of its run comparing the canonical count to `daily_review.actionable_signals`
and `daily_session.actionable_signals`. If they diverge, the script
prints a WARNING. This catches drift between the counters and the
research-side filter.
