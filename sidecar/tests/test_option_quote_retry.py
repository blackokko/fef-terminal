"""Tests for the option-chain retry helper used at EOD when IBKR chain
validation rejects strikes or returns empty quotes (Bug #6).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from fef_sidecar.ibkr.models import Contract, ContractKind, OptionQuote
from fef_sidecar.loop import _fetch_option_quote_with_retry


def _ok_quote() -> OptionQuote:
    return OptionQuote(
        timestamp=datetime.now(),
        contract=Contract(
            symbol="SPX",
            kind=ContractKind.OPTION,
            exchange="CBOE",
            expiry="20260522",
            strike=7505.0,
            right="C",
        ),
        last=5.0,
        bid=4.75,
        ask=5.25,
        delta=0.5,
    )


@pytest.mark.asyncio
async def test_retry_returns_immediately_on_success() -> None:
    """When fetch succeeds on first try, no retries are attempted."""
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return _ok_quote()

    quote, attempts = await _fetch_option_quote_with_retry(fetch, max_retries=3, backoff_ms=1)
    assert quote is not None
    assert attempts == []
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retry_recovers_after_two_failures() -> None:
    """Simulate IBKR chain validation rejecting first 2 calls, succeeding on 3rd."""
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError(f"chain not ready (attempt {calls['n']})")
        return _ok_quote()

    quote, attempts = await _fetch_option_quote_with_retry(fetch, max_retries=3, backoff_ms=1)
    assert quote is not None
    assert calls["n"] == 3
    assert len(attempts) == 2
    assert attempts[0][0] == "ValueError"
    assert attempts[1][0] == "ValueError"


@pytest.mark.asyncio
async def test_retry_exhausts_and_returns_none_with_attempts_log() -> None:
    """If every retry fails, return None and the full attempts log."""
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        raise RuntimeError(f"persistent failure {calls['n']}")

    quote, attempts = await _fetch_option_quote_with_retry(fetch, max_retries=3, backoff_ms=1)
    assert quote is None
    assert len(attempts) == 3
    assert calls["n"] == 3
    assert all(t[0] == "RuntimeError" for t in attempts)


@pytest.mark.asyncio
async def test_retry_treats_none_result_as_retryable() -> None:
    """fetch_fn returning None (instead of raising) is also retried."""
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        if calls["n"] < 2:
            return None
        return _ok_quote()

    quote, attempts = await _fetch_option_quote_with_retry(fetch, max_retries=3, backoff_ms=1)
    assert quote is not None
    assert calls["n"] == 2
    assert len(attempts) == 1
    assert attempts[0][0] == "None"


@pytest.mark.asyncio
async def test_retry_mixed_pydantic_validation_and_success() -> None:
    """Reproduce the actual Bug #6 failure pattern: ValidationError on first
    two calls (last=None), success on 3rd."""
    from pydantic import ValidationError

    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        if calls["n"] < 3:
            # Mimic what live.py raises when IBKR returns no last/bid/ask
            try:
                OptionQuote(
                    timestamp=datetime.now(),
                    contract=Contract(
                        symbol="SPX", kind=ContractKind.OPTION, exchange="CBOE",
                        expiry="20260522", strike=7505.0, right="C",
                    ),
                    last=None,  # type: ignore[arg-type]  # this is the bug pattern
                )
            except ValidationError:
                raise
        return _ok_quote()

    quote, attempts = await _fetch_option_quote_with_retry(fetch, max_retries=3, backoff_ms=1)
    assert quote is not None
    assert calls["n"] == 3
    assert len(attempts) == 2
    assert attempts[0][0] == "ValidationError"
