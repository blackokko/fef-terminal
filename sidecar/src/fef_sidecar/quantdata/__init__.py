"""Native async QuantData client + typed models.

Ports the QuantData MCP's REST endpoints into a typed async client suitable
for use inside the sidecar's signal loop.

Public entrypoints:
    QuantDataClient      - live client (httpx + ~/.quantdata-mcp/config.json)
    MockQuantDataClient  - offline provider with frozen 2026-05-15 data
    QuantDataError       - base exception
    compute_ntm_max_pain - NTM max pain calc (pure function)

All response models live in `fef_sidecar.quantdata.models`.
"""

from fef_sidecar.quantdata.client import QuantDataClient
from fef_sidecar.quantdata.errors import (
    QuantDataAuthError,
    QuantDataError,
    QuantDataNotConfiguredError,
    QuantDataRateLimitError,
)
from fef_sidecar.quantdata.max_pain import (
    NTMMaxPainResult,
    compute_ntm_max_pain,
)
from fef_sidecar.quantdata.mock import MockQuantDataClient
from fef_sidecar.quantdata.models import (
    AggressionStats,
    ContractAggression,
    DriftEntry,
    DriftSummary,
    ExposureRow,
    GreekType,
    IVRankSnapshot,
    MaxPainSnapshot,
    OIRow,
    OpenInterestSnapshot,
    StrikeExposureSnapshot,
)

__all__ = [
    "AggressionStats",
    "ContractAggression",
    "DriftEntry",
    "DriftSummary",
    "ExposureRow",
    "GreekType",
    "IVRankSnapshot",
    "MaxPainSnapshot",
    "MockQuantDataClient",
    "NTMMaxPainResult",
    "OIRow",
    "OpenInterestSnapshot",
    "QuantDataAuthError",
    "QuantDataClient",
    "QuantDataError",
    "QuantDataNotConfiguredError",
    "QuantDataRateLimitError",
    "StrikeExposureSnapshot",
    "compute_ntm_max_pain",
]
