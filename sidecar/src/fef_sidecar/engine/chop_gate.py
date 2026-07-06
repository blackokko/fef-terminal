"""CHOP skip-gate — session/TOD-relative trailing-range regime filter.

Validated 2026-07-02 by the 22,683-fire sweep over scanner (flow-surge
ALIGNED) history: fires launched while the trailing 30-min SPX range sits
in the COMPRESSED tercile of its own time-of-day distribution lose
-$25 to -$48/ct in every configuration tested, while expanded-regime
fires are ~breakeven-to-positive. This module implements the
harm-reduction filter:

    SKIP a scanner fire when the trailing 30-min SPX range percentile —
    ranked against the SAME time-of-day's (+/-30 min) r30 values over the
    prior 20 sessions — is below the threshold (default 33.3).

Era-safe by construction: each fire is judged against its own recent
history at the same clock time, so vol-regime drift and the intraday
range smile cancel out instead of requiring re-tuned absolute levels.

FAIL-OPEN by design: every missing-data condition yields a percentile of
None and the gate PASSES — the filter can only skip a trade when it has a
full-quality answer; it never blocks on a data gap (it logs the gap and
stamps it into the signal row instead).

Data source: `historical_bar` rows (symbol='SPX', bar_size='1 min').
The live capture was 2-min-cadence on 2026-07-01 → early 07-02 (bars
labeled '1 min' arrived every 120s but each still covered its full time
span), and 1-min again from the 07-02 deploy. All range math here is
cadence-agnostic — max(high)-min(low) over whatever bars fall inside the
window is correct at either cadence; only the coverage checks count bars.

Pure functions (r30 / build_tod_distribution / tod_percentile /
r30_tod_percentile) take plain bar sequences and are fully unit-testable.
The async `assess_chop` wraps them with the DB read + a per-(symbol,
session) distribution cache (built once per session, not per fire).
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

MS_PER_MIN = 60_000

# Same fixed-offset ET convention as the rest of the app (_today_et in
# loop.py / multi_symbol_scanner.py). The +/-30 min TOD window absorbs the
# residual DST edge cases; consistency with session_date grouping matters
# more here than absolute wall-clock precision.
_ET = timezone(timedelta(hours=-4))

# ---- Validated spec constants (2026-07-02 sweep) ----
R30_WINDOW_MIN = 30          # trailing range window (minutes)
TOD_WINDOW_MIN = 30          # +/- minutes for "same time-of-day" matching
N_PRIOR_SESSIONS = 20        # distribution depth
DEFAULT_THRESHOLD_PCT = 33.3  # compressed-tercile boundary
BAR_SYMBOL = "SPX"           # the regime ruler — always SPX per the sweep
# ---- Robustness floors — below any of these the gate reports None (open)
MIN_BARS = 8                 # min bars inside the r30 window (15 at 2-min cadence)
MIN_SPAN_FRAC = 0.5          # min (last-first)/window coverage inside the r30 window
MIN_TOD_SAMPLES = 25         # min prior-session samples inside the TOD window
MIN_TOD_SESSIONS = 8         # min distinct prior sessions contributing samples
LOOKBACK_CALENDAR_DAYS = 45  # calendar days queried to find 20 trading sessions

# Structured code stamped on the audit row when the gate skips a fire.
# Deliberately NOT added to auto_trader.BlockReason — the gate lives outside
# the auto-trader (the trade is never offered), and auto_trader internals are
# frozen. block_reason_code is a free-form string column.
CHOP_GATE_BLOCK_CODE = "CHOP_GATE_SKIP"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _bar_thl(bar: Any) -> tuple[int, float, float]:
    """Normalize one bar to (timestamp_ms, high, low). Accepts HistoricalBar
    rows / any object with those attributes, dicts, or (ts, high, low)
    sequences — keeps the pure functions fixture-friendly."""
    if isinstance(bar, dict):
        return int(bar["timestamp_ms"]), float(bar["high"]), float(bar["low"])
    if isinstance(bar, (tuple, list)):
        return int(bar[0]), float(bar[1]), float(bar[2])
    return int(bar.timestamp_ms), float(bar.high), float(bar.low)


def _normalize_1min(bars: Iterable[Any]) -> list[tuple[int, float, float]]:
    """Sort + merge bars onto minute boundaries.

    Duplicate/sub-minute rows (e.g. the 6/30 backfill overlap wrote rows
    seconds apart) are merged into one point per minute keeping the widest
    high/low, so ranges and coverage counts stay honest. 2-min-cadence rows
    simply occupy every other minute — no interpolation needed because the
    range math only cares about extremes, not sample count."""
    by_minute: dict[int, tuple[float, float]] = {}
    for b in bars:
        ts, hi, lo = _bar_thl(b)
        m = (ts // MS_PER_MIN) * MS_PER_MIN
        prev = by_minute.get(m)
        if prev is None:
            by_minute[m] = (hi, lo)
        else:
            by_minute[m] = (max(prev[0], hi), min(prev[1], lo))
    return [(m, hl[0], hl[1]) for m, hl in sorted(by_minute.items())]


def _minute_of_day_et(at_ms: int) -> int:
    dt = datetime.fromtimestamp(at_ms / 1000, _ET)
    return dt.hour * 60 + dt.minute


def session_date_et(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, _ET).strftime("%Y-%m-%d")


def tod_bucket(at_ms: int) -> str:
    """30-min ET clock bucket label, e.g. '13:30-14:00'. Tag-only — persisted
    on every scanner fire so future TOD filters have live data."""
    mod = _minute_of_day_et(at_ms)
    start = (mod // 30) * 30
    end = start + 30
    return f"{start // 60:02d}:{start % 60:02d}-{(end // 60) % 24:02d}:{end % 60:02d}"


def r30(
    bars_1min: Iterable[Any],
    at_ms: int,
    *,
    window_min: int = R30_WINDOW_MIN,
    min_bars: int = MIN_BARS,
    min_span_frac: float = MIN_SPAN_FRAC,
) -> float | None:
    """High-low range of the trailing `window_min` minutes ending at `at_ms`.

    A bar belongs to the window when its start timestamp lies in
    [at_ms - window_min*60s, at_ms) — i.e. the last ~30 one-minute bars
    that have CLOSED by `at_ms`. Returns None (missing data → gate passes
    open) when the window holds fewer than `min_bars` bars or the covered
    span is under `min_span_frac` of the window."""
    window_ms = window_min * MS_PER_MIN
    lo_ts = at_ms - window_ms
    pts = [p for p in _normalize_1min(bars_1min) if lo_ts <= p[0] < at_ms]
    if len(pts) < min_bars:
        return None
    span = pts[-1][0] - pts[0][0]
    if span < min_span_frac * window_ms:
        return None
    return max(p[1] for p in pts) - min(p[2] for p in pts)


def build_tod_distribution(
    bars_by_session: dict[str, Sequence[Any]],
    *,
    session_date: str,
    n_sessions: int = N_PRIOR_SESSIONS,
    window_min: int = R30_WINDOW_MIN,
    min_bars: int = MIN_BARS,
    min_span_frac: float = MIN_SPAN_FRAC,
) -> list[tuple[str, int, float]]:
    """r30 sampled at every bar close across the prior `n_sessions` sessions.

    bars_by_session: {'YYYY-MM-DD' (ET): [1-min bars]}. Sessions >= the
    current `session_date` are excluded — the distribution is strictly
    PRIOR history, so today's own compression can't dilute its ranking.

    Returns [(session_date, minute_of_day_et, r30_value), ...]. Sessions
    with unusable windows (too few bars — e.g. the first ~15 minutes of
    each session, or capture outages) simply contribute fewer samples;
    the caller's MIN_TOD_* floors decide whether that's still enough."""
    prior = sorted(d for d in bars_by_session if d < session_date)[-n_sessions:]
    samples: list[tuple[str, int, float]] = []
    for d in prior:
        pts = _normalize_1min(bars_by_session[d])
        window_ms = window_min * MS_PER_MIN
        start_idx = 0
        for i, (ts, _h, _l) in enumerate(pts):
            eval_ms = ts + MS_PER_MIN  # r30 as of this bar's close
            lo_ts = eval_ms - window_ms
            while pts[start_idx][0] < lo_ts:
                start_idx += 1
            win = pts[start_idx: i + 1]
            if len(win) < min_bars:
                continue
            if win[-1][0] - win[0][0] < min_span_frac * window_ms:
                continue
            val = max(p[1] for p in win) - min(p[2] for p in win)
            samples.append((d, _minute_of_day_et(eval_ms), val))
    return samples


def tod_window_samples(
    samples: Sequence[tuple[str, int, float]],
    at_ms: int,
    *,
    tod_window_min: int = TOD_WINDOW_MIN,
) -> list[tuple[str, float]]:
    """Distribution samples whose time-of-day is within +/-tod_window_min
    of at_ms's ET time-of-day. Returns [(session_date, r30_value), ...]."""
    mod = _minute_of_day_et(at_ms)
    return [(d, v) for (d, m, v) in samples if abs(m - mod) <= tod_window_min]


def percentile_of(values: Sequence[float], x: float) -> float:
    """Midrank percentile of x within values (0..100). Ties count half."""
    below = sum(1 for v in values if v < x)
    equal = sum(1 for v in values if v == x)
    return 100.0 * (below + 0.5 * equal) / len(values)


def tod_percentile(
    samples: Sequence[tuple[str, int, float]],
    at_ms: int,
    current_r30: float,
    *,
    tod_window_min: int = TOD_WINDOW_MIN,
    min_samples: int = MIN_TOD_SAMPLES,
    min_sessions: int = MIN_TOD_SESSIONS,
) -> float | None:
    """Percentile of current_r30 vs the TOD-matched prior-session samples.
    None (gate passes open) when the matched distribution is too thin."""
    sel = tod_window_samples(samples, at_ms, tod_window_min=tod_window_min)
    if len(sel) < min_samples or len({d for d, _ in sel}) < min_sessions:
        return None
    return percentile_of([v for _, v in sel], current_r30)


def r30_tod_percentile(
    symbol_bars_by_session: dict[str, Sequence[Any]],
    at_ms: int,
    *,
    n_sessions: int = N_PRIOR_SESSIONS,
    window_min: int = R30_WINDOW_MIN,
    tod_window_min: int = TOD_WINDOW_MIN,
) -> float | None:
    """Convenience composition matching the validated spec: percentile of
    the current trailing-30-min range vs the same time-of-day's r30 values
    over the prior `n_sessions` sessions. The current session's bars come
    from symbol_bars_by_session[<ET date of at_ms>]; every earlier key is
    distribution candidate history. None on any missing-data condition."""
    sess = session_date_et(at_ms)
    cur = r30(symbol_bars_by_session.get(sess, ()), at_ms, window_min=window_min)
    if cur is None:
        return None
    samples = build_tod_distribution(
        symbol_bars_by_session, session_date=sess,
        n_sessions=n_sessions, window_min=window_min,
    )
    return tod_percentile(samples, at_ms, cur, tod_window_min=tod_window_min)


# ---------------------------------------------------------------------------
# Async assessment — DB read + per-session distribution cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChopAssessment:
    """One gate evaluation. percentile None => gate MUST pass open."""

    bar_symbol: str
    session_date: str
    at_ms: int
    tod_bucket: str
    r30: float | None
    percentile: float | None
    n_samples: int = 0       # TOD-matched prior samples the percentile used
    n_sessions: int = 0      # distinct prior sessions among those samples
    data_gap: str | None = None  # why percentile is None (logged, persisted)

    def is_compressed(self, threshold_pct: float) -> bool:
        """True only when we have a full-quality percentile below threshold.
        Missing data can never skip a trade."""
        return self.percentile is not None and self.percentile < threshold_pct

    def to_extras(self) -> dict[str, Any]:
        return {
            "bar_symbol": self.bar_symbol,
            "r30": round(self.r30, 4) if self.r30 is not None else None,
            "r30_pct": round(self.percentile, 2) if self.percentile is not None else None,
            "tod_bucket": self.tod_bucket,
            "n_samples": self.n_samples,
            "n_sessions": self.n_sessions,
            "data_gap": self.data_gap,
        }


@dataclass
class _DistCacheEntry:
    samples: list[tuple[str, int, float]] = field(default_factory=list)
    built_at_ms: int = 0


def _open_assessment(bar_symbol: str, at_ms: int, gap: str) -> ChopAssessment:
    return ChopAssessment(
        bar_symbol=bar_symbol, session_date=session_date_et(at_ms), at_ms=at_ms,
        tod_bucket=tod_bucket(at_ms), r30=None, percentile=None, data_gap=gap,
    )


async def _load_distribution(
    state: Any, bar_symbol: str, session_date: str, at_ms: int,
) -> _DistCacheEntry:
    """Build (or fetch cached) the prior-20-session TOD r30 distribution.
    Cached per (bar_symbol, session_date) on state — one build per session,
    not per fire. Stale keys for other sessions are dropped."""
    cache = getattr(state, "chop_tod_dist_cache", None)
    if cache is None:
        cache = {}
        try:
            state.chop_tod_dist_cache = cache
        except Exception:
            pass
    key = (bar_symbol, session_date)
    entry = cache.get(key)
    if isinstance(entry, _DistCacheEntry):
        return entry

    from fef_sidecar.db.repository import SignalIterRepository
    bars = await SignalIterRepository.query_bars(
        bar_symbol, "1 min",
        from_ms=at_ms - LOOKBACK_CALENDAR_DAYS * 86_400_000,
        to_ms=at_ms,
        limit=40_000,
    )
    bars_by_session: dict[str, list[Any]] = {}
    for b in bars:
        bars_by_session.setdefault(session_date_et(b.timestamp_ms), []).append(b)
    samples = build_tod_distribution(bars_by_session, session_date=session_date)
    entry = _DistCacheEntry(samples=samples, built_at_ms=int(_time.time() * 1000))
    # Evict other sessions' entries for this symbol (bounded memory).
    for k in [k for k in cache if isinstance(k, tuple) and k and k[0] == bar_symbol and k != key]:
        cache.pop(k, None)
    cache[key] = entry
    logger.info(
        "chop_gate: built TOD r30 distribution for %s %s — %d samples / %d sessions",
        bar_symbol, session_date, len(samples), len({d for d, _, _ in samples}),
    )
    return entry


async def assess_chop(
    state: Any,
    *,
    bar_symbol: str = BAR_SYMBOL,
    at_ms: int | None = None,
) -> ChopAssessment:
    """Full gate evaluation for 'now' (or at_ms). Never raises — any failure
    or data gap returns percentile None so the caller passes open."""
    at = int(at_ms if at_ms is not None else _time.time() * 1000)
    sess = session_date_et(at)
    try:
        entry = await _load_distribution(state, bar_symbol, sess, at)

        from fef_sidecar.db.repository import SignalIterRepository
        cur_bars = await SignalIterRepository.query_bars(
            bar_symbol, "1 min",
            from_ms=at - (R30_WINDOW_MIN + 10) * MS_PER_MIN,
            to_ms=at,
            limit=200,
        )
        cur = r30(cur_bars, at)
        if cur is None:
            gap = (
                f"insufficient current-session {bar_symbol} 1-min bars for r30 "
                f"({len(cur_bars)} rows in trailing {R30_WINDOW_MIN + 10}m)"
            )
            logger.info("chop_gate: PASS-OPEN (data gap: %s)", gap)
            return _open_assessment(bar_symbol, at, gap)

        sel = tod_window_samples(entry.samples, at)
        n_sess = len({d for d, _ in sel})
        if len(sel) < MIN_TOD_SAMPLES or n_sess < MIN_TOD_SESSIONS:
            gap = (
                f"TOD distribution too thin ({len(sel)} samples / {n_sess} sessions; "
                f"need >={MIN_TOD_SAMPLES}/{MIN_TOD_SESSIONS})"
            )
            logger.info("chop_gate: PASS-OPEN (data gap: %s)", gap)
            return ChopAssessment(
                bar_symbol=bar_symbol, session_date=sess, at_ms=at,
                tod_bucket=tod_bucket(at), r30=cur, percentile=None,
                n_samples=len(sel), n_sessions=n_sess, data_gap=gap,
            )
        pct = percentile_of([v for _, v in sel], cur)
        return ChopAssessment(
            bar_symbol=bar_symbol, session_date=sess, at_ms=at,
            tod_bucket=tod_bucket(at), r30=cur, percentile=pct,
            n_samples=len(sel), n_sessions=n_sess, data_gap=None,
        )
    except Exception as e:  # pragma: no cover - defensive; individual paths tested
        logger.exception("chop_gate: assessment failed — PASS-OPEN")
        return _open_assessment(bar_symbol, at, f"assessment error: {e}")
