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
from fef_sidecar.engine.tick_candles import TickCandleService
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
    # Account class — populated by the IBKR provider on connect. Determines
    # whether SignalIter / LiveOrder writes get tagged 'paper' / 'live' /
    # 'mock' / 'unknown'. Research scripts filter on this so paper and
    # live data never silently mix once both flow through the same DB.
    account_class: str = "unknown"
    account_id: str = ""
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
    # In-memory mutex shared between the polling spx_target_watcher_loop
    # and the new tick-driven spx_stop_tick_watcher. Both can detect a
    # stop breach within milliseconds of each other; this set ensures
    # only one path fires the market sell per parent. Asyncio is
    # single-threaded so set add/discard are atomic.
    firing_parent_ids: set[int] = field(default_factory=set)
    # Contract-level pending-sell reservation. Prevents cross-parent races on
    # the same (symbol, strike, right, expiry) — each parent that places a SELL
    # reserves its qty here, and subsequent parents subtract it from available
    # long_qty before placing their own SELL. Decremented when the SELL fills
    # (or after a 2s safety timeout).
    firing_contracts: dict[tuple, int] = field(default_factory=dict)
    spx_stop_tick_task: object | None = None
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
    auto_trade_count_session: int = 0           # bumped after each successful fire (display only; cap is per-symbol)
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
    # How the fixed_dollar tier targets are interpreted:
    #   'cumulative_realized' (default) — d_targets are CUMULATIVE banked totals;
    #     each tier's slice must earn the increment between targets, so hitting
    #     all 3 banks ≈ the top number. Intuitive ("$1000 TP" ≈ $1000 booked).
    #   'whole_unrealized' — legacy: tier fires on full-position unrealized P&L
    #     crossing the target (the 7510C behaviour where 3 TPs banked ~$650).
    exit_dollar_semantics: str = "cumulative_realized"
    spx_target_task: object | None = None

    # ---- Phase detector (RIDE / FADE / WAIT direction refiner) ----
    # Reads WHERE in a move we are (ignition vs exhaustion) from 6 mechanical
    # pillars and emits a direction — fixes the 2026-07-01 failure where the
    # engine followed a dump into PUTS at the capitulation low instead of
    # fading the exhaustion (the +$25k 7470C play). See engine/phase_detector.py.
    #   mode: 'off'      — not evaluated.
    #         'advisory' — evaluated + logged every iter, ZERO trade impact.
    #         'active'   — overrides the auto-trade entry direction.
    # Default 'advisory' (zero trade impact — computes + logs + shows in the panel
    # so it can be watched/validated). Promote to 'active' only after the backtest
    # + advisory-session validation clear the pivot kill-gate (human decision).
    phase_mode: str = "advisory"
    phase_window_sec: int = 300
    phase_vel_sec: int = 75
    phase_vel_ignite: float = 16.0
    phase_vel_stall: float = 3.0
    phase_min_move: float = 18.0
    phase_bounce_pt: float = 3.0
    phase_iv_delta: float = 0.006
    phase_mp_overshoot: float = 8.0
    phase_fade_min: int = 5       # backtest: only >=5 is sane + positive
    phase_ride_min: int = 7       # >6-pillar max => RIDE off (chasing thrust loses)
    # Runtime holders (not config): the live detector + its latest verdict dict.
    # POST /phase_detector nulls phase_detector to force a rebuild with new
    # thresholds on the next iter.
    phase_detector: object | None = None
    phase_last_verdict: dict | None = None

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
            "SPX,SPY,QQQ,IWM,GLD,SMH,TLT,ES,NQ,RTY"
        ).split(",") if s.strip()
    ])

    # Prior-day close per symbol — populated once at startup and on each
    # symbol switch. Read by tick/bar loops to attach prior_close to
    # TickEvent so the Live Quote panel can show day-change %.
    prior_close_by_symbol: dict[str, float] = field(default_factory=dict)

    # ML inference service — loads the latest persisted RF estimator from
    # disk and scores live signals for `ml_expected_r`. Observational only;
    # does not affect verdict generation. None until first model is trained
    # or if scikit-learn is unavailable.
    ml_inference: object | None = None

    # Per-symbol option expiry cache. Keyed by (symbol, session_date_et)
    # so we hit IBKR's reqContractDetails at most once per symbol per day.
    # Without this, the multi-symbol scanner hard-coded today's date as
    # the option expiry for every symbol, silently failing for GLD/SMH/TLT
    # (no 0DTE) and for SPY/QQQ/IWM on Tu/Th.
    expiry_cache_by_key: dict[tuple, str] = field(default_factory=dict)

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
        "GLD": False, "SMH": False, "TLT": False,
    })
    scanner_engines: dict[str, object] = field(default_factory=dict)
    scanner_task: object | None = None
    # Per-symbol auto-trade fire counters — the only binding cap. Each
    # symbol has an independent budget; the global "total budget" shown
    # in UI = max_per_symbol × enabled_symbols (auto-derived display only).
    auto_trade_count_by_symbol: dict[str, int] = field(default_factory=dict)
    auto_trade_max_per_symbol: int = 2
    # Trigger types the auto-trader is willing to fire on. Maps engine
    # verdict flags (aligned, wall_collapse_trigger) to named categories.
    # Default permissive: keep data flowing on both subsets so we can
    # observe whether confluence sustains its edge across sessions.
    #   'confluence'    = wall_collapse + aligned (today: +0.37R, 41% win)
    #   'aligned_only'  = aligned without wall_collapse (today: -0.26R, 22% win)
    #   'wall_only'     = wall_collapse without aligned (currently impossible
    #                     post wall_collapse_requires_aligned gate)
    auto_trade_trigger_types: list[str] = field(default_factory=lambda: [
        "confluence", "aligned_only"
    ])

    # Shadow engine — parallel evaluator for A/B-testing engine changes.
    # When set, every iter is pushed to BOTH the production engine and the
    # shadow. Auto-trade still uses production. Verdicts are compared and
    # logged to ShadowComparison so the user can see whether the shadow
    # config would have produced different (better?) signals.
    shadow_engine: object | None = None
    shadow_label: str = ""
    # Per-symbol last-fire timestamps — drives per-symbol cool-down. The
    # global auto_trade_last_at_ms stays as a backstop; each symbol's
    # cool-down clock is independent so SPX firing doesn't block QQQ.
    auto_trade_last_at_ms_by_symbol: dict[str, int] = field(default_factory=dict)
    # Latest HFT + microstructure snapshots — populated by hft_loop +
    # microstructure_loop on every iter so _run_one_iter can stamp them
    # into SignalIter.extras for offline analysis (which HFT/microstructure
    # state coincided with each signal).
    last_hft_state: dict = field(default_factory=dict)
    last_microstructure_state: dict = field(default_factory=dict)

    # Last verdict cache per scanner symbol — feeds the FE ScannerPanel
    # tiles so each symbol shows its most recent direction/confidence
    # without subscribing to every IterEvent in render.
    last_scanner_verdict_by_symbol: dict[str, dict] = field(default_factory=dict)

    # Option chain capture loop task handle. Owned task, cancelled on shutdown.
    option_chain_capture_task: object | None = None
    # ES VWAP capture loop task handle.
    es_vwap_task: object | None = None

    # N-tick candle aggregation (1000T / 2000T chart resolutions). Fed by
    # microstructure_loop's trade callback — same ES AllLast stream, zero
    # extra IBKR tick-by-tick subscriptions. Served by /chart/tick_candles.
    tick_candles: TickCandleService = field(default_factory=TickCandleService)

    # Pattern trigger loop task handle (confirmed-bar pattern fires ->
    # advisory audit rows + opt-in auto-trade). Restarted on symbol switch.
    pattern_trigger_task: object | None = None

    # Per-symbol auto-trade firing locks. maybe_auto_trade serializes the
    # whole gate+place sequence per symbol so near-simultaneous calls from
    # different signal sources (scanner / main loop / pattern trigger) can't
    # both pass cool-down/cap/duplicate-parent gates before either records
    # its fire (7/1: twin QQQ 720P parents 16ms apart).
    auto_trade_symbol_locks: dict[str, object] = field(default_factory=dict)

    # CHOP skip-gate — per-(bar_symbol, session_date) cached time-of-day r30
    # distribution (built once per session from the prior ~20 sessions of
    # 1-min bars; see engine/chop_gate.py). Values are chop_gate._DistCacheEntry.
    chop_tod_dist_cache: dict[tuple, object] = field(default_factory=dict)

    # Impulse attention flag — per-(symbol, direction) last-fire epoch ms for
    # the advisory flag's own cooldown (UIConfig.impulse_flag_cooldown_sec).
    # Advisory only; never gates or places trades.
    impulse_flag_last_at_ms: dict[tuple, int] = field(default_factory=dict)

    # BE-ladder exit shadow — per-parent simulated SPX-point BE-ladder exit
    # state (see spx_target_watcher._shadow_be_ladder_cycle). Keyed by
    # LiveOrder parent id; in-memory only (lost on restart — acceptable for
    # an A/B exit shadow). NEVER places orders.
    shadow_be_ladder: dict[int, dict] = field(default_factory=dict)

    # Loop watchdog (2026-07-07): heartbeat map (loop name -> monotonic
    # seconds, stamped every healthy cycle), supervised-loop registry, and
    # the supervisor task handle. The week's failure class is "async task
    # dies or wedges silently" (bar writer + live_position snapshot writer
    # both stopped ~13:31Z with nothing surfaced) — the watchdog audits and
    # respawns. See engine/loop_watchdog.py.
    loop_heartbeats: dict[str, float] = field(default_factory=dict)
    watchdog_loops: dict[str, object] = field(default_factory=dict)
    watchdog_task: object | None = None

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


def _make_engine(
    *,
    interval_sec: float | None = None,
    flow_surge_gate: bool | None = None,
) -> FEFEngine:
    """Engine factory honoring env-var thresholds (IMPROVEMENTS #2 + #7).

    THE canonical FEFEngine constructor — every engine in the app (main
    loop, multi-symbol scanner, shadow) must come through here so env-tuned
    behavior applies uniformly. The scanner previously used a local 5-param
    factory: flow_surge_gate_enabled fell back to the CONSTRUCTOR default
    (True), so the flow-surge path kept firing live auto-trades from the
    scanner while the main engine had it disabled (the 7/1-7/2 fires).

    interval_sec: evaluation cadence for magnitude-threshold scaling.
    Default = FEF_LOOP_INTERVAL_SEC (main loop, 120s).

    flow_surge_gate: explicit gate override. None (main loop + shadow) =
    FEF_FLOW_SURGE_GATE_ENABLED env, default FALSE — the main engine's
    flow-surge path stays RETIRED and only logs the advisory
    'FLOW-SURGE GATE DISABLED — would be ALIGNED...' WAIT rows. The
    SCANNER passes its own explicit value (see multi_symbol_scanner.
    _make_engine_for): per the 2026-07-02 config decision, the STRICT
    scanner configuration is the ONE canonical flow-surge signaler; the
    global env flag must NOT be used to wake the main engine's actionable
    path alongside it (two engines with different effective thresholds
    firing into one account = uninterpretable mixed signal population)."""
    interval = (
        float(interval_sec) if interval_sec is not None
        else float(os.environ.get("FEF_LOOP_INTERVAL_SEC", "120"))
    )
    # Auto-scale magnitude thresholds by interval (default 120s = scale 1.0)
    threshold_scale = max(0.1, interval / 120.0)
    return FEFEngine(
        wall_proximity_pts=float(os.environ.get("FEF_WALL_PROXIMITY_PTS", "5.0")),
        # WALL GATE OFF BY DEFAULT (2026-07-02 validation): the proximity
        # skip fails the harm-reduction bar (near-wall fires are the least
        # bad; a 5pt gate removes 58.6% of fires and keeps the worse 5-10pt
        # band; the chop gate already owns the real harm region). The
        # per-family ranking fix + wall context logging remain; the SKIP is
        # explicit opt-in via this env.
        wall_proximity_skip_enabled=(
            os.environ.get("FEF_WALL_PROXIMITY_SKIP_ENABLED", "false").lower()
            in ("true", "1", "yes", "on")
        ),
        wall_collapse_pct=float(os.environ.get("FEF_WALL_COLLAPSE_PCT", "0.20")),
        wall_collapse_floor_m=float(os.environ.get("FEF_WALL_COLLAPSE_FLOOR_M", "5.0")),
        absorption_fe_ratio_min=float(os.environ.get("FEF_ABSORPTION_FE_MIN", "5.0")),
        # Flow-surge gate: adaptive rolling percentile of recent |ΔDrift|, with the
        # absolute value as a warm-up bootstrap (non-stationarity fix 2026-06-30).
        flow_surge_min_m=float(os.environ.get("FEF_FLOW_SURGE_MIN_M", "25.0")),
        flow_surge_pct=float(os.environ.get("FEF_FLOW_SURGE_PCT", "90.0")),
        flow_surge_window=int(os.environ.get("FEF_FLOW_SURGE_WINDOW", "120")),
        flow_surge_noise_floor_m=float(os.environ.get("FEF_FLOW_SURGE_NOISE_FLOOR_M", "1.0")),
        flow_surge_min_samples=int(os.environ.get("FEF_FLOW_SURGE_MIN_SAMPLES", "8")),
        # Reject corrupted-capture drift glitches (e.g. QuantData -2839M) before
        # they fire a false surge or poison the adaptive percentile.
        drift_sanity_ceiling_m=float(os.environ.get("FEF_DRIFT_SANITY_CEILING_M", "250.0")),
        # INERT by default (2026-06-30): the flow-surge directional edge was retired
        # as a spot-reconstruction artifact. The drift input still feeds bias/regime,
        # but flow-surge alignment no longer fires actionable trades from the MAIN
        # engine. Flow-surge signaling lives EXCLUSIVELY in the strict scanner
        # (FEF_SCANNER_FLOW_SURGE_ENABLED, default on) — do NOT flip this global
        # flag to trade flow-surge from the main loop as well (mixed-threshold
        # signal population; see the 2026-07-02 config decision).
        flow_surge_gate_enabled=(
            flow_surge_gate if flow_surge_gate is not None
            else os.environ.get("FEF_FLOW_SURGE_GATE_ENABLED", "false").lower()
            in ("true", "1", "yes", "on")
        ),
        threshold_scale=threshold_scale,
    )
