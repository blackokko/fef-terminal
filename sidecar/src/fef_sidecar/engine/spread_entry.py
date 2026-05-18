"""Credit-spread entry detector — TA-driven, independent of the FEF model.

Encodes the user's spread playbook:

PCS (Put Credit Spread — bullish):
  * 8 EMA above 21 EMA (uptrend)
  * 8 EMA sloping up
  * 8 EMA recently crossed above 21 EMA (within trend-confirmation window)
  * Most recent COMPLETED candle is a bullish engulfing
  * That engulfing candle's low touched the lower STD1 OR STD2 of the
    regression channel (pullback into the channel)

CCS (Call Credit Spread — bearish): mirror image.

Returns a SpreadEntryTrigger describing WHY it fired (or didn't), so the
UI can show the user exactly which conditions are pending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from fef_sidecar.engine.technical import (
    ChannelPoint,
    bars_since_ema_cross,
    ema,
    is_bearish_engulfing,
    is_bullish_engulfing,
    stddev_channel,
    touches_lower_band,
    touches_upper_band,
)
from fef_sidecar.ibkr.models import Bar


# How far back the EMA cross can be and still count as "in the trend".
# On 30-min bars, 8 means ~4 hours = generally same trading session.
DEFAULT_CROSS_LOOKBACK_BARS = 8


SpreadKind = Literal["PCS", "CCS", "NONE"]


@dataclass(frozen=True)
class SpreadEntryTrigger:
    kind: SpreadKind
    reasons: list[str] = field(default_factory=list)
    # Optional diagnostics for the UI
    ema8_value: float | None = None
    ema21_value: float | None = None
    bars_since_cross: int | None = None
    channel_band_touched: str | None = None  # 'STD1' / 'STD2' / 'STD3'
    trigger_bar_close_ms: int | None = None
    # signature uniquely identifies this trigger across iters so the loop
    # can fire the alert exactly once per fresh trigger (matches the user's
    # 'fire once per completed trigger bar' answer)
    signature: str = ""

    @property
    def triggered(self) -> bool:
        return self.kind != "NONE"


def check_pcs_entry(
    bars: list[Bar],
    *,
    fast_period: int = 8,
    slow_period: int = 21,
    cross_lookback: int = DEFAULT_CROSS_LOOKBACK_BARS,
) -> SpreadEntryTrigger:
    """Evaluate PCS (bullish credit spread) entry conditions on a list of
    bars. Only fires on the most recent COMPLETED bar engulfing the prior
    bar — the last bar in the list is treated as the trigger candle.
    """
    if len(bars) < max(slow_period + 2, 22):
        return SpreadEntryTrigger(kind="NONE", reasons=["not enough bars"])

    closes = [b.close for b in bars]
    ema_fast = ema(closes, fast_period)
    ema_slow = ema(closes, slow_period)
    channel = stddev_channel(closes)

    last = bars[-1]
    prev = bars[-2]
    last_pt = channel[-1]

    f_last = ema_fast[-1]
    s_last = ema_slow[-1]
    f_prev = ema_fast[-2]

    fails: list[str] = []
    if f_last <= s_last:
        fails.append(f"EMA8 ({f_last:.2f}) not above EMA21 ({s_last:.2f})")
    if f_last <= f_prev:
        fails.append(f"EMA8 not sloping up (last={f_last:.2f} <= prev={f_prev:.2f})")

    cross_bars = bars_since_ema_cross(ema_fast, ema_slow, direction="GOLDEN")
    if cross_bars is None or cross_bars > cross_lookback:
        fails.append(
            f"no golden cross within last {cross_lookback} bars "
            f"(found: {'never' if cross_bars is None else f'{cross_bars} bars ago'})"
        )

    band = touches_lower_band(last, last_pt)
    if band not in ("STD1", "STD2"):
        fails.append(
            f"trigger bar low ({last.low:.2f}) didn't touch lower STD1 or STD2 "
            f"(STD1={last_pt.lower_1:.2f}, STD2={last_pt.lower_2:.2f})"
        )

    if not is_bullish_engulfing(prev, last):
        fails.append("most recent bar is not a bullish engulfing")

    if fails:
        return SpreadEntryTrigger(
            kind="NONE", reasons=fails,
            ema8_value=f_last, ema21_value=s_last,
            bars_since_cross=cross_bars, channel_band_touched=band,
        )

    return SpreadEntryTrigger(
        kind="PCS",
        reasons=[
            f"EMA8 ({f_last:.2f}) > EMA21 ({s_last:.2f}), trending up",
            f"golden cross {cross_bars} bars ago",
            f"bullish engulfing closed at {last.close:.2f}",
            f"trigger low ({last.low:.2f}) touched lower {band}",
        ],
        ema8_value=f_last,
        ema21_value=s_last,
        bars_since_cross=cross_bars,
        channel_band_touched=band,
        trigger_bar_close_ms=int(last.timestamp.timestamp() * 1000),
        signature=f"PCS|{int(last.timestamp.timestamp())}|{band}",
    )


def check_ccs_entry(
    bars: list[Bar],
    *,
    fast_period: int = 8,
    slow_period: int = 21,
    cross_lookback: int = DEFAULT_CROSS_LOOKBACK_BARS,
) -> SpreadEntryTrigger:
    """Evaluate CCS (bearish credit spread) entry conditions. Mirror of PCS:
    EMA8 below EMA21, recent death cross, bearish engulfing touching upper
    STD1 or STD2.
    """
    if len(bars) < max(slow_period + 2, 22):
        return SpreadEntryTrigger(kind="NONE", reasons=["not enough bars"])

    closes = [b.close for b in bars]
    ema_fast = ema(closes, fast_period)
    ema_slow = ema(closes, slow_period)
    channel = stddev_channel(closes)

    last = bars[-1]
    prev = bars[-2]
    last_pt = channel[-1]

    f_last = ema_fast[-1]
    s_last = ema_slow[-1]
    f_prev = ema_fast[-2]

    fails: list[str] = []
    if f_last >= s_last:
        fails.append(f"EMA8 ({f_last:.2f}) not below EMA21 ({s_last:.2f})")
    if f_last >= f_prev:
        fails.append(f"EMA8 not sloping down (last={f_last:.2f} >= prev={f_prev:.2f})")

    cross_bars = bars_since_ema_cross(ema_fast, ema_slow, direction="DEATH")
    if cross_bars is None or cross_bars > cross_lookback:
        fails.append(
            f"no death cross within last {cross_lookback} bars "
            f"(found: {'never' if cross_bars is None else f'{cross_bars} bars ago'})"
        )

    band = touches_upper_band(last, last_pt)
    if band not in ("STD1", "STD2"):
        fails.append(
            f"trigger bar high ({last.high:.2f}) didn't touch upper STD1 or STD2 "
            f"(STD1={last_pt.upper_1:.2f}, STD2={last_pt.upper_2:.2f})"
        )

    if not is_bearish_engulfing(prev, last):
        fails.append("most recent bar is not a bearish engulfing")

    if fails:
        return SpreadEntryTrigger(
            kind="NONE", reasons=fails,
            ema8_value=f_last, ema21_value=s_last,
            bars_since_cross=cross_bars, channel_band_touched=band,
        )

    return SpreadEntryTrigger(
        kind="CCS",
        reasons=[
            f"EMA8 ({f_last:.2f}) < EMA21 ({s_last:.2f}), trending down",
            f"death cross {cross_bars} bars ago",
            f"bearish engulfing closed at {last.close:.2f}",
            f"trigger high ({last.high:.2f}) touched upper {band}",
        ],
        ema8_value=f_last,
        ema21_value=s_last,
        bars_since_cross=cross_bars,
        channel_band_touched=band,
        trigger_bar_close_ms=int(last.timestamp.timestamp() * 1000),
        signature=f"CCS|{int(last.timestamp.timestamp())}|{band}",
    )


_PCS_CCS_FAIL_REASONS_CACHE: dict[str, list[str]] = {}


def fmt_reasons(reasons: list[str]) -> str:
    """Compact fail-reason summary for UI tooltip/log lines."""
    return "; ".join(reasons)
