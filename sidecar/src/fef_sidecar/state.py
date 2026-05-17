"""Shared app state — providers, engine, broadcaster.

Constructed once during FastAPI lifespan and attached to app.state so
routes/WS handlers can pull it via Depends().
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

from fef_sidecar.engine import FEFEngine
from fef_sidecar.events import EventBroadcaster
from fef_sidecar.ibkr import IBKRProvider, LiveIBKRProvider, MockIBKRProvider
from fef_sidecar.quantdata import MockQuantDataClient, QuantDataClient


class QuantDataProvider(Protocol):
    """Both QuantDataClient and MockQuantDataClient satisfy this duck-type."""

    async def __aenter__(self) -> "QuantDataProvider": ...
    async def __aexit__(self, *args: object) -> None: ...
    async def aclose(self) -> None: ...
    async def fetch_market_snapshot(self, **kwargs: object) -> object: ...


def _env_use_mock(key: str, default: bool = True) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.lower() not in ("live", "true", "1", "yes", "on")


def make_quantdata_client() -> QuantDataProvider:
    if _env_use_mock("FEF_PROVIDER_QUANTDATA", default=True):
        return MockQuantDataClient()  # type: ignore[return-value]
    return QuantDataClient()  # type: ignore[return-value]


def make_ibkr_provider() -> IBKRProvider:
    if _env_use_mock("FEF_PROVIDER_IBKR", default=True):
        return MockIBKRProvider()
    host = os.environ.get("FEF_IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("FEF_IBKR_PORT", "7497"))
    client_id = int(os.environ.get("FEF_IBKR_CLIENT_ID", "1"))
    return LiveIBKRProvider(host=host, port=port, client_id=client_id)


@dataclass
class AppState:
    quantdata: QuantDataProvider
    ibkr: IBKRProvider
    engine: FEFEngine
    broadcaster: EventBroadcaster
    loop_task: object | None = None
    bar_task: object | None = None
    tick_task: object | None = None
    quantdata_poll_task: object | None = None
    health_check_task: object | None = None
    last_spx_price: float = 0.0
    last_iter_num: int = 0
    last_verdict: dict[str, object] = field(default_factory=dict)
    last_snapshot: object | None = None  # cached MarketSnapshot for FEF to consume
    ibkr_status: str = "unknown"  # "connected" | "disconnected" | "reconnecting" | "unknown"
    last_iter_perf_ms: dict[str, float] = field(default_factory=dict)  # IMPROVEMENTS #10
    session_date: str = ""  # IMPROVEMENTS #22 — tracks current session for reset detection

    @classmethod
    def build(cls) -> AppState:
        return cls(
            quantdata=make_quantdata_client(),
            ibkr=make_ibkr_provider(),
            engine=_make_engine(),
            broadcaster=EventBroadcaster(),
        )


def _make_engine() -> FEFEngine:
    """Engine factory honoring env-var thresholds (IMPROVEMENTS #2 + #7)."""
    interval = float(os.environ.get("FEF_LOOP_INTERVAL_SEC", "120"))
    # Auto-scale magnitude thresholds by interval (default 120s = scale 1.0)
    threshold_scale = max(0.1, interval / 120.0)
    return FEFEngine(
        wall_proximity_pts=float(os.environ.get("FEF_WALL_PROXIMITY_PTS", "5.0")),
        wall_collapse_pct=float(os.environ.get("FEF_WALL_COLLAPSE_PCT", "0.20")),
        wall_collapse_floor_m=float(os.environ.get("FEF_WALL_COLLAPSE_FLOOR_M", "5.0")),
        absorption_fe_ratio_min=float(os.environ.get("FEF_ABSORPTION_FE_MIN", "5.0")),
        threshold_scale=threshold_scale,
    )
