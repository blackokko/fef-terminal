"""Live ThetaData flow ingest — builds the VALIDATED `option_flow_minute`
construction in real time into the new `theta_flow_minute` table and exposes
the session drift series (`drift_theta_cum_m`) for the scanner's shadow
comparison and the (default-OFF) `flow_source='theta'` swap.

WHY (2026-07-15 parity diagnostic, scratchpad/parity_report.md): the live
QuantData `drift_cum_net_m` (feed removed 2026-08-25) moved at ~7-9% of the
magnitude of the backtest's
`option_flow_minute`-derived drift on the same sessions (31x fewer >=10M 3-min
surges). The strict-HIGH backtest was measured on Theta-scale inputs; the live
scanner can neither harvest nor validate that edge through the QD feed. The
fix direction adopted here is parity option 1: construct the live drift the
way the backtest does.

CONSTRUCTION CONTRACT — replicated TO THE LETTER from the scripts that built
the validated table (do NOT "improve" any of this without re-running the
proof-of-construction gate):

  * trades:  GET /v3/option/history/trade  symbol=SPXW expiration=<date>
             date=<date> right=both strike_range=20 format=ndjson
             (scripts/maintenance/ingest_thetadata_flow.py:29-31,51-55)
  * quotes:  GET /v3/option/history/quote  interval=1m strike_range=20
             (scripts/maintenance/ingest_thetadata_quotes.py:36-41,72-81) —
             the NBBO the classifier marks against is the 1-MINUTE snapshot
             band (ATM+/-20 strikes), NOT per-trade prevailing quotes.
  * scope:   SPXW root, 0DTE only (expiration == session date). Trades
             outside the +/-20-strike quote band classify to 0 and contribute
             nothing (ingest_thetadata_flow.py:96-98 quote-miss -> sign 0).
  * timestamps: ThetaData returns NAIVE ET wall-clock ISO strings; parse as
             America/New_York (DST-correct) -> UTC ms
             (ingest_thetadata_flow.py:58-62, ingest_thetadata_quotes.py:43-64).
  * aggressor rule (ingest_thetadata_flow.py:65-74): with the minute NBBO at
             or before the trade ts: price >= ask-1e-9 -> +1 (buy / lifted);
             price <= bid+1e-9 -> -1 (sell / hit); else strictly above mid
             -> +1, strictly below mid -> -1, exactly mid -> 0; missing quote
             or ask <= bid -> 0.
  * premium: price * size * 100 (ingest_thetadata_flow.py:99).
  * minute bucket: (trade_utc_ms // 60000) * 60000; a bucket is created for
             EVERY parsed trade, even sign-0 (ingest_thetadata_flow.py:95,100).
  * net_signed_prem = (call_buy - call_sell) - (put_buy - put_sell); bullish
             > 0 (ingest_thetadata_flow.py:114).
  * quote lookup: minute_at_or_before over the GLOBAL sorted quote-minute
             list, then exact (minute, strike, right) key; a missing key at
             that minute -> no quote -> sign 0 (backtest.data.Session.quote /
             minute_at_or_before, scripts/research/backtest/data.py:50-51,90-96).

DRIFT SERIES: fresh per-session cumulative sum of net_signed_prem / 1e6 over
the completed minutes, starting at ZERO at the first session minute (09:30 ET)
— the strict replay's construction (scratchpad strict_replay_producer.py
run_session lines 295-303, 318). This construction inherently has NO premarket
carry and therefore no open-reset artifact: SPXW 0DTE prints only exist in
RTH, so the cumsum is structurally 0 at 09:30.

LIVE MECHANICS (additive; zero engine-behavior change):
  * supervised loop (vix_daily_ingest conventions): watchdog heartbeat, kill
    switch FEF_THETA_FLOW_ENABLED (default true), never raises, audit rows.
  * incremental polling every FEF_THETA_FLOW_POLL_SEC (default 25s) during
    RTH using start_time cursors (verified live 2026-07-15: same-day pulls
    ~2.8s, freshness ~1s; the 1m quote row for minute M lands seconds after
    M:00 so trades of minute M classify against quote-minute M exactly like
    the historical construction).
  * a minute is FINALIZED (classified + persisted + added to the drift
    series) once it is complete AND its quote minute has been seen (or a
    grace period expires) — the drift head therefore lags ~1-2 minutes, the
    same information timing as the backtest's completed-minute spine.
  * restart mid-session: full-session rebuild from the day's tape (one
    chain-band pull, exact); the SAME rebuild path is the proof-of-
    construction harness and the EOD self-check.
  * EOD settle (>= ~16:06 ET; the SPXW tape ends 16:00, i.e. 15:00 CT cash /
    15:15 CT SPXW close handled by construction — no prints, no rows): force-
    finalize, then rebuild the whole session from the tape and RECONCILE the
    persisted rows against the authoritative full-day construction (late
    prints / band drift), audit category 'theta_flow'.
  * fail-SOFT: Terminal down -> holder marked stale, ONE log per outage,
    engine unaffected (nothing in the trade path reads this unless
    flow_source='theta', and that path falls back to QD input).
"""

from __future__ import annotations

import asyncio
import bisect
import datetime as dt
import json
import logging
import os
import time as _time
from typing import Any, AsyncIterator, Iterable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Construction constants (Phase-A citations in the module docstring)
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("FEF_THETA_BASE_URL", "http://127.0.0.1:25503/v3")
TRADE_URL = f"{BASE_URL}/option/history/trade"
QUOTE_URL = f"{BASE_URL}/option/history/quote"
SYMBOL = "SPXW"            # ingest_thetadata_flow.py:30
STRIKE_RANGE = 20          # ingest_thetadata_flow.py:31 / quotes.py:40
QUOTE_INTERVAL = "1m"      # ingest_thetadata_quotes.py:38

ET = ZoneInfo("America/New_York")

POLL_SEC_DEFAULT = 25.0
IDLE_SEC = 30.0
# A minute [M, M+60s) is eligible to finalize once now >= M + 60s + LAG.
FINALIZE_LAG_MS = 15_000
# ... but we additionally wait for quote-minute >= M (exact classification
# parity with a post-hoc rebuild) up to this grace past the minute end.
QUOTE_GRACE_MS = 120_000
# Incremental trade pulls re-cover this much tape behind the cursor; the
# seen-key set dedupes the overlap.
TRADE_OVERLAP_MS = 60_000
RTH_START_MIN = 9 * 60 + 30    # 09:30 ET
POLL_START_MIN = 9 * 60 + 28   # start polling slightly early (empty pulls ok)
POLL_END_MIN = 16 * 60 + 8     # SPXW prints end 16:00 ET; small margin
SETTLE_AFTER_MIN = 16 * 60 + 6


def _enabled() -> bool:
    return os.environ.get("FEF_THETA_FLOW_ENABLED", "true").lower() in (
        "true", "1", "yes", "on",
    )


# ---------------------------------------------------------------------------
# Pure construction functions (fixture-tested, no network)
# ---------------------------------------------------------------------------


def classify(price: float, bid: float | None, ask: float | None) -> int:
    """+1 aggressive buy (lifted ask), -1 aggressive sell (hit bid), 0 neutral.

    VERBATIM ingest_thetadata_flow.py:65-74 — the rule the validated table
    was built with, including the 1e-9 epsilons and the ask<=bid -> 0 guard.
    """
    if bid is None or ask is None or ask <= bid:
        return 0
    mid = (bid + ask) / 2
    if price >= ask - 1e-9:
        return 1
    if price <= bid + 1e-9:
        return -1
    return 1 if price > mid else (-1 if price < mid else 0)


def et_iso_to_utc_ms(ts_iso: str) -> int:
    """Naive ET wall-clock ISO string -> UTC epoch ms (DST-correct).

    VERBATIM semantics of ingest_thetadata_flow.py:58-62 (_utc_ms).
    """
    naive = dt.datetime.fromisoformat(ts_iso)
    if naive.tzinfo is None:
        naive = naive.replace(tzinfo=ET)
    return int(naive.timestamp() * 1000)


_RIGHT_MAP = {"CALL": "C", "PUT": "P"}  # ingest_thetadata_flow.py:82


def parse_trade_obj(o: dict) -> tuple[int, float, str, float, int, tuple] | None:
    """One ndjson trade object -> (utc_ms, strike, right, price, size, disc).

    Field handling mirrors ingest_thetadata_flow.py:88-94 (malformed rows
    are silently dropped, exactly like the original `except: continue`).

    disc = (sequence, exchange, condition) — the tape's own print
    discriminator, used ONLY for incremental-overlap dedupe. The original
    construction had NO dedupe (it processed every line of one full-day
    pull); a coarser key silently merged distinct prints — sweep components
    routinely land at the SAME ms/strike/right/price/size on different
    exchanges/sequences (measured 13.2% of a real session tape,
    proof-of-construction root cause 2026-07-15). Zero collisions measured
    with the full discriminator.
    """
    try:
        price = float(o["price"])
        size = int(o["size"])
        strike = float(o["strike"])
        right = _RIGHT_MAP.get(o.get("right"), o.get("right"))
        tms = et_iso_to_utc_ms(o["timestamp"])
    except Exception:
        return None
    if right not in ("C", "P"):
        return None
    disc = (o.get("sequence"), o.get("exchange"), o.get("condition"))
    return (tms, strike, right, price, size, disc)


def parse_quote_obj(o: dict) -> tuple[int, float, str, float, float] | None:
    """One ndjson quote object -> (utc_ms, strike, right, bid, ask).

    Mirrors ingest_thetadata_quotes.py:175-198 build_rows: bid/ask default
    0.0, right normalized by first letter, naive-ET timestamp -> UTC ms.
    (quote_quality is NOT part of the flow construction — the classifier
    consumed raw bid/ask: ingest_thetadata_flow.py:97-98.)
    """
    try:
        bid = float(o.get("bid", 0) or 0)
        ask = float(o.get("ask", 0) or 0)
        strike = o.get("strike")
        right = o.get("right")
        ts = o.get("timestamp")
        if strike is None or right is None or ts is None:
            return None
        r = str(right).upper()
        r = "C" if r.startswith("C") else ("P" if r.startswith("P") else r)
        if r not in ("C", "P"):
            return None
        return (et_iso_to_utc_ms(str(ts)), float(strike), r, bid, ask)
    except Exception:
        return None


class QuoteBook:
    """Session NBBO store replicating backtest.data.Session lookup semantics:
    a GLOBAL sorted minute list + exact (minute, strike, right) keys
    (scripts/research/backtest/data.py:30-51, 90-96)."""

    def __init__(self) -> None:
        self.q: dict[tuple[int, float, str], tuple[float, float]] = {}
        self._minutes: list[int] = []
        self._minute_set: set[int] = set()

    def add(self, ts_ms: int, strike: float, right: str,
            bid: float, ask: float) -> None:
        self.q[(ts_ms, strike, right)] = (bid, ask)
        if ts_ms not in self._minute_set:
            self._minute_set.add(ts_ms)
            bisect.insort(self._minutes, ts_ms)

    def minute_at_or_before(self, ts: int) -> int | None:
        i = bisect.bisect_right(self._minutes, ts)
        return self._minutes[i - 1] if i else None

    def max_minute(self) -> int | None:
        return self._minutes[-1] if self._minutes else None

    def quote(self, minute: int, strike: float, right: str) -> tuple[float, float] | None:
        return self.q.get((minute, strike, right))

    def __len__(self) -> int:
        return len(self.q)


class ThetaFlowSessionBuilder:
    """One session's live construction state: quote book, raw trade buffers,
    finalized per-minute rows and the session drift cumsum.

    The SAME object/path serves live incremental polling, boot mid-session
    rebuild, the EOD self-check and the proof-of-construction harness — feed
    it parsed rows in any chunking; finalized output is identical (the unit
    suite asserts chunking-invariance).
    """

    def __init__(self, session_date: str) -> None:
        self.session_date = session_date          # 'YYYY-MM-DD' (ET)
        self.quotes = QuoteBook()
        # minute_ms -> list of (tms, strike, right, price, size), pre-finalize
        self._raw: dict[int, list[tuple[int, float, str, float, int]]] = {}
        # dedupe keys for the incremental overlap window
        self._seen: set[tuple] = set()
        self.trade_cursor_ms: int = 0             # max trade ts ingested
        # finalized rows: minute_ms -> tuple mirroring option_flow_minute
        self.rows: dict[int, tuple] = {}
        self._final_minutes: list[int] = []       # sorted finalized minutes
        self._cum: list[float] = []               # cumsum net_signed_prem ($)
        self.trades_ingested = 0
        self.trades_deduped = 0

    # -- ingest ------------------------------------------------------------

    def ingest_quotes(self, parsed: Iterable[tuple]) -> int:
        n = 0
        for ts_ms, strike, right, bid, ask in parsed:
            self.quotes.add(ts_ms, strike, right, bid, ask)
            n += 1
        return n

    def ingest_trades(self, parsed: Iterable[tuple]) -> int:
        """Buffer parsed trades for later finalization.

        Dedupe (incremental-overlap protection) keys on the FULL tuple
        including the tape discriminator — distinct prints sharing
        ms/strike/right/price/size (sweep components across exchanges) must
        all count, exactly as the original no-dedupe full-day construction
        counted them."""
        n = 0
        for t in parsed:
            tms = t[0]
            key = t
            if key in self._seen:
                self.trades_deduped += 1
                continue
            self._seen.add(key)
            minute = (tms // 60000) * 60000        # ingest_thetadata_flow.py:95
            if minute in self.rows:
                # Late print behind an already-finalized minute — cannot be
                # applied incrementally; the EOD reconcile pass repairs it.
                self.trades_deduped += 1
                continue
            self._raw.setdefault(minute, []).append(t)
            if tms > self.trade_cursor_ms:
                self.trade_cursor_ms = tms
            n += 1
            self.trades_ingested += 1
        return n

    # -- finalize ----------------------------------------------------------

    def _aggregate_minute(self, minute: int,
                          trades: list[tuple]) -> tuple:
        """Classify + aggregate one minute — VERBATIM construction of
        ingest_thetadata_flow.py aggregate() lines 95-116."""
        b = dict(cbp=0.0, csp=0.0, pbp=0.0, psp=0.0, cbv=0, csv=0, pbv=0, psv=0)
        for tms, strike, right, price, size, *_ in trades:
            qmin = self.quotes.minute_at_or_before(tms)
            q = self.quotes.quote(qmin, strike, right) if qmin is not None else None
            sign = classify(price, q[0], q[1]) if q else 0
            prem = price * size * 100
            if right == "C":
                if sign > 0:
                    b["cbp"] += prem; b["cbv"] += size
                elif sign < 0:
                    b["csp"] += prem; b["csv"] += size
            elif right == "P":
                if sign > 0:
                    b["pbp"] += prem; b["pbv"] += size
                elif sign < 0:
                    b["psp"] += prem; b["psv"] += size
        net = (b["cbp"] - b["csp"]) - (b["pbp"] - b["psp"])  # bullish > 0
        return (self.session_date, minute,
                b["cbp"], b["csp"], b["pbp"], b["psp"],
                b["cbv"], b["csv"], b["pbv"], b["psv"], net)

    def finalize(self, now_ms: int, *, force: bool = False) -> list[tuple]:
        """Finalize every eligible raw minute; return the NEW rows (sorted).

        Eligible: minute complete (now >= M+60s+lag) AND (a quote minute >= M
        exists, OR the quote grace expired, OR force). force=True is the
        historical/rebuild path where the tape is complete.
        """
        new: list[tuple] = []
        qmax = self.quotes.max_minute()
        for minute in sorted(self._raw.keys()):
            if not force:
                if now_ms < minute + 60_000 + FINALIZE_LAG_MS:
                    continue
                quote_ready = qmax is not None and qmax >= minute
                if not quote_ready and now_ms < minute + 60_000 + QUOTE_GRACE_MS:
                    continue
            row = self._aggregate_minute(minute, self._raw.pop(minute))
            self.rows[minute] = row
            i = bisect.bisect_left(self._final_minutes, minute)
            self._final_minutes.insert(i, minute)
            prev = self._cum[i - 1] if i > 0 else 0.0
            delta_net = row[10]
            self._cum.insert(i, prev + delta_net)
            # A minute can only insert out-of-order on rebuild edge cases;
            # re-accumulate the tail to keep the cumsum exact.
            for j in range(i + 1, len(self._cum)):
                self._cum[j] = self._cum[j - 1] + self.rows[self._final_minutes[j]][10]
            new.append(row)
        # Prune the dedupe set to the overlap window behind the cursor —
        # bounded memory over a full session.
        if self._seen and self.trade_cursor_ms:
            floor = self.trade_cursor_ms - (TRADE_OVERLAP_MS + QUOTE_GRACE_MS)
            if len(self._seen) > 50_000:
                self._seen = {k for k in self._seen if k[0] >= floor}
        return new

    # -- drift -------------------------------------------------------------

    def drift_cum_net_m(self, at_ms: int | None = None) -> float | None:
        """Session cumsum of net_signed_prem / 1e6 through the last finalized
        minute <= at_ms (strict replay construction: fresh per-session cumsum
        on the 1-min flow spine, 0 at the open — strict_replay_producer.py
        run_session lines 295-303/318, 337-338). None before the first
        finalized minute."""
        if not self._final_minutes:
            return None
        if at_ms is None:
            return self._cum[-1] / 1e6
        i = bisect.bisect_right(self._final_minutes, at_ms)
        if i == 0:
            return None
        return self._cum[i - 1] / 1e6

    def drift_d3_m(self) -> float | None:
        """3-minute drift delta at the series head (evidence stream for the
        flow_shadow comparison; mirrors the parity diagnostic's d3)."""
        if not self._final_minutes:
            return None
        head = self._final_minutes[-1]
        cur = self._cum[-1] / 1e6
        prev = self.drift_cum_net_m(head - 180_000)
        if prev is None:
            prev = 0.0   # within the first 3 session minutes: delta from 0
        return cur - prev

    def drift_bias(self, at_ms: int | None = None) -> str | None:
        """Bias from the theta call/put legs — the EXACT strict-replay
        ``_drift_bias``: cum_call = Σ(call_buy−call_sell), cum_put =
        Σ(put_buy−put_sell) over finalized minutes ≤ at_ms; BULLISH/BEARISH
        when the net exceeds 15% of the gross (|cum_call|+|cum_put|), else
        NEUTRAL. None before the first finalized minute. This is the validated
        construction, so the live bias-demotion input equals the backtest's
        (2026-07-17 parity fix: the QD snapshot bias contradicted the theta
        drift and wrongly demoted every aligned HIGH call that session)."""
        if not self._final_minutes:
            return None
        if at_ms is None:
            upto = len(self._final_minutes)
        else:
            upto = bisect.bisect_right(self._final_minutes, at_ms)
            if upto == 0:
                return None
        cum_call = cum_put = 0.0
        for k in range(upto):
            row = self.rows[self._final_minutes[k]]
            cum_call += row[2] - row[3]   # call_buy_prem − call_sell_prem
            cum_put += row[4] - row[5]    # put_buy_prem − put_sell_prem
        total_net = cum_call - cum_put
        total_abs = abs(cum_call) + abs(cum_put)
        if total_abs <= 0:
            return "NEUTRAL"
        if total_net > 0.15 * total_abs:
            return "BULLISH"
        if total_net < -0.15 * total_abs:
            return "BEARISH"
        return "NEUTRAL"

    def last_final_minute(self) -> int | None:
        return self._final_minutes[-1] if self._final_minutes else None

    def raw_pending(self) -> int:
        return sum(len(v) for v in self._raw.values())


# ---------------------------------------------------------------------------
# Terminal I/O (streaming ndjson; bounded memory)
# ---------------------------------------------------------------------------


async def _stream_ndjson(client: Any, url: str, params: dict) -> AsyncIterator[dict]:
    async with client.stream("GET", url, params=params) as r:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} from {url}")
        async for line in r.aiter_lines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if isinstance(o, dict):
                yield o


def _yyyymmdd(session_date: str) -> str:
    return session_date.replace("-", "")


def _et_hhmmss(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, ET).strftime("%H:%M:%S")


# Ingest in chunks of this many parsed rows, yielding to the event loop
# between chunks — a full-day tape is order 10^6 prints and a single
# synchronous ingest would stall the shared loop (adversarial review MAJOR-1)
# and hold the whole batch in memory at once (MINOR-9).
_INGEST_CHUNK = 20_000


async def pull_quotes(client: Any, builder: ThetaFlowSessionBuilder,
                      start_time: str | None = None) -> int:
    d = _yyyymmdd(builder.session_date)
    params = {"symbol": SYMBOL, "expiration": d, "date": d, "right": "both",
              "interval": QUOTE_INTERVAL, "strike_range": str(STRIKE_RANGE),
              "format": "ndjson"}
    if start_time:
        params["start_time"] = start_time
    n = 0
    batch: list[tuple] = []
    async for o in _stream_ndjson(client, QUOTE_URL, params):
        p = parse_quote_obj(o)
        if p is not None:
            batch.append(p)
            n += 1
        if len(batch) >= _INGEST_CHUNK:
            builder.ingest_quotes(batch)
            batch = []
            await asyncio.sleep(0)
    builder.ingest_quotes(batch)
    return n


async def pull_trades(client: Any, builder: ThetaFlowSessionBuilder,
                      start_time: str | None = None) -> int:
    d = _yyyymmdd(builder.session_date)
    params = {"symbol": SYMBOL, "expiration": d, "date": d, "right": "both",
              "strike_range": str(STRIKE_RANGE), "format": "ndjson"}
    if start_time:
        params["start_time"] = start_time
    n = 0
    batch: list[tuple] = []
    async for o in _stream_ndjson(client, TRADE_URL, params):
        p = parse_trade_obj(o)
        if p is not None:
            batch.append(p)
        if len(batch) >= _INGEST_CHUNK:
            n += builder.ingest_trades(batch)
            batch = []
            await asyncio.sleep(0)
    n += builder.ingest_trades(batch)
    return n


async def rebuild_session(client: Any, session_date: str, *,
                          finalize_now_ms: int | None = None,
                          force: bool = False) -> ThetaFlowSessionBuilder:
    """Full-session rebuild from the day's tape — THE code path shared by
    boot-mid-session recovery, the EOD self-check and the proof-of-
    construction harness. One chain-band quote pull + one trade pull through
    the exact live parse/classify/aggregate functions."""
    builder = ThetaFlowSessionBuilder(session_date)
    await pull_quotes(client, builder)
    await pull_trades(client, builder)
    # The full-day finalize classifies order 10^6 prints — run it off the
    # event loop (the builder is loop-owned and untouched while awaited;
    # adversarial review MAJOR-1).
    await asyncio.to_thread(
        builder.finalize,
        int(_time.time() * 1000) if finalize_now_ms is None else finalize_now_ms,
        force=force,
    )
    return builder


# ---------------------------------------------------------------------------
# Persistence (idempotent; unique (session_date, ts_ms) index via init_db)
# ---------------------------------------------------------------------------


async def persist_rows(rows: list[tuple]) -> int:
    """Insert finalized minute rows; swallow duplicate-key conflicts (same
    insert-and-swallow-IntegrityError pattern as loop._persist_rows_idempotent).
    """
    if not rows:
        return 0
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import ThetaFlowMinute

    # Pre-filter minutes already persisted (one indexed read) — a boot
    # rebuild re-produces the whole session and a naive bulk insert would
    # hit the unique index and degrade to ~390 per-row rollback transactions
    # (adversarial review MAJOR-1 amplifier).
    try:
        dates = {r[0] for r in rows}
        async with get_session() as session:
            existing = {
                (e.session_date, e.ts_ms) for e in (await session.exec(
                    select(ThetaFlowMinute)
                    .where(ThetaFlowMinute.session_date.in_(dates))  # type: ignore[attr-defined]
                )).all()
            }
        rows = [r for r in rows if (r[0], int(r[1])) not in existing]
        if not rows:
            return 0
    except Exception:
        logger.exception("theta_flow: persist pre-filter failed — inserting raw")

    def _model(r: tuple) -> ThetaFlowMinute:
        return ThetaFlowMinute(
            session_date=r[0], ts_ms=int(r[1]),
            call_buy_prem=r[2], call_sell_prem=r[3],
            put_buy_prem=r[4], put_sell_prem=r[5],
            call_buy_vol=int(r[6]), call_sell_vol=int(r[7]),
            put_buy_vol=int(r[8]), put_sell_vol=int(r[9]),
            net_signed_prem=r[10],
        )

    try:
        async with get_session() as session:
            session.add_all([_model(r) for r in rows])
            await session.flush()
        return len(rows)
    except IntegrityError:
        pass
    except Exception:
        logger.exception("theta_flow: bulk persist failed; trying per-row")
    n = 0
    for r in rows:
        try:
            async with get_session() as session:
                session.add(_model(r))
                await session.flush()
            n += 1
        except IntegrityError:
            continue
        except Exception:
            logger.exception("theta_flow: per-row persist failed (%s)", r[1])
    return n


async def reconcile_session_rows(session_date: str,
                                 authoritative: dict[int, tuple]) -> dict:
    """EOD reconcile: make the persisted session rows EXACTLY equal the
    authoritative full-day rebuild (late prints / intraday ATM-band drift can
    leave provisional intraday rows slightly off; the stored table must equal
    the validated full-tape construction). Returns summary counts."""
    from sqlmodel import select
    from fef_sidecar.db.engine import get_session
    from fef_sidecar.db.models import ThetaFlowMinute

    changed = added = removed = 0
    async with get_session() as session:
        existing = {
            r.ts_ms: r for r in (await session.exec(
                select(ThetaFlowMinute)
                .where(ThetaFlowMinute.session_date == session_date)
            )).all()
        }
        # Rows the authoritative tape does NOT contain (e.g. a provisional
        # minute built from later-busted prints) must be REMOVED — the table
        # contract is exact equality with the full-tape construction
        # (adversarial review MINOR-6).
        for ts, cur in existing.items():
            if ts not in authoritative:
                await session.delete(cur)
                removed += 1
        for minute, row in sorted(authoritative.items()):
            cur = existing.get(minute)
            vals = dict(
                call_buy_prem=row[2], call_sell_prem=row[3],
                put_buy_prem=row[4], put_sell_prem=row[5],
                call_buy_vol=int(row[6]), call_sell_vol=int(row[7]),
                put_buy_vol=int(row[8]), put_sell_vol=int(row[9]),
                net_signed_prem=row[10],
            )
            if cur is None:
                session.add(ThetaFlowMinute(
                    session_date=session_date, ts_ms=minute, **vals))
                added += 1
                continue
            if any(abs(getattr(cur, k) - v) > 1e-6 for k, v in vals.items()):
                for k, v in vals.items():
                    setattr(cur, k, v)
                session.add(cur)
                changed += 1
    return {"added": added, "changed": changed, "removed": removed,
            "total": len(authoritative)}


# ---------------------------------------------------------------------------
# State holder + supervised loop
# ---------------------------------------------------------------------------


def _update_holder(state: Any, builder: ThetaFlowSessionBuilder | None, *,
                   stale: bool, note: str = "") -> None:
    """Publish the live drift read for consumers (scanner shadow row, the
    flow_source='theta' swap, GET /theta_flow/status). Plain dict; asyncio
    single-threaded so no locking."""
    now_ms = int(_time.time() * 1000)
    holder = {
        "enabled": _enabled(),
        "stale": stale,
        "note": note,
        "updated_ms": now_ms,
        "session_date": builder.session_date if builder else None,
        "drift_theta_cum_m": builder.drift_cum_net_m() if builder else None,
        "drift_theta_d3_m": builder.drift_d3_m() if builder else None,
        "drift_theta_bias": builder.drift_bias() if builder else None,
        "last_final_minute_ms": builder.last_final_minute() if builder else None,
        "minutes_finalized": len(builder.rows) if builder else 0,
        "trades_ingested": builder.trades_ingested if builder else 0,
        "raw_pending": builder.raw_pending() if builder else 0,
        "quote_rows": len(builder.quotes) if builder else 0,
    }
    try:
        state.theta_flow = holder
    except Exception:
        pass


def theta_drift_read(state: Any, *, max_age_min: float = 8.0) -> tuple[float | None, bool]:
    """(drift_theta_cum_m, fresh) for consumers. fresh=False when the ingest
    is disabled/stale/absent or the last finalized minute is older than
    max_age_min — callers must then fall back to the QD input (fail-soft)."""
    holder = getattr(state, "theta_flow", None)
    if not holder or not isinstance(holder, dict):
        return None, False
    drift = holder.get("drift_theta_cum_m")
    if drift is None or holder.get("stale"):
        return drift, False
    last_min = holder.get("last_final_minute_ms") or 0
    age_min = (int(_time.time() * 1000) - last_min) / 60000.0
    return drift, age_min <= max_age_min


def theta_bias_read(state: Any, *, max_age_min: float = 8.0) -> tuple[str | None, bool]:
    """(drift_theta_bias, fresh) — the validated call/put-leg bias for the
    demotion/veto layer, gated on the SAME freshness rule as theta_drift_read
    so the live bias input equals the backtest's construction when fresh, and
    callers fall back to the QD snapshot bias otherwise (fail-soft). Freshness
    is independent of the drift read so a caller can accept one and not the
    other, but in practice both come from the same finalized minute."""
    holder = getattr(state, "theta_flow", None)
    if not holder or not isinstance(holder, dict):
        return None, False
    bias = holder.get("drift_theta_bias")
    if bias is None or holder.get("stale"):
        return bias, False
    last_min = holder.get("last_final_minute_ms") or 0
    age_min = (int(_time.time() * 1000) - last_min) / 60000.0
    return bias, age_min <= max_age_min


def _now_et() -> dt.datetime:
    return dt.datetime.now(ET)


def _minute_of_day(d: dt.datetime) -> int:
    return d.hour * 60 + d.minute


async def _audit(summary: str, details: dict | None = None) -> None:
    try:
        from fef_sidecar.db.repository import SignalIterRepository
        await SignalIterRepository.write_audit_log(
            category="theta_flow", actor="system",
            summary=summary, details=details or {},
        )
    except Exception:
        logger.exception("theta_flow: audit write failed")


async def _poll_cycle(client: Any, builder: ThetaFlowSessionBuilder) -> list[tuple]:
    """One incremental poll: quotes (from last quote minute), trades (cursor
    minus overlap), finalize. Returns newly finalized rows."""
    qmax = builder.quotes.max_minute()
    q_start = _et_hhmmss(qmax) if qmax else None
    await pull_quotes(client, builder, start_time=q_start)
    t_start = None
    if builder.trade_cursor_ms:
        t_start = _et_hhmmss(builder.trade_cursor_ms - TRADE_OVERLAP_MS)
    await pull_trades(client, builder, start_time=t_start)
    return builder.finalize(int(_time.time() * 1000))


async def _settle_session(client: Any, builder: ThetaFlowSessionBuilder) -> bool:
    """EOD: force-finalize the remainder, then rebuild the full session from
    the tape through the same code path and reconcile the persisted rows to
    the authoritative construction. Audit row carries the self-check stats —
    the per-session construction evidence for the swap decision.

    NEVER raises; returns True on full success. A False from the reconcile
    stage is retried by the caller a bounded number of times (adversarial
    review MINOR-7: an unbounded 30s retry would hammer the Terminal with
    full-day rebuilds until midnight)."""
    try:
        rows = await asyncio.to_thread(
            builder.finalize, int(_time.time() * 1000), force=True)
        await persist_rows(rows)
    except Exception:
        logger.exception("theta_flow: EOD force-finalize/persist failed")
        return False
    try:
        authoritative = await rebuild_session(
            client, builder.session_date, force=True)
    except Exception:
        logger.exception("theta_flow: EOD rebuild failed — reconcile skipped")
        await _audit(
            f"theta_flow EOD settle {builder.session_date}: rebuild FAILED, "
            f"reconcile skipped (intraday rows stand)",
            {"session_date": builder.session_date, "eod_rebuild": "failed"},
        )
        return False
    try:
        # Compare intraday (incremental) construction vs authoritative tape.
        live_rows, auth_rows = builder.rows, authoritative.rows
        common = sorted(set(live_rows) & set(auth_rows))
        mismatched = [m for m in common
                      if abs(live_rows[m][10] - auth_rows[m][10]) > 1e-6]
        summary_stats = {
            "session_date": builder.session_date,
            "live_minutes": len(live_rows),
            "auth_minutes": len(auth_rows),
            "net_mismatched_minutes": len(mismatched),
            "live_eod_drift_m": builder.drift_cum_net_m(),
            "auth_eod_drift_m": authoritative.drift_cum_net_m(),
        }
        recon = await reconcile_session_rows(builder.session_date, auth_rows)
        summary_stats.update(recon)
        await _audit(
            f"theta_flow EOD settle {builder.session_date}: "
            f"{len(auth_rows)} authoritative minutes, "
            f"{len(mismatched)} intraday net mismatches, "
            f"reconcile changed={recon['changed']} added={recon['added']} "
            f"removed={recon['removed']}; "
            f"EOD drift live {summary_stats['live_eod_drift_m']} vs "
            f"auth {summary_stats['auth_eod_drift_m']} (M$)",
            summary_stats,
        )
        logger.info("theta_flow: EOD settle %s — %s",
                    builder.session_date, summary_stats)
        return True
    except Exception:
        logger.exception("theta_flow: EOD reconcile failed")
        return False


async def theta_flow_ingest_loop(state: Any) -> None:
    """Supervised loop (vix_daily_ingest conventions): heartbeat every wake,
    kill switch checked every wake, never raises out of an iteration.

    Lifecycle per ET session: boot/roll -> full-session rebuild (exact
    restart semantics) -> incremental polls during RTH -> EOD settle +
    reconcile -> idle until next session.
    """
    import httpx
    from fef_sidecar.engine.loop_watchdog import heartbeat as _wd_beat

    poll_sec = float(os.environ.get("FEF_THETA_FLOW_POLL_SEC", str(POLL_SEC_DEFAULT)))
    await asyncio.sleep(10)   # let the app finish booting
    logger.info("theta_flow ingest loop starting (poll=%.0fs, kill switch "
                "FEF_THETA_FLOW_ENABLED=%s)", poll_sec, _enabled())

    builder: ThetaFlowSessionBuilder | None = None
    session_ready = False          # boot rebuild done for builder.session_date
    settled_date: str | None = None
    settle_attempts = 0            # bounded EOD retry (MINOR-7)
    outage = False
    disabled_logged = False
    SETTLE_MAX_ATTEMPTS = 3
    PULL_DEADLINE_SEC = 240.0      # end-to-end bound per poll/rebuild stage
    REBUILD_DEADLINE_SEC = 600.0   # a drip-feeding Terminal must not wedge
                                   # the loop past the watchdog (MINOR-8)

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        while True:
            _wd_beat(state, "theta-flow-ingest")
            try:
                if not _enabled():
                    if not disabled_logged:
                        logger.info("theta_flow: disabled via FEF_THETA_FLOW_ENABLED")
                        disabled_logged = True
                    _update_holder(state, None, stale=True, note="disabled")
                    await asyncio.sleep(IDLE_SEC)
                    continue
                disabled_logged = False

                now = _now_et()
                today = now.strftime("%Y-%m-%d")
                mod = _minute_of_day(now)
                weekday = now.weekday() < 5

                # Session roll
                if builder is None or builder.session_date != today:
                    builder = ThetaFlowSessionBuilder(today)
                    session_ready = False
                    settle_attempts = 0

                in_poll_window = weekday and POLL_START_MIN <= mod <= POLL_END_MIN
                past_settle = weekday and mod >= SETTLE_AFTER_MIN

                if in_poll_window or (past_settle and settled_date != today):
                    if not session_ready:
                        # Boot / mid-session restart: rebuild today from the
                        # tape (exact; the pull is cheap on the live day).
                        rebuilt = await asyncio.wait_for(
                            rebuild_session(client, today),
                            REBUILD_DEADLINE_SEC,
                        )
                        builder = rebuilt
                        session_ready = True
                        n_rows = await persist_rows(list(builder.rows.values()))
                        logger.info(
                            "theta_flow: session %s rebuilt from tape — %d "
                            "minutes finalized (%d persisted new), drift=%s M$",
                            today, len(builder.rows), n_rows,
                            builder.drift_cum_net_m(),
                        )
                        await _audit(
                            f"theta_flow session {today} boot rebuild: "
                            f"{len(builder.rows)} minutes, {n_rows} new rows, "
                            f"drift={builder.drift_cum_net_m()} M$",
                            {"session_date": today,
                             "minutes": len(builder.rows),
                             "persisted_new": n_rows,
                             "drift_cum_net_m": builder.drift_cum_net_m()},
                        )
                    elif in_poll_window:
                        new_rows = await asyncio.wait_for(
                            _poll_cycle(client, builder), PULL_DEADLINE_SEC)
                        if new_rows:
                            await persist_rows(new_rows)

                    if past_settle and settled_date != today:
                        settle_attempts += 1
                        ok = await asyncio.wait_for(
                            _settle_session(client, builder),
                            REBUILD_DEADLINE_SEC,
                        )
                        if ok or settle_attempts >= SETTLE_MAX_ATTEMPTS:
                            if not ok:
                                await _audit(
                                    f"theta_flow EOD settle {today}: gave up "
                                    f"after {settle_attempts} attempts — "
                                    f"intraday rows stand unreconciled",
                                    {"session_date": today,
                                     "attempts": settle_attempts},
                                )
                            settled_date = today

                    if outage:
                        outage = False
                        logger.info("theta_flow: Terminal reachable again — resumed")
                    _update_holder(state, builder, stale=False)
                else:
                    _update_holder(
                        state, builder if session_ready else None,
                        stale=not session_ready, note="outside session window",
                    )

                await asyncio.sleep(poll_sec if in_poll_window else IDLE_SEC)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Fail-SOFT: single log per outage; holder marked stale;
                # engine unaffected. Next wake retries.
                if not outage:
                    outage = True
                    logger.warning(
                        "theta_flow: ingest error (%s: %s) — marking stale; "
                        "will retry quietly", type(e).__name__, e,
                    )
                _update_holder(state, builder, stale=True, note="ingest error")
                await asyncio.sleep(IDLE_SEC)
