"""Multi-timeframe STRUCTURE LEVELS -- a DISPLAY-ONLY chart overlay.

For whatever symbol the chart shows, this marks the levels a discretionary trader draws on his
5m -> 1D charts -- swing highs/lows, multi-touch range zones, midpoints, fib retracements
0.5 / 0.618 / 0.75 / 0.87 of the last two swings, floor pivots, prior day / week levels and round
numbers -- and the MIDENTRY setups (enter at the midpoint of the move that breaks, reclaims or
rejects a level; stop at the move's origin, target its extreme).

THE DEFINITIONS ARE THE ONES THE `mtflevels` STUDY TESTED: fef-research-archive
``mtflevels_LOCK.md`` sections 2-3, ``mtflevels_AMENDMENT_1.md`` (MIDENTRY), ``_AMENDMENT_2.md``
(binding rulings R1-R11) and ``_AMENDMENT_XA.md`` Part 3 (the executor's conventions). The code is a
pure-Python port of executor A's reference implementation (``D:\\fef-data\\mtflevels\\A``:
``mtfl_core.zigzag``, ``mtfl_marker``, ``mtfl_events``, ``mtfl_mid``), which agreed with an
independent implementation on 99.7 % of events. ``tests/test_structure_levels.py`` checks this port
against that code on a QQQ fixture (``tests/fixtures/make_structure_levels_golden.py``).

THE STUDY FOUND NO MECHANICAL EDGE (QQQ 2022-26): price was rejected slightly LESS at these levels
than at matched random prices, breaks / reclaims / retests were nil, the fib ratios were not
special, and midpoint entries made -0.02 R a trade. This is decision support for the operator's
eyes only. It must never feed the scanner, a trader, a gate or any order path: nothing here
imports a trading module, and no trading module may import this one.

Data model
----------
``bars_by_res`` maps a resolution key to a list of bar dicts ``{"time", "open", "high", "low",
"close"}`` (``time`` = bar START, epoch SECONDS). Intraday keys: ``"1m"``, ``"5m"``, ``"30m"``
(any minute size that divides 30, aligned to 09:30 ET). Daily key: ``"1D"``, bars carrying
``"date": "YYYY-MM-DD"`` (the ET session date) or a ``time`` inside that date.

Only regular hours count: 09:30-16:00 ET (13:00 on NYSE early closes), anchored at 09:30 like a
TradingView RTH chart. For every session the finest resolution that covers it completely becomes
the PATH (the study's 1-minute series); the session in progress uses whichever resolution reaches
furthest. tau-bars (5m, 15m, 30m, 1H, 4H, 1D) are built from the path per session, so 1H =
09:30-10:30 ... 15:30-16:00 and 4H = 09:30-13:30 / 13:30-16:00 (``pattern_overlay.resample_bars``
cuts on epoch boundaries and is NOT used). Early closes truncate. A tau exists for a session only
when the session's path is fine enough to build it (5m bars build 5m...1D; 30m bars build 30m, 1H,
4H and 1D; daily bars build 1D only).

"Now" is the end of the last complete path bar (``asof``). Index conventions follow the study:
a level is known from path index ``kf`` (the first bar after the tau close that defined it) and is
ACTIVE while ``kf <= n < actend``, where ``n`` = the number of complete path bars. A tau-bar still
forming is never used for a close, an ATR or a pivot.

Live-vs-study differences (all documented in the tests)
-------------------------------------------------------
* The study's fib / leg-midpoint expiry is "the first 1-minute bar that trades beyond the leg",
  indexed at that bar's START, i.e. one bar of lookahead. Live, the bar that trades through is only
  known once it has printed, so at the very last bar boundary a level the study already calls
  expired is still shown for at most one path bar.
* History older than the finest data is coarser: the study's 1-minute conventions (side and arming
  on 1m closes, the R7 born-expired window, MIDENTRY's O bar, REJECT touches) are evaluated on the
  finest bars available for that session. With 1-minute input they are exact.
* Era-end truncations in the study (horizons capped at the last session, ``kf < n``) do not apply
  live: anything not yet decided is simply pending.
"""

from __future__ import annotations

import bisect
import math
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping

from fef_sidecar.et_time import ET

# =====================================================================================
# constants (lock section 2, AMENDMENT_1)
# =====================================================================================
TAUS: tuple[str, ...] = ("5m", "15m", "30m", "1H", "4H", "1D")
_TAU_MIN: tuple[int, ...] = (5, 15, 30, 60, 240, 0)
TI_5M, TI_15M, TI_30M, TI_1H, TI_4H, TI_1D = 0, 1, 2, 3, 4, 5
FAMILIES: tuple[str, ...] = ("S", "Z", "M", "F", "P", "D", "R")
_FAM_IDX = {f: i for i, f in enumerate(FAMILIES)}

K_ZZ = 3.0                 # zigzag reversal multiple (lock 2.3, primary k = 3)
ATR_N = 14                 # lock 2.2
MAXAGE_TAU = 300           # S/Z/M/F max age in tau-bars (lock 2.11)
MAXAGE_1D = 250            # ... 1D, in sessions
W_BARS = 12                # BROKEN window / RECLAIM window, tau-bars
ZONE_WIN = 200             # zone member window, tau-bars (lock 2.4)
ZONE_WIN_1D = 250
F_RATIOS = (0.618, 0.75, 0.87)
DEEP_RATIOS = (0.618, 0.75, 0.87)
QUAL_ATR = 1.5             # MIDENTRY qualification: leg >= 1.5 ATR_tau(t0) (A1.2)
PDR_SCALES = (TI_5M, TI_1H)  # P/D/R are evaluated at the 5m and 1H reaction scales (lock 2.9)
DISPLAY_PDR_SCALE = TI_1H  # the instance drawn on the chart (see compute())
RECENT_S = 30 * 60         # resolved setups stay listed for 30 minutes

_RTH_OPEN_MIN = 9 * 60 + 30
_BIG = 1 << 62
_EPS_C = 1e-6              # exact-cent thresholds (mtfl_core.c_ge & co.)
_EPS_TIE = 1e-9            # inclusive tie tolerance for ATR-multiple comparisons
_NAN = float("nan")

# race / event outcome codes (mtfl_events) + PENDING for a race the data has not finished yet
SUCC, FAIL, AMB, UNRES, GAP, PEND = 1, 0, -1, -2, -3, -5
ORIGINS = ("BREAK", "RECLAIM", "REJECT")

_P_KINDS = ("P", "R1", "S1", "R2", "S2")
_D_KINDS = ("PWH", "PWL", "PDH", "PDL", "PDC")
_TICK_025 = frozenset({"ES", "NQ", "RTY", "MES", "MNQ", "M2K"})
_NICE_STEPS = (1.0, 2.0, 2.5, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0)
_ROUND_STEP_OVERRIDE = {"QQQ": 5.0, "SPY": 5.0}   # the study used $5 on QQQ -- kept exact

# =====================================================================================
# NYSE calendar (mtfl_core lists 2016-2026 + the published 2027 schedule)
# =====================================================================================
_NYSE_HOLIDAYS = frozenset("""
2016-01-01 2016-01-18 2016-02-15 2016-03-25 2016-05-30 2016-07-04 2016-09-05 2016-11-24 2016-12-26
2017-01-02 2017-01-16 2017-02-20 2017-04-14 2017-05-29 2017-07-04 2017-09-04 2017-11-23 2017-12-25
2018-01-01 2018-01-15 2018-02-19 2018-03-30 2018-05-28 2018-07-04 2018-09-03 2018-11-22 2018-12-05 2018-12-25
2019-01-01 2019-01-21 2019-02-18 2019-04-19 2019-05-27 2019-07-04 2019-09-02 2019-11-28 2019-12-25
2020-01-01 2020-01-20 2020-02-17 2020-04-10 2020-05-25 2020-07-03 2020-09-07 2020-11-26 2020-12-25
2021-01-01 2021-01-18 2021-02-15 2021-04-02 2021-05-31 2021-07-05 2021-09-06 2021-11-25 2021-12-24
2022-01-17 2022-02-21 2022-04-15 2022-05-30 2022-06-20 2022-07-04 2022-09-05 2022-11-24 2022-12-26
2023-01-02 2023-01-16 2023-02-20 2023-04-07 2023-05-29 2023-06-19 2023-07-04 2023-09-04 2023-11-23 2023-12-25
2024-01-01 2024-01-15 2024-02-19 2024-03-29 2024-05-27 2024-06-19 2024-07-04 2024-09-02 2024-11-28 2024-12-25
2025-01-01 2025-01-09 2025-01-20 2025-02-17 2025-04-18 2025-05-26 2025-06-19 2025-07-04 2025-09-01 2025-11-27 2025-12-25
2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07 2026-11-26 2026-12-25
2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18 2027-07-05 2027-09-06 2027-11-25 2027-12-24
""".split())
_NYSE_EARLY_CLOSES = frozenset("""
2016-11-25 2017-07-03 2017-11-24 2018-07-03 2018-11-23 2018-12-24 2019-07-03 2019-11-29 2019-12-24
2020-11-27 2020-12-24 2021-11-26 2022-11-25 2023-07-03 2023-11-24 2024-07-03 2024-11-29 2024-12-24
2025-07-03 2025-11-28 2025-12-24 2026-11-27 2026-12-24 2027-11-26
""".split())
CALENDAR_FIRST = date(2016, 1, 1)
CALENDAR_LAST = date(2027, 12, 31)   # beyond it: every weekday is a 16:00 session (flagged in output)


def calendar_covers(d: date) -> bool:
    return CALENDAR_FIRST <= d <= CALENDAR_LAST


def is_session_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in _NYSE_HOLIDAYS


def close_minute(d: date) -> int:
    """Session close as minutes after midnight ET (960 = 16:00, 780 = 13:00 early close)."""
    return 780 if d.isoformat() in _NYSE_EARLY_CLOSES else 960


def _prev_session_day(d: date) -> date:
    x = d - timedelta(days=1)
    for _ in range(20):
        if is_session_day(x):
            return x
        x -= timedelta(days=1)
    return x


def _next_session_day(d: date) -> date:
    x = d + timedelta(days=1)
    for _ in range(20):
        if is_session_day(x):
            return x
        x += timedelta(days=1)
    return x


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _week_session_days(monday: date) -> list[date]:
    return [monday + timedelta(days=k) for k in range(5) if is_session_day(monday + timedelta(days=k))]


def _epoch(d: date, minute_of_day: int) -> int:
    return int(datetime(d.year, d.month, d.day, minute_of_day // 60, minute_of_day % 60,
                        tzinfo=ET).timestamp())


# =====================================================================================
# small helpers
# =====================================================================================
def _c_ge(x: float) -> int:
    """integer-cent threshold T with  price >= x  <=>  price_c >= T  (mtfl_core.c_ge)."""
    return math.ceil(x * 100.0 - _EPS_C)


def _c_gt(x: float) -> int:
    return math.floor(x * 100.0 + _EPS_C) + 1


def _c_le(x: float) -> int:
    return math.floor(x * 100.0 + _EPS_C)


def _c_lt(x: float) -> int:
    return math.ceil(x * 100.0 - _EPS_C) - 1


def round_step(symbol: str, price: float) -> float:
    """Round-number step: $5 for QQQ / SPY (the study's exact value), otherwise the nice value in
    {1, 2, 2.5, 5, 10, 25, 50, 100, 250, 500} nearest price / 120 (SPX / ES -> 50, NQ -> 250)."""
    s = (symbol or "").upper()
    if s in _ROUND_STEP_OVERRIDE:
        return _ROUND_STEP_OVERRIDE[s]
    if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
        return 5.0
    target = float(price) / 120.0
    return min(_NICE_STEPS, key=lambda v: (abs(v - target), v))


def tick_size(symbol: str) -> float:
    """Trade-through increment for the CONV-LEVEL fill: 1 cent, or 1 tick for the index futures."""
    return 0.25 if (symbol or "").upper() in _TICK_025 else 0.01


class _Sparse:
    """Sparse table over a list for first-passage and range queries (mtfl_core.Sparse, scalar)."""

    __slots__ = ("t", "n", "mx")

    def __init__(self, a: list, mx: bool) -> None:
        self.n = len(a)
        self.mx = mx
        t = [list(a)]
        k = 1
        op = max if mx else min
        while (1 << k) <= self.n:
            prev = t[-1]
            half = 1 << (k - 1)
            t.append(list(map(op, prev[:-half], prev[half:])))
            k += 1
        self.t = t

    def first_ge(self, s: int, thr: float) -> int:
        """smallest j >= s with a[j] >= thr; n if none (max table)."""
        n = self.n
        pos = s if s > 0 else 0
        if pos >= n:
            return n
        for k in range(len(self.t) - 1, -1, -1):
            tk = self.t[k]
            if pos < len(tk) and tk[pos] < thr:
                pos += 1 << k
        return pos if pos < n else n

    def first_le(self, s: int, thr: float) -> int:
        """smallest j >= s with a[j] <= thr; n if none (min table)."""
        n = self.n
        pos = s if s > 0 else 0
        if pos >= n:
            return n
        for k in range(len(self.t) - 1, -1, -1):
            tk = self.t[k]
            if pos < len(tk) and tk[pos] > thr:
                pos += 1 << k
        return pos if pos < n else n

    def ext(self, i: int, j: int):
        """max (or min) over [i, j] inclusive, i <= j."""
        k = (j - i + 1).bit_length() - 1
        tk = self.t[k]
        a, b = tk[i], tk[j - (1 << k) + 1]
        if self.mx:
            return a if a >= b else b
        return a if a <= b else b


# =====================================================================================
# input normalisation -> the PATH
# =====================================================================================
def parse_resolution(key: Any) -> int | None:
    """'1m' / '5 mins' / '30m' / '1h' -> minutes; '1D' / '1 day' -> 0 (daily); else None."""
    k = str(key).strip().lower().replace(" ", "")
    if k in ("1d", "d", "day", "1day", "daily"):
        return 0
    num = ""
    for ch in k:
        if ch.isdigit():
            num += ch
        else:
            break
    if not num:
        return None
    unit = k[len(num):]
    v = int(num)
    if v <= 0:
        return None
    if unit in ("m", "min", "mins", "minute", "minutes"):
        return v
    if unit in ("h", "hour", "hours"):
        return v * 60
    return None


def _ohlc_cents(b: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    try:
        o = float(b["open"])
        h = float(b["high"])
        lo = float(b["low"])
        c = float(b["close"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(o) and math.isfinite(h) and math.isfinite(lo) and math.isfinite(c)):
        return None
    if min(o, h, lo, c) <= 0:
        return None
    oc, hc, lc, cc = round(o * 100), round(h * 100), round(lo * 100), round(c * 100)
    hc = max(hc, oc, cc)
    lc = min(lc, oc, cc)
    return oc, hc, lc, cc


def _bar_time_s(b: Mapping[str, Any]) -> int | None:
    try:
        t = b["time"]
    except (KeyError, TypeError):
        return None
    try:
        t = int(t)
    except (TypeError, ValueError):
        return None
    if t > 10**11:          # milliseconds slipped in -- the contract is seconds
        t //= 1000
    return t


def _intraday_by_date(bars: Iterable[Mapping[str, Any]], res: int, now_s: int):
    out: dict[date, dict[int, tuple[int, int, int, int]]] = {}
    step = res * 60
    for b in bars or ():
        t = _bar_time_s(b)
        if t is None or t + step > now_s:
            continue                       # unparseable or still forming
        et = datetime.fromtimestamp(t, ET)
        if et.second:
            continue
        d = et.date()
        mod = et.hour * 60 + et.minute - _RTH_OPEN_MIN
        if mod < 0 or mod % res or mod + res > close_minute(d) - _RTH_OPEN_MIN:
            continue                       # outside RTH (or misaligned with the 09:30 grid)
        if not is_session_day(d):
            continue
        v = _ohlc_cents(b)
        if v is None:
            continue
        out.setdefault(d, {})[mod] = v
    return out


def _daily_date(b: Mapping[str, Any]) -> date | None:
    v = b.get("date") if hasattr(b, "get") else None
    if isinstance(v, datetime):
        return (v.astimezone(ET) if v.tzinfo is not None else v).date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and len(v) >= 10:
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    t = _bar_time_s(b)
    if t is None:
        return None
    return datetime.fromtimestamp(t, ET).date()


def _daily_by_date(bars: Iterable[Mapping[str, Any]], now_s: int):
    out: dict[date, tuple[int, int, int, int]] = {}
    for b in bars or ():
        d = _daily_date(b)
        if d is None or not is_session_day(d):
            continue
        if now_s < _epoch(d, close_minute(d)):
            continue                       # the session has not closed: its daily bar is partial
        v = _ohlc_cents(b)
        if v is not None:
            out[d] = v
    return out


def _session_items(bd: dict[int, tuple[int, int, int, int]], r: int, close: int,
                   in_progress: bool):
    """The session's bars at resolution r as [(minute, ohlc)], or None if too incomplete.

    IBKR omits a TRADES bar when nothing printed in it, so a few missing bars are filled flat at
    the last price (the opening gap at the first print's open): at most max(2, 2 %) per session.
    A finished session must reach its close; one in progress counts up to its last bar."""
    if not bd:
        return None
    present = sorted(bd)
    cov = close if not in_progress else present[-1] + r
    if cov > close:
        return None
    want = cov // r
    have = sum(1 for m in present if m < cov)
    if want - have > max(2, (close // r) // 50):
        return None
    first = bd[present[0]]
    items = []
    prev = None
    for m in range(0, cov, r):
        v = bd.get(m)
        if v is None:
            p = first[0] if prev is None else prev[3]
            v = (p, p, p, p)
        items.append((m, v))
        prev = v
    return items, cov


class _Sess:
    __slots__ = ("date", "start", "end", "close", "res", "complete", "cov_end", "exp_end", "open_s")

    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


# =====================================================================================
# tau bars
# =====================================================================================
def _fits(res: int, ti: int) -> bool:
    """Can a session whose path has resolution `res` (minutes) build tau ti?"""
    if ti == TI_1D:
        return True
    m = _TAU_MIN[ti]
    return res <= m and m % res == 0


def _bucket(mod: int, ti: int) -> int:
    if ti == TI_1D:
        return 0
    if ti == TI_4H:
        return 0 if mod < 240 else 1
    return mod // _TAU_MIN[ti]


def _bucket_end(b: int, ti: int, close: int) -> int:
    if ti == TI_1D:
        return close
    if ti == TI_4H:
        return min(240, close) if b == 0 else close
    return min((b + 1) * _TAU_MIN[ti], close)


class _Tau:
    """Completed tau-bars of one timeframe, with ATR (lock 2.2) and path-index maps."""

    def __init__(self, ti: int) -> None:
        self.ti = ti
        self.start: list[int] = []
        self.end: list[int] = []
        self.o: list[float] = []
        self.h: list[float] = []
        self.l: list[float] = []
        self.c: list[float] = []
        self.cc: list[int] = []
        self.sess: list[int] = []
        self.atr: list[float] = []
        self.nb = 0
        self.forming_start: int | None = None
        self.last_done: list[int] = []
        self.first_after: list[int] = []
        self.sp_cmax: _Sparse | None = None
        self.sp_cmin: _Sparse | None = None
        self.sess_bar: dict[int, int] = {}

    def start_of(self, e: int) -> int:
        """path index at which tau-bar e starts (the forming bar's start if e == nb)."""
        if e < self.nb:
            return self.start[e]
        if e == self.nb and self.forming_start is not None:
            return self.forming_start
        return _BIG

    def end_of(self, e: int) -> int:
        return self.end[e] if 0 <= e < self.nb else _BIG


# =====================================================================================
# the level instance
# =====================================================================================
class Level:
    """One level instance with the lock 2.11 lifecycle (fields mirror mtfl_marker.COLS)."""

    __slots__ = ("fam", "ti", "kind", "lo", "hi", "kf", "ref", "atr", "exp", "leg", "za", "zb",
                 "seq", "side0", "side", "armb", "brk", "brkc", "actend", "brkend", "touches",
                 "uid")

    def __init__(self, fam: str, ti: int, kind: int, lo: float, hi: float, kf: int, ref: float,
                 atr: float, exp: int, *, leg: int = -1, za: int = -1, zb: int = -1,
                 seq: int = 0) -> None:
        self.fam, self.ti, self.kind = fam, ti, kind
        self.lo, self.hi, self.kf, self.ref, self.atr, self.exp = lo, hi, kf, ref, atr, exp
        self.leg, self.za, self.zb, self.seq = leg, za, zb, seq
        self.side0 = self.side = 0
        self.armb = kf
        self.brk = -1
        self.brkc = self.actend = self.brkend = _BIG
        self.touches = 0
        self.uid = ""

    def state_at(self, t: int) -> str | None:
        """'active' / 'broken' (inside its W-bar window) / None at path index t."""
        if self.kf > t:
            return None
        if t < self.actend:
            return "active"
        if self.brk >= 0 and self.brkc <= t < self.brkend:
            return "broken"
        return None


class Setup:
    """A MIDENTRY setup (AMENDMENT_1 A1.1-A1.4, AMENDMENT_2 R9, AMENDMENT_XA Part 3.10)."""

    __slots__ = ("ti", "d", "otype", "t0", "level", "O", "Oc", "Ob", "atr0", "conf", "tq", "last",
                 "qual", "X", "fb", "entry", "Xf", "code", "R", "rbar", "e", "status")

    def __init__(self) -> None:
        self.ti = self.d = self.otype = self.t0 = self.Oc = self.Ob = self.conf = 0
        self.level: Level | None = None
        self.O = self.atr0 = self.X = _NAN
        self.tq = self.last = _BIG
        self.qual = False
        self.fb = self.rbar = self.e = _BIG
        self.entry = self.Xf = self.R = _NAN
        self.code = PEND
        self.status = "pending"


# =====================================================================================
# the analysis
# =====================================================================================
class Analysis:
    """Everything computed for one snapshot. ``compute()`` turns it into the API payload; the
    tests read it directly to compare with the study's arrays."""

    def __init__(self, symbol: str, now_s: int) -> None:
        self.symbol = (symbol or "").upper()
        self.now_s = int(now_s)
        self.n = 0
        self.status = "ok"
        self.t0: list[int] = []
        self.t1: list[int] = []
        self.mod0: list[int] = []
        self.sess: list[int] = []
        self.oc: list[int] = []
        self.hc: list[int] = []
        self.lc: list[int] = []
        self.cc: list[int] = []
        self.o: list[float] = []
        self.h: list[float] = []
        self.l: list[float] = []
        self.c: list[float] = []
        self.sessions: list[_Sess] = []
        self.in_session = False
        self.asof = 0
        self.taus: list[_Tau] = []
        self.structs: dict[int, Any] = {}
        self.levels: list[Level] = []        # real levels: S, Z, M, F, P, D, R (deduped)
        self.fib05: list[Level] = []         # display-only 0.5 of each leg, F lifecycle, no dedupe
        self.zones: dict[int, list[Level]] = {}
        self.origins: list[tuple] = []
        self.setups: list[Setup] = []
        self.pending: list[dict] = []
        self.calendar_stale = False
        self.round_step = 5.0
        self.tick = 0.01
        self.req_taus: list[int] = []
        self.next_open_s: int | None = None
        self._seq = 0
        self._week_index: dict[date, list[int]] = {}
        self._first_week: date | None = None

    # ------------------------------------------------------------------ path
    def build_path(self, bars_by_res: Mapping[str, Any]) -> None:
        intra: dict[int, dict] = {}
        daily: dict[date, tuple] = {}
        for key, bars in (bars_by_res or {}).items():
            r = parse_resolution(key)
            if r is None:
                continue
            if r == 0:
                daily.update(_daily_by_date(bars, self.now_s))
            elif 30 % r == 0:
                intra[r] = _intraday_by_date(bars, r, self.now_s)
        dates = set(daily)
        for bd in intra.values():
            dates.update(bd)
        dates_sorted = sorted(dates)
        res_list = sorted(intra)
        chosen = []
        for i, d in enumerate(dates_sorted):
            close = close_minute(d) - _RTH_OPEN_MIN
            pick = None
            in_progress = i == len(dates_sorted) - 1 and self.now_s < _epoch(d, close_minute(d))
            if not in_progress:
                for r in res_list:                       # finest complete resolution
                    got = _session_items(intra[r].get(d, {}), r, close, False)
                    if got is not None:
                        pick = (r, got[0], True, close)
                        break
            else:                                        # session in progress: furthest reach
                best = None
                for r in res_list:
                    got = _session_items(intra[r].get(d, {}), r, close, True)
                    if got is not None and (best is None or got[1] > best[2]):
                        best = (r, got[0], got[1])
                if best is not None:
                    r, items, cov = best
                    pick = (r, items, cov >= close, cov)
            if pick is None and d in daily:
                pick = (close, [(0, daily[d])], True, close)
            if pick is not None:
                chosen.append((d, pick))
        for d, (r, items, complete, cov) in chosen:
            s = len(self.sessions)
            open_s = _epoch(d, _RTH_OPEN_MIN)
            start = len(self.t0)
            for m, (oc, hc, lc, cc) in items:
                self.t0.append(open_s + m * 60)
                self.t1.append(open_s + (m + r) * 60)
                self.mod0.append(m)
                self.sess.append(s)
                self.oc.append(oc)
                self.hc.append(hc)
                self.lc.append(lc)
                self.cc.append(cc)
            close = close_minute(d) - _RTH_OPEN_MIN
            self.sessions.append(_Sess(date=d, start=start, end=len(self.t0) - 1, close=close, res=r,
                                       complete=complete, cov_end=cov,
                                       exp_end=start + close // r - 1, open_s=open_s))
            if not calendar_covers(d):
                self.calendar_stale = True
        self.n = len(self.t0)
        self.o = [x / 100.0 for x in self.oc]
        self.h = [x / 100.0 for x in self.hc]
        self.l = [x / 100.0 for x in self.lc]
        self.c = [x / 100.0 for x in self.cc]
        if self.n:
            self.asof = self.t1[-1]
            self.in_session = not self.sessions[-1].complete
            self.sp_h = _Sparse(self.hc, True)
            self.sp_l = _Sparse(self.lc, False)

    # ------------------------------------------------------------------ session helpers
    def sess_of(self, j: int) -> int:
        """session index of path bar j; j >= n is the session in progress (if it continues) or
        the next, not-yet-started session (index S)."""
        if j < self.n:
            return self.sess[j]
        if self.in_session and j <= self.sessions[-1].exp_end:
            return len(self.sessions) - 1
        return len(self.sessions)

    def sess_end_idx(self, s: int) -> int:
        """(virtual) path index of session s's last bar; _BIG for a session not started yet."""
        if s < len(self.sessions):
            return self.sessions[s].exp_end
        return _BIG

    def sess_end_excl(self, s: int) -> int:
        e = self.sess_end_idx(s)
        return e + 1 if e < _BIG else _BIG

    # ------------------------------------------------------------------ tau bars + ATR
    def build_taus(self) -> None:
        self.taus = [self._build_tau(ti) for ti in range(len(TAUS))]

    def _build_tau(self, ti: int) -> _Tau:
        T = _Tau(ti)
        n = self.n
        for s, se in enumerate(self.sessions):
            if not _fits(se.res, ti):
                continue
            j = se.start
            last_complete_bucket = True
            while j <= se.end:
                b = _bucket(self.mod0[j], ti)
                k = j
                while k + 1 <= se.end and _bucket(self.mod0[k + 1], ti) == b:
                    k += 1
                if se.complete or _bucket_end(b, ti, se.close) <= se.cov_end:
                    T.start.append(j)
                    T.end.append(k)
                    T.o.append(self.o[j])
                    T.h.append(max(self.h[j:k + 1]))
                    T.l.append(min(self.l[j:k + 1]))
                    T.c.append(self.c[k])
                    T.cc.append(self.cc[k])
                    T.sess.append(s)
                    last_complete_bucket = True
                else:
                    T.forming_start = j
                    last_complete_bucket = False
                j = k + 1
            if not se.complete and last_complete_bucket and se.cov_end < se.close:
                T.forming_start = n          # next bucket began by the clock, no bar complete yet
        nb = T.nb = len(T.start)
        tr = [0.0] * nb
        for i in range(nb):
            h, lo = T.h[i], T.l[i]
            if i == 0 or (ti != TI_1D and T.sess[i] != T.sess[i - 1]):
                tr[i] = h - lo
            else:
                pc = T.c[i - 1]
                tr[i] = (h if h > pc else pc) - (lo if lo < pc else pc)
        cs = [0.0]
        for x in tr:
            cs.append(cs[-1] + x)
        atr = [_NAN] * nb
        for i in range(ATR_N - 1, nb):
            atr[i] = (cs[i + 1] - cs[i + 1 - ATR_N]) / ATR_N
        T.atr = atr
        last_done = [-1] * (n + 1)
        first_after = [nb] * (n + 1)
        k = 0
        ld = -1
        for j in range(n + 1):
            while k < nb and T.end[k] < j:
                ld = k
                k += 1
            last_done[j] = ld
            first_after[j] = k
        T.last_done = last_done
        T.first_after = first_after
        T.sp_cmax = _Sparse(T.cc, True)
        T.sp_cmin = _Sparse(T.cc, False)
        if ti == TI_1D:
            T.sess_bar = {T.sess[i]: i for i in range(nb)}
        return T

    def atr_at(self, ti: int, j: int) -> float:
        """ATR_tau known at the START of path bar j (the last COMPLETED tau-bar; AMENDMENT_2 R2)."""
        T = self.taus[ti]
        b = T.last_done[j if j <= self.n else self.n]
        return T.atr[b] if b >= 0 else _NAN

    def maxage_exp(self, ti: int, kfb: int) -> int:
        """S/Z/M/F max age: expire at the start of the 300th (1D: 250th) tau-bar after the one
        containing known_from (mtfl_marker.maxage_exp)."""
        T = self.taus[ti]
        b = T.first_after[kfb if kfb <= self.n else self.n]
        return T.start_of(b + (MAXAGE_1D if ti == TI_1D else MAXAGE_TAU))

    # ------------------------------------------------------------------ structure (zigzag)
    def build_structure(self, ti: int) -> None:
        T = self.taus[ti]
        ptype, ppx, pbar, pconf, patr, state = _zigzag(T.h, T.l, T.atr, K_ZZ)
        kf = [T.end[cf] + 1 for cf in pconf]
        p1m = []
        for q, b in enumerate(pbar):
            s0, e0 = T.start[b], T.end[b]
            pxc = round(ppx[q] * 100)
            j = s0
            if ptype[q] == 1:
                while j < e0 and self.hc[j] < pxc:
                    j += 1
            else:
                while j < e0 and self.lc[j] > pxc:
                    j += 1
            p1m.append(j)
        self.structs[ti] = _Struct(ti, ptype, ppx, pbar, pconf, patr, kf, p1m, state)

    # ------------------------------------------------------------------ lifecycle (lock 2.11)
    def lifecycle(self, lv: Level) -> None:
        n = self.n
        side0 = 1 if lv.ref > lv.hi else (-1 if lv.ref < lv.lo else 0)
        lv.side0 = lv.side = side0
        lv.armb = lv.kf
        if side0 == 0:
            # inside its band at known_from: armed at the close of the first path bar that
            # leaves the band by >= 0.25 ATR (ATR of the last COMPLETED tau-bar, R2)
            lv.armb = _BIG
            lv.side = 0
            for j in range(lv.kf, min(lv.exp, n)):
                aa = self.atr_at(lv.ti, j)
                if aa != aa:
                    continue
                if self.cc[j] >= _c_ge(lv.hi + 0.25 * aa):
                    lv.armb, lv.side = j + 1, 1
                    break
                if self.cc[j] <= _c_le(lv.lo - 0.25 * aa):
                    lv.armb, lv.side = j + 1, -1
                    break
        lv.brk = -1
        brkc = wend = _BIG
        if lv.side != 0 and lv.armb < n:
            T = self.taus[lv.ti]
            b0 = T.first_after[lv.armb]
            if lv.side == -1:
                b = T.sp_cmax.first_ge(b0, _c_gt(lv.hi))
            else:
                b = T.sp_cmin.first_le(b0, _c_lt(lv.lo))
            if b < T.nb and T.end[b] < lv.exp:
                lv.brk = b
                brkc = T.end[b] + 1
                w = b + W_BARS
                wend = T.end[w] + 1 if w < T.nb else _BIG
        if lv.brk >= 0:
            lv.brkc = brkc
            lv.actend = min(brkc, lv.exp)
            lv.brkend = min(wend, lv.exp)
        else:
            lv.brkc = _BIG
            lv.actend = lv.exp
            lv.brkend = lv.exp

    # ------------------------------------------------------------------ families
    def _new(self, fam: str, ti: int, kind: int, lo: float, hi: float, kf: int, ref: float,
             atr: float, exp: int, **kw: Any) -> Level:
        self._seq += 1
        return Level(fam, ti, kind, lo, hi, kf, ref, atr, exp, seq=self._seq, **kw)

    def levels_S(self, ti: int) -> list[Level]:
        S = self.structs[ti]
        out = []
        for q in range(len(S.px)):
            kf = S.kf[q]
            if kf > self.n:
                continue
            px = S.px[q]
            out.append(self._new("S", ti, 1 if S.type[q] == 1 else 0, px, px, kf, self.c[kf - 1],
                                 self.atr_at(ti, kf), self.maxage_exp(ti, kf)))
        return out

    def legs(self, ti: int) -> list[tuple]:
        """completed legs (mtfl_marker.legs_of): (q, start, end, kf, exp), born-expired dropped (R7)."""
        S = self.structs[ti]
        npv = len(S.px)
        out = []
        n = self.n
        for q in range(npv - 1):
            st, en = S.px[q], S.px[q + 1]
            kf = S.kf[q + 1]
            if kf > n:
                continue
            drop = S.kf[q + 3] if q + 3 < npv and S.kf[q + 3] <= n else _BIG
            hiP = st if st > en else en
            loP = st if st < en else en
            j1 = self.sp_h.first_ge(kf, _c_gt(hiP)) if kf < n else n
            j2 = self.sp_l.first_le(kf, _c_lt(loP)) if kf < n else n
            beyond = min(j1, j2)
            if beyond >= n:
                beyond = _BIG
            exp = min(beyond, drop, self.maxage_exp(ti, kf))
            xb = S.p1m[q + 1]
            lastb = max(kf - 1, xb)
            born = (self.sp_h.ext(xb, lastb) >= _c_gt(hiP)) or (self.sp_l.ext(xb, lastb) <= _c_lt(loP))
            if born:
                continue
            out.append((q, st, en, kf, exp))
        return out

    def levels_from_legs(self, ti: int, fam: str, ratios: tuple[float, ...], legs: list) -> list[Level]:
        out = []
        for q, st, en, kf, exp in legs:
            for ri, r in enumerate(ratios):
                px = en - r * (en - st)
                out.append(self._new(fam, ti, ri, px, px, kf, self.c[kf - 1], self.atr_at(ti, kf), exp,
                                     leg=q + 100000 * ti))
        return out

    def levels_Z(self, ti: int) -> list[Level]:
        """Sequential multi-touch zone formation (lock 2.4, R8; mtfl_marker.levels_Z)."""
        S = self.structs[ti]
        win = ZONE_WIN_1D if ti == TI_1D else ZONE_WIN
        zones: list[Level] = []
        active: list[Level] = []
        for p in range(len(S.px)):
            t = S.kf[p]
            if t > self.n:
                break
            active = [z for z in active if z.actend > t]
            ppx = S.px[p]
            inside = [z for z in active if z.lo <= ppx <= z.hi]
            if inside:
                inside[0].touches += 1
                continue
            atrP = self.atr_at(ti, t)
            if atrP != atrP:
                continue
            q0 = bisect.bisect_right(S.bar, S.conf[p] - win, 0, p + 1)
            rad = 0.5 * atrP + _EPS_TIE
            mem = [S.px[q] for q in range(q0, p + 1) if abs(S.px[q] - ppx) <= rad]
            if len(mem) < 3:
                continue
            lo, hi = min(mem), max(mem)
            if any(z.lo <= hi and lo <= z.hi for z in active):
                continue
            thr = 0.1 * atrP - _EPS_TIE
            if any(max(0.0, z.lo - hi, lo - z.hi) < thr for z in active):
                continue
            z = Level("Z", ti, 0, lo, hi, t, self.c[t - 1], atrP, self.maxage_exp(ti, t),
                      za=len(zones) + 100000 * ti)
            self.lifecycle(z)
            z.touches = len(mem)
            zones.append(z)
            active.append(z)
        return zones

    def levels_Mb(self, ti: int, zones: list[Level]) -> list[Level]:
        """Dealing-range midpoints (lock 2.5b; mtfl_marker.levels_Mb)."""
        if len(zones) < 2:
            return []
        n = self.n
        times = sorted({z.kf for z in zones} | {z.actend for z in zones if z.actend <= n})
        seen: set = set()
        out = []
        for t in times:
            act = [z for z in zones if z.kf <= t < z.actend and z.side != 0]
            ab = [z for z in act if z.side == -1]
            be = [z for z in act if z.side == 1]
            if not ab or not be:
                continue
            a = min(ab, key=lambda z: z.lo)
            b = max(be, key=lambda z: z.hi)
            key = (a.za, b.za)
            if key in seen:
                continue
            seen.add(key)
            atr_t = self.atr_at(ti, t)
            gap = a.lo - b.hi
            if atr_t != atr_t or not (1.0 * atr_t - _EPS_TIE <= gap <= 8.0 * atr_t + _EPS_TIE):
                continue
            ex = min(a.actend, b.actend, self.maxage_exp(ti, t))
            if t >= ex:
                continue
            px = 0.5 * (a.lo + b.hi)
            out.append(self._new("M", ti, 1, px, px, t, self.c[t - 1], atr_t, ex, za=a.za, zb=b.za))
        return out

    # ------------------------------------------------------------------ P / D / R (lock 2.7-2.9)
    def _prev_ok(self, s: int) -> bool:
        if s < 1:
            return False
        d, pd = self.sessions[s].date, self.sessions[s - 1].date
        if calendar_covers(d):
            return _prev_session_day(d) == pd
        return (d - pd).days <= 4

    def _week_complete(self, monday: date) -> bool:
        have = {self.sessions[k].date for k in self._week_index.get(monday, [])}
        exp = _week_session_days(monday)
        if calendar_covers(monday):
            return all(x in have for x in exp)
        return len(have) >= max(1, len(exp) - 1) and monday != self._first_week

    def _week_end_excl(self, s: int) -> int:
        """exclusive end of session s's calendar week (the study's week_end_sess + 1)."""
        mon = _monday(self.sessions[s].date)
        days = _week_session_days(mon)
        last_day = days[-1] if days else self.sessions[s].date
        idx = self._week_index.get(mon, [])
        if last_day > self.sessions[-1].date:
            return _BIG                  # the week is not over in the data
        return self.sess_end_excl(idx[-1]) if idx else _BIG

    def pdr_rows(self, s: int) -> list[tuple[str, int, float, int]]:
        """(family, kind, price, exp) rows for session s, in the study's creation order."""
        T1 = self.taus[TI_1D]
        se = self.sessions[s]
        kf = se.start
        op = self.o[kf]
        rows: list[tuple[str, int, float, int]] = []
        prev = self._prev_ok(s) and (s - 1) in T1.sess_bar
        send = self.sess_end_excl(s)
        if prev:
            b = T1.sess_bar[s - 1]
            H, L, C = T1.h[b], T1.l[b], T1.c[b]
            Pp = (H + L + C) / 3.0
            for kk, v in enumerate((Pp, 2 * Pp - L, 2 * Pp - H, Pp + (H - L), Pp - (H - L))):
                rows.append(("P", kk, v, send))
        mon = _monday(se.date)
        week_first = s == 0 or _monday(self.sessions[s - 1].date) != mon
        if week_first and s >= 1:
            pmon = _monday(self.sessions[s - 1].date)
            if (mon - pmon).days == 7 and self._week_complete(pmon):
                idx = [k for k in self._week_index.get(pmon, []) if k in T1.sess_bar]
                if idx:
                    WH = max(T1.h[T1.sess_bar[k]] for k in idx)
                    WL = min(T1.l[T1.sess_bar[k]] for k in idx)
                    wend = self._week_end_excl(s)
                    rows.append(("D", 0, WH, wend))
                    rows.append(("D", 1, WL, wend))
        if prev:
            rows.append(("D", 2, H, send))
            rows.append(("D", 3, L, send))
            rows.append(("D", 4, C, send))
        pb = T1.sess_bar.get(s - 1, -1)
        A1 = T1.atr[pb] if pb >= 0 else _NAN
        if A1 == A1:
            step = self.round_step
            k0 = math.ceil((op - 2 * A1) / step - 1e-12)
            k1 = math.floor((op + 2 * A1) / step + 1e-12)
            for k in range(k0, k1 + 1):
                rows.append(("R", 0, step * k, send))
        return rows

    def levels_PDR(self, scales: Iterable[int]) -> list[Level]:
        scales = tuple(scales)
        out = []
        for s in range(1, len(self.sessions)):
            kf = self.sessions[s].start
            if kf >= self.n:
                continue
            rows = self.pdr_rows(s)
            if not rows:
                continue
            op = self.o[kf]
            for ti in scales:
                a = self.atr_at(ti, kf)
                if a != a:
                    continue
                for fam, kind, v, ex in rows:
                    out.append(self._new(fam, ti, kind, v, v, kf, op, a, ex))
        return out

    # ------------------------------------------------------------------ dedupe (lock 2.11)
    @staticmethod
    def dedupe(cands: list[Level]) -> list[Level]:
        """Within one family-tau, drop a new level within 0.1 ATR_tau of an ACTIVE one (older kept)."""
        groups: dict[tuple[str, int], list[Level]] = {}
        for lv in cands:
            groups.setdefault((lv.fam, lv.ti), []).append(lv)
        keep: list[Level] = []
        for _, L in groups.items():
            L.sort(key=lambda x: (x.kf, x.seq))
            act: list[Level] = []
            for lv in L:
                t = lv.kf
                if act:
                    act = [a for a in act if a.actend > t]
                thr = 0.1 * lv.atr - _EPS_TIE
                bad = False
                for a in act:
                    if max(0.0, a.lo - lv.hi, lv.lo - a.hi) < thr:
                        bad = True
                        break
                if bad:
                    continue
                keep.append(lv)
                act.append(lv)
        return keep

    # ------------------------------------------------------------------ the marker
    def mark(self, req: list[int], pdr_scales: Iterable[int]) -> None:
        self._seq = 0
        self._week_index: dict[date, list[int]] = {}
        for s, se in enumerate(self.sessions):
            self._week_index.setdefault(_monday(se.date), []).append(s)
        self._first_week = _monday(self.sessions[0].date)
        for ti in req:
            self.build_structure(ti)
        legs = {ti: self.legs(ti) for ti in req}
        Ls = [lv for ti in req for lv in self.levels_S(ti)]
        for ti in req:
            self.zones[ti] = self.levels_Z(ti)
        Ma = [lv for ti in req for lv in self.levels_from_legs(ti, "M", (0.5,), legs[ti])]
        Mb = [lv for ti in req for lv in self.levels_Mb(ti, self.zones[ti])]
        Fl = [lv for ti in req for lv in self.levels_from_legs(ti, "F", F_RATIOS, legs[ti])]
        PDR = self.levels_PDR(pdr_scales)
        cand = Ls + Ma + Mb + Fl + PDR
        for lv in cand:
            self.lifecycle(lv)
        kept = self.dedupe(cand)
        self.levels = kept + [z for ti in req for z in self.zones[ti]]
        # the study's canonical order (fam, tau, kf, kind, lo, leg): it decides which level is the
        # defining origin when several origins of one type tie on a MIDENTRY setup
        self.levels.sort(key=lambda lv: (_FAM_IDX[lv.fam], lv.ti, lv.kf, lv.kind, lv.lo, lv.leg))
        # the 0.5 of every leg for the fib set on the chart: the study's fib-ratio arm (lock 4.3)
        # -- F lifecycle, R7, NO family dedupe (it never displaces a real level)
        self.fib05 = [lv for ti in req for lv in self.levels_from_legs(ti, "F", (0.5,), legs[ti])]
        for lv in self.fib05:
            self.lifecycle(lv)
        for lv in self.levels:
            lv.uid = self._uid(lv, _kind_name(lv))
        for lv in self.fib05:
            lv.uid = self._uid(lv, "fib_0.5")

    def _uid(self, lv: Level, kind: str) -> str:
        kfs = self.known_from_s(lv)
        tail = f"{round(lv.lo * 10000)}" if lv.lo == lv.hi else f"{round(lv.lo * 10000)}-{round(lv.hi * 10000)}"
        return f"{lv.fam}{TAUS[lv.ti]}-{kind}-{kfs}-{tail}"

    def known_from_s(self, lv: Level) -> int:
        """epoch seconds the level became known: the tau close (S/Z/M/F), or the session open (P/D/R)."""
        if lv.fam in ("P", "D", "R"):
            return self.t0[lv.kf] if lv.kf < self.n else self.asof
        return self.t1[lv.kf - 1] if 0 < lv.kf <= self.n else self.asof

    # ------------------------------------------------------------------ events -> MIDENTRY origins
    def horizon_end(self, ti: int, first_bar: int, trig_bar: int) -> int:
        """last bar a race may use: 5m-1H the trigger session's end; 4H/1D up to 5 sessions
        counting the session of the first monitored bar (mtfl_events.horizon_end)."""
        if ti <= TI_1H:
            return self.sess_end_idx(self.sess_of(trig_bar))
        return self.sess_end_idx(self.sess_of(first_bar) + 4)

    def race(self, s: int, e: int, U: float, Dn: float) -> tuple[int, int]:
        """first of U (up) / Dn (down) hit from bar s through e: +1 / -1, 0 both in one bar,
        2 unresolved at e, 3 still running (e not reached yet)."""
        n = self.n
        if s > e:
            return 2, _BIG
        tU, tD = _c_ge(U), _c_le(Dn)
        lim = min(e, n - 1)
        jU = jD = _BIG
        if s <= lim:
            j = self.sp_h.first_ge(s, tU)
            if j <= lim:
                jU = j
            j = self.sp_l.first_le(s, tD)
            if j <= lim:
                jD = j
        if jU == _BIG and jD == _BIG:
            return (2, _BIG) if e < n else (3, _BIG)
        if jU < jD:
            return 1, jU
        if jD < jU:
            return -1, jD
        oc = self.oc[jU]
        if oc >= tU:
            return 1, jU
        if oc <= tD:
            return -1, jU
        return 0, jU

    def touch_race(self, j: int, below: bool, lo: float, hi: float, x: float, e: int) -> tuple[int, int]:
        """REJECT race from the touch bar, AMENDMENT_2 R3 conventions (mtfl_events.touch_race)."""
        U, Dn = hi + x, lo - x
        tU, tD = _c_ge(U), _c_le(Dn)
        oc, hc, lc = self.oc[j], self.hc[j], self.lc[j]
        if below:
            gap = oc >= tU
            at_open = oc >= _c_ge(lo)
        else:
            gap = oc <= tD
            at_open = oc <= _c_le(hi)
        hit_u, hit_d = hc >= tU, lc <= tD
        hit_fail = hit_u if below else hit_d
        hit_succ = hit_d if below else hit_u
        if gap:
            return GAP, j
        if at_open:
            if hit_fail and hit_succ:
                return AMB, j
            if hit_fail:
                return FAIL, j
            if hit_succ:
                return SUCC, j
        else:
            if hit_succ:
                return AMB, j
            if hit_fail:
                return FAIL, j
        code, bar = self.race(j + 1, e, U, Dn)
        if code == 2:
            return UNRES, bar
        if code == 3:
            return PEND, bar
        if code == 0:
            return AMB, bar
        return (SUCC if (code == 1) == (not below) else FAIL), bar

    def find_origins(self, req: set[int]) -> None:
        """BREAK / RECLAIM triggers (any race outcome) and REJECT successes at real levels
        (AMENDMENT_1 A1.1, AMENDMENT_XA 3.10): (ti, t0, d, otype, level)."""
        n = self.n
        out = []
        for lv in self.levels:
            if lv.ti not in req or lv.side == 0 or lv.armb >= n:
                continue
            T = self.taus[lv.ti]
            below = lv.side == -1
            # REJECT: first touch after arming, before the level stops being ACTIVE
            if below:
                jA = self.sp_h.first_ge(lv.armb, _c_ge(lv.lo))
            else:
                jA = self.sp_l.first_le(lv.armb, _c_le(lv.hi))
            if jA < n and jA < lv.actend:
                x = 0.5 * self.atr_at(lv.ti, jA)
                if x == x:
                    e = self.horizon_end(lv.ti, jA, jA)
                    code, rb = self.touch_race(jA, below, lv.lo, lv.hi, x, e)
                    if code == SUCC:
                        out.append((lv.ti, rb, -1 if below else 1, 2, lv))
            # BREAK at the tau close beyond the band, RECLAIM back inside within W tau-bars
            if lv.brk >= 0:
                b = lv.brk
                out.append((lv.ti, T.end[b], 1 if below else -1, 0, lv))
                if b + 1 < T.nb:
                    if below:
                        bb = T.sp_cmin.first_le(b + 1, _c_lt(lv.lo))
                    else:
                        bb = T.sp_cmax.first_ge(b + 1, _c_gt(lv.hi))
                    if bb < T.nb and bb <= b + W_BARS and T.end[bb] < lv.exp:
                        out.append((lv.ti, T.end[bb], -1 if below else 1, 1, lv))
        self.origins = out

    # ------------------------------------------------------------------ MIDENTRY setups
    def order_cap(self, ti: int, t0: int) -> int:
        if ti <= TI_1H:
            return self.sess_end_idx(self.sess_of(t0)) - 1
        return self.sess_end_idx(self.sess_of(t0) + 4)

    def build_setups(self, req: set[int]) -> None:
        setups: list[Setup] = []
        for ti in sorted(req):
            S = self.structs[ti]
            T = self.taus[ti]
            hk = [S.kf[q] for q in range(len(S.px)) if S.type[q] == 1]
            hq = [q for q in range(len(S.px)) if S.type[q] == 1]
            lk = [S.kf[q] for q in range(len(S.px)) if S.type[q] == -1]
            lq = [q for q in range(len(S.px)) if S.type[q] == -1]
            cands = []
            for (oti, t0, d, otype, lv) in self.origins:
                if oti != ti:
                    continue
                # A1.2: O = extreme since the last OPPOSITE swing's extreme bar (confirmed by t0)
                if d == 1:
                    k = bisect.bisect_right(hk, t0) - 1
                    q = hq[k] if k >= 0 else -1
                else:
                    k = bisect.bisect_right(lk, t0) - 1
                    q = lq[k] if k >= 0 else -1
                if q < 0:
                    continue
                e1 = S.p1m[q]
                if e1 > t0:
                    continue
                if d == 1:
                    Oc = self.sp_l.ext(e1, t0)
                    Ob = self.sp_l.first_le(e1, Oc)
                else:
                    Oc = self.sp_h.ext(e1, t0)
                    Ob = self.sp_h.first_ge(e1, Oc)
                a0 = self.atr_at(ti, t0 + 1)       # ATR known at the close of t0's bar (XA 3.10)
                if a0 != a0:
                    continue
                cands.append((Ob * 2 + (1 if d == 1 else 0), t0, otype, d, Oc, Ob, a0, lv))
            cands.sort(key=lambda x: (x[0], x[1], x[2]))
            i = 0
            while i < len(cands):
                j = i
                while j + 1 < len(cands) and cands[j + 1][0] == cands[i][0]:
                    j += 1
                key, t0, otype, d, Oc, Ob, a0, lv = cands[i]
                st = Setup()
                st.ti, st.d, st.otype, st.t0, st.level = ti, d, otype, t0, lv
                st.Oc, st.Ob, st.atr0, st.conf = Oc, Ob, a0, j - i + 1
                st.O = Oc / 100.0
                self._run_setup(st, S, T)
                setups.append(st)
                i = j + 1
        self.setups = setups

    def _run_setup(self, st: Setup, S: Any, T: _Tau) -> None:
        n = self.n
        d, opx = st.d, st.O
        thr = opx + d * QUAL_ATR * st.atr0
        if d == 1:
            jq = self.sp_h.first_ge(st.Ob, _c_ge(thr))
        else:
            jq = self.sp_l.first_le(st.Ob, _c_le(thr))
        if jq >= n:
            st.tq = _BIG
            st.qual = False
            st.status = "forming"
            return
        st.tq = tq = max(st.t0, jq)
        cap = self.order_cap(st.ti, st.t0)
        bq = T.first_after[tq]
        w_end = T.end_of(bq + W_BARS)
        st.last = last = min(cap, w_end)
        st.qual = tq < n and tq + 1 <= last
        if not st.qual:
            st.status = "unqualified"
            return
        # A1.3: buy limit at M(t-1) re-placed each bar close; CONV-LEVEL fill (trade through by 1 tick)
        X = (self.sp_h.ext(st.Ob, tq) if d == 1 else self.sp_l.ext(st.Ob, tq)) / 100.0
        tick = self.tick
        t = tq + 1
        lim = min(last, n - 1)
        fb = _BIG
        while t <= lim:
            entry = X - 0.5 * (X - opx)
            if d == 1:
                hit = self.lc[t] <= _c_le(entry - tick)
            else:
                hit = self.hc[t] >= _c_ge(entry + tick)
            if hit:
                fb = t
                st.entry = entry
                st.Xf = X
                break
            if d == 1:
                X = X if X >= self.h[t] else self.h[t]
            else:
                X = X if X <= self.l[t] else self.l[t]
            t += 1
        st.X = X
        st.fb = fb
        if fb == _BIG:
            if last < n:
                st.status = "expired"
                st.code = UNRES
                st.rbar = last
            else:
                st.status = "waiting"
            return
        # A1.4: stop O, target X (frozen at the fill); fill bar reaching either -> AMBIGUOUS (R9b)
        entry, stop, target = st.entry, opx, st.Xf
        risk = abs(entry - stop)
        rwin = round(abs(target - entry) / risk, 9) if risk > 0 else _NAN
        if d == 1:
            hit_stop = self.lc[fb] <= _c_le(stop)
            hit_tgt = self.hc[fb] >= _c_ge(target)
        else:
            hit_stop = self.hc[fb] >= _c_ge(stop)
            hit_tgt = self.lc[fb] <= _c_le(target)
        if hit_stop or hit_tgt:
            st.code, st.rbar, st.R, st.status = AMB, fb, -1.0, "lost"
            return
        e = self.horizon_end(st.ti, fb + 1, fb)
        st.e = e
        U, Dn = (target, stop) if d == 1 else (stop, target)
        cr, bar = self.race(fb + 1, e, U, Dn)
        if cr == 3:
            st.code, st.status = PEND, "filled"
        elif cr == 2:
            st.code, st.rbar, st.status = UNRES, e, "expired"
            st.R = (self.c[e] - entry) * d / risk if risk > 0 and e < n else _NAN
        elif cr == 0:
            st.code, st.rbar, st.R, st.status = AMB, bar, -1.0, "lost"
        elif cr == d:
            st.code, st.rbar, st.R, st.status = SUCC, bar, rwin, "won"
        else:
            st.code, st.rbar, st.R, st.status = FAIL, bar, -1.0, "lost"

    # ------------------------------------------------------------------ the NEXT session's P / D / R
    def pending_pdr(self) -> list[Level]:
        """P / D / R of the next session while none is in progress (after the close, pre-market).

        The study defines them as known at 09:30 (lock 2.7-2.9), so between sessions it has none
        active. Their prices are fixed by the last session (and, for PWH/PWL, the last complete
        week), so they are shown early with state 'pending'. Round numbers and the role
        (support / resistance) use the last close in place of the not-yet-known open. Deduped at
        the 1H scale like the real instances (lock 2.11)."""
        if self.n == 0 or self.in_session or not self.sessions:
            return []
        s = len(self.sessions) - 1
        T1 = self.taus[TI_1D]
        b = T1.sess_bar.get(s)
        if b is None:
            return []
        last_day = self.sessions[s].date
        nxt = _next_session_day(last_day)
        H, L, C = T1.h[b], T1.l[b], T1.c[b]
        rows: list[tuple[str, int, float]] = []
        Pp = (H + L + C) / 3.0
        for kk, v in enumerate((Pp, 2 * Pp - L, 2 * Pp - H, Pp + (H - L), Pp - (H - L))):
            rows.append(("P", kk, v))
        pmon = _monday(last_day)
        if _monday(nxt) != pmon and (_monday(nxt) - pmon).days == 7 and self._week_complete(pmon):
            idx = [k for k in self._week_index.get(pmon, []) if k in T1.sess_bar]
            if idx:
                rows.append(("D", 0, max(T1.h[T1.sess_bar[k]] for k in idx)))
                rows.append(("D", 1, min(T1.l[T1.sess_bar[k]] for k in idx)))
        rows += [("D", 2, H), ("D", 3, L), ("D", 4, C)]
        A1 = T1.atr[b]
        if A1 == A1:
            step = self.round_step
            for k in range(math.ceil((C - 2 * A1) / step - 1e-12), math.floor((C + 2 * A1) / step + 1e-12) + 1):
                rows.append(("R", 0, step * k))
        a = self.atr_now(TI_1H)
        act: dict[str, list[tuple[float, float]]] = {
            f: [(lv.lo, lv.hi) for lv in self.levels
                if lv.fam == f and lv.ti == TI_1H and lv.state_at(self.n) == "active"]
            for f in ("P", "D", "R")}
        thr = 0.1 * a - _EPS_TIE
        out = []
        open_s = _epoch(nxt, _RTH_OPEN_MIN)
        for fam, kind, v in rows:
            if any(max(0.0, lo - v, v - hi) < thr for lo, hi in act[fam]):
                continue
            lv = Level(fam, TI_1H, kind, v, v, self.n, C, a, _BIG)
            lv.side0 = lv.side = 1 if C > v else (-1 if C < v else 0)
            lv.uid = f"{fam}1H-{_kind_name(lv)}-{open_s}-{round(v * 10000)}-pending"
            out.append(lv)
            act[fam].append((v, v))
        self.next_open_s = open_s
        return out

    # ------------------------------------------------------------------ helpers for output
    def atr_now(self, ti: int) -> float:
        return self.atr_at(ti, self.n)


class _Struct:
    __slots__ = ("ti", "type", "px", "bar", "conf", "patr", "kf", "p1m", "state")

    def __init__(self, ti, ptype, ppx, pbar, pconf, patr, kf, p1m, state) -> None:
        self.ti, self.type, self.px, self.bar, self.conf = ti, ptype, ppx, pbar, pconf
        self.patr, self.kf, self.p1m, self.state = patr, kf, p1m, state


def _zigzag(h: list[float], lo: list[float], atr: list[float], k: float):
    """ATR zigzag on tau-bars (lock 2.3 + AMENDMENT_2 R1; port of mtfl_core.zigzag).

    The reversal threshold uses the ATR at the close of the extreme's own bar; an outside bar that
    sets a new extreme updates the extreme first and tests reversal from the next bar."""
    nb = len(h)
    ptype: list[int] = []
    ppx: list[float] = []
    pbar: list[int] = []
    pconf: list[int] = []
    patr: list[float] = []
    state = [0] * nb
    st = 0
    first = nb
    for i in range(nb):
        if atr[i] == atr[i]:
            first = i
            break
    Hs, Hb, Ha = -math.inf, -1, _NAN
    Ls, Lb, La = math.inf, -1, _NAN

    def _argmin(a: int, b: int) -> int:
        j = a
        for x in range(a + 1, b + 1):
            if lo[x] < lo[j]:
                j = x
        return j

    def _argmax(a: int, b: int) -> int:
        j = a
        for x in range(a + 1, b + 1):
            if h[x] > h[j]:
                j = x
        return j

    def _pivot(kind: int, px: float, bar: int, conf: int, a: float) -> None:
        ptype.append(kind)
        ppx.append(px)
        pbar.append(bar)
        pconf.append(conf)
        patr.append(a)

    for i in range(first, nb):
        hi_, lo_ = h[i], lo[i]
        if st == 0:
            newH = hi_ > Hs
            newL = lo_ < Ls
            if newH:
                Hs, Hb, Ha = hi_, i, atr[i]
            if newL:
                Ls, Lb, La = lo_, i, atr[i]
            confH = (not newH) and Hb >= 0 and lo_ <= Hs - k * Ha + _EPS_TIE
            confL = (not newL) and Lb >= 0 and hi_ >= Ls + k * La - _EPS_TIE
            if confH and confL:
                if Hb < Lb:
                    confL = False
                else:
                    confH = False
            if confH:
                _pivot(1, Hs, Hb, i, Ha)
                st = -1
                j = _argmin(Hb + 1, i)
                Ls, Lb, La = lo[j], j, atr[j]
            elif confL:
                _pivot(-1, Ls, Lb, i, La)
                st = 1
                j = _argmax(Lb + 1, i)
                Hs, Hb, Ha = h[j], j, atr[j]
        elif st == 1:
            if hi_ > Hs:
                Hs, Hb, Ha = hi_, i, atr[i]
            elif lo_ <= Hs - k * Ha + _EPS_TIE:
                _pivot(1, Hs, Hb, i, Ha)
                st = -1
                j = _argmin(Hb + 1, i)
                Ls, Lb, La = lo[j], j, atr[j]
        else:
            if lo_ < Ls:
                Ls, Lb, La = lo_, i, atr[i]
            elif hi_ >= Ls + k * La - _EPS_TIE:
                _pivot(-1, Ls, Lb, i, La)
                st = 1
                j = _argmax(Lb + 1, i)
                Hs, Hb, Ha = h[j], j, atr[j]
        state[i] = st
    return ptype, ppx, pbar, pconf, patr, state


# =====================================================================================
# labels / kinds
# =====================================================================================
_FIB_TXT = {0: ".618", 1: ".75", 2: ".87"}


def _kind_name(lv: Level) -> str:
    f = lv.fam
    if f == "S":
        return "swing_high" if lv.kind == 1 else "swing_low"
    if f == "Z":
        return "zone"
    if f == "M":
        return "mid_range" if lv.kind == 1 else "mid_leg"
    if f == "F":
        return ("fib_0.618", "fib_0.75", "fib_0.87")[lv.kind]
    if f == "P":
        return _P_KINDS[lv.kind]
    if f == "D":
        return _D_KINDS[lv.kind]
    return "round"


def _fmt_price(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v))}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _label(lv: Level, fib05: bool = False) -> str:
    tf = TAUS[lv.ti]
    f = lv.fam
    if fib05:
        return f"{tf} .5"
    if f == "S":
        return f"{tf} SwH" if lv.kind == 1 else f"{tf} SwL"
    if f == "Z":
        return f"{tf} zone x{lv.touches}"
    if f == "M":
        return f"{tf} rng mid" if lv.kind == 1 else f"{tf} mid"
    if f == "F":
        return f"{tf} {_FIB_TXT[lv.kind]}"
    if f == "P":
        return _P_KINDS[lv.kind]
    if f == "D":
        return _D_KINDS[lv.kind]
    return f"${_fmt_price(lv.lo)}"


_FAM_PRIO = {"S": 0, "Z": 1, "F": 2, "M": 3, "D": 4, "P": 5, "R": 6}


# =====================================================================================
# public API
# =====================================================================================
def analyze(bars_by_res: Mapping[str, Any], now_s: int, symbol: str = "", *,
            taus: Iterable[str] = TAUS, with_setups: bool = True,
            pdr_scales: Iterable[int] | None = None) -> Analysis:
    """Build the full analysis (path, tau-bars, levels with lifecycle, origins, setups)."""
    A = Analysis(symbol, now_s)
    A.build_path(bars_by_res)
    if A.n == 0:
        A.status = "no regular-hours bars"
        return A
    A.build_taus()
    A.tick = tick_size(A.symbol)
    A.round_step = round_step(A.symbol, A.c[-1])
    req = sorted({TAUS.index(t) for t in taus if t in TAUS})
    A.req_taus = req
    if pdr_scales is None:
        # the 1H instance is the one drawn; the 5m instance only feeds 5m MIDENTRY origins
        scales = {DISPLAY_PDR_SCALE}
        if with_setups and TI_5M in req:
            scales.add(TI_5M)
    else:
        scales = {int(x) for x in pdr_scales if int(x) in PDR_SCALES}
    A.mark(req, sorted(scales))
    if with_setups:
        rs = set(req)
        A.find_origins(rs)
        A.build_setups(rs)
    return A


def compute(bars_by_res: Mapping[str, Any], now_s: int, symbol: str, *,
            taus: Iterable[str] = TAUS, families: Iterable[str] = FAMILIES,
            with_setups: bool = True, include_all_setups: bool = False) -> dict[str, Any]:
    """The /chart/structure_levels payload as of ``now_s`` (epoch seconds).

    ``levels``: ACTIVE levels plus BROKEN ones still inside their 12-tau-bar window (``state``),
    one entry per level instance. P / D / R are reported once, at the 1H reaction scale, with
    ``tf`` = "1D" (they are daily levels); the timeframe filter does not apply to them. Between
    sessions the next session's P / D / R are listed with ``state`` = "pending".
    ``groups``: point levels of different family-tau within 0.25 ATR_5m share a ``group`` id; the
    group carries a merged label and the dominant (highest-timeframe) member's tf / family.
    ``setups``: MIDENTRY setups that are ``waiting`` or ``filled``, plus those resolved in the last
    30 minutes (``recent``: true).
    """
    taus = [t for t in taus if t in TAUS]
    fams = {f.upper() for f in families if isinstance(f, str) and f.upper() in FAMILIES}
    A = analyze(bars_by_res, now_s, symbol, taus=taus, with_setups=with_setups)
    out: dict[str, Any] = {
        "symbol": A.symbol, "asof": A.asof or None, "now": int(now_s), "status": A.status,
        "taus": taus, "families": sorted(fams), "levels": [], "groups": [], "setups": [],
        "atr": {}, "last_price": None, "round_step": A.round_step, "in_session": A.in_session,
        "calendar_through": CALENDAR_LAST.isoformat(), "calendar_stale": A.calendar_stale,
        "sessions": 0, "path": {},
    }
    if A.n == 0:
        return out
    n = A.n
    out["last_price"] = A.c[-1]
    out["sessions"] = len(A.sessions)
    out["path"] = {"first_session": A.sessions[0].date.isoformat(),
                   "last_session": A.sessions[-1].date.isoformat(),
                   "resolutions": sorted({se.res for se in A.sessions if se.res < se.close}),
                   "daily_only_sessions": sum(1 for se in A.sessions if se.res >= se.close)}
    for ti, name in enumerate(TAUS):
        a = A.atr_now(ti)
        out["atr"][name] = round(a, 4) if a == a else None
    req = set(A.req_taus)
    rows: list[dict[str, Any]] = []
    for lv in A.levels:
        st = lv.state_at(n)
        if st is None:
            continue
        if lv.fam in ("P", "D", "R"):
            if lv.ti != DISPLAY_PDR_SCALE or lv.fam not in fams:
                continue
        elif lv.ti not in req or lv.fam not in fams:
            continue
        rows.append(_level_row(A, lv, st, False))
    if "F" in fams:
        for lv in A.fib05:
            st = lv.state_at(n)
            if st is not None and lv.ti in req:
                rows.append(_level_row(A, lv, st, True))
    if not A.in_session and fams & {"P", "D", "R"}:
        for lv in A.pending_pdr():
            if lv.fam in fams:
                row = _level_row(A, lv, "pending", False)
                row["known_from"] = A.next_open_s
                rows.append(row)
        out["next_session_open"] = A.next_open_s
    rows.sort(key=lambda r: (-r["price"], r["id"]))
    out["levels"] = rows
    out["groups"] = _groups(A, rows)
    if with_setups:
        out["setups"] = _setup_rows(A, include_all_setups)
    return out


def _level_row(A: Analysis, lv: Level, st: str, fib05: bool) -> dict[str, Any]:
    pdr = lv.fam in ("P", "D", "R")
    row: dict[str, Any] = {
        "id": lv.uid,
        "family": lv.fam,
        "tf": "1D" if pdr else TAUS[lv.ti],
        "kind": "fib_0.5" if fib05 else _kind_name(lv),
        "lo": round(lv.lo, 4),
        "hi": round(lv.hi, 4),
        "price": round(0.5 * (lv.lo + lv.hi), 4),
        "known_from": A.known_from_s(lv),
        "state": st,
        "label": _label(lv, fib05),
        "role": "resistance" if lv.side == -1 else ("support" if lv.side == 1 else "inside"),
        "group": None,
    }
    if pdr:
        row["scale"] = TAUS[lv.ti]
    if lv.fam == "Z":
        row["touches"] = lv.touches
    if lv.leg >= 0:
        row["leg"] = lv.leg
    if st == "broken":
        row["broken_at"] = A.t1[lv.brkc - 1] if 0 < lv.brkc <= A.n else None
    return row


def merge_labels(labels: list[str]) -> str:
    """One title for a confluence group, highest timeframe first:
    ["PWL", "4H SwL", "1H SwL", "30m .618"] -> "PWL · 4H/1H SwL · 30m .618".
    A leg's 0.5 is both "<tf> .5" (fib set) and "<tf> mid" (M(a)); the "mid" twin is dropped."""
    parts: dict[str, list[str]] = {}
    order: list[str] = []
    for lab in labels:
        tf, _, rest = lab.partition(" ")
        key, tfs = (rest, [tf]) if tf in TAUS and rest else (lab, [])
        if key not in parts:
            parts[key] = []
            order.append(key)
        for x in tfs:
            if x not in parts[key]:
                parts[key].append(x)
    if "mid" in parts and ".5" in parts:
        parts["mid"] = [x for x in parts["mid"] if x not in parts[".5"]]
        if not parts["mid"]:
            order.remove("mid")
    return " · ".join(f"{'/'.join(parts[k])} {k}" if parts[k] else k for k in order)


def _groups(A: Analysis, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    a5 = A.atr_now(TI_5M)
    if a5 != a5:
        a30 = A.atr_now(TI_30M)
        a5 = a30 * math.sqrt(5.0 / 30.0) if a30 == a30 else 0.0
    radius = 0.25 * a5
    pts = sorted((r for r in rows if r["lo"] == r["hi"]), key=lambda r: (r["price"], r["id"]))
    groups: list[list[dict[str, Any]]] = []
    for r in pts:
        if groups and r["price"] - groups[-1][0]["price"] <= radius + 1e-12:
            groups[-1].append(r)
        else:
            groups.append([r])
    out = []
    for gi, members in enumerate(groups):
        gid = f"g{gi}"
        for r in members:
            r["group"] = gid

        def rank(r: dict[str, Any]) -> tuple:
            return (-TAUS.index(r["tf"]), _FAM_PRIO.get(r["family"], 9), r["id"])

        ordered = sorted(members, key=rank)
        dom = ordered[0]
        fam_tau = {(r["family"], r["tf"], r.get("scale", "")) for r in members}
        out.append({
            "id": gid, "price": dom["price"], "tf": dom["tf"], "family": dom["family"],
            "kind": dom["kind"], "label": merge_labels([r["label"] for r in ordered]),
            "state": ("active" if any(r["state"] == "active" for r in members)
                      else "broken" if any(r["state"] == "broken" for r in members) else "pending"),
            "members": [r["id"] for r in ordered], "confluence": len(fam_tau),
        })
    return out


def _setup_rows(A: Analysis, include_all: bool) -> list[dict[str, Any]]:
    n = A.n
    out = []
    for st in A.setups:
        if not st.qual:
            if include_all:
                out.append(_setup_row(A, st))
            continue
        resolved_at = A.t1[st.rbar] if st.rbar < n else None
        recent = resolved_at is not None and resolved_at >= A.asof - RECENT_S
        if st.status in ("waiting", "filled") or include_all or recent:
            row = _setup_row(A, st)
            row["recent"] = bool(recent and st.status not in ("waiting", "filled"))
            out.append(row)
    out.sort(key=lambda r: (r["status"] not in ("waiting", "filled"), -(r["qualified_at"] or 0)))
    return out


def _setup_row(A: Analysis, st: Setup) -> dict[str, Any]:
    n = A.n
    lv = st.level
    X = st.Xf if st.fb < _BIG else (st.X if st.qual else _NAN)
    M = st.entry if st.fb < _BIG else (X - 0.5 * (X - st.O) if X == X else _NAN)
    deeper = {}
    if X == X:
        for f in DEEP_RATIOS:
            deeper[f"{f}"] = round(X - f * (X - st.O), 4)
    resolved_at = A.t1[st.rbar] if st.rbar < n else None
    return {
        "id": f"{TAUS[st.ti]}-{st.d}-{A.t0[st.Ob] if st.Ob < n else 0}",
        "tf": TAUS[st.ti],
        "origin": ORIGINS[st.otype],
        "direction": "long" if st.d == 1 else "short",
        "level_id": lv.uid if lv is not None else None,
        "level_label": _label(lv) if lv is not None else None,
        "O": round(st.O, 4),
        "X": round(X, 4) if X == X else None,
        "M": round(M, 4) if M == M else None,
        "deeper": deeper,
        "status": st.status,
        "ambiguous": st.code == AMB,
        "unresolved": st.code == UNRES and st.fb < _BIG,
        "R": round(st.R, 4) if st.R == st.R else None,
        "origins": st.conf,
        "origin_at": A.t1[st.t0] if st.t0 < n else None,
        "qualified_at": A.t1[st.tq] if st.qual and st.tq < n else None,
        "filled_at": A.t1[st.fb] if st.fb < n else None,
        "resolved_at": resolved_at,
    }
