"""Pre-built strategy presets for different market conditions.

Each preset is a named threshold set that hot-applies to the live FEF engine
via POST /presets/{id}/apply. The user can extend or override these from the
Strategy Presets panel.

Tuned for SPX 0DTE; values shift with market type:
- Pin days: tighter proximity (more skips), higher wall_collapse floor
- Trend days: looser proximity (let trades through), lower collapse floor
- Vol days: wider proximity (avoid noise), high collapse floor + absorption FE
- Chop days: extra tight everything (almost nothing fires)
- Power hour: balanced thresholds, tighter proximity for end-of-day pin
"""

DEFAULT_PRESETS: tuple[dict, ...] = (
    {
        "name": "Default 2-min",
        "description": "Standard thresholds tuned for 120s FEF cadence — start here",
        "fef_loop_interval_sec": 120,
        "wall_proximity_pts": 5.0,
        "wall_collapse_pct": 0.20,
        "wall_collapse_floor_m": 5.0,
        "absorption_fe_ratio_min": 5.0,
    },
    {
        "name": "Tight Pin Day",
        "description": "Low-vol pin regime — tighter proximity skips, higher collapse floor",
        "fef_loop_interval_sec": 120,
        "wall_proximity_pts": 3.0,
        "wall_collapse_pct": 0.30,
        "wall_collapse_floor_m": 10.0,
        "absorption_fe_ratio_min": 8.0,
    },
    {
        "name": "Trend Day Aggressive",
        "description": "Clear directional day — let signals through, lower absorption bar",
        "fef_loop_interval_sec": 60,
        "wall_proximity_pts": 2.0,
        "wall_collapse_pct": 0.15,
        "wall_collapse_floor_m": 3.0,
        "absorption_fe_ratio_min": 4.0,
    },
    {
        "name": "Volatile / Fed Day",
        "description": "High vol, wide ranges — wider proximity to avoid noise, higher collapse bar",
        "fef_loop_interval_sec": 120,
        "wall_proximity_pts": 8.0,
        "wall_collapse_pct": 0.25,
        "wall_collapse_floor_m": 10.0,
        "absorption_fe_ratio_min": 6.0,
    },
    {
        "name": "Chop / Sit Out",
        "description": "No clear edge — set thresholds high so almost nothing fires",
        "fef_loop_interval_sec": 120,
        "wall_proximity_pts": 15.0,
        "wall_collapse_pct": 0.40,
        "wall_collapse_floor_m": 20.0,
        "absorption_fe_ratio_min": 10.0,
    },
    {
        "name": "0DTE Power Hour",
        "description": "Last 90min of session — balanced, tighter proximity for closing pin",
        "fef_loop_interval_sec": 60,
        "wall_proximity_pts": 4.0,
        "wall_collapse_pct": 0.20,
        "wall_collapse_floor_m": 5.0,
        "absorption_fe_ratio_min": 5.0,
    },
)
