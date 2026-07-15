"""Multi-symbol FEF scanner.

Runs FEF iters in parallel for each symbol the user has enabled via
state.scan_enabled_symbols. Each symbol has its own FEFEngine instance
in state.scanner_engines so warmup, drift, and wall history are tracked
independently per ticker.

Independent of state.active_symbol (the chart symbol) — the scanner
runs in the background regardless of what the user is viewing.

For SPX / SPY / QQQ / IWM: full options auto-trade pipeline (same path
as the existing single-symbol fef_tick_loop, just per-symbol).

For MES / MNQ / M2K (CME micro futures): FEF verdict is computed and
broadcast, but auto-trade is currently SCAN-ONLY — futures position
sizing + bracket logic aren't wired yet. An audit row notes 'futures
auto-trade not implemented' when an actionable verdict fires on a
futures symbol.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from typing import Any

from fef_sidecar.config_store import load_config
from fef_sidecar.db.repository import SignalIterRepository
from fef_sidecar.engine import FEFEngine
from fef_sidecar.engine.fef import snapshot_to_inputs

logger = logging.getLogger(__name__)


OPTION_SYMBOLS = {"SPX", "SPY", "QQQ", "IWM", "GLD", "SMH", "TLT"}

# Per-symbol input sanity guard (2026-07-06): reject an incoming spot that
# deviates more than this fraction from the engine's last ACCEPTED spot.
# Cross-symbol contamination is orders of magnitude beyond it (SPX 7526.8 on
# a GLD ~380 engine = +1882%); no legitimate 60s move approaches 20%.
SCANNER_SPOT_MAX_DEVIATION = 0.20

# Reference-pollution guard (2026-07-06 19:07 residual): even with a VALID
# current spot, the engine's own delta window can still hold a cross-stream
# reference accepted before the band guard deployed (6 post-fix rows carried
# dP ±6,794 off a polluted PREVIOUS reference). The implied per-iteration
# delta is validated against max(SCANNER_DELTA_ABS_MAX_PTS,
# SCANNER_DELTA_MAX_FRAC × spot); a violation rejects the iteration AND
# resets (quarantines) the engine's history window so the pollution cannot
# survive into the next delta computation.
SCANNER_DELTA_ABS_MAX_PTS = 50.0
SCANNER_DELTA_MAX_FRAC = 0.03


def _reference_delta_threshold(spot: float) -> float:
    return max(SCANNER_DELTA_ABS_MAX_PTS, SCANNER_DELTA_MAX_FRAC * abs(spot))


def _polluted_window_refs(engine, spot: float) -> list[float]:
    """Prices in the engine's history window whose implied delta vs `spot`
    violates the reference-pollution threshold. Empty list = window clean
    (or engine exposes no window — nothing to validate)."""
    history = getattr(engine, "_history", None) if engine is not None else None
    if not history:
        return []
    thresh = _reference_delta_threshold(spot)
    out: list[float] = []
    for entry in history:
        try:
            ref = float(getattr(entry, "spx_price", 0.0) or 0.0)
        except Exception:
            continue
        if ref > 0 and abs(spot - ref) > thresh:
            out.append(ref)
    return out


def _quarantine_engine_window(engine) -> bool:
    """Reset the engine's history/delta window (public reset() when
    available). Returns True when a reset actually happened."""
    if engine is None:
        return False
    try:
        if hasattr(engine, "reset"):
            engine.reset()
            return True
        history = getattr(engine, "_history", None)
        if history is not None:
            history.clear()
            return True
    except Exception:
        logger.exception("scanner: engine window quarantine failed")
    return False


async def _reject_scanner_input_glitch(state, symbol: str, spot, engine=None) -> bool:
    """Input-boundary price sanity guard for one scanner engine (2026-07-06).

    A race on the shared QuantData page filter (set_page_filter mutates
    client._ticker; every sub-fetch reads it at request time) let the GLD
    scan fetch an SPX-priced snapshot → "[SCANNER GLD] ALIGNED — ΔPrice
    +7147.15pts". The race itself is closed by loop._qd_scope_lock; this
    guard is the boundary backstop, in TWO phases:

    Phase 1 — current-spot band (original guard): if the incoming spot
    deviates >20% from this engine's last ACCEPTED spot, REJECT the whole
    iteration — no engine.push (drift/delta windows stay unpolluted), no
    verdict cache, no trade path — and write a category='scanner_input_glitch'
    audit row (kind='spot_band') with both values. A rejected spot is NOT
    recorded as the new baseline; the first-ever spot for a symbol is
    accepted (no baseline to judge against). On rejection the engine's
    history window is ALSO validated against the accepted baseline and
    quarantined if it holds a cross-stream reference.

    Phase 2 — reference pollution (2026-07-06 19:07 residual): a VALID
    current spot judged against a POLLUTED previous reference still produced
    dP ±6,794 in 6 post-fix rows (cross-stream reference already inside the
    engine's delta window, e.g. accepted pre-deploy or after a baseline
    reset). If any window reference implies |Δprice| > max(50, 3% of spot),
    REJECT the iteration and RESET/quarantine the engine's history window
    (engine.reset() → clean re-warmup), audit row kind='reference_pollution'.

    Returns True → caller must skip the iteration."""
    if not hasattr(state, "_scanner_last_spot"):
        state._scanner_last_spot = {}  # type: ignore[attr-defined]
    last = state._scanner_last_spot.get(symbol)  # type: ignore[attr-defined]
    spot_f = float(spot or 0.0)
    if (last is not None and last > 0
            and abs(spot_f - last) > last * SCANNER_SPOT_MAX_DEVIATION):
        dev_pct = abs(spot_f - last) / last * 100.0
        # The window must never retain a reference that is itself outside
        # the accepted band (cross-symbol pollution) — validate + quarantine
        # while we're rejecting anyway, so the next accepted tick can't be
        # judged against a foreign previous price.
        window_bad = bool(_polluted_window_refs(engine, last))
        quarantined = _quarantine_engine_window(engine) if window_bad else False
        logger.warning(
            "scanner: %s REJECTED snapshot spot %.2f — deviates %.1f%% from "
            "last accepted %.2f (cross-symbol contamination guard%s)",
            symbol, spot_f, dev_pct, last,
            "; polluted window quarantined" if quarantined else "",
        )
        try:
            await SignalIterRepository.write_audit_log(
                category="scanner_input_glitch", actor="system",
                summary=(
                    f"SCANNER {symbol}: REJECTED snapshot spot {spot_f:.2f} — "
                    f"deviates {dev_pct:.1f}% from last accepted {last:.2f} "
                    f"(>{SCANNER_SPOT_MAX_DEVIATION:.0%} cross-symbol contamination "
                    f"guard); iteration skipped, engine state untouched"
                ),
                details={
                    "kind": "spot_band",
                    "symbol": symbol,
                    "incoming_spot": spot_f,
                    "last_accepted_spot": last,
                    "deviation_pct": round(dev_pct, 2),
                    "threshold_pct": SCANNER_SPOT_MAX_DEVIATION * 100.0,
                    "window_quarantined": quarantined,
                },
            )
        except Exception:
            logger.exception("scanner: input-glitch audit write failed for %s", symbol)
        return True

    # Phase 2 — the current spot is acceptable; validate the DELTA it would
    # produce against every reference in the engine's own history window.
    if spot_f > 0:
        polluted = _polluted_window_refs(engine, spot_f)
        if polluted:
            worst = max(polluted, key=lambda r: abs(spot_f - r))
            delta = spot_f - worst
            thresh = _reference_delta_threshold(spot_f)
            quarantined = _quarantine_engine_window(engine)
            logger.warning(
                "scanner: %s REJECTED iteration — valid spot %.2f against "
                "polluted window reference %.2f (dPrice %+.2f exceeds "
                "max(50, 3%%·spot)=%.2f); reference window %s",
                symbol, spot_f, worst, delta, thresh,
                "quarantined" if quarantined else "NOT quarantined",
            )
            try:
                await SignalIterRepository.write_audit_log(
                    category="scanner_input_glitch", actor="system",
                    summary=(
                        f"SCANNER {symbol}: REJECTED iteration — valid spot "
                        f"{spot_f:.2f} vs polluted window reference {worst:.2f} "
                        f"(dPrice {delta:+.2f} > {thresh:.2f} = max(50, 3% of "
                        f"spot)); engine history window "
                        f"{'quarantined' if quarantined else 'not quarantined'}"
                    ),
                    details={
                        "kind": "reference_pollution",
                        "symbol": symbol,
                        "incoming_spot": spot_f,
                        "polluted_reference": worst,
                        "delta_price": round(delta, 2),
                        "threshold_pts": round(thresh, 2),
                        "polluted_refs": [round(r, 2) for r in polluted[:8]],
                        "window_quarantined": quarantined,
                    },
                )
            except Exception:
                logger.exception(
                    "scanner: reference-pollution audit write failed for %s", symbol,
                )
            # The spot itself validated against the band — record it as the
            # baseline so the band guard stays current while the engine
            # re-warms from a clean window.
            state._scanner_last_spot[symbol] = spot_f  # type: ignore[attr-defined]
            return True
        state._scanner_last_spot[symbol] = spot_f  # type: ignore[attr-defined]
    return False


def _make_engine_for(symbol: str) -> FEFEngine:
    """Build a per-symbol FEFEngine via the CANONICAL factory — configured
    as THE one canonical flow-surge signaler.

    CONFIG DECISION (2026-07-02, final): the STRICT scanner configuration
    ("whatever fired today") is the ONE canonical flow-surge signaler. The
    MAIN engine's flow-surge path stays RETIRED (advisory WAIT rows only).
    Two engines with different effective thresholds firing into one account
    would produce an uninterpretable mixed signal population — so the
    scanner gate is controlled by its OWN env, independent of the global
    FEF_FLOW_SURGE_GATE_ENABLED (which governs main + shadow and stays
    false at deploy):

        FEF_SCANNER_FLOW_SURGE_ENABLED  (default true = today's behavior)

    History: this used to be a local 5-param FEFEngine() call, so
    flow_surge_gate_enabled silently fell back to the CONSTRUCTOR default
    True — which is HOW the scanner kept firing after the 6/30 retirement
    flipped only state._make_engine's env default. Now the same behavior
    is explicit, named, and switchable.

    threshold_scale — CANONICAL STRICT CONFIGURATION (not a legacy quirk):
    scanner engines deliberately use the FEF_LOOP_INTERVAL_SEC-derived
    scale (120s -> 1.0) while evaluating every FEF_SCAN_INTERVAL_SEC (60s),
    i.e. a 60s delta window judged against 120s-calibrated magnitude
    thresholds — ~2x stricter per window. That selectivity (~1 fire/day)
    IS the configuration being forward-tested; do not "fix" it to
    cadence-matched scaling. Cadence-matched scaling remains available as
    an explicit opt-in via FEF_SCAN_THRESHOLD_SCALE_SEC (e.g. 60).
    """
    from fef_sidecar.state import _make_engine
    scanner_gate = os.environ.get(
        "FEF_SCANNER_FLOW_SURGE_ENABLED", "true"
    ).lower() in ("true", "1", "yes", "on")
    raw = os.environ.get("FEF_SCAN_THRESHOLD_SCALE_SEC", "").strip()
    if raw:
        return _make_engine(interval_sec=float(raw), flow_surge_gate=scanner_gate)
    # Default: FEF_LOOP_INTERVAL_SEC scale = the canonical strict config.
    return _make_engine(flow_surge_gate=scanner_gate)


async def _apply_flow_source_and_shadow(state, inputs, engine):
    """Drift-input source selection + flow_shadow evidence row (2026-07-15
    input-parity fix). SPX only.

    With UIConfig.flow_source='qd' (DEFAULT) the returned inputs are the
    UNTOUCHED snapshot inputs — live behavior byte-identical to before this
    change. With 'theta', drift_cum_net_m is replaced by the live
    theta_flow_ingest read (the validated option_flow_minute construction);
    a stale/absent theta read falls back to the QD input (fail-soft, audited
    once per session).

    Every SPX scan also emits ONE category='flow_shadow' audit row with both
    feeds' drift levels + 3-minute deltas — the evidence stream the eventual
    swap decision requires (>=5 sessions; see parity_report.md). Best-effort:
    any failure here returns the original inputs and never blocks the scan.

    Source flips are expected PRE-OPEN only; if one happens mid-session the
    engine's delta window would straddle two scales (QD ~7-9% of theta), so
    the window is quarantined (engine.reset()) on flip detection.
    """
    from dataclasses import replace as _dc_replace

    flow_source = "qd"
    try:
        flow_source = str(getattr(load_config(), "flow_source", "qd"))
    except Exception:
        logger.exception("scanner: flow_source config read failed — using 'qd'")

    # Mid-session source-flip quarantine (see docstring).
    try:
        prev = getattr(state, "_flow_source_last", None)
        if prev is not None and prev != flow_source and engine is not None:
            if hasattr(engine, "reset"):
                engine.reset()
            logger.warning(
                "scanner: flow_source flipped %s -> %s mid-process — SPX "
                "engine window quarantined (scale discontinuity)", prev, flow_source,
            )
        state._flow_source_last = flow_source
    except Exception:
        pass

    from fef_sidecar.engine.theta_flow_ingest import theta_drift_read
    holder = getattr(state, "theta_flow", None)
    theta_drift, theta_fresh = theta_drift_read(state)

    now_ms = int(_time.time() * 1000)
    qd_drift = float(inputs.drift_cum_net_m)

    # QD 3-min delta from the scanner's own sample history (evidence only).
    qd_d3 = None
    try:
        hist = state.flow_shadow_qd_history
        hist.append((now_ms, qd_drift))
        floor = now_ms - 10 * 60_000
        while hist and hist[0][0] < floor:
            hist.pop(0)
        target = now_ms - 180_000
        past = [v for t, v in hist if t <= target + 10_000]
        if past:
            qd_d3 = qd_drift - past[-1]
    except Exception:
        qd_d3 = None

    out = inputs
    swap_applied = False
    if flow_source == "theta":
        if theta_fresh and theta_drift is not None:
            out = _dc_replace(inputs, drift_cum_net_m=float(theta_drift))
            swap_applied = True
        else:
            # Fail-soft fallback to the QD input — audit once per session so
            # a silent mid-session source degradation is visible.
            sess, _ = _now_et_parts(now_ms)
            if getattr(state, "_theta_fallback_audited", None) != sess:
                state._theta_fallback_audited = sess
                try:
                    await SignalIterRepository.write_audit_log(
                        category="theta_flow", actor="system",
                        summary=(
                            "flow_source='theta' but theta drift read is "
                            "stale/absent — SPX scanner fell back to the QD "
                            "drift input (fail-soft)"
                        ),
                        details={"session_date": sess,
                                 "theta_drift": theta_drift,
                                 "holder_present": holder is not None},
                    )
                except Exception:
                    logger.exception("scanner: theta fallback audit failed")

    # flow_shadow evidence row — only once the ingest loop has published.
    if holder is not None:
        try:
            details = {
                "qd_drift_m": round(qd_drift, 4),
                "theta_drift_m": (round(theta_drift, 4)
                                  if theta_drift is not None else None),
                "qd_d3_m": round(qd_d3, 4) if qd_d3 is not None else None,
                "theta_d3_m": holder.get("drift_theta_d3_m"),
                "theta_last_final_minute_ms": holder.get("last_final_minute_ms"),
                "theta_stale": bool(holder.get("stale")),
                "theta_fresh": theta_fresh,
                "spx": float(inputs.spx_price),
                "flow_source": flow_source,
                "swap_applied": swap_applied,
            }
            td = details["theta_drift_m"]
            await SignalIterRepository.write_audit_log(
                category="flow_shadow", actor="system",
                summary=(
                    f"flow_shadow SPX: qd={qd_drift:+.2f}M "
                    f"theta={'n/a' if td is None else format(td, '+.2f') + 'M'} "
                    f"(d3 qd={'n/a' if qd_d3 is None else format(qd_d3, '+.2f')}"
                    f"/theta={details['theta_d3_m'] if details['theta_d3_m'] is not None else 'n/a'}) "
                    f"src={flow_source}{'*' if swap_applied else ''}"
                ),
                details=details,
            )
        except Exception:
            logger.exception("scanner: flow_shadow audit write failed")
    return out


async def _scan_one_symbol(state, symbol: str) -> None:
    """Run one FEF iter for the given symbol. Broadcasts an IterEvent
    with symbol field set so the FE can route to the right per-symbol UI.
    """
    # Lazy-create the per-symbol engine
    if symbol not in state.scanner_engines:
        state.scanner_engines[symbol] = _make_engine_for(symbol)
    engine = state.scanner_engines[symbol]

    # Fetch QuantData snapshot for this symbol. Temporarily flip the page
    # filter; the global poll will reset it next cycle. HOLD THE SCOPE LOCK
    # across set+fetch (2026-07-06): set_page_filter mutates the shared
    # client's _ticker and every sub-fetch reads it at request time, so an
    # unlocked pair interleaved with the main poll's set_page_filter fetched
    # SPX-priced data into this symbol's snapshot (the GLD +7147pt glitch).
    try:
        from fef_sidecar.loop import _qd_scope_lock
        async with _qd_scope_lock(state.quantdata):
            await state.quantdata.set_page_filter(ticker=symbol)
            snap = await state.quantdata.fetch_market_snapshot()
    except Exception as e:
        logger.warning("scanner: QuantData fetch failed for %s: %s", symbol, e)
        return
    if snap is None:
        return

    inputs = snapshot_to_inputs(snap)

    # PER-SYMBOL PRICE SANITY GUARD (2026-07-06): reject cross-symbol
    # contamination at the input boundary BEFORE any consumption — no
    # engine.push, no verdict cache, no impulse flag, no trade path. The
    # engine is passed so the guard can also validate/quarantine its
    # history window (reference-pollution extension, 19:07 residual).
    if await _reject_scanner_input_glitch(
        state, symbol, inputs.spx_price, engine=engine,
    ):
        return

    # Drift-input source selection + flow_shadow evidence row (2026-07-15
    # input-parity fix). SPX only; with the default flow_source='qd' this
    # returns `inputs` untouched — behavior byte-identical to before.
    if symbol == "SPX":
        try:
            inputs = await _apply_flow_source_and_shadow(state, inputs, engine)
        except Exception:
            logger.exception("scanner: flow-source/shadow hook failed — QD input used")

    verdict = engine.push(inputs)

    # Cache the verdict for the FE Scanner panel (it polls /scanner/symbols).
    # We do NOT broadcast IterEvent from the scanner — that would push the
    # other symbol's walls / max-pain / drift onto the active chart since
    # the chart subscribes to ALL iter events. The user wants scanner work
    # to stay in the background, not pollute the chart.
    state.last_scanner_verdict_by_symbol[symbol] = {
        "direction": verdict.direction.value,
        "confidence": verdict.confidence.value,
        "reasoning": verdict.reasoning,
        "spx_price": inputs.spx_price,
        "strike_rec": verdict.strike_rec,
        "timestamp_ms": int(_time.time() * 1000),
    }

    # IMPULSE ATTENTION FLAG — advisory only (validated 2026-07-02 as
    # decision-support; whipsaws as an auto-trigger). Evaluated on EVERY
    # scan, WAIT verdicts included — the verdict carries delta_price /
    # delta_drift_m regardless of direction. Never trades.
    try:
        await maybe_impulse_flag(
            state, symbol, spot=inputs.spx_price,
            delta_price=verdict.delta_price,
            delta_drift_m=verdict.delta_drift_m,
            # Real drift context for the advisory row (the values the engine
            # actually consumed this scan — post flow_source selection).
            drift_cum_net_m=inputs.drift_cum_net_m,
            drift_bias=inputs.drift_bias,
        )
    except Exception:
        logger.exception("scanner: impulse flag evaluation failed for %s", symbol)

    # Build a verdict dict for the auto-trader (not broadcast — local only).
    verdict_dict = {
        "direction": verdict.direction.value,
        "confidence": verdict.confidence.value,
        "reasoning": verdict.reasoning,
        "strike_rec": verdict.strike_rec,
        "t1": verdict.t1, "t2": verdict.t2, "t3": verdict.t3,
        "stop": verdict.stop,
        "delta_price": verdict.delta_price,
        "delta_drift_m": verdict.delta_drift_m,
        "fe_ratio": verdict.fe_ratio,
        # Bias at verdict time — the auto-trader's wall-collapse re-grade
        # needs it to mirror fef.py's demotion exactly at gate time.
        "drift_bias": inputs.drift_bias,
        "aligned": verdict.aligned,
        "absorption": verdict.absorption,
        "wall_proximity_skip": verdict.wall_proximity_skip,
        "wall_collapse_trigger": verdict.wall_collapse_trigger,
        "nearest_wall_strike": verdict.nearest_wall_strike,
        "nearest_wall_distance": verdict.nearest_wall_distance,
        "nearest_wall_family": verdict.nearest_wall_family,
        "secondary_scores": verdict.secondary_scores,
        "scanner_symbol": symbol,
    }
    # Auto-trader needs option_pricing to pass gate 8. Fetch the option
    # quote for the scanner-recommended strike here (same path as the
    # main iter loop's _maybe_fetch_option_targets does for SPX).
    if verdict.direction.value in ("LONG_CALLS", "LONG_PUTS") and verdict.strike_rec:
        try:
            from fef_sidecar.engine.option_targets import build_option_targets
            from fef_sidecar.engine.expiry_resolver import resolve_nearest_expiry
            from fef_sidecar.ibkr.models import Contract, ContractKind
            # Per-symbol nearest-expiry resolution. Fixes the bug where
            # GLD/SMH/TLT (no 0DTE) and SPY/QQQ/IWM on Tu/Th silently
            # failed Gate 8 because today's expiry didn't exist.
            expiry = await resolve_nearest_expiry(state, symbol)
            right = "C" if verdict.direction.value == "LONG_CALLS" else "P"
            opt_contract = Contract(
                symbol=symbol, kind=ContractKind.OPTION,
                exchange="CBOE" if symbol == "SPX" else "SMART",
                expiry=expiry, strike=verdict.strike_rec, right=right,
            )
            quote = await state.ibkr.get_option_quote(opt_contract)
            targets = build_option_targets(
                quote=quote, current_spx=inputs.spx_price,
                spx_t1=verdict.t1, spx_t2=verdict.t2,
                spx_t3=verdict.t3, spx_stop=verdict.stop,
            )
            if targets is not None:
                verdict_dict["option_pricing"] = {
                    "contract_strike": targets.contract_strike,
                    "right": targets.right,
                    "entry_limit": targets.entry_limit,
                    "delta": targets.delta,
                    "underlying_price_at_quote": targets.underlying_price_at_quote,
                    "t1": targets.t1, "t2": targets.t2,
                    "t3": targets.t3, "stop": targets.stop,
                }
        except Exception:
            logger.exception("scanner: option quote fetch failed for %s", symbol)

    if symbol in OPTION_SYMBOLS:
        await handle_actionable_scanner_verdict(
            state, symbol, spot=inputs.spx_price, verdict_dict=verdict_dict,
            inputs=inputs,
        )


# Structured block code for bias-veto skips (same rationale as
# chop_gate.CHOP_GATE_BLOCK_CODE: the veto lives outside the frozen
# auto-trader; block_reason_code is a free-form column).
BIAS_VETO_BLOCK_CODE = "BIAS_VETO_SKIP"
# FOMC-morning gate (2026-07-06 event study: FOMC pre-14:00 fires -$61/ct,
# t=-4.4, 80% of FOMC sessions negative).
FOMC_GATE_BLOCK_CODE = "FOMC_GATE_SKIP"
FOMC_STATEMENT_MINUTE_ET = 14 * 60  # statement drops 14:00 ET
# Time-of-day no-trade window (entry-filter sweep: worst TOD bucket
# 14:00-15:30 ET at -$21/ct OOS; user decision 2026-07-06).
TOD_GATE_BLOCK_CODE = "TOD_GATE_SKIP"


def _parse_hhmm(raw: Any) -> int | None:
    """'14:00' -> 840 (minute of day). None on anything unparseable —
    callers treat that as a misconfigured (disabled) window."""
    try:
        s = str(raw).strip()
        hh, mm = s.split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:
        pass
    return None


def _fmt_minute_et(minute_of_day: int) -> str:
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


def _now_et_parts(now_ms: int | None = None) -> tuple[str, int]:
    """(session_date 'YYYY-MM-DD', minute_of_day) under the app's fixed-offset
    ET convention (same as _today_et / chop_gate). Monkeypatchable in tests."""
    from datetime import datetime, timedelta, timezone
    et = timezone(timedelta(hours=-4))
    if now_ms is None:
        now_ms = int(_time.time() * 1000)
    dt = datetime.fromtimestamp(now_ms / 1000, et)
    return dt.strftime("%Y-%m-%d"), dt.hour * 60 + dt.minute


class _EntryGatesConfig:
    """Hot-read snapshot of the scanner entry-gate config (one read per
    actionable evaluation). Plain attributes — trivially fake-able."""

    __slots__ = (
        "chop_enabled", "chop_threshold", "bias_veto_enabled",
        "fomc_gate_enabled", "tod_gate_enabled",
        "tod_start_min", "tod_end_min", "tod_window_label",
        "high_bypass_enabled",
    )

    def __init__(self) -> None:
        # Mirror UIConfig defaults (user decisions 2026-07-02 + 2026-07-06)
        self.chop_enabled = True
        self.chop_threshold = 33.3
        self.bias_veto_enabled = True
        self.fomc_gate_enabled = True
        self.tod_gate_enabled = True
        self.tod_start_min: int | None = 14 * 60
        self.tod_end_min: int | None = 15 * 60 + 30
        self.tod_window_label = "14:00-15:30"
        # Tier-scoped gate bypass (2026-07-09): HIGH fires skip chop/bias/tod.
        self.high_bypass_enabled = True


async def _chop_gate_context(state) -> tuple[Any | None, _EntryGatesConfig]:
    """Hot-read the entry-gate config + evaluate the CHOP assessment for 'now'.

    Returns (chop_assessment, gates_config).

    Config is re-read on EVERY actionable evaluation (load_config is a
    module-level import so tests can monkeypatch it), which makes every gate
    knob (chop_gate_enabled / chop_gate_pct_threshold / bias_veto_enabled /
    fomc_gate_enabled / tod_gate_enabled / gate_high_bypass_enabled + the TOD
    window edges) tunable via POST /config without a restart. Both steps are exception-guarded; a
    failed assessment returns None which the caller treats as gate-open (the
    filters are harm-reduction — they must never block on their own failure).
    An unparseable or inverted TOD window disables the TOD gate (fail-open)
    and logs the misconfiguration."""
    gates = _EntryGatesConfig()
    try:
        cfg = load_config()
        gates.chop_enabled = bool(cfg.chop_gate_enabled)
        gates.chop_threshold = float(cfg.chop_gate_pct_threshold)
        gates.bias_veto_enabled = bool(cfg.bias_veto_enabled)
        gates.fomc_gate_enabled = bool(cfg.fomc_gate_enabled)
        gates.tod_gate_enabled = bool(cfg.tod_gate_enabled)
        gates.high_bypass_enabled = bool(cfg.gate_high_bypass_enabled)
        start = _parse_hhmm(cfg.tod_gate_start_et)
        end = _parse_hhmm(cfg.tod_gate_end_et)
        if start is None or end is None or start >= end:
            if gates.tod_gate_enabled:
                logger.warning(
                    "scanner: TOD gate window misconfigured (start=%r end=%r) "
                    "— gate fails open", cfg.tod_gate_start_et, cfg.tod_gate_end_et,
                )
            gates.tod_gate_enabled = False
            gates.tod_start_min = gates.tod_end_min = None
            gates.tod_window_label = f"{cfg.tod_gate_start_et}-{cfg.tod_gate_end_et}"
        else:
            gates.tod_start_min, gates.tod_end_min = start, end
            gates.tod_window_label = (
                f"{_fmt_minute_et(start)}-{_fmt_minute_et(end)}"
            )
    except Exception:
        logger.exception("scanner: gate config read failed — using defaults")
        # Fail RESTRICTIVE for the tier bypass (mirrors the floor's fail-
        # restrictive HIGH default in auto_trader._config_floor): a config
        # outage must NOT silently open HIGH fires past chop/bias/tod. Every
        # OTHER knob keeps its harm-reduction fail-OPEN default; only the
        # bypass flips off, so all gates apply to all tiers until config
        # reads cleanly again (2026-07-09 review MINOR).
        gates.high_bypass_enabled = False
    assessment = None
    try:
        from fef_sidecar.engine import chop_gate as _cg
        assessment = await _cg.assess_chop(state)
    except Exception:
        logger.exception("scanner: chop_gate assessment failed — gate passes open")
    return assessment, gates


async def maybe_impulse_flag(
    state, symbol: str, *, spot: float,
    delta_price: float | None, delta_drift_m: float | None,
    drift_cum_net_m: float | None = None, drift_bias: str | None = None,
) -> bool:
    """IMPULSE ATTENTION FLAG — advisory only, NEVER trades.

    Validated 2026-07-02: this exact combination works as a real-time
    attention flag (caught both of the day's missed moves, one 4 minutes
    before the scanner's late fire) but WHIPSAWS as an auto-trigger — so it
    ships as decision-support. Fires when ALL of:

      1. |delta_price| >= UIConfig.impulse_flag_min_pts — SPX-calibrated
         points, moneyness-scaled by spot/7500 for ETF-class symbols (the
         same _price_scale as the strike fix; SPX-class spot>=2000 = 1.0),
      2. sign(delta_drift) == sign(delta_price) when
         impulse_flag_require_agree (default true). The disagree-VETO is the
         validated core — 7/2 17:58's disagreeing impulse was a session-low
         puts trap,
      3. the chop regime is NOT confirmed-compressed (reuses the chop-gate
         percentile — flag fires only when the regime has room; an unknown
         percentile passes, consistent with the gate's fail-open design).

    Per-(symbol, direction) cooldown (impulse_flag_cooldown_sec, default
    600s) so a trending tape doesn't re-flag every 60s scan.

    Effects when it fires: one audit_log row (category='impulse_flag') +
    one ADVISORY signal_iter row — direction stays WAIT with confidence
    NONE and extras.source='impulse_flag'/advisory=true, so it is
    structurally invisible to maybe_auto_trade (gate 3 rejects WAIT) and
    to every actionable-signal analytic. Returns True when flagged."""
    # Hot-read config each scan (module-level load_config — test-patchable).
    enabled, min_pts, require_agree, cooldown_sec, chop_threshold = (
        True, 6.0, True, 600, 33.3
    )
    try:
        cfg = load_config()
        enabled = bool(cfg.impulse_flag_enabled)
        min_pts = float(cfg.impulse_flag_min_pts)
        require_agree = bool(cfg.impulse_flag_require_agree)
        cooldown_sec = int(cfg.impulse_flag_cooldown_sec)
        chop_threshold = float(cfg.chop_gate_pct_threshold)
    except Exception:
        logger.exception("scanner: impulse flag config read failed — using defaults")
    if not enabled:
        return False
    if delta_price is None or not spot or spot <= 0:
        return False

    # 1. Impulse magnitude — SPX-calibrated, moneyness-scaled per symbol.
    from fef_sidecar.engine.strikes import _price_scale
    eff_min_pts = min_pts * _price_scale(spot)
    if abs(delta_price) < eff_min_pts:
        return False
    direction = "UP" if delta_price > 0 else "DOWN"

    # 2. Flow agreement (the validated veto).
    agree = bool(
        delta_drift_m is not None
        and ((delta_price > 0 and delta_drift_m > 0)
             or (delta_price < 0 and delta_drift_m < 0))
    )
    if require_agree and not agree:
        return False

    # Per-(symbol, direction) cooldown — checked before any DB work.
    now_ms = int(_time.time() * 1000)
    last_map = getattr(state, "impulse_flag_last_at_ms", None)
    if last_map is None:
        last_map = {}
        try:
            state.impulse_flag_last_at_ms = last_map
        except Exception:
            pass
    key = (symbol, direction)
    last = last_map.get(key, 0)
    if last and (now_ms - last) < cooldown_sec * 1000:
        return False

    # 3. Regime room — only a CONFIRMED compressed regime suppresses.
    chop = None
    try:
        from fef_sidecar.engine import chop_gate as _cg
        chop = await _cg.assess_chop(state)
    except Exception:
        logger.exception("scanner: impulse flag chop assessment failed — treating as unknown")
    if chop is not None and chop.is_compressed(chop_threshold):
        return False

    # Stamp the cooldown BEFORE the writes so a failing write can't turn
    # into a 60s-cadence retry spam.
    last_map[key] = now_ms

    pct = chop.percentile if chop is not None else None
    pct_str = f"{pct:.0f}" if pct is not None else "n/a"
    dd_str = (
        f"{delta_drift_m:+.1f}M {'agree' if agree else 'disagree'}"
        if delta_drift_m is not None else "n/a"
    )
    body = (
        f"{symbol} @ {spot:.1f} (dP {delta_price:+.1f}pts, "
        f"dDrift {dd_str}, r30 pct {pct_str})"
    )
    summary = f"IMPULSE {direction} {body}"
    logger.info("scanner: %s", summary)

    signal_id = None
    try:
        from fef_sidecar.db import FEFConfidence, FEFDirection
        from fef_sidecar.loop import _current_account_class, _current_provider_mode
        signal = await SignalIterRepository.write_iter(
            spx_price=spot,
            direction=FEFDirection.WAIT,       # advisory — untradeable by construction
            confidence=FEFConfidence.NONE,
            reasoning=(
                f"[IMPULSE {direction}] {body}. "
                "Advisory attention flag — decision-support only, no trade."
            ),
            # Log the REAL drift context the scan evaluated (2026-07-15 fix:
            # these advisory rows used to hardcode drift_cum_net_m=0.0,
            # polluting any signal_iter drift analytics that didn't filter
            # them out). The column is non-nullable, so when the caller has
            # no drift context the legacy 0.0/NEUTRAL stands — the
            # extras.is_impulse marker below makes the rows unambiguously
            # separable either way.
            drift_cum_net_m=(
                float(drift_cum_net_m) if drift_cum_net_m is not None else 0.0
            ),
            drift_bias=(drift_bias if drift_bias else "NEUTRAL"),
            ntm_max_pain=0.0, raw_max_pain=0.0,
            top_gex_walls=[], top_dex_walls=[],
            extras={
                "source": "impulse_flag",
                "is_impulse": True,
                "drift_context_logged": drift_cum_net_m is not None,
                "advisory": True,
                "scanner_symbol": symbol,
                "impulse_direction": direction,
                "delta_price": delta_price,
                "delta_drift_m": delta_drift_m,
                "flow_agree": agree,
                "threshold_pts_effective": round(eff_min_pts, 4),
                "r30_pct": round(pct, 2) if pct is not None else None,
                "tod_bucket": chop.tod_bucket if chop is not None else None,
                "account_class": _current_account_class(state),
                "account_id": getattr(state.ibkr, "account_id", "") or "",
            },
            session_date=_today_et(),
            provider_mode=_current_provider_mode(state),
        )
        signal_id = signal.id
    except Exception:
        logger.exception("scanner: impulse flag signal persist failed for %s", symbol)

    try:
        await SignalIterRepository.write_audit_log(
            category="impulse_flag", actor="system",
            summary=summary,
            details={
                "source": "impulse_flag",
                "symbol": symbol,
                "direction": direction,
                "spot": spot,
                "delta_price": delta_price,
                "delta_drift_m": delta_drift_m,
                "flow_agree": agree,
                "threshold_pts_effective": round(eff_min_pts, 4),
                "r30_pct": round(pct, 2) if pct is not None else None,
                "tod_bucket": chop.tod_bucket if chop is not None else None,
                "cooldown_sec": cooldown_sec,
                "signal_iter_id": signal_id,
            },
        )
    except Exception:
        logger.exception("scanner: impulse flag audit write failed for %s", symbol)
    return True


async def _tag_high_bypass(signal_iter_id: int | None, gate: str) -> None:
    """Post-FIRE counterfactual tag for the tier-scoped gate bypass.

    Written ONLY after maybe_auto_trade returns fired=True, so
    extras.high_bypassed_gate ('chop'|'bias'|'tod') and the matching
    [HIGH-BYPASS <gate>] reasoning note land on rows that BYPASSED a gate AND
    actually TRADED. 2026-07-09 review MAJOR-1: tagging at persist time (before
    the trade) co-occurred with the floor / cross-symbol-cap skipped_by tag -
    their auto_trader._tag_signal_skip MERGES skipped_by into this same row, so a
    bypassed-then-blocked fire falsely read as 'bypassed and traded' and inflated
    the grading (`high_bypassed_gate IS NOT NULL`). Written here it can NEVER
    co-occur with skipped_by. Mirrors _tag_signal_skip's replace-not-mutate JSON
    contract (SQLAlchemy JSON does not change-track in-place mutation). Never
    raises."""
    if signal_iter_id is None:
        return
    try:
        from sqlmodel import select as _sm_select
        from fef_sidecar.db.engine import get_session as _gs
        from fef_sidecar.db.models import SignalIter as _SI
        note = f"[HIGH-BYPASS {gate}]"
        async with _gs() as session:
            row = (await session.exec(
                _sm_select(_SI).where(_SI.id == signal_iter_id)
            )).first()
            if row is None:
                return
            if note not in (row.reasoning or ""):
                row.reasoning = f"{row.reasoning or ''} {note}".strip()
            # Replace (never mutate) the JSON column so SQLAlchemy change-tracks it.
            row.extras = {**(row.extras or {}), "high_bypassed_gate": gate}
            session.add(row)
    except Exception:
        logger.exception(
            "scanner: high_bypass tag write failed for signal %s (%s)",
            signal_iter_id, gate,
        )


async def handle_actionable_scanner_verdict(
    state, symbol: str, *, spot: float, verdict_dict: dict[str, Any],
    inputs: Any | None = None,
) -> None:
    """Persist + (maybe) trade one scanner verdict.

    VISIBILITY FIX (2026-07-02): the scanner used to call maybe_auto_trade
    directly and persist NOTHING — its fires never appeared in signal
    history and blocks never updated auto_trade_last_block_reason, so the
    UI showed 'verdict is WAIT' while the scanner traded in parallel. Now
    every ACTIONABLE scanner verdict:
      1. writes a signal_iter row (reasoning '[SCANNER <sym>] ...',
         extras.source='scanner' + scanner_symbol + trigger/option data +
         tag-only chop/TOD/bias context for future filters),
      2. passes the CHOP SKIP-GATE (engine/chop_gate.py — 2026-07-02
         22,683-fire sweep): if the trailing 30-min SPX range percentile
         vs the same time-of-day over the prior 20 sessions is below
         UIConfig.chop_gate_pct_threshold (default 33.3, compressed
         tercile), the fire is SKIPPED — no maybe_auto_trade call; the
         signal row stays in history tagged extras.skipped_by='chop_gate'
         and a category='chop_gate' audit row records the counterfactual.
         Missing data (percentile None) NEVER skips — gate passes open,
      2b. passes the BIAS-DISAGREE VETO (2026-07-02, promoted from tag):
         a fire whose reasoning carries the '(bias-disagrees, demoted)'
         stamp is skipped when UIConfig.bias_veto_enabled — signal row
         tagged extras.skipped_by='bias_veto', category='bias_veto' audit
         row. Chop gate wins when both would skip (first gate wins),
      2c. passes the FOMC-MORNING GATE (2026-07-06 event study: FOMC
         pre-14:00 fires -$61/ct t=-4.4, 80% of FOMC sessions negative):
         on an event_calendar FOMC day before 14:00 ET the fire is SKIPPED —
         extras.skipped_by='fomc_gate', category='fomc_gate' audit row,
         block_reason_code=FOMC_GATE_SKIP. UIConfig.fomc_gate_enabled
         (default true, hot-read),
      2d. passes the TOD NO-TRADE WINDOW (user request 2026-07-06; sweep's
         worst OOS bucket 14:00-15:30 ET at -$21/ct): a fire whose ET clock
         time falls in [tod_gate_start_et, tod_gate_end_et) is SKIPPED —
         extras.skipped_by='tod_gate', category='tod_gate' audit row,
         block_reason_code=TOD_GATE_SKIP. UIConfig.tod_gate_enabled +
         window edges hot-read. Gate order: chop -> bias -> fomc -> tod
         (first gate to skip wins; distinct audit categories),
      2e. TIER SCOPING (2026-07-09, UIConfig.gate_high_bypass_enabled,
         default true): a HIGH-confidence verdict BYPASSES 2, 2b and 2d
         (chop / bias-veto / TOD). Those gates run ahead of the now-armed
         HIGH confidence floor, so on HIGH they only ever pre-empted fires
         the floor already passes (study: ~+$3,134/ct and ~35% of fire
         frequency left on the table). 2c (FOMC) is NOT bypassed — it binds
         on ALL tiers. The bypass ALSO requires the floor ARMED (its
         FEF_DISABLE_CONFIDENCE_FLOOR kill-switch OFF) - with the floor gone
         there is nothing to catch the regrade gap it opens, so gates apply
         to all tiers (2026-07-09 review MAJOR-2). A bypassing HIGH fire the
         old all-tiers chain would have chop/bias/TOD-skipped is tagged
         extras.high_bypassed_gate='chop'|'bias'|'tod' + a [HIGH-BYPASS
         <gate>] reasoning note, written POST-FIRE (only once the fire
         actually TRADES, so it can never co-occur with a floor/cap
         skipped_by on the same row - review MAJOR-1); observable, never
         blocking. LOW/MEDIUM run the full chain unchanged. Flag false (or a
         failed gate-config read) => old all-tiers behavior,
      3. flows through maybe_auto_trade with source='auto_scanner'
         (per-symbol lock + all gates; cool-downs/caps are shared with the
         pattern trigger and main loop on the same symbol),
      4. surfaces its outcome in state.auto_trade_last_block_reason and —
         when blocked while auto-trade is ON — an auto_trade_block audit
         row carrying the source.
    WAIT verdicts stay unpersisted (the scanner panel + /auto_trade/status
    show them live; persisting every 60s WAIT scan would flood analytics).
    """
    direction = verdict_dict.get("direction", "WAIT")
    if direction not in ("LONG_CALLS", "LONG_PUTS"):
        return

    # ---- Entry skip-gates + tag-only annotations (2026-07-02/06 sweeps) ----
    # Gate ORDER (first gate wins, each skip logged distinctly so the
    # counterfactual populations never blur):
    #   1. chop gate (compressed-regime skip)
    #   2. bias veto (drift-bias disagreement skip)
    #   3. FOMC-morning gate (FOMC day, pre-14:00 ET statement skip)
    #   4. TOD no-trade window (default 14:00-15:30 ET skip)
    #   5. wall proximity — engine-internal, already produced a WAIT verdict
    #      upstream, so it never reaches this function.
    chop, gates = await _chop_gate_context(state)
    gate_enabled, gate_threshold = gates.chop_enabled, gates.chop_threshold
    bias_veto_enabled = gates.bias_veto_enabled
    reasoning_txt = verdict_dict.get("reasoning", "") or ""
    # The engine stamps ' (bias-disagrees, demoted)' into ALIGNED reasoning
    # when drift_bias opposes the fire direction — parse it out.
    bias_disagree = "bias-disagrees" in reasoning_txt
    # ---- Tier-scoped gate bypass (2026-07-09 registered study) ----
    # The chop / bias-veto / TOD gates run BEFORE the now-armed HIGH
    # confidence floor, so on a HIGH fire they only ever pre-empt trades the
    # floor already passes — profitable ones (study: ~+$3,134/ct and ~35% of
    # fire frequency left on the table). When gate_high_bypass_enabled a
    # HIGH-confidence verdict BYPASSES those three; LOW/MEDIUM fires run the
    # full chain unchanged. FOMC is NOT bypassed — it binds on ALL tiers as an
    # event-risk guard (blocks HIGH fires that lose -$28/ct on FOMC mornings).
    # The confidence floor remains the downstream arbiter (a wall-collapse
    # HIGH badge it re-grades is still re-graded there — bypassing chop/bias/
    # tod can never trade a fire the floor would reject). Mental model: gates
    # apply BELOW the floor tier, not above it.
    verdict_confidence = str(verdict_dict.get("confidence", "LOW"))
    # MAJOR-2 coupling (2026-07-09 review): the bypass is only SAFE because the
    # ARMED HIGH floor catches the gap it opens - a raw-HIGH wall-collapse badge
    # that bypasses chop is re-graded + floor-blocked downstream. The floor has a
    # kill-switch (FEF_DISABLE_CONFIDENCE_FLOOR); when it is ON the floor is gone,
    # so the bypass MUST also be off (gates apply to all tiers - safe: no floor
    # left to catch the regrade). Read the SAME switch the floor reads
    # (auto_trader._floor_kill_switch_active - never a copy). Probe failure =>
    # treat the floor as DISARMED (bypass off), fail-restrictive.
    floor_armed = True
    try:
        from fef_sidecar.engine.auto_trader import _floor_kill_switch_active
        floor_armed = not _floor_kill_switch_active()
    except Exception:
        logger.exception(
            "scanner: floor kill-switch probe failed — treating floor DISARMED "
            "(HIGH-bypass off, gates apply to all tiers)"
        )
        floor_armed = False
    high_bypass = bool(
        gates.high_bypass_enabled and verdict_confidence == "HIGH" and floor_armed
    )
    # Raw per-gate block conditions in chain order — each already encodes "no
    # earlier gate in the chop->bias->tod sub-chain already won". Tier scoping
    # is applied AFTER: chop_skip/bias_skip (and tod_skip below) are forced
    # open for a bypassing HIGH fire. FOMC (further down) is computed from the
    # SCOPED skips, so it still catches a HIGH fire whose chop/bias would
    # otherwise have won first.
    chop_would_block = bool(
        gate_enabled and chop is not None and chop.is_compressed(gate_threshold)
    )
    # Bias veto (2026-07-02): 3 of the day's 5 live losers wore the tag
    # (-$1,710); sweep OOS disagree -$17.00/ct vs agree -$1.85/ct. Applies
    # only when the chop gate did not already skip (first gate wins).
    bias_would_block = bool(
        bias_veto_enabled and bias_disagree and not chop_would_block
    )
    chop_skip = bool(chop_would_block and not high_bypass)
    bias_skip = bool(bias_would_block and not high_bypass)
    # FOMC-morning gate (2026-07-06 event study: FOMC pre-statement fires
    # -$61/ct t=-4.4, 80% of FOMC sessions negative). Skips actionable fires
    # on an event_calendar FOMC day while the ET clock is before the 14:00
    # statement. Evaluated only when no earlier gate already skipped.
    session_date_now, minute_now = _now_et_parts()
    try:
        from fef_sidecar.engine.event_calendar import is_fomc_day
        fomc_day = is_fomc_day(session_date_now)
    except Exception:
        logger.exception("scanner: FOMC calendar lookup failed — gate passes open")
        fomc_day = False
    fomc_pre_statement = bool(fomc_day and minute_now < FOMC_STATEMENT_MINUTE_ET)
    fomc_skip = bool(
        gates.fomc_gate_enabled and fomc_pre_statement
        and not (chop_skip or bias_skip)
    )
    # TOD no-trade window (user request 2026-07-06; sweep's worst OOS bucket
    # 14:00-15:30 ET at -$21/ct). start inclusive, end exclusive.
    tod_in_window = bool(
        gates.tod_start_min is not None and gates.tod_end_min is not None
        and gates.tod_start_min <= minute_now < gates.tod_end_min
    )
    tod_would_block = bool(
        gates.tod_gate_enabled and tod_in_window
        and not (chop_skip or bias_skip or fomc_skip)
    )
    tod_skip = bool(tod_would_block and not high_bypass)
    skipped_by = (
        "chop_gate" if chop_skip
        else "bias_veto" if bias_skip
        else "fomc_gate" if fomc_skip
        else "tod_gate" if tod_skip
        else None
    )
    # Counterfactual bypass tag (tier scoping observability): a HIGH fire that
    # FOMC did NOT catch proceeds to maybe_auto_trade even though the OLD
    # all-tiers chop->bias->tod chain would have skipped it. Record the first
    # such gate so the behavioral change (fires that NOW trade) is gradeable
    # in the verdict window. Tag-only — never blocks. When FOMC DID catch the
    # HIGH fire there is no bypass to grade (the fire is still skipped); the
    # fomc_skip row is written as usual and this stays None. Because we only
    # reach the tod branch when chop_would_block and bias_would_block are both
    # False, tod_would_block there equals the old chain's tod decision.
    high_bypassed_gate: str | None = None
    if high_bypass and not fomc_skip:
        if chop_would_block:
            high_bypassed_gate = "chop"
        elif bias_would_block:
            high_bypassed_gate = "bias"
        elif tod_would_block:
            high_bypassed_gate = "tod"
    chop_extras: dict[str, Any] = (
        chop.to_extras() if chop is not None
        else {"r30": None, "r30_pct": None, "tod_bucket": None,
              "data_gap": "chop assessment unavailable"}
    )
    chop_extras.update({
        "enabled": gate_enabled,
        "threshold_pct": gate_threshold,
        "skipped": chop_skip,
    })
    fomc_extras: dict[str, Any] = {
        "enabled": gates.fomc_gate_enabled,
        "fomc_day": fomc_day,
        "pre_statement": fomc_pre_statement,
        "minute_et": minute_now,
        "statement_minute_et": FOMC_STATEMENT_MINUTE_ET,
        "skipped": fomc_skip,
    }
    tod_extras: dict[str, Any] = {
        "enabled": gates.tod_gate_enabled,
        "window_et": gates.tod_window_label,
        "in_window": tod_in_window,
        "minute_et": minute_now,
        "skipped": tod_skip,
    }
    # LO-VIX DAY (2026-07-07): session-level VIX-open-percentile classifier
    # stamped on every scanner fire, fired or gate-skipped — true/false/
    # null(unknown). TAG-ONLY + input to the spx_target_watcher
    # lowvix_shrink_shadow; never gates. Session-cached => one DB read per
    # session (engine/lo_vix_day.py).
    lo_vix_day: bool | None = None
    try:
        from fef_sidecar.engine.lo_vix_day import is_lo_vix_day
        lo_vix_day = await is_lo_vix_day(state)
    except Exception:
        logger.exception("scanner: lo_vix_day classification failed — stamping null")
    if chop_skip:
        skip_note = (
            f" [CHOP-GATE SKIPPED: r30 pct {chop.percentile:.1f} < {gate_threshold:.1f}]"
        )
    elif bias_skip:
        skip_note = " [BIAS-VETO SKIPPED]"
    elif fomc_skip:
        skip_note = " [FOMC-GATE SKIPPED: pre-statement]"
    elif tod_skip:
        skip_note = (
            f" [TOD-GATE SKIPPED: {_fmt_minute_et(minute_now)} in "
            f"{gates.tod_window_label} ET]"
        )
    else:
        skip_note = ""
    # NOTE (2026-07-09 review MAJOR-1): the [HIGH-BYPASS <gate>] reasoning note
    # and extras.high_bypassed_gate are NOT written here (pre-fire). A HIGH fire
    # that bypasses chop/bias/tod may STILL be blocked downstream by the floor
    # (wall-collapse re-grade) or the cross-symbol cap, whose _tag_signal_skip
    # MERGES skipped_by into THIS same row - pre-fire tagging then mislabeled
    # non-traded rows as 'bypassed and traded' and inflated the grading. Both
    # are written by _tag_high_bypass AFTER maybe_auto_trade returns fired=True.
    # Barrier class of the nearest wall (decision-support tag, never gates):
    # True = the wall lies in the fire's direction of travel (today's 7440P
    # pattern = toward-barrier — flat-to-POSITIVE at scale per the wall
    # validation), False = wall behind the trade, None = no wall data.
    _nw_strike = verdict_dict.get("nearest_wall_strike")
    nearest_wall_toward: bool | None = None
    if _nw_strike is not None:
        nearest_wall_toward = (
            _nw_strike >= spot if direction == "LONG_CALLS" else _nw_strike <= spot
        )

    signal_id = None
    try:
        from fef_sidecar.db import FEFConfidence, FEFDirection
        from fef_sidecar.loop import (
            _current_account_class,
            _current_provider_mode,
            _walls_json,
        )
        conf_value = verdict_confidence
        signal = await SignalIterRepository.write_iter(
            spx_price=spot,
            direction=FEFDirection(direction),
            confidence=FEFConfidence(conf_value) if conf_value in FEFConfidence._value2member_map_ else FEFConfidence.LOW,
            reasoning=f"[SCANNER {symbol}] {verdict_dict.get('reasoning', '')}{skip_note}",
            drift_cum_net_m=(inputs.drift_cum_net_m if inputs is not None else 0.0),
            drift_bias=(inputs.drift_bias if inputs is not None else "NEUTRAL"),
            ntm_max_pain=(inputs.ntm_max_pain if inputs is not None else 0.0),
            raw_max_pain=(inputs.raw_max_pain if inputs is not None else 0.0),
            top_gex_walls=_walls_json(inputs.top_gex_walls) if inputs is not None else [],
            top_dex_walls=_walls_json(inputs.top_dex_walls) if inputs is not None else [],
            extras={
                "source": "scanner",
                "scanner_symbol": symbol,
                "trigger_type": verdict_dict.get("trigger_type"),
                "aligned": verdict_dict.get("aligned"),
                "wall_collapse_trigger": verdict_dict.get("wall_collapse_trigger"),
                "strike_rec": verdict_dict.get("strike_rec"),
                "option_pricing": verdict_dict.get("option_pricing"),
                "account_class": _current_account_class(state),
                "account_id": getattr(state.ibkr, "account_id", "") or "",
                # TAG-ONLY context on every scanner fire (live data for
                # future filters): drift-bias disagreement, 30-min ET time
                # bucket, the TOD-relative 30-min range percentile, the
                # lo-VIX day flag, and the nearest per-family wall
                # (strike/distance/family — was dropped from extras until
                # the 2026-07-02 proximity fix).
                "bias_disagree": bias_disagree,
                "tod_bucket": chop_extras.get("tod_bucket"),
                "r30_pct": chop_extras.get("r30_pct"),
                "lo_vix_day": lo_vix_day,
                "nearest_wall_strike": verdict_dict.get("nearest_wall_strike"),
                "nearest_wall_distance": verdict_dict.get("nearest_wall_distance"),
                "nearest_wall_family": verdict_dict.get("nearest_wall_family"),
                "nearest_wall_toward": nearest_wall_toward,
                "chop_gate": chop_extras,
                "bias_veto_enabled": bias_veto_enabled,
                "fomc_gate": fomc_extras,
                "tod_gate": tod_extras,
                # Tier scoping (2026-07-09): the flag's live state on every
                # fire (parity with bias_veto_enabled) so the counterfactual
                # logs record whether HIGH-bypass was active for this row.
                "high_bypass_enabled": gates.high_bypass_enabled,
                # Counterfactual marker — row persisted, trade withheld.
                # 'chop_gate' / 'bias_veto' / 'fomc_gate' / 'tod_gate'
                # (first gate wins); absent on fires.
                **({"skipped_by": skipped_by} if skipped_by else {}),
                # high_bypassed_gate is written POST-FIRE by _tag_high_bypass
                # (review MAJOR-1), never here - see the note above the persist
                # block. It lands on FIRED rows only, so it can never co-occur
                # with a floor/cap skipped_by tag on the same row.
            },
            session_date=_today_et(),
            provider_mode=_current_provider_mode(state),
        )
        signal_id = signal.id
    except Exception:
        logger.exception("scanner: signal persist failed for %s (still evaluating trade)", symbol)

    if chop_skip:
        # Compressed-regime skip: the signal row above keeps the full
        # counterfactual visible in signal history (option_pricing included,
        # lookahead worker scores it like any other row); the audit row makes
        # the skip itself first-class. maybe_auto_trade is NOT called — no
        # cool-down consumed, no per-symbol budget spent.
        assert chop is not None  # chop_skip => assessed percentile exists
        msg = (
            f"CHOP-GATE SKIPPED SCANNER {direction} {symbol}: "
            f"r30 pct {chop.percentile:.1f} < {gate_threshold:.1f}"
        )
        state.auto_trade_last_block_reason = f"[scanner {symbol}] {msg}"
        logger.info("scanner: %s", msg)
        try:
            from fef_sidecar.engine.chop_gate import CHOP_GATE_BLOCK_CODE
            await SignalIterRepository.write_audit_log(
                category="chop_gate", actor="system",
                summary=msg,
                details={
                    "source": "auto_scanner",
                    "symbol": symbol,
                    "direction": direction,
                    "confidence": verdict_dict.get("confidence"),
                    "strike_rec": verdict_dict.get("strike_rec"),
                    "r30": chop.r30,
                    "r30_pct": round(chop.percentile, 2) if chop.percentile is not None else None,
                    "threshold_pct": gate_threshold,
                    "tod_bucket": chop.tod_bucket,
                    "n_samples": chop.n_samples,
                    "n_sessions": chop.n_sessions,
                    "signal_iter_id": signal_id,
                },
                block_reason_code=CHOP_GATE_BLOCK_CODE,
            )
        except Exception:
            logger.exception("scanner: chop_gate audit write failed for %s", symbol)
        return

    if bias_skip:
        # Bias-disagree veto: same counterfactual mechanics as the chop gate
        # (signal row persisted above with option_pricing + skipped_by, the
        # lookahead worker scores it), distinct category so the two filters'
        # saves/costs are separately measurable. No maybe_auto_trade call —
        # no cool-down consumed, no per-symbol budget spent.
        msg = (
            f"BIAS-VETO SKIPPED SCANNER {direction} {symbol}: "
            f"drift bias disagrees with fire direction"
        )
        state.auto_trade_last_block_reason = f"[scanner {symbol}] {msg}"
        logger.info("scanner: %s", msg)
        try:
            await SignalIterRepository.write_audit_log(
                category="bias_veto", actor="system",
                summary=msg,
                details={
                    "source": "auto_scanner",
                    "symbol": symbol,
                    "direction": direction,
                    "confidence": verdict_dict.get("confidence"),
                    "strike_rec": verdict_dict.get("strike_rec"),
                    "drift_bias": (inputs.drift_bias if inputs is not None else None),
                    "r30_pct": chop_extras.get("r30_pct"),
                    "tod_bucket": chop_extras.get("tod_bucket"),
                    "signal_iter_id": signal_id,
                },
                block_reason_code=BIAS_VETO_BLOCK_CODE,
            )
        except Exception:
            logger.exception("scanner: bias_veto audit write failed for %s", symbol)
        return

    if fomc_skip:
        # FOMC-morning skip: same counterfactual mechanics as the chop gate
        # (signal row persisted above with option_pricing + skipped_by, the
        # lookahead worker scores it), distinct category so this gate's
        # saves/costs are separately measurable. No maybe_auto_trade call —
        # no cool-down consumed, no per-symbol budget spent.
        msg = (
            f"FOMC-GATE SKIPPED SCANNER {direction} {symbol}: "
            f"pre-statement ({_fmt_minute_et(minute_now)} ET < 14:00 on "
            f"FOMC day {session_date_now})"
        )
        state.auto_trade_last_block_reason = f"[scanner {symbol}] {msg}"
        logger.info("scanner: %s", msg)
        try:
            await SignalIterRepository.write_audit_log(
                category="fomc_gate", actor="system",
                summary=msg,
                details={
                    "source": "auto_scanner",
                    "symbol": symbol,
                    "direction": direction,
                    "confidence": verdict_dict.get("confidence"),
                    "strike_rec": verdict_dict.get("strike_rec"),
                    "session_date": session_date_now,
                    "minute_et": minute_now,
                    "statement_minute_et": FOMC_STATEMENT_MINUTE_ET,
                    "r30_pct": chop_extras.get("r30_pct"),
                    "tod_bucket": chop_extras.get("tod_bucket"),
                    "signal_iter_id": signal_id,
                },
                block_reason_code=FOMC_GATE_BLOCK_CODE,
            )
        except Exception:
            logger.exception("scanner: fomc_gate audit write failed for %s", symbol)
        return

    if tod_skip:
        # TOD no-trade window skip: counterfactual mechanics identical to the
        # other gates, its own audit category. The window ships hot-tunable
        # (tod_gate_start_et / tod_gate_end_et) and the persisted skips let
        # the counterfactual logs grade the window like the other gates.
        msg = (
            f"TOD-GATE SKIPPED SCANNER {direction} {symbol}: "
            f"{_fmt_minute_et(minute_now)} ET inside no-trade window "
            f"{gates.tod_window_label}"
        )
        state.auto_trade_last_block_reason = f"[scanner {symbol}] {msg}"
        logger.info("scanner: %s", msg)
        try:
            await SignalIterRepository.write_audit_log(
                category="tod_gate", actor="system",
                summary=msg,
                details={
                    "source": "auto_scanner",
                    "symbol": symbol,
                    "direction": direction,
                    "confidence": verdict_dict.get("confidence"),
                    "strike_rec": verdict_dict.get("strike_rec"),
                    "window_et": gates.tod_window_label,
                    "minute_et": minute_now,
                    "r30_pct": chop_extras.get("r30_pct"),
                    "tod_bucket": chop_extras.get("tod_bucket"),
                    "signal_iter_id": signal_id,
                },
                block_reason_code=TOD_GATE_BLOCK_CODE,
            )
        except Exception:
            logger.exception("scanner: tod_gate audit write failed for %s", symbol)
        return

    try:
        from fef_sidecar.engine.auto_trader import maybe_auto_trade
        decision = await maybe_auto_trade(
            state, verdict_dict, symbol=symbol, source="auto_scanner",
            # Counterfactual gates (floor/cap) tag the row persisted above.
            signal_iter_id=signal_id,
        )
        state.auto_trade_last_block_reason = f"[scanner {symbol}] {decision.reason}"
        if decision.fired and decision.order_result:
            logger.info("scanner: %s auto-trade fired: %s", symbol, decision.order_result)
            # Tag the HIGH-bypass ONLY now that the fire actually TRADED (review
            # MAJOR-1). fired=True always carries order_result - the sole fired
            # return is AutoTradeDecision(True, ..., order_result=result) - so
            # this branch == the fire outcome, and high_bypassed_gate can never
            # co-occur with a floor/cap skipped_by tag on the same row.
            if high_bypassed_gate:
                await _tag_high_bypass(signal_id, high_bypassed_gate)
        elif state.auto_trade_enabled and decision.reason != "auto_trade disabled":
            try:
                await SignalIterRepository.write_audit_log(
                    category="auto_trade_block", actor="system",
                    summary=(
                        f"BLOCKED SCANNER {direction} {symbol}: {decision.reason}"
                    ),
                    details={
                        "source": "auto_scanner",
                        "symbol": symbol,
                        "direction": direction,
                        "confidence": verdict_dict.get("confidence"),
                        "strike_rec": verdict_dict.get("strike_rec"),
                        "reason": decision.reason,
                        "reason_code": decision.reason_code,
                        "signal_iter_id": signal_id,
                    },
                    block_reason_code=decision.reason_code,
                )
            except Exception:
                pass
    except Exception:
        logger.exception("scanner: auto-trade evaluation failed for %s", symbol)


def _today_et() -> str:
    from datetime import datetime, timedelta, timezone
    et = timezone(timedelta(hours=-4))
    return datetime.now(et).strftime("%Y-%m-%d")


async def multi_symbol_scanner_loop(state) -> None:
    """Background loop. Every FEF_SCAN_INTERVAL_SEC, iterates through
    enabled symbols and runs a scan for each. Skips disabled symbols
    and never touches the active chart symbol's primary loop.
    """
    from fef_sidecar.engine.loop_watchdog import heartbeat as _wd_beat
    interval = float(os.environ.get("FEF_SCAN_INTERVAL_SEC", "60"))
    logger.info("multi-symbol scanner loop starting (interval=%.1fs)", interval)

    # Wait a few seconds for the main fef_tick_loop's warmup to settle
    await asyncio.sleep(15)

    while True:
        _wd_beat(state, "multi-symbol-scanner")
        try:
            enabled = [
                s for s, on in state.scan_enabled_symbols.items() if on
            ]
            for symbol in enabled:
                try:
                    await _scan_one_symbol(state, symbol)
                    await asyncio.sleep(0.5)  # spread QD load
                except Exception:
                    logger.exception("scanner: %s scan failed", symbol)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("multi-symbol scanner loop iteration failed")
        await asyncio.sleep(interval)
