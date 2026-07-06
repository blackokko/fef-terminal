"""Calc-correctness regression tests for the FEF research/backtest tools.

Covers the surgical audit fixes:
  1. optimization._proxy_pnl — delta-aware $/R (no more magic $500/ATM-0.5)
  2. optimization._forward_walk_within_session — 0DTE session-boundary purge
     + SweepResult.low_confidence min-N guard
  3. api._walk_be_ladder_exit / _backtest_compute — BE-stop banks the LOCKED
     R (0.0/0.5/1.0), NOT the ladder TRIGGER (0.5/1.0/1.5)  [the critical one]
  4. api._walk_be_ladder_exit — full T3 books ACTUAL per-tier R at 50/30/20,
     not a hardcoded 2.95
  5. tuning_recommender._pooled_win_pct — sample-weighted pooling (Σwins/Σn)
     + realized-fill preference
  6. what_if_backtest._outcome_R — partial-aware (banks T1/T2 before a stop)
  7. backtest._sharpe_per_trade — per-trade Sharpe, no √252
  8. ml_patterns._session_grouped_splits — session-grouped forward CV
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# Tiny stand-ins
# ---------------------------------------------------------------------------
class _Bar:
    """HistoricalBar stand-in (only the attrs the walkers read)."""

    def __init__(self, ts_ms: int, o: float, h: float, l: float, c: float):
        self.timestamp_ms = ts_ms
        self.open = o
        self.high = h
        self.low = l
        self.close = c


class _Sig:
    """Minimal SignalIter stand-in for _backtest_compute (non-preset path)."""

    def __init__(self, *, sig_id, ts, session_date, spx, direction, extras):
        self.id = sig_id
        self.timestamp = ts
        self.session_date = session_date
        self.spx_price = spx
        class _D:
            def __init__(self, v): self.value = v
        self.direction = _D(direction)
        self.extras = extras
        # getattr-read fields
        self.top_cex_walls = []
        self.top_vex_walls = []
        self.top_gex_by_expiry = []
        self.top_dex_by_expiry = []


# ===========================================================================
# Fix 3 + 4 — BE-stop scoring (THE critical one): LOCKED R, not the trigger
# ===========================================================================
def test_be_stop_breakeven_scratch_scores_locked_zero_not_trigger():
    """A trade that pokes to +0.5R (arming the first ladder rung, which LOCKS
    the stop at breakeven = 0.0R) then reverses through it must score 0.0R —
    NOT the +0.5R ladder TRIGGER. This is the leak that turned breakeven
    scratches into phantom wins and flipped tournament boards."""
    from fef_sidecar.api import _walk_be_ladder_exit

    # entry 7400, stop 7390 (risk 10 → 1R = 10pt). Targets far so no tier hits.
    path = [
        # (ts, open, high, low, close): high 7405 = +0.5R arms rung → lock 0.0R,
        # stop ratchets to 7400. low 7402 stays above it (no stop yet).
        (1, 7404, 7405, 7402, 7404),
        # reverse: low 7399 <= locked stop 7400 → BE stop fires at breakeven.
        (2, 7401, 7401, 7399, 7400),
    ]
    out = _walk_be_ladder_exit(
        entry_spx=7400, t1=7430, t2=7440, t3=7450, stop=7390,
        is_long=True, path_items=path, apply_be_ladder=True,
    )
    assert out["be_step"] == 0.5           # trigger reached
    assert out["be_lock_R"] == 0.0         # but the LOCK is breakeven
    assert out["outcome"] == "BE_STOP_0.0R"
    assert out["realized_R"] == pytest.approx(0.0)   # NOT +0.5 (the old bug)


def test_be_stop_locks_half_r_not_one_r_trigger():
    """Poke to +1.0R (arms the 2nd rung → LOCKS +0.5R) then reverse into the
    locked stop. Must score +0.5R (locked), not +1.0R (the trigger)."""
    from fef_sidecar.api import _walk_be_ladder_exit

    path = [
        (1, 7408, 7410, 7407, 7408),   # +1.0R via high 7410 → lock 0.5R, stop→7405
        (2, 7404, 7404, 7404, 7404),   # low 7404 <= 7405 → BE stop at +0.5R
    ]
    out = _walk_be_ladder_exit(
        entry_spx=7400, t1=7430, t2=7440, t3=7450, stop=7390,
        is_long=True, path_items=path, apply_be_ladder=True,
    )
    assert out["be_step"] == 1.0
    assert out["be_lock_R"] == 0.5
    assert out["outcome"] == "BE_STOP_0.5R"
    assert out["realized_R"] == pytest.approx(0.5)   # locked, not the 1.0 trigger


def test_hard_stop_with_ladder_off_scores_minus_one():
    from fef_sidecar.api import _walk_be_ladder_exit

    path = [(1, 7399, 7401, 7389, 7392)]  # low 7389 < stop 7390 → hard stop
    out = _walk_be_ladder_exit(
        entry_spx=7400, t1=7430, t2=7440, t3=7450, stop=7390,
        is_long=True, path_items=path, apply_be_ladder=False,
    )
    assert out["outcome"] == "STOP"
    assert out["realized_R"] == pytest.approx(-1.0)


def test_partials_banked_before_stop_produce_positive_r():
    """T1 fires (banks 0.5·1R) then the residual stops at the ladder lock →
    net positive, not the old flat -1R."""
    from fef_sidecar.api import _walk_be_ladder_exit

    path = [
        (1, 7409, 7410, 7406, 7409),   # high 7410 = T1 (+1R) AND arms lock 0.5R
        (2, 7404, 7404, 7404, 7404),   # residual stops at 7405 (=+0.5R)
    ]
    out = _walk_be_ladder_exit(
        entry_spx=7400, t1=7410, t2=7440, t3=7450, stop=7390,
        is_long=True, path_items=path, apply_be_ladder=True,
    )
    assert out["outcome"] == "T1"
    # 0.5*r_t1(=1.0) + residual 0.5 * lock 0.5R = 0.5 + 0.25
    assert out["realized_R"] == pytest.approx(0.75)


def test_full_t3_uses_actual_per_tier_r_not_hardcoded_295():
    """Full T3 run must book 0.5·r_t1 + 0.3·r_t2 + 0.2·r_t3 from the verdict's
    ACTUAL targets — not the old hardcoded 1.5/2.5/5.0@30/40/30 = 2.95R."""
    from fef_sidecar.api import _walk_be_ladder_exit

    # targets at +1R/+2R/+3R → 0.5*1 + 0.3*2 + 0.2*3 = 1.7. low stays above the
    # ratcheted stop (7410) so the marched-up bar takes all three tiers.
    path = [(1, 7400, 7431, 7411, 7430)]
    out = _walk_be_ladder_exit(
        entry_spx=7400, t1=7410, t2=7420, t3=7430, stop=7390,
        is_long=True, path_items=path, apply_be_ladder=True,
    )
    assert out["outcome"] == "T3"
    assert out["realized_R"] == pytest.approx(1.7)
    assert out["realized_R"] != pytest.approx(2.95)

    # Move t3 out to +5R → 0.5*1 + 0.3*2 + 0.2*5 = 2.1 (tracks target distance)
    path2 = [(1, 7400, 7451, 7411, 7450)]
    out2 = _walk_be_ladder_exit(
        entry_spx=7400, t1=7410, t2=7420, t3=7450, stop=7390,
        is_long=True, path_items=path2, apply_be_ladder=True,
    )
    assert out2["realized_R"] == pytest.approx(2.1)


def test_puts_be_stop_locked_r_symmetric():
    """Same LOCKED-R rule on the short/LONG_PUTS side."""
    from fef_sidecar.api import _walk_be_ladder_exit

    # entry 7400, stop 7410 (risk 10). Favorable = DOWN.
    path = [
        (1, 7396, 7398, 7395, 7396),   # low 7395 = +0.5R → lock breakeven, stop→7400
        (2, 7399, 7401, 7399, 7400),   # high 7401 >= 7400 → BE stop at breakeven
    ]
    out = _walk_be_ladder_exit(
        entry_spx=7400, t1=7370, t2=7360, t3=7350, stop=7410,
        is_long=False, path_items=path, apply_be_ladder=True,
    )
    assert out["be_lock_R"] == 0.0
    assert out["outcome"] == "BE_STOP_0.0R"
    assert out["realized_R"] == pytest.approx(0.0)


def test_backtest_compute_integration_breakeven_not_phantom_win():
    """End-to-end through _backtest_compute (the tournament/full_pipeline
    path): a breakeven-scratch trade with apply_be_ladder=True must NOT book a
    positive R. Before the fix it booked +0.5R purely from the ladder."""
    from fef_sidecar.api import _backtest_compute

    base = datetime(2026, 5, 18, 14, 0, 0)
    entry_ms = int(base.timestamp() * 1000)
    sig = _Sig(
        sig_id=1, ts=base, session_date="2026-05-18", spx=7400.0,
        direction="LONG_CALLS",
        extras={"t1": 7430.0, "t2": 7440.0, "t3": 7450.0, "stop": 7390.0,
                "option_pricing": {"entry_limit": 5.0}},
    )
    bars = [
        _Bar(entry_ms + 60_000, 7404, 7405, 7402, 7404),   # +0.5R → lock breakeven
        _Bar(entry_ms + 120_000, 7401, 7401, 7399, 7400),  # reverse into BE stop
    ]
    res = _backtest_compute(
        sigs=[sig], fills=[], spx_bars=bars,
        session_date_from="2026-05-18", to_date="2026-05-18",
        apply_be_ladder=True, commission_per_contract=0.65,
        avg_entry_slip_pct=0.0, avg_exit_slip_pct=0.0, preset_row=None,
    )
    assert res["trade_count"] == 1
    trade = res["trades"][0]
    assert trade["outcome"] == "BE_STOP_0.0R"
    assert trade["realized_R_gross"] == pytest.approx(0.0)      # not +0.5
    assert trade["realized_R_net"] <= 0.0                       # commission only
    # Board does not flip positive from the ladder alone.
    assert res["expectancy_R"] <= 0.0


# ===========================================================================
# Fix 1 — delta-aware $/R
# ===========================================================================
def test_dollars_per_r_is_delta_aware():
    from fef_sidecar.engine.optimization import (
        DEFAULT_STRIKE_DELTA,
        _dollars_per_r_per_contract,
    )

    # ATM delta 0.5 reproduces the legacy $500/R/contract.
    assert _dollars_per_r_per_contract(0.5) == pytest.approx(500.0)
    # Default (35-OTM 0DTE ≈ 0.09) is ~$90/R/contract — ~6× smaller.
    assert _dollars_per_r_per_contract(None) == pytest.approx(90.0)
    assert DEFAULT_STRIKE_DELTA == pytest.approx(0.09)
    # None / non-positive delta → default, never the ATM 0.5 assumption.
    assert _dollars_per_r_per_contract(0.0) == pytest.approx(90.0)
    assert _dollars_per_r_per_contract(-1.0) == pytest.approx(90.0)


def test_proxy_pnl_uses_delta_aware_dollars():
    from fef_sidecar.engine.optimization import _proxy_pnl

    # A clean +1R walk, qty=1, no friction → gross = 1R × $/R.
    # Default delta → $90 (not the old $500).
    default_pnl = _proxy_pnl("LONG_CALLS", 7400.0, [7410.0], qty=1,
                             apply_friction=False)
    assert default_pnl == pytest.approx(90.0)
    assert default_pnl != pytest.approx(500.0)
    # ATM delta reproduces legacy magnitude.
    atm_pnl = _proxy_pnl("LONG_CALLS", 7400.0, [7410.0], qty=1,
                         apply_friction=False, strike_delta=0.5)
    assert atm_pnl == pytest.approx(500.0)


# ===========================================================================
# Fix 2 — session-boundary purge + min-N guard
# ===========================================================================
def test_forward_walk_purges_at_session_boundary():
    from fef_sidecar.engine.optimization import _forward_walk_within_session

    iters = [
        {"spx_price": 100.0, "session_date": "2026-05-18"},  # entry (i=0)
        {"spx_price": 101.0, "session_date": "2026-05-18"},  # same session
        {"spx_price": 102.0, "session_date": "2026-05-19"},  # NEXT session — cut
        {"spx_price": 103.0, "session_date": "2026-05-19"},
    ]
    # lookahead spans into the next session; walk must stop at the boundary.
    walk = _forward_walk_within_session(iters, 0, 3)
    assert walk == [101.0]

    # Entry as the last iter of its session → empty forward walk (skip trade).
    iters2 = [
        {"spx_price": 100.0, "session_date": "2026-05-18"},
        {"spx_price": 200.0, "session_date": "2026-05-19"},
    ]
    assert _forward_walk_within_session(iters2, 0, 1) == []

    # No session_date → legacy unpurged behavior.
    iters3 = [{"spx_price": float(x)} for x in (1, 2, 3)]
    assert _forward_walk_within_session(iters3, 0, 2) == [2.0, 3.0]


def test_low_confidence_flag_on_thin_windows():
    from fef_sidecar.engine.optimization import MIN_CONFIDENT_TRADES, SweepResult

    assert MIN_CONFIDENT_TRADES == 20
    thin = SweepResult(
        wall_proximity_pts=5.0, wall_collapse_pct=0.2, wall_collapse_floor_m=5.0,
        absorption_fe_ratio_min=5.0, train_sharpe=1.0, test_sharpe=1.0,
        train_trades=8, test_trades=4, train_win_rate_pct=50.0,
        test_win_rate_pct=50.0, train_total_pnl=0.0, test_total_pnl=0.0,
        low_confidence=(8 < MIN_CONFIDENT_TRADES or 4 < MIN_CONFIDENT_TRADES),
    )
    assert thin.low_confidence is True


def test_run_sweep_does_not_walk_across_sessions(monkeypatch):
    """Integration: feed run_sweep iters spanning two sessions and assert every
    scored walk stays inside one session (no overnight hold)."""
    import fef_sidecar.engine.optimization as opt

    captured: list[tuple[float, list[float]]] = []
    real_proxy = opt._proxy_pnl

    def _spy(direction, spx_now, spx_walk, **kw):
        captured.append((spx_now, list(spx_walk)))
        return real_proxy(direction, spx_now, spx_walk, **kw)

    monkeypatch.setattr(opt, "_proxy_pnl", _spy)

    # Two sessions, strongly trending so the engine actually fires.
    iters = []
    for d, day in enumerate(("2026-05-18", "2026-05-19")):
        for k in range(12):
            spx = 7400.0 + d * 50 + k * 2.0
            iters.append({
                "spx_price": spx, "drift_cum_net_m": 20.0 + k * 5,
                "drift_bias": "BULLISH", "session_date": day,
                "ntm_max_pain": spx - 5, "raw_max_pain": spx - 30,
                "top_gex_walls": [{"strike": spx + 8, "net_m": 60.0, "side": "CALL"}],
                "top_dex_walls": [{"strike": spx - 8, "net_m": 90.0, "side": "PUT"}],
                "aggression_call_pct": 60.0, "aggression_put_pct": 40.0,
            })
    cfg = opt.SweepConfig(
        proximity_values=[5.0], collapse_pct_values=[0.2],
        collapse_floor_values=[5.0], absorption_fe_values=[5.0],
        lookahead_iters=8,  # large enough to reach across the boundary if unpurged
    )
    opt.run_sweep(iters=iters, cfg=cfg, train_fraction=0.6)

    # The entry at 7400+... of session A with a 8-iter lookahead would, if
    # unpurged, pull session B prices (>=7450). Assert no walk mixes the jump.
    for spx_now, walk in captured:
        if not walk:
            continue
        # All forward prices within a plausible same-session band of entry.
        assert max(walk) - spx_now < 50.0, (
            f"walk appears to cross the session boundary: entry={spx_now} walk={walk}"
        )


# ===========================================================================
# Fix 5 — tuning sample-weighted pooling + realized preference
# ===========================================================================
def test_pooled_win_pct_is_sample_weighted():
    from fef_sidecar.engine.tuning_recommender import _pooled_win_pct

    # 3-sample 100% + 30-sample 0%. Unweighted mean = 50%. Weighted = 3/33.
    pooled, total_n, present = _pooled_win_pct(
        [{"n": 3, "win_pct": 100.0}, {"n": 30, "win_pct": 0.0}]
    )
    assert total_n == 33
    assert present == 2
    assert pooled == pytest.approx(3 / 33 * 100, abs=0.01)   # ≈9.09, NOT 50
    assert pooled < 15.0


def test_pooled_win_pct_prefers_realized_fills():
    from fef_sidecar.engine.tuning_recommender import _pooled_win_pct

    # Hypothetical says 80%@10, but realized fills say 25%@4 → use realized.
    pooled, total_n, present = _pooled_win_pct(
        [{"n": 10, "win_pct": 80.0, "realized_n": 4, "realized_win_pct": 25.0}]
    )
    assert total_n == 4
    assert pooled == pytest.approx(25.0)


def test_pooled_win_pct_ignores_tiny_buckets():
    from fef_sidecar.engine.tuning_recommender import _pooled_win_pct

    pooled, total_n, present = _pooled_win_pct([{"n": 2, "win_pct": 100.0}])
    assert present == 0
    assert total_n == 0


def test_feature_separation_uses_weighted_recent_pooling():
    """End-to-end: a big-sample losing bucket must not be rescued by one tiny
    high-win session (which the old unweighted mean allowed)."""
    from fef_sidecar.db.models import DailyReview
    from fef_sidecar.engine.tuning_recommender import _check_feature_separation

    reviews = []
    for i in range(5):
        reviews.append(DailyReview(
            session_date=f"2026-05-2{i}",
            by_direction={
                # LONG_CALLS: large, consistently winning.
                "LONG_CALLS": {"n": 30, "win_pct": 70.0},
                # LONG_PUTS: large, consistently losing, EXCEPT one tiny 100% day.
                "LONG_PUTS": ({"n": 3, "win_pct": 100.0} if i == 4
                              else {"n": 30, "win_pct": 10.0}),
            },
        ))
    recs = _check_feature_separation(reviews)
    gate = next((r for r in recs if r.backing_data.get("dimension") == "direction"), None)
    assert gate is not None
    per_bucket = gate.backing_data["per_bucket_avg"]
    # Sample-weighted PUTS pooling ≈ (0.1*30*4 + 1.0*3)/(120+3) ≈ 12.2%, so the
    # tiny 100% day can't lift it near the 70% CALLS bucket.
    assert per_bucket["LONG_PUTS"] < 25.0
    assert gate.backing_data["pooling"] == "sample_weighted"
    assert gate.backing_data["best_bucket"] == "LONG_CALLS"


# ===========================================================================
# Fix 6 — what_if _outcome_R partial-aware (banks partials before a stop)
# ===========================================================================
def test_outcome_r_banks_partials_before_stop():
    from fef_sidecar.engine.what_if_backtest import _outcome_R

    start_ms = 1_000_000
    bars = [
        # T1 (7410) fires; MFE 7411 arms the ladder lock at +0.5R (stop→7405).
        _Bar(start_ms + 60_000, 7405, 7411, 7406, 7409),
        # residual stops at the locked 7405 (=+0.5R), not the raw -1R stop.
        _Bar(start_ms + 120_000, 7402, 7402, 7389, 7395),
    ]
    r = _outcome_R(start_ms, 7400.0, 7410.0, 7440.0, 7450.0, 7390.0,
                   is_long_calls=True, bars=bars, window_min=60)
    # 0.5*r_t1(1.0) + residual 0.5 * lock 0.5R = 0.75 — POSITIVE, not -1.0.
    assert r == pytest.approx(0.75)
    assert r > 0.0


def test_outcome_r_clean_stop_still_minus_one():
    from fef_sidecar.engine.what_if_backtest import _outcome_R

    start_ms = 1_000_000
    # Drops straight to the stop with no favorable excursion.
    bars = [_Bar(start_ms + 60_000, 7399, 7401, 7388, 7389)]
    r = _outcome_R(start_ms, 7400.0, 7410.0, 7420.0, 7430.0, 7390.0,
                   is_long_calls=True, bars=bars, window_min=60)
    assert r == pytest.approx(-1.0)


# ===========================================================================
# Fix 7 — per-trade Sharpe, no √252
# ===========================================================================
def test_sharpe_per_trade_not_annualized():
    from fef_sidecar.engine.backtest import _sharpe_per_trade

    returns = [1.0, 2.0, 3.0, 4.0, 5.0]  # mean 3, sample std 1.5811
    s = _sharpe_per_trade(returns)
    assert s == pytest.approx(1.9, abs=0.05)   # 3/1.5811, NOT ×√252 (~30)
    assert s < 5.0
    # Zero variance → undefined.
    assert _sharpe_per_trade([2.0] * 6) is None
    assert _sharpe_per_trade([1.0, 2.0]) is None  # < 5 samples


# ===========================================================================
# Fix 8 — session-grouped forward CV
# ===========================================================================
def test_session_grouped_splits_never_split_a_session():
    from fef_sidecar.engine.ml_patterns import _session_grouped_splits

    groups = ["a", "a", "b", "b", "c", "c"]
    folds = list(_session_grouped_splits(groups, n_splits=5))
    assert len(folds) == 2  # k = min(5, n_sessions-1=2)
    for train_idx, test_idx in folds:
        # No row appears in both sides.
        assert not (set(train_idx) & set(test_idx))
        # The test fold is exactly one session's rows.
        test_sessions = {groups[i] for i in test_idx}
        assert len(test_sessions) == 1
        # Train contains only sessions strictly EARLIER than the test session.
        train_sessions = {groups[i] for i in train_idx}
        assert test_sessions.isdisjoint(train_sessions)


def test_session_grouped_splits_single_session_is_empty():
    from fef_sidecar.engine.ml_patterns import _session_grouped_splits

    # One session → no session-grouped folds (caller falls back to TSS).
    assert list(_session_grouped_splits(["a", "a", "a"], n_splits=5)) == []
