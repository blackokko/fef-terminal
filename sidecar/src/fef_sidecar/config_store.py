"""Persistent UI-configurable settings (IMPROVEMENTS #24).

Lives at ~/.fef-terminal/config.json. Loaded at startup, overlaid on
env-var defaults; updates from POST /config write here and hot-reload
engine thresholds. Env vars still take precedence on startup if set.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

CONFIG_PATH = Path.home() / ".fef-terminal" / "config.json"


class UIConfig(BaseModel):
    """User-tunable runtime config — settable from the UI Settings panel."""

    fef_loop_interval_sec: int = Field(default=120, ge=10, le=600)
    quantdata_poll_interval_sec: int = Field(default=15, ge=5, le=120)
    wall_proximity_pts: float = Field(default=5.0, ge=1.0, le=30.0)
    wall_collapse_pct: float = Field(default=0.20, ge=0.05, le=0.80)
    wall_collapse_floor_m: float = Field(default=5.0, ge=0.0, le=1000.0)
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


def load_config() -> UIConfig:
    if not CONFIG_PATH.exists():
        return UIConfig()
    try:
        raw = json.loads(CONFIG_PATH.read_text())
        return UIConfig.model_validate(raw)
    except Exception:
        return UIConfig()


def save_config(cfg: UIConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")


def merge_partial(current: UIConfig, updates: dict[str, Any]) -> UIConfig:
    """Update only the fields present in updates; return a new validated UIConfig."""
    merged = current.model_dump() | updates
    return UIConfig.model_validate(merged)
