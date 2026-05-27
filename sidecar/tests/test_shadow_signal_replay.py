"""Tests for scripts/research/shadow_signal_replay.py.

Targeting Phase A-D implementation:
  - VerdictVariant dataclass behavior
  - baseline keeps every original verdict
  - conjunction_req_2 drops counter-bias signals lacking 2 gates
  - conjunction_req_2 keeps counter-bias signals with 2+ gates
  - conjunction_req_2 doesn't touch aligned signals
  - drop_low_counter_bias correctly identifies LOW + counter-bias
  - vol_adaptive offset bounds (floor 5, ceiling 40)
  - vol_adaptive falls back to 20.0 when VIX missing
  - run_replay structure (metrics dict keys)
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Add scripts/research to path so we can import shadow_signal_replay.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RESEARCH_DIR = _PROJECT_ROOT / "scripts" / "research"
sys.path.insert(0, str(_RESEARCH_DIR))

from shadow_signal_replay import (  # noqa: E402
    REGISTERED_VARIANTS,
    VerdictVariant,
    _conjunction_req_2_re_verdict,
    _count_confirming_gates,
    _drop_low_counter_bias_re_verdict,
    _is_counter_bias,
    _vol_adaptive_offset,
    apply_variant,
    compute_outcome_metrics,
    run_replay,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_signal(
    *,
    sid: int = 1,
    direction: str = "LONG_PUTS",
    drift_bias: str = "BULLISH",
    confidence: str = "LOW",
    spx_price: float = 7500.0,
    extras: dict | None = None,
    aggression_call_pct: float | None = None,
    aggression_put_pct: float | None = None,
    would_have_hit_t1: int | None = 0,
    would_have_hit_stop: int | None = 1,
    timestamp: str = "2026-05-26T15:00:00",
    regime: str = "TREND_DAY",
    session_date: str = "2026-05-26",
) -> dict:
    return {
        "id": sid,
        "session_date": session_date,
        "timestamp": timestamp,
        "spx_price": spx_price,
        "direction": direction,
        "drift_bias": drift_bias,
        "confidence": confidence,
        "regime": regime,
        "extras": extras or {},
        "aggression_call_pct": aggression_call_pct,
        "aggression_put_pct": aggression_put_pct,
        "iv_rank_call_pct": None,
        "iv_rank_put_pct": None,
        "engine_version": 2,
        "vix_at_fire": None,
        "lookahead_minutes": 60,
        "would_have_hit_t1": would_have_hit_t1,
        "would_have_hit_t2": 0,
        "would_have_hit_t3": 0,
        "would_have_hit_stop": would_have_hit_stop,
        "t1_hit_probability": 1.0 if would_have_hit_t1 else 0.0,
        "spx_mfe_pts": 5.0,
        "spx_mae_pts": -10.0,
        "spx_at_signal": spx_price,
        "spx_close_at_window_end": spx_price - 5.0,
    }


def _seed_minimal_db(db_path: str, signals: list[dict]) -> None:
    """Create the minimal schema + rows needed for run_replay."""
    db = sqlite3.connect(db_path)
    cur = db.cursor()
    cur.executescript(
        """
        CREATE TABLE signal_iter (
            id INTEGER PRIMARY KEY, session_date TEXT NOT NULL,
            iter_num INTEGER NOT NULL, timestamp TEXT NOT NULL,
            spx_price REAL NOT NULL, direction TEXT NOT NULL,
            confidence TEXT NOT NULL, reasoning TEXT NOT NULL,
            drift_cum_net_m REAL, drift_bias TEXT,
            ntm_max_pain REAL, raw_max_pain REAL,
            top_gex_walls TEXT, top_dex_walls TEXT,
            aggression_call_pct REAL, aggression_put_pct REAL,
            iv_rank_call_pct REAL, iv_rank_put_pct REAL,
            extras TEXT, engine_version INTEGER
        );
        CREATE TABLE signal_lookahead_outcome (
            id INTEGER PRIMARY KEY, signal_iter_id INTEGER NOT NULL,
            session_date TEXT NOT NULL, lookahead_minutes INTEGER NOT NULL,
            spx_at_signal REAL, spx_mfe_pts REAL, spx_mae_pts REAL,
            spx_close_at_window_end REAL,
            would_have_hit_t1 INTEGER, would_have_hit_stop INTEGER,
            would_have_hit_t2 INTEGER, would_have_hit_t3 INTEGER,
            t1_hit_probability REAL,
            direction_at_signal TEXT, confidence_at_signal TEXT,
            computed_at TEXT
        );
        CREATE TABLE vix_sample (
            id INTEGER PRIMARY KEY,
            timestamp_ms INTEGER NOT NULL, value REAL NOT NULL
        );
        CREATE TABLE variant_ledger (
            id INTEGER PRIMARY KEY,
            variant_id TEXT NOT NULL UNIQUE,
            family TEXT NOT NULL, sub_family TEXT,
            description TEXT NOT NULL, hypothesis TEXT NOT NULL,
            registered_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            tested_in TEXT NOT NULL, sample_window TEXT NOT NULL,
            outcome_summary TEXT NOT NULL,
            still_active INTEGER NOT NULL,
            variant_params TEXT
        );
        """
    )
    import json
    for s in signals:
        cur.execute(
            """
            INSERT INTO signal_iter (
                id, session_date, iter_num, timestamp, spx_price,
                direction, confidence, reasoning, drift_bias, drift_cum_net_m,
                ntm_max_pain, raw_max_pain, top_gex_walls, top_dex_walls,
                aggression_call_pct, aggression_put_pct, extras, engine_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                s["id"], s["session_date"], s["id"], s["timestamp"],
                s["spx_price"], s["direction"], s["confidence"], "seed",
                s["drift_bias"], 0.0, 0.0, 0.0, "[]", "[]",
                s.get("aggression_call_pct"), s.get("aggression_put_pct"),
                json.dumps(s["extras"]),
                s.get("engine_version", 2),
            ),
        )
        if s.get("lookahead_minutes") is not None:
            cur.execute(
                """
                INSERT INTO signal_lookahead_outcome (
                    signal_iter_id, session_date, lookahead_minutes,
                    spx_at_signal, spx_mfe_pts, spx_mae_pts,
                    spx_close_at_window_end,
                    would_have_hit_t1, would_have_hit_stop,
                    would_have_hit_t2, would_have_hit_t3,
                    t1_hit_probability,
                    direction_at_signal, confidence_at_signal, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    s["id"], s["session_date"], s["lookahead_minutes"],
                    s["spx_price"], s["spx_mfe_pts"], s["spx_mae_pts"],
                    s["spx_close_at_window_end"],
                    s["would_have_hit_t1"], s["would_have_hit_stop"],
                    s.get("would_have_hit_t2", 0), s.get("would_have_hit_t3", 0),
                    s["t1_hit_probability"],
                    s["direction"], s["confidence"],
                    "2026-05-26T15:00:00",
                ),
            )
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_baseline_variant_produces_no_changes() -> None:
    """The baseline variant must keep every original verdict unchanged."""
    signals = [
        _make_signal(sid=1, direction="LONG_CALLS", drift_bias="BULLISH"),
        _make_signal(sid=2, direction="LONG_PUTS", drift_bias="BULLISH"),
        _make_signal(sid=3, direction="WAIT", drift_bias="NEUTRAL"),
    ]
    baseline = next(v for v in REGISTERED_VARIANTS if v.name == "baseline")
    applied = apply_variant(signals, baseline)
    assert len(applied) == 3
    for orig, out in zip(signals, applied):
        assert out["original_direction"] == orig["direction"]
        assert out["new_direction"] == orig["direction"]
        assert out["changed"] is False
        assert out["new_offset_pts"] is None


def test_conjunction_req_2_drops_counter_bias_with_zero_gates() -> None:
    """LONG_PUTS on BULLISH drift with zero confirming gates → WAIT."""
    # extras has no alt_direction_30m/4h/microstructure, no aggression
    sig = _make_signal(
        direction="LONG_PUTS", drift_bias="BULLISH",
        extras={},  # no gates at all
        aggression_call_pct=40.0,  # delta <10 → no aggression vote
        aggression_put_pct=39.0,
    )
    applied = apply_variant([sig], next(
        v for v in REGISTERED_VARIANTS if v.name == "conjunction_req_2"
    ))
    assert applied[0]["new_direction"] == "WAIT"
    assert applied[0]["changed"] is True


def test_conjunction_req_2_keeps_counter_bias_with_2_gates() -> None:
    """LONG_PUTS on BULLISH drift with 2 confirming gates (alt_30m +
    microstructure both BEARISH) → kept."""
    extras = {
        "alt_direction_30m": {"signal": "BEARISH"},
        "microstructure_at_fire": {"direction": "BEARISH_PRESSURE"},
    }
    sig = _make_signal(
        direction="LONG_PUTS", drift_bias="BULLISH", extras=extras,
        aggression_call_pct=39.0, aggression_put_pct=40.0,  # delta <10 → no vote
    )
    applied = apply_variant([sig], next(
        v for v in REGISTERED_VARIANTS if v.name == "conjunction_req_2"
    ))
    assert applied[0]["new_direction"] == "LONG_PUTS"
    assert applied[0]["changed"] is False


def test_conjunction_req_2_does_not_touch_aligned_signals() -> None:
    """LONG_CALLS on BULLISH drift is aligned; variant must leave it alone."""
    sig = _make_signal(direction="LONG_CALLS", drift_bias="BULLISH",
                       extras={})
    applied = apply_variant([sig], next(
        v for v in REGISTERED_VARIANTS if v.name == "conjunction_req_2"
    ))
    assert applied[0]["new_direction"] == "LONG_CALLS"
    assert applied[0]["changed"] is False


def test_conjunction_req_2_with_one_gate_still_drops() -> None:
    """Counter-bias + only 1 confirming gate must still be demoted."""
    extras = {"alt_direction_30m": {"signal": "BEARISH"}}
    sig = _make_signal(
        direction="LONG_PUTS", drift_bias="BULLISH", extras=extras,
    )
    applied = apply_variant([sig], next(
        v for v in REGISTERED_VARIANTS if v.name == "conjunction_req_2"
    ))
    assert applied[0]["new_direction"] == "WAIT"


def test_count_confirming_gates_handles_aggression_threshold() -> None:
    """aggression_call_pct must exceed aggression_put_pct by >10pp to vote."""
    sig_low_delta = _make_signal(
        aggression_call_pct=45.0, aggression_put_pct=40.0,  # +5pp → no vote
        extras={},
    )
    assert _count_confirming_gates(
        sig_low_delta["extras"], sig_low_delta, "BULLISH"
    ) == 0
    sig_high_delta = _make_signal(
        aggression_call_pct=55.0, aggression_put_pct=40.0,  # +15pp → vote
        extras={},
    )
    assert _count_confirming_gates(
        sig_high_delta["extras"], sig_high_delta, "BULLISH"
    ) == 1


def test_drop_low_counter_bias_correctly_identifies_low_and_counter() -> None:
    """LOW + counter-bias → WAIT; MEDIUM + counter-bias → keep."""
    sig_low = _make_signal(
        direction="LONG_PUTS", drift_bias="BULLISH", confidence="LOW"
    )
    sig_med = _make_signal(
        direction="LONG_PUTS", drift_bias="BULLISH", confidence="MEDIUM"
    )
    sig_aligned_low = _make_signal(
        direction="LONG_CALLS", drift_bias="BULLISH", confidence="LOW"
    )
    variant = next(
        v for v in REGISTERED_VARIANTS if v.name == "drop_low_counter_bias"
    )
    out_low = apply_variant([sig_low], variant)[0]
    out_med = apply_variant([sig_med], variant)[0]
    out_aligned = apply_variant([sig_aligned_low], variant)[0]
    assert out_low["new_direction"] == "WAIT"
    assert out_med["new_direction"] == "LONG_PUTS"  # MEDIUM not dropped
    assert out_aligned["new_direction"] == "LONG_CALLS"  # aligned not dropped


def test_vol_adaptive_offset_bounds() -> None:
    """offset = clamp(VIX*1.5, 5, 40)."""
    # Very low VIX → clamped to 5
    assert _vol_adaptive_offset(7500.0, 1.0, {}) == 5.0
    # Mid VIX → VIX*1.5
    assert _vol_adaptive_offset(7500.0, 14.0, {}) == pytest.approx(21.0)
    # Very high VIX → clamped to 40
    assert _vol_adaptive_offset(7500.0, 50.0, {}) == 40.0
    # Edge case: floor exactly
    assert _vol_adaptive_offset(7500.0, 3.33, {}) == 5.0
    # Edge case: ceiling exactly
    assert _vol_adaptive_offset(7500.0, 26.67, {}) == 40.0


def test_vol_adaptive_offset_falls_back_to_20_when_vix_missing() -> None:
    """No VIX (None or <=0) → 20.0 fallback."""
    assert _vol_adaptive_offset(7500.0, None, {}) == 20.0
    assert _vol_adaptive_offset(7500.0, 0.0, {}) == 20.0
    assert _vol_adaptive_offset(7500.0, -1.0, {}) == 20.0


def test_is_counter_bias_helper() -> None:
    """LONG_PUTS on BULLISH = counter; LONG_CALLS on BEARISH = counter."""
    assert _is_counter_bias("LONG_PUTS", "BULLISH") is True
    assert _is_counter_bias("LONG_CALLS", "BEARISH") is True
    assert _is_counter_bias("LONG_CALLS", "BULLISH") is False
    assert _is_counter_bias("LONG_PUTS", "BEARISH") is False
    assert _is_counter_bias("WAIT", "BULLISH") is False
    assert _is_counter_bias("LONG_PUTS", "NEUTRAL") is False


def test_compute_outcome_metrics_basic_structure() -> None:
    """Output dict has expected keys + counts are correct on a small sample."""
    signals = [
        # 1 aligned T1 hit
        dict(
            _make_signal(sid=1, direction="LONG_CALLS", drift_bias="BULLISH",
                         would_have_hit_t1=1, would_have_hit_stop=0),
            original_direction="LONG_CALLS",
            new_direction="LONG_CALLS",
            changed=False, new_offset_pts=None, original_offset_pts=35.0,
        ),
        # 1 counter-bias stop hit (kept)
        dict(
            _make_signal(sid=2, direction="LONG_PUTS", drift_bias="BULLISH",
                         would_have_hit_t1=0, would_have_hit_stop=1),
            original_direction="LONG_PUTS",
            new_direction="LONG_PUTS",
            changed=False, new_offset_pts=None, original_offset_pts=35.0,
        ),
        # 1 counter-bias dropped
        dict(
            _make_signal(sid=3, direction="LONG_PUTS", drift_bias="BULLISH",
                         would_have_hit_t1=0, would_have_hit_stop=1),
            original_direction="LONG_PUTS",
            new_direction="WAIT",
            changed=True, new_offset_pts=None, original_offset_pts=35.0,
        ),
    ]
    m = compute_outcome_metrics(signals)
    # Expected keys present
    expected_keys = {
        "n_total", "n_actionable", "n_dropped", "n_changed",
        "n_counter_bias_in_actionable", "counter_bias_pct",
        "n_with_lookahead", "mean_expected_r", "sum_expected_r",
        "mean_aligned_r", "mean_counter_bias_r",
        "t1_hit_rate", "stop_hit_rate",
        "by_regime", "by_confidence", "by_hour", "offset_stats",
    }
    assert expected_keys.issubset(m.keys())
    assert m["n_total"] == 3
    assert m["n_actionable"] == 2  # the WAIT signal dropped
    assert m["n_dropped"] == 1
    assert m["n_changed"] == 1
    assert m["n_counter_bias_in_actionable"] == 1
    # T1 hit (1.5R) + stop hit (-1R) = +0.5R sum across 2 evals
    assert m["sum_expected_r"] == pytest.approx(0.5)


def test_run_replay_returns_metrics_dict_with_expected_keys(
    tmp_path: Path,
) -> None:
    """End-to-end run_replay test against a tiny seeded DB.

    Validates that the result dict has the structure callers depend on
    and that the baseline variant matches the input outcomes."""
    db_path = str(tmp_path / "test_shadow_replay.db")
    signals = [
        _make_signal(sid=1, direction="LONG_CALLS", drift_bias="BULLISH",
                     would_have_hit_t1=1, would_have_hit_stop=0,
                     timestamp="2026-05-26T15:00:00",
                     session_date="2026-05-26"),
        _make_signal(sid=2, direction="LONG_PUTS", drift_bias="BULLISH",
                     would_have_hit_t1=0, would_have_hit_stop=1,
                     timestamp="2026-05-26T15:05:00",
                     session_date="2026-05-26"),
        _make_signal(sid=3, direction="WAIT", drift_bias="NEUTRAL",
                     would_have_hit_t1=0, would_have_hit_stop=0,
                     timestamp="2026-05-26T15:10:00",
                     session_date="2026-05-26"),
    ]
    _seed_minimal_db(db_path, signals)
    result = run_replay(
        "2026-05-26", "2026-05-26", db_path=db_path,
        persist_ledger=True,  # exercises variant_ledger upsert path
    )
    assert result["date_from"] == "2026-05-26"
    assert result["date_to"] == "2026-05-26"
    assert result["n_signals"] == 3
    assert result["n_actionable_original"] == 2
    assert "baseline" in result["metrics"]
    # baseline must equal input
    bm = result["metrics"]["baseline"]
    assert bm["n_actionable"] == 2
    assert bm["n_dropped"] == 0
    assert bm["n_changed"] == 0
    # deltas exist for non-baseline variants
    assert "conjunction_req_2" in result["deltas_vs_baseline"]
    assert "strike_vol_adaptive" in result["deltas_vs_baseline"]
    # variant_ledger upserts succeeded (non-baseline only)
    db = sqlite3.connect(db_path)
    n = db.execute(
        "SELECT COUNT(*) FROM variant_ledger WHERE tested_in='shadow_signal_replay'"
    ).fetchone()[0]
    db.close()
    # baseline excluded (family=''), so we expect the other 5 variants:
    # conjunction_req_2, conjunction_req_1, drop_low_counter_bias,
    # strike_vol_adaptive, conjunction_req_2_AND_vol_adaptive
    assert n == 5
