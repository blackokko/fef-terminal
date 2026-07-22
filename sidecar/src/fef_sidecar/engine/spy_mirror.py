"""SPY mirror vehicle (2026-07-17).

When the SPX auto-trade path commits a fire (all gates passed, SPX entry
placed), ALSO place a SPY option mirror of the same trade at ~1/10 scale:
**SPX signal (theta flow, validated) -> SPY vehicle.** This is explicitly
NOT the SPY *scanner symbol* — per-ticker QD flow is the unvalidated input
class and scanner symbol enablement is untouched by this module.

Validated 2026-07-17 (scratchpad spy_mapping_report.md, pre-registered):
the SPOT-ADJUSTED strike map puts the SPY contract at the SAME percentage
moneyness as the validated SPX contract (entry-premium ratio ~1/9.3 calls /
~1/9.4 puts, confound RESOLVED); SPY transfers the SPX book at 1/10 scale
with no measurable venue friction (matched delta vs SPX/10 = +$0.53/ct OOS,
clustered t +1.65). The study's honesty cap: "consistent with SPX/10,
awaiting live confirmation" — THIS module is that live confirmation, on its
own ledger (LiveOrder.mirror_of tag), never mixed into the SPX verdict
window (which filters symbol='SPX' AND mirror_of IS NULL).

Design rules:
  * FAIL-SOFT: any failure (spot fetch, expiry, ex-div, quote, placement)
    SKIPS the mirror with an audit row (category='spy_mirror') and never
    touches the already-placed SPX fire.
  * The mirror rides the SPX fire's gates + cooldown — no gates of its own
    except: per-symbol cap (auto_trade_max_per_symbol applies to SPY),
    duplicate-parent guard on the SPY contract, same-day-expiry check, and
    the ex-dividend assignment guard.
  * The SPX leg and the SPY leg are INDEPENDENT positions once placed: each
    has its own watcher plan in its own price space, its own entry timeout,
    its own dollar-loss cap (scaled), and its own exits. One leg stopping
    while the other runs is by design (venue-difference measurement), not a
    defect.
  * Kill switches: UIConfig.spy_mirror_enabled (hot-read per fire) AND the
    FEF_SPY_MIRROR_ENABLED env (set to false/0/no/off to kill regardless of
    config). Any config read failure disables the mirror (a misconfiguration
    can only PREVENT trades — the auto_trader design rule).

Exit management is the existing spx_target_watcher: the mirror parent is a
watcher-managed LiveOrder with symbol='SPY' whose plan levels live in SPY
price space (SPY targets/stop = SPY_entry_spot +/- SPX point offsets / 10).
The watcher's 2026-07-07 per-symbol machinery (_watcher_symbol_spot units
guard + per-symbol spot resolution — the same path that managed the SPY 749P
parent on 7/7) evaluates it against SPY's own spot, never the SPX tick.
The 15:40 ET assignment guard (hard flat before SPY's physical-settlement
expiry) lives in spx_target_watcher._eod_assignment_flat_cycle.
"""

from __future__ import annotations

import logging
import math
import os
import time

logger = logging.getLogger(__name__)

MIRROR_SYMBOL = "SPY"

# SPX notional / SPY notional — the share-class ratio the validated study
# monetized at (SPY ~ SPX/10 with a ~0.5-0.7% dividend-basis offset that the
# strike map absorbs; the LADDER offsets use the clean /10 per the study's
# matched-friction comparator "SPX532_net/10").
SPX_TO_SPY_POINTS = 10.0

# Plausibility band for spx_spot/spy_spot at fire time. The true ratio is
# ~10.05-10.10 (SPY trades ~0.5-0.7% under SPX/10); anything outside this
# band means one of the two spots is stale/garbage — skip the mirror rather
# than map a strike off a broken input. TIGHTENED per adversarial review
# (Finding 1): the old [8, 13] band passed an ES-contaminated SPX ref
# (ratio ~12) — [9.7, 10.6] rejects anything beyond ~±4% of the true ratio
# (a 0.7% ES basis lands at ~10.12 x 1.007 ≈ 10.17, still inside; the
# PRIMARY defense against contamination is the verdict-spot anchor below —
# this band is the backstop for stale-quote classes).
_SPOT_RATIO_MIN = 9.7
_SPOT_RATIO_MAX = 10.6

# Mirror entry premium plausibility vs the SPX leg's own fire ask: the
# validated study puts the SPY contract at ~1/9.3-1/9.4 of the SPX premium.
# A resolved SPY ask far outside [0.4, 1.6] x (SPX ask / 10) means a
# mismapped strike, a garbage quote, or a delayed feed — skip. Also the
# mirror-side substitute for the chase bound resolve_entry_ask cannot apply
# with opt={} (adversarial review Finding 6).
_PREMIUM_RATIO_MIN = 0.4
_PREMIUM_RATIO_MAX = 1.6

# SPY spot sanity band (units guard on the stock quote itself).
_SPY_SPOT_MIN = 50.0
_SPY_SPOT_MAX = 2000.0

# --- Non-blocking SPY spot resolution (2026-07-20 contention fix) -----------
# The fire-time SPY spot must NEVER come from a synchronous IBKR round-trip.
# The old resolve_spy_spot did a blocking get_last_quote on the single shared
# IBKR connection INSIDE the SPX fire hook; it timed out ("SPY MIRROR SKIPPED:
# SPY spot fetch timed out after 5s") whenever that connection was busy placing
# the SPX entry (diagnosed live 2026-07-20 — NOT a data/subscription issue).
# The spot is now resolved WITHOUT any inline IBKR call: (a) the watcher's
# detached-refresh TTL stock-spot cache, else (b) SPY = SPX / ratio derived off
# the SAME validated SPX underlying the SPX leg mapped its strike from.
#
# Derivation ratio (SPX_spot / SPY_spot). Default = the 2026-07-20 live probe
# (SPX/SPY ~= 10.065; SPY trades ~0.6% under SPX/10 on the dividend basis).
# Overridable via env; ALWAYS clamped to [_SPOT_RATIO_MIN, _SPOT_RATIO_MAX]
# before use so a bad env or stale paired ratio can only skip, never misland.
_SPY_SPX_RATIO_DEFAULT = float(os.environ.get("FEF_SPY_MIRROR_SPX_RATIO", "10.065"))
# A recent live paired SPX+SPY observation supplies the derivation ratio until
# this old (ms); past it, fall back to the default constant. Intraday basis
# drift is only a few bps (spy_mapping_report.md) so a generous window is safe.
_LIVE_RATIO_MAX_AGE_MS = int(
    float(os.environ.get("FEF_SPY_MIRROR_RATIO_MAX_AGE_SEC", "900")) * 1000
)

# ---- SPY ex-dividend assignment guard --------------------------------------
# SPY goes ex-dividend QUARTERLY on the third Friday of Mar/Jun/Sep/Dec
# (SSgA SPDR S&P 500 ETF Trust distribution schedule; the pattern has held
# every quarter 2020-2025 — ex-date = quarterly options expiration Friday).
# A 0DTE SPY option expiring ON an ex-div date carries early-assignment /
# dividend-capture risk (American exercise, physical settlement) — the
# validated study explicitly left this OUT of scope, so the mirror SKIPS
# those sessions entirely. 2026 third Fridays, computed + cross-checked:
#   Mar 20, Jun 19, Sep 18, Dec 18 (all Fridays).
# FAIL-SAFE: for years with no hardcoded entry (2027+ until updated), any
# expiry in a quarterly month (Mar/Jun/Sep/Dec) is skipped with a loud
# reason — if uncertain, skip and log (spec rule).
SPY_EX_DIV_DATES: frozenset[str] = frozenset({
    "20260320", "20260619", "20260918", "20261218",
})
_EX_DIV_KNOWN_YEARS: frozenset[int] = frozenset({2026})
_EX_DIV_QUARTER_MONTHS: frozenset[int] = frozenset({3, 6, 9, 12})


def spy_ex_div_skip(expiry_yyyymmdd: str) -> tuple[bool, str]:
    """(skip?, reason). Fail-safe: unparseable or unknown-year quarterly
    months are SKIPPED — never mirror into an uncertain ex-div session."""
    e = str(expiry_yyyymmdd or "").strip()
    if e in SPY_EX_DIV_DATES:
        return True, f"SPY ex-dividend date {e} (quarterly distribution)"
    try:
        year = int(e[0:4])
        month = int(e[4:6])
        day = int(e[6:8])
        if not (2020 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
            raise ValueError(e)
    except Exception:
        return True, (
            f"unparseable SPY expiry {e!r} — fail-safe skip (cannot rule out "
            f"ex-div)"
        )
    if year not in _EX_DIV_KNOWN_YEARS and month in _EX_DIV_QUARTER_MONTHS:
        return True, (
            f"expiry {e} is in a quarterly ex-div month of a year with no "
            f"hardcoded SPY ex-div dates — fail-safe skip (update "
            f"SPY_EX_DIV_DATES in spy_mirror.py)"
        )
    return False, ""


# ---- Config / kill switch ---------------------------------------------------

def _env_killed() -> bool:
    """FEF_SPY_MIRROR_ENABLED env kill switch. Unset -> config governs.
    Set to anything not truthy -> mirror OFF regardless of config."""
    raw = os.environ.get("FEF_SPY_MIRROR_ENABLED")
    if raw is None:
        return False
    return raw.lower() not in ("true", "1", "yes", "on")


def mirror_enabled() -> tuple[bool, str]:
    """(enabled?, reason-when-disabled). Config read failure = DISABLED —
    a misconfiguration may only PREVENT trades, never place extras."""
    if _env_killed():
        return False, "FEF_SPY_MIRROR_ENABLED env kill switch is off"
    try:
        from fef_sidecar.config_store import load_config
        if not bool(load_config().spy_mirror_enabled):
            return False, "spy_mirror_enabled=false in config"
    except Exception:
        logger.exception("spy_mirror config read failed — mirror disabled")
        return False, "config read failed (fail-safe: mirror disabled)"
    return True, ""


def mirror_qty() -> int:
    """Configured mirror size (default 10 = one SPX-contract-equivalent).
    Out-of-band values fall back to the UIConfig default."""
    try:
        from fef_sidecar.config_store import UIConfig, load_config
        q = int(load_config().spy_mirror_qty)
        if 1 <= q <= 100:
            return q
        return int(UIConfig().spy_mirror_qty)
    except Exception:
        logger.exception("spy_mirror qty read failed — using default 10")
        return 10


# ---- Strike + ladder mapping ------------------------------------------------

def map_spy_strike(spx_strike: float, spx_spot: float, spy_spot: float) -> float:
    """THE validated spot-adjusted map (spy_mapping_report.md): the SPY
    strike at the SAME percentage moneyness as the SPX contract —
        SPY_strike = SPY_spot * (1 + (SPX_strike - SPX_spot)/SPX_spot)
    rounded HALF-UP to SPY's $1 strike grid (the study's round-half-up).
    The naive K/10 map is exactly the confound the study killed (calls
    landed ~0.7% further OTM, puts ~2x nearer the money)."""
    if spx_spot <= 0 or spy_spot <= 0:
        raise ValueError(f"bad spots spx={spx_spot} spy={spy_spot}")
    moneyness = (float(spx_strike) - float(spx_spot)) / float(spx_spot)
    raw = float(spy_spot) * (1.0 + moneyness)
    return float(math.floor(raw + 0.5))


def map_spy_level(spx_level: float, spx_spot: float, spy_spot: float) -> float:
    """A watcher plan level in SPY price space: SPY_entry_spot +/- the SPX
    point offset / 10 (the study's matched comparator geometry). Returned at
    cent precision — the watcher compares floats against penny spots."""
    offset = (float(spx_level) - float(spx_spot)) / SPX_TO_SPY_POINTS
    return round(float(spy_spot) + offset, 2)


def mirror_tier_split(qty: int) -> tuple[int, int, int]:
    """50/30/20 T1/T2/T3 split — the VALIDATED study's exit fractions (the
    mini-venue + spot-adjusted studies both walked 50/30/20 by tier), which
    is what the mirror is live-confirming. qty=10 -> (5, 3, 2).

    Small-qty degeneracy handled exactly like auto_trader._tier_split
    (0-qty tiers are the one safe skip-marker for the watcher):
    1 -> (1,0,0); 2 -> (1,1,0). Parts always sum to qty, none negative."""
    q = int(qty)
    if q <= 0:
        return (0, 0, 0)
    if q == 1:
        return (1, 0, 0)
    if q == 2:
        return (1, 1, 0)
    t1 = max(1, round(q * 0.50))
    t2 = max(1, round(q * 0.30))
    t3 = q - t1 - t2
    if t3 < 0:
        t2 = max(1, t2 + t3)
        t3 = q - t1 - t2
    if t3 < 0:
        t1 = max(1, t1 + t3)
        t3 = q - t1 - t2
    return (t1, t2, max(0, t3))


def scale_mirror_dollars(x: float, scale: float) -> float:
    """Position-level $ target scaled to the mirror's notional. Whole
    dollars, $10 minimum magnitude, sign preserved (same convention as the
    auto_trader's ETF $-exit scaling)."""
    return math.copysign(max(10.0, abs(round(float(x) * scale))), float(x))


def mirror_notional_scale(mirror_q: int, spx_qty: int) -> float:
    """(mirror notional) / (SPX position notional) — one SPY contract is
    ~1/10 of one SPX contract, so a 10-lot mirror of a 3-lot SPX fire is
    (10/10)/3 = 1/3 of the SPX position's dollar scale. Used to scale the
    fixed_dollar ladder and the catastrophe dollar_loss_cap so the mirror
    is monitored independently AT ITS OWN SCALE."""
    if spx_qty <= 0:
        return 1.0 / SPX_TO_SPY_POINTS
    return (float(mirror_q) / SPX_TO_SPY_POINTS) / float(spx_qty)


# ---- SPY spot (NON-BLOCKING — 2026-07-20 contention fix) --------------------

def _record_live_ratio(state, spx_spot: float, spy_spot: float) -> None:
    """Cache the live SPX/SPY ratio from a fresh PAIRED observation (a cache
    hit alongside the current SPX underlying) so a later cold-cache fire can
    derive off the real basis instead of the constant. Only plausible ratios
    are stored; failures are swallowed (a dynamic-attr write must never break
    the fire hook)."""
    try:
        if not (isinstance(spx_spot, (int, float)) and isinstance(spy_spot, (int, float))):
            return
        if spy_spot <= 0 or not math.isfinite(spx_spot) or not math.isfinite(spy_spot):
            return
        ratio = float(spx_spot) / float(spy_spot)
        if _SPOT_RATIO_MIN <= ratio <= _SPOT_RATIO_MAX:
            state._spy_mirror_live_ratio = (int(time.time() * 1000), ratio)
    except Exception:
        pass


def _derivation_ratio(state) -> tuple[float, str]:
    """(ratio, source) for the SPX->SPY spot derivation: a recent live paired
    ratio if it is fresh AND inside the plausibility band, else the configured
    default constant. Never raises."""
    try:
        rec = getattr(state, "_spy_mirror_live_ratio", None)
        if rec is not None:
            ts_ms, ratio = rec
            if (int(time.time() * 1000) - int(ts_ms) <= _LIVE_RATIO_MAX_AGE_MS
                    and _SPOT_RATIO_MIN <= float(ratio) <= _SPOT_RATIO_MAX):
                return float(ratio), "live_paired"
    except Exception:
        pass
    return _SPY_SPX_RATIO_DEFAULT, "default_const"


async def resolve_spy_spot(state, spx_spot: float) -> tuple[float | None, str]:
    """Resolve the fire-time SPY spot WITHOUT a synchronous IBKR round-trip.

    Preference order (the mirror must NEVER block the SPX fire hook on a live
    SPY quote — see the module note + the 2026-07-20 contention diagnosis):

      (a) the watcher's detached-refresh TTL stock-spot cache
          (`_watcher_stock_quote_spot`) — NON-BLOCKING: a stale/missing entry
          only KICKS a background refresh and returns what it already has.
          BID/ASK MID ONLY (the same source the watcher uses to manage the
          mirror parent); a hit also records the live SPX/SPY ratio for (b).
      (b) derive off the in-process validated SPX underlying (the SAME
          spx_spot the SPX leg mapped its strike from):
              SPY = SPX / ratio,  ratio = a fresh live paired observation if
          available else the configured default (~10.065). Pure arithmetic —
          no IBKR call.
      (c) fail-soft: (None, reason). The caller skips with the existing
          spy_mirror audit row and NEVER blocks the SPX leg. Now a RARE last
          resort, reached only if spx_spot itself is unusable (it is already
          validated >= 2000 upstream, so this is defense-in-depth).
    """
    # (a) non-blocking cache read; a stale/cold entry self-kicks a detached
    #     refresh (so this call also helps keep the cache warm).
    cached: float | None = None
    csrc: str | None = None
    try:
        from fef_sidecar.engine.spx_target_watcher import (
            _watcher_stock_quote_spot,
        )
        cached, csrc = await _watcher_stock_quote_spot(state, MIRROR_SYMBOL)
    except Exception:  # noqa: BLE001 — any cache-path failure falls through to (b)
        cached, csrc = None, None
    if (isinstance(cached, (int, float)) and math.isfinite(cached)
            and _SPY_SPOT_MIN <= cached <= _SPY_SPOT_MAX):
        _record_live_ratio(state, spx_spot, float(cached))
        return round(float(cached), 4), f"watcher_cache:{csrc or 'ibkr_stock_quote'}"

    # (b) derive off the validated SPX underlying — no IBKR call.
    if (isinstance(spx_spot, (int, float)) and math.isfinite(spx_spot)
            and spx_spot > 0):
        ratio, rsrc = _derivation_ratio(state)
        derived = float(spx_spot) / ratio
        if _SPY_SPOT_MIN <= derived <= _SPY_SPOT_MAX:
            return round(derived, 4), f"spx_ratio_derived:{rsrc}:{ratio:.4f}"

    # (c) fail-soft skip (rare last resort — spx_spot unusable).
    return None, (
        f"no non-blocking SPY spot: cache cold and spx_spot={spx_spot!r} "
        f"is unusable for SPX/ratio derivation"
    )


async def warm_spy_spot_cache(state) -> None:
    """Kick the watcher's DETACHED SPY stock-spot refresh (non-blocking) so the
    TTL cache is warm before the first mirror fire — making resolve_spy_spot
    path (a) the normal case rather than always deriving. Called each scan from
    the main tick loop; cheap + idempotent (the watcher's refresh latch dedups
    within the TTL). No-op when the mirror is disabled or the kick fails; never
    raises and never awaits the IBKR round-trip itself."""
    try:
        enabled, _why = mirror_enabled()
        if not enabled:
            return
        from fef_sidecar.engine.spx_target_watcher import (
            _watcher_stock_quote_spot,
        )
        # Returns immediately (cache read + detached create_task); we ignore
        # the value — this call exists only to spawn/refresh the background
        # fetch so a later fire hits path (a).
        await _watcher_stock_quote_spot(state, MIRROR_SYMBOL)
    except Exception:
        logger.debug("spy_mirror cache warm kick failed", exc_info=True)


# ---- Skip-audit helper -------------------------------------------------------

async def _audit_skip(reason_code: str, summary: str, details: dict) -> None:
    try:
        from fef_sidecar.db.repository import SignalIterRepository
        await SignalIterRepository.write_audit_log(
            category="spy_mirror", actor="system",
            summary=f"SPY MIRROR SKIPPED: {summary}",
            details={"skip_reason": reason_code, **details},
        )
    except Exception:
        logger.exception("spy_mirror skip-audit write failed (%s)", reason_code)


# ---- The mirror --------------------------------------------------------------

def _now_et_hhmm_ymd() -> tuple[str, str]:
    """ET clock, delegated to the watcher's DST-correct helper (separate
    symbol so tests can pin the mirror's clock independently)."""
    from fef_sidecar.engine.spx_target_watcher import _et_now_hhmm_ymd
    return _et_now_hhmm_ymd()


def _late_session_cutoff(today_ymd: str, entry_timeout_sec: float | None) -> str:
    """Last ET minute a NEW mirror may be placed: the session's EOD-flat
    window start minus (entry timeout + 60s margin), min 2 minutes
    (adversarial review Finding 4a: a mirror placed at/after ~15:39 is
    either churned by the 15:40 flat seconds after filling, or its unfilled
    entry races the flat's refusal/park machinery)."""
    from fef_sidecar.engine.spx_target_watcher import _eod_flat_window
    start, _end = _eod_flat_window(today_ymd)
    lead_min = max(2, math.ceil(((entry_timeout_sec or 45.0) + 60.0) / 60.0))
    total = int(start[:2]) * 60 + int(start[3:]) - lead_min
    return f"{total // 60:02d}:{total % 60:02d}"


async def maybe_place_spy_mirror(
    state,
    *,
    direction: str,
    spx_strike: float,
    spx_qty: int,
    spx_parent_id: int | None,
    source: str,
    strategy: str,
    spx_t1: float | None,
    spx_t2: float | None,
    spx_t3: float | None,
    spx_stop: float | None,
    entry_timeout_sec: float | None,
    spx_spot_ref: float | None = None,
    spx_entry_ask: float | None = None,
) -> dict:
    """Place the SPY mirror of a just-committed SPX auto-trade fire.

    Called by auto_trader AFTER the SPX order placed + counters bumped, still
    inside the SPX firing lock (ordering guarantees the mirror references a
    persisted SPX parent id). EVERY failure path returns {"ok": False,
    "skipped": reason} with an audit row — never raises into the SPX path
    (the caller additionally wraps in try/except as a belt).

    spx_spot_ref: the verdict's own SPX underlying (option_pricing.
    underlying_price_at_quote, gate-8-validated) — the PRIMARY anchor for
    the strike/ladder map. The active-chart tick is only trusted as a
    fallback when the active symbol IS SPX (adversarial review Finding 1:
    an ES chart feeds ES-priced ticks into last_live_tick_spx — mapping off
    it lands the strike ~0.7% low and puts T1 BELOW spot, instant-dumping
    half the mirror).
    spx_entry_ask: the SPX leg's resolved fire ask — bounds the mirror's
    premium at ~1/10 (the chase-bound substitute; review Finding 6).
    """
    details_base = {
        "spx_parent_id": spx_parent_id, "source": source,
        "direction": direction, "spx_strike": spx_strike,
        "spx_qty": spx_qty, "strategy": strategy,
    }

    enabled, why = mirror_enabled()
    if not enabled:
        await _audit_skip("disabled", why, details_base)
        return {"ok": False, "skipped": "disabled", "reason": why}

    if spx_parent_id is None:
        # Structural invariant (review Finding 17): an untagged mirror would
        # poison the ledger split AND cross-symbol-block later SPX fires.
        await _audit_skip(
            "no_spx_parent_id",
            "SPX fire returned no parent_order_id — refusing an untaggable "
            "mirror", details_base,
        )
        return {"ok": False, "skipped": "no_spx_parent_id"}

    if strategy not in ("spx_points", "fixed_dollar"):
        # option_price (legacy IBKR bracket) is NOT mirrored: its children are
        # premium-space limits which have no validated 1/10 mapping and no
        # watcher management. Documented scope cut.
        await _audit_skip(
            "strategy_unsupported",
            f"exit strategy {strategy!r} not watcher-managed — mirror only "
            f"supports spx_points/fixed_dollar", details_base,
        )
        return {"ok": False, "skipped": "strategy_unsupported"}

    right = "C" if direction == "LONG_CALLS" else "P"
    m_qty = mirror_qty()

    # Same-day SPY expiry (SPY lists daily expiries; the resolver asks IBKR
    # and caches per session). A non-today nearest expiry would be a
    # DIFFERENT trade (extra theta/vega) — skip instead.
    try:
        from fef_sidecar.engine.expiry_resolver import (
            _today_et_ymd,
            resolve_nearest_expiry,
        )
        today_ymd = _today_et_ymd()
        spy_expiry = await resolve_nearest_expiry(state, MIRROR_SYMBOL)
    except Exception as e:  # noqa: BLE001
        await _audit_skip("expiry_resolve_failed", f"expiry resolve failed: {e}",
                          details_base)
        return {"ok": False, "skipped": "expiry_resolve_failed"}
    if spy_expiry != today_ymd:
        await _audit_skip(
            "no_same_day_expiry",
            f"nearest SPY expiry {spy_expiry} != today {today_ymd} — a "
            f"non-0DTE mirror is a different trade", details_base,
        )
        return {"ok": False, "skipped": "no_same_day_expiry"}

    # Ex-dividend assignment guard (quarterly; fail-safe skip).
    ex_skip, ex_why = spy_ex_div_skip(spy_expiry)
    if ex_skip:
        await _audit_skip("ex_div", ex_why, {**details_base, "expiry": spy_expiry})
        return {"ok": False, "skipped": "ex_div", "reason": ex_why}

    # Late-session cutoff (review Finding 4a): no NEW mirror within
    # (entry timeout + margin) of the EOD assignment flat — it would be
    # churned (buy at ask, market-sold seconds later) or its unfilled entry
    # would race the flat's refusal machinery.
    try:
        now_hhmm, _now_ymd = _now_et_hhmm_ymd()
        cutoff = _late_session_cutoff(today_ymd, entry_timeout_sec)
    except Exception:
        now_hhmm, cutoff = "00:00", "23:59"  # clock failure -> don't gate
    if now_hhmm >= cutoff:
        await _audit_skip(
            "late_session",
            f"{now_hhmm} ET is past the mirror cutoff {cutoff} (EOD "
            f"assignment flat imminent — a new SPY 0DTE would be churned)",
            details_base,
        )
        return {"ok": False, "skipped": "late_session"}

    # Per-symbol cap — SPY carries the same session budget any symbol does.
    try:
        spy_count = int(state.auto_trade_count_by_symbol.get(MIRROR_SYMBOL, 0))
        cap = int(state.auto_trade_max_per_symbol)
    except Exception:
        spy_count, cap = 0, 2
    if spy_count >= cap:
        await _audit_skip(
            "symbol_cap", f"SPY session cap reached ({spy_count}/{cap})",
            details_base,
        )
        return {"ok": False, "skipped": "symbol_cap"}

    # SPX spot reference — PRIMARY: the verdict's own gate-8-validated
    # underlying (spx_spot_ref = option_pricing.underlying_price_at_quote).
    # The active-chart tick (last_live_tick_spx / last_spx_price) is the
    # documented 7/6 contamination class — it carries the ACTIVE symbol's
    # price (an ES chart feeds ES-priced ~+0.7% values) — so it is only
    # trusted as a fallback when the active symbol IS SPX (review Finding 1).
    spx_spot = 0.0
    spx_spot_src = "verdict_underlying"
    if (isinstance(spx_spot_ref, (int, float)) and math.isfinite(spx_spot_ref)
            and spx_spot_ref >= 2000.0):
        spx_spot = float(spx_spot_ref)
    elif str(getattr(state, "active_symbol", "") or "").upper() == "SPX":
        spx_spot = float(
            getattr(state, "last_live_tick_spx", 0.0)
            or getattr(state, "last_spx_price", 0.0)
            or 0.0
        )
        spx_spot_src = "active_spx_tick"
    if spx_spot < 2000.0:
        await _audit_skip(
            "no_spx_spot",
            f"no trustworthy SPX spot reference (verdict underlying="
            f"{spx_spot_ref!r}, active_symbol="
            f"{getattr(state, 'active_symbol', '?')} — the active-chart tick "
            f"is only trusted while charting SPX)",
            details_base,
        )
        return {"ok": False, "skipped": "no_spx_spot"}

    # SPY spot — NON-BLOCKING (2026-07-20 fix): the watcher's detached-refresh
    # TTL cache, else derived from the validated SPX underlying (SPY = SPX /
    # ratio). NEVER a synchronous SPY quote round-trip inside the SPX fire hook
    # (the diagnosed 5s-timeout-under-contention bug). Fail-soft skip is now a
    # rare last resort (spx_spot was validated >= 2000 just above). The audit
    # reason CODE stays "spot_fetch_failed" for monitoring/ledger continuity.
    spy_spot, spot_src = await resolve_spy_spot(state, spx_spot)
    if spy_spot is None:
        await _audit_skip("spot_fetch_failed", spot_src, details_base)
        return {"ok": False, "skipped": "spot_fetch_failed", "reason": spot_src}

    ratio = spx_spot / spy_spot
    if not (_SPOT_RATIO_MIN <= ratio <= _SPOT_RATIO_MAX):
        await _audit_skip(
            "spot_ratio_implausible",
            f"SPX/SPY spot ratio {ratio:.2f} outside [{_SPOT_RATIO_MIN:.0f}, "
            f"{_SPOT_RATIO_MAX:.0f}] (spx={spx_spot:.2f} spy={spy_spot:.2f}) — "
            f"one spot is stale/garbage", details_base,
        )
        return {"ok": False, "skipped": "spot_ratio_implausible"}

    spy_strike = map_spy_strike(spx_strike, spx_spot, spy_spot)

    # Env risk-cap clip (review Finding 8): the SPX gate clips its qty to
    # FEF_MAX_CONTRACTS_PER_TRADE; the mirror honors the same knob at SPY's
    # 1/10 notional (cap x 10 SPY contracts = the same notional ceiling).
    try:
        _max_contracts = int(os.environ.get("FEF_MAX_CONTRACTS_PER_TRADE", "20"))
    except ValueError:
        _max_contracts = 20
    _spy_cap = max(1, _max_contracts * int(SPX_TO_SPY_POINTS))
    if m_qty > _spy_cap:
        logger.info("spy_mirror qty %d clipped to %d (FEF_MAX_CONTRACTS_PER_"
                    "TRADE=%d x 10)", m_qty, _spy_cap, _max_contracts)
        m_qty = _spy_cap

    # SPY firing lock (review Finding 11): the mirror's duplicate/cap checks
    # + placement run under the same per-symbol lock maybe_auto_trade uses,
    # so a concurrent SPY scanner fire (if ever re-enabled) can't twin-parent
    # the contract. Ordering is always SPX lock -> SPY lock (the scanner's
    # SPY path never acquires SPX inside SPY), so no deadlock cycle exists.
    import asyncio as _asyncio
    locks = getattr(state, "auto_trade_symbol_locks", None)
    if locks is None:
        locks = {}
        try:
            state.auto_trade_symbol_locks = locks  # type: ignore[attr-defined]
        except Exception:
            pass
    spy_lock = locks.setdefault(MIRROR_SYMBOL, _asyncio.Lock())
    async with spy_lock:
        return await _place_mirror_locked(
            state, details_base=details_base, direction=direction,
            right=right, m_qty=m_qty, spx_qty=spx_qty, spy_expiry=spy_expiry,
            spx_spot=spx_spot, spx_spot_src=spx_spot_src, spy_spot=spy_spot,
            spot_src=spot_src, ratio=ratio, spy_strike=spy_strike,
            strategy=strategy, spx_t1=spx_t1, spx_t2=spx_t2, spx_t3=spx_t3,
            spx_stop=spx_stop, entry_timeout_sec=entry_timeout_sec,
            spx_parent_id=spx_parent_id, source=source,
            spx_strike=spx_strike, spx_entry_ask=spx_entry_ask,
        )


async def _place_mirror_locked(
    state, *, details_base: dict, direction: str, right: str, m_qty: int,
    spx_qty: int, spy_expiry: str, spx_spot: float, spx_spot_src: str,
    spy_spot: float, spot_src: str, ratio: float, spy_strike: float,
    strategy: str, spx_t1: float | None, spx_t2: float | None,
    spx_t3: float | None, spx_stop: float | None,
    entry_timeout_sec: float | None, spx_parent_id: int | None, source: str,
    spx_strike: float, spx_entry_ask: float | None,
) -> dict:
    """Gate+place section of the mirror, serialized under the SPY firing
    lock. Same fail-soft contract as maybe_place_spy_mirror."""

    # Duplicate-parent guard on the SPY contract (mirror of auto_trader
    # gate 10): never stack a second active parent on one contract.
    try:
        from sqlmodel import select as _sm_select
        from fef_sidecar.db.engine import get_session as _gs
        from fef_sidecar.db.models import LiveOrder as _LO
        async with _gs() as session:
            existing = (await session.exec(
                _sm_select(_LO)
                .where(_LO.symbol == MIRROR_SYMBOL)
                .where(_LO.strike == float(spy_strike))
                .where(_LO.right == right)
                .where(_LO.expiry == spy_expiry)
                .where(_LO.bracket_role == "parent")
                .where(_LO.status.in_(("SUBMITTED", "FILLED", "PARTIAL")))
            )).first()
        if existing is not None:
            await _audit_skip(
                "duplicate_parent",
                f"active parent already open at SPY {spy_strike:.0f}{right} "
                f"(id={existing.id})",
                {**details_base, "existing_parent_id": existing.id},
            )
            return {"ok": False, "skipped": "duplicate_parent"}
    except Exception:
        # Guard query failure: log + proceed (matches the SPX gate's own
        # fail-open on query error; the watcher's structural guards remain).
        logger.exception("spy_mirror duplicate-parent guard failed; proceeding")

    # Ladder in SPY price space / mirror-scale dollars.
    spy_t1 = spy_t2 = spy_t3 = spy_stop = None
    d_t1 = d_t2 = d_t3 = d_stop = None
    if strategy == "spx_points":
        if any(v is None for v in (spx_t1, spx_t2, spx_t3, spx_stop)):
            await _audit_skip(
                "no_spx_targets",
                f"SPX fire carried incomplete spx_points targets "
                f"(t1={spx_t1} t2={spx_t2} t3={spx_t3} stop={spx_stop})",
                details_base,
            )
            return {"ok": False, "skipped": "no_spx_targets"}
        spy_t1 = map_spy_level(spx_t1, spx_spot, spy_spot)
        spy_t2 = map_spy_level(spx_t2, spx_spot, spy_spot)
        spy_t3 = map_spy_level(spx_t3, spx_spot, spy_spot)
        spy_stop = map_spy_level(spx_stop, spx_spot, spy_spot)
    else:  # fixed_dollar
        n_scale = mirror_notional_scale(m_qty, spx_qty)
        d_t1 = scale_mirror_dollars(float(state.exit_dollar_t1), n_scale)
        d_t2 = scale_mirror_dollars(float(state.exit_dollar_t2), n_scale)
        d_t3 = scale_mirror_dollars(float(state.exit_dollar_t3), n_scale)
        d_stop = scale_mirror_dollars(float(state.exit_dollar_stop), n_scale)
        # Monotonic-ladder guard (review Finding 7): the $10 clamp floor can
        # collapse a small-notional ladder into equal targets — under
        # cumulative_realized semantics T2/T3 increments become $0 and the
        # whole mirror dumps at ~breakeven after T1. Skip instead.
        if not (d_t1 < d_t2 < d_t3):
            await _audit_skip(
                "degenerate_dollar_ladder",
                f"scaled $ ladder not strictly increasing "
                f"(T1/T2/T3={d_t1}/{d_t2}/{d_t3} at scale {n_scale:.4f}) — "
                f"clamp floor collapsed the exit design",
                details_base,
            )
            return {"ok": False, "skipped": "degenerate_dollar_ladder"}

    # Catastrophe cap at the mirror's own notional scale (~1/10 premia per
    # contract; position scale = mirror_notional_scale). Monitored by the
    # watcher's dollar-loss check independently of the SPX leg.
    cap_scale = mirror_notional_scale(m_qty, spx_qty)
    loss_cap = -abs(scale_mirror_dollars(
        float(os.environ.get("FEF_DOLLAR_LOSS_CAP", "1800")), cap_scale,
    ))

    # Marketable-limit entry off a FRESH SPY option quote (same policy as the
    # SPX leg: LMT at ask+buffer on SPY's penny grid, 45s timeout, never MKT).
    #
    # STREAMING-FIRST (2026-07-21 starvation fix): open a short-lived
    # reqMktData STREAMING subscription on the mapped SPY option and read the
    # ask from the ticker's memory — the one-shot snapshot fetch starved at
    # fire time (live 7/21: 6s re-fetch timeout while TWS quotes were healthy
    # on a free connection). Fail-soft: subscribe failure OR no fresh tick
    # within the bounded first-tick wait -> the EXISTING resolve_entry_ask
    # path (bounded one-shot fetch), then the EXISTING no_fresh_quote skip —
    # refusal semantics preserved exactly. The stream is ALWAYS closed here
    # (try/finally) BEFORE place_bracket runs, so at most one subscription
    # per contract is ever open (place_bracket's entry chase opens its own
    # stream for the SPY leg — double-subscribing one contract on one
    # connection can leak a market-data line on unsubscribe).
    entry_res = None
    _stream = None
    try:
        import asyncio as _aio

        from fef_sidecar.engine.entry_pricing import (
            EntryAskResolution,
            marketable_entry_limit,
        )
        from fef_sidecar.ibkr.models import Contract as _Contract
        from fef_sidecar.ibkr.models import ContractKind as _ContractKind
        from fef_sidecar.ibkr.quote_stream import (
            quote_stream_first_tick_wait_sec,
            quote_stream_qualify_timeout_sec,
        )
        _opener = getattr(state.ibkr, "open_option_quote_stream", None)
        if _opener is not None:
            # Outer bound = the provider's own qualify bound + margin, so even
            # a misbehaving provider cannot wedge the fire hook here.
            _stream = await _aio.wait_for(
                _opener(_Contract(
                    symbol=MIRROR_SYMBOL, kind=_ContractKind.OPTION,
                    exchange="SMART", expiry=spy_expiry,
                    strike=float(spy_strike), right=right,
                )),
                timeout=quote_stream_qualify_timeout_sec() + 1.0,
            )
            _deadline = time.monotonic() + quote_stream_first_tick_wait_sec()
            while True:
                _ask = _stream.fresh_ask()
                if _ask is not None:
                    _age = _stream.tick_age_sec()
                    entry_res = EntryAskResolution(
                        ask=float(_ask),
                        limit_price=marketable_entry_limit(
                            MIRROR_SYMBOL, float(_ask)),
                        source="stream",
                        age_ms=int((_age or 0.0) * 1000),
                        refetched=False,
                    )
                    break
                if time.monotonic() >= _deadline:
                    break  # no fresh tick in the window -> legacy fallback
                await _aio.sleep(0.05)
    except Exception:  # noqa: BLE001 — stream problems NEVER veto the mirror
        logger.exception(
            "spy_mirror quote stream failed — falling back to the snapshot "
            "re-fetch path")
        entry_res = None
    finally:
        if _stream is not None:
            try:
                _stream.close()
            except Exception:  # noqa: BLE001 — close must never mask the fire
                logger.exception("spy_mirror quote stream close failed")

    # Legacy path (unchanged): bounded one-shot fetch via resolve_entry_ask
    # (opt={} routes straight to the live re-fetch); no usable ask -> skip.
    if entry_res is None:
        try:
            from fef_sidecar.engine.entry_pricing import resolve_entry_ask
            entry_res = await resolve_entry_ask(
                state, symbol=MIRROR_SYMBOL, strike=float(spy_strike), right=right,
                expiry=spy_expiry, opt={},
            )
        except Exception as e:  # noqa: BLE001
            await _audit_skip("entry_quote_failed", f"entry ask resolve raised: {e}",
                              details_base)
            return {"ok": False, "skipped": "entry_quote_failed"}
    if entry_res.limit_price is None:
        await _audit_skip(
            "no_fresh_quote",
            f"no usable SPY option ask at {spy_strike:.0f}{right} "
            f"({entry_res.error})",
            {**details_base, "spy_strike": spy_strike},
        )
        return {"ok": False, "skipped": "no_fresh_quote"}

    # Premium plausibility vs the SPX leg's own fire ask (review Finding 6:
    # resolve_entry_ask has no chase bound with opt={}; the validated map
    # puts the SPY premium at ~1/9.3-1/9.4 of SPX — a resolved ask far off
    # that is a mismapped strike / garbage / delayed quote).
    if (isinstance(spx_entry_ask, (int, float)) and math.isfinite(spx_entry_ask)
            and spx_entry_ask > 0):
        expected = float(spx_entry_ask) / SPX_TO_SPY_POINTS
        if not (_PREMIUM_RATIO_MIN * expected
                <= float(entry_res.ask)
                <= _PREMIUM_RATIO_MAX * expected):
            await _audit_skip(
                "premium_implausible",
                f"SPY ask {entry_res.ask:.2f} outside "
                f"[{_PREMIUM_RATIO_MIN}, {_PREMIUM_RATIO_MAX}] x SPX ask/10 "
                f"({expected:.2f}) — mismapped strike or bad quote",
                {**details_base, "spy_strike": spy_strike,
                 "spy_ask": entry_res.ask, "spx_entry_ask": spx_entry_ask},
            )
            return {"ok": False, "skipped": "premium_implausible"}

    t1_q, t2_q, t3_q = mirror_tier_split(m_qty)
    entry_limit = float(entry_res.limit_price)

    # ENTRY ORDER TYPE (2026-07-22 operator revert — see UIConfig.
    # spx_entry_order_type): the mirror follows the SPX book's setting.
    # "market" -> static MKT via the existing use_market_entry path (SPY
    # fills trivially on its penny grid; no chase, no chase-side stream) —
    # the ask resolution + premium-plausibility gate above still ran, so a
    # mismapped strike can never be market-bought blind; entry_limit becomes
    # informational. Same no-naked-MKT invariant as the SPX leg: timeout
    # disabled -> stay on chase_limit behavior.
    _use_mkt = False
    try:
        from fef_sidecar.config_store import load_config as _load_cfg_entry
        _use_mkt = _load_cfg_entry().spx_entry_order_type == "market"
    except Exception:
        logger.exception(
            "spx_entry_order_type config read failed — mirror using chase_limit")
    if _use_mkt and not (entry_timeout_sec and entry_timeout_sec > 0):
        _use_mkt = False
        logger.warning(
            "spy_mirror: spx_entry_order_type='market' DOWNGRADED to "
            "chase_limit (entry timeout disabled: %s) — no MKT without the "
            "watchdog (2026-07-16 invariant).", entry_timeout_sec,
        )
    _entry_desc = ("MKT" if _use_mkt else f"LMT ${entry_limit:.2f}")

    from fef_sidecar.engine.live_trading import BracketSpec, place_bracket
    spec = BracketSpec(
        symbol=MIRROR_SYMBOL, strike=float(spy_strike), right=right,
        expiry=spy_expiry, qty=m_qty,
        entry_limit=entry_limit,
        # Watcher-managed strategies place parent-only; option-price tier
        # limits are unused filler (same convention as the SPX leg).
        t1_limit=entry_limit, t2_limit=entry_limit,
        t3_limit=entry_limit, stop_limit=entry_limit,
        t1_qty=t1_q, t2_qty=t2_q, t3_qty=t3_q,
        exit_strategy=strategy,
        spx_t1=spy_t1, spx_t2=spy_t2, spx_t3=spy_t3, spx_stop=spy_stop,
        d_t1=d_t1, d_t2=d_t2, d_t3=d_t3, d_stop=d_stop,
        # BE-ladder / plan anchor in SPY points (the watcher's per-symbol
        # spot machinery evaluates this parent against SPY's own spot).
        entry_spx_price=float(spy_spot) if strategy == "spx_points" else None,
        dollar_loss_cap=loss_cap,
        use_market_entry=_use_mkt,
        entry_timeout_sec=entry_timeout_sec,
    )

    try:
        result = await place_bracket(
            state.ibkr, spec, confirm=True, source=source,
            signal_ref={
                "mirror_of": spx_parent_id,
                "direction": direction,
                "spx_strike": spx_strike,
                "spx_spot": spx_spot,
                "spy_spot": spy_spot,
                "spy_spot_source": spot_src,
                "entry_ask": entry_res.ask,
                "entry_ask_source": entry_res.source,
            },
            state=state,
            mirror_of=spx_parent_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("spy_mirror place_bracket failed")
        await _audit_skip("place_error", f"place_bracket raised: {e}", details_base)
        return {"ok": False, "skipped": "place_error"}

    if not result.get("ok"):
        await _audit_skip(
            "place_refused",
            f"place_bracket refused: {result.get('error', 'unknown')}",
            {**details_base, "result": {k: result.get(k) for k in ("error", "refused_safety_gate")}},
        )
        return {"ok": False, "skipped": "place_refused", "result": result}

    # Budget accounting — the mirror consumes a SPY session-cap slot (and the
    # entry-timeout stand-down releases exactly this slot on a zero fill).
    now_ms = int(time.time() * 1000)
    try:
        state.auto_trade_count_by_symbol[MIRROR_SYMBOL] = (
            state.auto_trade_count_by_symbol.get(MIRROR_SYMBOL, 0) + 1
        )
        state.auto_trade_last_at_ms_by_symbol[MIRROR_SYMBOL] = now_ms
        state.auto_trade_count_session += 1
    except Exception:
        logger.exception("spy_mirror counter bump failed")

    try:
        from fef_sidecar.db.repository import SignalIterRepository
        ladder_desc = (
            f"SPY T1/T2/T3/STOP={spy_t1}/{spy_t2}/{spy_t3}/{spy_stop}"
            if strategy == "spx_points"
            else f"$ T1/T2/T3/STOP={d_t1}/{d_t2}/{d_t3}/{d_stop}"
        )
        await SignalIterRepository.write_audit_log(
            category="spy_mirror", actor="system",
            summary=(
                f"SPY MIRROR PLACED: BUY {m_qty} SPY {spy_strike:.0f}{right} "
                f"{_entry_desc} (mirror_of SPX parent {spx_parent_id}; "
                f"map: SPX K{spx_strike:.0f} @ spot {spx_spot:.2f} -> SPY spot "
                f"{spy_spot:.2f} [{spot_src}], ratio {ratio:.3f}). "
                f"{ladder_desc}; split {t1_q}/{t2_q}/{t3_q}; "
                f"loss cap {loss_cap:+.0f}"
            ),
            details={
                **details_base,
                "mirror_parent_id": result.get("parent_order_id"),
                "spy_strike": spy_strike, "spy_expiry": spy_expiry,
                "qty": m_qty, "tier_split": [t1_q, t2_q, t3_q],
                "entry_limit": entry_limit, "entry_ask": entry_res.ask,
                "spx_spot": spx_spot, "spx_spot_source": spx_spot_src,
                "spy_spot": spy_spot, "spy_spot_source": spot_src,
                "spx_entry_ask": spx_entry_ask,
                "spot_ratio": round(ratio, 4),
                "spy_t1": spy_t1, "spy_t2": spy_t2, "spy_t3": spy_t3,
                "spy_stop": spy_stop,
                "d_t1": d_t1, "d_t2": d_t2, "d_t3": d_t3, "d_stop": d_stop,
                "dollar_loss_cap": loss_cap,
            },
        )
    except Exception:
        logger.exception("spy_mirror placed-audit write failed")

    logger.warning(
        "SPY MIRROR FIRED: %s %d SPY %s%.0f %s (mirror_of=%s)",
        direction, m_qty, right, spy_strike, _entry_desc, spx_parent_id,
    )
    return {"ok": True, "result": result, "spy_strike": spy_strike, "qty": m_qty}
