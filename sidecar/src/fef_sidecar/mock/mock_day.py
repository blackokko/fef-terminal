"""Facade over the multi-archetype day generator.

The actual logic lives in archetypes.py. Pick which archetype via
FEF_MOCK_ARCHETYPE env var (default 5_15_REPLAY). Set FEF_MOCK_SEED=0
for fresh stochastic generation each run.
"""

from __future__ import annotations

from fef_sidecar.mock.archetypes import DayFrame, build_day_script

# Generated once at import. Restart the sidecar to switch archetypes.
DAY_SCRIPT: list[DayFrame] = build_day_script()


def total_frames() -> int:
    return len(DAY_SCRIPT)


def frame_at(index: int) -> DayFrame:
    """Clamp at last frame — never wraps (which would regress timestamps)."""
    if index >= len(DAY_SCRIPT):
        return DAY_SCRIPT[-1]
    return DAY_SCRIPT[index]


def replay_complete(index: int) -> bool:
    return index >= len(DAY_SCRIPT)
