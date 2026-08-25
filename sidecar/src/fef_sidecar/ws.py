"""WebSocket endpoint — streams events from the broadcaster to the frontend."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from fef_sidecar.events import StatusEvent
from fef_sidecar.state import AppState

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    state: AppState = websocket.app.state.fef
    await websocket.accept()

    # Send initial status snapshot
    # [REMOVED 2026-08-25] StatusEvent.quantdata — QuantData subscription
    # CANCELLED; the frontend stopped declaring the field in the same change.
    initial = StatusEvent(
        ibkr=state.ibkr_status if state.ibkr_status != "unknown"
        else ("connected" if state.ibkr.is_connected else "disconnected"),
        engine="running" if state.loop_task is not None else "stopped",
        history_size=state.engine.history_size,
    )
    await websocket.send_json(initial.model_dump(mode="json"))
    logger.info("WS client connected (total=%d)", state.broadcaster.subscriber_count + 1)

    async with state.broadcaster.subscribe() as queue:
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event.model_dump(mode="json"))
        except WebSocketDisconnect:
            logger.info("WS client disconnected")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WS handler crashed — closing connection")
            try:
                await websocket.close()
            except Exception:  # pragma: no cover
                pass
