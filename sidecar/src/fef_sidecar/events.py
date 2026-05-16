"""Event types + in-process broadcaster.

The WS layer subscribes to the broadcaster; the FEF loop + bar stream
publish to it. Each subscriber gets its own asyncio.Queue (bounded), with
drop-oldest semantics so a slow client can't block the producer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class TickEvent(BaseModel):
    type: Literal["tick"] = "tick"
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    spx_price: float
    bar: dict[str, Any] | None = None


class IterEvent(BaseModel):
    type: Literal["iter"] = "iter"
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    iter_num: int
    session_date: str
    spx_price: float
    verdict: dict[str, Any]
    snapshot: dict[str, Any]


class TicketEvent(BaseModel):
    type: Literal["ticket"] = "ticket"
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    ticket_id: int
    signal_iter_id: int
    direction: str
    contract_strike: float
    contract_expiry: str
    qty: int
    entry_limit: float
    t1: float
    t2: float
    t3: float
    stop: float
    status: str


class StatusEvent(BaseModel):
    type: Literal["status"] = "status"
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    quantdata: str  # "connected" | "disconnected" | "mock"
    ibkr: str
    engine: str  # "running" | "stopped"
    history_size: int = 0


Event = TickEvent | IterEvent | TicketEvent | StatusEvent


class EventBroadcaster:
    """Fan-out publish/subscribe with bounded per-subscriber queues."""

    def __init__(self, queue_maxsize: int = 256) -> None:
        self._queue_maxsize = queue_maxsize
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        async with self._lock:
            stale: list[asyncio.Queue[Event]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # Drop oldest, push new — keep latest data
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        stale.append(q)
            for q in stale:
                self._subscribers.discard(q)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_maxsize)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
