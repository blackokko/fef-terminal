"""Loop watchdog — heartbeat supervisor for critical background tasks.

2026-07-07 incident class: async tasks die or wedge SILENTLY. The bar
writer stopped producing rows at ~13:31Z (a wedged keepUpToDate
subscription blocks `await queue.get()` forever — no exception, no log),
the live_position snapshot writer wrote its last row at 13:33:31Z, and
nothing surfaced either until the EOD audit. This module makes silent
death impossible for supervised loops:

  * every supervised loop stamps a HEARTBEAT (name -> monotonic seconds)
    on each healthy cycle via `heartbeat(state, name)`;
  * the loop body is wrapped (`spawn_supervised`) so an escaping
    exception writes an audit row category="loop_watchdog" with the full
    traceback in details BEFORE the task exits;
  * a supervisor (`loop_watchdog_loop`, every FEF_WATCHDOG_CHECK_SEC=30s)
    detects dead tasks and stale heartbeats (per-loop threshold, default
    3x cadence), writes a loud audit row once per detection (not per
    check), and respawns the loop — max FEF_WATCHDOG_MAX_RESPAWNS_PER_HOUR
    (3) per loop, then a single "loop_dead_giving_up" row until the
    hourly budget frees again.

Re-spawning a name that is already registered cancels the previous task
first — this also structurally prevents the /config/symbol duplicate-task
leak (spawn_data_loops used to re-create watcher/scanner tasks that
cancel_data_loops never cancelled).

Intentional cancellation (shutdown, symbol-switch respawn) marks the
entry cancelled and is never treated as a death.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

WATCHDOG_CHECK_SEC = float(os.environ.get("FEF_WATCHDOG_CHECK_SEC", "30"))
MAX_RESPAWNS_PER_HOUR = int(os.environ.get("FEF_WATCHDOG_MAX_RESPAWNS_PER_HOUR", "3"))


@dataclass
class SupervisedLoop:
    name: str
    factory: Callable[[], Coroutine[Any, Any, None]]
    cadence_sec: float
    stale_after_sec: float
    task: Any | None = None
    respawn_ts: list[float] = field(default_factory=list)
    gave_up: bool = False       # respawn budget exhausted (resets when window frees)
    cancelled: bool = False     # intentional cancel — never respawn
    completed: bool = False     # clean return — never respawn


def heartbeat(state: Any, name: str) -> None:
    """Stamp a healthy cycle for `name`. Must never raise and must be cheap
    enough for a 0.1s loop — a dict store on the shared state."""
    try:
        hb = getattr(state, "loop_heartbeats", None)
        if hb is None:
            hb = {}
            state.loop_heartbeats = hb
        hb[name] = time.monotonic()
    except Exception:
        pass


def _registry(state: Any) -> dict[str, SupervisedLoop]:
    reg = getattr(state, "watchdog_loops", None)
    if reg is None:
        reg = {}
        try:
            state.watchdog_loops = reg
        except Exception:
            pass
    return reg


async def _write_watchdog_audit(summary: str, details: dict | None = None,
                                category: str = "loop_watchdog") -> None:
    """Best-effort audit write — a broken DB must not take the supervisor
    down with the loops it is supervising."""
    try:
        from fef_sidecar.db.repository import SignalIterRepository
        await SignalIterRepository.write_audit_log(
            category=category, actor="system", summary=summary,
            details=details,
        )
    except Exception:
        logger.exception("loop_watchdog: audit write failed (%s)", summary)


async def _supervised_body(
    state: Any, spec: SupervisedLoop, wait_for_prev: Any | None = None,
) -> None:
    """Run one supervised loop to completion. An escaping exception lands a
    traceback in the audit log BEFORE the task exits — the supervisor then
    respawns from the heartbeat/done check.

    wait_for_prev: the cancelled predecessor task. Awaited (bounded) before
    this instance starts — two live instances of one loop must never overlap
    (2026-07-07 review: overlapping poll-loop instances each held their own
    ORM parent copies and could both sell the same tier)."""
    if wait_for_prev is not None:
        try:
            await asyncio.wait_for(
                asyncio.gather(wait_for_prev, return_exceptions=True),
                timeout=10.0,
            )
        except Exception:
            logger.warning(
                "supervised loop %s: predecessor did not finish within 10s — starting anyway",
                spec.name,
            )
    heartbeat(state, spec.name)
    try:
        await spec.factory()
        # Identity guard: only the CURRENT registered run may mark the spec
        # terminal — a superseded body (watchdog replaced a wedged task and
        # cancelled it afterwards) must not flip flags on the live spec.
        if spec.task is asyncio.current_task():
            spec.completed = True
        logger.info("supervised loop %s returned cleanly — not respawning", spec.name)
    except asyncio.CancelledError:
        if spec.task is asyncio.current_task():
            spec.cancelled = True
        raise
    except Exception as e:
        tb = traceback.format_exc()
        logger.exception("supervised loop %s DIED", spec.name)
        await _write_watchdog_audit(
            f"LOOP DIED: {spec.name} — {e!r} (traceback in details; supervisor will respawn)",
            details={"loop": spec.name, "error": repr(e), "traceback": tb},
        )


def spawn_supervised(
    state: Any,
    name: str,
    factory: Callable[[], Coroutine[Any, Any, None]],
    *,
    cadence_sec: float,
    stale_after_sec: float | None = None,
) -> Any:
    """Create (or replace) a supervised background task.

    factory: zero-arg callable returning a FRESH coroutine — invoked again
    on every respawn. stale_after_sec defaults to 3x cadence_sec; pass an
    explicit value for loops whose single iteration can legitimately run
    long (position monitor with many positions, scanner with many symbols).
    """
    reg = _registry(state)
    prev = reg.get(name)
    prev_task = None
    if prev is not None and prev.task is not None and not prev.task.done():
        # Replacing a live loop (symbol-switch respawn / re-registration) —
        # cancel the old task; the new instance AWAITS it before starting
        # (see _supervised_body) so two copies never run side by side.
        prev.cancelled = True
        prev.task.cancel()
        prev_task = prev.task
    spec = SupervisedLoop(
        name=name, factory=factory, cadence_sec=float(cadence_sec),
        stale_after_sec=(
            float(stale_after_sec) if stale_after_sec is not None
            else 3.0 * float(cadence_sec)
        ),
    )
    spec.task = asyncio.create_task(
        _supervised_body(state, spec, wait_for_prev=prev_task), name=name,
    )
    heartbeat(state, name)
    reg[name] = spec
    return spec.task


async def _watchdog_check_once(state: Any, *, now: float | None = None) -> list[str]:
    """One supervisor pass over the registry. Returns the actions taken
    (["respawned:<name>", "gave_up:<name>", ...]) so tests can drive checks
    directly without waiting out the 30s cadence."""
    actions: list[str] = []
    reg = _registry(state)
    hbs = getattr(state, "loop_heartbeats", {}) or {}
    if now is None:
        now = time.monotonic()
    # BLOCKER-1 (2026-07-07 review): never cancel/respawn tasks while an exit
    # fire is in flight — cancellation mid-sell is exactly how an optimistic
    # latch gets stranded. AGE-BOUNDED (re-review N1): defer only while the
    # youngest tracked fire is younger than the fire deadline + slack — a
    # wedge past that is exactly what the watchdog exists for, and an
    # unconditional deferral would freeze respawns of EVERY loop (bar/tick
    # streams included) for as long as the wedge lasts. No tracked fires
    # (leaked firing id) also proceeds.
    if getattr(state, "firing_parent_ids", None):
        starts = getattr(state, "_watcher_fire_started_mono", None) or {}
        if starts:
            defer_max = float(os.environ.get("FEF_WATCHDOG_FIRE_DEFER_MAX_SEC", "45"))
            youngest_age = min(time.monotonic() - t0 for t0 in starts.values())
            if youngest_age < defer_max:
                return actions
    for spec in list(reg.values()):
        if spec.cancelled or spec.completed:
            continue
        task_dead = spec.task is None or spec.task.done()
        hb_age = now - hbs.get(spec.name, 0.0)
        stale = hb_age > spec.stale_after_sec
        if not task_dead and not stale:
            if spec.gave_up:
                spec.gave_up = False  # recovered on its own — re-arm the budget
            continue
        # Respawn budget: max N per rolling hour, then one giving-up row.
        spec.respawn_ts = [t for t in spec.respawn_ts if now - t < 3600.0]
        if len(spec.respawn_ts) >= MAX_RESPAWNS_PER_HOUR:
            if not spec.gave_up:
                spec.gave_up = True
                actions.append(f"gave_up:{spec.name}")
                await _write_watchdog_audit(
                    f"LOOP DEAD (giving up): {spec.name} respawned "
                    f"{len(spec.respawn_ts)}x in the last hour and is still "
                    f"{'dead' if task_dead else 'stalled'} — manual intervention needed",
                    details={"loop": spec.name, "respawns_last_hour": len(spec.respawn_ts),
                             "heartbeat_age_sec": round(hb_age, 1)},
                    category="loop_watchdog",
                )
            continue
        spec.gave_up = False
        await _write_watchdog_audit(
            f"LOOP {'DEAD' if task_dead else 'STALLED'}: {spec.name} — last heartbeat "
            f"{hb_age:.0f}s ago (threshold {spec.stale_after_sec:.0f}s) — respawning "
            f"({len(spec.respawn_ts) + 1}/{MAX_RESPAWNS_PER_HOUR} this hour)",
            details={"loop": spec.name, "task_dead": task_dead,
                     "heartbeat_age_sec": round(hb_age, 1),
                     "stale_after_sec": spec.stale_after_sec},
        )
        try:
            old_task = spec.task
            if old_task is not None and not old_task.done():
                old_task.cancel()  # wedged, not dead — replace it
            # The replacement awaits the cancelled predecessor before it
            # starts (never two live instances of one loop side by side).
            spec.task = asyncio.create_task(
                _supervised_body(state, spec, wait_for_prev=old_task),
                name=spec.name,
            )
            spec.respawn_ts.append(now)
            heartbeat(state, spec.name)
            actions.append(f"respawned:{spec.name}")
        except Exception:
            logger.exception("loop_watchdog: respawn of %s failed", spec.name)
    return actions


async def loop_watchdog_loop(state: Any) -> None:
    """Supervisor task — runs `_watchdog_check_once` every
    FEF_WATCHDOG_CHECK_SEC (default 30s). Exception-guarded per pass."""
    logger.info("loop watchdog starting (check every %.0fs)", WATCHDOG_CHECK_SEC)
    while True:
        try:
            await _watchdog_check_once(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("loop watchdog check failed")
        await asyncio.sleep(WATCHDOG_CHECK_SEC)
