"""Persistent UI-configurable settings (IMPROVEMENTS #24).

Lives at ~/.fef-terminal/config.json. Loaded at startup, overlaid on
env-var defaults; updates from POST /config write here and hot-reload
engine thresholds. Env vars still take precedence on startup if set.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

CONFIG_PATH = Path.home() / ".fef-terminal" / "config.json"


class UIConfig(BaseModel):
    """User-tunable runtime config — settable from the UI Settings panel."""

    fef_loop_interval_sec: int = Field(default=120, ge=10, le=600)
    # [REMOVED 2026-08-25] `quantdata_poll_interval_sec` — QuantData
    # subscription CANCELLED and the poll loop it paced is deleted. UIConfig is
    # a pydantic model over a JSON file, not a DB schema, so an already-persisted
    # key in ~/.fef-terminal/config.json is ignored on load (BaseModel ignores
    # extra keys by default) — no migration needed.
    wall_proximity_pts: float = Field(default=5.0, ge=1.0, le=30.0)
    wall_collapse_pct: float = Field(default=0.20, ge=0.05, le=0.80)
    wall_collapse_floor_m: float = Field(default=5.0, ge=0.0, le=1000.0)
    # Added 2026-07-30 with the Research-tab teardown. This knob previously
    # had NO config path: the only way to change it was the Strategy Presets
    # panel, whose apply handler wrote engine._absorption_fe_min directly
    # (api.py). Retiring presets would have left it env-var/restart-only, so
    # it moves here — the canonical, audited, validated change path. Default
    # 5.0 matches the FEFEngine ctor and the Preset model, so existing
    # installs see no behaviour change on upgrade.
    absorption_fe_ratio_min: float = Field(default=5.0, ge=0.5, le=50.0)
    default_qty: int = Field(default=15, ge=1, le=100)
    audio_alerts: bool = Field(default=True)

    # Risk overlay — null = no limit
    daily_loss_cap_dollars: float | None = Field(default=None, ge=0)
    max_position_dollars: float | None = Field(default=None, ge=0)
    max_contracts_per_trade: int = Field(default=25, ge=1, le=200)

    # ---- Pattern overlay / pattern trigger sensitivity ----
    # ONE source of truth for both GET /chart/pattern_overlay and the live
    # confirmed-bar pattern trigger loop. Defaults mirror the Pine reference.
    # Hot: the trigger loop re-reads config each cycle; no restart needed.
    pattern_hammer_wick_frac: float = Field(default=0.60, ge=0.3, le=0.9)
    pattern_hammer_other_wick_max: float = Field(default=0.25, ge=0.0, le=0.5)
    pattern_soldier_body_frac: float = Field(default=0.50, ge=0.2, le=0.9)
    pattern_star_mid_body_max: float = Field(default=0.30, ge=0.05, le=0.6)
    pattern_doji_body_max: float = Field(default=0.10, ge=0.01, le=0.3)
    pattern_pole_bars: int = Field(default=3, ge=2, le=6)
    pattern_pole_min_pts: float = Field(default=8.0, ge=0.5, le=100.0)
    pattern_flag_frac: float = Field(default=0.333, ge=0.1, le=1.0)
    pattern_flag_min_bars: int = Field(default=3, ge=2, le=8)
    pattern_flag_max_bars: int = Field(default=6, ge=3, le=12)
    pattern_refire_cooldown_bars: int = Field(default=10, ge=1, le=60)
    pattern_pivot_strength: int = Field(default=10, ge=3, le=30)
    # Candle-pattern kinds eligible to AUTO-TRADE when the pattern trigger
    # types are enabled in the active preset. Flags are governed separately
    # by the flag_bull/flag_bear trigger types. Valid kinds: engulfing,
    # hammer, soldiers, star. Default: engulfing only (higher-signal subset).
    pattern_trade_kinds: list[str] = Field(default_factory=lambda: ["engulfing"])

    # ---- CHOP skip-gate (multi-symbol scanner flow-surge fires) ----
    # Harm-reduction entry filter validated by the 2026-07-02 22,683-fire
    # sweep: skip a scanner fire when the trailing 30-min SPX range sits in
    # the compressed tercile of its own time-of-day distribution over the
    # prior 20 sessions (see engine/chop_gate.py). Hot: the scanner re-reads
    # config on every actionable evaluation — tunable without restart via
    # POST /config. Enabled by default per the 2026-07-02 user decision.
    chop_gate_enabled: bool = Field(default=True)
    chop_gate_pct_threshold: float = Field(default=33.3, ge=0.0, le=100.0)

    # ---- Bias-disagree veto (multi-symbol scanner flow-surge fires) ----
    # Promoted from tag to SKIP 2026-07-02: an ALIGNED fire whose direction
    # disagrees with drift_bias (the engine's '(bias-disagrees, demoted)'
    # stamp) is skipped, not traded. Evidence: 3 of the day's 5 live losers
    # wore the tag (-$1,710); sweep OOS disagree -$17.00/ct vs agree
    # -$1.85/ct. Gate order: chop gate first, then bias veto — each skip is
    # logged distinctly so the counterfactuals never blur. Hot-read.
    bias_veto_enabled: bool = Field(default=True)

    # ---- Per-symbol dollar-exit scaling (fixed_dollar) ----
    # The operator's $ targets are calibrated on SPX-class notional. Applied
    # raw to ETF-class symbols they are unreachable ($300 T1 on a 3-lot SPY
    # position = +$1.00/ct premium = +50% on a ~$2 option ≈ 15-20 SPX-
    # equivalent points — live exhibit 2026-07-02 SPY 743C: profitable,
    # couldn't bank). When enabled, fixed_dollar targets are scaled by the
    # strike fix's notional ratio (_price_scale = spot/7500 below 2000;
    # SPX-class stays bit-identical at 1.0). Hot-read at spec-build time.
    etf_dollar_exit_scaling: bool = Field(default=True)

    # ---- FOMC-morning skip-gate (multi-symbol scanner flow-surge fires) ----
    # Evidence (2026-07-06 event study over scanner-fire history): fires on
    # FOMC statement days BEFORE the 14:00 ET statement average -$61/ct
    # (t=-4.4), with 80% of FOMC sessions negative. Gate order: chop gate ->
    # bias veto -> THIS -> TOD window (first gate to skip wins; distinct
    # audit categories so the counterfactual populations never blur). FOMC
    # dates come from engine/event_calendar.FOMC_DATES (official schedule
    # hardcoded through 2026-12; next: 2026-07-29). Hot-read per evaluation.
    fomc_gate_enabled: bool = Field(default=True)

    # ---- Time-of-day no-trade window (multi-symbol scanner fires) ----
    # User request 2026-07-06; evidence: the entry-filter sweep's worst TOD
    # bucket = 14:00-15:30 ET at -$21/ct OOS. Skips actionable scanner fires
    # whose ET clock time falls in [start, end) — start inclusive, end
    # exclusive. Counterfactual rows persist like the other gates so the
    # window grades itself in the logs. Hot-read; window edges are "HH:MM"
    # ET strings tunable via POST /config without restart.
    tod_gate_enabled: bool = Field(default=True)
    tod_gate_start_et: str = Field(default="14:00", pattern=r"^\d{2}:\d{2}$")
    tod_gate_end_et: str = Field(default="15:30", pattern=r"^\d{2}:\d{2}$")

    # ---- Auto-trade confidence floor (ARMED 2026-07-08) ----
    # THE floor the auto-trader enforces on every fire — this config value is
    # the single source of truth (state.auto_trade_confidence_floor is
    # display-legacy; /auto_trade/toggle IGNORES any floor in its body so the
    # panel can never again stomp it — observed 3+ times live). Persistent
    # across restarts, hot-read at gate time. Evidence (12k-fire registered
    # study): floor=HIGH +37.81/ct OOS vs LOW -14.84/ct (t=3.31); book flips
    # -$142k -> +$117k. Kill switch: FEF_DISABLE_CONFIDENCE_FLOOR=true
    # (fail-open, loud audit once per session).
    auto_trade_confidence_floor: Literal["LOW", "MEDIUM", "HIGH"] = Field(
        default="HIGH",
    )

    # ---- Auto-trade quantity (PERSISTENT 2026-07-09) ----
    # THE contracts-per-fire the auto-trader places — this config value is the
    # single source of truth (state.auto_trade_qty_override is display-legacy;
    # /auto_trade/toggle + presets IGNORE any qty_override in their body so the
    # panel can never again set it in-memory-only). Persistent across restarts,
    # hot-read at fire time (mirrors auto_trade_confidence_floor exactly).
    # 2026-07-09 near-miss (pre-open): qty_override lived ONLY in memory and
    # reset to 0 on restart while floor/symbols persisted; scanner verdicts
    # carry suggested_qty=None, so the gate's retired `override if >0 else
    # suggested_qty or 15` resolved a fire to 15 contracts — 5x the operator's
    # 3-lot size, and the -$1,800 catastrophe cap + every exit geometry are
    # calibrated to the chosen size. Validated 1..100; 0 and >100 are REJECTED
    # (there is no "use suggested" sentinel any more — the gate always resolves
    # a concrete size, never 15). Default 3 = the operator's live size.
    auto_trade_qty_override: int = Field(default=3, ge=1, le=100)

    # ---- Tier-scoped gate bypass (2026-07-09 registered study) ----
    # The chop / bias-veto / TOD scanner gates were built BEFORE the HIGH
    # confidence floor was armed (2026-07-08). They run ahead of the floor in
    # the chain, so now that the floor is HIGH-only they only ever pre-empt
    # HIGH-confidence fires that the floor would have PASSED — i.e. profitable
    # fires. The study attributes ~+$3,134/ct left on the table and ~35% of
    # fire frequency suppressed to that overlap; the gates' real job (culling
    # LOW/MEDIUM fires) is now fully handled by the floor. When True (default)
    # a HIGH-confidence fire BYPASSES chop / bias-veto / TOD (they still apply
    # normally to LOW/MEDIUM). The FOMC gate is deliberately NOT in the bypass
    # set — it is an event-risk guard that must bind on ALL tiers (it blocks
    # HIGH fires that lose -$28/ct on FOMC mornings). Mental model: "gates
    # apply BELOW the floor tier, not above it." Set False to restore the old
    # all-tiers behavior (every gate applies to every tier). Hot-read per
    # actionable scanner evaluation — reversible via POST /config, no restart.
    gate_high_bypass_enabled: bool = Field(default=True)

    # ---- Cross-symbol direction cap (2026-07-08) ----
    # Operational risk rule, user-approved: block a fire when a watcher-
    # managed parent (SUBMITTED/FILLED/PARTIAL) with the SAME right is
    # already open on a DIFFERENT index-complex symbol (SPX/SPY/QQQ/IWM) —
    # they are one correlated underlying trade, not diversification.
    # Evidence is anecdote-grade (+$1,380 across 2 sessions): shipped as
    # EXPOSURE DISCIPLINE, not edge. Dormant when only SPX is enabled
    # (same-symbol stacking stays governed by per-symbol cooldown/cap +
    # the duplicate-parent guard). Hot-read at fire time.
    cross_symbol_cap_enabled: bool = Field(default=True)

    # ---- Impulse attention flag (ADVISORY ONLY — never trades) ----
    # Validated 2026-07-02: |dPrice| impulse + flow-sign agreement + non-
    # compressed regime works as a real-time attention flag (caught both of
    # the day's missed moves, one 4 min before the scanner's late fire) but
    # WHIPSAWS as an auto-trigger — so it ships as decision-support only.
    # min_pts is SPX-calibrated points; ETF-class symbols scale by
    # spot/7500 (same moneyness scale as the strike fix). Hot-read each scan.
    impulse_flag_enabled: bool = Field(default=True)
    impulse_flag_min_pts: float = Field(default=6.0, ge=0.5, le=100.0)
    impulse_flag_require_agree: bool = Field(default=True)
    impulse_flag_cooldown_sec: int = Field(default=600, ge=0, le=7200)

    # ---- SPY mirror vehicle (2026-07-17) ----
    # When the SPX auto-trade path commits a fire (all gates passed, entry
    # placed), also place a SPY option mirror of the same trade: SPX signal
    # (theta flow, validated) -> SPY vehicle at the SAME percentage moneyness
    # (spot-adjusted strike map, validated 2026-07-17 spy_mapping_report.md:
    # SPY transfers the SPX book at 1/10 scale with ~zero venue friction).
    # Explicitly NOT the SPY scanner symbol — scanner enablement untouched.
    # The mirror is fail-soft (any failure skips it with an audit row, never
    # blocks the SPX fire) and rides on its OWN ledger (LiveOrder.mirror_of)
    # so the SPX verdict window stays computable excluding mirrors.
    # Kill switches: this flag (hot-read per fire) + FEF_SPY_MIRROR_ENABLED
    # env (set false/0/no/off to kill regardless of config).
    # Default ON per operator order 2026-07-17.
    spy_mirror_enabled: bool = Field(default=True)
    # Mirror size in SPY contracts. 10 = one SPX-contract-equivalent
    # (validated study ratio ~1/10 premium per contract).
    spy_mirror_qty: int = Field(default=10, ge=1, le=100)

    # ---- Scanner drift-input source (2026-07-15 input-parity fix) ----
    # Which flow feed supplies the SPX scanner engine's drift_cum_net_m input:
    #   'qd'    (DEFAULT) — QuantData snapshot drift, byte-identical to the
    #           pre-change behavior. The parity diagnostic (scratchpad
    #           parity_report.md) showed this feed runs at ~7-9% of the
    #           validated backtest input's magnitude (31x fewer >=10M 3-min
    #           surges) — but swapping is a REGISTERED-VALIDATION decision.
    #   'theta' — the live theta_flow_ingest drift (the validated
    #           option_flow_minute construction rebuilt in real time). Falls
    #           back to 'qd' input when the theta read is stale/absent
    #           (fail-soft; audited once per session).
    # FLIP ONLY after >=5 sessions of flow_shadow evidence (construction match
    # + HIGH-eligible rate in the validated 1-6/day band) — human decision.
    # SCOPE: governs the SCANNER's SPX engine only (the canonical flow-surge
    # signaler). The main fef_tick_loop engine keeps QD inputs; its flow-surge
    # path is retired (advisory-only) — never arm 'theta' here together with
    # FEF_FLOW_SURGE_GATE_ENABLED=true on the main engine.
    #
    # [REMOVED 2026-08-25] QuantData subscription CANCELLED. This key is now
    # INERT: its only reader was multi_symbol_scanner._apply_flow_source_and_shadow,
    # reached from `_scan_one_symbol`, which is deleted along with the scanner
    # loop. The key is KEPT (not removed) because it is persisted in
    # ~/.fef-terminal/config.json and the frontend Settings panel still round-
    # trips the whole UIConfig object; dropping it is a separate, safe cleanup.
    # Neither value does anything today.
    flow_source: Literal["qd", "theta"] = Field(default="qd")

    # ---- SPX auto-trade entry order type (2026-07-22 operator decision) ----
    # "just go back to market orders on SPX. order hangup is just what will
    # come with it." — operator, reverting the entry-order arc:
    #   7/16: raw MKT entry hung 4m20s on IBKR's price cap (live_order 776,
    #         filled $8.10 vs ~$5.50-6.00 at signal) -> marketable-LMT era.
    #   7/20: static marketable LMT missed momentum fires (1-of-5 SPX HIGH)
    #         -> bounded chase; 7/21: chase re-fetch starved at fire time ->
    #         streaming fire-time quotes. Net across the three limit-era
    #         sessions: 2-of-8 momentum fires filled — the hang was solved
    #         but the entry stopped doing its job (filling on momentum).
    # "market" (DEFAULT): auto-trade entries ride the EXISTING static-MKT
    #         path (BracketSpec.use_market_entry=True): no chase, no
    #         streaming subscription, _mk_market places the parent. The
    #         hang risk is accepted AND BOUNDED: the 45s entry-timeout
    #         watchdog (entry_timeout.py — did not exist on 7/16) arms on
    #         the MKT parent via the existing non-chase derivation and
    #         cancels a rested order. If the operator disables the timeout
    #         (FEF_ENTRY_TIMEOUT_SEC<=0) the auto-trader REFUSES the naked
    #         MKT and downgrades to chase_limit behavior — the same
    #         no-unwatched-MKT invariant the chase's fallback enforces.
    # "chase_limit": the 7/20-7/21 behavior, kept dormant (not removed):
    #         marketable LMT + bounded chase + streaming quote + MKT
    #         fallback under the same watchdog.
    # Scope: the AUTO-TRADE entry path — scanner/main-loop fires AND the
    # SPY mirror (SPY fills trivially at MKT on its penny grid). Manual
    # tickets untouched. Gate 9.5 (fresh-quote resolution) still runs under
    # "market": a fire with no usable quote stays BLOCKED (NO_FRESH_QUOTE)
    # and the resolved ask still feeds the audit trail + the mirror's
    # premium-plausibility bound; the marketable limit becomes informational
    # (stored on the row, not sent). Hot-read at fire time via load_config().
    spx_entry_order_type: Literal["market", "chase_limit"] = Field(
        default="market",
    )

    # ---- Scanner enabled-symbols persistence (2026-07-17 boot-stomp fix) ----
    # state.scan_enabled_symbols is in-memory only and resets to all-False on
    # every process start, so a restart silently dropped the operator's SPX pin
    # (re-pinned by hand every morning; a stray relaunch also re-enabled the
    # 6-symbol default). Persist the enablement here: POST /scanner/symbols
    # writes it, lifespan restores it on boot. ARMING is deliberately NOT
    # persisted — auto_trade_enabled stays fail-safe OFF on boot so a crashed
    # trader is re-armed only by explicit action, never silently on restart
    # (matches the operator's deliberate morning-arm discipline; a silent
    # auto-resume of live trading after a crash is the failure mode to avoid).
    scan_enabled_symbols: dict[str, bool] = Field(
        default_factory=lambda: {
            "SPX": False, "SPY": False, "QQQ": False, "IWM": False,
            "GLD": False, "SMH": False, "TLT": False,
        })


def load_config() -> UIConfig:
    if not CONFIG_PATH.exists():
        return UIConfig()
    try:
        raw = json.loads(CONFIG_PATH.read_text())
        return UIConfig.model_validate(raw)
    except Exception:
        return UIConfig()


def config_file_status() -> str:
    """'missing' | 'invalid' | 'ok' — the trace load_config() swallows.

    load_config() silently returns defaults for BOTH a fresh install
    (legitimate) and a corrupt/truncated file (a config OUTAGE that silently
    changes enforced policy — 2026-07-08 review F1: a corrupt config.json
    would enforce the default HIGH floor forever with zero trace,
    indistinguishable from a deliberate setting). Consumers that enforce
    policy from config (the confidence-floor gate) probe this so
    exists-but-invalid gets AUDITED while missing stays quiet."""
    if not CONFIG_PATH.exists():
        return "missing"
    try:
        UIConfig.model_validate(json.loads(CONFIG_PATH.read_text()))
        return "ok"
    except Exception:
        return "invalid"


def save_config(cfg: UIConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")


def merge_partial(current: UIConfig, updates: dict[str, Any]) -> UIConfig:
    """Update only the fields present in updates; return a new validated UIConfig."""
    merged = current.model_dump() | updates
    return UIConfig.model_validate(merged)
