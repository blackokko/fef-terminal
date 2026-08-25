"""Scanner cross-symbol price-contamination guard (2026-07-06).

Live failure: the GLD scanner engine received an SPX-priced snapshot —
"[SCANNER GLD] ALIGNED — ΔPrice +7147.15pts", one verdict row with
spx_price=7526.8 followed by 379.6. Mechanism: set_page_filter only mutates
the shared QuantData client's _ticker (no HTTP) and fetch_market_snapshot's
concurrent sub-fetches read it at request-build time, so the main poll's
set_page_filter(ticker='SPX') interleaving mid-GLD-fetch made some GLD
sub-requests carry ticker='SPX' → snapshot.spx_price came from SPX GEX.

Source race was closed by loop._qd_scope_lock (atomic set+fetch); these tests
cover the input-boundary backstop: a spot deviating >20% from the engine's
last ACCEPTED spot rejects the whole iteration (no engine.push, no verdict
cache, audit row 'scanner_input_glitch' with both values).

[REMOVED 2026-08-25] QuantData subscription CANCELLED. Two INTEGRATION tests in
this file drove `_scan_one_symbol` through a `_FakeQD` client
(set_page_filter/fetch_market_snapshot) with `mss.snapshot_to_inputs`
monkeypatched — i.e. they existed only to exercise the QuantData path, and both
that function and `_scan_one_symbol` are deleted. They are removed with it:
  * test_scan_one_symbol_skips_engine_on_contaminated_snapshot
  * test_scan_one_symbol_rejects_iteration_on_reference_pollution
The guard they wrapped, `_reject_scanner_input_glitch`, is UNCHANGED and still
covered directly by every other test here (band rejection, reference pollution,
threshold boundaries, window quarantine)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ["FEF_DB_URL"] = "sqlite+aiosqlite:///:memory:"

from fef_sidecar.db import SignalIterRepository, dispose_db, init_db
from fef_sidecar.engine.multi_symbol_scanner import _reject_scanner_input_glitch


@pytest.fixture(autouse=True)
async def _db_fixture():
    await dispose_db()
    await init_db()
    yield
    await dispose_db()


# ---------------------------------------------------------------------------
# Guard unit behavior
# ---------------------------------------------------------------------------


async def test_contaminated_spot_rejected_and_logged() -> None:
    state = SimpleNamespace()
    # First-ever spot: no baseline to judge against → accepted.
    assert await _reject_scanner_input_glitch(state, "GLD", 379.65) is False
    # The live glitch: an SPX price reaching the GLD engine.
    assert await _reject_scanner_input_glitch(state, "GLD", 7526.8) is True

    rows = await SignalIterRepository.query_audit_log(category="scanner_input_glitch")
    assert len(rows) == 1
    assert "7526.80" in rows[0].summary and "379.65" in rows[0].summary
    d = rows[0].details
    assert d["symbol"] == "GLD"
    assert d["incoming_spot"] == pytest.approx(7526.8)
    assert d["last_accepted_spot"] == pytest.approx(379.65)

    # Rejected value must NOT become the new baseline — the next sane GLD
    # tick is accepted (engine state / baseline unpolluted).
    assert state._scanner_last_spot["GLD"] == pytest.approx(379.65)
    assert await _reject_scanner_input_glitch(state, "GLD", 380.1) is False
    assert state._scanner_last_spot["GLD"] == pytest.approx(380.1)


async def test_large_but_sane_moves_pass() -> None:
    state = SimpleNamespace()
    await _reject_scanner_input_glitch(state, "GLD", 380.0)
    # +19% is huge but under the 20% guard → passes, baseline advances.
    assert await _reject_scanner_input_glitch(state, "GLD", 380.0 * 1.19) is False
    assert state._scanner_last_spot["GLD"] == pytest.approx(380.0 * 1.19)
    # +21% from the new baseline → rejected.
    assert await _reject_scanner_input_glitch(
        state, "GLD", 380.0 * 1.19 * 1.21,
    ) is True
    # Downside works symmetrically.
    assert await _reject_scanner_input_glitch(
        state, "GLD", 380.0 * 1.19 * 0.75,
    ) is True


async def test_per_symbol_baselines_are_independent() -> None:
    state = SimpleNamespace()
    await _reject_scanner_input_glitch(state, "GLD", 380.0)
    await _reject_scanner_input_glitch(state, "SPX", 7526.8)
    assert await _reject_scanner_input_glitch(state, "SPX", 7530.0) is False
    assert await _reject_scanner_input_glitch(state, "GLD", 7526.8) is True


# ---------------------------------------------------------------------------
# Integration: _scan_one_symbol skips the engine entirely on a glitch
# ---------------------------------------------------------------------------


class _FakeEngine:
    def __init__(self) -> None:
        self.pushed: list = []

    def push(self, inputs):
        self.pushed.append(inputs)
        return SimpleNamespace(
            direction=SimpleNamespace(value="WAIT"),
            confidence=SimpleNamespace(value="NONE"),
            reasoning="", strike_rec=None,
            t1=None, t2=None, t3=None, stop=None,
            delta_price=None, delta_drift_m=None, fe_ratio=None,
            aligned=False, absorption=None,
            wall_proximity_skip=False, wall_collapse_trigger=False,
            nearest_wall_strike=None, nearest_wall_distance=None,
            nearest_wall_family=None, secondary_scores={},
        )


# ---------------------------------------------------------------------------
# Reference-pollution extension (2026-07-06 19:07 residual): a VALID current
# spot judged against a POLLUTED previous reference in the engine's own delta
# window produced dP ±6,794 in 6 post-band-fix rows. The guard now validates
# the implied delta against max(50, 3% of spot) and RESETS/quarantines the
# engine's history window on a violation.
# ---------------------------------------------------------------------------


async def _pollution_audits():
    rows = await SignalIterRepository.query_audit_log(category="scanner_input_glitch")
    return [r for r in rows if (r.details or {}).get("kind") == "reference_pollution"]


async def test_reference_pollution_rejected_exact_1907_signature() -> None:
    """Regression pin on the exact 19:07 signature: VALID current spot 7546
    (in-band vs last accepted) against a polluted window reference 14,340 →
    dP −6,794. The iteration is rejected, the engine window is RESET, the
    audit row carries kind='reference_pollution' with both values."""
    from fef_sidecar.engine import FEFEngine

    engine = FEFEngine()
    engine._history.append(SimpleNamespace(spx_price=14340.0))  # cross-stream ref
    state = SimpleNamespace(_scanner_last_spot={"SPX": 7540.0})

    rejected = await _reject_scanner_input_glitch(
        state, "SPX", 7546.0, engine=engine,
    )
    assert rejected is True
    # Quarantine: the polluted window is GONE — engine re-warms cleanly.
    assert engine.history_size == 0

    rows = await _pollution_audits()
    assert len(rows) == 1
    d = rows[0].details
    assert d["symbol"] == "SPX"
    assert d["incoming_spot"] == pytest.approx(7546.0)
    assert d["polluted_reference"] == pytest.approx(14340.0)
    assert d["delta_price"] == pytest.approx(-6794.0)
    assert d["threshold_pts"] == pytest.approx(max(50.0, 0.03 * 7546.0), abs=0.01)
    assert d["window_quarantined"] is True
    assert "7546.00" in rows[0].summary and "14340.00" in rows[0].summary
    # The spot itself validated against the band — it becomes the new
    # baseline so subsequent sane ticks flow.
    assert state._scanner_last_spot["SPX"] == pytest.approx(7546.0)
    assert await _reject_scanner_input_glitch(
        state, "SPX", 7548.0, engine=engine,
    ) is False


async def test_reference_delta_threshold_boundaries() -> None:
    """|dPrice| must exceed max(50, 3% of spot) to reject: the absolute floor
    governs low-priced symbols (a 49-pt GLD-class delta passes, 51 rejects);
    the 3% term governs index-class (a 226-pt SPX delta passes, 250 rejects)."""
    from fef_sidecar.engine import FEFEngine

    # Absolute floor at spot=1000: max(50, 30) = 50.
    e1 = FEFEngine()
    e1._history.append(SimpleNamespace(spx_price=1049.0))       # Δ49 < 50
    s1 = SimpleNamespace(_scanner_last_spot={"X": 1000.0})
    assert await _reject_scanner_input_glitch(s1, "X", 1000.0, engine=e1) is False
    assert e1.history_size == 1                                  # untouched

    e2 = FEFEngine()
    e2._history.append(SimpleNamespace(spx_price=1051.0))       # Δ51 > 50
    s2 = SimpleNamespace(_scanner_last_spot={"X": 1000.0})
    assert await _reject_scanner_input_glitch(s2, "X", 1000.0, engine=e2) is True
    assert e2.history_size == 0

    # 3% term at spot=7546: max(50, 226.38) = 226.38.
    e3 = FEFEngine()
    e3._history.append(SimpleNamespace(spx_price=7546.0 + 226.0))  # Δ226 < 226.38
    s3 = SimpleNamespace(_scanner_last_spot={"SPX": 7546.0})
    assert await _reject_scanner_input_glitch(s3, "SPX", 7546.0, engine=e3) is False

    e4 = FEFEngine()
    e4._history.append(SimpleNamespace(spx_price=7546.0 + 250.0))  # Δ250 > 226.38
    s4 = SimpleNamespace(_scanner_last_spot={"SPX": 7546.0})
    assert await _reject_scanner_input_glitch(s4, "SPX", 7546.0, engine=e4) is True


async def test_band_rejection_also_quarantines_polluted_window() -> None:
    """Phase-1 (band) rejection must ALSO purge a window that already holds a
    cross-stream reference — otherwise the next accepted tick is judged
    against the foreign previous price (the residual's other entry path)."""
    from fef_sidecar.engine import FEFEngine

    engine = FEFEngine()
    engine._history.append(SimpleNamespace(spx_price=7526.0))   # SPX ref on GLD engine
    state = SimpleNamespace(_scanner_last_spot={"GLD": 380.0})

    # Incoming SPX-priced spot on the GLD engine: band-rejected AND the
    # polluted window is quarantined in the same pass.
    assert await _reject_scanner_input_glitch(
        state, "GLD", 7526.8, engine=engine,
    ) is True
    assert engine.history_size == 0
    rows = await SignalIterRepository.query_audit_log(category="scanner_input_glitch")
    band = [r for r in rows if (r.details or {}).get("kind") == "spot_band"]
    assert len(band) == 1
    assert band[0].details["window_quarantined"] is True
    # Baseline untouched by the rejection; a sane GLD tick then flows.
    assert state._scanner_last_spot["GLD"] == pytest.approx(380.0)
    assert await _reject_scanner_input_glitch(
        state, "GLD", 380.4, engine=engine,
    ) is False


async def test_band_rejection_leaves_clean_window_alone() -> None:
    """A band rejection with a HEALTHY window (references inside the accepted
    band) must not cost the engine its warmup."""
    from fef_sidecar.engine import FEFEngine

    engine = FEFEngine()
    engine._history.append(SimpleNamespace(spx_price=379.9))
    engine._history.append(SimpleNamespace(spx_price=380.3))
    state = SimpleNamespace(_scanner_last_spot={"GLD": 380.0})

    assert await _reject_scanner_input_glitch(
        state, "GLD", 7526.8, engine=engine,
    ) is True
    assert engine.history_size == 2                              # window kept
    rows = await SignalIterRepository.query_audit_log(category="scanner_input_glitch")
    assert rows[0].details["window_quarantined"] is False


class _FakeEngineWithWindow(_FakeEngine):
    def __init__(self, refs: list[float]) -> None:
        super().__init__()
        self._history = [SimpleNamespace(spx_price=r) for r in refs]
        self.resets = 0

    def reset(self) -> None:
        self._history.clear()
        self.resets += 1
