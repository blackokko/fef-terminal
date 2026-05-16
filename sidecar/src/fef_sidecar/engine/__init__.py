"""FEF (Flow Effectiveness Filter) signal engine.

Public entrypoints:
    FEFEngine       - stateful engine consuming MarketSnapshots
    FEFInputs       - one iter's worth of derived inputs
    FEFVerdict      - engine output (direction, confidence, reasoning, targets)
    snapshot_to_inputs - adapter from QuantData MarketSnapshot

The engine maintains a rolling 4-iter buffer (3 prior + current) so it can
compute ΔPrice / ΔDrift / FE-ratio over the last 3 iters per the FEF spec.
"""

from fef_sidecar.engine.fef import FEFEngine, snapshot_to_inputs
from fef_sidecar.engine.types import FEFInputs, FEFVerdict

__all__ = [
    "FEFEngine",
    "FEFInputs",
    "FEFVerdict",
    "snapshot_to_inputs",
]
