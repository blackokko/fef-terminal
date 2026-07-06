"""Pattern-overlay detector tests — synthetic bar fixtures, one fire +
one non-fire per detector, plus flag pole/coil/breakout mechanics,
trendline pivots, and the resampler."""

from __future__ import annotations

from fef_sidecar.engine.pattern_overlay import (
    PatternOverlayConfig,
    build_overlay,
    detect_candles,
    detect_flags,
    detect_trendlines,
    ema_series,
    pivot_points,
    resample_bars,
)

_T0 = 1_750_000_000_000  # arbitrary epoch ms anchor
_MIN = 60_000


def mk(o: float, h: float, low: float, c: float, i: int, v: float = 100.0) -> dict:
    assert h >= max(o, c) and low <= min(o, c), "malformed synthetic bar"
    return {"timestamp_ms": _T0 + i * _MIN, "open": o, "high": h, "low": low,
            "close": c, "volume": v}


def flat(n: int, price: float = 100.0, start: int = 0) -> list[dict]:
    """Zero-body bars — fire nothing (no bull/bear body, equal highs/lows
    mean no inside bar, doji is off by default)."""
    return [mk(price, price + 0.1, price - 0.1, price, start + i) for i in range(n)]


def kinds(hits) -> list[str]:
    return [h.kind for h in hits]


# ---------------------------------------------------------------------------
# Engulfing
# ---------------------------------------------------------------------------

def test_bull_engulfing_fires() -> None:
    bars = flat(5)
    n = len(bars)
    bars.append(mk(101.0, 101.1, 99.9, 100.0, n))          # bearish setup bar
    bars.append(mk(99.8, 101.4, 99.7, 101.2, n + 1))       # bull body engulfs it
    hits = detect_candles(bars)
    assert "engulf_bull" in kinds(hits)
    hit = next(h for h in hits if h.kind == "engulf_bull")
    assert hit.direction == "BULL" and hit.label == "Engulf"
    assert hit.timestamp_ms == bars[-1]["timestamp_ms"]


def test_bull_engulfing_requires_full_engulf() -> None:
    bars = flat(5)
    n = len(bars)
    bars.append(mk(101.0, 101.1, 99.9, 100.0, n))
    # close 100.9 < prior open 101.0 — body does NOT engulf
    bars.append(mk(99.8, 101.0, 99.7, 100.9, n + 1))
    assert "engulf_bull" not in kinds(detect_candles(bars))


def test_bear_engulfing_fires_and_needs_bigger_body() -> None:
    bars = flat(5)
    n = len(bars)
    bars.append(mk(100.0, 101.1, 99.9, 101.0, n))          # bullish setup
    bars.append(mk(101.2, 101.3, 99.6, 99.8, n + 1))       # bear engulf
    assert "engulf_bear" in kinds(detect_candles(bars))
    # Same shape but smaller body than prior -> no fire
    bars2 = flat(5)
    bars2.append(mk(100.0, 101.6, 99.9, 101.5, n))          # body 1.5
    bars2.append(mk(101.5, 101.6, 99.4, 100.1, n + 1))      # engulfs but body 1.4 < 1.5
    assert "engulf_bear" not in kinds(detect_candles(bars2))


# ---------------------------------------------------------------------------
# Hammer / shooting star (EMA20 context)
# ---------------------------------------------------------------------------

def test_hammer_fires_below_ema() -> None:
    bars = flat(25)  # EMA20 ~ 100
    n = len(bars)
    # long lower wick, tiny upper wick, close below EMA
    bars.append(mk(99.95, 100.0, 99.0, 99.9, n))
    hits = detect_candles(bars)
    assert "hammer" in kinds(hits)


def test_hammer_blocked_above_ema_and_by_upper_wick() -> None:
    bars = flat(25)
    n = len(bars)
    # Same wick shape but close ABOVE the EMA -> no hammer
    bars.append(mk(100.55, 100.6, 99.6, 100.5, n))
    assert "hammer" not in kinds(detect_candles(bars))
    # Below EMA but upper wick too large (> 25% of range)
    bars2 = flat(25)
    bars2.append(mk(99.5, 100.0, 99.0, 99.45, n))  # upper wick 0.5 of range 1.0
    assert "hammer" not in kinds(detect_candles(bars2))


def test_shooting_star_fires_above_ema() -> None:
    bars = flat(25)
    n = len(bars)
    bars.append(mk(100.05, 101.0, 100.0, 100.1, n))  # long upper wick, close > EMA
    assert "shooting_star" in kinds(detect_candles(bars))
    # Mirror below EMA -> no fire
    bars2 = flat(25)
    bars2.append(mk(99.0, 99.95, 98.95, 99.05, n))
    assert "shooting_star" not in kinds(detect_candles(bars2))


# ---------------------------------------------------------------------------
# Three white soldiers / three black crows
# ---------------------------------------------------------------------------

def test_three_soldiers_fires() -> None:
    bars = flat(5)
    n = len(bars)
    bars.append(mk(100.0, 101.1, 99.95, 101.0, n))
    bars.append(mk(101.0, 102.1, 100.95, 102.0, n + 1))
    bars.append(mk(102.0, 103.1, 101.95, 103.0, n + 2))
    hits = detect_candles(bars)
    assert "soldiers" in kinds(hits)
    assert next(h for h in hits if h.kind == "soldiers").label == "3 Soldiers"


def test_three_soldiers_requires_ascending_closes() -> None:
    bars = flat(5)
    n = len(bars)
    bars.append(mk(100.0, 101.1, 99.95, 101.0, n))
    bars.append(mk(100.2, 100.9, 100.15, 100.8, n + 1))  # close DOWN vs prior
    bars.append(mk(100.8, 101.9, 100.75, 101.8, n + 2))
    assert "soldiers" not in kinds(detect_candles(bars))


def test_three_crows_fires() -> None:
    bars = flat(5)
    n = len(bars)
    bars.append(mk(103.0, 103.05, 101.9, 102.0, n))
    bars.append(mk(102.0, 102.05, 100.9, 101.0, n + 1))
    bars.append(mk(101.0, 101.05, 99.9, 100.0, n + 2))
    assert "crows" in kinds(detect_candles(bars))


# ---------------------------------------------------------------------------
# Morning / evening star
# ---------------------------------------------------------------------------

def test_morning_star_fires() -> None:
    bars = flat(5)
    n = len(bars)
    bars.append(mk(101.5, 101.6, 99.9, 100.0, n))            # big bear bar
    bars.append(mk(99.9, 100.05, 99.8, 99.95, n + 1))        # small middle body
    bars.append(mk(100.0, 101.0, 99.95, 100.9, n + 2))       # bull close > 50% into bar 1
    hits = detect_candles(bars)
    assert "morning_star" in kinds(hits)
    assert next(h for h in hits if h.kind == "morning_star").label == "M-Star"


def test_morning_star_requires_deep_reversal_close() -> None:
    bars = flat(5)
    n = len(bars)
    bars.append(mk(101.5, 101.6, 99.9, 100.0, n))
    bars.append(mk(99.9, 100.05, 99.8, 99.95, n + 1))
    # close 100.5 < midpoint 100.75 -> no fire
    bars.append(mk(100.0, 100.6, 99.95, 100.5, n + 2))
    assert "morning_star" not in kinds(detect_candles(bars))


def test_evening_star_fires() -> None:
    bars = flat(5)
    n = len(bars)
    bars.append(mk(100.0, 101.6, 99.9, 101.5, n))            # big bull bar
    bars.append(mk(101.6, 101.7, 101.45, 101.55, n + 1))     # small middle
    bars.append(mk(101.5, 101.55, 100.4, 100.5, n + 2))      # bear close < midpoint 100.75
    assert "evening_star" in kinds(detect_candles(bars))


# ---------------------------------------------------------------------------
# Inside-bar coil / doji
# ---------------------------------------------------------------------------

def test_inside_bar_coil_fires_only_without_directional_pattern() -> None:
    bars = flat(5)
    n = len(bars)
    bars.append(mk(100.8, 101.0, 99.0, 99.2, n))            # wide bear bar
    bars.append(mk(99.9, 100.5, 99.5, 100.0, n + 1))        # inside it, small bull body
    hits = detect_candles(bars)
    assert "inside_coil" in kinds(hits)


def test_inside_bar_requires_containment() -> None:
    bars = flat(5)
    n = len(bars)
    bars.append(mk(100.8, 101.0, 99.0, 99.2, n))
    bars.append(mk(99.9, 101.2, 99.5, 100.0, n + 1))        # high pokes above prior
    assert "inside_coil" not in kinds(detect_candles(bars))


def test_doji_off_by_default_on_by_config() -> None:
    bars = flat(5)
    n = len(bars)
    bars.append(mk(100.0, 100.3, 99.8, 100.0, n))           # varied prior bar
    bars.append(mk(100.0, 100.5, 99.5, 100.02, n + 1))      # tiny body, not inside
    assert "doji" not in kinds(detect_candles(bars))  # default config: off
    cfg = PatternOverlayConfig(show_doji=True)
    assert "doji" in kinds(detect_candles(bars, cfg))


# ---------------------------------------------------------------------------
# Measured-move flags
# ---------------------------------------------------------------------------

def _bull_flag_bars(start: int = 0, base: float = 100.0) -> list[dict]:
    """11 flat bars, 3-bar pole +10pts, 3 tight flag bars, breakout bar.
    Pole: close[13]-close[10] = +10 (k=3 at the breakout index 16)."""
    bars = [mk(base, base + 0.1, base - 0.1, base, start + i) for i in range(11)]
    i = start + 11
    bars.append(mk(base, base + 4.6, base - 0.1, base + 4.5, i))            # 11
    bars.append(mk(base + 4.5, base + 9.9, base + 4.4, base + 9.8, i + 1))  # 12
    bars.append(mk(base + 9.8, base + 10.3, base + 9.7, base + 10.0, i + 2))  # 13 pole top
    bars.append(mk(base + 10.0, base + 10.3, base + 9.6, base + 10.1, i + 3))  # 14 flag
    bars.append(mk(base + 10.1, base + 10.25, base + 9.7, base + 9.9, i + 4))  # 15 flag
    bars.append(mk(base + 10.0, base + 11.0, base + 9.9, base + 10.8, i + 5))  # 16 breakout
    return bars


def test_bull_flag_fires_with_measured_target_and_zone() -> None:
    bars = _bull_flag_bars()
    hits = detect_flags(bars)
    assert kinds(hits) == ["flag_bull"]
    h = hits[0]
    assert h.index == 16
    # move = close[13] - close[10] = 110.0 - 100.0 = 10.0
    assert h.target == round(110.8 + 10.0, 2)
    assert h.zone is not None
    # flag window = bars 13..15 (k=3)
    assert h.zone["from_ms"] == bars[13]["timestamp_ms"]
    assert h.zone["to_ms"] == bars[15]["timestamp_ms"]
    assert h.zone["top"] == 110.3 and h.zone["bottom"] == 109.6


def test_bull_flag_blocked_when_flag_too_loose() -> None:
    bars = _bull_flag_bars()
    # widen the flag range beyond flag_frac * pole (3.33): drop a flag bar low
    bars[14]["low"] = 106.0
    assert detect_flags(bars) == []


def test_bull_flag_blocked_without_breakout() -> None:
    bars = _bull_flag_bars()
    bars[16]["close"] = 110.2  # inside the flag (fHi=110.3)
    bars[16]["high"] = 110.25
    assert detect_flags(bars) == []


def test_flag_refire_cooldown_then_refire() -> None:
    bars = _bull_flag_bars()
    # An immediate second breakout bar within cooldown -> suppressed
    bars.append(mk(110.8, 112.0, 110.7, 111.9, 17))
    assert len(detect_flags(bars)) == 1
    # A fresh pole+flag+breakout far beyond the 10-bar cooldown -> second fire
    far = _bull_flag_bars(start=len(bars) + 20, base=112.0)
    filler = [mk(111.9, 112.0, 111.8, 111.9, 18 + i) for i in range(20)]
    hits = detect_flags(bars + filler + far)
    assert kinds(hits) == ["flag_bull", "flag_bull"]


def test_bear_flag_fires() -> None:
    base = 100.0
    bars = [mk(base, base + 0.1, base - 0.1, base, i) for i in range(11)]
    bars.append(mk(base, base + 0.1, base - 4.6, base - 4.5, 11))
    bars.append(mk(base - 4.5, base - 4.4, base - 9.9, base - 9.8, 12))
    bars.append(mk(base - 9.8, base - 9.7, base - 10.3, base - 10.0, 13))
    bars.append(mk(base - 10.0, base - 9.6, base - 10.3, base - 10.1, 14))
    bars.append(mk(base - 10.1, base - 9.7, base - 10.25, base - 9.9, 15))
    bars.append(mk(base - 10.0, base - 9.9, base - 11.0, base - 10.8, 16))  # breakdown
    hits = detect_flags(bars)
    assert kinds(hits) == ["flag_bear"]
    assert hits[0].target == round((base - 10.8) - 10.0, 2)


# ---------------------------------------------------------------------------
# Trendlines
# ---------------------------------------------------------------------------

def test_trendlines_from_last_two_pivots() -> None:
    # Zig-zag with pivot highs at idx 5 (105) and 12 (103); pivot lows at
    # idx 8 (95) and 15 (96). Strength 2 keeps the fixture compact.
    prices = [100, 101, 102, 103, 104, 105, 103, 99, 95, 98,
              101, 102, 103, 101, 98, 96, 99, 100]
    bars = [mk(p, p + 0.5, p - 0.5, p, i) for i, p in enumerate(prices)]
    # make the pivots strict extremes on high/low
    bars[5]["high"] = 105.9
    bars[12]["high"] = 103.9
    bars[8]["low"] = 94.1
    bars[15]["low"] = 95.1
    cfg = PatternOverlayConfig(pivot_strength=2)
    lines = detect_trendlines(bars, cfg)
    by_kind = {t.kind: t for t in lines}
    assert set(by_kind) == {"support", "resistance"}
    res = by_kind["resistance"]
    assert res.points == [[bars[5]["timestamp_ms"], 105.9], [bars[12]["timestamp_ms"], 103.9]]
    # slope = (103.9-105.9)/(12-5) per bar; projected at idx 17
    expected = 103.9 + (103.9 - 105.9) / 7 * (17 - 12)
    assert abs(res.projected[1] - round(expected, 2)) < 0.011
    sup = by_kind["support"]
    assert sup.points == [[bars[8]["timestamp_ms"], 94.1], [bars[15]["timestamp_ms"], 95.1]]


def test_trendlines_need_two_pivots_per_side() -> None:
    # Monotonic ramp — no interior pivot highs/lows at all
    bars = [mk(100 + i, 100.5 + i, 99.5 + i, 100 + i, i) for i in range(30)]
    assert detect_trendlines(bars, PatternOverlayConfig(pivot_strength=3)) == []


def test_pivot_points_strictness() -> None:
    prices = [100, 100, 100, 100, 100]
    bars = [mk(p, p + 0.5, p - 0.5, p, i) for i, p in enumerate(prices)]
    highs, lows = pivot_points(bars, 1)  # equal highs -> no strict pivot
    assert highs == [] and lows == []


# ---------------------------------------------------------------------------
# EMA / resample / payload assembly
# ---------------------------------------------------------------------------

def test_ema_series_seeds_with_sma() -> None:
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = ema_series(closes, 3)
    assert out[0] is None and out[1] is None
    assert out[2] == 2.0  # SMA seed of first 3
    assert abs(out[3] - (0.5 * 4 + 0.5 * 2.0)) < 1e-9


def test_resample_bars_5min_alignment_and_ohlcv() -> None:
    t0 = 1_750_000_200_000  # xx:x0:00 wall clock not guaranteed; use explicit floor
    base = (t0 // 300_000) * 300_000
    bars = []
    prices = [(100, 101, 99, 100.5), (100.5, 102, 100, 101.5), (101.5, 101.6, 100.2, 100.3),
              (100.3, 100.9, 99.8, 100.0), (100.0, 100.4, 99.5, 100.2), (100.2, 100.7, 100.1, 100.6)]
    for i, (o, h, low, c) in enumerate(prices):
        bars.append({"timestamp_ms": base + i * 60_000, "open": o, "high": h,
                     "low": low, "close": c, "volume": 10})
    out = resample_bars(bars, 5)
    assert len(out) == 2
    b0 = out[0]
    assert b0["timestamp_ms"] == base
    assert b0["open"] == 100 and b0["close"] == 100.2
    assert b0["high"] == 102 and b0["low"] == 99 and b0["volume"] == 50
    assert out[1]["timestamp_ms"] == base + 300_000 and out[1]["volume"] == 10


def test_build_overlay_payload_shapes() -> None:
    bars = _bull_flag_bars()
    payload = build_overlay(bars)
    assert set(payload) == {"markers", "target_lines", "flag_zones", "trendlines"}
    flag_markers = [m for m in payload["markers"] if m["kind"] == "flag_bull"]
    assert len(flag_markers) == 1
    m = flag_markers[0]
    # marker times are epoch SECONDS
    assert m["time"] == bars[16]["timestamp_ms"] // 1000
    assert m["position"] == "belowBar" and m["shape"] == "arrowUp"
    assert len(payload["target_lines"]) == 1
    assert payload["target_lines"][0]["price"] == 120.8
    assert len(payload["flag_zones"]) == 1
    assert payload["flag_zones"][0]["from_time"] == bars[13]["timestamp_ms"] // 1000
