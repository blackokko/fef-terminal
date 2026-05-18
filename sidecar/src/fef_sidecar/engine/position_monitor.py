"""Live position monitor — polls IBKR for open positions + per-position greeks.

Writes LivePosition snapshots every FEF_POSITION_POLL_INTERVAL_SEC (default 5s)
so the UI can show real-time unrealized P/L + delta/gamma/theta exposure.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fef_sidecar.db.models import LivePosition
from fef_sidecar.db.repository import SignalIterRepository
from fef_sidecar.ibkr.live import LiveIBKRProvider
from fef_sidecar.state import AppState

logger = logging.getLogger(__name__)


async def position_monitor_loop(state: AppState) -> None:
    interval = float(os.environ.get("FEF_POSITION_POLL_INTERVAL_SEC", "5"))
    logger.info("Starting position monitor loop (interval=%.1fs)", interval)

    while True:
        try:
            if not isinstance(state.ibkr, LiveIBKRProvider):
                # Mock mode: skip — no real positions
                await asyncio.sleep(interval)
                continue

            ib = state.ibkr._ib  # type: ignore[attr-defined]
            if ib is None or not ib.isConnected():
                await asyncio.sleep(interval)
                continue

            positions = ib.positions()
            for p in positions:
                if abs(p.position) < 0.5:
                    continue
                if getattr(p.contract, "secType", "") != "OPT":
                    continue
                # Fetch live greeks via reqMktData. No generic tick list for
                # OPT — IBKR rejects "13" with error 321. modelGreeks stream
                # automatically via tickOptionComputation events.
                ticker = ib.reqMktData(p.contract, "", False, False)
                await asyncio.sleep(0.5)  # let greeks populate
                last = float(ticker.last) if ticker.last and ticker.last > 0 else (
                    float(ticker.close or 0))
                delta = gamma = theta = vega = iv = None
                if ticker.modelGreeks:
                    delta = float(ticker.modelGreeks.delta) if ticker.modelGreeks.delta is not None else None
                    gamma = float(ticker.modelGreeks.gamma) if ticker.modelGreeks.gamma is not None else None
                    theta = float(ticker.modelGreeks.theta) if ticker.modelGreeks.theta is not None else None
                    vega = float(ticker.modelGreeks.vega) if ticker.modelGreeks.vega is not None else None
                    iv = float(ticker.modelGreeks.impliedVol) if ticker.modelGreeks.impliedVol is not None else None
                ib.cancelMktData(p.contract)

                unrealized = (last - p.avgCost / 100) * p.position * 100
                snap = LivePosition(
                    symbol=p.contract.symbol, strike=float(p.contract.strike),
                    right=p.contract.right, expiry=p.contract.lastTradeDateOrContractMonth,
                    qty=int(p.position), avg_cost=p.avgCost / 100,
                    market_price=last, unrealized_pnl=round(unrealized, 2),
                    delta=delta, gamma=gamma, theta=theta, vega=vega, iv=iv,
                )
                await SignalIterRepository.write_live_position(snap)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Position monitor iteration failed")
        await asyncio.sleep(interval)
