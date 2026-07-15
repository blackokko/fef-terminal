"""Proof-of-construction gate (Phase C, 2026-07-15): the LIVE theta-flow
builder code path must reproduce the stored, validated `option_flow_minute`
construction from the Theta Terminal tape.

Runs `rebuild_session` — the EXACT function the live loop uses for boot
recovery and the EOD self-check — against one historical session and compares
to the stored table (read-only). Measured result at build time (3 sessions:
2026-06-02 calm, 2026-06-10 vol, 2025-10-15): pearson r = 1.000000, 390/390
exact-match minutes, every premium/volume sum ratio 1.000000, drift max
|cum diff| = 0. The asserted gate below is deliberately looser (r > 0.99,
sums within 2%) so a minor vendor tape revision degrades to a WARNING-grade
failure rather than flaking; a real construction drift will still fail loudly.

SKIPPED (not failed) when the local Theta Terminal is not listening on
127.0.0.1:25503 or the production DB / stored table is unavailable.
"""

from __future__ import annotations

import asyncio
import math
import os
import socket
import sqlite3

import pytest

TERMINAL_HOST, TERMINAL_PORT = "127.0.0.1", 25503
PROD_DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
SESSION = "2026-06-02"


def _terminal_up() -> bool:
    try:
        with socket.create_connection((TERMINAL_HOST, TERMINAL_PORT), timeout=1.5):
            return True
    except OSError:
        return False


def _stored_rows() -> dict[int, tuple] | None:
    if not os.path.exists(PROD_DB):
        return None
    try:
        con = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
        rows = {
            int(r[0]): r[1:]
            for r in con.execute(
                "SELECT ts_ms, call_buy_prem, call_sell_prem, put_buy_prem, "
                "put_sell_prem, net_signed_prem FROM option_flow_minute "
                "WHERE session_date=? ORDER BY ts_ms", (SESSION,))
        }
        con.close()
        return rows or None
    except Exception:
        return None


def _pearson(xs, ys) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy)


@pytest.mark.skipif(not _terminal_up(), reason="Theta Terminal not running on 25503")
def test_live_builder_reproduces_validated_construction():
    stored = _stored_rows()
    if stored is None:
        pytest.skip("prod DB / option_flow_minute rows unavailable")

    import httpx
    from fef_sidecar.engine.theta_flow_ingest import rebuild_session

    async def _rebuild():
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            return await rebuild_session(client, SESSION, force=True)

    builder = asyncio.run(_rebuild())
    rebuilt = {m: r[2:] for m, r in builder.rows.items()}

    common = sorted(set(stored) & set(rebuilt))
    # Row spine must match (the stored construction is 390 RTH minutes).
    assert len(common) >= 385, (
        f"minute spine mismatch: stored={len(stored)} rebuilt={len(rebuilt)} "
        f"common={len(common)}"
    )

    # net_signed_prem per-minute correlation — THE gate (target > 0.99).
    net_s = [stored[m][4] for m in common]
    net_r = [rebuilt[m][8] for m in common]
    r = _pearson(net_s, net_r)
    assert r > 0.99, f"net_signed_prem correlation {r:.6f} <= 0.99"

    # Premium sums within a few percent (measured: exactly 1.000000).
    for i_s, i_r, name in ((0, 0, "call_buy_prem"), (1, 1, "call_sell_prem"),
                           (2, 2, "put_buy_prem"), (3, 3, "put_sell_prem")):
        ss = sum(stored[m][i_s] for m in common)
        sr = sum(rebuilt[m][i_r] for m in common)
        assert ss > 0
        ratio = sr / ss
        assert abs(ratio - 1.0) < 0.02, f"{name} sum ratio {ratio:.4f} off > 2%"

    # Drift series: EOD cumsum within 2% and no minute of the cum path
    # diverging by more than 2M$ (measured: 0.0000M).
    cs = cr = 0.0
    max_cum_diff = 0.0
    for m in common:
        cs += stored[m][4]
        cr += rebuilt[m][8]
        max_cum_diff = max(max_cum_diff, abs(cs - cr))
    assert abs(cs) > 0
    assert abs(cr - cs) <= max(0.02 * abs(cs), 1e6), (
        f"EOD drift mismatch: stored {cs/1e6:+.3f}M rebuilt {cr/1e6:+.3f}M")
    assert max_cum_diff <= 2e6, f"drift path diverged {max_cum_diff/1e6:.2f}M"
