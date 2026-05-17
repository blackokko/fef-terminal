"""ML pattern discovery — train a random forest classifier on historical
SignalIter features to predict win/loss for actionable verdicts.

Uses scikit-learn if available; falls back to a stub if not installed.

Features extracted per row:
  - drift_cum_net_m
  - ntm_max_pain - spx_price (distance)
  - aggression_call_pct / aggression_put_pct
  - iv_rank_call_pct / iv_rank_put_pct
  - direction (one-hot)
  - confidence (ordinal)
  - hour of day
  - top GEX wall distance, top DEX wall distance
  - fe_ratio, delta_price, delta_drift_m

Label: TradeTicket.pnl_dollars > 0 for the linked signal_iter_id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import MLModelRun, SignalIter, TicketStatus, TradeTicket

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrainResult:
    n_training_rows: int
    n_features: int
    train_accuracy: float
    test_accuracy: float
    test_auc: float | None
    feature_importances: list[dict]


def _extract_features(sig: SignalIter) -> dict | None:
    if sig.direction.value == "WAIT":
        return None
    extras = sig.extras or {}
    hour = int(sig.timestamp.strftime("%H"))
    top_gex = sig.top_gex_walls[0] if sig.top_gex_walls else None
    top_dex = sig.top_dex_walls[0] if sig.top_dex_walls else None

    conf_map = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
    return {
        "drift_cum_net_m": float(sig.drift_cum_net_m),
        "mp_distance": float(sig.spx_price - sig.ntm_max_pain),
        "aggression_call_pct": float(sig.aggression_call_pct or 50),
        "aggression_put_pct": float(sig.aggression_put_pct or 50),
        "iv_rank_call_pct": float(sig.iv_rank_call_pct or 50),
        "iv_rank_put_pct": float(sig.iv_rank_put_pct or 50),
        "is_long_calls": 1 if sig.direction.value == "LONG_CALLS" else 0,
        "is_long_puts": 1 if sig.direction.value == "LONG_PUTS" else 0,
        "confidence_ord": conf_map.get(sig.confidence.value, 0),
        "hour_of_day": hour,
        "top_gex_distance": abs(sig.spx_price - top_gex["strike"]) if top_gex else 0.0,
        "top_dex_distance": abs(sig.spx_price - top_dex["strike"]) if top_dex else 0.0,
        "fe_ratio": float(extras.get("fe_ratio") or 0),
        "delta_price": float(extras.get("delta_price") or 0),
        "delta_drift_m": float(extras.get("delta_drift_m") or 0),
    }


async def train_model(*, test_size: float = 0.3) -> TrainResult | None:
    """Pull all closed-ticket signals, train RF, return importances."""
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import roc_auc_score
        from sklearn.model_selection import train_test_split
    except ImportError:
        logger.warning("scikit-learn not installed; ML pattern discovery unavailable")
        return None

    async with get_session() as session:
        tickets = list((await session.exec(
            select(TradeTicket).where(
                TradeTicket.status.in_([TicketStatus.CLOSED, TicketStatus.STOPPED, TicketStatus.PARTIAL])  # type: ignore[attr-defined]
            )
        )).all())
        if len(tickets) < 30:
            return TrainResult(n_training_rows=len(tickets), n_features=0,
                               train_accuracy=0, test_accuracy=0,
                               test_auc=None, feature_importances=[])
        sig_ids = {t.signal_iter_id for t in tickets}
        sigs = list((await session.exec(
            select(SignalIter).where(SignalIter.id.in_(sig_ids))  # type: ignore[attr-defined]
        )).all())
        sigs_by_id = {s.id: s for s in sigs if s.id is not None}

    X = []
    y = []
    feature_names: list[str] = []
    for t in tickets:
        sig = sigs_by_id.get(t.signal_iter_id)
        if sig is None:
            continue
        feats = _extract_features(sig)
        if feats is None:
            continue
        if not feature_names:
            feature_names = list(feats.keys())
        X.append([feats[k] for k in feature_names])
        y.append(1 if (t.pnl_dollars or 0) > 0 else 0)

    if len(X) < 30:
        return TrainResult(n_training_rows=len(X), n_features=len(feature_names),
                           train_accuracy=0, test_accuracy=0,
                           test_auc=None, feature_importances=[])

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=test_size, random_state=42)
    clf = RandomForestClassifier(n_estimators=100, max_depth=8, random_state=42)
    clf.fit(X_train, y_train)
    train_acc = float(clf.score(X_train, y_train))
    test_acc = float(clf.score(X_test, y_test))
    try:
        proba = clf.predict_proba(X_test)[:, 1]
        auc = float(roc_auc_score(y_test, proba))
    except Exception:
        auc = None

    importances = sorted(
        [{"name": n, "importance": float(i)}
         for n, i in zip(feature_names, clf.feature_importances_)],
        key=lambda d: -d["importance"],
    )

    return TrainResult(
        n_training_rows=len(X), n_features=len(feature_names),
        train_accuracy=round(train_acc, 3), test_accuracy=round(test_acc, 3),
        test_auc=round(auc, 3) if auc is not None else None,
        feature_importances=importances,
    )


async def train_and_persist() -> dict:
    result = await train_model()
    if result is None:
        return {"ok": False, "error": "scikit-learn not installed"}
    from fef_sidecar.db.repository import SignalIterRepository
    run = MLModelRun(
        algorithm="RandomForestClassifier(n=100, depth=8)",
        n_training_rows=result.n_training_rows,
        n_features=result.n_features,
        train_accuracy=result.train_accuracy,
        test_accuracy=result.test_accuracy,
        test_auc=result.test_auc,
        feature_importances=result.feature_importances,
    )
    await SignalIterRepository.write_ml_model_run(run)
    return {
        "ok": True,
        "n_training_rows": result.n_training_rows,
        "train_accuracy": result.train_accuracy,
        "test_accuracy": result.test_accuracy,
        "test_auc": result.test_auc,
        "feature_importances": result.feature_importances[:10],
    }
