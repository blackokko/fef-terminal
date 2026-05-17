"""IBKR market data layer (via ib_async).

Public entrypoints:
    IBKRProvider          - Protocol any provider implements
    LiveIBKRProvider      - real ib_async-backed provider (requires TWS/Gateway)
    MockIBKRProvider      - scripted bar replay for offline dev + tests
    Bar, Quote, Contract  - typed market data models
"""

from fef_sidecar.ibkr.live import LiveIBKRProvider
from fef_sidecar.ibkr.mock import MockIBKRProvider
from fef_sidecar.ibkr.models import (
    AccountSummary,
    Bar,
    Contract,
    ContractKind,
    OptionQuote,
    Quote,
)
from fef_sidecar.ibkr.provider import IBKRProvider

__all__ = [
    "AccountSummary",
    "Bar",
    "Contract",
    "ContractKind",
    "IBKRProvider",
    "LiveIBKRProvider",
    "MockIBKRProvider",
    "OptionQuote",
    "Quote",
]
