"""Mock IBKR provider that replays a scripted sequence of bars.

Lets the frontend chart and FEF engine run end-to-end without a live TWS
connection. Default sequence is a synthetic SPX 5/15 morning replay.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from fef_sidecar.ibkr.models import AccountSummary, Bar, Contract, ContractKind, OptionQuote, Quote


def _default_spx_5_15_bars() -> list[Bar]:
    """Synthetic 2-min bars replicating the 5/15 morning pin → absorption → breakout arc."""
    start = datetime(2026, 5, 15, 9, 30)
    pattern = [
        # (open, high, low, close)
        (7420, 7422, 7418, 7420),
        (7420, 7421, 7416, 7418),
        (7418, 7419, 7414, 7416),
        (7416, 7418, 7410, 7412),  # absorption begins
        (7412, 7414, 7406, 7408),
        (7408, 7410, 7404, 7409),
        (7409, 7413, 7407, 7412),  # bottoming
        (7412, 7416, 7411, 7415),
        (7415, 7420, 7414, 7419),
        (7419, 7424, 7418, 7423),  # breakout
        (7423, 7428, 7421, 7427),
        (7427, 7432, 7425, 7430),
        (7430, 7435, 7428, 7434),
        (7434, 7438, 7432, 7436),
        (7436, 7440, 7434, 7439),
    ]
    bars: list[Bar] = []
    for i, (o, h, low, c) in enumerate(pattern):
        bars.append(
            Bar(
                timestamp=start + timedelta(minutes=2 * i),
                open=float(o),
                high=float(h),
                low=float(low),
                close=float(c),
                volume=1500 + i * 100,
            )
        )
    return bars


class MockIBKRProvider:
    """In-memory bar replayer matching IBKRProvider's interface."""

    def __init__(
        self,
        bars: list[Bar] | None = None,
        *,
        playback_interval: float = 0.0,
        use_coherent_track: bool | None = None,
    ) -> None:
        """
        Args:
            bars: Scripted bar sequence (test-mode: yields these once then ends).
                If None, the coherent PriceTrack streams indefinitely (production mode).
            playback_interval: Seconds between yielded bars in test mode.
            use_coherent_track: Force track mode on/off. Default None = auto
                (track when bars is None, replay when bars provided).
        """
        self._bars_explicit = bars is not None
        self._bars = bars if bars is not None else _default_spx_5_15_bars()
        self._playback_interval = playback_interval
        self._connected = False
        self._scripted_index = 0  # legacy — kept for backwards compat with /bars endpoint
        # Coherent track mode is opt-in via env (production) or explicit ctor arg.
        # Default to OFF so existing test assertions (15 default bars, last=7439)
        # continue to hold.
        import os as _os
        env_coherent = _os.environ.get("FEF_MOCK_COHERENT", "false").lower() in ("true", "1", "yes", "on")
        self._use_coherent_track = use_coherent_track if use_coherent_track is not None else env_coherent

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def stream_bars(
        self,
        contract: Contract,
        *,
        bar_size: str = "2 mins",
        duration: str = "1 D",
    ) -> AsyncIterator[Bar]:
        """Production mode (default): emit bars from the coherent PriceTrack
        at REAL timestamps so chart bars + live ticks always align.
        Test mode (explicit bars=...): replay the passed bars once and exit."""
        if not self._connected:
            await self.connect()

        # Test path — explicit bars replayed once
        if not self._use_coherent_track:
            for bar in self._bars:
                yield bar
                if self._playback_interval > 0:
                    await asyncio.sleep(self._playback_interval)
            return

        import os as _os
        from datetime import datetime as _dt

        from fef_sidecar.mock import MockClock, get_track

        track = get_track()
        bar_size_minutes = {
            "1 min": 1, "2 mins": 2, "5 mins": 5, "15 mins": 15, "30 mins": 30,
            "1 hour": 60, "4 hours": 240, "1 day": 390,
        }.get(bar_size, 1)

        # Real seconds per bar = bar_size_minutes / speedup
        real_seconds_per_bar = max(0.5, bar_size_minutes * 60 / MockClock.speedup())

        last_yielded_minute = -1.0
        while True:
            sim_min = MockClock.simulated_minutes_since_open()
            # Emit a new bar each time we cross a bar boundary
            current_bar_idx = int(sim_min // bar_size_minutes)
            if current_bar_idx > int(last_yielded_minute // bar_size_minutes):
                bar_end_min = current_bar_idx * bar_size_minutes
                bar_start_min = bar_end_min - bar_size_minutes
                if bar_start_min >= 0:
                    open_ = track.price_at_minute(bar_start_min)
                    close = track.price_at_minute(bar_end_min)
                    # Sample 5 points for high/low
                    samples = [track.price_at_minute(bar_start_min + bar_size_minutes * k / 5)
                               for k in range(6)]
                    yield Bar(
                        timestamp=_dt.now(),
                        open=round(open_, 2),
                        high=round(max(samples) + 0.2, 2),
                        low=round(min(samples) - 0.2, 2),
                        close=round(close, 2),
                        volume=1500,
                    )
                    last_yielded_minute = bar_end_min
            if MockClock.is_session_complete():
                # Wait then re-check (in case speedup changes)
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(real_seconds_per_bar)

    async def _scripted_day_bars(self) -> AsyncIterator[Bar]:
        """Generate bars from the DAY_SCRIPT — ONE-SHOT replay.

        After all 190 frames are yielded, the generator exits and stays
        exited (state in self._scripted_bars_done). The outer bar loop will
        sleep + retry, but subsequent calls return an empty generator so
        we never go backwards in time on the chart.

        Each frame's SPX becomes the close; open is the previous close;
        high/low add small random wiggle; timestamps anchored to today's
        market open + frame.minutes_since_open.
        """
        from datetime import datetime, time, timedelta
        import os as _os
        import random as _random
        from fef_sidecar.quantdata.mock_day import DAY_SCRIPT

        if getattr(self, "_scripted_bars_done", False):
            return  # already replayed — don't re-yield with same timestamps

        interval = float(_os.environ.get("FEF_LOOP_INTERVAL_SEC", "3"))
        rng = _random.Random(123)

        today = datetime.now().date()
        market_open = datetime.combine(today, time(9, 30))

        prev_close = DAY_SCRIPT[0].spx
        for i, frame in enumerate(DAY_SCRIPT):
            close = frame.spx
            open_ = prev_close
            wiggle_high = rng.uniform(0.1, 1.5)
            wiggle_low = rng.uniform(0.1, 1.5)
            high = max(open_, close) + wiggle_high
            low = min(open_, close) - wiggle_low
            # Use archetype's volume profile (U-shape: spike at open/close,
            # midday lull). Falls back to base volume if attr missing.
            volume = getattr(frame, "volume", 1500) + rng.randint(-100, 200)
            self._scripted_index = i
            yield Bar(
                timestamp=market_open + timedelta(minutes=frame.minutes_since_open),
                open=round(open_, 2),
                high=round(high, 2),
                low=round(low, 2),
                close=round(close, 2),
                volume=volume,
            )
            await asyncio.sleep(interval)
            prev_close = close

        self._scripted_bars_done = True

    async def fetch_historical_bars(
        self,
        contract: Contract,
        *,
        bar_size: str,
        duration: str,
        use_rth: bool = False,
        what_to_show: str = "TRADES",
        timeout_seconds: float = 20.0,
    ) -> list[Bar]:
        """Synthetic 30-min (or other) bar series for spread-recommender
        testing. Generates ~80 bars walking from a base price with a mild
        trend and modest noise so the EMA cross + StdDev channel logic has
        realistic data to evaluate.
        """
        import random as _random
        if not self._connected:
            await self.connect()

        try:
            target_minutes = int(bar_size.split()[0]) if "min" in bar_size else 30
        except Exception:
            target_minutes = 30

        if self._use_coherent_track:
            from fef_sidecar.mock import get_track
            base = float(get_track().price_now())
        elif self._bars:
            base = float(self._bars[-1].close)
        else:
            base = 7400.0

        rng = _random.Random(int(base * 100))
        n_bars = 80
        now = datetime.now()
        bars: list[Bar] = []
        price = base * 0.985
        for i in range(n_bars):
            ts = now - timedelta(minutes=(n_bars - i) * target_minutes)
            drift = (base - price) * 0.05
            wiggle = rng.uniform(-3.0, 3.0)
            open_ = price
            close = price + drift + wiggle
            high = max(open_, close) + rng.uniform(0.5, 3.0)
            low = min(open_, close) - rng.uniform(0.5, 3.0)
            bars.append(Bar(
                timestamp=ts,
                open=round(open_, 2), high=round(high, 2),
                low=round(low, 2), close=round(close, 2),
                volume=rng.randint(800, 2500),
            ))
            price = close
        return bars

    async def get_prior_close(self, contract: Contract) -> float | None:
        """Mock prior close: 0.4% below the coherent track's session-open price.

        Picks a small offset so the Live Quote panel demonstrates a realistic
        day-change % rather than always showing 0.00%. Deterministic per-run.
        """
        if self._use_coherent_track:
            from fef_sidecar.mock import get_track
            try:
                return round(float(get_track().session_open_price()) * 0.996, 2)
            except Exception:
                pass
        if self._bars:
            return round(float(self._bars[0].open) * 0.996, 2)
        return None

    async def get_last_quote(self, contract: Contract) -> Quote:
        if not self._connected:
            await self.connect()
        # Mock VIX: gentle drift around 15.0
        if contract.symbol == "VIX":
            import math as _m
            import time as _t
            vix = 15.0 + _m.sin(_t.time() / 60.0) * 1.5
            return Quote(
                timestamp=self._bars[-1].timestamp,
                last=round(vix, 2),
                bid=round(vix - 0.05, 2),
                ask=round(vix + 0.05, 2),
                bid_size=10,
                ask_size=10,
            )
        # Coherent mode: live PriceTrack price. Test mode: last static bar.
        if self._use_coherent_track:
            from datetime import datetime as _dt

            from fef_sidecar.mock import get_track
            price = get_track().price_now()
            return Quote(
                timestamp=_dt.now(), last=round(price, 2),
                bid=round(price - 0.25, 2), ask=round(price + 0.25, 2),
                bid_size=10, ask_size=10,
            )
        last_bar = self._bars[-1]
        return Quote(
            timestamp=last_bar.timestamp, last=last_bar.close,
            bid=last_bar.close - 0.25, ask=last_bar.close + 0.25,
            bid_size=10, ask_size=10,
        )

    async def get_option_quote(self, contract: Contract) -> OptionQuote:
        """Synthetic option quote: intrinsic + flat extrinsic, ATM-ish delta."""
        if contract.kind != ContractKind.OPTION:
            raise ValueError(f"Expected OPTION contract, got {contract.kind}")
        if contract.strike is None or contract.right is None:
            raise ValueError("Option contract requires strike + right")

        if not self._connected:
            await self.connect()

        from datetime import datetime as _dt

        from fef_sidecar.mock import get_track
        spx = get_track().price_now()
        strike = contract.strike
        is_call = contract.right == "C"

        intrinsic = max(0.0, (spx - strike) if is_call else (strike - spx))
        # Simple extrinsic model: $8 ATM, decays linearly with moneyness up to $50 OTM
        moneyness = abs(spx - strike)
        extrinsic = max(0.05, 8.0 * max(0.0, 1.0 - moneyness / 50.0))
        price = round(intrinsic + extrinsic, 2)

        # Delta approximation: 0.5 ATM, scales with how OTM/ITM
        if is_call:
            raw_delta = 0.5 + (spx - strike) / 200.0
        else:
            raw_delta = -0.5 + (spx - strike) / 200.0
        delta = round(max(-0.95, min(0.95, raw_delta)), 3)

        return OptionQuote(
            timestamp=_dt.now(),
            contract=contract,
            last=price,
            bid=round(price - 0.25, 2),
            ask=round(price + 0.25, 2),
            delta=delta,
            underlying_price=spx,
        )

    async def ping(self) -> bool:
        return self._connected

    async def get_account_summary(self) -> AccountSummary | None:
        """Synthetic account scorecard.

        In scripted-day mode, daily P/L correlates with the day's price
        action so the scorecard moves with the simulated session. Otherwise
        returns flat baseline values appropriate for a $50k account.
        """
        import os as _os
        import time as _time
        import math as _math

        if not self._connected:
            await self.connect()

        net_liq = 50_000.0
        # If scripted day on, derive a plausible P/L from where we are in the day
        if _os.environ.get("FEF_MOCK_SCRIPTED_DAY", "false").lower() in ("true", "1", "yes"):
            from fef_sidecar.quantdata.mock_day import DAY_SCRIPT
            idx = max(0, min(self._scripted_index, len(DAY_SCRIPT) - 1))
            frame = DAY_SCRIPT[idx]
            # Simple model: daily P/L scales with how far drift has moved from 0,
            # plus a small sin component for intraday variation
            base_pnl = frame.drift_cum_net_m * 8  # $8 P/L per $1M drift (arbitrary)
            jitter = _math.sin(idx * 0.3) * 200
            daily_pnl = round(base_pnl + jitter, 2)
            realized = round(daily_pnl * 0.6, 2)  # 60% closed
            unrealized = round(daily_pnl * 0.4, 2)
            positions = 1 if abs(unrealized) > 50 else 0
        else:
            # Default mock: small positive P/L baseline
            daily_pnl = 250.0
            realized = 150.0
            unrealized = 100.0
            positions = 0

        return AccountSummary(
            account="MOCK_ACCOUNT",
            timestamp_ms=int(_time.time() * 1000),
            net_liquidation=net_liq + daily_pnl,
            buying_power=net_liq * 2,  # 2:1 reg-T margin
            available_funds=net_liq + daily_pnl - abs(unrealized) * 100,
            daily_pnl=daily_pnl,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            gross_position_value=abs(unrealized) * 100,
            open_positions_count=positions,
            open_options_count=positions,
            currency="USD",
        )

    async def stream_last_price(
        self,
        contract: Contract,
    ) -> AsyncIterator[Quote]:
        """Emit ticks from the coherent PriceTrack — price always matches
        what bars / QuantData / microstructure see at the same instant."""
        import os
        from datetime import datetime

        from fef_sidecar.mock import get_track

        if not self._connected:
            await self.connect()
        interval = float(os.environ.get("FEF_TICK_INTERVAL_SEC", "0.5"))
        track = get_track()

        while True:
            price = track.price_with_tick_noise()
            yield Quote(
                timestamp=datetime.now(),
                last=price,
                bid=round(price - 0.25, 2),
                ask=round(price + 0.25, 2),
                bid_size=10,
                ask_size=10,
            )
            await asyncio.sleep(interval)
