"""MockClock — translates real wall-clock time into simulated market time.

The single source of truth for 'how far into the trading day are we now?'
that all mock subsystems consult. Set FEF_MOCK_SPEEDUP to change how fast
simulated time advances (default 40x = ~10min real per 6.5h simulated day).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, time as dtime, timedelta, timezone

ET = timezone(timedelta(hours=-4))

# 6.5h trading day = 390 minutes
SESSION_MINUTES = 390


class MockClock:
    """Singleton clock — initialized once on import."""

    _start_real: float | None = None
    _speedup: float = 40.0

    @classmethod
    def init(cls) -> None:
        if cls._start_real is not None:
            return
        cls._start_real = time.time()
        cls._speedup = float(os.environ.get("FEF_MOCK_SPEEDUP", "40"))

    @classmethod
    def elapsed_real_seconds(cls) -> float:
        cls.init()
        return time.time() - cls._start_real  # type: ignore[operator]

    @classmethod
    def simulated_seconds_since_open(cls) -> float:
        """Returns simulated seconds since 9:30 ET. Capped at end of session."""
        elapsed_real = cls.elapsed_real_seconds()
        sim_sec = elapsed_real * cls._speedup
        return min(SESSION_MINUTES * 60, sim_sec)

    @classmethod
    def simulated_minutes_since_open(cls) -> float:
        return cls.simulated_seconds_since_open() / 60.0

    @classmethod
    def is_session_complete(cls) -> bool:
        return cls.simulated_minutes_since_open() >= SESSION_MINUTES

    @classmethod
    def speedup(cls) -> float:
        cls.init()
        return cls._speedup

    @classmethod
    def reset(cls) -> None:
        """For tests: zero out the clock so a fresh session starts."""
        cls._start_real = None
        cls._speedup = float(os.environ.get("FEF_MOCK_SPEEDUP", "40"))
