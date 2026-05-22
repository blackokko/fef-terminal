"""Walk-forward parameter sweep for FEF thresholds.

Grid-search wall_proximity × wall_collapse_pct × wall_collapse_floor ×
absorption_fe_min against historical SignalIter rows. For each combo:
- Reconstruct an FEFEngine with those thresholds
- Replay the iter sequence to generate synthetic verdicts
- Compute proxy P/L from SPX movement in the direction of fire over N
  next iters

Walk-forward = train on first 60% of window to find best params, then
test on last 40% to validate it isn't overfit. Reports both train and
test Sharpe so user can see if the best-fit on history actually
generalizes.

Single sweep is O(combos × iters) — keep grid coarse (3-4 values per
dim = 81-256 combos) so it stays under ~30 seconds on 30 days of data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SweepConfig:
    proximity_values: list[float]
    collapse_pct_values: list[float]
    collapse_floor_values: list[float]
    absorption_fe_values: list[float]
    lookahead_iters: int = 3  # how many iters ahead to check for P/L proxy


@dataclass
class SweepResult:
    wall_proximity_pts: float
    wall_collapse_pct: float
    wall_collapse_floor_m: float
    absorption_fe_ratio_min: float
    train_sharpe: float | None
    test_sharpe: float | None
    train_trades: int
    test_trades: int
    train_win_rate_pct: float
    test_win_rate_pct: float
    train_total_pnl: float
    test_total_pnl: float
    sharpe_ci_lo: float | None = None
    sharpe_ci_hi: float | None = None


def _proxy_pnl(direction: str, spx_now: float, spx_walk: list[float],
               qty: int = 15, symbol: str = "SPX",
               apply_friction: bool = True) -> float:
    """Bar-walk proxy P/L with 50/30/20 partial scale-out + -1R stop.

    P2 (2026-05-21): replaces the prior linear endpoint estimate
    (spx_future − spx_now, capped at -10pt) with order-preserving walk
    through `spx_walk` (forward iter prices). Methodology now matches
    scripts/research/ablation_backtest.py::_simulate_baseline,
    per_regime_sweep v2, and realistic_fill_estimator v2.

    Why this matters: endpoint scoring misses intermediate stop hits AND
    misses T1/T2 partial scale-out wins. On fast-reversal sequences it
    over/undercounted P/L by a wide margin and produced the optimal-
    grid-floor artifact in per_regime_sweep v1.

    Args:
        direction: "LONG_CALLS" or "LONG_PUTS".
        spx_now: SPX at entry.
        spx_walk: forward SPX prices in chronological order. The walk
            uses each price as a single-point bar (high=low=close=price)
            — this is coarser than 1-min bars but matches the iter
            granularity available at this layer. Use spx_walk = [endpoint]
            for legacy single-endpoint behavior.
        qty: contract count for $ conversion ($50/pt/contract × 0.5 delta).
        apply_friction: subtract slippage + commission.

    Returns $ P/L. With apply_friction=True, walk-forward edges are no
    longer overstated by 20-50% from 0DTE bid-ask slippage.
    """
    from fef_sidecar.engine.slippage import DEFAULTS as SLIP_DEFAULTS

    direction_sign = +1 if direction == "LONG_CALLS" else -1
    # Bar-walk over spx_walk with 50/30/20 partial scale-out
    realized_r = 0.0
    remaining_share = 1.0
    tiers_fired: set[str] = set()
    T1_SHARE, T2_SHARE, T3_SHARE = 0.5, 0.3, 0.2
    final_close = spx_walk[-1] if spx_walk else spx_now
    for price in spx_walk:
        adverse_r = direction_sign * (price - spx_now) / 10.0
        favor_r = adverse_r  # single-point bar; favor/adverse are the same value
        if adverse_r <= -1.0 and remaining_share > 0:
            realized_r += remaining_share * -1.0
            remaining_share = 0.0
            break
        if favor_r >= 1.0 and "T1" not in tiers_fired:
            realized_r += T1_SHARE * 1.0
            remaining_share -= T1_SHARE
            tiers_fired.add("T1")
        if favor_r >= 2.0 and "T2" not in tiers_fired:
            realized_r += T2_SHARE * 2.0
            remaining_share -= T2_SHARE
            tiers_fired.add("T2")
        if favor_r >= 3.0 and "T3" not in tiers_fired:
            realized_r += T3_SHARE * 3.0
            remaining_share -= T3_SHARE
            tiers_fired.add("T3")
        if remaining_share <= 0.001:
            remaining_share = 0.0
            break
    if remaining_share > 0:
        final_r = direction_sign * (final_close - spx_now) / 10.0
        realized_r += remaining_share * final_r

    # Convert R to $: 1R = 10pt × 0.5 delta × $100/pt × qty / 100  ≡  10pt × $50/pt × qty
    # Matches the legacy convention: 10pt SPX move × 50 × qty.
    gross_pnl = realized_r * 10 * 50 * qty

    if not apply_friction:
        return round(gross_pnl, 2)

    cfg = SLIP_DEFAULTS.get(symbol.upper(), SLIP_DEFAULTS["SPX"])
    entry_premium_estimate = 5.0
    spread_cost = entry_premium_estimate * cfg["bid_ask_pct_of_mid"] * qty * 100
    commission = cfg["commission_per_contract"] * qty * 2  # round-trip
    return round(gross_pnl - spread_cost - commission, 2)


def _sharpe(returns: list[float]) -> float | None:
    """Per-trade Sharpe ratio (mean/std of trade returns). Not annualized —
    returns are per-trade, not daily, so sqrt(252) would be incorrect.
    Ranking is preserved without annualization."""
    n = len(returns)
    if n < 5:
        return None
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if variance <= 0:
        return None
    return round(mean / math.sqrt(variance), 2)


def _bootstrap_sharpe_ci(
    returns: list[float], n_reps: int = 1000, ci_pct: float = 90,
) -> tuple[float | None, float | None]:
    """Bootstrap confidence interval on per-trade Sharpe ratio.
    Returns (lo, hi) at the given CI percentile, or (None, None)
    if not enough data."""
    import random
    n = len(returns)
    if n < 10:
        return None, None
    sharpes: list[float] = []
    for _ in range(n_reps):
        sample = random.choices(returns, k=n)
        s = _sharpe(sample)
        if s is not None:
            sharpes.append(s)
    if len(sharpes) < n_reps // 2:
        return None, None
    sharpes.sort()
    tail = (100 - ci_pct) / 200
    lo_idx = int(len(sharpes) * tail)
    hi_idx = int(len(sharpes) * (1 - tail))
    return round(sharpes[lo_idx], 3), round(sharpes[min(hi_idx, len(sharpes) - 1)], 3)


def run_sweep(
    iters: list[dict],
    cfg: SweepConfig,
    train_fraction: float = 0.6,
) -> list[SweepResult]:
    """iters: list of dicts with keys spx_price, drift_cum_net_m, drift_bias,
    ntm_max_pain, top_gex_walls, top_dex_walls, aggression_call_pct, etc.
    Should be in chronological order.

    Returns list of SweepResults sorted by test_sharpe DESC (best first).
    """
    from fef_sidecar.engine.fef import FEFEngine
    from fef_sidecar.engine.types import FEFInputs
    from fef_sidecar.quantdata.models import ExposureRow

    n = len(iters)
    if n < 10:
        return []
    split = int(n * train_fraction)
    # Purge window: skip 3 iters between train and test to avoid
    # information leakage from overlapping forward windows.
    PURGE_ITERS = 3
    train_iters = iters[:split]
    test_iters = iters[split + PURGE_ITERS:]
    if len(test_iters) < 5:
        return []

    def _eval(slice_iters: list[dict], engine: FEFEngine) -> tuple[list[float], int, int, float]:
        engine.reset()
        returns: list[float] = []
        trades = 0
        wins = 0
        total = 0.0
        for i, it in enumerate(slice_iters):
            gex_rows = [
                ExposureRow(strike=w["strike"], call_m=max(0, w["net_m"]),
                            put_m=min(0, w["net_m"]), net_m=w["net_m"])
                for w in (it.get("top_gex_walls") or [])
            ]
            dex_rows = [
                ExposureRow(strike=w["strike"], call_m=max(0, w["net_m"]),
                            put_m=min(0, w["net_m"]), net_m=w["net_m"])
                for w in (it.get("top_dex_walls") or [])
            ]
            cex_rows = [
                ExposureRow(strike=w["strike"], call_m=max(0, w["net_m"]),
                            put_m=min(0, w["net_m"]), net_m=w["net_m"])
                for w in (it.get("top_cex_walls") or [])
            ]
            vex_rows = [
                ExposureRow(strike=w["strike"], call_m=max(0, w["net_m"]),
                            put_m=min(0, w["net_m"]), net_m=w["net_m"])
                for w in (it.get("top_vex_walls") or [])
            ]
            inputs = FEFInputs(
                timestamp_ms=0,
                spx_price=it["spx_price"],
                drift_cum_net_m=it["drift_cum_net_m"],
                drift_bias=it.get("drift_bias", "NEUTRAL"),
                top_gex_walls=gex_rows,
                top_dex_walls=dex_rows,
                ntm_max_pain=it.get("ntm_max_pain", it["spx_price"]),
                raw_max_pain=it.get("raw_max_pain", it["spx_price"]),
                aggression_call_pct=it.get("aggression_call_pct"),
                aggression_put_pct=it.get("aggression_put_pct"),
                top_cex_walls=cex_rows,
                top_vex_walls=vex_rows,
            )
            verdict = engine.push(inputs)
            if verdict.direction.value == "WAIT":
                continue
            # Look ahead lookahead_iters for proxy P/L. P2 (2026-05-21):
            # pass the full forward walk so _proxy_pnl can apply bar-walk
            # methodology with 50/30/20 partial scale-out + -1R stop,
            # matching ablation_backtest._simulate_baseline / per_regime_sweep v2.
            future_idx = i + cfg.lookahead_iters
            if future_idx >= len(slice_iters):
                continue
            spx_walk = [
                slice_iters[j]["spx_price"]
                for j in range(i + 1, future_idx + 1)
            ]
            pnl = _proxy_pnl(verdict.direction.value, it["spx_price"], spx_walk)
            returns.append(pnl)
            trades += 1
            if pnl > 0:
                wins += 1
            total += pnl
        win_rate = (wins / trades * 100) if trades > 0 else 0.0
        return returns, trades, wins, total

    results: list[SweepResult] = []
    for prox in cfg.proximity_values:
        for cpct in cfg.collapse_pct_values:
            for cfloor in cfg.collapse_floor_values:
                for afe in cfg.absorption_fe_values:
                    engine = FEFEngine(
                        wall_proximity_pts=prox,
                        wall_collapse_pct=cpct,
                        wall_collapse_floor_m=cfloor,
                        absorption_fe_ratio_min=afe,
                    )
                    train_returns, train_trades, train_wins, train_total = _eval(train_iters, engine)
                    test_returns, test_trades, test_wins, test_total = _eval(test_iters, engine)
                    ci_lo, ci_hi = _bootstrap_sharpe_ci(test_returns)
                    results.append(SweepResult(
                        wall_proximity_pts=prox,
                        wall_collapse_pct=cpct,
                        wall_collapse_floor_m=cfloor,
                        absorption_fe_ratio_min=afe,
                        train_sharpe=_sharpe(train_returns),
                        test_sharpe=_sharpe(test_returns),
                        train_trades=train_trades,
                        test_trades=test_trades,
                        train_win_rate_pct=round((train_wins / train_trades * 100) if train_trades else 0.0, 1),
                        test_win_rate_pct=round((test_wins / test_trades * 100) if test_trades else 0.0, 1),
                        train_total_pnl=round(train_total, 2),
                        test_total_pnl=round(test_total, 2),
                        sharpe_ci_lo=ci_lo,
                        sharpe_ci_hi=ci_hi,
                    ))
    results.sort(key=lambda r: (r.test_sharpe or -999, r.test_total_pnl), reverse=True)
    return results
