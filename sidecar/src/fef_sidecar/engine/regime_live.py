"""Live Market Regime tiles — VALIDATED measurements only (GET /regime/live).

Replaces the retired session-'vibe' classifier display (engine/regime.py
RegimeDetector: PIN_DAY/TREND_DAY/CHOP/... — unvalidated, documented
mislabeling e.g. 5/27's chop day scored 67% TREND_DAY, and consumed by
nothing in the trading path). Every tile here reads a measurement that is
either validated by a study or is a plain factual state of the bot:

  range_regime  — trailing-30-min SPX range + its time-of-day percentile
                  (the 2026-07-02 22,683-fire chop-gate sweep) and whether
                  the live chop gate is currently SKIPPING scanner fires.
  expected_move — VIX tercile vs the trailing 20 sessions + 30-min realized
                  -vol TOD percentile (magnitude study: VIX/RVOL rank-order
                  |next move| ~2-3x between extremes, OOS) -> a plain
                  "~0.5x / ~1x / ~2-3x normal next-15m move" label, plus the
                  POST-CAPITULATION flag (|15-min move| >= max(1.5x trailing
                  -median 15-min |move|, 0.25% of spot) => expect ~2x move —
                  ported from the phase-detector/magnitude study).
  flow_bias     — current drift bias (BULLISH/BEARISH/NEUTRAL from the
                  engine inputs) + whether the bias-disagree veto is armed.
  events        — today's deterministic calendar flags (FOMC hardcoded;
                  CPI/NFP approximate; OPEX/EOM/quarter-end rule-based) +
                  the next event within 3 sessions. FOMC: engine's worst
                  class (-$61/ct pre-announcement), ~2.2x vol after 14:00.
  dealer_gamma  — long/short/transition/unknown from the latest exposure
                  snapshot (gamma_regime.classify); degrades to "unknown".

Every tile: {reading, label, effect_on_bot, updated_at}. FAIL-OPEN: any
missing data or error yields an "n/a" tile — this endpoint must never 500
while the user is live-trading.
"""

from __future__ import annotations

import logging
import math
import time as _time
from statistics import median
from typing import Any, Iterable

from fef_sidecar.config_store import load_config
from fef_sidecar.engine import chop_gate as _cg
from fef_sidecar.engine import event_calendar as _cal
from fef_sidecar.engine.chop_gate import (
    _ET,
    MS_PER_MIN,
    percentile_of,
    session_date_et,
    tod_window_samples,
)

logger = logging.getLogger(__name__)

# ---- Tercile boundaries (percentile space) ----
TERCILE_LO = 100.0 / 3.0
TERCILE_HI = 200.0 / 3.0

# ---- Expected-move constants ----
RVOL_WINDOW_MIN = 30          # realized-vol window (minutes)
RVOL_MIN_BARS = 8             # same robustness floor family as chop_gate
RVOL_MIN_SPAN_FRAC = 0.5
POSTCAP_WINDOW_MIN = 15       # |15-min move| for the post-capitulation rule
POSTCAP_MEDIAN_MULT = 1.5     # >= 1.5x trailing-median 15-min |move| ...
POSTCAP_MIN_FRAC_SPOT = 0.0025  # ... AND >= 0.25% of spot
POSTCAP_MIN_SAMPLES = 20      # need this many m15 samples before flagging
VIX_N_SESSIONS = 20           # trailing VIX distribution depth
VIX_MIN_SESSIONS = 5          # below this the VIX tercile is n/a
VIX_LOOKBACK_DAYS = 45        # calendar days queried to find 20 sessions
FOMC_STATEMENT_MINUTE = 14 * 60  # 14:00 ET

_MOVE_LABELS = (
    "~0.5x normal next-15m move",
    "~1x normal next-15m move",
    "~2-3x normal next-15m move",
)


def _tile(reading: str, label: str, effect: str,
          updated_at: int | None, **extra: Any) -> dict[str, Any]:
    t: dict[str, Any] = {
        "reading": reading, "label": label,
        "effect_on_bot": effect, "updated_at": updated_at,
    }
    t.update(extra)
    return t


def _na_tile(reason: str, updated_at: int | None = None, **extra: Any) -> dict[str, Any]:
    return _tile("n/a", "n/a", reason, updated_at, **extra)


def _tercile_label(pct: float) -> str:
    if pct < TERCILE_LO:
        return "low"
    if pct < TERCILE_HI:
        return "mid"
    return "high"


def _tercile_idx(pct: float) -> int:
    return 0 if pct < TERCILE_LO else (1 if pct < TERCILE_HI else 2)


def _ordinal(pct: float) -> str:
    n = int(round(pct))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _minute_of_day_et(at_ms: int) -> int:
    from datetime import datetime as _dtm
    dt = _dtm.fromtimestamp(at_ms / 1000, _ET)
    return dt.hour * 60 + dt.minute


# ---------------------------------------------------------------------------
# Pure helpers — realized vol + 15-min move (unit-testable, plain bar inputs)
# ---------------------------------------------------------------------------


def _bar_tsc(bar: Any) -> tuple[int, float]:
    """Normalize one bar to (timestamp_ms, close). Accepts HistoricalBar rows,
    dicts, or (ts, close) sequences — fixture-friendly like chop_gate."""
    if isinstance(bar, dict):
        return int(bar["timestamp_ms"]), float(bar["close"])
    if isinstance(bar, (tuple, list)):
        return int(bar[0]), float(bar[-1])
    return int(bar.timestamp_ms), float(bar.close)


def _minute_closes(bars: Iterable[Any]) -> list[tuple[int, float]]:
    """One (minute_ms, close) per minute — last write wins on duplicates."""
    by_minute: dict[int, float] = {}
    for b in bars:
        ts, c = _bar_tsc(b)
        by_minute[(ts // MS_PER_MIN) * MS_PER_MIN] = c
    return sorted(by_minute.items())


def rvol30(
    bars_1min: Iterable[Any],
    at_ms: int,
    *,
    window_min: int = RVOL_WINDOW_MIN,
    min_bars: int = RVOL_MIN_BARS,
    min_span_frac: float = RVOL_MIN_SPAN_FRAC,
) -> float | None:
    """Realized vol of the trailing `window_min` minutes ending at `at_ms`:
    sqrt(sum of squared minute-close log returns), in return units (e.g.
    0.001 = 0.1%). None (n/a) on insufficient bars/coverage — the same
    robustness-floor discipline as chop_gate.r30."""
    window_ms = window_min * MS_PER_MIN
    lo_ts = at_ms - window_ms
    pts = [p for p in _minute_closes(bars_1min) if lo_ts <= p[0] < at_ms]
    if len(pts) < min_bars:
        return None
    if pts[-1][0] - pts[0][0] < min_span_frac * window_ms:
        return None
    acc = 0.0
    for (_, c0), (_, c1) in zip(pts, pts[1:]):
        if c0 > 0 and c1 > 0:
            r = math.log(c1 / c0)
            acc += r * r
    return math.sqrt(acc)


def build_rvol_tod_distribution(
    bars_by_session: dict[str, Any],
    *,
    session_date: str,
    n_sessions: int = _cg.N_PRIOR_SESSIONS,
    window_min: int = RVOL_WINDOW_MIN,
) -> list[tuple[str, int, float]]:
    """rvol30 sampled at every bar close across the prior `n_sessions`
    sessions — same shape as chop_gate.build_tod_distribution samples
    ([(session_date, minute_of_day_et, value)]) so the TOD-window matching
    and percentile helpers are shared."""
    prior = sorted(d for d in bars_by_session if d < session_date)[-n_sessions:]
    window_ms = window_min * MS_PER_MIN
    samples: list[tuple[str, int, float]] = []
    for d in prior:
        pts = _minute_closes(bars_by_session[d])
        start_idx = 0
        for i, (ts, _c) in enumerate(pts):
            eval_ms = ts + MS_PER_MIN  # rvol as of this bar's close
            lo_ts = eval_ms - window_ms
            while pts[start_idx][0] < lo_ts:
                start_idx += 1
            win = pts[start_idx: i + 1]
            if len(win) < RVOL_MIN_BARS:
                continue
            if win[-1][0] - win[0][0] < RVOL_MIN_SPAN_FRAC * window_ms:
                continue
            acc = 0.0
            for (_, c0), (_, c1) in zip(win, win[1:]):
                if c0 > 0 and c1 > 0:
                    r = math.log(c1 / c0)
                    acc += r * r
            samples.append((d, _minute_of_day_et(eval_ms), math.sqrt(acc)))
    return samples


def move15(
    bars_1min: Iterable[Any],
    at_ms: int,
    *,
    window_min: int = POSTCAP_WINDOW_MIN,
) -> tuple[float | None, float | None, int]:
    """(current |15-min move|, trailing-median |15-min move|, n_samples).

    Samples m(t) = |close(t) - close(t - window)| for every minute t at or
    before at_ms that has a close exactly `window_min` minutes earlier.
    Current = the sample at the latest such minute; median = over ALL
    samples up to at_ms (the session-so-far trailing median the post-cap
    rule ranks against)."""
    window_ms = window_min * MS_PER_MIN
    pts = [p for p in _minute_closes(bars_1min) if p[0] <= at_ms]
    by_minute = dict(pts)
    samples: list[tuple[int, float]] = []
    for m, c in pts:
        c_prev = by_minute.get(m - window_ms)
        if c_prev is not None:
            samples.append((m, abs(c - c_prev)))
    if not samples:
        return None, None, 0
    vals = [v for _, v in samples]
    return samples[-1][1], float(median(vals)), len(samples)


def post_cap_fired(
    cur_m15: float | None, med_m15: float | None, n_samples: int, spot: float | None,
) -> tuple[bool, float | None]:
    """The validated post-capitulation rule (phase-detector/magnitude study):
    |15-min move| >= max(1.5 x trailing-median 15-min |move|, 0.25% of spot).
    Returns (fired, threshold). Never fires on thin data."""
    if cur_m15 is None or med_m15 is None or n_samples < POSTCAP_MIN_SAMPLES:
        return False, None
    if not spot or spot <= 0:
        return False, None
    threshold = max(POSTCAP_MEDIAN_MULT * med_m15, POSTCAP_MIN_FRAC_SPOT * spot)
    return cur_m15 >= threshold, threshold


# ---------------------------------------------------------------------------
# Tile a — RANGE REGIME (chop gate's own assessment + live gate effect)
# ---------------------------------------------------------------------------


async def _range_regime_tile(state: Any, at_ms: int) -> dict[str, Any]:
    gate_enabled, threshold = True, _cg.DEFAULT_THRESHOLD_PCT
    try:
        cfg = load_config()
        gate_enabled = bool(cfg.chop_gate_enabled)
        threshold = float(cfg.chop_gate_pct_threshold)
    except Exception:
        logger.exception("regime_live: config read failed — using gate defaults")

    a = await _cg.assess_chop(state, at_ms=at_ms)
    extra = {
        "r30": round(a.r30, 2) if a.r30 is not None else None,
        "percentile": round(a.percentile, 1) if a.percentile is not None else None,
        "tod_bucket": a.tod_bucket,
        "gate_enabled": gate_enabled,
        "gate_threshold_pct": threshold,
        "bar_symbol": a.bar_symbol,
    }
    if a.percentile is None:
        reading = (
            f"r30 {a.r30:.1f}pts — percentile n/a" if a.r30 is not None else "n/a"
        )
        return _tile(
            reading, "unknown",
            f"chop gate passes OPEN (data gap: {a.data_gap})",
            at_ms, **extra,
        )

    label = (
        "compressed" if a.percentile < TERCILE_LO
        else ("mid-range" if a.percentile < TERCILE_HI else "expanded")
    )
    reading = (
        f"r30 {a.r30:.1f}pts — {_ordinal(a.percentile)} %ile for "
        f"{a.tod_bucket} ET ({a.n_sessions} prior sessions)"
    )
    if gate_enabled and a.percentile < threshold:
        effect = (
            f"SKIPPING fires — chop gate: r30 percentile {a.percentile:.0f} "
            f"< {threshold:.0f} (compressed regimes lose -$25 to -$48/ct)"
        )
    elif gate_enabled:
        effect = f"fires allowed (chop gate armed, skips below {threshold:.0f}th %ile)"
    else:
        effect = "chop gate DISABLED — fires allowed regardless of range regime"
    return _tile(reading, label, effect, at_ms, **extra)


# ---------------------------------------------------------------------------
# Tile b — EXPECTED MOVE (VIX tercile + rvol TOD percentile + post-cap flag)
# ---------------------------------------------------------------------------


async def _vix_context(state: Any, at_ms: int) -> tuple[float | None, list[float]]:
    """(current VIX, prior-session closing values <= VIX_N_SESSIONS).

    Distribution = last vix_sample of each prior ET session in the trailing
    VIX_LOOKBACK_DAYS window, cached per session on state. Current = live
    state.last_vix, falling back to today's latest sample."""
    session = session_date_et(at_ms)
    cache = getattr(state, "regime_vix_dist_cache", None)
    if cache is None:
        cache = {}
        try:
            state.regime_vix_dist_cache = cache
        except Exception:
            pass

    entry = cache.get(session)
    today_last: float | None = None
    if entry is None:
        from sqlmodel import select
        from fef_sidecar.db.engine import get_session
        from fef_sidecar.db.models import VixSample

        by_session: dict[str, float] = {}
        async with get_session() as db:
            stmt = (
                select(VixSample)
                .where(VixSample.timestamp_ms >= at_ms - VIX_LOOKBACK_DAYS * 86_400_000)
                .where(VixSample.timestamp_ms <= at_ms)
                .order_by(VixSample.timestamp_ms.asc())  # type: ignore[attr-defined]
            )
            for row in (await db.exec(stmt)).all():
                by_session[session_date_et(row.timestamp_ms)] = float(row.value)
        today_last = by_session.pop(session, None)
        prior = [by_session[d] for d in sorted(by_session)][-VIX_N_SESSIONS:]
        entry = {"prior": prior, "today_last": today_last}
        cache.clear()
        cache[session] = entry
    cur = getattr(state, "last_vix", None)
    if cur is None:
        cur = entry.get("today_last")
    return (float(cur) if cur is not None else None), list(entry["prior"])


async def _load_rvol_distribution(state: Any, at_ms: int) -> list[tuple[str, int, float]]:
    """Prior-20-session TOD rvol30 distribution for the SPX ruler, cached per
    (symbol, session) on state — one build per session, same pattern as
    chop_gate._load_distribution."""
    session = session_date_et(at_ms)
    cache = getattr(state, "regime_rvol_dist_cache", None)
    if cache is None:
        cache = {}
        try:
            state.regime_rvol_dist_cache = cache
        except Exception:
            pass
    key = (_cg.BAR_SYMBOL, session)
    entry = cache.get(key)
    if entry is not None:
        return entry

    from fef_sidecar.db.repository import SignalIterRepository
    bars = await SignalIterRepository.query_bars(
        _cg.BAR_SYMBOL, "1 min",
        from_ms=at_ms - _cg.LOOKBACK_CALENDAR_DAYS * 86_400_000,
        to_ms=at_ms,
        limit=40_000,
    )
    bars_by_session: dict[str, list[Any]] = {}
    for b in bars:
        bars_by_session.setdefault(session_date_et(b.timestamp_ms), []).append(b)
    samples = build_rvol_tod_distribution(bars_by_session, session_date=session)
    for k in [k for k in cache if k != key]:
        cache.pop(k, None)
    cache[key] = samples
    logger.info(
        "regime_live: built TOD rvol distribution for %s %s — %d samples",
        _cg.BAR_SYMBOL, session, len(samples),
    )
    return samples


async def _expected_move_tile(state: Any, at_ms: int) -> dict[str, Any]:
    # --- VIX tercile vs trailing 20 sessions ---
    vix_now: float | None = None
    vix_pct: float | None = None
    n_vix_sessions = 0
    try:
        vix_now, prior = await _vix_context(state, at_ms)
        n_vix_sessions = len(prior)
        if vix_now is not None and n_vix_sessions >= VIX_MIN_SESSIONS:
            vix_pct = percentile_of(prior, vix_now)
    except Exception:
        logger.exception("regime_live: VIX context failed — n/a")

    # --- current-session bars (one query serves rvol30 + post-cap) ---
    cur_bars: list[Any] = []
    try:
        from fef_sidecar.db.repository import SignalIterRepository
        cur_bars = await SignalIterRepository.query_bars(
            _cg.BAR_SYMBOL, "1 min",
            from_ms=at_ms - 8 * 3_600_000,  # covers the session from open
            to_ms=at_ms,
            limit=2_000,
        )
    except Exception:
        logger.exception("regime_live: current-session bar query failed")

    # --- 30-min realized-vol TOD percentile ---
    rvol_pct: float | None = None
    rvol_cur: float | None = None
    try:
        rvol_cur = rvol30(cur_bars, at_ms)
        if rvol_cur is not None:
            samples = await _load_rvol_distribution(state, at_ms)
            sel = tod_window_samples(samples, at_ms)
            if (len(sel) >= _cg.MIN_TOD_SAMPLES
                    and len({d for d, _ in sel}) >= _cg.MIN_TOD_SESSIONS):
                rvol_pct = percentile_of([v for _, v in sel], rvol_cur)
    except Exception:
        logger.exception("regime_live: rvol percentile failed — n/a")

    # --- post-capitulation flag ---
    fired = False
    cur_m15 = med_m15 = threshold = None
    n15 = 0
    try:
        cur_m15, med_m15, n15 = move15(cur_bars, at_ms)
        closes = _minute_closes(cur_bars)
        spot = closes[-1][1] if closes else (getattr(state, "last_spx_price", 0.0) or None)
        fired, threshold = post_cap_fired(cur_m15, med_m15, n15, spot)
    except Exception:
        logger.exception("regime_live: post-cap flag failed — not fired")

    # --- lo-VIX day flag (VIX daily-bar open percentile; session-cached
    # classifier behind the lowvix ladder-shrink shadow — see
    # engine/lo_vix_day.py). null = unknown = NOT lo. ---
    lo_vix_day: bool | None = None
    try:
        from fef_sidecar.engine.lo_vix_day import is_lo_vix_day
        lo_vix_day = await is_lo_vix_day(state, at_ms=at_ms)
    except Exception:
        logger.exception("regime_live: lo_vix_day flag failed — null")

    # --- combine into the validated multiple label ---
    idxs: list[int] = []
    parts: list[str] = []
    if vix_pct is not None:
        idxs.append(_tercile_idx(vix_pct))
        parts.append(f"VIX {vix_now:.1f} ({_tercile_label(vix_pct)} tercile, 20 sess)")
    elif vix_now is not None:
        parts.append(f"VIX {vix_now:.1f} (tercile n/a: {n_vix_sessions} prior sessions)")
    else:
        parts.append("VIX n/a")
    if rvol_pct is not None:
        idxs.append(_tercile_idx(rvol_pct))
        parts.append(f"30m rvol {_ordinal(rvol_pct)} %ile (TOD-matched)")
    else:
        parts.append("30m rvol %ile n/a")

    extra = {
        "vix": round(vix_now, 2) if vix_now is not None else None,
        "vix_percentile": round(vix_pct, 1) if vix_pct is not None else None,
        "lo_vix_day": lo_vix_day,
        "rvol_30m": round(rvol_cur, 6) if rvol_cur is not None else None,
        "rvol_percentile": round(rvol_pct, 1) if rvol_pct is not None else None,
        "post_cap": fired,
        "m15_current": round(cur_m15, 2) if cur_m15 is not None else None,
        "m15_median": round(med_m15, 2) if med_m15 is not None else None,
        "m15_samples": n15,
        "post_cap_threshold": round(threshold, 2) if threshold is not None else None,
    }

    if not idxs and not fired:
        return _na_tile(
            "expected-move inputs unavailable — no sizing guidance", at_ms, **extra,
        )

    if fired:
        label = "post-cap: expect ~2x move"
        effect = (
            f"POST-CAPITULATION — |15m move| {cur_m15:.1f}pts >= "
            f"{threshold:.1f}pts (1.5x median / 0.25% spot): expect ~2x the "
            "normal next-15m move. Magnitude flag only — direction is NOT predictable."
        )
    else:
        score = sum(idxs) / len(idxs)
        label = _MOVE_LABELS[0] if score <= 0.5 else (
            _MOVE_LABELS[1] if score < 1.5 else _MOVE_LABELS[2]
        )
        effect = (
            f"{label} (validated ~2.7x magnitude ranking). "
            "Magnitude only — no directional edge; use for attention + exit sizing."
        )
    return _tile(" · ".join(parts), label, effect, at_ms, **extra)


# ---------------------------------------------------------------------------
# Tile c — FLOW BIAS (drift bias + bias-veto armed state)
# ---------------------------------------------------------------------------


def _flow_bias_tile(state: Any, at_ms: int) -> dict[str, Any]:
    bias: str | None = None
    ts: int | None = None
    drift_m: float | None = None
    hist = getattr(getattr(state, "engine", None), "_history", None)
    if hist:
        try:
            last = hist[-1]
            bias = getattr(last, "drift_bias", None)
            ts = getattr(last, "timestamp_ms", None)
            drift_m = getattr(last, "drift_cum_net_m", None)
        except Exception:
            pass
    if bias is None:
        snap = getattr(state, "last_snapshot", None)
        drift = getattr(snap, "drift", None)
        bias = getattr(drift, "bias", None)
        drift_m = getattr(drift, "cumulative_net_m", None)
        ts = getattr(snap, "fetched_at_ms", None)

    armed = True
    try:
        armed = bool(load_config().bias_veto_enabled)
    except Exception:
        logger.exception("regime_live: bias veto config read failed — assuming armed")

    extra = {
        "drift_bias": bias,
        "drift_cum_net_m": round(drift_m, 1) if drift_m is not None else None,
        "bias_veto_armed": armed,
    }
    if bias is None:
        return _na_tile("no engine inputs yet — flow bias unknown", ts, **extra)

    drift_str = f" (drift {drift_m:+.0f}M)" if drift_m is not None else ""
    reading = f"drift bias {bias}{drift_str}"
    if bias in ("BULLISH", "BEARISH"):
        against = "PUTS" if bias == "BULLISH" else "CALLS"
        if armed:
            effect = (
                f"bias veto ARMED — scanner fires against {bias} flow "
                f"(i.e. {against}) are skipped"
            )
        else:
            effect = (
                f"bias veto DISARMED — fires against {bias} flow are tagged but traded"
            )
    else:
        effect = (
            "no dominant flow — bias veto does not bind"
            + ("" if armed else " (veto disarmed)")
        )
    return _tile(reading, bias, effect, ts, **extra)


# ---------------------------------------------------------------------------
# Tile d — EVENTS (deterministic calendar; FOMC hardcoded, CPI/NFP approx)
# ---------------------------------------------------------------------------


_EVENT_EFFECTS = {
    "CPI": "CPI 8:30 AM release (approx date) — elevated open volatility",
    "NFP": "NFP 8:30 AM release (approx date) — elevated open volatility",
    "OPEX": "monthly OPEX — expiry pin/unpin flows into the close",
    "QUARTER_END": "quarter-end — rebalancing flows, possible EOD distortion",
    "EOM": "month-end — rebalancing flows, possible EOD distortion",
}


def _events_tile(at_ms: int) -> dict[str, Any]:
    today = session_date_et(at_ms)
    mod = _minute_of_day_et(at_ms)
    todays = _cal.events_on(today)
    upcoming = _cal.upcoming_events(today, within_sessions=3)

    effects: list[str] = []
    for e in todays:
        if e["kind"] == "FOMC":
            if mod < FOMC_STATEMENT_MINUTE:
                effects.append(
                    "FOMC 2:00 PM ET — engine's worst class (-$61/ct "
                    "pre-announcement): stand down until the statement"
                )
            else:
                effects.append("post-FOMC — expect ~2.2x normal vol into the close")
        else:
            effects.append(_EVENT_EFFECTS.get(e["kind"], e["label"]))

    next_line = None
    if upcoming:
        n = upcoming[0]
        plural = "session" if n["sessions_away"] == 1 else "sessions"
        next_line = f"next: {n['kind']} in {n['sessions_away']} {plural} ({n['date']})"

    if todays:
        reading = " + ".join(e["label"] for e in todays)
        label = todays[0]["kind"]
        effect = "; ".join(effects)
        if next_line:
            effect = f"{effect}. {next_line}"
    else:
        reading = "no scheduled events today"
        label = "clear"
        effect = next_line or "no event within the next 3 sessions"

    return _tile(
        reading, label, effect, at_ms,
        today=[{k: e[k] for k in ("kind", "label", "approx")} for e in todays],
        upcoming=upcoming,
        is_trading_day=_cal.is_trading_day(today),
    )


# ---------------------------------------------------------------------------
# Tile e — DEALER GAMMA (latest exposure snapshot; degrade to unknown)
# ---------------------------------------------------------------------------


_GAMMA_EFFECT = {
    "LONG_GAMMA": (
        "dealers long gamma — hedging dampens moves (mean-revert tape; "
        "breakouts less likely to run)"
    ),
    "SHORT_GAMMA": (
        "dealers short gamma — hedging amplifies moves (momentum tape; "
        "moves can run)"
    ),
    "TRANSITION": "spot near the gamma flip — regime can change quickly",
}

_GAMMA_LABEL = {"LONG_GAMMA": "long", "SHORT_GAMMA": "short", "TRANSITION": "transition"}


def _dealer_gamma_tile(state: Any, at_ms: int) -> dict[str, Any]:
    snap = getattr(state, "last_snapshot", None)
    gex = getattr(snap, "gex", None)
    rows = getattr(gex, "rows", None)
    updated = getattr(snap, "fetched_at_ms", None)
    if not rows:
        return _tile(
            "no exposure snapshot", "unknown",
            "dealer gamma unknown — no positioning read; treat tape as unclassified",
            updated, regime="UNKNOWN", net_gex_m=None, gamma_flip=None,
        )
    try:
        from fef_sidecar.engine.signals.gamma_regime import classify

        spot = (
            getattr(gex, "spx_price", 0.0)
            or getattr(snap, "spx_price", 0.0)
            or getattr(state, "last_spx_price", 0.0)
        )
        res = classify([(float(r.strike), float(r.net_m)) for r in rows], float(spot))
        label = _GAMMA_LABEL.get(res.regime, "unknown")
        flip_str = (
            f", flip {res.gamma_flip:.0f} ({res.spot_vs_flip:+.0f}pts)"
            if res.gamma_flip is not None and res.spot_vs_flip is not None else ""
        )
        reading = f"net GEX {res.net_gex_m:+.0f}M{flip_str}"
        effect = _GAMMA_EFFECT.get(
            res.regime, "dealer gamma unknown — treat tape as unclassified",
        )
        return _tile(
            reading, label, effect, updated,
            regime=res.regime,
            net_gex_m=round(res.net_gex_m, 1),
            gamma_flip=round(res.gamma_flip, 1) if res.gamma_flip is not None else None,
            spot_vs_flip=round(res.spot_vs_flip, 1) if res.spot_vs_flip is not None else None,
        )
    except Exception:
        logger.exception("regime_live: dealer gamma classify failed — unknown")
        return _tile(
            "exposure snapshot unreadable", "unknown",
            "dealer gamma unknown — no positioning read; treat tape as unclassified",
            updated, regime="UNKNOWN", net_gex_m=None, gamma_flip=None,
        )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


async def build_regime_live(state: Any, at_ms: int | None = None) -> dict[str, Any]:
    """Assemble the full validated tile set. Never raises — each tile is
    individually fail-open and an unexpected error degrades that tile to n/a."""
    at = int(at_ms if at_ms is not None else _time.time() * 1000)

    async def _safe_async(fn, name: str) -> dict[str, Any]:
        try:
            return await fn(state, at)
        except Exception as e:
            logger.exception("regime_live: %s tile failed", name)
            return _na_tile(f"tile error: {e}", at)

    def _safe(fn, name: str, *args: Any) -> dict[str, Any]:
        try:
            return fn(*args)
        except Exception as e:
            logger.exception("regime_live: %s tile failed", name)
            return _na_tile(f"tile error: {e}", at)

    tiles = {
        "range_regime": await _safe_async(_range_regime_tile, "range_regime"),
        "expected_move": await _safe_async(_expected_move_tile, "expected_move"),
        "flow_bias": _safe(_flow_bias_tile, "flow_bias", state, at),
        "events": _safe(_events_tile, "events", at),
        "dealer_gamma": _safe(_dealer_gamma_tile, "dealer_gamma", state, at),
    }
    return {
        "generated_at_ms": at,
        "session_date": session_date_et(at),
        "active_symbol": getattr(state, "active_symbol", _cg.BAR_SYMBOL),
        "bar_symbol": _cg.BAR_SYMBOL,
        "tiles": tiles,
    }
