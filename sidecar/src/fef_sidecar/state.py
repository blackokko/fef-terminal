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
    # Live tick from IBKR (tick_stream_loop) — set ONLY by that loop, never
    # overwritten by QuantData fetches. Used by _run_one_iter to override the
    # snapshot's spx_price with the freshest possible spot, so the engine
    # evaluates wall distance / MP gravity / etc. against current price
    # instead of the up-to-15s-stale QuantData snapshot price.
    last_live_tick_spx: float = 0.0
    last_live_tick_ms: int = 0
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

    # Auto-trade controls — OFF by default. Toggled via /auto_trade/toggle.
    # When ON, every iter that produces a verdict meeting all safety
    # criteria triggers an automatic bracket order via place_bracket().
    auto_trade_enabled: bool = False
    auto_trade_confidence_floor: str = "HIGH"   # 'HIGH' / 'MEDIUM' / 'LOW'
    auto_trade_cool_down_sec: int = 300         # min seconds between auto fires
    auto_trade_max_per_session: int = 5         # hard cap per sidecar session
    auto_trade_count_session: int = 0           # bumped after each successful fire
    auto_trade_last_at_ms: int = 0              # epoch ms of last fire
    auto_trade_last_block_reason: str = ""      # why most-recent eligible signal was skipped
    # Override the FEF engine's suggested_qty (which is 15/10/5 by
    # HIGH/MEDIUM/LOW confidence). 0 = use suggested. Otherwise fixed
    # contracts per fire across all confidences. Capped by
    # FEF_MAX_CONTRACTS_PER_TRADE env.
    auto_trade_qty_override: int = 0

    # Runner protector — after T1 fills, market-exit remaining T2/T3 qty if
    # the option premium retraces back below the T1 fill price. Locks in the
    # 85% T1 win without giving back gains on the 15% runners.
    runner_protect_enabled: bool = True
    runner_protect_task: object | None = None

    # Exit strategy for LONG_CALLS / LONG_PUTS positions:
    #   'spx_points'   = watcher fires market sells when SPX crosses target
    #                     levels (no option-price bracket). Mathematically
    #                     exact — no gamma/theta/IV approximation.
    #   'fixed_dollar' = translate $1k / $2.5k / $5k targets to option-price
    #                     bracket via current delta. Standard IBKR bracket.
    exit_strategy_long: str = "spx_points"
    # Dollar targets (per position) used when exit_strategy_long='fixed_dollar'
    exit_dollar_t1: float = 1000.0
    exit_dollar_t2: float = 2500.0
    exit_dollar_t3: float = 5000.0
    exit_dollar_stop: float = -1000.0
    spx_target_task: object | None = None

    # Spread exit rule (always premium_pct for now):
    #   TP  = close for $0.05 debit (basically expire worthless)
    #   Stop = close when current debit reaches stop_multiplier × initial credit
    spread_stop_multiplier: float = 4.0
    spread_tp_close_debit: float = 0.05
    spread_exit_task: object | None = None
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

    # Prior-day close per symbol — populated once at startup and on each
    # symbol switch. Read by tick/bar loops to attach prior_close to
    # TickEvent so the Live Quote panel can show day-change %.
    prior_close_by_symbol: dict[str, float] = field(default_factory=dict)

    # 30-min bar cache per symbol — feeds the credit-spread recommender's
    # TA entry detector (EMA 8/21, StdDev channel, engulfing). Re-fetched
    # at most every 25 minutes per symbol so freshly closed 30-min bars
    # get picked up without hammering IBKR.
    spread_bars_by_symbol: dict[str, list[object]] = field(default_factory=dict)
    spread_bars_fetched_ms: dict[str, int] = field(default_factory=dict)
    # Last triggered signature — used so the recommender alert fires
    # exactly once per fresh trigger bar (per user preference).
    last_spread_trigger_signature: str = ""
    # Cached recommendation object so callers don't have to re-fetch when
    # the trigger hasn't moved.
    last_spread_recommendation: dict[str, object] | None = None

    # ---- Multi-symbol scanner ----
    # Scanner runs FEF iters for each symbol in scan_enabled_symbols every
    # FEF_SCAN_INTERVAL_SEC seconds (default 60s). Independent of the
    # active_symbol / chart symbol. Each enabled symbol gets its own
    # FEFEngine in scanner_engines (lazy-created on first scan).
    scan_enabled_symbols: dict[str, bool] = field(default_factory=lambda: {
        "SPX": False, "SPY": False, "QQQ": False, "IWM": False,
    })
    scanner_engines: dict[str, object] = field(default_factory=dict)
    scanner_task: object | None = None
    # Per-symbol auto-trade fire counters — used for the hybrid cap
    # (global max_per_session AND per-symbol max_per_symbol).
    auto_trade_count_by_symbol: dict[str, int] = field(default_factory=dict)
    auto_trade_max_per_symbol: int = 2
    # Per-symbol last-fire timestamps — drives per-symbol cool-down. The
    # global auto_trade_last_at_ms stays as a backstop; each symbol's
    # cool-down clock is independent so SPX firing doesn't block QQQ.
    auto_trade_last_at_ms_by_symbol: dict[str, int] = field(default_factory=dict)
    # Last verdict cache per scanner symbol — feeds the FE ScannerPanel
    # tiles so each symbol shows its most recent direction/confidence
    # without subscribing to every IterEvent in render.
    last_scanner_verdict_by_symbol: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def build(cls) -> AppState:
        return cls(
            quantdata=make_quantdata_client(),
            ibkr=make_ibkr_provider(),
            engine=_make_engine(),
            regime_detector=RegimeDetector(
                window_size=int(os.environ.get("FEF_REGIME_WINDOW", "60"))
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
