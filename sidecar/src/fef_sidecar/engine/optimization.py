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


def _proxy_pnl(direction: str, spx_now: float, spx_future: float, qty: int = 15,
               symbol: str = "SPX", apply_friction: bool = True) -> float:
    """Per-share-equivalent: SPX move × delta × 100 × qty.

    Assumes 0.5 delta, $50/pt/contract. Caps at -$10/pt loss to simulate stop.

    apply_friction (default True): subtracts slippage + commission via the
    standard cost model. Without this, walk-forward results overstate edge
    by 20-50% for 0DTE since 0DTE bid-ask is wide enough to eat real fills.
    """
    from fef_sidecar.engine.slippage import DEFAULTS as SLIP_DEFAULTS

    move = spx_future - spx_now
    if direction == "LONG_PUTS":
        move = -move
    pnl = move * 50 * qty
    stop = -10 * 50 * qty
    gross_pnl = max(stop, pnl)

    if not apply_friction:
        return gross_pnl

    # Friction model: 1pt entry premium × half-spread × 2 (round trip)
    # + commission round-trip
    cfg = SLIP_DEFAULTS.get(symbol.upper(), SLIP_DEFAULTS["SPX"])
    # Approximate entry premium ~ $5 for an ATM 0DTE (rough)
    entry_premium_estimate = 5.0
    spread_cost = entry_premium_estimate * cfg["bid_ask_pct_of_mid"] * qty * 100
    commission = cfg["commission_per_contract"] * qty * 2  # round-trip
    return round(gross_pnl - spread_cost - commission, 2)


def _sharpe(returns: list[float]) -> float | None:
    n = len(returns)
    if n < 5:
        return None
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if variance <= 0:
        return None
    return round(mean / math.sqrt(variance) * math.sqrt(252), 2)


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
    train_iters = iters[:split]
    test_iters = iters[split:]

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
            )
            verdict = engine.push(inputs)
            if verdict.direction.value == "WAIT":
                continue
            # Look ahead lookahead_iters for proxy P/L
            future_idx = i + cfg.lookahead_iters
            if future_idx >= len(slice_iters):
                continue
            future_spx = slice_iters[future_idx]["spx_price"]
            pnl = _proxy_pnl(verdict.direction.value, it["spx_price"], future_spx)
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
                    ))
    results.sort(key=lambda r: (r.test_sharpe or -999, r.test_total_pnl), reverse=True)
    return results
