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


class SnapshotEvent(BaseModel):
    """Lightweight market snapshot — fires on the QuantData poll cadence (faster
    than the FEF loop). Lets the sidebar walls/drift/MP refresh without waiting
    for the next FEF iter."""

    type: Literal["snapshot"] = "snapshot"
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    spx_price: float
    top_gex: list[dict[str, Any]]
    top_dex: list[dict[str, Any]]
    drift: dict[str, Any]
    ntm_max_pain: float
    raw_max_pain: float
    aggression: dict[str, Any] | None = None


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
    vix: float | None = None  # IMPROVEMENTS #M — last VIX quote


class MicrostructureEvent(BaseModel):
    """Sub-second order flow / aggression / microprice signal.

    Based on established microstructure literature (OFI/Cont-Kukanov-Stoikov,
    AVI/Lee-Ready, Microprice/Stoikov). Independent of FEF model.
    """

    type: Literal["microstructure"] = "microstructure"
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    ofi_window: float
    avi_pct: float
    aggressive_buy_volume: float
    aggressive_sell_volume: float
    microprice: float | None = None
    last_trade_price: float | None = None
    microprice_deviation_bp: float | None = None
    direction: str  # BULLISH_PRESSURE / BEARISH_PRESSURE / BALANCED
    confidence: str  # HIGH / MEDIUM / LOW
    reasoning: str
    quote_count: int
    trade_count: int


class TournamentEvent(BaseModel):
    """Parallel-strategy verdicts — side-by-side what each preset would fire."""

    type: Literal["tournament"] = "tournament"
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    iter_num: int
    spx_price: float
    variants: list[dict[str, Any]]  # [{name, direction, confidence, reasoning, aligned, absorption}]
    consensus: dict[str, Any]


class AccountEvent(BaseModel):
    """Live IBKR account scorecard — realized + unrealized P/L from broker."""

    type: Literal["account"] = "account"
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    account: str
    net_liquidation: float | None = None
    buying_power: float | None = None
    available_funds: float | None = None
    daily_pnl: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    gross_position_value: float | None = None
    open_positions_count: int = 0
    open_options_count: int = 0
    currency: str = "USD"


class RegimeEvent(BaseModel):
    """IMPROVEMENTS: market regime classification + recommendations.

    Broadcast every iter so the regime banner stays current. UI can also
    detect transitions (compare regime to previous) for toast alerts.
    """

    type: Literal["regime"] = "regime"
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    regime: str
    label: str
    confidence: float
    rationale: str
    recommended_preset_hint: str = ""
    recommended_patterns: list[str] = Field(default_factory=list)
    bias: str = "NEUTRAL"
    sizing: str = "STANDARD"


class PatternMatchEvent(BaseModel):
    """IMPROVEMENTS #R — fires when a user-defined pattern matches an iter."""

    type: Literal["pattern_match"] = "pattern_match"
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    pattern_id: int
    pattern_name: str
    pattern_description: str = ""
    signal_iter_id: int
    iter_num: int
    spx_price: float
    audio_alert: bool = True
    toast_alert: bool = True


Event = (
    TickEvent
    | IterEvent
    | TicketEvent
    | StatusEvent
    | SnapshotEvent
    | PatternMatchEvent
    | RegimeEvent
    | AccountEvent
    | TournamentEvent
    | MicrostructureEvent
)


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
