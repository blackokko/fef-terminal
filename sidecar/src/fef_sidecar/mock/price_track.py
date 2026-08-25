"""PriceTrack — the single source of truth for SPX price in mock mode.

Uses the existing archetype day script (5_15_REPLAY / STEADY_BULL / etc.)
for the SHAPE of the day, but exposes a smooth interpolated price as a
function of simulated time. Every mock subsystem (bars, ticks,
microstructure) should call PriceTrack.price_now() so they all agree.

Also exposes derived state (drift, walls, aggression) that evolves
coherently with the price arc.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from fef_sidecar.mock.clock import MockClock
from fef_sidecar.mock.mock_day import DAY_SCRIPT


@dataclass
class PriceTrack:
    """Wraps DAY_SCRIPT with interpolation + derived state queries.

    DAY_SCRIPT has 195 frames spaced 2 minutes apart covering 9:30–16:00 ET.
    Every accessor takes a simulated minute (0-390) and returns the value
    at that point.
    """

    def __post_init__(self) -> None:
        self._frames = list(DAY_SCRIPT)
        # Stable noise for tick-level price wiggle so successive calls at
        # very-close simulated times don't flicker
        self._noise_rng = random.Random(42)
        self._last_noise: dict[int, float] = {}

    # -- core lookup --

    def _frame_pair(self, minute: float) -> tuple[object, object, float]:
        """Return (frame_a, frame_b, fractional_position) for interpolation."""
        if not self._frames:
            return (None, None, 0.0)
        idx = minute / 2.0  # frames are 2 minutes apart
        i = int(idx)
        if i >= len(self._frames) - 1:
            return (self._frames[-1], self._frames[-1], 0.0)
        if i < 0:
            return (self._frames[0], self._frames[0], 0.0)
        return (self._frames[i], self._frames[i + 1], idx - i)

    def price_at_minute(self, minute: float) -> float:
        a, b, frac = self._frame_pair(minute)
        if a is None:
            return 0.0
        if frac == 0.0:
            return a.spx
        return a.spx + (b.spx - a.spx) * frac

    def price_now(self) -> float:
        """Current SPX price. THE single source of truth for mock mode."""
        return self.price_at_minute(MockClock.simulated_minutes_since_open())

    def price_with_tick_noise(self) -> float:
        """Same as price_now() but with tiny ±$0.25 noise that's stable for
        one real second (so two callers within the same second see the same
        price, but consecutive seconds tick around naturally)."""
        import time as _t
        bucket = int(_t.time())  # one bucket per second
        if bucket not in self._last_noise:
            # Clear old noise to bound memory
            for k in list(self._last_noise.keys())[:-10]:
                del self._last_noise[k]
            self._last_noise[bucket] = round(self._noise_rng.uniform(-0.25, 0.25), 2)
        return round(self.price_now() + self._last_noise[bucket], 2)

    # -- derived state for the mock day arc --

    def drift_at_minute(self, minute: float) -> float:
        a, _, _ = self._frame_pair(minute)
        if a is None:
            return 0.0
        return getattr(a, "drift_cum_net_m", 0.0)

    def drift_now(self) -> float:
        return self.drift_at_minute(MockClock.simulated_minutes_since_open())

    def aggression_now(self) -> tuple[float, float]:
        """Returns (call_pct, put_pct) — bullish biased when price rising,
        bearish biased when falling. Computed from short-term price slope."""
        now_min = MockClock.simulated_minutes_since_open()
        now = self.price_at_minute(now_min)
        prev = self.price_at_minute(max(0, now_min - 5))
        slope = now - prev
        # ±5pts → 100%/0% extreme, linear scaling between
        bias = max(-1.0, min(1.0, slope / 5.0))
        call_pct = round(50 + bias * 20, 1)
        put_pct = round(50 - bias * 20, 1)
        return (call_pct, put_pct)

    def session_open_price(self) -> float:
        return self._frames[0].spx if self._frames else 0.0

    def session_high(self) -> float:
        if not self._frames:
            return 0.0
        now_min = MockClock.simulated_minutes_since_open()
        end_idx = min(len(self._frames), int(now_min / 2) + 1)
        return max((f.spx for f in self._frames[:end_idx]), default=self._frames[0].spx)

    def session_low(self) -> float:
        if not self._frames:
            return 0.0
        now_min = MockClock.simulated_minutes_since_open()
        end_idx = min(len(self._frames), int(now_min / 2) + 1)
        return min((f.spx for f in self._frames[:end_idx]), default=self._frames[0].spx)

    # -- bar synthesis --

    def build_bars(self, *, bar_size_minutes: int = 1, up_to_minute: float | None = None) -> list[dict]:
        """Build a list of bars from session open up to `up_to_minute` at the
        given bar size. Each bar's OHLC = sampled from the interpolated track
        across its time window. Used by the /bars endpoint."""
        from datetime import datetime, timedelta

        now_min = up_to_minute if up_to_minute is not None else MockClock.simulated_minutes_since_open()
        if now_min <= 0:
            return []

        # Anchor real timestamps so the LAST bar is at real-now and
        # earlier bars step back by bar_size_minutes / speedup real seconds
        speedup = MockClock.speedup()
        real_seconds_per_simulated_minute = 60.0 / speedup
        real_seconds_per_bar = bar_size_minutes * real_seconds_per_simulated_minute
        now_real = datetime.now()

        out: list[dict] = []
        bar_idx = 0
        cur_min = 0.0
        while cur_min < now_min:
            end_min = min(now_min, cur_min + bar_size_minutes)
            # Sample 5 points across the window for OHLC
            samples = []
            step = (end_min - cur_min) / 5
            for k in range(6):
                samples.append(self.price_at_minute(cur_min + k * step))
            open_ = samples[0]
            close = samples[-1]
            high = max(samples)
            low = min(samples)
            # Volume from the underlying frame's value at this minute
            a, _, _ = self._frame_pair(cur_min)
            volume = getattr(a, "volume", 1500) if a else 1500

            # Real-time timestamp for this bar: now - ((total_bars - bar_idx - 1) * real_seconds_per_bar)
            # We'll compute total_bars below and patch
            out.append({
                "_cur_min": cur_min,
                "open": round(open_, 2),
                "high": round(high + 0.2, 2),  # tiny wick
                "low": round(low - 0.2, 2),
                "close": round(close, 2),
                "volume": int(volume),
            })
            cur_min = end_min
            bar_idx += 1

        # Assign real timestamps so the LAST bar timestamp is now,
        # earlier bars step back. This guarantees ticks (real-now) align
        # with the last bar.
        total = len(out)
        for i, bar in enumerate(out):
            from datetime import timedelta as _td
            offset_back_seconds = (total - 1 - i) * real_seconds_per_bar
            ts = now_real - _td(seconds=offset_back_seconds)
            bar["timestamp_ms"] = int(ts.timestamp() * 1000)
            del bar["_cur_min"]
        return out


_track: PriceTrack | None = None


def get_track() -> PriceTrack:
    global _track
    if _track is None:
        _track = PriceTrack()
    return _track


def reset_track() -> None:
    global _track
    _track = None
    MockClock.reset()
