"""Spread auto-trader — turns a credit-spread recommendation into an order.

THE GAP THIS CLOSES. The sidecar could already do everything except this:
`spread_entry` detects, `spread_recommender` picks strikes by delta,
`live_trading.place_credit_spread` submits a combo (BAG), `spread_exit_watcher`
manages the exit, `position_reconcile` reconciles. But `place_credit_spread` was
reachable ONLY from an API endpoint — nothing connected the recommendation to
the order. This module is that wire, and nothing else.

WHY THE DEFAULTS LOOK NOTHING LIKE THE PLAYBOOK. The structure is configured in
`state.py`; see there for what each value is worth and which study measured it.
The short version, from the research archive (2026-08-10/11):

  * width 10 not 5      -- edge/friction 2.59x vs 1.47x; widest won at EVERY
                           tenor, 4 of 4 complete rectangles.
  * hold to expiry      -- beat the 4x-stop + $0.05-TP pair in 49 of 72 cells;
                           that exact pair ranked 6 of 6. On a defined-risk
                           spread the long leg IS the stop; it is already bought.
  * puts only           -- call spreads measured negative even at MID fills in
                           BOTH eras.
  * no entry gate       -- 0 of 236 gates beat "sell every session", and gating
                           fraction r forfeits sqrt(r) of Sharpe outright.

WHAT IS BEING HARVESTED. Not a forecast. Options over-price the probability of
touching the short strike by 7.25pp (t = +7.36, both eras, 2022 included) -- the
only finding in the archive that survived every threshold. It is a risk premium:
payment for bearing a tail, which is why it persists and why no signal is needed.

⚠ THE OPEN QUESTION THIS EXISTS TO MEASURE. The measured edge is +$16.26/ct at
MID. Against it sits an adverse-selection drag estimated at -$16.34/ct with a
confidence interval of [-156, +84] -- LARGER than the edge, and entirely a MODEL,
because no order was ever actually resting there. No backtest can narrow it.
Only sending orders can. **The skipped sessions are the control group** -- a run
that only records fills answers nothing, so every attempt is logged either way.

SAFETY. Disabled by default. One position per session. Fixed qty (1). Every gate
denies loudly with a reason code. Placement goes through `place_credit_spread`,
which is itself dry-run unless live trading is separately enabled AND confirmed.
This module never cancels, modifies or closes anything -- exits belong entirely
to `spread_exit_watcher`.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Regular trading hours, ET. The entry window opens at the cash open: 0DTE
# friction runs 1.69% of premium at the open rising to 3.87% in the final hour
# (`chainpull`), so the open is the cheapest moment of the session to cross.
RTH_OPEN = datetime.time(9, 30)
RTH_LAST_ENTRY = datetime.time(15, 30)  # never open a new spread into the close

# Serialises the dedupe check and the placement. Without it two concurrent
# iterations can both read "no spread today" and both send — the failure the
# directional path was hardened against after twin QQQ 720P parents went out
# 16 ms apart (auto_trader.py:545-565).
_PLACE_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class SpreadAutoDecision:
    """Outcome of one evaluation. `fired` False always carries a reason."""
    fired: bool
    reason: str
    reason_code: str
    order_result: dict[str, Any] | None = None


def _deny(code: str, why: str) -> SpreadAutoDecision:
    return SpreadAutoDecision(fired=False, reason=why, reason_code=code)


def _now_et() -> datetime.datetime:
    return datetime.datetime.now(ET)


async def maybe_auto_trade_spread(
    state: Any,
    spread_rec: dict[str, Any] | None,
    *,
    source: str = "auto_spread",
) -> SpreadAutoDecision:
    """Evaluate every gate; place at most one credit spread per session.

    Gates are ordered cheapest-first so the common no-op path does no I/O.
    Returns a decision in all cases and raises nothing the caller must handle --
    the caller still wraps it defensively, because an auto-trade fault must
    never break the iteration pipeline.
    """
    # --- 1. master switch -------------------------------------------------
    if not getattr(state, "spread_auto_trade_enabled", False):
        return _deny("disabled", "spread auto-trade disabled")

    # --- 2. is there anything to trade? -----------------------------------
    if not spread_rec:
        return _deny("no_rec", "no spread recommendation this iter")
    kind = spread_rec.get("kind")
    if kind in (None, "NONE"):
        return _deny("rec_none", f"recommendation is {kind}: {spread_rec.get('reasoning', '')}")

    short_strike = spread_rec.get("short_strike")
    long_strike = spread_rec.get("long_strike")
    credit = spread_rec.get("estimated_credit_per_contract")
    if short_strike is None or long_strike is None or not credit or credit <= 0:
        return _deny(
            "rec_incomplete",
            f"recommendation missing strikes or credit "
            f"(short={short_strike}, long={long_strike}, credit={credit})",
        )

    # --- 3. puts-only guard, enforced here as well as in the recommender ---
    # Defence in depth: call spreads measured negative even at MID in both eras.
    if getattr(state, "spread_puts_only", True) and kind != "PCS":
        return _deny("calls_disabled", f"puts_only is set and recommendation is {kind}")

    # --- 3b. price the order NOW, before any I/O --------------------------
    # This is pure arithmetic on the recommendation, so a malformed structure is
    # rejected before we touch the DB or the broker. It also means the guard is
    # reachable in tests without stubbing either.
    #
    # ⚠ UNIT CONVERSION — the bug the adversarial review caught (finding A1).
    # `estimated_credit_per_contract` is dollars per CONTRACT (net_credit * 100).
    # `live_trading` uses `SpreadSpec.entry_credit` DIRECTLY as the combo limit
    # price, which IBKR expects in dollars per SHARE. Passing the per-contract
    # figure straight through offered a $1.50 spread at a $150.00 limit on a
    # 10-wide whose maximum possible credit is $10 — unfillable, while still
    # burning the session and writing a SUBMITTED row that blocked every later
    # attempt. The manual UI path divides by 100 here; this one now agrees.
    limit_per_share = float(credit) / 100.0

    # Structural guard so this class of fault can never reach the broker again,
    # whatever upstream sends. A vertical's credit is bounded by its width.
    width = spread_rec.get("spread_width")
    try:
        width_f = float(width) if width else 0.0
    except (TypeError, ValueError):
        width_f = 0.0
    if width_f <= 0:
        return _deny("no_width", f"recommendation has no usable spread_width ({width!r})")
    if not (0 < limit_per_share < width_f):
        return _deny(
            "limit_out_of_range",
            f"computed limit {limit_per_share:.2f} outside (0, width={width_f:.0f}) "
            f"— refusing to send. raw credit field was {credit!r}",
        )
    # The guard above is one-sided: it catches a per-CONTRACT value arriving where
    # per-share is expected (the original 100x bug), but NOT the mirror — a
    # per-share value arriving already converted would divide to 1/100th and place
    # far under intent. Both directions are the same units confusion. A real
    # credit spread at a tradeable delta is worth cents, not hundredths of a cent.
    if limit_per_share < 0.05:
        return _deny(
            "limit_implausibly_small",
            f"computed limit {limit_per_share:.4f} is below $0.05 — raw credit field "
            f"was {credit!r}; refusing to send a sub-penny credit",
        )

    # --- 4. session window ------------------------------------------------
    now = _now_et()
    # Use the shipped NYSE calendar rather than a weekday test — it already
    # exists in engine/event_calendar.py and covers holidays the naive check
    # would trade straight through (review finding).
    try:
        from fef_sidecar.engine.event_calendar import is_trading_day
        if not is_trading_day(now.strftime("%Y-%m-%d")):
            return _deny("not_trading_day", f"{now:%Y-%m-%d} is not an NYSE trading day")
    except Exception:
        # Fail CLOSED on a calendar fault rather than trading blind.
        logger.exception("spread_auto_trade: trading-day check failed; refusing")
        return _deny("calendar_failed", "could not confirm today is a trading day")

    # --- 4b. the recommendation must be from THIS session and THIS symbol ---
    rec_session = spread_rec.get("session_date")
    today_ymd = now.strftime("%Y%m%d")
    if rec_session != today_ymd:
        return _deny(
            "rec_stale",
            f"recommendation is stamped {rec_session!r}, today is {today_ymd} "
            "— refusing to trade a stale structure",
        )
    rec_symbol = spread_rec.get("symbol")
    active = getattr(state, "active_symbol", None)
    if rec_symbol and active and rec_symbol != active:
        return _deny(
            "rec_symbol_mismatch",
            f"recommendation was built for {rec_symbol}, active symbol is {active}",
        )
    as_of = spread_rec.get("as_of_ms")
    if as_of:
        age_sec = (datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000 - float(as_of)) / 1000.0
        max_age = float(getattr(state, "spread_rec_max_age_sec", 300.0))
        if age_sec > max_age:
            return _deny("rec_too_old", f"recommendation is {age_sec:.0f}s old (max {max_age:.0f}s)")

    if not (RTH_OPEN <= now.time() <= RTH_LAST_ENTRY):
        return _deny(
            "outside_window",
            f"{now:%H:%M} ET outside entry window "
            f"{RTH_OPEN:%H:%M}-{RTH_LAST_ENTRY:%H:%M}",
        )

    today = now.strftime("%Y-%m-%d")

    # --- 5. one per session, SERIALISED and DB-BACKED -----------------------
    # Everything from here to the placement runs under a module lock. The
    # directional path added exactly this after a documented incident where twin
    # QQQ 720P parents were placed 16 ms apart; two iterations can otherwise both
    # pass the dedupe and both send.
    async with _PLACE_LOCK:
        # In-memory latch first (cheap), then the DB (authoritative). The latch
        # alone is lost on restart, so a mid-session restart could place twice.
        if getattr(state, "spread_auto_trade_last_date", "") == today:
            return _deny("already_today", f"already placed a spread on {today}")

        # Authoritative check: any spread parent submitted since today's ET
        # midnight that still counts against the session budget. DRY_RUN counts
        # (a rehearsal must exercise the same guard the live path does); a
        # parent our own gates REFUSED before transmission does not — on
        # 2026-08-12 counting one of those burned the whole session and produced
        # 104 `already_today` blocks against an order that never existed at the
        # broker. The exclusion list lives in
        # `SignalIterRepository.SPREAD_DEDUPE_IGNORED_STATUSES`.
        try:
            from fef_sidecar.db.repository import SignalIterRepository
            et_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            todays = await SignalIterRepository.spread_parents_since(
                et_midnight.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            )
            if todays:
                state.spread_auto_trade_last_date = today  # re-latch after a restart
                ids = ", ".join(str(getattr(o, "id", "?")) for o in todays[:5])
                return _deny(
                    "already_today_db",
                    f"{len(todays)} spread parent(s) already submitted today (ids {ids})",
                )
        except Exception:
            # Fail CLOSED: an unverifiable session count must not become a trade.
            logger.exception("spread_auto_trade: session-count check failed; refusing")
            return _deny("dedupe_check_failed", "could not verify today's placements; refusing")

        return await _evaluate_and_place(state, spread_rec, now, today, kind,
                                         short_strike, long_strike, credit,
                                         limit_per_share, width_f, source)


async def _evaluate_and_place(
    state: Any, spread_rec: dict[str, Any], now: datetime.datetime, today: str,
    kind: Any, short_strike: Any, long_strike: Any, credit: Any,
    limit_per_share: float, width_f: float, source: str,
) -> SpreadAutoDecision:
    """Gates 6-8, executed under `_PLACE_LOCK` held by the caller."""
    observe = bool(getattr(state, "spread_auto_trade_observe_only", True))

    # --- 6. no open spread ------------------------------------------------
    # Cheap DB check. An unreconciled position must never be doubled.
    # Same predicate the exit watcher uses, so the two agree on what "open" means.
    try:
        from fef_sidecar.db.repository import SignalIterRepository
        for o in await SignalIterRepository.query_live_orders(limit=200):
            if (
                o.exit_strategy == "spread_premium"
                and o.bracket_role == "parent"
                and o.status in ("SUBMITTED", "FILLED", "PARTIAL")
            ):
                return await _observe_or_deny(
                    state, observe, today, "spread_open",
                    f"a spread is already open (order {getattr(o, 'id', '?')})",
                    spread_rec,
                )
    except Exception:
        # Fail CLOSED: if we cannot confirm the book is flat, do not add to it.
        logger.exception("spread_auto_trade: open-order check failed; refusing to place")
        return await _observe_or_deny(
            state, observe, today, "open_check_failed",
            "could not verify open spreads; refusing to place", spread_rec,
        )

    # --- 7. broker present ------------------------------------------------
    ibkr = getattr(state, "ibkr", None)
    if ibkr is None:
        return await _observe_or_deny(
            state, observe, today, "no_ibkr", "no IBKR provider on state", spread_rec,
        )

    # --- 8. place ----------------------------------------------------------
    from fef_sidecar.engine.live_trading import SpreadSpec, place_credit_spread

    qty = int(getattr(state, "spread_auto_trade_qty", 1) or 1)

    # `expiry` is not a field on the recommendation; it never was. Compute it
    # here explicitly rather than relying on a .get() that always misses and a
    # fallback that is only correct at 0DTE by coincidence.
    expiry = now.strftime("%Y%m%d")
    spec = SpreadSpec(
        symbol=getattr(state, "active_symbol", "SPX"),
        expiry=str(expiry),
        kind=str(kind),
        short_strike=float(short_strike),
        long_strike=float(long_strike),
        qty=qty,
        entry_credit=limit_per_share,
        # Exit config comes from state so hold-to-expiry is a setting, not a
        # hardcode. Sentinels: stop >= 1e9 and tp <= 0 can never trigger, so the
        # position is carried to expiry by the existing watcher untouched.
        stop_multiplier=float(getattr(state, "spread_stop_multiplier", 1e9)),
        tp_close_debit=float(getattr(state, "spread_tp_close_debit", 0.0)),
    )

    # --- 8a. OBSERVE-ONLY: record the intent, send nothing ------------------
    # Every gate above has passed against live data. This is the exact order
    # that would have gone out. Recording it here — and latching the session —
    # produces one attempt row per day with zero order risk, which is what the
    # fill measurement needs and what the five open review findings cannot touch.
    if observe:
        state.spread_auto_trade_last_date = today
        await _audit(
            state, "spread_auto_trade_observed",
            f"WOULD PLACE {kind} {short_strike}/{long_strike} x{qty} "
            f"@ {limit_per_share:.2f} net credit (observe-only, nothing sent)",
            {"source": source, "observe_only": True, "spec": _spec_dict(spec),
             "limit_per_share": limit_per_share,
             "credit_per_contract_raw": credit,
             "short_delta": spread_rec.get("short_delta"),
             "spread_width": spread_rec.get("spread_width"),
             "reasoning": spread_rec.get("reasoning"),
             "gated": bool(getattr(state, "spread_require_gate", False)),
             "trigger_signature": spread_rec.get("trigger_signature"),
             "rec_as_of_ms": spread_rec.get("as_of_ms"),
             "observed_at_et": now.isoformat()},
        )
        logger.info(
            "spread_auto_trade OBSERVED (not sent) %s %s/%s x%d @ %.2f",
            kind, short_strike, long_strike, qty, limit_per_share,
        )
        return SpreadAutoDecision(
            fired=False,
            reason=(
                f"observe-only: would place {kind} {short_strike}/{long_strike} "
                f"x{qty} @ {limit_per_share:.2f}"
            ),
            reason_code="observed",
        )

    try:
        result = await place_credit_spread(ibkr, spec, confirm=True)
    except Exception as e:
        logger.exception("spread_auto_trade: placement raised")
        await _audit(
            state, "spread_auto_trade_error",
            f"PLACEMENT RAISED {kind} {short_strike}/{long_strike}: {e}",
            {"source": source, "spec": _spec_dict(spec), "error": repr(e)},
        )
        return _deny("place_raised", f"placement raised: {e}")

    # ⚠ `place_credit_spread` RETURNS {"ok": False} rather than raising when it
    # refuses — paper-account gate, FEF_ALLOW_CREDIT_SPREADS unset (its DEFAULT),
    # or a submission failure. Not checking this reported "PLACED" on every
    # session while nothing was submitted, which would have silently corrupted
    # the attempt ledger this module exists to produce (review finding B3).
    if not isinstance(result, dict) or not result.get("ok"):
        reason = (result or {}).get("reason") or (result or {}).get("error") or "no reason given"
        await _audit(
            state, "spread_auto_trade_refused",
            f"REFUSED {kind} {short_strike}/{long_strike}: {reason}",
            {"source": source, "spec": _spec_dict(spec), "result": result},
        )
        # Day NOT marked used: nothing was sent, so the session is still live.
        return _deny("place_refused", f"placement refused: {reason}")

    # Mark the session used ONLY after a placement actually returned ok. A raise
    # or a refusal above leaves the day open for a retry on the next iteration.
    state.spread_auto_trade_last_date = today

    await _audit(
        state, "spread_auto_trade",
        f"PLACED {kind} {short_strike}/{long_strike} x{qty} @ {limit_per_share:.2f} net credit",
        {"source": source, "spec": _spec_dict(spec), "result": result,
         "reasoning": spread_rec.get("reasoning"),
         "short_delta": spread_rec.get("short_delta"),
         "gated": bool(getattr(state, "spread_require_gate", False)),
         "trigger_signature": spread_rec.get("trigger_signature")},
    )
    logger.info("spread_auto_trade PLACED %s %s/%s x%d @ %.2f net credit/share "
                "(raw per-contract field %.2f)",
                kind, short_strike, long_strike, qty, limit_per_share, float(credit))
    return SpreadAutoDecision(
        fired=True,
        reason=f"placed {kind} {short_strike}/{long_strike} x{qty}",
        reason_code="placed",
        order_result=result,
    )


async def _observe_or_deny(
    state: Any, observe: bool, today: str, code: str, why: str,
    spread_rec: dict[str, Any],
) -> SpreadAutoDecision:
    """Deny — and in observe-only mode also RECORD the skip, once per session.

    The skipped sessions are not noise; they are the control group. `touchsell`
    Phase 0 measured mid fills adverse in 8 of 8 cells with a -$16.34/ct point
    estimate and a CI of [-156, +84], and the only way to narrow that band is to
    compare the sessions that would have traded against the ones that would not.
    A ledger of fills alone answers nothing.

    Latches the day so a skip is recorded once, not on every iteration.
    """
    if observe and getattr(state, "spread_auto_trade_last_date", "") != today:
        state.spread_auto_trade_last_date = today
        await _audit(
            state, "spread_auto_trade_observed_skip",
            f"WOULD SKIP ({code}): {why}",
            {"observe_only": True, "reason_code": code, "reason": why,
             "recommendation": spread_rec},
        )
        logger.info("spread_auto_trade OBSERVED SKIP (%s): %s", code, why)
    return _deny(code, why)


def _spec_dict(spec: Any) -> dict[str, Any]:
    return {
        "symbol": spec.symbol, "expiry": spec.expiry, "kind": spec.kind,
        "short_strike": spec.short_strike, "long_strike": spec.long_strike,
        "qty": spec.qty, "entry_credit": spec.entry_credit,
        "stop_multiplier": spec.stop_multiplier, "tp_close_debit": spec.tp_close_debit,
    }


async def _audit(state: Any, category: str, summary: str, details: dict[str, Any]) -> None:
    """Best-effort audit write. Never raises into the trading path."""
    try:
        from fef_sidecar.db.repository import SignalIterRepository
        await SignalIterRepository.write_audit_log(
            category=category, actor="system", summary=summary, details=details,
        )
    except Exception:
        logger.exception("spread_auto_trade: audit write failed (non-fatal)")
