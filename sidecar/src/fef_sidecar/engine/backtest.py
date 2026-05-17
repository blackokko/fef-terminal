"""Pattern backtesting against historical SignalIter rows.

For each pattern, walk every iter in a date window, evaluate the pattern's
conditions against the stored field values, and aggregate match stats.
When matched iters have linked TradeTicket outcomes, also compute P/L
distribution.

Statistical add-ons:
- Wilson score binomial confidence interval on win rate (95%)
- Sharpe ratio on per-trade returns (annualized assuming ~252 trading days)
- Max drawdown on the cumulative P/L curve
- T-test vs zero (is win rate statistically different from coinflip?)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class TradeOutcome:
    iter_id: int
    iter_num: int
    session_date: str
    pnl_dollars: float
    won: bool


@dataclass
class BacktestResult:
    pattern_id: int
    pattern_name: str
    window_days: int
    total_iters_scanned: int
    total_matches: int
    matches_with_trades: int
    matches_per_day: float

    # Win/loss
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    win_rate_ci_low: float = 0.0   # Wilson 95% lower bound
    win_rate_ci_high: float = 0.0  # Wilson 95% upper bound

    # P/L
    total_pnl: float = 0.0
    avg_pnl_per_trade: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float | None = None   # annualized, None if insufficient samples
    t_stat_vs_zero: float | None = None  # t-statistic for mean_pnl ≠ 0

    # Time series (for chart)
    cumulative_pnl_curve: list[dict[str, float]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.cumulative_pnl_curve is None:
            self.cumulative_pnl_curve = []


def _wilson_ci(wins: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score binomial CI — better than normal approx at small N or extreme p."""
    if total == 0:
        return 0.0, 0.0
    p = wins / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, (center - half) * 100), min(100.0, (center + half) * 100)


def _max_drawdown(curve: list[float]) -> float:
    if not curve:
        return 0.0
    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _sharpe_annualized(returns: list[float], periods_per_year: int = 252) -> float | None:
    """Daily Sharpe annualized. None if insufficient samples or zero stdev."""
    n = len(returns)
    if n < 5:
        return None
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if variance <= 0:
        return None
    std = math.sqrt(variance)
    return round((mean / std) * math.sqrt(periods_per_year), 2)


def _t_stat(returns: list[float]) -> float | None:
    """One-sample t-statistic for H0: mean = 0."""
    n = len(returns)
    if n < 5:
        return None
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if variance <= 0:
        return None
    se = math.sqrt(variance / n)
    return round(mean / se, 2)


def aggregate_backtest(
    pattern_id: int,
    pattern_name: str,
    window_days: int,
    total_iters_scanned: int,
    matches: list[int],  # iter ids that matched
    outcomes: list[TradeOutcome],
    unique_dates: int,
) -> BacktestResult:
    result = BacktestResult(
        pattern_id=pattern_id,
        pattern_name=pattern_name,
        window_days=window_days,
        total_iters_scanned=total_iters_scanned,
        total_matches=len(matches),
        matches_with_trades=len(outcomes),
        matches_per_day=round(len(matches) / max(1, unique_dates), 2),
    )

    if not outcomes:
        return result

    pnls = [o.pnl_dollars for o in outcomes]
    result.wins = sum(1 for o in outcomes if o.won)
    result.losses = sum(1 for o in outcomes if not o.won)
    result.win_rate_pct = round((result.wins / len(outcomes)) * 100, 1)
    low, high = _wilson_ci(result.wins, len(outcomes))
    result.win_rate_ci_low = round(low, 1)
    result.win_rate_ci_high = round(high, 1)

    result.total_pnl = round(sum(pnls), 2)
    result.avg_pnl_per_trade = round(result.total_pnl / len(outcomes), 2)
    result.best_trade = round(max(pnls), 2)
    result.worst_trade = round(min(pnls), 2)

    # Build cumulative curve sorted by iter num + compute drawdown
    sorted_outcomes = sorted(outcomes, key=lambda o: (o.session_date, o.iter_num))
    cum = 0.0
    curve_points = []
    for o in sorted_outcomes:
        cum += o.pnl_dollars
        curve_points.append({
            "iter_num": o.iter_num,
            "session_date": o.session_date,
            "cumulative_pnl": round(cum, 2),
        })
    result.cumulative_pnl_curve = curve_points
    result.max_drawdown = round(_max_drawdown([p["cumulative_pnl"] for p in curve_points]), 2)
    result.sharpe_ratio = _sharpe_annualized(pnls)
    result.t_stat_vs_zero = _t_stat(pnls)

    return result
