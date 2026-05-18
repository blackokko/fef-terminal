"""Test fixtures — auto-reset per-process counters so tests stay isolated."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_iter_counter() -> None:
    """Reset the per-process iter counter before every test.

    Without this, tests that exercise the iter pipeline accumulate counts
    from earlier tests in the same pytest session and assertions about
    expected iter_num values (e.g. 'after 3 calls, iter_num == 3') fail."""
    from fef_sidecar.db.repository import SignalIterRepository
    SignalIterRepository._process_iter_counter = 0
    yield
