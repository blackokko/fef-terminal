"""Auto-trade decision engine.

Called from _run_one_iter AFTER the FEF verdict is computed. Checks every
gate in order:

  1. auto_trade_enabled
  2. Live trading env (FEF_LIVE_TRADING=true)
  3. Verdict is actionable (LONG_CALLS or LONG_PUTS, not WAIT)
  4. Confidence >= floor (config_store.auto_trade_confidence_floor —
     ALWAYS ON; wall-collapse badges are re-graded through the formula)
  5. Calendar pause window not active
  6. Cool-down elapsed since last auto fire
  7. Session fire-cap not exceeded
  8. Verdict has option_pricing (strike + premium + targets)
  9. Risk caps not breached (daily loss / max position)
  9.5 Fresh ask resolvable for the marketable-LMT entry (2026-07-16:
     entries are LMT at ask+buffer with a 45s cancel timeout, never MKT;
     stale quote re-fetched once, else NO_FRESH_QUOTE block)
 10. No duplicate active parent on the same contract
 11. Cross-symbol direction cap (no same-right watcher parent open on
     another index-complex symbol)

If ANY gate fails, returns a reason string (logged + exposed via
state.auto_trade_last_block_reason). If ALL pass, calls
place_bracket(confirm=True) and bumps counters.

Designed so a misconfiguration can only PREVENT trades, never accidentally
permit them — defaults are restrictive, every gate must explicitly pass.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


_CONFIDENCE_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


class BlockReason:
    """Canonical block reason codes — written alongside the human reason
    string so DailyReview.by_block_reason aggregates cleanly without
    fuzzy string matching."""
    DISABLED = "DISABLED"
    ENV_NOT_SET = "ENV_NOT_SET"
    VERDICT_WAIT = "VERDICT_WAIT"
    TRIGGER_NOT_ENABLED = "TRIGGER_NOT_ENABLED"
    BELOW_FLOOR = "BELOW_FLOOR"
    CALENDAR_PAUSE = "CALENDAR_PAUSE"
    COOL_DOWN = "COOL_DOWN"
    GLOBAL_CAP = "GLOBAL_CAP"
    SYMBOL_CAP = "SYMBOL_CAP"
    NO_OPTION_PRICING = "NO_OPTION_PRICING"
    INVALID_PRICING = "INVALID_PRICING"
    NO_STRIKE = "NO_STRIKE"
    PLACE_ERROR = "PLACE_ERROR"
    DUPLICATE_PARENT = "DUPLICATE_PARENT"
    PHASE_DISAGREE = "PHASE_DISAGREE"
    # Confidence floor re-armed 2026-07-08 (counterfactual-tagged skips).
    # BELOW_FLOOR above is the RETIRED env-gated code — kept so historical
    # audit rows still resolve; new floor skips use this code.
    CONFIDENCE_FLOOR_SKIP = "CONFIDENCE_FLOOR_SKIP"
    CROSS_SYMBOL_CAP_SKIP = "CROSS_SYMBOL_CAP_SKIP"
    # Marketable-limit entry (2026-07-16): no fresh ask could be resolved to
    # price the entry LMT (fire-time quote stale >10s AND the one-shot
    # re-fetch failed). NEVER falls back to a market order — the 7/16 MKT
    # entry hang (live_order 776, 4m20s rest, $8.10 vs ~$5.50-6.00) is the
    # incident this gate exists to prevent.
    NO_FRESH_QUOTE = "NO_FRESH_QUOTE"
    FIRED = "FIRED"
    # Non-auto-trader codes for other gate paths — used so all block-style
    # audit_log rows carry a structured tag for aggregation.
    OPTION_QUOTE_FAIL = "OPTION_QUOTE_FAIL"
    SAFE_SELL_NO_POSITION = "SAFE_SELL_NO_POSITION"
    SAFE_SELL_SHORT_BLOCKED = "SAFE_SELL_SHORT_BLOCKED"
    SAFE_SELL_CLAMPED = "SAFE_SELL_CLAMPED"
    SAFE_SELL_ERROR = "SAFE_SELL_ERROR"
    CREDIT_SPREAD_GATE_OFF = "CREDIT_SPREAD_GATE_OFF"
    ORDER_REFUSED = "ORDER_REFUSED"


def _verdict_trigger_type(v: dict) -> str:
    """Map a verdict's boolean flags to a named trigger category.
    Same logic used by daily_review's by_trigger aggregation.

    A verdict may carry an EXPLICIT trigger_type (pattern-trigger fires set
    'flag_bull' / 'flag_bear' / 'pattern_bull' / 'pattern_bear'); that wins.
    FEF engine verdicts never set the key, so their behavior is unchanged.
    New trigger types are NOT in the default enabled list — pattern fires
    stay blocked (TRIGGER_NOT_ENABLED) until the user applies a preset that
    enables them."""
    explicit = v.get("trigger_type")
    if isinstance(explicit, str) and explicit:
        return explicit
    aligned = bool(v.get("aligned"))
    wall = bool(v.get("wall_collapse_trigger"))
    if wall and aligned: return "confluence"
    if aligned: return "aligned_only"
    if wall: return "wall_only"
    return "other"


# The index-complex the cross-symbol direction cap treats as ONE correlated
# underlying (same-right entries across these are stacked exposure, not
# diversification).
_INDEX_COMPLEX_SYMBOLS = ("SPX", "SPY", "QQQ", "IWM")

# Statuses that mark a watcher-managed parent as still OPEN — mirrors the
# spx_target_watcher loop's own scan filter exactly (SUBMITTED/FILLED/PARTIAL;
# DRY_RUN/STOPPED/CLOSED/... fall outside and never gate).
_OPEN_PARENT_STATUSES = ("SUBMITTED", "FILLED", "PARTIAL")


def _floor_kill_switch_active() -> bool:
    """FEF_DISABLE_CONFIDENCE_FLOOR=true = the floor gate's ONLY off switch
    (fail-open). There is deliberately no quiet enable/disable knob — the
    floor is validated policy (12k-fire study: HIGH +37.81/ct OOS vs LOW
    -14.84, t=3.31) and turning it off must be loud (audited once/session)."""
    return os.environ.get("FEF_DISABLE_CONFIDENCE_FLOOR", "false").lower() in (
        "true", "1", "yes",
    )


async def _config_floor(state) -> str:
    """Hot-read the enforced floor from config_store — the ONE source of
    truth (state.auto_trade_confidence_floor is display-legacy; the panel
    toggle used to stomp it in memory, observed 3+ times live). Fails
    RESTRICTIVE (HIGH) so a corrupt config file can't silently open the
    volume knob.

    OUTAGE TRACE (2026-07-08 review F1): load_config() swallows an
    exists-but-invalid config.json into silent defaults — the gate would
    enforce HIGH forever with zero trace, and /auto_trade/status reads
    through the same silent default, so the outage is indistinguishable
    from a deliberate setting. Probe the FILE separately
    (config_file_status) and on exists-but-invalid say so LOUDLY once per
    session (audit + error log). A missing file (fresh install) stays
    quiet — that default is legitimate."""
    from fef_sidecar import config_store as _cs
    try:
        floor = str(_cs.load_config().auto_trade_confidence_floor).upper()
    except Exception:
        logger.exception("confidence-floor config read failed — defaulting HIGH")
        return "HIGH"
    try:
        status = _cs.config_file_status()
    except Exception:
        status = "ok"  # probe failure must never affect the read path
    if status == "invalid":
        await _audit_floor_config_invalid_once(state)
    return floor


async def _audit_floor_config_invalid_once(state) -> None:
    """One loud row per session when config.json EXISTS but cannot be
    parsed/validated — the enforced floor is the DEFAULT (HIGH), not an
    operator setting, and that distinction must be visible in the log
    (review F1). Session-keyed like the kill-switch marker."""
    from fef_sidecar.db.repository import SignalIterRepository
    try:
        from fef_sidecar.engine.chop_gate import session_date_et
        sess = session_date_et(int(time.time() * 1000))
    except Exception:
        sess = time.strftime("%Y-%m-%d")
    if getattr(state, "_floor_config_invalid_audited_session", None) == sess:
        return
    try:
        state._floor_config_invalid_audited_session = sess  # type: ignore[attr-defined]
    except Exception:
        pass
    logger.error(
        "CONFIG UNREADABLE: config.json exists but fails to parse/validate — "
        "confidence floor enforcing default HIGH (NOT an operator setting). "
        "Fix or delete the file."
    )
    try:
        await SignalIterRepository.write_audit_log(
            category="confidence_floor", actor="system",
            summary=(
                "CONFIG UNREADABLE — config.json exists but fails to parse/"
                "validate; enforcing default floor HIGH (NOT an operator "
                "setting; /auto_trade/status shows the same silent default). "
                "Fix or delete the file to clear this."
            ),
        )
    except Exception:
        logger.exception("floor config-invalid audit write failed")


async def _audit_floor_killswitch_once(state) -> None:
    """Loud fail-open marker, once per session, when the kill-switch env has
    the floor gate off. Session-keyed (not process-keyed) so a sidecar left
    running overnight re-warns each trading day."""
    from fef_sidecar.db.repository import SignalIterRepository
    try:
        from fef_sidecar.engine.chop_gate import session_date_et
        sess = session_date_et(int(time.time() * 1000))
    except Exception:
        sess = time.strftime("%Y-%m-%d")
    if getattr(state, "_floor_killswitch_audited_session", None) == sess:
        return
    try:
        state._floor_killswitch_audited_session = sess  # type: ignore[attr-defined]
    except Exception:
        pass
    logger.warning(
        "CONFIDENCE FLOOR DISABLED via FEF_DISABLE_CONFIDENCE_FLOOR — "
        "auto-trade is running WITHOUT the validated floor gate (fail-open)"
    )
    try:
        await SignalIterRepository.write_audit_log(
            category="confidence_floor", actor="system",
            summary=(
                "CONFIDENCE FLOOR DISABLED via FEF_DISABLE_CONFIDENCE_FLOOR — "
                "gate is fail-open this session. The floor is validated policy "
                "(12k-fire study: HIGH +37.81/ct OOS vs LOW -14.84, t=3.31); "
                "unset the env to re-arm."
            ),
        )
    except Exception:
        logger.exception("floor kill-switch audit write failed")


def _default_config_qty() -> int:
    """The validated UIConfig default for auto_trade_qty_override (3) — read
    from the model so the gate's fallback size can't drift from the field
    definition. Fails to 3 if config_store is unimportable."""
    try:
        from fef_sidecar.config_store import UIConfig
        return int(UIConfig().auto_trade_qty_override)
    except Exception:
        return 3


async def _config_qty(state) -> int:
    """Hot-read the persistent contracts-per-fire from config_store — the ONE
    source of truth (state.auto_trade_qty_override is display-legacy; the panel
    toggle/preset used to set it in memory only, so it reset to 0 on restart
    while floor/symbols persisted). Mirrors _config_floor exactly.

    The RETIRED resolution (`override if >0 else suggested_qty or 15`) placed
    15 contracts on a scanner fire (suggested_qty=None) with override reset to
    0 — 5x the operator's 3-lot size, against which the -$1,800 cap and all
    exit geometry are calibrated (2026-07-09 near-miss, caught pre-open). There
    is NO `or 15` any more: if no valid operator size resolves, fall back to
    the validated config DEFAULT (3) and AUDIT it loudly once per session so a
    config outage sizing at the default is visible in the log — never 15.

    A valid UIConfig always carries 1..100, so the normal path returns the
    operator's size. The fallback fires only when load_config()/the read
    RAISES, when the resolved value is somehow out of 1..100, or when the file
    EXISTS-but-invalid (load_config silently returned defaults — the same F1
    outage class the floor audits)."""
    from fef_sidecar import config_store as _cs
    try:
        qty = int(_cs.load_config().auto_trade_qty_override)
    except Exception:
        logger.exception("auto-trade qty config read failed — using default")
        fallback = _default_config_qty()
        await _audit_qty_fallback_once(state, fallback, "config read raised")
        return fallback
    if qty < 1 or qty > 100:
        fallback = _default_config_qty()
        await _audit_qty_fallback_once(
            state, fallback, f"resolved qty {qty!r} outside 1..100",
        )
        return fallback
    try:
        status = _cs.config_file_status()
    except Exception:
        status = "ok"  # probe failure must never affect the read path
    if status == "invalid":
        # config.json exists but fails to parse/validate -> load_config()
        # silently returned the DEFAULT (qty here == the default), NOT an
        # operator setting. Say so loudly once (mirrors the floor's F1 audit)
        # and use the default.
        await _audit_qty_fallback_once(
            state, qty, "config.json exists but is invalid — enforcing default",
        )
    return qty


async def _audit_qty_fallback_once(state, qty: int, reason: str) -> None:
    """One loud row per session when the auto-trade size falls back to the
    config DEFAULT instead of a resolved operator setting. The retired `or 15`
    fallback would have placed 15 contracts on this path (5x size); now the
    fallback is the validated default and it is AUDITED so a silent config
    outage sizing at the default is visible (2026-07-09). Session-keyed like
    the floor audits."""
    from fef_sidecar.db.repository import SignalIterRepository
    try:
        from fef_sidecar.engine.chop_gate import session_date_et
        sess = session_date_et(int(time.time() * 1000))
    except Exception:
        sess = time.strftime("%Y-%m-%d")
    if getattr(state, "_qty_fallback_audited_session", None) == sess:
        return
    try:
        state._qty_fallback_audited_session = sess  # type: ignore[attr-defined]
    except Exception:
        pass
    logger.error(
        "QTY FALLBACK — using default %d contracts/fire (%s); NOT an operator "
        "setting (the retired `or 15` fallback would have sized 5x here). Set "
        "auto_trade_qty_override via POST /config.", qty, reason,
    )
    try:
        await SignalIterRepository.write_audit_log(
            category="auto_trade_qty", actor="system",
            summary=(
                f"QTY FALLBACK — using default {qty} contracts/fire ({reason}); "
                f"NOT an operator setting (the retired `or 15` fallback would "
                f"have sized 5x here). Set auto_trade_qty_override via POST "
                f"/config to clear this."
            ),
            details={"qty": qty, "reason": reason},
        )
    except Exception:
        logger.exception("qty fallback audit write failed")


def _tier_split(qty: int) -> tuple[int, int, int]:
    """30/40/30 T1/T2/T3 contract split (weight toward the 2.5R T2).

    For qty < 3 there aren't enough lots for three tiers — fill T1, then T2,
    leaving the rest at 0 (qty=1 -> (1,0,0); qty=2 -> (1,1,0)). A 0-qty tier is
    the ONLY valid "skip this tier" marker: every watcher tier-gate is
    `not fired and p.spx_target_tN_qty` (0 is falsy -> never evaluates/fires,
    no _market_sell(0) churn) and the CLOSED-sync banks on SOLD qty vs original
    (_all_tiers_banked) + all-NON-ZERO-tiers-fired, so a 0-qty tier neither
    orphans a runner nor deadlocks CLOSED. The parts always sum to qty.

    The retired `max(1, round(q*.3))/.4/rest` split produced (0,1,1) for qty=2
    (T1 silently never fires — no first partial) and (-1,1,1) for qty=1 (a
    NEGATIVE t1_qty is truthy -> T1 fires every 0.1s, sells -1 = noop, reverts
    the flag -> ~20 DB writes/sec for the life of the position). FEF_MAX_
    CONTRACTS_PER_TRADE=1|2 clips ANY size into the same degeneracy; this
    function makes all of those safe. For qty >= 3 it is the SAME 30/40/30
    formula as before (behavior byte-unchanged)."""
    q = int(qty)
    if q <= 0:
        return (0, 0, 0)
    if q == 1:
        return (1, 0, 0)
    if q == 2:
        return (1, 1, 0)
    # qty >= 3: historical 30/40/30 with the remainder on T3 (each tier >= 1,
    # parts summing to qty). Unchanged from the pre-2026-07-09 split.
    t1 = max(1, round(q * 0.30))
    t2 = max(1, round(q * 0.40))
    t3 = max(1, q - t1 - t2)
    if t1 + t2 + t3 != q:
        t1 = q - t2 - t3
    return (t1, t2, t3)


def _threshold_scale_for(state, sym: str, source: str) -> float:
    """The threshold_scale of the engine that PRODUCED the verdict, so the
    wall-collapse re-grade runs the formula at the verdict's own calibration.
    Scanner verdicts come from per-symbol engines; everything else from the
    main engine. Both are built by state._make_engine (same scale unless
    FEF_SCAN_THRESHOLD_SCALE_SEC opts the scanner out)."""
    eng = None
    if source == "auto_scanner":
        eng = (getattr(state, "scanner_engines", None) or {}).get(sym)
    if eng is None:
        eng = getattr(state, "engine", None)
    try:
        return float(getattr(eng, "_threshold_scale", 1.0) or 1.0)
    except Exception:
        return 1.0


def _wall_collapse_regrade(
    state, verdict_dict: dict, sym: str, source: str,
) -> tuple[str, str | None]:
    """Formula grade for a wall-collapse verdict, for the FLOOR decision.

    fef.py stamps every wall-collapse fire confidence=HIGH, bypassing
    _classify_confidence — a badge the studies show it does not earn
    (wc fires -1.56/ct OOS vs formula-HIGH +27.88/ct). At gate time we
    recompute what the ALIGNED formula would have graded this exact fire
    (same delta_price/delta_drift_m/fe_ratio + the same bias demotion,
    imported from fef.py — never copied) and gate on THAT.

    Returns (grade, note). note is set when the recompute differs from the
    stored badge (audit-noted by the caller). Missing recompute inputs
    grade LOW — fail-restrictive, matching this module's design rule."""
    from fef_sidecar.engine.fef import formula_confidence

    stored = str(verdict_dict.get("confidence", "NONE")).upper()
    dp = verdict_dict.get("delta_price")
    dd = verdict_dict.get("delta_drift_m")
    fe = verdict_dict.get("fe_ratio")
    if any(v is None or not isinstance(v, (int, float)) for v in (dp, dd, fe)):
        return "LOW", (
            f"wall-collapse re-grade inputs missing "
            f"(dp={dp!r}, dd={dd!r}, fe={fe!r}) -> LOW (fail-restrictive), "
            f"stored badge {stored}"
        )
    grade = formula_confidence(
        float(dp), float(dd), float(fe),
        direction=verdict_dict.get("direction", "WAIT"),
        drift_bias=verdict_dict.get("drift_bias"),
        threshold_scale=_threshold_scale_for(state, sym, source),
    ).value
    note = (
        f"wall-collapse badge re-graded: stored {stored} -> formula {grade}"
        if grade != stored else None
    )
    return grade, note


async def _tag_signal_skip(
    signal_iter_id: int | None, skipped_by: str, suffix: str,
) -> None:
    """Counterfactual marker on the ALREADY-persisted signal row (callers
    write the row before maybe_auto_trade): reasoning gets the human-visible
    skip suffix, extras.skipped_by gets the machine tag — the same contract
    the scanner gates (chop/bias/fomc/tod) follow, so the lookahead worker
    scores floor/cap skips like any other withheld fire. Never raises."""
    if signal_iter_id is None:
        return
    try:
        from sqlmodel import select as _sm_select
        from fef_sidecar.db.engine import get_session as _gs
        from fef_sidecar.db.models import SignalIter as _SI
        async with _gs() as session:
            row = (await session.exec(
                _sm_select(_SI).where(_SI.id == signal_iter_id)
            )).first()
            if row is None:
                return
            if suffix and suffix not in (row.reasoning or ""):
                row.reasoning = f"{row.reasoning or ''} {suffix}".strip()
            # Replace (never mutate) the JSON column — SQLAlchemy JSON does
            # not change-track in-place mutation.
            row.extras = {**(row.extras or {}), "skipped_by": skipped_by}
            session.add(row)
    except Exception:
        logger.exception(
            "counterfactual tag write failed for signal %s (%s)",
            signal_iter_id, skipped_by,
        )


# Scanner-verdict freshness bound for the entry-ref fallback — matches the
# watcher's _shadow_cash_spot scanner_snapshot freshness window.
_ENTRY_REF_VERDICT_MAX_AGE_MS = 180_000


def _entry_spx_ref(state, sym: str, opt: dict | None) -> float | None:
    """SYMBOL-CORRECT entry spot reference for a spx_points parent
    (2026-07-23 incident B): the old `entry_spx_price=state.last_spx_price`
    stamped the ACTIVE-CHART tick — an ES morning put ES-priced ~7473/7485/
    7464 entry refs (+~35pt vs cash) on every SPX parent, which (a) made
    _maybe_reanchor_plan rewrite CORRECT put plans onto the polluted anchor
    and (b) silently inflated the call BE-ladder risk ~4x, disabling every
    step (parent 796 rode to the full stop, -$1,770).

    Preference order (freshest symbol-correct source wins):
      1. option_pricing.underlying_price_at_quote — gate-8-validated, the
         same anchor the SPY mirror trusts as PRIMARY;
      2. the scanner's own per-symbol verdict spot (<=180s old) — the SAME
         value the plan's T1/T2/T3/stop were built from;
      3. the active-chart tick, ONLY when the active symbol IS this symbol;
      4. None — the watcher's plan-sanity/BE-ladder paths treat a missing
         entry ref as "nothing to validate yet", which is strictly safer
         than a cross-symbol one.
    """
    try:
        u = float((opt or {}).get("underlying_price_at_quote") or 0)
        if u > 0:
            return u
    except (TypeError, ValueError):
        pass
    try:
        v = (getattr(state, "last_scanner_verdict_by_symbol", None) or {}).get(sym)
        if v:
            ts = int(v.get("timestamp_ms") or 0)
            px = float(v.get("spx_price") or 0.0)
            if px > 0 and (ts == 0
                           or int(time.time() * 1000) - ts <= _ENTRY_REF_VERDICT_MAX_AGE_MS):
                return px
    except (TypeError, ValueError):
        pass
    if getattr(state, "active_symbol", None) == sym:
        px = getattr(state, "last_spx_price", 0) or 0
        if px > 0:
            return float(px)
    return None


@dataclass(frozen=True)
class AutoTradeDecision:
    fired: bool
    reason: str
    reason_code: str = BlockReason.FIRED
    order_result: dict | None = None


async def maybe_auto_trade(
    state, verdict_dict: dict, *, symbol: str | None = None,
    source: str = "auto_main", signal_iter_id: int | None = None,
) -> AutoTradeDecision:
    """Evaluate gates + maybe place a bracket order.

    verdict_dict: the verdict from _run_one_iter (with direction, confidence,
    strike_rec, option_pricing, suggested_qty, t1/t2/t3/stop).

    symbol: optional override. When provided (from multi-symbol scanner),
    counters check both the GLOBAL session cap AND the per-symbol sub-cap.

    source: which signal path is asking — 'auto_main' (fef_tick_loop),
    'auto_scanner' (multi-symbol scanner), 'auto_pattern' (pattern trigger).
    Passed through to place_bracket for end-to-end trade attribution.
    NOTE: cool-down + session caps are PER-SYMBOL and shared ACROSS sources —
    a pattern fire can be cool-down-blocked by a recent scanner fire on the
    same symbol (and vice versa). That is intentional risk throttling.

    signal_iter_id: the caller's already-persisted SignalIter row for this
    verdict. Counterfactual gates (confidence floor, cross-symbol cap) tag
    that row (extras.skipped_by + reasoning suffix) when they block, so the
    withheld fire stays scoreable in signal history. None = no tagging
    (gates still block; only the row marker is skipped).

    Serialized per symbol via an asyncio.Lock: two near-simultaneous calls
    (e.g. scanner + tick-loop, or a scanner retry racing itself — the 7/1
    twin QQQ 720P parents 16ms apart) can no longer both pass the cool-down
    /cap/duplicate-parent gates before either records its fire."""

    sym = (symbol or state.active_symbol).upper()

    # Gate 1: toggle
    if not state.auto_trade_enabled:
        return AutoTradeDecision(False, "auto_trade disabled", BlockReason.DISABLED)

    # Per-symbol firing lock (see docstring). Cheap gates run inside it too —
    # simpler and this path is low-frequency. Locks live on state so all
    # callers (main loop / scanner / pattern trigger) share them.
    import asyncio as _asyncio
    locks = getattr(state, "auto_trade_symbol_locks", None)
    if locks is None:
        locks = {}
        state.auto_trade_symbol_locks = locks  # type: ignore[attr-defined]
    lock = locks.setdefault(sym, _asyncio.Lock())
    async with lock:
        return await _maybe_auto_trade_locked(
            state, verdict_dict, sym, source, signal_iter_id,
        )


async def _maybe_auto_trade_locked(
    state, verdict_dict: dict, sym: str, source: str,
    signal_iter_id: int | None = None,
) -> AutoTradeDecision:

    # Gate 2: live trading env
    from fef_sidecar.engine.live_trading import is_live_trading_enabled
    if not is_live_trading_enabled():
        return AutoTradeDecision(False, "FEF_LIVE_TRADING env not set", BlockReason.ENV_NOT_SET)

    # Gate 3: actionable verdict
    direction = verdict_dict.get("direction", "WAIT")
    if direction not in ("LONG_CALLS", "LONG_PUTS"):
        return AutoTradeDecision(False, f"verdict is {direction}", BlockReason.VERDICT_WAIT)

    # Gate 3b: phase detector (RIDE/FADE/WAIT direction refiner). SPX-only —
    # the live detector is fed on SPX in the 15s poll loop (see loop.py). Modes:
    #   'off'/'advisory' → no gating (advisory just logs its verdict).
    #   'active'         → VETO when a FRESH, actionable phase verdict DISAGREES
    #                      with the engine's direction. Stops wrong-way entries
    #                      (e.g. buying PUTS into an exhausting dump — the losing
    #                      side per the PUT-T1=1.9% finding). Fail-OPEN when the
    #                      phase is WAIT/stale/missing so a broken feed can't
    #                      silently disable auto-trade. Direction *flipping* (to
    #                      also capture the fade) is a later, validated increment.
    if getattr(state, "phase_mode", "off") == "active" and sym == "SPX":
        pv = getattr(state, "phase_last_verdict", None)
        if pv and pv.get("actionable"):
            phase_age_ms = int(time.time() * 1000) - int(pv.get("ts_ms", 0) or 0)
            if 0 <= phase_age_ms <= 90_000:
                phase_dir = pv.get("direction")
                if phase_dir and phase_dir != direction:
                    return AutoTradeDecision(
                        False,
                        f"phase {pv.get('state')}/{phase_dir} disagrees with {direction}",
                        BlockReason.PHASE_DISAGREE,
                    )

    # Gate 3.5: trigger type must be enabled in state.auto_trade_trigger_types.
    # Replaces the confidence_floor as the primary volume knob — today's data
    # showed confidence-floor was inverted (HIGH 14% win vs LOW 41%).
    # Trigger-type gating uses the actual engine flag (aligned, wall_collapse)
    # which we know separates winners from losers more cleanly.
    trigger_type = _verdict_trigger_type(verdict_dict)
    enabled_triggers = getattr(state, "auto_trade_trigger_types", ["confluence", "aligned_only"])
    if trigger_type not in enabled_triggers:
        return AutoTradeDecision(
            False,
            f"trigger '{trigger_type}' not in enabled {enabled_triggers}",
            BlockReason.TRIGGER_NOT_ENABLED,
        )

    # Gate 4: confidence floor — RE-ARMED 2026-07-08, ALWAYS ON. The May
    # demotion (env-gated FEF_USE_CONFIDENCE_FLOOR) reacted to a single-day
    # HIGH/LOW inversion; the 12k-fire registered study reversed it decisively
    # OOS: floor=HIGH +37.81/ct vs LOW -14.84/ct (t=3.31), book -$142k ->
    # +$117k. The floor is read from config_store (persistent, hot) — NEVER
    # state.auto_trade_confidence_floor, which the panel toggle stomped in
    # memory 3+ times live. Only off switch: FEF_DISABLE_CONFIDENCE_FLOOR
    # (fail-open kill switch, loud audit once per session).
    #
    # WALL-COLLAPSE EXCLUSION: wall-collapse verdicts carry a hardcoded HIGH
    # badge that bypasses the formula (fef.py) and does not earn it (wc fires
    # -1.56/ct OOS vs formula-HIGH +27.88/ct) — re-grade them through the
    # exact ALIGNED formula (classify + bias demotion, imported from fef.py)
    # and gate on the recomputed grade.
    conf = verdict_dict.get("confidence", "NONE")
    if _floor_kill_switch_active():
        await _audit_floor_killswitch_once(state)
    else:
        floor = await _config_floor(state)
        effective_conf = str(conf).upper()
        regrade_note: str | None = None
        if verdict_dict.get("wall_collapse_trigger"):
            effective_conf, regrade_note = _wall_collapse_regrade(
                state, verdict_dict, sym, source,
            )
        if _CONFIDENCE_RANK.get(effective_conf, 0) < _CONFIDENCE_RANK.get(floor, 3):
            # Counterfactual, same contract as the scanner gates: signal row
            # stays in history tagged + suffixed, dedicated audit category,
            # structured block code. No cooldown consumed, no budget spent
            # (counters/timestamps only move on a successful fire).
            suffix = f"[FLOOR SKIPPED: {effective_conf} < {floor}]"
            await _tag_signal_skip(signal_iter_id, "confidence_floor", suffix)
            summary = (
                f"FLOOR SKIPPED {direction} {sym}: {effective_conf} < floor {floor}"
                + (f" ({regrade_note})" if regrade_note else "")
            )
            try:
                from fef_sidecar.db.repository import SignalIterRepository as _Repo_fl
                await _Repo_fl.write_audit_log(
                    category="confidence_floor", actor="system",
                    summary=summary,
                    details={
                        "source": source,
                        "symbol": sym,
                        "direction": direction,
                        "stored_confidence": conf,
                        "effective_confidence": effective_conf,
                        "floor": floor,
                        "wall_collapse_regraded": bool(regrade_note),
                        "strike_rec": verdict_dict.get("strike_rec"),
                        "signal_iter_id": signal_iter_id,
                    },
                    block_reason_code=BlockReason.CONFIDENCE_FLOOR_SKIP,
                )
            except Exception:
                logger.exception("confidence_floor audit write failed")
            return AutoTradeDecision(
                False,
                f"{effective_conf} below floor {floor}"
                + (" (wall-collapse re-graded)" if regrade_note else ""),
                BlockReason.CONFIDENCE_FLOOR_SKIP,
            )
        if regrade_note:
            # Passed the floor but the recomputed grade differs from the
            # stored badge — audit-note the divergence (rare: wall-collapse
            # fires only) so the badge inflation stays measurable.
            try:
                from fef_sidecar.db.repository import SignalIterRepository as _Repo_fl
                await _Repo_fl.write_audit_log(
                    category="confidence_floor", actor="system",
                    summary=(
                        f"WALL-COLLAPSE RE-GRADE (passed floor {floor}) "
                        f"{direction} {sym}: {regrade_note}"
                    ),
                    details={
                        "source": source, "symbol": sym, "direction": direction,
                        "stored_confidence": conf,
                        "effective_confidence": effective_conf,
                        "floor": floor,
                        "signal_iter_id": signal_iter_id,
                    },
                )
            except Exception:
                logger.exception("confidence_floor regrade-note write failed")

    # Gate 5: calendar pause window
    try:
        from fef_sidecar.engine.calendar import is_pause_window
        pause = await is_pause_window()
        if pause.paused:
            return AutoTradeDecision(False, f"calendar pause: {pause.reason}",
                                     BlockReason.CALENDAR_PAUSE)
    except Exception:
        pass

    # Gate 6: per-symbol cool-down. Each symbol's clock is independent so
    # SPX firing doesn't gate out a fresh QQQ signal. The global
    # auto_trade_last_at_ms is still updated for visibility but isn't a
    # blocking gate here (multi-symbol scanning needs per-symbol isolation).
    now_ms = int(time.time() * 1000)
    sym_last = state.auto_trade_last_at_ms_by_symbol.get(sym, 0)
    elapsed_sec = (now_ms - sym_last) / 1000
    if sym_last > 0 and elapsed_sec < state.auto_trade_cool_down_sec:
        remaining = state.auto_trade_cool_down_sec - int(elapsed_sec)
        return AutoTradeDecision(
            False, f"{sym} cool-down ({remaining}s remaining)", BlockReason.COOL_DOWN
        )

    # Gate 7: per-symbol session cap. Each symbol has its own budget so
    # SPX hitting its cap doesn't block fresh QQQ/SPY/IWM signals. The
    # implicit "global cap" = sum(per_symbol_cap for enabled symbols);
    # there's no separate global gate — the per-symbol cap is the only
    # binding constraint, which matches user intent (each symbol carries
    # its own independent trade budget).
    sym_count = state.auto_trade_count_by_symbol.get(sym, 0)
    if sym_count >= state.auto_trade_max_per_symbol:
        return AutoTradeDecision(
            False, f"{sym} cap reached ({state.auto_trade_max_per_symbol})",
            BlockReason.SYMBOL_CAP
        )

    # Gate 8: option pricing available — note NaN is truthy in Python, so
    # explicit isfinite() guard is required (IBKR silently rejects LMT orders
    # built from NaN entry_limit).
    import math as _math
    opt = verdict_dict.get("option_pricing")
    if not opt:
        return AutoTradeDecision(False, "no option pricing on verdict", BlockReason.NO_OPTION_PRICING)
    for k in ("entry_limit", "t1", "t2", "t3", "stop"):
        v = opt.get(k)
        if v is None or not isinstance(v, (int, float)) or not _math.isfinite(v) or v <= 0:
            return AutoTradeDecision(False, f"invalid option_pricing.{k}={v!r}",
                                     BlockReason.INVALID_PRICING)

    strike = verdict_dict.get("strike_rec")
    # QTY (2026-07-09): the PERSISTENT config value is the ONE source of truth.
    # NEVER state.auto_trade_qty_override (display-legacy; reset to 0 on restart
    # while floor/symbols persisted) and NEVER suggested_qty (scanner verdicts
    # carry None). The retired `override if >0 else suggested_qty or 15` sized a
    # fire at 15 contracts on that path — 5x the operator's 3-lot, against which
    # the -$1,800 cap + every exit geometry are calibrated (live near-miss).
    # _config_qty hot-reads config and falls back to the validated default (3)
    # with a loud once/session audit — never 15.
    qty = await _config_qty(state)
    if not strike:
        return AutoTradeDecision(False, "no strike_rec on verdict", BlockReason.NO_STRIKE)

    # Gate 9: risk caps — limit qty if it would breach max_contracts_per_trade
    max_contracts = int(os.environ.get("FEF_MAX_CONTRACTS_PER_TRADE", "20"))
    if qty > max_contracts:
        original_qty = qty
        qty = max_contracts
        logger.info("Auto-trade qty %d clipped to max_contracts_per_trade=%d", original_qty, max_contracts)

    # All gates passed — place the bracket
    from fef_sidecar.engine.live_trading import BracketSpec, place_bracket

    today = time.strftime("%Y%m%d")

    # Entry timeout (2026-07-16, paired with gate 9.5 below): specs carry the
    # 45s cancel deadline so place_bracket arms the watchdog on the live parent.
    from fef_sidecar.engine.entry_timeout import entry_timeout_sec as _entry_tmo
    entry_timeout = _entry_tmo()
    # 30/40/30 split — shift weight toward T2 (now 2.5R). At 41% win rate
    # (confluence), the math favors carrying more capital into the larger
    # R-multiples. _tier_split handles small qty (1->(1,0,0), 2->(1,1,0))
    # so a 1/2-lot fire (or a MAX_CONTRACTS clip to 1/2) can't produce a
    # zero/negative T1 tier; qty >= 3 is the unchanged 30/40/30.
    t1_qty, t2_qty, t3_qty = _tier_split(qty)

    # Honor the operator's chosen exit strategy at ANY size (freeze lifted 2026-06-30).
    # The old qty>=15 gate silently forced spx_points below 15 contracts, overriding
    # the panel -> the fixed-dollar profit exits never armed at live size (2-5 ct), so
    # gains were given back. The fixed_dollar $ targets are position-level and scale
    # per-contract, so they work fine at small qty (esp. now that strikes are near-ATM
    # with real delta). Falls back to spx_points only if nothing is set.
    strategy = getattr(state, "exit_strategy_long", None) or "spx_points"

    spx_t1 = verdict_dict.get("t1")
    spx_t2 = verdict_dict.get("t2")
    spx_t3 = verdict_dict.get("t3")
    spx_stop = verdict_dict.get("stop")
    # Use the scanner-provided symbol if present, else fall back to active
    trade_symbol = sym

    if strategy == "fixed_dollar":
        # WATCHER-MANAGED fixed_dollar (2026-06-30): pass the operator's $ targets
        # straight through to the watcher, which fires staged market sells as live
        # open P&L crosses each level. The old code translated $ → option-price
        # bracket children via `/ max(abs(delta), 0.05)`, which divided by the
        # option delta and turned a -$500 stop into a ~$1.35 (-$1640) stop. No
        # translation now — the watcher reads the $ targets directly, so there is
        # nothing to mis-scale. t1/t2/t3/stop option-limits are unused for this
        # strategy (parent-only placement) and left at entry as harmless filler.
        d_t1 = float(state.exit_dollar_t1)
        d_t2 = float(state.exit_dollar_t2)
        d_t3 = float(state.exit_dollar_t3)
        d_stop = float(state.exit_dollar_stop)
        # PER-SYMBOL $-EXIT SCALING (2026-07-02): the operator's $ targets are
        # calibrated on SPX-class notional. Applied raw to an ETF-class symbol
        # they're unreachable — $300 T1 on a 3-lot SPY = +$1.00/ct premium =
        # +50% on a ~$2 option ≈ 15-20 SPX-equivalent points (live exhibit:
        # today's SPY 743C — profitable, couldn't bank). Scale all four by the
        # SAME notional ratio the strike fix uses (_price_scale: spot/7500
        # below 2000; SPX-class pins to 1.0 → SPX behavior BIT-IDENTICAL).
        # Whole dollars, $10 minimum magnitude, sign preserved. Hot-read via
        # UIConfig.etf_dollar_exit_scaling; the entry audit line already
        # prints the ladder so the scaled numbers are visible per trade.
        try:
            from fef_sidecar.config_store import load_config as _load_cfg
            _etf_scaling = bool(_load_cfg().etf_dollar_exit_scaling)
        except Exception:
            logger.exception("etf_dollar_exit_scaling config read failed — defaulting ON")
            _etf_scaling = True
        if _etf_scaling:
            import math as _math_ds
            from fef_sidecar.engine.strikes import _price_scale
            _spot_ref = float(opt.get("underlying_price_at_quote") or 0) or float(strike)
            _scale = _price_scale(_spot_ref)
            if _scale != 1.0:
                def _scale_d(x: float) -> float:
                    return _math_ds.copysign(max(10.0, abs(round(x * _scale))), x)
                d_t1, d_t2, d_t3, d_stop = (
                    _scale_d(d_t1), _scale_d(d_t2), _scale_d(d_t3), _scale_d(d_stop)
                )
                logger.info(
                    "fixed_dollar $ targets scaled for %s (ref %.2f, scale %.4f): "
                    "T1/T2/T3/STOP=%s/%s/%s/%s",
                    sym, _spot_ref, _scale, d_t1, d_t2, d_t3, d_stop,
                )
        spec = BracketSpec(
            symbol=trade_symbol, strike=float(strike),
            right="C" if direction == "LONG_CALLS" else "P",
            expiry=today, qty=qty,
            # PLACEHOLDER: replaced with the marketable LMT (fresh ask +
            # buffer) by gate 9.5 just before placement. t1..stop option-
            # limits are unused for this strategy (parent-only placement)
            # and stay at the fire-quote entry_limit as filler.
            entry_limit=float(opt["entry_limit"]),
            t1_limit=float(opt["entry_limit"]), t2_limit=float(opt["entry_limit"]),
            t3_limit=float(opt["entry_limit"]), stop_limit=float(opt["entry_limit"]),
            t1_qty=t1_qty, t2_qty=t2_qty, t3_qty=t3_qty,
            exit_strategy="fixed_dollar",
            d_t1=d_t1, d_t2=d_t2,
            d_t3=d_t3, d_stop=d_stop,
            dollar_loss_cap=-abs(float(os.environ.get("FEF_DOLLAR_LOSS_CAP", "1800"))),
            entry_timeout_sec=entry_timeout,
        )
    elif strategy == "spx_points":
        # Auto-trade default: marketable-LMT entry (gate 9.5, was MKT until
        # 2026-07-16) + catastrophic dollar loss cap (FEF_DOLLAR_LOSS_CAP,
        # default -$1800 per user decision 2026-07-06 — sits beyond the -10pt
        # ladder stop, binds only on gap disasters) + BE ladder (initialized
        # via entry_spx_price captured from state).
        spec = BracketSpec(
            symbol=trade_symbol, strike=float(strike),
            right="C" if direction == "LONG_CALLS" else "P",
            expiry=today, qty=qty,
            # PLACEHOLDER — gate 9.5 swaps in the marketable LMT pre-placement.
            entry_limit=float(opt["entry_limit"]),
            t1_limit=float(opt["t1"]), t2_limit=float(opt["t2"]),
            t3_limit=float(opt["t3"]), stop_limit=float(opt["stop"]),
            t1_qty=t1_qty, t2_qty=t2_qty, t3_qty=t3_qty,
            exit_strategy="spx_points",
            spx_t1=spx_t1, spx_t2=spx_t2, spx_t3=spx_t3, spx_stop=spx_stop,
            # SYMBOL-CORRECT entry ref (2026-07-23 incident B) — never the
            # raw active-chart tick on a cross-symbol chart. See _entry_spx_ref.
            entry_spx_price=_entry_spx_ref(state, sym, opt),
            dollar_loss_cap=-abs(float(os.environ.get("FEF_DOLLAR_LOSS_CAP", "1800"))),
            entry_timeout_sec=entry_timeout,
        )
    else:  # 'option_price' (legacy default)
        spec = BracketSpec(
            symbol=trade_symbol, strike=float(strike),
            right="C" if direction == "LONG_CALLS" else "P",
            expiry=today, qty=qty,
            # PLACEHOLDER — gate 9.5 swaps in the marketable LMT pre-placement.
            entry_limit=float(opt["entry_limit"]),
            t1_limit=float(opt["t1"]), t2_limit=float(opt["t2"]),
            t3_limit=float(opt["t3"]), stop_limit=float(opt["stop"]),
            t1_qty=t1_qty, t2_qty=t2_qty, t3_qty=t3_qty,
            exit_strategy="option_price",
            dollar_loss_cap=-abs(float(os.environ.get("FEF_DOLLAR_LOSS_CAP", "1800"))),
            entry_timeout_sec=entry_timeout,
        )

    # PATCH 3: Refuse to open a new parent if an active parent already exists
    # for the same contract. Prevents stacked-parent cross-race in the watcher
    # (see spx_target_watcher safety patch). The 7-parent SPX 7520C race that
    # placed a naked short was created by this very gap.
    try:
        from sqlmodel import select as _sm_select
        from fef_sidecar.db.engine import get_session as _gs_dup
        from fef_sidecar.db.models import LiveOrder as _LO_dup
        from fef_sidecar.db.repository import SignalIterRepository as _Repo_dup

        _right_dup = "C" if direction == "LONG_CALLS" else "P"
        _expiry_dup = today
        async with _gs_dup() as _session_dup:
            _existing_dup = list((await _session_dup.exec(
                _sm_select(_LO_dup)
                .where(_LO_dup.symbol == sym)
                .where(_LO_dup.strike == float(strike))
                .where(_LO_dup.right == _right_dup)
                .where(_LO_dup.expiry == _expiry_dup)
                .where(_LO_dup.bracket_role == "parent")
                .where(_LO_dup.status.in_(("SUBMITTED", "FILLED", "PARTIAL")))
            )).all())
        if _existing_dup:
            await _Repo_dup.write_audit_log(
                category="auto_trade_block", actor="system",
                summary=(
                    f"BLOCKED {direction}: already have active parent at "
                    f"{sym} {strike}{_right_dup} (parent_id={_existing_dup[0].id})"
                ),
                details={"block_reason_code": BlockReason.DUPLICATE_PARENT,
                         "source": source},
                block_reason_code=BlockReason.DUPLICATE_PARENT,
            )
            return AutoTradeDecision(
                False,
                f"duplicate parent at {sym} {strike}{_right_dup} (id={_existing_dup[0].id})",
                BlockReason.DUPLICATE_PARENT,
            )
    except AutoTradeDecision:
        raise
    except Exception:
        # If the duplicate-parent guard itself errors, log + proceed (do not
        # block trading on a query failure). The structural HARD-BLOCK in
        # spx_target_watcher still prevents the bad outcome.
        logger.exception("Auto-trade duplicate-parent guard failed; proceeding")

    # CROSS-SYMBOL DIRECTION CAP (2026-07-08, user-approved OPERATIONAL RISK
    # RULE): refuse to open a same-right position while a watcher-managed
    # parent (SUBMITTED/FILLED/PARTIAL) is open on a DIFFERENT index-complex
    # symbol {SPX,SPY,QQQ,IWM} — SPX C + QQQ C is one correlated underlying
    # trade at double size, not two trades. Evidence is anecdote-grade
    # (+$1,380 across 2 sessions): shipped as EXPOSURE DISCIPLINE, not edge —
    # the counterfactual rows grade it in production like the scanner gates.
    # DORMANT when only SPX is enabled: same-symbol stacking stays governed
    # by the per-symbol cooldown/cap + the duplicate-parent guard above.
    # Runs LAST (after every other gate) so its counterfactual population is
    # exactly "fires that would otherwise have placed". Hot-read config flag;
    # fail-open on query error (same rationale as the duplicate guard).
    try:
        from fef_sidecar.config_store import load_config as _load_cfg_cap
        _cap_enabled = bool(_load_cfg_cap().cross_symbol_cap_enabled)
    except Exception:
        logger.exception("cross_symbol_cap config read failed — defaulting ON")
        _cap_enabled = True
    if _cap_enabled:
        try:
            from sqlmodel import select as _sm_select_cap
            from fef_sidecar.db.engine import get_session as _gs_cap
            from fef_sidecar.db.models import LiveOrder as _LO_cap
            from fef_sidecar.db.repository import SignalIterRepository as _Repo_cap

            _right_cap = "C" if direction == "LONG_CALLS" else "P"
            _other_syms = [s for s in _INDEX_COMPLEX_SYMBOLS if s != sym]
            async with _gs_cap() as _session_cap:
                _open_cap = (await _session_cap.exec(
                    _sm_select_cap(_LO_cap)
                    .where(_LO_cap.bracket_role == "parent")
                    .where(_LO_cap.exit_strategy.in_(("spx_points", "fixed_dollar")))
                    .where(_LO_cap.status.in_(_OPEN_PARENT_STATUSES))
                    .where(_LO_cap.right == _right_cap)
                    .where(_LO_cap.symbol.in_(_other_syms))
                    # SPY-mirror exclusion (2026-07-17): a mirror parent is BY
                    # DESIGN the same trade as its SPX parent at 1/10 scale —
                    # it is deliberate duplicate exposure, not an independent
                    # cross-symbol position. Without this, the first mirrored
                    # fire's open SPY parent would cross-symbol-block every
                    # subsequent same-right SPX fire (choking the verdict
                    # window on its own mirror).
                    .where(_LO_cap.mirror_of.is_(None))
                )).first()
            if _open_cap is not None:
                suffix = (
                    f"[CROSS-SYMBOL CAP SKIPPED: {_open_cap.symbol} "
                    f"{_open_cap.strike:.0f}{_open_cap.right} open]"
                )
                await _tag_signal_skip(signal_iter_id, "cross_symbol_cap", suffix)
                try:
                    await _Repo_cap.write_audit_log(
                        category="cross_symbol_cap", actor="system",
                        summary=(
                            f"CROSS-SYMBOL CAP SKIPPED {direction} {sym}: "
                            f"same-right parent open on {_open_cap.symbol} "
                            f"{_open_cap.strike:.0f}{_open_cap.right} "
                            f"(parent_id={_open_cap.id}, status={_open_cap.status})"
                        ),
                        details={
                            "source": source,
                            "symbol": sym,
                            "direction": direction,
                            "right": _right_cap,
                            "open_parent_id": _open_cap.id,
                            "open_parent_symbol": _open_cap.symbol,
                            "open_parent_strike": _open_cap.strike,
                            "open_parent_status": _open_cap.status,
                            "strike_rec": verdict_dict.get("strike_rec"),
                            "signal_iter_id": signal_iter_id,
                        },
                        block_reason_code=BlockReason.CROSS_SYMBOL_CAP_SKIP,
                    )
                except Exception:
                    logger.exception("cross_symbol_cap audit write failed")
                # No cooldown consumed, no budget spent — counters move only
                # on a successful fire.
                return AutoTradeDecision(
                    False,
                    f"cross-symbol cap: {_open_cap.symbol} "
                    f"{_open_cap.strike:.0f}{_open_cap.right} open "
                    f"(id={_open_cap.id})",
                    BlockReason.CROSS_SYMBOL_CAP_SKIP,
                )
        except Exception:
            logger.exception("Cross-symbol cap check failed; proceeding")

    # Gate 9.5 (2026-07-16): MARKETABLE-LIMIT ENTRY. Entries were raw MKT
    # orders; IBKR's price cap on market option orders RESTED live_order 776
    # for 4m20s on a running ask (filled $8.10 vs ~$5.50-6.00 at signal,
    # ~$750 hang-slippage at 3 lots). Entries are now LMT at (fresh ask +
    # buffer: max($0.10, 2% of ask) capped $0.50, tick-rounded UP). The ask
    # comes from the same fire-time quote gate 8 validated; stale (>10s) =
    # one bounded re-fetch, chase-bounded so a runaway tape can't repeat the
    # incident price at fill time; no usable ask = BLOCK (counterfactual-
    # tagged like the other gates) — NEVER fall back to MKT. Runs LAST,
    # after every cheap DB gate (dup-parent / cross-symbol keep their
    # documented counterfactual populations AND no IBKR round-trip is spent
    # on fires they'd reject), so the quote is at its freshest at placement.
    # Paired with the 45s entry timeout (entry_timeout.py).
    from dataclasses import replace as _dc_replace
    from fef_sidecar.engine.entry_pricing import resolve_entry_ask
    _right = "C" if direction == "LONG_CALLS" else "P"
    entry_res = await resolve_entry_ask(
        state, symbol=sym, strike=float(strike), right=_right,
        expiry=today, opt=opt,
    )
    if entry_res.limit_price is None:
        suffix = f"[NO_FRESH_QUOTE: {entry_res.error}]"
        await _tag_signal_skip(signal_iter_id, "no_fresh_quote", suffix)
        try:
            from fef_sidecar.db.repository import SignalIterRepository as _Repo_nfq
            await _Repo_nfq.write_audit_log(
                category="auto_trade_block", actor="system",
                summary=(
                    f"NO_FRESH_QUOTE {direction} {sym} {strike}{_right}: "
                    f"cannot price marketable-limit entry ({entry_res.error}) "
                    f"— fire blocked (no MKT fallback by design)"
                ),
                details={
                    "source": source, "symbol": sym, "direction": direction,
                    "strike_rec": strike, "signal_iter_id": signal_iter_id,
                    "quote_age_ms": entry_res.age_ms,
                    "refetched": entry_res.refetched,
                    "error": entry_res.error,
                },
                block_reason_code=BlockReason.NO_FRESH_QUOTE,
            )
        except Exception:
            logger.exception("no_fresh_quote audit write failed")
        return AutoTradeDecision(
            False, f"no fresh quote for entry limit ({entry_res.error})",
            BlockReason.NO_FRESH_QUOTE,
        )
    entry_limit_price = float(entry_res.limit_price)

    # ENTRY ORDER TYPE (2026-07-22 operator revert — full history on
    # UIConfig.spx_entry_order_type): "market" rides the EXISTING static-MKT
    # path — use_market_entry=True makes place_bracket's did_chase False, so
    # chase + streaming stay fully dormant (no subscription is even opened)
    # and _mk_market places the parent; the 45s entry-timeout watchdog arms
    # via the existing non-chase derivation (full budget, limit_price=None).
    # NEVER a naked MKT: with the timeout disabled (entry_timeout <= 0) we
    # refuse static-MKT and keep chase_limit behavior — the same
    # no-MKT-without-watchdog invariant the chase fallback enforces (the
    # 7/16 hang was a MKT with no watchdog; that combination stays
    # unreachable from this path). Gate 9.5 above still ran either way, so
    # "market" fires keep the fresh-quote block + the resolved ask in the
    # audit/signal_ref; entry_limit_price becomes informational on the row.
    _use_mkt = False
    try:
        from fef_sidecar.config_store import load_config as _load_cfg_entry
        _use_mkt = _load_cfg_entry().spx_entry_order_type == "market"
    except Exception:
        logger.exception(
            "spx_entry_order_type config read failed — using chase_limit")
    if _use_mkt and not (entry_timeout and entry_timeout > 0):
        _use_mkt = False
        logger.warning(
            "spx_entry_order_type='market' DOWNGRADED to chase_limit for this "
            "fire: entry timeout is disabled (entry_timeout_sec=%s) and a "
            "market order with no watchdog can hang on IBKR's price cap "
            "(2026-07-16). Re-enable FEF_ENTRY_TIMEOUT_SEC>0 to restore MKT "
            "entries.", entry_timeout,
        )
    spec = _dc_replace(spec, entry_limit=entry_limit_price,
                       use_market_entry=_use_mkt)

    try:
        result = await place_bracket(
            state.ibkr, spec, confirm=True,
            # Attribution: auto-trader orders must be distinguishable from
            # manual tickets in the audit log (actor='system' + AUTO-TRADE
            # prefix + details.source) — see place_bracket docstring.
            source=source,
            signal_ref={
                "direction": direction,
                "confidence": conf,
                "trigger_type": trigger_type,
                "strike_rec": strike,
                "reasoning": (verdict_dict.get("reasoning") or "")[:200],
                # For the entry-timeout stand-down's counterfactual tag —
                # same signal row the other gates tag on a block.
                "signal_iter_id": signal_iter_id,
                "entry_ask": entry_res.ask,
                "entry_ask_source": entry_res.source,
            },
            # Entry-timeout monitor needs state to release the per-symbol
            # session-cap slot on a zero-fill stand-down.
            state=state,
        )
    except Exception as e:
        logger.exception("Auto-trade place_bracket failed")
        return AutoTradeDecision(False, f"place_bracket error: {e}", BlockReason.PLACE_ERROR)

    # Bump counters on successful submission — global + per-symbol counts
    # AND per-symbol last-fire time (for the per-symbol cool-down clock).
    if result.get("ok"):
        state.auto_trade_last_at_ms = now_ms
        state.auto_trade_last_at_ms_by_symbol[sym] = now_ms
        state.auto_trade_count_session += 1
        state.auto_trade_count_by_symbol[sym] = state.auto_trade_count_by_symbol.get(sym, 0) + 1
        logger.warning(
            "AUTO-TRADE FIRED: %s %d %s %s%.0f %s (ask $%.2f %s, "
            "timeout %.0fs; session total %d, %s budget %d/%d)",
            direction, qty, sym,
            "C" if direction == "LONG_CALLS" else "P",
            strike,
            (f"MKT (ref ${entry_limit_price:.2f})" if _use_mkt
             else f"LMT ${entry_limit_price:.2f}"),
            entry_res.ask, entry_res.source,
            entry_timeout,
            state.auto_trade_count_session,
            sym, state.auto_trade_count_by_symbol[sym], state.auto_trade_max_per_symbol,
        )

        # SPY MIRROR (2026-07-17): a committed SPX fire ALSO places a SPY
        # option mirror of the same trade — SPX signal (theta flow,
        # validated) -> SPY vehicle at the same percentage moneyness
        # (spot-adjusted map, spy_mapping_report.md). SPX fires ONLY (never
        # scanner fires on other symbols), AFTER the SPX order + counters so
        # the mirror references a persisted SPX parent. FAIL-SOFT: every
        # mirror failure is an audit row, never an exception into this path
        # — the SPX fire is already placed and MUST return FIRED.
        if sym == "SPX":
            try:
                from fef_sidecar.engine.spy_mirror import maybe_place_spy_mirror
                await maybe_place_spy_mirror(
                    state,
                    direction=direction,
                    spx_strike=float(strike),
                    spx_qty=qty,
                    spx_parent_id=result.get("parent_order_id"),
                    source=source,
                    strategy=strategy,
                    spx_t1=spx_t1, spx_t2=spx_t2, spx_t3=spx_t3,
                    spx_stop=spx_stop,
                    entry_timeout_sec=entry_timeout,
                    # The verdict's own gate-8-validated SPX underlying — the
                    # mirror's map anchor (NEVER the active-chart tick, which
                    # carries ES prices on an ES chart; adversarial review
                    # Finding 1).
                    spx_spot_ref=opt.get("underlying_price_at_quote"),
                    # The SPX leg's resolved fire ask — bounds the mirror
                    # premium at ~1/10 (chase-bound substitute, Finding 6).
                    spx_entry_ask=entry_res.ask,
                )
            except Exception:
                logger.exception(
                    "SPY mirror placement failed (SPX fire unaffected)"
                )

    return AutoTradeDecision(True, "fired", BlockReason.FIRED, order_result=result)
