"""Loop watchdog (2026-07-07): silent async-task death made loud + self-healing.

The incident class: the bar writer wedged at ~13:31Z (blocked forever on a
dead keepUpToDate subscription — no exception, no log) and the live_position
snapshot writer died the same minute; nothing surfaced until the EOD audit.
Covers: death -> traceback in audit details; supervisor respawn (dead task +
stale heartbeat); respawn budget -> loop_dead_giving_up; intentional cancels
never respawned; duplicate registration replaces the live task.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.engine.loop_watchdog import (
    MAX_RESPAWNS_PER_HOUR,
    _watchdog_check_once,
    heartbeat,
    spawn_supervised,
)


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


async def _drain() -> None:
    await asyncio.sleep(0.02)
    for t in asyncio.all_tasks():
        if t is not asyncio.current_task() and not t.done():
            t.cancel()
    await asyncio.sleep(0)


async def test_escaping_exception_lands_traceback_in_audit_details() -> None:
    state = SimpleNamespace()

    async def dies():
        heartbeat(state, "dier")
        raise RuntimeError("deliberately killed")

    spawn_supervised(state, "dier", dies, cadence_sec=0.01, stale_after_sec=3600)
    await asyncio.sleep(0.05)

    rows = await SignalIterRepository.query_audit_log(category="loop_watchdog")
    died = [r for r in rows if "LOOP DIED: dier" in r.summary]
    assert len(died) == 1
    tb = died[0].details.get("traceback", "")
    assert "RuntimeError" in tb and "deliberately killed" in tb
    await _drain()


async def test_dead_loop_is_detected_respawned_and_audited() -> None:
    state = SimpleNamespace()
    runs: list[int] = []
    die = {"flag": True}

    async def dummy():
        runs.append(1)
        heartbeat(state, "dummy")
        if die["flag"]:
            raise RuntimeError("boom")
        await asyncio.Event().wait()  # healthy park

    spawn_supervised(state, "dummy", dummy, cadence_sec=0.01, stale_after_sec=3600)
    await asyncio.sleep(0.05)  # first run dies
    assert runs == [1]

    die["flag"] = False
    actions = await _watchdog_check_once(state)
    assert "respawned:dummy" in actions
    await asyncio.sleep(0.05)
    assert runs == [1, 1]  # the respawned copy ran

    rows = await SignalIterRepository.query_audit_log(category="loop_watchdog")
    assert any("LOOP DEAD: dummy" in r.summary and "respawning" in r.summary for r in rows)
    # Healthy now -> further checks take no action.
    assert await _watchdog_check_once(state) == []
    await _drain()


async def test_stale_heartbeat_detects_wedged_task() -> None:
    """A task that is alive but WEDGED (the bar-writer failure mode) is
    detected by heartbeat age and replaced."""
    state = SimpleNamespace()
    started: list[int] = []

    async def wedged():
        started.append(1)
        heartbeat(state, "wedge")
        await asyncio.Event().wait()  # never beats again

    spawn_supervised(state, "wedge", wedged, cadence_sec=0.01, stale_after_sec=0.03)
    await asyncio.sleep(0.1)  # heartbeat now stale, task still alive

    actions = await _watchdog_check_once(state)
    assert "respawned:wedge" in actions
    await asyncio.sleep(0.05)
    assert len(started) == 2  # old copy cancelled, replacement running

    rows = await SignalIterRepository.query_audit_log(category="loop_watchdog")
    assert any("LOOP STALLED: wedge" in r.summary for r in rows)
    await _drain()


async def test_respawn_budget_exhausts_to_single_giving_up_audit() -> None:
    state = SimpleNamespace()

    async def always_dies():
        heartbeat(state, "hopeless")
        raise ValueError("always dies")

    spawn_supervised(state, "hopeless", always_dies, cadence_sec=0.01, stale_after_sec=3600)
    await asyncio.sleep(0.05)

    for _ in range(MAX_RESPAWNS_PER_HOUR):
        actions = await _watchdog_check_once(state)
        assert "respawned:hopeless" in actions
        await asyncio.sleep(0.05)  # each respawn dies again

    actions = await _watchdog_check_once(state)
    assert "gave_up:hopeless" in actions
    # Once per episode — subsequent checks stay silent.
    assert await _watchdog_check_once(state) == []

    rows = await SignalIterRepository.query_audit_log(category="loop_watchdog")
    assert sum(1 for r in rows if "giving up" in r.summary) == 1
    await _drain()


async def test_intentional_cancel_is_never_respawned() -> None:
    state = SimpleNamespace()

    async def loop_():
        heartbeat(state, "cancelme")
        await asyncio.Event().wait()

    task = spawn_supervised(state, "cancelme", loop_, cadence_sec=0.01, stale_after_sec=0.01)
    await asyncio.sleep(0.02)
    task.cancel()  # shutdown / symbol-switch teardown
    await asyncio.sleep(0.02)
    assert await _watchdog_check_once(state) == []
    await _drain()


async def test_respawn_same_name_replaces_live_task() -> None:
    """Re-registering a name (the /config/symbol respawn) cancels the old
    task — the duplicate-loop leak can't recur for supervised loops."""
    state = SimpleNamespace()
    alive: list[int] = []

    async def loop_():
        alive.append(1)
        try:
            heartbeat(state, "dup")
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            alive.pop()
            raise

    t1 = spawn_supervised(state, "dup", loop_, cadence_sec=1)
    await asyncio.sleep(0.02)
    t2 = spawn_supervised(state, "dup", loop_, cadence_sec=1)
    await asyncio.sleep(0.02)
    assert len(alive) == 1
    assert t1.done() and not t2.done()
    await _drain()
