"""Slippage + commission model — applied to backtest + walk-forward results.

Without these, every simulated trade fills at mid — overstates edge by
20-50% for 0DTE because:
  - 0DTE bid-ask is often $0.10-0.30 wide (1-3% of premium)
  - Commission is $0.65/contract on SPX, $0.04 on SPY, $0.005 on stocks
  - Aggressive entries cross the spread; passive entries don't fill at all
  - Late-day exits hit gappier markets

Defaults are tuned for SPX 0DTE practitioner reality. Override per-symbol
via the constructor.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FillResult:
    realized_price: float
    slippage_dollars: float
    commission_dollars: float
    net_cost_dollars: float       # what actually came out of the account
    breakdown: str


# Per-symbol cost defaults — commission per contract + typical bid-ask % of mid
DEFAULTS = {
    "SPX": {"commission_per_contract": 0.65, "bid_ask_pct_of_mid": 0.025},
    "SPY": {"commission_per_contract": 0.65, "bid_ask_pct_of_mid": 0.010},
    "QQQ": {"commission_per_contract": 0.65, "bid_ask_pct_of_mid": 0.012},
    "IWM": {"commission_per_contract": 0.65, "bid_ask_pct_of_mid": 0.020},
    "ES":  {"commission_per_contract": 2.25, "bid_ask_pct_of_mid": 0.005},
    "NQ":  {"commission_per_contract": 2.25, "bid_ask_pct_of_mid": 0.005},
}


def fill_buy(
    mid_price: float,
    qty: int,
    *,
    symbol: str = "SPX",
    aggressive: bool = True,
    spread_pct_override: float | None = None,
) -> FillResult:
    """Simulate a BUY fill — aggressive = cross the ask (full slippage),
    passive = fills at mid (no slippage). Most chasers are aggressive."""
    cfg = DEFAULTS.get(symbol.upper(), DEFAULTS["SPX"])
    spread_pct = spread_pct_override if spread_pct_override is not None else cfg["bid_ask_pct_of_mid"]
    half_spread = mid_price * spread_pct / 2

    realized = mid_price + half_spread if aggressive else mid_price
    slip = (realized - mid_price) * qty * 100  # 100 multiplier per options contract
    commission = qty * cfg["commission_per_contract"]
    net = realized * qty * 100 + commission
    return FillResult(
        realized_price=round(realized, 2),
        slippage_dollars=round(slip, 2),
        commission_dollars=round(commission, 2),
        net_cost_dollars=round(net, 2),
        breakdown=f"mid ${mid_price:.2f} {'+' if aggressive else '='} half-spread ${half_spread:.2f} × {qty} contracts + ${commission:.2f} commission",
    )


def fill_sell(
    mid_price: float,
    qty: int,
    *,
    symbol: str = "SPX",
    aggressive: bool = True,
    spread_pct_override: float | None = None,
) -> FillResult:
    """Simulate a SELL fill — aggressive = cross the bid (full slippage)."""
    cfg = DEFAULTS.get(symbol.upper(), DEFAULTS["SPX"])
    spread_pct = spread_pct_override if spread_pct_override is not None else cfg["bid_ask_pct_of_mid"]
    half_spread = mid_price * spread_pct / 2

    realized = mid_price - half_spread if aggressive else mid_price
    slip = (mid_price - realized) * qty * 100
    commission = qty * cfg["commission_per_contract"]
    net = realized * qty * 100 - commission
    return FillResult(
        realized_price=round(realized, 2),
        slippage_dollars=round(slip, 2),
        commission_dollars=round(commission, 2),
        net_cost_dollars=round(net, 2),
        breakdown=f"mid ${mid_price:.2f} {'-' if aggressive else '='} half-spread ${half_spread:.2f} × {qty} contracts - ${commission:.2f} commission",
    )


def round_trip_cost(
    entry_mid: float,
    exit_mid: float,
    qty: int,
    *,
    symbol: str = "SPX",
    aggressive_entry: bool = True,
    aggressive_exit: bool = True,
) -> dict:
    """Net P/L of a round-trip including slippage + commission both legs.
    Use this in backtests instead of (exit - entry) × qty × 100."""
    buy = fill_buy(entry_mid, qty, symbol=symbol, aggressive=aggressive_entry)
    sell = fill_sell(exit_mid, qty, symbol=symbol, aggressive=aggressive_exit)
    gross = (exit_mid - entry_mid) * qty * 100
    net = sell.net_cost_dollars - buy.net_cost_dollars
    total_slip = buy.slippage_dollars + sell.slippage_dollars
    total_comm = buy.commission_dollars + sell.commission_dollars

    return {
        "gross_pnl": round(gross, 2),
        "net_pnl": round(net, 2),
        "slippage_total": round(total_slip, 2),
        "commission_total": round(total_comm, 2),
        "friction_pct_of_gross": round((total_slip + total_comm) / max(abs(gross), 1) * 100, 2),
        "entry_fill": buy.realized_price,
        "exit_fill": sell.realized_price,
    }
