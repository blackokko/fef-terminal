"""VWAP directional signal detectors — anchored VWAP + volume-weighted bands.

Pure functions over an OHLCV series WITH VOLUME (use ES, not SPX cash — the
index has no traded volume). Phase 0 research detectors; not live-wired.
Direction from structure: reclaim/reject-long -> CALLS, loss/reject-short -> PUTS.

Confirmation windows are expressed in minutes and converted to bar counts via
the series' bar_minutes, so the same logic works at 5-min (0DTE) or 30-min
(1DTE). All confirmations fire WITHOUT lookahead — a reject that needs "no
fresh low for 5 bars" fires on the bar that completes those 5 bars.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field

from fef_sidecar import et_time
from fef_sidecar.engine.signals.dale import CALLS, PUTS, Bar, Signal

# Real ET (DST-correct). On RTH bars the session/week keys are identical to the
# old fixed UTC-4 in both EDT and EST (RTH never comes within an hour of ET
# midnight); only overnight bars near midnight could key differently.
_ET = et_time.ET


def _et(ts_ms: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ts_ms / 1000, _ET)


def session_key(ts_ms: int) -> str:
    return _et(ts_ms).strftime("%Y-%m-%d")


def week_key(ts_ms: int) -> str:
    d = _et(ts_ms)
    monday = d - _dt.timedelta(days=d.weekday())
    return monday.strftime("%Y-%m-%d")


def _typical(b: Bar) -> float:
    return (b.high + b.low + b.close) / 3.0


@dataclass
class _VwapState:
    vwap: float
    sigma: float
    lower1: float
    upper1: float
    lower2: float
    upper2: float


def anchored_vwap(bars: list[Bar], key_fn) -> list[_VwapState]:
    """Per-bar anchored VWAP + volume-weighted std, reset whenever key_fn
    changes (session / week). Uses typical price (H+L+C)/3, volume-weighted.
    """
    out: list[_VwapState] = []
    cur_key = None
    cum_v = cum_pv = cum_pv2 = 0.0
    for b in bars:
        k = key_fn(b.ts_ms)
        if k != cur_key:
            cur_key = k
            cum_v = cum_pv = cum_pv2 = 0.0
        tp = _typical(b)
        v = max(b.volume, 0.0)
        cum_v += v
        cum_pv += tp * v
        cum_pv2 += tp * tp * v
        if cum_v > 0:
            vwap = cum_pv / cum_v
            var = max(cum_pv2 / cum_v - vwap * vwap, 0.0)
            sigma = math.sqrt(var)
        else:
            vwap = tp
            sigma = 0.0
        out.append(_VwapState(
            vwap=vwap, sigma=sigma,
            lower1=vwap - sigma, upper1=vwap + sigma,
            lower2=vwap - 2 * sigma, upper2=vwap + 2 * sigma,
        ))
    return out


# ---------------------------------------------------------------------------
# Volume profile (developing, from session open) — HVN/LVN confluence
# ---------------------------------------------------------------------------


def _developing_hvns(bars_so_far: list[Bar], *, bin_pts: float = 2.0,
                     hvn_quantile: float = 0.7) -> list[float]:
    """High-volume-node price centers from session-open through the given bars.
    Bins typical price by bin_pts; HVN = bins at/above the hvn_quantile of bin
    volume. No lookahead — caller passes only bars up to the signal bar."""
    if not bars_so_far:
        return []
    vol_by_bin: dict[int, float] = {}
    for b in bars_so_far:
        bucket = int(_typical(b) // bin_pts)
        vol_by_bin[bucket] = vol_by_bin.get(bucket, 0.0) + max(b.volume, 0.0)
    if not vol_by_bin:
        return []
    vols = sorted(vol_by_bin.values())
    idx = min(len(vols) - 1, int(hvn_quantile * len(vols)))
    threshold = vols[idx]
    return [(bucket + 0.5) * bin_pts for bucket, v in vol_by_bin.items() if v >= threshold and threshold > 0]


def _hvn_confluence(level: float, hvns: list[float], *, tol_pts: float = 5.0) -> bool:
    return any(abs(level - h) <= tol_pts for h in hvns)


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------


def detect(
    bars: list[Bar],
    *,
    timeframe_min: int,
    reclaim_min: int = 15,
    hold_bars: int = 2,
    reject_hold_bars: int = 5,
) -> list[Signal]:
    """Detect VWAP signals on `bars` (must carry volume).

    - vwap_reclaim_long: below session VWAP for >= reclaim_min, then closes
      above and holds `hold_bars` bars. CALLS.
    - vwap_loss_short: above for >= reclaim_min, closes below, holds. PUTS.
    - vwap_band_reject_long: touches lower 2σ, closes back inside, no fresh low
      for `reject_hold_bars` bars. CALLS. (fires on the completing bar)
    - vwap_band_reject_short: symmetric at upper 2σ. PUTS.
    """
    n = len(bars)
    sv = anchored_vwap(bars, session_key)
    reclaim_bars = max(1, math.ceil(reclaim_min / timeframe_min))
    sigs: list[Signal] = []

    # side relative to session VWAP per bar: -1 below, +1 above, 0 at
    side = [(-1 if b.close < s.vwap else (1 if b.close > s.vwap else 0)) for b, s in zip(bars, sv, strict=True)]

    # --- reclaim / loss ---
    for i in range(n):
        sk = session_key(bars[i].ts_ms)
        # reclaim_long: prior `reclaim_bars` all below VWAP (same session), then
        # this bar + (hold_bars-1) following all close above. Fire on the last hold bar.
        below_start = i - reclaim_bars
        if below_start >= 0 and all(
            side[k] < 0 and session_key(bars[k].ts_ms) == sk for k in range(below_start, i)
        ):
            end = i + hold_bars
            if end <= n and all(
                bars[k].close > sv[k].vwap and session_key(bars[k].ts_ms) == sk for k in range(i, end)
            ):
                j = end - 1
                hv = _developing_hvns(bars[: j + 1])
                sigs.append(Signal(
                    ts_ms=bars[j].ts_ms, kind="vwap_reclaim_long", direction=CALLS,
                    timeframe_min=timeframe_min, price=bars[j].close, trend="",
                    detail={"vwap": sv[j].vwap, "hvn_confluence": _hvn_confluence(sv[j].vwap, hv)},
                ))
        above_start = i - reclaim_bars
        if above_start >= 0 and all(
            side[k] > 0 and session_key(bars[k].ts_ms) == sk for k in range(above_start, i)
        ):
            end = i + hold_bars
            if end <= n and all(
                bars[k].close < sv[k].vwap and session_key(bars[k].ts_ms) == sk for k in range(i, end)
            ):
                j = end - 1
                hv = _developing_hvns(bars[: j + 1])
                sigs.append(Signal(
                    ts_ms=bars[j].ts_ms, kind="vwap_loss_short", direction=PUTS,
                    timeframe_min=timeframe_min, price=bars[j].close, trend="",
                    detail={"vwap": sv[j].vwap, "hvn_confluence": _hvn_confluence(sv[j].vwap, hv)},
                ))

    # --- band rejections (no lookahead: fire on the bar completing the hold) ---
    for i in range(n):
        sk = session_key(bars[i].ts_ms)
        # lower-band reject: bar i touches lower2 and closes back inside
        if bars[i].low <= sv[i].lower2 and bars[i].close > sv[i].lower2:
            end = i + reject_hold_bars
            if end < n and all(
                session_key(bars[k].ts_ms) == sk and bars[k].low >= bars[i].low
                for k in range(i + 1, end + 1)
            ):
                j = end
                hv = _developing_hvns(bars[: j + 1])
                sigs.append(Signal(
                    ts_ms=bars[j].ts_ms, kind="vwap_band_reject_long", direction=CALLS,
                    timeframe_min=timeframe_min, price=bars[j].close, trend="",
                    detail={"reacted_level": sv[i].lower2, "touch_ts": bars[i].ts_ms,
                            "hvn_confluence": _hvn_confluence(sv[i].lower2, hv)},
                ))
        if bars[i].high >= sv[i].upper2 and bars[i].close < sv[i].upper2:
            end = i + reject_hold_bars
            if end < n and all(
                session_key(bars[k].ts_ms) == sk and bars[k].high <= bars[i].high
                for k in range(i + 1, end + 1)
            ):
                j = end
                hv = _developing_hvns(bars[: j + 1])
                sigs.append(Signal(
                    ts_ms=bars[j].ts_ms, kind="vwap_band_reject_short", direction=PUTS,
                    timeframe_min=timeframe_min, price=bars[j].close, trend="",
                    detail={"reacted_level": sv[i].upper2, "touch_ts": bars[i].ts_ms,
                            "hvn_confluence": _hvn_confluence(sv[i].upper2, hv)},
                ))

    from fef_sidecar.engine.signals.dale import _dedup
    return _dedup(sigs)
