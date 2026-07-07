"""Lo-VIX day classifier — session-level flag for the lo-VIX ladder shrink.

Registered backtest (2026-07-07, scratchpad lowvix_score.py over the
strict-replay fire set — N=11,963 fires / 1,082 sessions 2022-2026):
shrinking the spx_points ladder to m=0.6 on lo-VIX days was ADOPTED on both
fill conventions. Per the operator's standing deployment rule it ships
SHADOW-FIRST: this module only CLASSIFIES the day; consumers stamp the flag
(scanner extras.lo_vix_day, regime expected_move tile) and drive the
lowvix_shrink_shadow in spx_target_watcher. Nothing here gates, trades, or
changes live behavior.

Definition — the EXACT day labels the backtest arm keyed on (mirrors
regime_exit_score.vix_pct_of):

    vix_pct = midrank percentile of today's VIX daily-bar OPEN vs the
              prior 20 sessions' OPENs;   lo := vix_pct < 33.333.
    Today's daily bar missing: prior session CLOSE vs the prior 20
    sessions' CLOSES (the registered same-series fallback basis). The
    live VIX 1-day rows are stamped at the 16:00 ET close, so intraday
    evaluation always lands on this basis; the two agree up to the
    overnight gap.
    Fewer than 10 prior values -> None (unknown).
    Ranked prior close older than FEF_VIX_DAILY_MAX_STALE_DAYS calendar
    days (default 4) -> None with reason 'stale_vix' — a lapsed backfill
    must fail OPEN, not classify today on a week-old close (review
    MAJOR-3: the series ended 2026-07-01 while 07-07 traded). Rare
    double-holiday weekends can false-stale one session; that too just
    fails open to normal behavior.

DOCUMENTED BASIS DEVIATION (adversarial review 2026-07-07; ledger entry
'lowvix_shadow_prior_close_basis', category='data'): the backtest labeled
days on the OPEN basis; live intraday classification runs on the
PRIOR-CLOSE basis because the 1-day rows land at the close. 8.9% of
historical sessions flip labels between the two bases. The registered
ARM-B (m=0.6 shrink on lo days) was RE-VALIDATED under prior-close labels
on the same bars and holds: OOS +$4.69/ct t=2.33 (pess), +$4.63/ct t=2.30
(mid), both OOS years positive, TRAIN >= 0.

Source: `historical_bar` rows, symbol='VIX', bar_size='1 day' — kept
current by engine/vix_daily_ingest.py (boot + one post-close pass per
session; the manual scripts/maintenance/backfill_vix.py remains for deep
history).

SERIES CONSISTENCY (do not mix sources): the live VIX series may be the QD
proxy (iv_rank vix_proxy), which reads ~3pts below real CBOE VIX. Ranking a
value against its OWN history cancels a constant bias, so the
classification stays valid AS LONG AS the current value and the pool come
from the SAME series — this module therefore reads BOTH from the same
historical_bar VIX 1-day rows (and the ingest loop only ever writes them
from the same IBKR CBOE VIX index the backfill used). Never rank
state.last_vix (live poller) against bar history, and never backfill the
1-day rows from a different vendor mid-stream without re-validating.

FAIL-OPEN: None (unknown) must be treated as NOT lo by every consumer —
missing data can never shrink a ladder; it only means normal behavior.
Computed once per session and cached on state (chop_gate-style session
cache); the determination (True/False/unknown) is audit-logged once per
session (category='lo_vix_day') with the percentile, basis, the ranked
value's own date, and the source rows' date range.
"""

from __future__ import annotations

import logging
import os
import time as _time
from dataclasses import dataclass
from datetime import date as _date
from typing import Any, Iterable

from fef_sidecar.engine.chop_gate import percentile_of, session_date_et

logger = logging.getLogger(__name__)

VIX_BAR_SYMBOL = "VIX"
VIX_BAR_SIZE = "1 day"
POOL_N = 20                      # prior-session pool depth (backtest spec)
MIN_POOL = 10                    # below this the day is unknown (None)
LO_THRESHOLD_PCT = 100.0 / 3.0   # lo-tercile boundary (33.333)
# Calendar days queried to find 20 prior trading sessions (chop_gate's
# convention; ~30 suffices for daily bars, 45 tolerates backfill gaps).
LOOKBACK_CALENDAR_DAYS = 45
# Prior-close staleness cutoff: 4 calendar days covers Fri-close -> Tue
# (weekend + one holiday). Beyond it the series has lapsed — fail open.
MAX_STALE_CALENDAR_DAYS = 4

# DB-failure log throttle (the watcher polls at 0.1s — an outage must not
# emit a traceback per tick; the failure itself is retried every call).
_ERR_LOG_INTERVAL_MS = 60_000
_last_err_log_ms = 0.0


def _max_stale_days() -> int:
    try:
        return int(os.environ.get(
            "FEF_VIX_DAILY_MAX_STALE_DAYS", str(MAX_STALE_CALENDAR_DAYS)
        ))
    except ValueError:
        return MAX_STALE_CALENDAR_DAYS


@dataclass(frozen=True)
class LoVixAssessment:
    """One session's classification. lo None => unknown => consumers treat
    as NOT lo (fail-open to normal behavior)."""

    session_date: str
    lo: bool | None
    vix_pct: float | None = None
    value: float | None = None    # the ranked VIX value (open or prior close)
    value_date: str | None = None  # the ranked value's OWN session date
    basis: str | None = None      # 'open' | 'prior_close'
    n_pool: int = 0
    pool_from: str | None = None  # ET dates of the first/last pool rows
    pool_to: str | None = None
    data_gap: str | None = None   # why lo is None


def _bar_toc(bar: Any) -> tuple[int, float | None, float | None]:
    """Normalize one daily bar to (timestamp_ms, open, close). Accepts
    HistoricalBar rows, dicts, or (ts, open, close) sequences —
    fixture-friendly like chop_gate._bar_thl."""
    if isinstance(bar, dict):
        return int(bar["timestamp_ms"]), bar.get("open"), bar.get("close")
    if isinstance(bar, (tuple, list)):
        return int(bar[0]), bar[1], bar[2]
    return int(bar.timestamp_ms), getattr(bar, "open", None), getattr(bar, "close", None)


def _unknown(session_date: str, gap: str, *, basis: str | None = None,
             value_date: str | None = None, n_pool: int = 0) -> LoVixAssessment:
    return LoVixAssessment(
        session_date=session_date, lo=None, basis=basis,
        value_date=value_date, n_pool=n_pool, data_gap=gap,
    )


def _calendar_days_between(from_date: str, to_date: str) -> int:
    return (_date.fromisoformat(to_date) - _date.fromisoformat(from_date)).days


def vix_open_percentile(
    daily_bars: Iterable[Any],
    session_date: str,
    *,
    pool_n: int = POOL_N,
    min_pool: int = MIN_POOL,
    max_stale_days: int = MAX_STALE_CALENDAR_DAYS,
) -> LoVixAssessment:
    """Classify `session_date` from VIX daily bars (any order; one row per
    ET date, last write wins). Pure mirror of the backtest's vix_pct_of:

      * today's bar present with open > 0 -> rank that OPEN against the
        prior `pool_n` sessions' positive opens ('open' basis);
      * else -> rank the PRIOR session's CLOSE against the `pool_n`
        sessions' positive closes before it ('prior_close' basis);
      * fewer than `min_pool` pool values -> unknown (None).

    Bars dated after `session_date` are ignored (no lookahead). Robustness
    additions over the backtest (which never hit either case): a prior
    close that is missing/<=0 is unknown rather than ranked, and a prior
    close more than `max_stale_days` calendar days old is unknown with
    reason 'stale_vix' (a lapsed backfill must fail open, not classify
    today on last week's close)."""
    by_date: dict[str, tuple[float | None, float | None]] = {}
    for b in daily_bars:
        ts, op, cl = _bar_toc(b)
        d = session_date_et(ts)
        if d <= session_date:
            by_date[d] = (op, cl)
    if not by_date:
        return _unknown(session_date, "no VIX 1-day bars in lookback window")
    dates = sorted(by_date)
    prior_dates = [d for d in dates if d < session_date]

    today = by_date.get(session_date)
    if today is not None and today[0] is not None and today[0] > 0:
        basis = "open"
        val = float(today[0])
        value_date = session_date
        pool_dates = prior_dates[-pool_n:]
        pool = [(d, by_date[d][0]) for d in pool_dates
                if by_date[d][0] is not None and by_date[d][0] > 0]
    else:
        basis = "prior_close"
        if not prior_dates:
            return _unknown(session_date, "no prior VIX daily bars", basis=basis)
        j = len(prior_dates) - 1
        value_date = prior_dates[j]
        stale_days = _calendar_days_between(value_date, session_date)
        if stale_days > max_stale_days:
            return _unknown(
                session_date,
                f"stale_vix: last VIX daily bar is {value_date} "
                f"({stale_days} calendar days before {session_date}; "
                f"max {max_stale_days}) — backfill lapsed",
                basis=basis, value_date=value_date,
            )
        cl = by_date[value_date][1]
        if cl is None or cl <= 0:
            return _unknown(
                session_date,
                f"prior session {value_date} has no usable close",
                basis=basis, value_date=value_date,
            )
        val = float(cl)
        pool_dates = prior_dates[max(0, j - pool_n): j]
        pool = [(d, by_date[d][1]) for d in pool_dates
                if by_date[d][1] is not None and by_date[d][1] > 0]

    if len(pool) < min_pool:
        return _unknown(
            session_date,
            f"only {len(pool)} prior VIX {basis} values (need >={min_pool})",
            basis=basis, value_date=value_date, n_pool=len(pool),
        )
    pct = percentile_of([v for _, v in pool], val)
    return LoVixAssessment(
        session_date=session_date,
        lo=pct < LO_THRESHOLD_PCT,
        vix_pct=pct,
        value=val,
        value_date=value_date,
        basis=basis,
        n_pool=len(pool),
        pool_from=pool[0][0],
        pool_to=pool[-1][0],
    )


async def _write_session_audit(a: LoVixAssessment) -> None:
    """One category='lo_vix_day' row per session — the determination with
    its percentile, basis, the ranked value's own date, and the pool's
    date range. Never raises."""
    try:
        from fef_sidecar.db.repository import SignalIterRepository
        if a.lo is None:
            summary = (
                f"LO-VIX DAY UNKNOWN for {a.session_date}: {a.data_gap} — "
                f"treated as NOT lo (fail-open, normal ladder)"
            )
        else:
            summary = (
                f"LO-VIX DAY {'YES' if a.lo else 'NO'} for {a.session_date}: "
                f"VIX {a.basis} {a.value:.2f} ({a.value_date}) at pctile "
                f"{a.vix_pct:.1f} of prior {a.n_pool} sessions "
                f"({a.pool_from}..{a.pool_to}); lo < {LO_THRESHOLD_PCT:.1f}"
            )
        await SignalIterRepository.write_audit_log(
            category="lo_vix_day", actor="system", summary=summary,
            details={
                "session_date": a.session_date,
                "lo": a.lo,
                "vix_pct": round(a.vix_pct, 2) if a.vix_pct is not None else None,
                "value": a.value,
                "value_date": a.value_date,
                "basis": a.basis,
                "n_pool": a.n_pool,
                "pool_from": a.pool_from,
                "pool_to": a.pool_to,
                "threshold_pct": round(LO_THRESHOLD_PCT, 3),
                "data_gap": a.data_gap,
            },
        )
    except Exception:
        logger.exception("lo_vix_day: session audit write failed")


async def assess_lo_vix(state: Any, *, at_ms: int | None = None) -> LoVixAssessment:
    """Session-cached classification for 'now' (or at_ms). Never raises.

    One DB read + one audit row per session; every later call that session
    returns the cached result. A FAILED read (exception) returns unknown
    WITHOUT caching or auditing, so a transient DB error retries on the
    next call — only definitive computations (True/False/insufficient
    data/stale) cache and audit. Failure tracebacks are throttled to one
    per minute (the watcher retries at 0.1s)."""
    global _last_err_log_ms
    at = int(at_ms if at_ms is not None else _time.time() * 1000)
    sess = session_date_et(at)
    cache = getattr(state, "lo_vix_day_cache", None)
    if cache is None:
        cache = {}
        try:
            state.lo_vix_day_cache = cache
        except Exception:
            cache = None
    if cache is not None:
        hit = cache.get(sess)
        if isinstance(hit, LoVixAssessment):
            return hit

    try:
        from fef_sidecar.db.repository import SignalIterRepository
        bars = await SignalIterRepository.query_bars(
            VIX_BAR_SYMBOL, VIX_BAR_SIZE,
            from_ms=at - LOOKBACK_CALENDAR_DAYS * 86_400_000,
            to_ms=at,
            limit=200,
        )
        a = vix_open_percentile(bars, sess, max_stale_days=_max_stale_days())
    except Exception as e:
        now_ms = _time.time() * 1000
        if now_ms - _last_err_log_ms >= _ERR_LOG_INTERVAL_MS:
            _last_err_log_ms = now_ms
            logger.exception(
                "lo_vix_day: assessment failed — unknown (not cached; "
                "further failures logged at most once/min)"
            )
        return _unknown(sess, f"assessment error: {e}")

    if cache is not None:
        # Evict other sessions' entries (bounded memory), then cache + audit
        # exactly once. A cache-less state (attr-frozen fake) skips the audit
        # too — without the cache 'once per session' cannot be guaranteed.
        for k in [k for k in cache if k != sess]:
            cache.pop(k, None)
        cache[sess] = a
        await _write_session_audit(a)
    logger.info(
        "lo_vix_day: %s -> %s (pct=%s basis=%s value_date=%s pool=%d %s..%s gap=%s)",
        sess, a.lo, f"{a.vix_pct:.1f}" if a.vix_pct is not None else "n/a",
        a.basis, a.value_date, a.n_pool, a.pool_from, a.pool_to, a.data_gap,
    )
    return a


async def is_lo_vix_day(state: Any, *, at_ms: int | None = None) -> bool | None:
    """True = lo-VIX session (VIX open pctile < 33.3 vs the prior 20 opens),
    False = not lo, None = unknown (insufficient/stale data) — consumers
    MUST treat None as NOT lo (fail-open to normal behavior). Session-
    cached; see the module docstring for the exact definition, the
    prior-close basis deviation, and the same-series consistency
    requirement."""
    return (await assess_lo_vix(state, at_ms=at_ms)).lo
