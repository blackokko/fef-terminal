"""Technical-analysis helpers used by the spread entry detector.

Pure functions over lists of Bar objects — no I/O, no state. The spread
recommender calls these on a cached 30-min bar series to decide whether
PCS/CCS entry conditions are met.

Mirrors the indicators drawn on the chart (EMA 8 / EMA 21 / centered-OLS
StdDev channel with +/-1/2/3 sigma bands) so what the user sees on the
chart and what the recommender evaluates are computed the same way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from fef_sidecar.ibkr.models import Bar


def ema(values: list[float], period: int) -> list[float]:
    """Standard exponentially-weighted moving average. Returns one EMA value
    per input. The first `period - 1` outputs are seeded with the simple
    mean to avoid early-bar instability.
    """
    if not values or period <= 1:
        return list(values)
    alpha = 2.0 / (period + 1)
    out: list[float] = []
    for i, v in enumerate(values):
        if i == 0:
            out.append(v)
            continue
        if i < period:
            out.append(sum(values[: i + 1]) / (i + 1))
            continue
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


@dataclass(frozen=True)
class ChannelPoint:
    """Regression-channel value at a single bar index."""

    center: float
    upper_1: float
    upper_2: float
    upper_3: float
    lower_1: float
    lower_2: float
    lower_3: float


def stddev_channel(closes: list[float]) -> list[ChannelPoint]:
    """OLS linear regression of close vs. bar index + residual std bands.

    Uses the centered formula (subtract mean before solving) for numerical
    stability — same approach as the chart's StandardDevChannel. Returns
    one ChannelPoint per input bar.

    ⚠ THIS IS THE FULL-RANGE (REPAINTING) FORM. It fits ONE line across every
    bar passed in, so every returned point depends on data that arrived AFTER
    that bar. Only ``result[-1]`` is honest at decision time.

    ``engine/spread_entry.py`` uses ``channel[-1]`` and is therefore correct.
    ANY caller that reads a middle index over a historical series is reading a
    lookahead — use ``stddev_channel_rolling`` instead.

    Measured 2026-08-11 (`tospane`, archive), replaying the terminal's own
    presets bar by bar and comparing the as-drawn line to the line as it stood
    at the time:

      preset      ±2σ drift (median)   as % of band width   P(was at 2σ | shown at 2σ)
      5D·5m       31.85 pts            37%                  47.2%
      10D·30m     49.90 pts            38%                  49.5%
      6mo·1D      152.4 pts            35%                  51.1%

    The displayed slope disagrees in SIGN with the live one 21–28% of the time,
    the live σ runs 0.68–0.74× the finished σ, and only 10–16% of the ±2σ touches
    that actually occurred are still visible afterwards. Scoring signals off the
    finished line yields +19 to +85 pts/signal and t≥3 in 43 of 46 cells; scored
    honestly it is 0 of 46. **Matched random walks reproduce all of it — the
    repaint is the estimator, not the market.**
    """
    n = len(closes)
    if n < 3:
        return [
            ChannelPoint(center=c, upper_1=c, upper_2=c, upper_3=c,
                        lower_1=c, lower_2=c, lower_3=c)
            for c in closes
        ]

    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(closes) / n
    num = sum((xs[i] - mean_x) * (closes[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    slope = num / den if den > 0 else 0.0
    intercept = mean_y - slope * mean_x

    line = [slope * i + intercept for i in xs]
    sigma = math.sqrt(sum((closes[i] - line[i]) ** 2 for i in range(n)) / n)

    return [
        ChannelPoint(
            center=line[i],
            upper_1=line[i] + sigma,
            upper_2=line[i] + 2 * sigma,
            upper_3=line[i] + 3 * sigma,
            lower_1=line[i] - sigma,
            lower_2=line[i] - 2 * sigma,
            lower_3=line[i] - 3 * sigma,
        )
        for i in xs
    ]


def stddev_channel_rolling(
    closes: list[float], lookback: int
) -> list[ChannelPoint]:
    """NON-REPAINTING standard-deviation channel.

    Each bar's channel value is fitted on that bar's OWN trailing window
    ``closes[i-lookback+1 : i+1]`` — i.e. exactly what the chart could have
    drawn at that bar, using no future information. Successive points are
    therefore NOT collinear: this traces a curve, not one straight line.

    This is the honest counterpart to :func:`stddev_channel`, which fits a
    single line across the whole array and consequently moves every historical
    band value as new bars arrive (see that function's docstring for the
    measured size — median 31.85–152.4 pts of drift, 35–38% of band width).

    Bars before ``lookback`` are fitted on however much history exists (minimum
    3); their values are still causal, just noisier. ``closes[:i+1]`` is never
    exceeded, so the guarantee holds at every index:

        result[i] depends only on closes[:i+1]

    Args:
        closes: bar closes, oldest first.
        lookback: trailing window length. Use the preset's own length —
            5D·5m ≈ 390, 10D·30m ≈ 130, 6mo·1D ≈ 126.

    Returns:
        One ChannelPoint per input bar, same shape as :func:`stddev_channel`.
    """
    n = len(closes)
    if lookback < 3:
        raise ValueError(f"lookback must be >= 3, got {lookback}")
    if n == 0:
        return []

    out: list[ChannelPoint] = []
    for i in range(n):
        lo = max(0, i - lookback + 1)
        window = closes[lo : i + 1]
        m = len(window)
        if m < 3:
            c = closes[i]
            out.append(
                ChannelPoint(center=c, upper_1=c, upper_2=c, upper_3=c,
                             lower_1=c, lower_2=c, lower_3=c)
            )
            continue

        # Centered OLS — same formula as stddev_channel, same numerical care.
        mean_x = (m - 1) / 2.0
        mean_y = sum(window) / m
        num = sum((j - mean_x) * (window[j] - mean_y) for j in range(m))
        den = sum((j - mean_x) ** 2 for j in range(m))
        slope = num / den if den > 0 else 0.0
        intercept = mean_y - slope * mean_x

        # Population-N residual sigma, matching the TOS convention the chart uses.
        ssq = sum((window[j] - (slope * j + intercept)) ** 2 for j in range(m))
        sigma = math.sqrt(ssq / m)

        # Evaluate at the window's LAST bar — the bar we are reporting.
        center = slope * (m - 1) + intercept
        out.append(
            ChannelPoint(
                center=center,
                upper_1=center + sigma,
                upper_2=center + 2 * sigma,
                upper_3=center + 3 * sigma,
                lower_1=center - sigma,
                lower_2=center - 2 * sigma,
                lower_3=center - 3 * sigma,
            )
        )
    return out


def is_bullish_engulfing(prev_bar: Bar, curr_bar: Bar) -> bool:
    """Classic bullish engulfing: prior candle red, current candle green,
    current real body engulfs prior real body.

    Strict definition used (open<=prior.close, close>=prior.open) — same
    as TradingView's built-in engulfing detector."""
    prev_red = prev_bar.close < prev_bar.open
    curr_green = curr_bar.close > curr_bar.open
    body_engulfs = curr_bar.open <= prev_bar.close and curr_bar.close >= prev_bar.open
    return prev_red and curr_green and body_engulfs


def is_bearish_engulfing(prev_bar: Bar, curr_bar: Bar) -> bool:
    """Classic bearish engulfing: prior candle green, current red, current
    body engulfs prior body."""
    prev_green = prev_bar.close > prev_bar.open
    curr_red = curr_bar.close < curr_bar.open
    body_engulfs = curr_bar.open >= prev_bar.close and curr_bar.close <= prev_bar.open
    return prev_green and curr_red and body_engulfs


def touches_lower_band(bar: Bar, point: ChannelPoint, tolerance_pts: float = 0.5) -> str | None:
    """If the bar's LOW pierces or touches lower STD1 / STD2 (within
    tolerance), return which band ('STD1' / 'STD2' / 'STD3'). Otherwise None.

    Pick the deepest band the low reached — STD3 wins over STD2 wins over STD1.
    """
    if bar.low <= point.lower_3 + tolerance_pts:
        return "STD3"
    if bar.low <= point.lower_2 + tolerance_pts:
        return "STD2"
    if bar.low <= point.lower_1 + tolerance_pts:
        return "STD1"
    return None


def touches_upper_band(bar: Bar, point: ChannelPoint, tolerance_pts: float = 0.5) -> str | None:
    """If the bar's HIGH reaches/pierces upper STD1 / STD2 / STD3, return
    the band tag. Otherwise None.
    """
    if bar.high >= point.upper_3 - tolerance_pts:
        return "STD3"
    if bar.high >= point.upper_2 - tolerance_pts:
        return "STD2"
    if bar.high >= point.upper_1 - tolerance_pts:
        return "STD1"
    return None


def bars_since_ema_cross(
    fast: list[float], slow: list[float], *, direction: str
) -> int | None:
    """Index distance (in bars) from the last EMA cross of the requested
    direction back to the most recent bar. Returns None if no such cross
    found in the provided series.

    direction='GOLDEN' = fast crossing ABOVE slow (bullish).
    direction='DEATH'  = fast crossing BELOW slow (bearish).
    """
    if len(fast) != len(slow) or len(fast) < 2:
        return None
    for i in range(len(fast) - 1, 0, -1):
        prev_above = fast[i - 1] > slow[i - 1]
        curr_above = fast[i] > slow[i]
        if direction == "GOLDEN" and not prev_above and curr_above:
            return (len(fast) - 1) - i
        if direction == "DEATH" and prev_above and not curr_above:
            return (len(fast) - 1) - i
    return None


def resample_to_minutes(bars: list[Bar], target_minutes: int) -> list[Bar]:
    """Resample a list of equal-spaced bars to a coarser minute interval.

    Buckets by ``floor(epoch / target_seconds)``. For each bucket: open =
    first bar's open, close = last bar's close, high/low = max/min,
    volume = sum. The final bucket may be partial — included anyway so
    the most-recent (still forming) coarse bar is visible to the engine.
    """
    if not bars or target_minutes <= 0:
        return list(bars)
    target_sec = target_minutes * 60
    buckets: dict[int, list[Bar]] = {}
    for b in bars:
        key = int(b.timestamp.timestamp()) // target_sec
        buckets.setdefault(key, []).append(b)

    out: list[Bar] = []
    for key in sorted(buckets):
        group = buckets[key]
        first = group[0]
        last = group[-1]
        out.append(Bar(
            timestamp=first.timestamp,
            open=first.open,
            high=max(b.high for b in group),
            low=min(b.low for b in group),
            close=last.close,
            volume=sum(b.volume for b in group),
        ))
    return out
