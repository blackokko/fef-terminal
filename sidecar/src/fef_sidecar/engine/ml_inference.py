"""ML inference — load the most recent persisted estimator and score live signals.

Phase 2 (overlay): produces a `ml_expected_r` value per actionable iter that
is stamped into SignalIter.extras and broadcast in the IterEvent payload.
Verdict generation is NOT touched — this is observational only so the engine
freeze counter stays put.

Phase 3 (bucketing): the persisted payload can carry a dict of per-hour
bucket models {bucket_name: estimator}. The inference service picks the
bucket matching the iter's hour-of-day, falling back to a "global" key.

Reload contract: call `MLInferenceService.reload()` after retraining. The
service is lightweight to construct, so callers can also just instantiate
a fresh one each time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import MLModelRun
from sqlmodel import select

logger = logging.getLogger(__name__)


# Same feature schema as ml_patterns._extract_features — kept here so the
# inference path doesn't have to import the SignalIter ORM model.
def _features_from_signal_payload(payload: dict) -> dict[str, float] | None:
    """Extract the same 16 features used during training, from a live payload.

    Expected payload keys mirror SignalIter fields. Returns None for WAIT
    direction (we don't train/score on those).
    """
    if payload.get("direction") == "WAIT":
        return None
    extras = payload.get("extras") or {}
    spx = float(payload.get("spx_price") or 0)
    ntm_mp = float(payload.get("ntm_max_pain") or 0)

    conf_map = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}

    top_gex_walls = payload.get("top_gex_walls") or []
    top_dex_walls = payload.get("top_dex_walls") or []
    top_gex = top_gex_walls[0] if top_gex_walls else None
    top_dex = top_dex_walls[0] if top_dex_walls else None

    return {
        "drift_cum_net_m": float(payload.get("drift_cum_net_m") or 0),
        "mp_distance": spx - ntm_mp,
        "aggression_call_pct": float(payload.get("aggression_call_pct") or 50),
        "aggression_put_pct": float(payload.get("aggression_put_pct") or 50),
        "iv_rank_call_pct": float(payload.get("iv_rank_call_pct") or 50),
        "iv_rank_put_pct": float(payload.get("iv_rank_put_pct") or 50),
        "is_long_calls": 1 if payload.get("direction") == "LONG_CALLS" else 0,
        "is_long_puts": 1 if payload.get("direction") == "LONG_PUTS" else 0,
        "confidence_ord": conf_map.get(payload.get("confidence", "NONE"), 0),
        "hour_of_day": int(payload.get("hour_of_day") or 0),
        "top_gex_distance": abs(spx - float(top_gex["strike"])) if top_gex else 0.0,
        "top_dex_distance": abs(spx - float(top_dex["strike"])) if top_dex else 0.0,
        "fe_ratio": float(extras.get("fe_ratio") or 0),
        "delta_price": float(extras.get("delta_price") or 0),
        "delta_drift_m": float(extras.get("delta_drift_m") or 0),
    }


def _hour_to_bucket(hour_et: int) -> str:
    """Map an ET hour to a bucket name used at training time.

    open:  09:30-10:30 ET  → hour 9 or 10
    mid:   10:30-14:00 ET  → hour 11-13 (we approximate by hour-of-day; the
                              30-min boundary blends into mid since most
                              iters in hour 10 will be after 10:30)
    power: 14:00-16:00 ET  → hour 14-15

    Anything outside is "off_hours" — falls back to global.
    """
    if hour_et in (9, 10):
        return "open"
    if 11 <= hour_et <= 13:
        return "mid"
    if hour_et in (14, 15):
        return "power"
    return "off_hours"


@dataclass
class MLInferenceService:
    """Lazily-loaded RF estimator(s). Thread-safe to read after init."""

    model_path: str | None = None
    feature_names: list[str] | None = None
    # Single estimator (Phase 2) or dict of bucket→estimator (Phase 3).
    # Always normalized to a dict with at least a "global" key.
    estimators: dict[str, Any] | None = None
    label_kind: str = "r_multiple"

    @classmethod
    async def from_latest_run(cls) -> "MLInferenceService":
        try:
            import joblib
        except ImportError:
            logger.warning("joblib not installed — ML inference disabled")
            return cls()
        async with get_session() as session:
            row = (
                await session.exec(
                    select(MLModelRun)
                    .where(MLModelRun.model_path.isnot(None))  # type: ignore[attr-defined,union-attr]
                    .order_by(MLModelRun.trained_at.desc())  # type: ignore[attr-defined]
                    .limit(1)
                )
            ).first()
        if row is None or not row.model_path:
            return cls()
        try:
            payload = joblib.load(row.model_path)
        except Exception:
            logger.exception("Failed to load %s", row.model_path)
            return cls()
        # Normalize: persisted payload is either {estimator, feature_names} OR
        # {estimators: {bucket: est, ...}, feature_names}. Both end up with
        # estimators dict keyed by bucket name (always contains "global").
        if "estimators" in payload:
            estimators = dict(payload["estimators"])
        else:
            estimators = {"global": payload["estimator"]}
        return cls(
            model_path=row.model_path,
            feature_names=list(payload["feature_names"]),
            estimators=estimators,
            label_kind=getattr(row, "label_kind", "r_multiple"),
        )

    def is_ready(self) -> bool:
        return bool(self.estimators and self.feature_names)

    def score(self, signal_payload: dict, *, hour_et: int) -> float | None:
        """Return predicted R-multiple for this signal, or None if not ready."""
        if not self.is_ready():
            return None
        feats = _features_from_signal_payload({**signal_payload, "hour_of_day": hour_et})
        if feats is None:
            return None
        try:
            import numpy as np
        except ImportError:
            return None
        bucket = _hour_to_bucket(hour_et)
        est = (self.estimators or {}).get(bucket) or (self.estimators or {}).get("global")
        if est is None:
            return None
        try:
            row = np.array([[feats[k] for k in (self.feature_names or [])]], dtype=float)
            pred = est.predict(row)
            return float(pred[0])
        except Exception:
            logger.exception("ML inference failed")
            return None
