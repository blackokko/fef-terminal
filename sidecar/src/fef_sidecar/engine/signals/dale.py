"""Dale directional signal detectors — 8/21 EMA trend + STD channels.

Pure functions over a resampled OHLCV series at a single timeframe. No live
wiring — these are Phase 0 research detectors (direction-from-trend pivot).
Direction comes from signal *structure*, never from flow prediction:
bull structure -> CALLS, bear structure -> PUTS.

Timeframes are load-bearing and supplied by the caller (NOT defaulted here):
  - 0DTE: detect on 5-min bars (~5d lookback), gate on 30-min trend
  - 1DTE: detect on 30-min bars (~10d lookback), gate on 6-month daily trend

No pandas/numpy dependency (the sidecar venv has neither) — resampling and
indicators are pure Python.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CALLS = "CALLS"
PUTS = "PUTS"
BULL = "BULL"
BEAR = "BEAR"
UNCLEAR = "UNCLEAR"


@dataclass
class Bar:
    """One OHLCV bar. ts_ms is the bar's START time (UTC ms)."""

    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Signal:
    ts_ms: int           # signal bar's start (UTC ms); fire = this bar's close
    kind: str            # dale_bull_cross / dale_bear_cross / dale_bull_pullback / dale_bear_pullback
    direction: str       # CALLS / PUTS
    timeframe_min: int
    price: float         # close at fire (signal-timeframe bar)
    trend: str           # trend state at fire (BULL/BEAR/UNCLEAR)
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Resampling + indicators (pure Python)
# ---------------------------------------------------------------------------


def resample(bars_1m: list[Bar], minutes: int) -> list[Bar]:
    """Aggregate 1-min bars into `minutes`-bar OHLCV, aligned to the UTC clock
    grid (so 30-min buckets land on :00/:30, which includes the 13:30 UTC =
    9:30 ET RTH open). Bars must be sorted by ts_ms.
    """
    if minutes <= 1:
        return list(bars_1m)
    step = minutes * 60_000
    buckets: dict[int, list[Bar]] = {}
    for b in bars_1m:
        key = (b.ts_ms // step) * step
        buckets.setdefault(key, []).append(b)
    out: list[Bar] = []
    for key in sorted(buckets):
        group = buckets[key]
        out.append(
            Bar(
                ts_ms=key,
                open=group[0].open,
                high=max(g.high for g in group),
                low=min(g.low for g in group),
                close=group[-1].close,
                volume=sum(g.volume for g in group),
            )
        )
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    """EMA seeded with the SMA of the first `period` values. Entries before the
    seed are None so indices stay aligned with `values`."""
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2 / (period + 1)
    prev = seed
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rolling_mean_std(values: list[float], period: int) -> tuple[list[float | None], list[float | None]]:
    """Rolling population mean + std over `period` (aligned to `values`)."""
    n = len(values)
    means: list[float | None] = [None] * n
    stds: list[float | None] = [None] * n
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        means[i] = m
        stds[i] = var ** 0.5
    return means, stds


def trend_states(
    bars: list[Bar], *, fast: int = 8, slow: int = 21, slope_lookback: int = 5
) -> list[str]:
    """Per-bar trend state (BULL/BEAR/UNCLEAR).

    BULL: fast-EMA > slow-EMA AND fast-EMA rising over the last `slope_lookback`.
    BEAR: fast-EMA < slow-EMA AND fast-EMA falling over the last `slope_lookback`.
    """
    closes = [b.close for b in bars]
    ef = ema(closes, fast)
    es = ema(closes, slow)
    states = [UNCLEAR] * len(bars)
    for i in range(len(bars)):
        if ef[i] is None or es[i] is None:
            continue
        j = i - slope_lookback
        if j < 0 or ef[j] is None:
            continue
        slope = ef[i] - ef[j]
        if ef[i] > es[i] and slope > 0:
            states[i] = BULL
        elif ef[i] < es[i] and slope < 0:
            states[i] = BEAR
    return states


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------


def detect(
    bars: list[Bar],
    *,
    timeframe_min: int,
    fast: int = 8,
    slow: int = 21,
    std_period: int = 20,
    slope_lookback: int = 5,
    pullback_window: int = 2,
    band_sigma: float = 2.0,
) -> list[Signal]:
    """Detect Dale signals on an already-resampled `bars` series.

    - dale_bull_cross / dale_bear_cross: fast-EMA crosses slow-EMA, with trend
      confirmation on the cross bar.
    - dale_bull_pullback: in BULL trend, price touches/breaches the lower
      `band_sigma`σ band and closes back above within `pullback_window` bars.
    - dale_bear_pullback: symmetric at the upper band.
    Fires at the confirming bar's close.
    """
    closes = [b.close for b in bars]
    ef = ema(closes, fast)
    es = ema(closes, slow)
    means, stds = rolling_mean_std(closes, std_period)
    states = trend_states(bars, fast=fast, slow=slow, slope_lookback=slope_lookback)
    sigs: list[Signal] = []

    for i in range(1, len(bars)):
        # --- EMA crosses (require both EMAs defined on i and i-1) ---
        if None not in (ef[i], es[i], ef[i - 1], es[i - 1]):
            crossed_up = ef[i - 1] <= es[i - 1] and ef[i] > es[i]
            crossed_dn = ef[i - 1] >= es[i - 1] and ef[i] < es[i]
            if crossed_up and states[i] == BULL:
                sigs.append(Signal(
                    ts_ms=bars[i].ts_ms, kind="dale_bull_cross", direction=CALLS,
                    timeframe_min=timeframe_min, price=bars[i].close, trend=states[i],
                    detail={"ema_fast": ef[i], "ema_slow": es[i]},
                ))
            elif crossed_dn and states[i] == BEAR:
                sigs.append(Signal(
                    ts_ms=bars[i].ts_ms, kind="dale_bear_cross", direction=PUTS,
                    timeframe_min=timeframe_min, price=bars[i].close, trend=states[i],
                    detail={"ema_fast": ef[i], "ema_slow": es[i]},
                ))

    # --- Pullbacks (touch band then close back inside within window) ---
    for i in range(len(bars)):
        if means[i] is None or stds[i] is None:
            continue
        lower = means[i] - band_sigma * stds[i]
        upper = means[i] + band_sigma * stds[i]
        # bull pullback: BULL trend, this bar breaches the lower band (low<=lower)
        if states[i] == BULL and bars[i].low <= lower:
            for j in range(i, min(i + pullback_window + 1, len(bars))):
                if means[j] is None or stds[j] is None:
                    continue
                lband = means[j] - band_sigma * stds[j]
                if bars[j].close > lband:
                    sigs.append(Signal(
                        ts_ms=bars[j].ts_ms, kind="dale_bull_pullback", direction=CALLS,
                        timeframe_min=timeframe_min, price=bars[j].close, trend=states[j],
                        detail={"band": lband, "touch_bar_ts": bars[i].ts_ms},
                    ))
                    break
        if states[i] == BEAR and bars[i].high >= upper:
            for j in range(i, min(i + pullback_window + 1, len(bars))):
                if means[j] is None or stds[j] is None:
                    continue
                uband = means[j] + band_sigma * stds[j]
                if bars[j].close < uband:
                    sigs.append(Signal(
                        ts_ms=bars[j].ts_ms, kind="dale_bear_pullback", direction=PUTS,
                        timeframe_min=timeframe_min, price=bars[j].close, trend=states[j],
                        detail={"band": uband, "touch_bar_ts": bars[i].ts_ms},
                    ))
                    break

    return _dedup(sigs)


def _dedup(sigs: list[Signal]) -> list[Signal]:
    """Drop duplicate (ts_ms, kind) signals — consecutive touch bars can resolve
    to the same confirming bar. Keep first; return sorted by (ts_ms, kind)."""
    seen: set[tuple[int, str]] = set()
    out: list[Signal] = []
    for s in sorted(sigs, key=lambda s: (s.ts_ms, s.kind)):
        key = (s.ts_ms, s.kind)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def trend_state_at(bars: list[Bar], ts_ms: int, *, fast: int = 8, slow: int = 21,
                   slope_lookback: int = 5) -> str:
    """Higher-timeframe trend state as of the last bar at or before ts_ms.
    Used by the multi-timeframe gate."""
    states = trend_states(bars, fast=fast, slow=slow, slope_lookback=slope_lookback)
    state = UNCLEAR
    for b, st in zip(bars, states, strict=True):
        if b.ts_ms <= ts_ms:
            state = st
        else:
            break
    return state


def mtf_gate_pass(signal_direction: str, htf_trend: str) -> bool:
    """Block longs when the higher timeframe is strongly opposing.
    CALLS blocked if HTF is BEAR; PUTS blocked if HTF is BULL. UNCLEAR passes."""
    if signal_direction == CALLS and htf_trend == BEAR:
        return False
    if signal_direction == PUTS and htf_trend == BULL:
        return False
    return True
