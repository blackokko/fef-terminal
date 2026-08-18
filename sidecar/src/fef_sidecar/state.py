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
    # Which symbol's feed last wrote last_live_tick_spx / last_spx_price
    # (2026-07-23 incident review MAJOR-2): consumers that trust the "active
    # tick" for a symbol must check this stamp — after a /config/symbol flip
    # the cached value belongs to the PREVIOUS symbol until the new stream's
    # first print lands.
    last_live_tick_symbol: str | None = None
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
    # DISPLAY-LEGACY (2026-07-09): the auto-trader NO LONGER reads this — the
    # traded contracts-per-fire is the PERSISTENT config_store value
    # (UIConfig.auto_trade_qty_override, hot-read via auto_trader._config_qty),
    # mirroring how the confidence floor was moved to config. This in-memory
    # field reset to 0 on restart while floor/symbols persisted; with the old
    # `override if >0 else suggested_qty or 15` resolution a scanner fire then
    # placed 15 contracts (5x the operator's 3-lot). The panel toggle + presets
    # still set this for backward-compat display, but it never sizes a trade.
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
    # --- credit-spread structure. Defaults are the MEASURED-BEST values, not the
    # playbook ones. Every prior default was the worst option on its own axis
    # (research archive, 2026-08-10/11). Set them back explicitly to restore the
    # original playbook behaviour; nothing here changes any logic.
    #
    #   width 5 -> 10        edge/friction 1.47x -> 2.59x (`touchsell2`); `jointopt`
    #                        found widest wins at EVERY tenor, 4 of 4 rectangles.
    #   premium_min 0.70 -> 0  a $0.70 floor on a 5-wide means credit/width >= 14%,
    #                        so the bot silently refused to trade on calm days and
    #                        traded on volatile ones. An UNDESIGNED vol filter.
    #   puts_only            call spreads are negative even at MID in BOTH eras
    #                        (`tds_sweep` S5); 1DTE calls miscalibrate the wrong way.
    #   require_gate False   0 of 236 entry gates beat "sell every session" (`vrpsig`),
    #                        and gating fraction r forfeits sqrt(r) of Sharpe outright.
    #   delta_max            0.15 wins on mean $/ct, 0.08 wins on Sortino (1.460 vs
    #                        1.146, `jointopt`). Left at 0.15 pending the operator.
    spread_width: float = 10.0
    spread_delta_max: float = 0.15
    spread_premium_min: float = 0.0
    spread_puts_only: bool = True
    spread_require_gate: bool = False

    # --- strike-selection guards (added 2026-08-14 after the 0.0329-delta pick)
    # ⚠ These MUST stay equal to the DEFAULT_* constants in engine/strike_picker.py.
    # The whole incident started with that divergence: the module said
    # premium_min=0.70 while this file passed 0.0, so the only credit guard in
    # the system had been switched off for the auto-trader's entire life and the
    # module docstring still advertised it.
    #
    #   delta_floor_frac   The short strike must be NEAREST spread_delta_max, not
    #                      merely under it. Floor = spread_delta_max * this, so at
    #                      0.15 the accepted band is [0.075, 0.15]. Anything below
    #                      is REJECTED (the old code's `delta_max * 0.5` test sat
    #                      after the accept test and could never fire).
    #   min_quote_coverage Fraction of scanned strikes that must return a delta
    #                      before "nearest" means anything. The 8/12 scan saw two
    #                      of six and picked from those two.
    #   min_credit_width_  Net credit floor as a fraction of WIDTH so it scales
    #     ratio            instead of becoming an accidental vol filter the way a
    #                      flat $0.70 on a 5-wide did (= 14%). 5% is a
    #                      "cannot pay its own commission" backstop: the rejected
    #                      8/12 spread was 1.55% of width, with commission at
    #                      16.5% of gross credit. A true 0.15Δ runs 15-25%.
    #   min_net_credit     Absolute per-share floor, so a narrow width cannot
    #                      ratio its way down to pennies.
    spread_delta_floor_frac: float = 0.5
    spread_min_quote_coverage: float = 0.5
    spread_min_credit_width_ratio: float = 0.05
    spread_min_net_credit: float = 0.10

    # HOLD TO EXPIRY is the measured-best exit: it beat the 4x-stop + $0.05-TP pair
    # in 49 of 72 cells, and that exact pair ranked 6 of 6 in `touchsell2`. Exiting
    # on a touch costs 43-68% of the whole expectation, and on a defined-risk spread
    # the long leg IS the stop -- it was already bought and paid for.
    # Disable via the sentinels below; the watcher needs no code change.
    #   stop  off: spread_stop_multiplier >= 1e9   (debit never reaches it)
    #   TP    off: spread_tp_close_debit  <= 0.0   (debit is always > 0)
    spread_stop_multiplier: float = 1e9
    spread_tp_close_debit: float = 0.0

    # --- spread auto-trade. OFF by default; this is the flag that makes the bot
    # actually place credit spreads rather than only recommend them. One position
    # per session, fixed qty. See engine/spread_auto_trader.py for every gate.
    #
    # What it harvests: options over-price the probability of touching the short
    # strike by 7.25pp (t = +7.36, both eras) -- a risk premium, not a forecast.
    # What it is measuring: the adverse-selection cost of insisting on a mid fill,
    # estimated at -$16.34/ct with a CI of [-156, +84] -- wider than the edge, and
    # a MODEL, because no order was ever actually resting there. Sixty sessions of
    # real attempts collapses that band; no amount of backtesting can.
    # ENABLED 2026-08-12 at the operator's direction. Target is the IBKR PAPER
    # account (DU098107); `require_paper_account()` is a code-level guard that
    # refuses any account id not starting with "DU", independent of every env
    # var, so no configuration of this path reaches real money.
    #
    # ⚠ DEFAULT FLIPPED True -> False 2026-08-17, and the reason is a safety
    # ASYMMETRY, not caution for its own sake. The directional bot requires a
    # per-session UI toggle before it can fire; this module had no API route and
    # no UI, so `True` here meant "armed the instant the process starts" — MORE
    # automatic than the bot it sits beside, and unflippable without a restart.
    # Now that GET/POST /spread_auto_trade/* exist, the launcher flag
    # (FEF_ALLOW_CREDIT_SPREADS) is the CAPABILITY and this toggle is the INTENT,
    # which is exactly how auto_trade_enabled behaves. Two levels, not one.
    spread_auto_trade_enabled: bool = False
    # OBSERVE-ONLY. Runs every gate against live data and records exactly what it
    # WOULD have placed and every reason it did not — but sends nothing. Default
    # True so that enabling auto-trade cannot, by itself, put on a position: the
    # operator must clear BOTH flags. Adversarial review (2026-08-11) returned
    # DO NOT SHIP with five open findings, three of which can establish a position
    # nobody intended; none of them apply while nothing is sent.
    #
    # It is not a stub. It starts the attempt ledger — INCLUDING THE SKIPS, which
    # are the control group the whole fill measurement depends on — and exercises
    # every gate against real market data, which is how the remaining bugs surface
    # before an order exists rather than after.
    spread_auto_trade_observe_only: bool = False
    spread_auto_trade_qty: int = 1
    spread_auto_trade_last_date: str = ""   # ET date of the last placement, "" = none
    # Max age of a recommendation before the auto-trader refuses it. The loop
    # does NOT refresh last_spread_recommendation when the bar fetch returns
    # empty, so without this a stale structure stays tradeable indefinitely.
    spread_rec_max_age_sec: float = 300.0
    # --- observability (2026-08-17) ------------------------------------------
    # Why the last evaluation did or did not fire. On 2026-08-12 answering that
    # question took an hour of hand-querying signal_log.db, because the decision
    # was returned to the loop, logged, and then discarded. /auto_trade/status
    # has exposed `last_block_reason` for months; this is the same idea, and its
    # absence here is why a self-refused order silently burned a whole session.
    # Diagnostics only — nothing in the gate stack reads these.
    spread_auto_trade_last_reason: str = ""
    spread_auto_trade_last_reason_code: str = ""
    spread_auto_trade_last_eval_at_ms: int = 0
    # Placements this process has made (NOT a P&L counter, and NOT the dedupe —
    # the once-per-session guarantee is enforced by spread_auto_trade_last_date
    # plus the DB check, both of which survive a restart. This does not.)
    spread_auto_trade_count_session: int = 0
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

    # Generalized historical-bar cache keyed by (symbol, bar_size, duration).
    # Backs BOTH the credit-spread recommender's 30-min/5-D TA fetch (EMA
    # 8/21, StdDev channel, engulfing) AND the chart regression-channel
    # overlay. Value = (fetched_at_ms, bars). Re-fetched at most every
    # 25 minutes per key so freshly closed bars get picked up without
    # hammering IBKR. Read-only DISPLAY/TA data — NOT part of the trade path.
    bars_cache: dict[tuple[str, str, str], tuple[int, list[object]]] = field(default_factory=dict)
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

    # Lo-VIX ladder-shrink exit shadow (2026-07-07 registered backtest,
    # shadow-first) — the SECOND shadow spx_points parents carry on lo-VIX
    # days: the same plan with all four offsets scaled by m=0.6. Separate
    # dict so both (parent, variant) rows coexist; same lifecycle as
    # shadow_be_ladder. NEVER places orders.
    shadow_lowvix: dict[int, dict] = field(default_factory=dict)

    # Lo-VIX day classifier — per-session cached LoVixAssessment
    # (engine/lo_vix_day.py). Keyed by ET session_date; one DB read + one
    # audit row per session. Tag/shadow input only — never gates.
    lo_vix_day_cache: dict[str, object] = field(default_factory=dict)
    # Newest VIX daily session (ET date) the ingest has committed this process
    # (engine/vix_daily_ingest.py). Advisory monotonic marker read by
    # assess_lo_vix to close the SYMMETRIC boot race: a consumer that suspended
    # at query_bars on the pre-insert series can detect that a fresher basis
    # landed during its read and recompute BEFORE caching a stale label — the
    # cache-invalidation alone can't catch a mid-computation consumer (it runs
    # against a still-empty cache). None until the first ingest inserts.
    vix_latest_ingested_date: str | None = None

    # ---- Live ThetaData flow ingest (2026-07-15 parity fix, Phase B) ----
    # Holder dict published by engine/theta_flow_ingest.theta_flow_ingest_loop
    # every cycle: {enabled, stale, session_date, drift_theta_cum_m,
    # drift_theta_d3_m, last_final_minute_ms, ...}. Consumers: the scanner's
    # flow_shadow audit row, the flow_source='theta' swap (default OFF) and
    # GET /theta_flow/status. None until the loop's first cycle.
    theta_flow: dict | None = None
    theta_flow_task: object | None = None
    # Rolling (ts_ms, qd_drift_m) samples appended by the SPX scanner scan —
    # gives the flow_shadow row its QD-side 3-minute delta. Pruned to ~10 min.
    flow_shadow_qd_history: list = field(default_factory=list)

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


def reset_active_tick_state(state: "AppState") -> None:
    """Zero the active-chart tick cache on a /config/symbol switch
    (2026-07-23 incident review MAJOR-2): the cached last_live_tick_spx /
    last_spx_price belong to the PREVIOUS symbol — serving them as the new
    symbol's "active tick" is exactly how a frozen ES 7473.75 became the SPX
    spot and fired a false T1. Zeroed, the poll loop idles on spot<=0 and the
    per-symbol scanner-snapshot resolution takes over until the new streams'
    first prints land (which also re-stamp last_live_tick_symbol)."""
    state.last_live_tick_spx = 0.0
    state.last_spx_price = 0.0
    state.last_live_tick_ms = 0
    state.last_live_tick_symbol = None
