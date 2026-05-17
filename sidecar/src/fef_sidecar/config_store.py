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
