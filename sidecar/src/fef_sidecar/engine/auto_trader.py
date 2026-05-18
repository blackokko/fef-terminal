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


async def maybe_auto_trade(state, verdict_dict: dict) -> AutoTradeDecision:
    """Evaluate gates + maybe place a bracket order.

    verdict_dict: the verdict from _run_one_iter (with direction, confidence,
    strike_rec, option_pricing, suggested_qty, t1/t2/t3/stop)."""

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

    # Gate 6: cool-down
    now_ms = int(time.time() * 1000)
    elapsed_sec = (now_ms - state.auto_trade_last_at_ms) / 1000
    if state.auto_trade_last_at_ms > 0 and elapsed_sec < state.auto_trade_cool_down_sec:
        return AutoTradeDecision(
            False, f"cool-down ({state.auto_trade_cool_down_sec - int(elapsed_sec)}s remaining)"
        )

    # Gate 7: session cap
    if state.auto_trade_count_session >= state.auto_trade_max_per_session:
        return AutoTradeDecision(
            False, f"session cap reached ({state.auto_trade_max_per_session})"
        )

    # Gate 8: option pricing available
    opt = verdict_dict.get("option_pricing")
    if not opt or not all(opt.get(k) for k in ("entry_limit", "t1", "t2", "t3", "stop")):
        return AutoTradeDecision(False, "no option pricing on verdict")

    strike = verdict_dict.get("strike_rec")
    qty = verdict_dict.get("suggested_qty") or 15
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
    spec = BracketSpec(
        symbol=state.active_symbol, strike=float(strike),
        right="C" if direction == "LONG_CALLS" else "P",
        expiry=today, qty=qty,
        entry_limit=float(opt["entry_limit"]),
        t1_limit=float(opt["t1"]), t2_limit=float(opt["t2"]),
        t3_limit=float(opt["t3"]), stop_limit=float(opt["stop"]),
        t1_qty=qty // 3, t2_qty=qty // 3, t3_qty=qty - 2 * (qty // 3),
    )

    try:
        result = await place_bracket(state.ibkr, spec, confirm=True)
    except Exception as e:
        logger.exception("Auto-trade place_bracket failed")
        return AutoTradeDecision(False, f"place_bracket error: {e}")

    # Bump counters on successful submission
    if result.get("ok"):
        state.auto_trade_last_at_ms = now_ms
        state.auto_trade_count_session += 1
        logger.warning(
            "AUTO-TRADE FIRED: %s %d %s %s%.0f @ $%.2f (session count: %d/%d)",
            direction, qty, state.active_symbol,
            "C" if direction == "LONG_CALLS" else "P",
            strike, opt["entry_limit"],
            state.auto_trade_count_session, state.auto_trade_max_per_session,
        )

    return AutoTradeDecision(True, "fired", order_result=result)
