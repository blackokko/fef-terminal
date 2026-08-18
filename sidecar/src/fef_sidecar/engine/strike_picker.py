"""Pick the short strike for a credit spread by delta proximity + net credit.

THE RULE, as the operator states it (PREMIUM_SELL_V1): **the short strike
NEAREST the delta target** — not "the first strike under the target". Those are
different instructions and the difference is not academic. On 2026-08-12 the
old "first under" scan selected delta −0.0329 against a 0.15 target and offered
$15.50 of credit against $984.50 of risk, on which the $2.56 commission alone is
16.5% of the gross credit (it runs 1–1.7% at a genuine 0.15Δ). Two independent
defects produced that pick, and BOTH are fixed here:

  1. Strikes whose quote failed were silently `continue`d, so the scan could
     step from >0.15Δ straight past four unquotable strikes to 0.033Δ and take
     it. Unquotable strikes are now COUNTED, and a scan with too little quote
     coverage is REFUSED outright — "nearest 0.15 among the two strikes that
     happened to quote" is not meaningfully nearest.

  2. The `if d < delta_max * 0.5: break` guard sat AFTER the accept test, so a
     too-far strike was accepted and returned before the guard was ever
     reached. It could not fire on the case it existed for. Selection now
     happens AFTER the whole scan, and the floor REJECTS rather than merely
     stopping the walk.

SELECTION. Scan the window; keep every strike that quotes a delta; require
`delta_floor <= |delta| <= delta_max`; among those choose the one whose |delta|
is closest to `delta_max`. The ceiling stays exactly where it was — `delta_max`
is a RISK bound and this module never walks above it — so "nearest" here means
nearest from below, which is the operator's rule on a monotone delta ladder.

CREDIT BACKSTOP. `premium_min` gates the SHORT leg's mid, not the net credit, so
it never protected the spread's economics; with `spread_premium_min = 0.0` in
state.py there was no floor at all. The floor is now expressed primarily as a
minimum net-credit / width RATIO so it scales with width instead of silently
becoming an undesigned vol filter the way a fixed $0.70 on a 5-wide did, plus a
small absolute floor. Both reject the candidate and move to the next one.

Async with conservative bounds to avoid hammering IBKR — at most
``max_scan_strikes`` short-leg quote calls per scan (usually far fewer: the walk
stops once delta drops below the floor, since nothing further OTM can re-enter
the band), and results cached for ``cache_ttl_sec``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from fef_sidecar.ibkr.models import Contract, ContractKind, OptionQuote
from fef_sidecar.ibkr.provider import IBKRProvider


logger = logging.getLogger(__name__)


# ⚠ THESE MUST STAY EQUAL TO THE `spread_*` DEFAULTS IN state.py. The 2026-08-12
# incident began with exactly this divergence: the module default was
# premium_min=0.70 while state.py passed 0.0, so the guard everyone believed was
# in force had been off for the entire life of the auto-trader. Change both or
# neither.
DEFAULT_DELTA_MAX = 0.15
# Floor as a fraction of the target. 0.5 preserves the intent of the old
# (never-reachable) `delta_max * 0.5` guard: at a 0.15 target the band is
# [0.075, 0.15] and the 0.033 pick that started all this is rejected.
DEFAULT_DELTA_FLOOR_FRAC = 0.5
# Fraction of SCANNED strikes that must have returned a usable delta before a
# "nearest" claim means anything.
DEFAULT_MIN_QUOTE_COVERAGE = 0.5
# Net credit must be at least this fraction of the width. Deliberately well
# below the ~14% that a $0.70 floor on a 5-wide imposed (state.py records that
# as an accidental vol filter) — this is a "cannot pay its own commission"
# backstop, not a regime filter. At a true 0.15Δ the ratio runs 15–25%.
DEFAULT_MIN_CREDIT_WIDTH_RATIO = 0.05
# Absolute floor per share, so a very narrow width cannot ratio its way down to
# a few cents of credit.
DEFAULT_MIN_NET_CREDIT = 0.10


@dataclass(frozen=True)
class StrikePick:
    short_strike: float
    long_strike: float
    short_delta: float
    short_mid: float
    short_bid: float | None
    short_ask: float | None
    width: float
    # NET credit per share, short_mid - long_mid. Was previously the SHORT leg's
    # mid alone, which overstated the credit by the entire cost of the long wing
    # and — because live_trading uses this value as the combo LIMIT PRICE — asked
    # the market for a credit the spread could never pay. Found in adversarial
    # review 2026-08-11 (SPREAD_AUTOTRADE_REVIEW.md, finding A1/A2).
    estimated_credit: float
    long_mid: float | None = None  # priced explicitly; None means unpriceable


@dataclass(frozen=True)
class _Candidate:
    """A short strike that actually quoted a delta."""
    strike: float
    delta: float
    mid: float
    bid: float | None
    ask: float | None


@dataclass
class _CacheEntry:
    pick: StrikePick | None
    fetched_at_ms: int


_pick_cache: dict[str, _CacheEntry] = {}


def _round_to_5(price: float) -> float:
    return round(price / 5) * 5


def _exchange_for(symbol: str) -> str:
    return "CBOE" if symbol == "SPX" else "SMART"


async def _quote_with_timeout(
    ibkr: IBKRProvider, contract: Contract, timeout_sec: float = 4.0
) -> OptionQuote | None:
    try:
        return await asyncio.wait_for(
            ibkr.get_option_quote(contract), timeout=timeout_sec
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.debug("Option quote timed out: %s", contract)
        return None
    except Exception:
        logger.exception("Option quote failed: %s", contract)
        return None


async def _scan_short_leg(
    ibkr: IBKRProvider,
    *,
    symbol: str,
    expiry: str,
    right: str,
    starting: float,
    direction: int,
    width: float,
    max_scan_strikes: int,
    delta_floor: float,
) -> tuple[list[_Candidate], int, int]:
    """Quote short-leg strikes walking OTM.

    Returns ``(candidates, scanned, unquotable)`` where ``scanned`` counts every
    strike we attempted and ``unquotable`` counts those that returned no quote
    or no delta. The caller needs all three: a candidate list alone cannot
    distinguish "the band really is empty" from "we could not see the band".
    """
    candidates: list[_Candidate] = []
    scanned = 0
    unquotable = 0

    for i in range(max_scan_strikes):
        short_strike = starting + direction * i * width
        if short_strike <= 0:
            break
        scanned += 1
        contract = Contract(
            symbol=symbol,
            kind=ContractKind.OPTION,
            exchange=_exchange_for(symbol),
            expiry=expiry,
            strike=short_strike,
            right=right,
        )
        quote = await _quote_with_timeout(ibkr, contract)
        if quote is None or quote.delta is None:
            unquotable += 1
            continue
        candidates.append(
            _Candidate(
                strike=short_strike,
                delta=quote.delta,
                mid=quote.mid,
                bid=quote.bid,
                ask=quote.ask,
            )
        )
        if abs(quote.delta) < delta_floor:
            # Delta falls monotonically as we walk OTM, so no strike beyond this
            # one can re-enter the band — stop paying for quotes.
            #
            # ⚠ THIS EARLY EXIT IS ONLY SAFE BECAUSE SELECTION HAPPENS AFTER THE
            # SCAN. The old code ran the same comparison immediately after an
            # accept test, which meant a strike far below the floor was returned
            # before the comparison was reached. Do not move selection back into
            # this loop.
            break

    return candidates, scanned, unquotable


def _rank_candidates(
    candidates: list[_Candidate],
    *,
    delta_max: float,
    delta_floor: float,
    premium_min: float,
) -> list[_Candidate]:
    """In-band candidates ordered NEAREST-target-first.

    Target is ``delta_max`` itself: it is simultaneously the operator's 0.15
    target and the hard risk ceiling, so nearest-to-target means the largest
    |delta| that does not exceed it.
    """
    in_band = [
        c
        for c in candidates
        if delta_floor <= abs(c.delta) <= delta_max and c.mid >= premium_min
    ]
    return sorted(in_band, key=lambda c: (abs(abs(c.delta) - delta_max), -abs(c.delta)))


async def _find_credit_strike(
    ibkr: IBKRProvider,
    *,
    kind: str,
    symbol: str,
    expiry: str,
    spot: float,
    right: str,
    direction: int,
    width: float,
    delta_max: float,
    premium_min: float,
    starting_offset: float,
    max_scan_strikes: int,
    cache_ttl_sec: int,
    delta_floor_frac: float,
    min_quote_coverage: float,
    min_credit_width_ratio: float,
    min_net_credit: float,
) -> StrikePick | None:
    """Shared PCS/CCS implementation.

    ``direction`` is -1 for puts (walk down from spot) and +1 for calls (walk
    up). The two sides were previously copy-pasted, which is why the same two
    defects existed twice and had to be fixed twice.
    """
    cache_key = f"{kind}|{symbol}|{expiry}|{_round_to_5(spot)}"
    now_ms = int(time.time() * 1000)
    entry = _pick_cache.get(cache_key)
    if entry and now_ms - entry.fetched_at_ms < cache_ttl_sec * 1000:
        return entry.pick

    def _refuse(why: str) -> None:
        logger.info("%s strike scan refused (%s): %s", kind, symbol, why)
        _pick_cache[cache_key] = _CacheEntry(None, now_ms)

    delta_floor = delta_max * delta_floor_frac
    starting = _round_to_5(spot + direction * starting_offset)

    candidates, scanned, unquotable = await _scan_short_leg(
        ibkr,
        symbol=symbol,
        expiry=expiry,
        right=right,
        starting=starting,
        direction=direction,
        width=width,
        max_scan_strikes=max_scan_strikes,
        delta_floor=delta_floor,
    )

    if scanned == 0:
        _refuse("no strikes in scan window")
        return None

    # --- quote-coverage gate ------------------------------------------------
    # A "nearest to target" claim is only as good as the sample it was drawn
    # from. If most of the window failed to quote, we did not observe the band;
    # we observed whatever happened to answer. Refuse rather than pick.
    quoted = len(candidates)
    coverage = quoted / scanned
    if unquotable:
        logger.info(
            "%s strike scan (%s): %d/%d strikes quoted, %d unquotable (coverage %.0f%%)",
            kind, symbol, quoted, scanned, unquotable, coverage * 100,
        )
    if coverage < min_quote_coverage:
        _refuse(
            f"only {quoted}/{scanned} strikes quoted a delta "
            f"({coverage:.0%} < {min_quote_coverage:.0%} required); "
            f"{unquotable} unquotable — cannot claim 'nearest {delta_max:.2f} delta'"
        )
        return None

    ranked = _rank_candidates(
        candidates, delta_max=delta_max, delta_floor=delta_floor, premium_min=premium_min
    )
    if not ranked:
        seen = ", ".join(f"{c.strike:.0f}@{c.delta:+.4f}" for c in candidates[:6])
        _refuse(
            f"no strike with |delta| in [{delta_floor:.3f}, {delta_max:.3f}]"
            + (f" and mid >= ${premium_min:.2f}" if premium_min > 0 else "")
            + f" — scanned {scanned}, saw [{seen}]"
        )
        return None

    # --- price the wing, nearest-target candidate first ----------------------
    for cand in ranked:
        long_strike = cand.strike + direction * width
        if long_strike <= 0:
            continue
        long_contract = Contract(
            symbol=symbol,
            kind=ContractKind.OPTION,
            exchange=_exchange_for(symbol),
            expiry=expiry,
            strike=long_strike,
            right=right,
        )
        long_quote = await _quote_with_timeout(ibkr, long_contract)
        if long_quote is None:
            continue
        # ⚠ DO NOT TRUST OptionQuote.mid ON THE WING. It falls back to `last`
        # whenever bid or ask is missing or zero, and a deep-OTM SPX wing is
        # routinely quoted 0.00 x 0.30 — so `mid` can be an hours-old print,
        # and this number BECOMES THE LIMIT PRICE. Measured scenario: wing
        # last=0.55 from a spike four hours earlier gives net_credit 0.95
        # against a true ~1.20, sells marketable, fills instantly $25/ct
        # under intent — and the error is then absorbed into the
        # adverse-selection estimate as if it were market impact, corrupting
        # the one number this path exists to measure. (Second review, F1.)
        #
        # Require a genuine two-sided quote and compute the mid ourselves.
        lb, la = long_quote.bid, long_quote.ask
        if lb is None or la is None or lb <= 0 or la <= 0 or la < lb:
            continue
        long_mid_true = (lb + la) / 2.0
        net_credit = cand.mid - long_mid_true
        # A vertical's credit is bounded by its width; anything outside that
        # is a pricing fault, not an opportunity.
        if not (0 < net_credit < width):
            continue
        # --- THE CREDIT BACKSTOP (see module docstring) ---------------------
        # Ratio first so the floor scales with width; absolute second so a
        # narrow spread cannot ratio down to pennies.
        if width > 0 and (net_credit / width) < min_credit_width_ratio:
            logger.info(
                "%s %s: rejecting short %.0f delta %+.4f — net credit $%.2f is "
                "%.1f%% of width %.0f, below the %.1f%% floor",
                kind, symbol, cand.strike, cand.delta, net_credit,
                100 * net_credit / width, width, 100 * min_credit_width_ratio,
            )
            continue
        if net_credit < min_net_credit:
            logger.info(
                "%s %s: rejecting short %.0f delta %+.4f — net credit $%.2f below "
                "absolute floor $%.2f",
                kind, symbol, cand.strike, cand.delta, net_credit, min_net_credit,
            )
            continue

        pick = StrikePick(
            short_strike=cand.strike,
            long_strike=long_strike,
            short_delta=cand.delta,
            short_mid=cand.mid,
            short_bid=cand.bid,
            short_ask=cand.ask,
            width=width,
            estimated_credit=net_credit,
            long_mid=long_mid_true,
        )
        logger.info(
            "%s %s: selected short %.0f delta %+.4f (target %.2f, band [%.3f, %.3f]), "
            "net credit $%.2f = %.1f%% of width %.0f; %d/%d strikes quoted",
            kind, symbol, cand.strike, cand.delta, delta_max, delta_floor, delta_max,
            net_credit, 100 * net_credit / width if width else float("nan"), width,
            quoted, scanned,
        )
        _pick_cache[cache_key] = _CacheEntry(pick, now_ms)
        return pick

    _refuse(
        f"{len(ranked)} in-band strike(s) but none could be priced into a "
        f"spread meeting the credit floor "
        f"({min_credit_width_ratio:.0%} of width, min ${min_net_credit:.2f})"
    )
    return None


async def find_pcs_strike(
    ibkr: IBKRProvider,
    *,
    symbol: str,
    expiry: str,
    spot: float,
    width: float = 5.0,
    delta_max: float = DEFAULT_DELTA_MAX,
    premium_min: float = 0.0,
    starting_offset: float = 5.0,
    max_scan_strikes: int = 10,
    cache_ttl_sec: int = 60,
    delta_floor_frac: float = DEFAULT_DELTA_FLOOR_FRAC,
    min_quote_coverage: float = DEFAULT_MIN_QUOTE_COVERAGE,
    min_credit_width_ratio: float = DEFAULT_MIN_CREDIT_WIDTH_RATIO,
    min_net_credit: float = DEFAULT_MIN_NET_CREDIT,
) -> StrikePick | None:
    """Scan PUT strikes BELOW spot and return the one whose |delta| is NEAREST
    ``delta_max`` without exceeding it, subject to the delta floor, the quote
    coverage gate and the net-credit floor. ``None`` when nothing qualifies —
    "show no spread" is the intended fallback, and on 2026-08-12 it was the
    correct answer.

    starting_offset = how many points below spot to begin scanning.
    Each iteration walks an additional `width` points OTM.
    """
    return await _find_credit_strike(
        ibkr,
        kind="PCS",
        symbol=symbol,
        expiry=expiry,
        spot=spot,
        right="P",
        direction=-1,
        width=width,
        delta_max=delta_max,
        premium_min=premium_min,
        starting_offset=starting_offset,
        max_scan_strikes=max_scan_strikes,
        cache_ttl_sec=cache_ttl_sec,
        delta_floor_frac=delta_floor_frac,
        min_quote_coverage=min_quote_coverage,
        min_credit_width_ratio=min_credit_width_ratio,
        min_net_credit=min_net_credit,
    )


async def find_ccs_strike(
    ibkr: IBKRProvider,
    *,
    symbol: str,
    expiry: str,
    spot: float,
    width: float = 5.0,
    delta_max: float = DEFAULT_DELTA_MAX,
    premium_min: float = 0.0,
    starting_offset: float = 5.0,
    max_scan_strikes: int = 10,
    cache_ttl_sec: int = 60,
    delta_floor_frac: float = DEFAULT_DELTA_FLOOR_FRAC,
    min_quote_coverage: float = DEFAULT_MIN_QUOTE_COVERAGE,
    min_credit_width_ratio: float = DEFAULT_MIN_CREDIT_WIDTH_RATIO,
    min_net_credit: float = DEFAULT_MIN_NET_CREDIT,
) -> StrikePick | None:
    """Mirror of find_pcs_strike for the CCS short leg — scans CALL strikes
    ABOVE spot. Shares the implementation rather than duplicating it; the two
    copies are how one bug became two."""
    return await _find_credit_strike(
        ibkr,
        kind="CCS",
        symbol=symbol,
        expiry=expiry,
        spot=spot,
        right="C",
        direction=1,
        width=width,
        delta_max=delta_max,
        premium_min=premium_min,
        starting_offset=starting_offset,
        max_scan_strikes=max_scan_strikes,
        cache_ttl_sec=cache_ttl_sec,
        delta_floor_frac=delta_floor_frac,
        min_quote_coverage=min_quote_coverage,
        min_credit_width_ratio=min_credit_width_ratio,
        min_net_credit=min_net_credit,
    )


def clear_strike_cache() -> None:
    """Wipe the strike-pick cache. Called when symbol changes."""
    _pick_cache.clear()
