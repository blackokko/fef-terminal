"""FEF (Flow Effectiveness Filter) signal engine.

Public entrypoints:
    FEFEngine       - stateful engine consuming FEFInputs
    FEFInputs       - one iter's worth of derived inputs
    FEFVerdict      - engine output (direction, confidence, reasoning, targets)

The engine maintains a rolling 4-iter buffer (3 prior + current) so it can
compute delta-Price / delta-Drift / FE-ratio over the last 3 iters per the
FEF spec.

[REMOVED 2026-08-25] QuantData subscription CANCELLED — `snapshot_to_inputs`
(the live MarketSnapshot -> FEFInputs adapter) is gone with it. The engine is
now reachable only from replay/backtest callers that build FEFInputs from
persisted rows.
"""

from fef_sidecar.engine.fef import FEFEngine
from fef_sidecar.engine.types import FEFInputs, FEFVerdict

__all__ = [
    "FEFEngine",
    "FEFInputs",
    "FEFVerdict",
]
