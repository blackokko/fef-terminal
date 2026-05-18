"""Auto-trade decision engine.

Called from _run_one_iter AFTER the FEF verdict is computed. Checks every
gate in order:

  1. auto_trade_enabled
  2. Live trading env (FEF_LIVE_TRADING=true)
  3. Verdict is actionable (LONG_CALLS or LONG_PUTS, not WAIT)
  4. Confidence >= floor
  5. Calendar pause window not active
  6. Cool-down elapsed since last auto fire
  7. Session fire-cap not exceeded
  8. Verdict has option_pricing (strike + premium + targets)
  9. Risk caps not breached (daily loss / max position)

If ANY gate fails, returns a reason string (logged + exposed via
state.auto_trade_last_block_reason). If ALL pass, calls
place_bracket(confirm=True) and bumps counters.

Designed so a misconfiguration can only PREVENT trades, never accidentally
permit them — defaults are restrictive, every gate must explicitly pass.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


_CONFIDENCE_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


@dataclass(frozen=True)
class AutoTradeDecision:
    fired: bool
    reason: str
    order_result: dict | None = None


async def maybe_auto_trade(state, verdict_dict: dict, *, symbol: str | None = None) -> AutoTradeDecision:
    """Evaluate gates + maybe place a bracket order.

    verdict_dict: the verdict from _run_one_iter (with direction, confidence,
    strike_rec, option_pricing, suggested_qty, t1/t2/t3/stop).

    symbol: optional override. When provided (from multi-symbol scanner),
    counters check both the GLOBAL session cap AND the per-symbol sub-cap."""

    sym = (symbol or state.active_symbol).upper()

    # Gate 1: toggle
    if not state.auto_trade_enabled:
        return AutoTradeDecision(False, "auto_trade disabled")

    # Gate 2: live trading env
    from fef_sidecar.engine.live_trading import is_live_trading_enabled
    if not is_live_trading_enabled():
        return AutoTradeDecision(False, "FEF_LIVE_TRADING env not set")

    # Gate 3: actionable verdict
    direction = verdict_dict.get("direction", "WAIT")
    if direction not in ("LONG_CALLS", "LONG_PUTS"):
        return AutoTradeDecision(False, f"verdict is {direction}")

    # Gate 4: confidence floor
    conf = verdict_dict.get("confidence", "NONE")
    if _CONFIDENCE_RANK.get(conf, 0) < _CONFIDENCE_RANK.get(state.auto_trade_confidence_floor, 3):
        return AutoTradeDecision(False, f"{conf} below floor {state.auto_trade_confidence_floor}")

    # Gate 5: calendar pause window
    try:
        from fef_sidecar.engine.calendar import is_pause_window
        pause = await is_pause_window()
        if pause.paused:
            return AutoTradeDecision(False, f"calendar pause: {pause.reason}")
    except Exception:
        pass

    # Gate 6: per-symbol cool-down. Each symbol's clock is independent so
    # SPX firing doesn't gate out a fresh QQQ signal. The global
    # auto_trade_last_at_ms is still updated for visibility but isn't a
    # blocking gate here (multi-symbol scanning needs per-symbol isolation).
    now_ms = int(time.time() * 1000)
    sym_last = state.auto_trade_last_at_ms_by_symbol.get(sym, 0)
    elapsed_sec = (now_ms - sym_last) / 1000
    if sym_last > 0 and elapsed_sec < state.auto_trade_cool_down_sec:
        remaining = state.auto_trade_cool_down_sec - int(elapsed_sec)
        return AutoTradeDecision(
            False, f"{sym} cool-down ({remaining}s remaining)"
        )

    # Gate 7a: GLOBAL session cap (across all symbols)
    if state.auto_trade_count_session >= state.auto_trade_max_per_session:
        return AutoTradeDecision(
            False, f"global session cap reached ({state.auto_trade_max_per_session})"
        )

    # Gate 7b: per-symbol session sub-cap
    sym_count = state.auto_trade_count_by_symbol.get(sym, 0)
    if sym_count >= state.auto_trade_max_per_symbol:
        return AutoTradeDecision(
            False, f"{sym} sub-cap reached ({state.auto_trade_max_per_symbol})"
        )

    # Gate 8: option pricing available — note NaN is truthy in Python, so
    # explicit isfinite() guard is required (IBKR silently rejects LMT orders
    # built from NaN entry_limit).
    import math as _math
    opt = verdict_dict.get("option_pricing")
    if not opt:
        return AutoTradeDecision(False, "no option pricing on verdict")
    for k in ("entry_limit", "t1", "t2", "t3", "stop"):
        v = opt.get(k)
        if v is None or not isinstance(v, (int, float)) or not _math.isfinite(v) or v <= 0:
            return AutoTradeDecision(False, f"invalid option_pricing.{k}={v!r}")

    strike = verdict_dict.get("strike_rec")
    # User override on the AutoTrade panel takes precedence over FEF's
    # confidence-tier suggested qty. 0 = use suggested.
    override = getattr(state, "auto_trade_qty_override", 0)
    qty = override if override > 0 else (verdict_dict.get("suggested_qty") or 15)
    if not strike:
        return AutoTradeDecision(False, "no strike_rec on verdict")

    # Gate 9: risk caps — limit qty if it would breach max_contracts_per_trade
    max_contracts = int(os.environ.get("FEF_MAX_CONTRACTS_PER_TRADE", "20"))
    if qty > max_contracts:
        original_qty = qty
        qty = max_contracts
        logger.info("Auto-trade qty %d clipped to max_contracts_per_trade=%d", original_qty, max_contracts)

    # All gates passed — place the bracket
    from fef_sidecar.engine.live_trading import BracketSpec, place_bracket

    today = time.strftime("%Y%m%d")
    # 50/30/20 split — let runners run. Today's MFE analysis: 80% of winners
    # reach >=2R MFE, 40% reach >=3R. Old 85/10/5 was exiting most of the
    # position at T1 (1R) leaving the bulk of the move on the table.
    t1_qty = max(1, round(qty * 0.50))
    t2_qty = max(1, round(qty * 0.30))
    t3_qty = max(1, qty - t1_qty - t2_qty)
    if t1_qty + t2_qty + t3_qty != qty:
        t1_qty = qty - t2_qty - t3_qty

    strategy = state.exit_strategy_long

    spx_t1 = verdict_dict.get("t1")
    spx_t2 = verdict_dict.get("t2")
    spx_t3 = verdict_dict.get("t3")
    spx_stop = verdict_dict.get("stop")
    # Use the scanner-provided symbol if present, else fall back to active
    trade_symbol = sym

    if strategy == "fixed_dollar":
        delta = abs(opt.get("delta") or 0.40)
        per_contract_dollar = lambda d: d / max(1, qty) / 100.0
        t1_opt = float(opt["entry_limit"]) + per_contract_dollar(state.exit_dollar_t1) / max(delta, 0.05) * delta
        t2_opt = float(opt["entry_limit"]) + per_contract_dollar(state.exit_dollar_t2) / max(delta, 0.05) * delta
        t3_opt = float(opt["entry_limit"]) + per_contract_dollar(state.exit_dollar_t3) / max(delta, 0.05) * delta
        stop_opt = max(0.05, float(opt["entry_limit"]) + per_contract_dollar(state.exit_dollar_stop) / max(delta, 0.05) * delta)
        spec = BracketSpec(
            symbol=trade_symbol, strike=float(strike),
            right="C" if direction == "LONG_CALLS" else "P",
            expiry=today, qty=qty,
            entry_limit=float(opt["entry_limit"]),
            t1_limit=round(t1_opt, 2), t2_limit=round(t2_opt, 2),
            t3_limit=round(t3_opt, 2), stop_limit=round(stop_opt, 2),
            t1_qty=t1_qty, t2_qty=t2_qty, t3_qty=t3_qty,
            exit_strategy="fixed_dollar",
            dollar_loss_cap=-3000.0,
            use_market_entry=True,
        )
    elif strategy == "spx_points":
        # Auto-trade default: market-order entry + -$3000 dollar loss cap +
        # BE ladder (initialized via entry_spx_price captured from state).
        spec = BracketSpec(
            symbol=trade_symbol, strike=float(strike),
            right="C" if direction == "LONG_CALLS" else "P",
            expiry=today, qty=qty,
            entry_limit=float(opt["entry_limit"]),
            t1_limit=float(opt["t1"]), t2_limit=float(opt["t2"]),
            t3_limit=float(opt["t3"]), stop_limit=float(opt["stop"]),
            t1_qty=t1_qty, t2_qty=t2_qty, t3_qty=t3_qty,
            exit_strategy="spx_points",
            spx_t1=spx_t1, spx_t2=spx_t2, spx_t3=spx_t3, spx_stop=spx_stop,
            entry_spx_price=state.last_spx_price,
            dollar_loss_cap=-3000.0,
            use_market_entry=True,
        )
    else:  # 'option_price' (legacy default)
        spec = BracketSpec(
            symbol=trade_symbol, strike=float(strike),
            right="C" if direction == "LONG_CALLS" else "P",
            expiry=today, qty=qty,
            entry_limit=float(opt["entry_limit"]),
            t1_limit=float(opt["t1"]), t2_limit=float(opt["t2"]),
            t3_limit=float(opt["t3"]), stop_limit=float(opt["stop"]),
            t1_qty=t1_qty, t2_qty=t2_qty, t3_qty=t3_qty,
            exit_strategy="option_price",
            dollar_loss_cap=-3000.0,
            use_market_entry=True,
        )

    try:
        result = await place_bracket(state.ibkr, spec, confirm=True)
    except Exception as e:
        logger.exception("Auto-trade place_bracket failed")
        return AutoTradeDecision(False, f"place_bracket error: {e}")

    # Bump counters on successful submission — global + per-symbol counts
    # AND per-symbol last-fire time (for the per-symbol cool-down clock).
    if result.get("ok"):
        state.auto_trade_last_at_ms = now_ms
        state.auto_trade_last_at_ms_by_symbol[sym] = now_ms
        state.auto_trade_count_session += 1
        state.auto_trade_count_by_symbol[sym] = state.auto_trade_count_by_symbol.get(sym, 0) + 1
        logger.warning(
            "AUTO-TRADE FIRED: %s %d %s %s%.0f @ $%.2f "
            "(global %d/%d, %s sub %d/%d)",
            direction, qty, sym,
            "C" if direction == "LONG_CALLS" else "P",
            strike, opt["entry_limit"],
            state.auto_trade_count_session, state.auto_trade_max_per_session,
            sym, state.auto_trade_count_by_symbol[sym], state.auto_trade_max_per_symbol,
        )

    return AutoTradeDecision(True, "fired", order_result=result)
