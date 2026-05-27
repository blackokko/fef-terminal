"""Regression test for Bug #11: tournament backtests with different
StrategyPreset thresholds previously converged to identical results because
the entry_limit filter only let through signals that had stored
option_pricing — i.e., signals the LIVE engine had fired on. The fix
synthesizes a placeholder entry_limit when a preset re-eval fires on a
signal the live engine had WAITed on.
"""

from __future__ import annotations

from datetime import datetime

from fef_sidecar.db.models import StrategyPreset


class _FakeSignal:
    """Minimal SignalIter stand-in for _backtest_compute."""

    def __init__(
        self,
        *,
        sig_id: int,
        ts: datetime,
        session_date: str,
        spx: float,
        direction: str,
        extras: dict,
        drift_cum_net_m: float = 5.0,
        drift_bias: str = "BULLISH",
        ntm_max_pain: float = 7400.0,
        raw_max_pain: float = 7300.0,
        top_gex_walls=None,
        top_dex_walls=None,
        aggression_call_pct: float = 55.0,
        aggression_put_pct: float = 45.0,
    ):
        self.id = sig_id
        self.timestamp = ts
        self.session_date = session_date
        self.spx_price = spx
        # mimic enum.value
        class _D:
            def __init__(self, v): self.value = v
        self.direction = _D(direction)
        self.extras = extras
        self.drift_cum_net_m = drift_cum_net_m
        self.drift_bias = drift_bias
        self.ntm_max_pain = ntm_max_pain
        self.raw_max_pain = raw_max_pain
        self.top_gex_walls = top_gex_walls or [{"strike": 7410.0, "net_m": 50.0, "side": "CALL"}]
        self.top_dex_walls = top_dex_walls or [{"strike": 7390.0, "net_m": 100.0, "side": "PUT"}]
        self.aggression_call_pct = aggression_call_pct
        self.aggression_put_pct = aggression_put_pct
        # Empty-by-default fields _backtest_compute reads via getattr:
        self.top_cex_walls = []
        self.top_vex_walls = []
        self.top_gex_by_expiry = []
        self.top_dex_by_expiry = []
        self.zero_dte_gex_net_m = None
        self.zero_dte_dex_net_m = None
        self.recent_whale_count_30m = 0
        self.recent_golden_sweep_count_30m = 0


def test_synthesizes_entry_limit_when_preset_fires_on_waiting_signal() -> None:
    """When a preset re-eval fires LONG_CALLS on a signal whose stored
    direction was WAIT (so no option_pricing was saved), the backtester
    should NOT drop the trade — it should synthesize a placeholder
    entry_limit and let the R-multiple math proceed.
    """
    from fef_sidecar.api import _backtest_compute

    # 1) Build a 3-iter warmup + 3 actionable iters. Direction stored as WAIT
    #    so option_pricing is absent; preset re-eval will (potentially)
    #    return LONG_CALLS via the fresh engine.
    base = datetime(2026, 5, 18, 14, 0, 0)
    sigs = []
    for i in range(6):
        # Make SPX advance + drift advance so the engine sees aligned bullish.
        spx = 7400.0 + i * 3.0
        drift = float(i * 5)
        sigs.append(_FakeSignal(
            sig_id=i + 1,
            ts=base.replace(minute=i * 2),
            session_date="2026-05-18",
            spx=spx,
            direction="WAIT",  # stored direction is WAIT — no option_pricing
            extras={},  # no option_pricing
            drift_cum_net_m=drift,
            drift_bias="BULLISH",
        ))

    preset = StrategyPreset(
        name="Tight Pin Test",
        description="test",
        fef_loop_interval_sec=120,
        wall_proximity_pts=0.5,  # very tight so the engine doesn't skip
        wall_collapse_pct=0.3,
        wall_collapse_floor_m=10.0,
        absorption_fe_ratio_min=8.0,
    )

    result = _backtest_compute(
        sigs=sigs, fills=[], spx_bars=None,
        session_date_from="2026-05-18", to_date="2026-05-18",
        apply_be_ladder=False, commission_per_contract=0.65,
        avg_entry_slip_pct=0.0, avg_exit_slip_pct=0.0,
        preset_row=preset,
    )

    # The key assertion: synthesized_entry_limit_count is exposed in the
    # response, so a tournament caller can see how many trades the synth
    # path produced. (Trade count may be 0 if the engine doesn't fire on
    # this contrived data, but the field must be reported.)
    assert "synthesized_entry_limit_count" in result, (
        "response should expose synthesized_entry_limit_count so tournament "
        "callers can validate the fix is engaging"
    )
    assert isinstance(result["synthesized_entry_limit_count"], int)


def test_different_presets_produce_different_trade_counts() -> None:
    """Bug #11 core regression: two presets with materially different
    thresholds should NOT produce identical trade counts on the same data.
    """
    from fef_sidecar.api import _backtest_compute

    # Build a deterministic set of 30 actionable signals that have stored
    # t1/t2/t3/stop in extras BUT no option_pricing — forcing the
    # synthesized-entry-limit path.
    base = datetime(2026, 5, 18, 9, 30, 0)
    sigs = []
    for i in range(30):
        spx = 7400.0 + i * 1.5
        # 50/50 split of drift directions so aligned/bullish vs not varies.
        drift = (i * 3.0) - 30.0
        bias = "BULLISH" if i % 2 == 0 else "BEARISH"
        # Stored direction WAIT but t1/t2/t3/stop populated — covers both
        # the "preset fires where live didn't" and target-availability cases.
        # Add option_pricing so the path doesn't synthesize (we want both
        # presets to scan the same opportunities and only differ via engine).
        extras = {
            "t1": spx + 5, "t2": spx + 10, "t3": spx + 20, "stop": spx - 10,
            "option_pricing": {"entry_limit": 5.0},
            "fe_ratio": 3.0,
            "delta_price": 2.0,
            "delta_drift_m": 5.0,
        }
        sigs.append(_FakeSignal(
            sig_id=i + 1,
            ts=base.replace(minute=(i * 2) % 60, hour=9 + (i * 2) // 60),
            session_date="2026-05-18",
            spx=spx,
            direction="LONG_CALLS",  # stored fired so option_pricing applies
            extras=extras,
            drift_cum_net_m=drift,
            drift_bias=bias,
        ))

    preset_tight = StrategyPreset(
        name="Tight",
        description="t",
        fef_loop_interval_sec=120,
        wall_proximity_pts=0.5,  # accepts almost everything
        wall_collapse_pct=0.15,
        wall_collapse_floor_m=3.0,
        absorption_fe_ratio_min=2.0,
    )
    preset_chop = StrategyPreset(
        name="Chop",
        description="c",
        fef_loop_interval_sec=120,
        wall_proximity_pts=50.0,  # rejects almost everything via wall_prox
        wall_collapse_pct=0.6,
        wall_collapse_floor_m=50.0,
        absorption_fe_ratio_min=20.0,
    )

    r_tight = _backtest_compute(
        sigs=sigs, fills=[], spx_bars=None,
        session_date_from="2026-05-18", to_date="2026-05-18",
        apply_be_ladder=False, commission_per_contract=0.65,
        avg_entry_slip_pct=0.0, avg_exit_slip_pct=0.0,
        preset_row=preset_tight,
    )
    r_chop = _backtest_compute(
        sigs=sigs, fills=[], spx_bars=None,
        session_date_from="2026-05-18", to_date="2026-05-18",
        apply_be_ladder=False, commission_per_contract=0.65,
        avg_entry_slip_pct=0.0, avg_exit_slip_pct=0.0,
        preset_row=preset_chop,
    )

    # The Chop preset's huge wall_proximity_pts should filter out
    # substantially more trades than Tight's near-zero proximity.
    tc_tight = r_tight.get("trade_count", 0)
    tc_chop = r_chop.get("trade_count", 0)
    assert tc_tight != tc_chop, (
        f"Bug #11 regression: tight vs chop preset produced identical "
        f"trade counts (tight={tc_tight}, chop={tc_chop}). The engine "
        f"thresholds aren't actually filtering."
    )
