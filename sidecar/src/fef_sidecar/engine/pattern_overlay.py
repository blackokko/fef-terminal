"""Candlestick / flag / trendline pattern detection on OHLC bars.

Faithful port of the parked Pine reference (fef_pattern_overlay.pine):
    - Bullish/bearish ENGULFING (body engulfs prior opposite body, body > prior)
    - HAMMER / SHOOTING STAR (wick fractions + EMA20 context filter)
    - THREE WHITE SOLDIERS / THREE BLACK CROWS
    - MORNING STAR / EVENING STAR
    - INSIDE-BAR COIL (compression), DOJI (off by default)
    - MEASURED-MOVE FLAGS (pole >= pole_min_pts over pole_bars, flag range
      <= flag_frac x pole across 3..6 bars, breakout beyond flag extreme,
      10-bar refire cooldown per direction, measured target = breakout
      close +/- pole height)
    - AUTO TRENDLINES from pivot highs/lows (strength N bars each side;
      last two pivots per side define the ray)

Everything here is PURE — no I/O, no state, no asyncio. Consumed by:
    - GET /chart/pattern_overlay (api.py) — chart decoration payload
    - pattern_trigger loop (engine/pattern_trigger.py) — confirmed-bar
      fires into the advisory surface / auto-trader

Bars are plain dicts shaped like the /bars endpoint output:
    {"timestamp_ms": int, "open": f, "high": f, "low": f, "close": f, "volume": f}

All thresholds live on PatternOverlayConfig so the overlay endpoint and the
live trigger share ONE source of truth, seeded from UIConfig (config_store).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Colors match the app's chart palette (ChartPane.tsx CANDLE_OPTIONS / WALL_COLORS)
_GREEN = "#3fb950"
_RED = "#f85149"
_AMBER = "#d29922"
_GRAY = "#7d8590"

_MIN_TICK = 0.01  # rng floor so a zero-range bar can't divide by zero


@dataclass(frozen=True)
class PatternOverlayConfig:
    """Detector thresholds — defaults identical to the Pine reference inputs."""

    # Candle toggles
    show_engulfing: bool = True
    show_hammer: bool = True
    show_soldiers: bool = True
    show_star: bool = True
    show_inside: bool = True
    show_doji: bool = False        # noisy — off by default (matches Pine)
    # Candle sensitivity
    hammer_wick_frac: float = 0.60       # lower wick >= this fraction of range
    hammer_other_wick_max: float = 0.25  # opposite wick <= this fraction
    soldier_body_frac: float = 0.50      # body > this fraction of range
    star_mid_body_max: float = 0.30      # middle bar body < this fraction
    doji_body_max: float = 0.10          # body <= this fraction of range
    ema_period: int = 20                 # context filter for hammer/shooter
    # Measured-move flags
    show_flags: bool = True
    pole_bars: int = 3
    pole_min_pts: float = 8.0
    flag_frac: float = 0.333
    flag_min_bars: int = 3
    flag_max_bars: int = 6
    refire_cooldown_bars: int = 10
    # Trendlines
    show_trendlines: bool = True
    pivot_strength: int = 10


@dataclass
class PatternHit:
    """One detector hit on a specific bar (all indexes into the bars list)."""

    index: int
    timestamp_ms: int
    kind: str          # engulf_bull / engulf_bear / hammer / shooting_star /
    #                    soldiers / crows / morning_star / evening_star /
    #                    inside_coil / doji / flag_bull / flag_bear
    direction: str     # BULL / BEAR / NEUTRAL
    label: str         # short marker text ("Engulf", "3 Soldiers", ...)
    price: float       # bar close at the hit
    # Flag-only extras
    target: float | None = None       # measured-move target
    zone: dict[str, Any] | None = None  # {from_ms, to_ms, top, bottom}


@dataclass
class Trendline:
    kind: str                       # support / resistance
    points: list[list[float]]       # [[t1_ms, p1], [t2_ms, p2]] older -> newer pivot
    projected: list[float] = field(default_factory=list)  # [t_last_ms, value]


def ema_series(closes: list[float], period: int) -> list[float | None]:
    """EMA with SMA seed at index period-1 (Pine ta.ema / ChartPane parity).
    Entries before the seed are None."""
    out: list[float | None] = [None] * len(closes)
    if period <= 0 or len(closes) < period:
        return out
    alpha = 2.0 / (period + 1)
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(closes)):
        prev = alpha * closes[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def _body(b: dict[str, Any]) -> float:
    return abs(b["close"] - b["open"])


def _rng(b: dict[str, Any]) -> float:
    return max(b["high"] - b["low"], _MIN_TICK)


def _up_wick(b: dict[str, Any]) -> float:
    return b["high"] - max(b["open"], b["close"])


def _dn_wick(b: dict[str, Any]) -> float:
    return min(b["open"], b["close"]) - b["low"]


def _bull(b: dict[str, Any]) -> bool:
    return b["close"] > b["open"]


def _bear(b: dict[str, Any]) -> bool:
    return b["close"] < b["open"]


def detect_candles(
    bars: list[dict[str, Any]],
    config: PatternOverlayConfig | None = None,
) -> list[PatternHit]:
    """Candlestick pattern hits across the whole bar list.

    Mirrors the Pine reference exactly, including its quirks (e.g. three
    soldiers checks body fraction on the last TWO bars only) and its
    priority order for the marker label (Engulf > Hammer > Soldiers > Star).
    Inside-bar coil / doji only label bars with no directional pattern.
    """
    cfg = config or PatternOverlayConfig()
    hits: list[PatternHit] = []
    if len(bars) < 2:
        return hits
    closes = [b["close"] for b in bars]
    ema = ema_series(closes, cfg.ema_period)

    for i in range(1, len(bars)):
        b0 = bars[i]
        b1 = bars[i - 1]
        body0, body1 = _body(b0), _body(b1)
        rng0, rng1 = _rng(b0), _rng(b1)

        bull_engulf = (
            cfg.show_engulfing and _bear(b1) and _bull(b0)
            and b0["close"] >= max(b1["open"], b1["close"])
            and b0["open"] <= min(b1["open"], b1["close"])
            and body0 > body1
        )
        bear_engulf = (
            cfg.show_engulfing and _bull(b1) and _bear(b0)
            and b0["close"] <= min(b1["open"], b1["close"])
            and b0["open"] >= max(b1["open"], b1["close"])
            and body0 > body1
        )
        e = ema[i]
        hammer = (
            cfg.show_hammer and e is not None
            and _dn_wick(b0) >= cfg.hammer_wick_frac * rng0
            and _up_wick(b0) <= cfg.hammer_other_wick_max * rng0
            and b0["close"] < e
        )
        shooting = (
            cfg.show_hammer and e is not None
            and _up_wick(b0) >= cfg.hammer_wick_frac * rng0
            and _dn_wick(b0) <= cfg.hammer_other_wick_max * rng0
            and b0["close"] > e
        )
        soldiers = crows = False
        if i >= 2 and cfg.show_soldiers:
            b2 = bars[i - 2]
            soldiers = (
                _bull(b0) and _bull(b1) and _bull(b2)
                and b0["close"] > b1["close"] > b2["close"]
                and body0 > cfg.soldier_body_frac * rng0
                and body1 > cfg.soldier_body_frac * rng1
            )
            crows = (
                _bear(b0) and _bear(b1) and _bear(b2)
                and b0["close"] < b1["close"] < b2["close"]
                and body0 > cfg.soldier_body_frac * rng0
                and body1 > cfg.soldier_body_frac * rng1
            )
        morn = even = False
        if i >= 2 and cfg.show_star:
            b2 = bars[i - 2]
            body2, rng2 = _body(b2), _rng(b2)
            mid_small = body1 < cfg.star_mid_body_max * rng1
            morn = (
                _bear(b2) and body2 > 0.5 * rng2 and mid_small
                and _bull(b0) and b0["close"] > (b2["open"] + b2["close"]) / 2
            )
            even = (
                _bull(b2) and body2 > 0.5 * rng2 and mid_small
                and _bear(b0) and b0["close"] < (b2["open"] + b2["close"]) / 2
            )

        ts = int(b0["timestamp_ms"])
        close = float(b0["close"])
        bull_pat = bull_engulf or hammer or soldiers or morn
        bear_pat = bear_engulf or shooting or crows or even

        if bull_pat:
            if bull_engulf:
                kind, label = "engulf_bull", "Engulf"
            elif hammer:
                kind, label = "hammer", "Hammer"
            elif soldiers:
                kind, label = "soldiers", "3 Soldiers"
            else:
                kind, label = "morning_star", "M-Star"
            hits.append(PatternHit(i, ts, kind, "BULL", label, close))
        if bear_pat:
            if bear_engulf:
                kind, label = "engulf_bear", "Engulf"
            elif shooting:
                kind, label = "shooting_star", "Shooter"
            elif crows:
                kind, label = "crows", "3 Crows"
            else:
                kind, label = "evening_star", "E-Star"
            hits.append(PatternHit(i, ts, kind, "BEAR", label, close))

        if not bull_pat and not bear_pat:
            inside = cfg.show_inside and b0["high"] < b1["high"] and b0["low"] > b1["low"]
            doji = cfg.show_doji and body0 <= cfg.doji_body_max * rng0
            if inside:
                hits.append(PatternHit(i, ts, "inside_coil", "NEUTRAL", "coil", close))
            elif doji:
                hits.append(PatternHit(i, ts, "doji", "NEUTRAL", "doji", close))
    return hits


def detect_flags(
    bars: list[dict[str, Any]],
    config: PatternOverlayConfig | None = None,
) -> list[PatternHit]:
    """Measured-move flag breakouts with per-direction refire cooldown.

    Pine reference logic per confirmed bar i:
        for k in flag_min_bars..flag_max_bars:
            move = close[i-k] - close[i-k-pole_bars]     # the pole
            if |move| >= pole_min_pts:
                flag hi/lo over the k bars BEFORE i (i-k .. i-1)
                if (hi - lo) <= flag_frac * |move|:
                    bull fire:  move > 0 and close[i] > hi and cooldown ok
                    bear fire:  move < 0 and close[i] < lo and cooldown ok
                    (break out of the k loop on fire)
    Target = close +/- |move|. Zone = the k flag bars' bounding box.
    """
    cfg = config or PatternOverlayConfig()
    hits: list[PatternHit] = []
    if not cfg.show_flags:
        return hits
    last_bull = -10**9
    last_bear = -10**9
    start = cfg.flag_min_bars + cfg.pole_bars
    for i in range(start, len(bars)):
        for k in range(cfg.flag_min_bars, cfg.flag_max_bars + 1):
            if i - k - cfg.pole_bars < 0:
                break
            move = bars[i - k]["close"] - bars[i - k - cfg.pole_bars]["close"]
            if abs(move) < cfg.pole_min_pts:
                continue
            f_hi = max(bars[j]["high"] for j in range(i - k, i))
            f_lo = min(bars[j]["low"] for j in range(i - k, i))
            if (f_hi - f_lo) > cfg.flag_frac * abs(move):
                continue
            close = float(bars[i]["close"])
            ts = int(bars[i]["timestamp_ms"])
            zone = {
                "from_ms": int(bars[i - k]["timestamp_ms"]),
                "to_ms": int(bars[i - 1]["timestamp_ms"]),
                "top": f_hi,
                "bottom": f_lo,
            }
            if move > 0 and close > f_hi and i - last_bull > cfg.refire_cooldown_bars:
                last_bull = i
                hits.append(PatternHit(
                    i, ts, "flag_bull", "BULL",
                    f"BULL FLAG > {round(close + abs(move), 2)}",
                    close, target=round(close + abs(move), 2), zone=zone,
                ))
                break
            if move < 0 and close < f_lo and i - last_bear > cfg.refire_cooldown_bars:
                last_bear = i
                hits.append(PatternHit(
                    i, ts, "flag_bear", "BEAR",
                    f"BEAR FLAG > {round(close - abs(move), 2)}",
                    close, target=round(close - abs(move), 2), zone=zone,
                ))
                break
    return hits


def pivot_points(
    bars: list[dict[str, Any]],
    strength: int,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """(pivot_highs, pivot_lows) as (index, price) tuples, oldest first.

    A pivot high at i means high[i] is STRICTLY greater than the highs of
    the `strength` bars on each side (ties disqualify — matches Pine
    ta.pivothigh semantics closely enough for ray anchoring)."""
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    n = len(bars)
    for i in range(strength, n - strength):
        h = bars[i]["high"]
        lo = bars[i]["low"]
        window = range(i - strength, i + strength + 1)
        if all(h > bars[j]["high"] for j in window if j != i):
            highs.append((i, float(h)))
        if all(lo < bars[j]["low"] for j in window if j != i):
            lows.append((i, float(lo)))
    return highs, lows


def detect_trendlines(
    bars: list[dict[str, Any]],
    config: PatternOverlayConfig | None = None,
) -> list[Trendline]:
    """Support/resistance rays through the last two pivot lows/highs.

    Slope is computed in bar-index space (Pine line.new in bar coords) and
    projected to the latest bar so the frontend can draw a simple 2-point
    line series from the older pivot to the projection."""
    cfg = config or PatternOverlayConfig()
    if not cfg.show_trendlines or len(bars) < 2 * cfg.pivot_strength + 1:
        return []
    highs, lows = pivot_points(bars, cfg.pivot_strength)
    out: list[Trendline] = []
    last_idx = len(bars) - 1
    last_ts = int(bars[last_idx]["timestamp_ms"])

    def _ray(kind: str, pivots: list[tuple[int, float]]) -> Trendline | None:
        if len(pivots) < 2:
            return None
        (i2, p2), (i1, p1) = pivots[-2], pivots[-1]  # older, newer
        if i1 == i2:
            return None
        slope = (p1 - p2) / (i1 - i2)
        projected = p1 + slope * (last_idx - i1)
        return Trendline(
            kind=kind,
            points=[
                [int(bars[i2]["timestamp_ms"]), round(p2, 2)],
                [int(bars[i1]["timestamp_ms"]), round(p1, 2)],
            ],
            projected=[last_ts, round(projected, 2)],
        )

    res = _ray("resistance", highs)
    sup = _ray("support", lows)
    if res:
        out.append(res)
    if sup:
        out.append(sup)
    return out


def resample_bars(bars: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    """Aggregate 1-min bars into `minutes`-sized buckets aligned to wall-clock
    boundaries (floor of timestamp), matching IBKR intraday bar alignment.
    Input must be sorted ascending by timestamp_ms; output is too."""
    if minutes <= 1:
        return list(bars)
    bucket_ms = minutes * 60_000
    out: list[dict[str, Any]] = []
    cur_key: int | None = None
    cur: dict[str, Any] | None = None
    for b in bars:
        key = (int(b["timestamp_ms"]) // bucket_ms) * bucket_ms
        if key != cur_key:
            if cur is not None:
                out.append(cur)
            cur_key = key
            cur = {
                "timestamp_ms": key,
                "open": b["open"], "high": b["high"],
                "low": b["low"], "close": b["close"],
                "volume": b.get("volume", 0) or 0,
            }
        else:
            assert cur is not None
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] += b.get("volume", 0) or 0
    if cur is not None:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# Overlay payload assembly — the /chart/pattern_overlay response body
# ---------------------------------------------------------------------------

_MARKER_STYLE = {
    "BULL": {"position": "belowBar", "shape": "arrowUp", "color": _GREEN},
    "BEAR": {"position": "aboveBar", "shape": "arrowDown", "color": _RED},
    "NEUTRAL": {"position": "aboveBar", "shape": "circle", "color": _AMBER},
}


def _marker_for(hit: PatternHit) -> dict[str, Any]:
    style = dict(_MARKER_STYLE[hit.direction])
    if hit.kind == "doji":
        style["color"] = _GRAY
    size = 2 if hit.kind.startswith("flag_") else 1
    return {
        "time": int(hit.timestamp_ms // 1000),  # epoch SECONDS for lightweight-charts
        "position": style["position"],
        "shape": style["shape"],
        "color": style["color"],
        "text": hit.label,
        "size": size,
        "kind": hit.kind,
    }


def build_overlay(
    bars: list[dict[str, Any]],
    config: PatternOverlayConfig | None = None,
) -> dict[str, Any]:
    """Full overlay payload for a bar window.

    Returns {markers, target_lines, flag_zones, trendlines} where marker/line
    times are epoch SECONDS (chart-native) and zone times are epoch ms."""
    cfg = config or PatternOverlayConfig()
    candle_hits = detect_candles(bars, cfg)
    flag_hits = detect_flags(bars, cfg)
    trendlines = detect_trendlines(bars, cfg)

    markers = [_marker_for(h) for h in sorted(
        candle_hits + flag_hits, key=lambda h: (h.timestamp_ms, h.kind))]

    target_lines = [
        {
            "price": h.target,
            "from_time": int(h.timestamp_ms // 1000),
            "label": ("FLAG TGT " if h.direction == "BULL" else "FLAG TGT ") + f"{h.target}",
            "color": _GREEN if h.direction == "BULL" else _RED,
            "dir": h.direction,
        }
        for h in flag_hits if h.target is not None
    ]
    flag_zones = [
        {
            "from_time": int(h.zone["from_ms"] // 1000),
            "to_time": int(h.zone["to_ms"] // 1000),
            "top": h.zone["top"],
            "bottom": h.zone["bottom"],
            "dir": h.direction,
        }
        for h in flag_hits if h.zone is not None
    ]
    trendline_payload = [
        {
            "kind": t.kind,
            "points": [[int(p[0] // 1000), p[1]] for p in t.points],
            "projected": [int(t.projected[0] // 1000), t.projected[1]] if t.projected else None,
        }
        for t in trendlines
    ]
    return {
        "markers": markers,
        "target_lines": target_lines,
        "flag_zones": flag_zones,
        "trendlines": trendline_payload,
    }
