"""TA-based credit-spread entry detector tests.

Each test builds a hand-crafted bar series to exercise one rule:
    * EMA8 above/below EMA21
    * Golden / death cross within lookback
    * Bullish / bearish engulfing on most recent bar
    * Lower / upper STD1/2 touch

Helpers below produce bars with controllable open/close/high/low so a
small series can isolate one rule at a time.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fef_sidecar.engine.spread_entry import check_ccs_entry, check_pcs_entry
from fef_sidecar.engine.technical import (
    bars_since_ema_cross,
    ema,
    is_bearish_engulfing,
    is_bullish_engulfing,
    stddev_channel,
)
from fef_sidecar.ibkr.models import Bar


def _bar(t: datetime, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(timestamp=t, open=o, high=h, low=l, close=c, volume=1000)


def _series_v_recovery_with_engulfing() -> list[Bar]:
    """V-shape series: 15-bar downtrend, then 6-bar rally that produces a
    golden cross, then a pullback red candle, then a bullish engulfing
    that pierces the lower regression band. Designed so all PCS rules
    pass simultaneously.
    """
    start = datetime(2026, 5, 18, 4, 30)
    bars: list[Bar] = []
    price = 7430.0
    for i in range(15):
        t = start + timedelta(minutes=30 * i)
        new_price = price - 4.0
        bars.append(_bar(t, price, price + 0.5, new_price - 0.5, new_price))
        price = new_price
    for i in range(15, 21):
        t = start + timedelta(minutes=30 * i)
        new_price = price + 6.0
        bars.append(_bar(t, price, new_price + 0.5, price - 0.5, new_price))
        price = new_price
    t = start + timedelta(minutes=30 * 21)
    pullback_open = price
    pullback_close = price - 7.0
    bars.append(_bar(t, pullback_open, pullback_open + 0.5,
                     pullback_close - 0.5, pullback_close))
    t = start + timedelta(minutes=30 * 22)
    engulf_open = pullback_close - 1.5
    engulf_close = pullback_open + 3.0
    engulf_low = pullback_close - 35.0
    bars.append(_bar(t, engulf_open, engulf_close + 0.5,
                     engulf_low, engulf_close))
    return bars


def _series_inverse_v_with_bearish_engulfing() -> list[Bar]:
    """Mirror of the V-shape for CCS: 15-bar uptrend, 6-bar reversal that
    produces a death cross, pullback green candle, bearish engulfing that
    pierces upper band."""
    start = datetime(2026, 5, 18, 4, 30)
    bars: list[Bar] = []
    price = 7370.0
    for i in range(15):
        t = start + timedelta(minutes=30 * i)
        new_price = price + 4.0
        bars.append(_bar(t, price, new_price + 0.5, price - 0.5, new_price))
        price = new_price
    for i in range(15, 21):
        t = start + timedelta(minutes=30 * i)
        new_price = price - 6.0
        bars.append(_bar(t, price, price + 0.5, new_price - 0.5, new_price))
        price = new_price
    t = start + timedelta(minutes=30 * 21)
    pullback_open = price
    pullback_close = price + 7.0
    bars.append(_bar(t, pullback_open, pullback_close + 0.5,
                     pullback_open - 0.5, pullback_close))
    t = start + timedelta(minutes=30 * 22)
    engulf_open = pullback_close + 1.5
    engulf_close = pullback_open - 3.0
    engulf_high = pullback_close + 35.0
    bars.append(_bar(t, engulf_open, engulf_high,
                     engulf_close - 0.5, engulf_close))
    return bars


def test_ema_basic_smooth() -> None:
    out = ema([10.0] * 30, period=8)
    assert all(abs(v - 10.0) < 1e-6 for v in out)


def test_ema_rising() -> None:
    out = ema(list(range(1, 31)), period=8)
    for i in range(8, len(out)):
        assert out[i] > out[i - 1]


def test_stddev_channel_flat() -> None:
    points = stddev_channel([100.0] * 20)
    last = points[-1]
    assert abs(last.center - 100.0) < 1e-6
    assert abs(last.upper_1 - last.center) < 1e-6


def test_stddev_channel_uptrend() -> None:
    closes = [100.0 + i + (0.3 if i % 2 else -0.3) for i in range(20)]
    points = stddev_channel(closes)
    assert points[-1].center > points[0].center
    assert points[-1].upper_1 > points[-1].center
    assert points[-1].lower_1 < points[-1].center
    sigma_via_upper = points[-1].upper_1 - points[-1].center
    sigma_via_2 = points[-1].upper_2 - points[-1].upper_1
    assert abs(sigma_via_upper - sigma_via_2) < 1e-9


def test_bullish_engulfing_classic() -> None:
    start = datetime(2026, 5, 18, 9, 30)
    prev_red = _bar(start, 105, 106, 100, 101)
    curr_green = _bar(start + timedelta(minutes=30), 100, 110, 99, 108)
    assert is_bullish_engulfing(prev_red, curr_green)


def test_bullish_engulfing_rejects_doji() -> None:
    start = datetime(2026, 5, 18, 9, 30)
    prev = _bar(start, 100, 101, 99, 100)
    curr = _bar(start + timedelta(minutes=30), 100, 102, 99, 100)
    assert not is_bullish_engulfing(prev, curr)


def test_bearish_engulfing_classic() -> None:
    start = datetime(2026, 5, 18, 9, 30)
    prev_green = _bar(start, 100, 105, 99, 104)
    curr_red = _bar(start + timedelta(minutes=30), 106, 107, 95, 96)
    assert is_bearish_engulfing(prev_green, curr_red)


def test_bars_since_cross_golden() -> None:
    fast = [9, 9, 9, 10, 11, 11]
    slow = [10, 10, 10, 10, 10, 10]
    # Cross at index 4 (10 -> 11 crosses 10 strict-above). Last index is 5.
    # Distance from cross to last = 5 - 4 = 1 bar.
    assert bars_since_ema_cross(fast, slow, direction="GOLDEN") == 1


def test_bars_since_cross_none_found() -> None:
    fast = [9, 9, 9, 9, 9, 9]
    slow = [10, 10, 10, 10, 10, 10]
    assert bars_since_ema_cross(fast, slow, direction="GOLDEN") is None


def test_check_pcs_entry_fires_on_full_setup() -> None:
    bars = _series_v_recovery_with_engulfing()
    trigger = check_pcs_entry(bars, cross_lookback=16)
    assert trigger.kind == "PCS", trigger.reasons
    assert trigger.signature.startswith("PCS|")
    assert trigger.channel_band_touched in ("STD1", "STD2", "STD3")


def test_check_pcs_entry_rejects_when_no_engulfing() -> None:
    bars = _series_v_recovery_with_engulfing()
    last = bars[-1]
    bars[-1] = Bar(
        timestamp=last.timestamp,
        open=last.close - 1,
        high=last.high,
        low=last.low,
        close=last.close,
        volume=last.volume,
    )
    trigger = check_pcs_entry(bars, cross_lookback=16)
    assert trigger.kind == "NONE"
    assert any("engulfing" in r for r in trigger.reasons)


def test_check_ccs_entry_fires_on_full_setup() -> None:
    bars = _series_inverse_v_with_bearish_engulfing()
    trigger = check_ccs_entry(bars, cross_lookback=16)
    assert trigger.kind == "CCS", trigger.reasons


def test_pcs_and_ccs_are_mutually_exclusive() -> None:
    bars = _series_v_recovery_with_engulfing()
    pcs = check_pcs_entry(bars, cross_lookback=16)
    ccs = check_ccs_entry(bars, cross_lookback=16)
    assert not (pcs.triggered and ccs.triggered)
