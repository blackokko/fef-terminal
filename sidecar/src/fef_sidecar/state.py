"""Shared app state — providers, engine, broadcaster.

Constructed once during FastAPI lifespan and attached to app.state so
routes/WS handlers can pull it via Depends().
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

from fef_sidecar.engine import FEFEngine
from fef_sidecar.engine.microstructure import MicrostructureState
from fef_sidecar.engine.mock_autotrader import MockAutoTraderState
from fef_sidecar.engine.regime import RegimeAssessment, RegimeDetector
from fef_sidecar.engine.tournament import DEFAULT_VARIANTS, TournamentRunner
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
        # Enable coherent track so this mock returns a price that matches
        # the IBKR mock + tick stream + microstructure at the same moment.
        os.environ.setdefault("FEF_MOCK_COHERENT", "true")
        return MockQuantDataClient()  # type: ignore[return-value]
    return QuantDataClient()  # type: ignore[return-value]


def make_ibkr_provider() -> IBKRProvider:
    if _env_use_mock("FEF_PROVIDER_IBKR", default=True):
        # Production mock: coherent PriceTrack so chart bars + ticks +
        # QuantData + microstructure all show the same price.
        return MockIBKRProvider(use_coherent_track=True)
    host = os.environ.get("FEF_IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("FEF_IBKR_PORT", "7497"))
    client_id = int(os.environ.get("FEF_IBKR_CLIENT_ID", "1"))
    return LiveIBKRProvider(host=host, port=port, client_id=client_id)


@dataclass
class AppState:
    quantdata: QuantDataProvider
    ibkr: IBKRProvider
    engine: FEFEngine
    regime_detector: RegimeDetector
    tournament: TournamentRunner
    autotrader: MockAutoTraderState
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
    last_vix: float | None = None  # IMPROVEMENTS #M
    vix_task: object | None = None
    last_regime: RegimeAssessment | None = None
    account_task: object | None = None
    last_account: object | None = None  # AccountSummary or None
    microstructure_state: MicrostructureState = field(default_factory=MicrostructureState)
    microstructure_task: object | None = None
    # HFT signal subsystems
    vol_state: object | None = None    # RealizedVolState
    stuffing_state: object | None = None  # StuffingState
    depth_state: object | None = None  # DepthState
    hft_task: object | None = None
    # Update counters per data source — used by HFT panel badges to show
    # LIVE / EMPTY / SYNTH. Bumped from the actual subscription callbacks.
    tick_quote_count_live: int = 0
    tick_trade_count_live: int = 0
    depth_update_count_live: int = 0
    position_task: object | None = None
    auto_wf_task: object | None = None
    # Active symbol — drives which contract is fed to bar/tick/iter loops and
    # which ticker is set on the QuantData page filter. Changing it triggers
    # a loop restart (api.py /config/symbol POST handler).
    active_symbol: str = field(default_factory=lambda: os.environ.get("FEF_ACTIVE_SYMBOL", "SPX").upper())
    # Symbols allowed in the UI selector (env-overridable for users with
    # extra QuantData subscriptions). Default = SPX + the new equity tickers.
    available_symbols: list[str] = field(default_factory=lambda: [
        s.strip().upper() for s in os.environ.get(
            "FEF_AVAILABLE_SYMBOLS",
            "SPX,SPY,QQQ,IWM,ES,NQ,RTY"
        ).split(",") if s.strip()
    ])

    @classmethod
    def build(cls) -> AppState:
        return cls(
            quantdata=make_quantdata_client(),
            ibkr=make_ibkr_provider(),
            engine=_make_engine(),
            regime_detector=RegimeDetector(
                window_size=int(os.environ.get("FEF_REGIME_WINDOW", "12"))
            ),
            tournament=TournamentRunner(DEFAULT_VARIANTS),
            autotrader=MockAutoTraderState(),
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
