"""ML pattern discovery — train a Random Forest *regressor* on historical
SignalIter features to predict the R-multiple of each closed trade.

Why regression on R-multiple, not binary win/loss?
  - A 0.1R scratch and a 3R winner both label as y=1 under win/loss, which
    throws away the magnitude signal we care about most.
  - Predicted expected-R per signal is directly usable as a position-size
    multiplier or as a soft gate threshold.

Why TimeSeriesSplit, not random train_test_split?
  - Random split leaks future trades into the training set. AUC/accuracy
    from a shuffled split is optimistic and not what you'd see in
    production. TimeSeriesSplit walks forward through time-ordered folds,
    matching how the model would actually be used.

Features extracted per row (same set as the legacy classifier):
  - drift_cum_net_m
  - mp_distance (spx - ntm_max_pain)
  - aggression_call_pct / aggression_put_pct
  - iv_rank_call_pct / iv_rank_put_pct
  - direction one-hot (is_long_calls, is_long_puts)
  - confidence ordinal
  - hour_of_day
  - top GEX wall distance, top DEX wall distance
  - fe_ratio, delta_price, delta_drift_m

Label: pnl_dollars / risk_dollars, where risk_dollars = qty * (entry_limit - stop) * 100.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import select

from fef_sidecar.db.engine import DEFAULT_DB_DIR, get_session
from fef_sidecar.db.models import MLModelRun, SignalIter, TicketStatus, TradeTicket

logger = logging.getLogger(__name__)


def _model_dir() -> Path:
    override = os.environ.get("FEF_MODEL_DIR")
    base = Path(override) if override else DEFAULT_DB_DIR / "models"
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass(frozen=True)
class TrainResult:
    n_training_rows: int
    n_features: int
    cv_mae: float | None
    cv_r2: float | None
    cv_n_folds: int
    feature_importances: list[dict]
    feature_names: list[str]
    model_path: str | None
    # Per-bucket row counts (empty if bucketing was skipped). Keys are
    # bucket names ("global", "open", "mid", "power") and values are the
    # number of rows the bucket model was trained on.
    bucket_row_counts: dict[str, int] | None = None


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


def _r_multiple(ticket: TradeTicket) -> float | None:
    """Convert ticket outcome into R-multiple = pnl / risk.

    risk_dollars = qty * (entry_limit - stop) * 100 (SPX multiplier).
    Returns None if entry/stop are degenerate.
    """
    if ticket.pnl_dollars is None:
        return None
    risk_per_contract = float(ticket.entry_limit) - float(ticket.stop)
    if risk_per_contract <= 0:
        return None
    risk_dollars = float(ticket.qty) * risk_per_contract * 100.0
    if risk_dollars <= 0:
        return None
    return float(ticket.pnl_dollars) / risk_dollars


async def _load_training_data() -> tuple[list[list[float]], list[float], list[str], list[datetime]]:
    """Pull closed tickets + linked signals, return (X, y, feature_names, timestamps).

    Rows are sorted by signal timestamp ascending — TimeSeriesSplit relies on
    that order being chronological.

    Logs row counts at each stage so dashboards / operators can pinpoint
    where training data is being dropped.
    """
    async with get_session() as session:
        tickets = list((await session.exec(
            select(TradeTicket).where(
                TradeTicket.status.in_(  # type: ignore[attr-defined]
                    [TicketStatus.CLOSED, TicketStatus.STOPPED, TicketStatus.PARTIAL]
                )
            )
        )).all())
        n_tickets = len(tickets)
        logger.info("ML training data: %d closed/stopped/partial trade_tickets pulled from DB", n_tickets)
        if not tickets:
            logger.info(
                "ML training data: trade_ticket table empty — no mock-trader rows. "
                "Falling back to signal_lookahead_outcome if available is not handled here; "
                "caller should chain to _load_training_data_from_lookahead."
            )
            return [], [], [], []
        sig_ids = {t.signal_iter_id for t in tickets if t.signal_iter_id is not None}
        sigs = list((await session.exec(
            select(SignalIter).where(SignalIter.id.in_(sig_ids))  # type: ignore[attr-defined]
        )).all())
        sigs_by_id = {s.id: s for s in sigs if s.id is not None}

    rows: list[tuple[datetime, list[float], float]] = []
    feature_names: list[str] = []
    skip_no_sig = 0
    skip_no_feats = 0
    skip_no_r = 0
    for t in tickets:
        sig = sigs_by_id.get(t.signal_iter_id) if t.signal_iter_id is not None else None
        if sig is None:
            skip_no_sig += 1
            continue
        feats = _extract_features(sig)
        if feats is None:
            skip_no_feats += 1
            continue
        r = _r_multiple(t)
        if r is None:
            skip_no_r += 1
            continue
        if not feature_names:
            feature_names = list(feats.keys())
        rows.append((sig.timestamp, [feats[k] for k in feature_names], r))

    logger.info(
        "ML training data (tickets): %d -> rows %d (skipped: no_sig=%d, no_feats=%d, no_r=%d)",
        n_tickets, len(rows), skip_no_sig, skip_no_feats, skip_no_r,
    )

    rows.sort(key=lambda row: row[0])
    X = [r[1] for r in rows]
    y = [r[2] for r in rows]
    timestamps = [r[0] for r in rows]
    return X, y, feature_names, timestamps


def _lookahead_r_multiple(
    *,
    hit_t1: bool,
    hit_t2: bool,
    hit_t3: bool,
    hit_stop: bool,
) -> float | None:
    """Convert lookahead boolean outcomes into a synthetic R-multiple.

    Convention matches how the auto-trader scales out:
      T3 hit -> +3R (full runner)
      T2 hit (no T3) -> +2R
      T1 hit (no T2) -> +1R
      stop hit and no T1 -> -1R
      neither -> 0R (timeout)

    Mirrors how shadow_exit_outcome's "realized_r" works when an exit
    sequence is reconstructed from price-action only.
    """
    if hit_t3:
        return 3.0
    if hit_t2:
        return 2.0
    if hit_t1:
        return 1.0
    if hit_stop:
        return -1.0
    return 0.0


async def _load_training_data_from_lookahead(
    *,
    lookahead_minutes: int = 15,
) -> tuple[list[list[float]], list[float], list[str], list[datetime]]:
    """Fallback data source: derive (X, y) from signal_lookahead_outcome.

    Used when the trade_ticket table is empty (live-trading-only setup with
    no mock paper trades). Each actionable signal at `lookahead_minutes`
    becomes one training row labeled with synthetic R derived from
    would_have_hit_t1/t2/t3/stop.

    Same feature extraction as the ticket path — switching label source
    does not change the model's expected feature interface.
    """
    from fef_sidecar.db.models import SignalLookaheadOutcome

    async with get_session() as session:
        outcomes = list((await session.exec(
            select(SignalLookaheadOutcome).where(
                SignalLookaheadOutcome.lookahead_minutes == lookahead_minutes  # type: ignore[arg-type]
            )
        )).all())
        n_outcomes = len(outcomes)
        logger.info(
            "ML training data (lookahead): %d signal_lookahead_outcome rows at %dmin window",
            n_outcomes, lookahead_minutes,
        )
        if not outcomes:
            return [], [], [], []

        sig_ids = {o.signal_iter_id for o in outcomes if o.signal_iter_id is not None}
        sigs = list((await session.exec(
            select(SignalIter).where(SignalIter.id.in_(sig_ids))  # type: ignore[attr-defined]
        )).all())
        sigs_by_id = {s.id: s for s in sigs if s.id is not None}

    rows: list[tuple[datetime, list[float], float]] = []
    feature_names: list[str] = []
    skip_wait = 0
    skip_no_sig = 0
    skip_no_feats = 0
    for outcome in outcomes:
        sig = sigs_by_id.get(outcome.signal_iter_id)
        if sig is None:
            skip_no_sig += 1
            continue
        # Skip WAIT verdicts — they have no actionable outcome to learn from
        if sig.direction.value == "WAIT":
            skip_wait += 1
            continue
        feats = _extract_features(sig)
        if feats is None:
            skip_no_feats += 1
            continue
        r = _lookahead_r_multiple(
            hit_t1=bool(outcome.would_have_hit_t1),
            hit_t2=bool(outcome.would_have_hit_t2),
            hit_t3=bool(outcome.would_have_hit_t3),
            hit_stop=bool(outcome.would_have_hit_stop),
        )
        if r is None:
            continue
        if not feature_names:
            feature_names = list(feats.keys())
        rows.append((sig.timestamp, [feats[k] for k in feature_names], r))

    logger.info(
        "ML training data (lookahead): %d outcomes -> %d rows (skipped: wait=%d, no_sig=%d, no_feats=%d)",
        n_outcomes, len(rows), skip_wait, skip_no_sig, skip_no_feats,
    )

    rows.sort(key=lambda row: row[0])
    X = [r[1] for r in rows]
    y = [r[2] for r in rows]
    timestamps = [r[0] for r in rows]
    return X, y, feature_names, timestamps


def _bucket_for_row(features: list[float], feature_names: list[str]) -> str:
    """Pick a bucket name based on the row's hour_of_day feature.

    Mirrors `ml_inference._hour_to_bucket` so training and inference stay
    in sync. Rows with hour outside the trading session land in
    "off_hours" — those train the global model only.
    """
    if "hour_of_day" not in feature_names:
        return "off_hours"
    idx = feature_names.index("hour_of_day")
    hour = int(features[idx])
    if hour in (9, 10):
        return "open"
    if 11 <= hour <= 13:
        return "mid"
    if hour in (14, 15):
        return "power"
    return "off_hours"


def _fit_and_score(X, y, *, n_splits: int):
    """Train a RandomForestRegressor with TimeSeriesSplit CV.

    Returns (final_estimator, cv_mae, cv_r2, n_folds_actually_run).
    """
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.metrics import mean_absolute_error, r2_score
    from sklearn.model_selection import TimeSeriesSplit
    import numpy as np

    effective_splits = max(2, min(n_splits, len(X) - 1))
    tscv = TimeSeriesSplit(n_splits=effective_splits)
    fold_mae: list[float] = []
    fold_r2: list[float] = []
    for train_idx, test_idx in tscv.split(X):
        if len(train_idx) < 5 or len(test_idx) < 1:
            continue
        clf = RandomForestRegressor(n_estimators=100, max_depth=8, random_state=42, n_jobs=1)
        clf.fit(X[train_idx], y[train_idx])
        pred = clf.predict(X[test_idx])
        fold_mae.append(float(mean_absolute_error(y[test_idx], pred)))
        if len(test_idx) >= 2:
            try:
                fold_r2.append(float(r2_score(y[test_idx], pred)))
            except Exception:
                pass
    final = RandomForestRegressor(n_estimators=100, max_depth=8, random_state=42, n_jobs=1)
    final.fit(X, y)
    cv_mae = float(np.mean(fold_mae)) if fold_mae else None
    cv_r2 = float(np.mean(fold_r2)) if fold_r2 else None
    return final, cv_mae, cv_r2, len(fold_mae)


# Minimum rows for a bucket to get its own model. Below this, the bucket
# falls back to the global model at inference time.
BUCKET_MIN_ROWS = 20


async def train_model(*, min_rows: int = 30, n_splits: int = 5) -> TrainResult | None:
    """Pull closed tickets, train per-hour bucket RF regressors + a global model.

    Walks training data, splits into buckets (open/mid/power/off_hours)
    based on the hour_of_day feature, trains a sub-model per bucket with
    ≥BUCKET_MIN_ROWS rows. Always trains a "global" model on all data.

    Persists `{estimators: {bucket: est, ...}, feature_names}` under
    FEF_MODEL_DIR. The inference service picks the matching bucket and
    falls back to "global" when the bucket model is absent.

    The top-level cv_mae / cv_r2 reported on the returned TrainResult are
    the GLOBAL model's CV metrics — the most comparable across runs.
    """
    try:
        import joblib
        import numpy as np
    except ImportError:
        logger.warning("scikit-learn/joblib not installed; ML pattern discovery unavailable")
        return None

    # Primary data source: closed/stopped TradeTickets (mock paper trades).
    # Live-trading-only setups never write TradeTicket rows — fall back to
    # signal_lookahead_outcome (synthetic R-multiples from price-action).
    X_list, y_list, feature_names, _ts = await _load_training_data()
    data_source = "trade_ticket"
    if len(X_list) == 0:
        logger.info(
            "ML training: trade_ticket source returned 0 rows — "
            "falling back to signal_lookahead_outcome (15min window)"
        )
        X_list, y_list, feature_names, _ts = await _load_training_data_from_lookahead(
            lookahead_minutes=15,
        )
        data_source = "signal_lookahead_outcome"

    logger.info(
        "ML training: source=%s, n_rows=%d, n_features=%d (min_rows=%d)",
        data_source, len(X_list), len(feature_names), min_rows,
    )

    if len(X_list) < min_rows:
        logger.info(
            "ML training: under min_rows (%d < %d) — skipping fit, returning empty result",
            len(X_list), min_rows,
        )
        return TrainResult(
            n_training_rows=len(X_list),
            n_features=len(feature_names),
            cv_mae=None, cv_r2=None, cv_n_folds=0,
            feature_importances=[], feature_names=feature_names,
            model_path=None,
            bucket_row_counts={},
        )

    # Global model trained on all rows
    X = np.array(X_list, dtype=float)
    y = np.array(y_list, dtype=float)
    global_est, cv_mae, cv_r2, n_folds = _fit_and_score(X, y, n_splits=n_splits)

    importances = sorted(
        [{"name": n, "importance": float(i)}
         for n, i in zip(feature_names, global_est.feature_importances_)],
        key=lambda d: -d["importance"],
    )

    # Per-bucket models — only trained if bucket has ≥BUCKET_MIN_ROWS rows.
    # Splitting preserves chronological order within each bucket because
    # _load_training_data returns rows sorted by timestamp.
    buckets: dict[str, list[int]] = {}  # bucket_name -> list of row indices
    for i, row in enumerate(X_list):
        buckets.setdefault(_bucket_for_row(row, feature_names), []).append(i)

    estimators: dict[str, object] = {"global": global_est}
    bucket_row_counts: dict[str, int] = {"global": len(X_list)}
    logger.info(
        "ML training: bucket pre-split counts: %s (BUCKET_MIN_ROWS=%d)",
        {b: len(idxs) for b, idxs in buckets.items()}, BUCKET_MIN_ROWS,
    )
    for bucket, idxs in buckets.items():
        if bucket == "off_hours" or len(idxs) < BUCKET_MIN_ROWS:
            bucket_row_counts[bucket] = len(idxs)
            continue
        Xb = X[idxs]
        yb = y[idxs]
        try:
            est, _, _, _ = _fit_and_score(Xb, yb, n_splits=n_splits)
            estimators[bucket] = est
            bucket_row_counts[bucket] = len(idxs)
        except Exception:
            logger.exception("Per-bucket training failed for %s", bucket)
            bucket_row_counts[bucket] = len(idxs)

    logger.info(
        "ML training: completed. data_source=%s n_rows=%d n_folds=%d cv_mae=%s buckets=%s",
        data_source, len(X_list), n_folds, cv_mae, bucket_row_counts,
    )

    model_dir = _model_dir()
    fname = f"rfr_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.joblib"
    model_path = model_dir / fname
    try:
        joblib.dump(
            {"estimators": estimators, "feature_names": feature_names},
            model_path,
        )
    except Exception:
        logger.exception("Failed to persist estimator")
        model_path = None  # type: ignore[assignment]

    return TrainResult(
        n_training_rows=len(X_list),
        n_features=len(feature_names),
        cv_mae=round(cv_mae, 4) if cv_mae is not None else None,
        cv_r2=round(cv_r2, 4) if cv_r2 is not None else None,
        cv_n_folds=n_folds,
        feature_importances=importances,
        feature_names=feature_names,
        model_path=str(model_path) if model_path else None,
        bucket_row_counts=bucket_row_counts,
    )


async def train_and_persist(*, state=None) -> dict:
    """Train + persist a new model run.

    If `state` is provided, also reloads the live inference service so the
    new estimator is picked up immediately without a sidecar restart.
    """
    result = await train_model()
    if result is None:
        return {"ok": False, "error": "scikit-learn/joblib not installed"}
    from fef_sidecar.db.repository import SignalIterRepository
    run = MLModelRun(
        algorithm="RandomForestRegressor(n=100, depth=8) — TimeSeriesSplit + buckets",
        n_training_rows=result.n_training_rows,
        n_features=result.n_features,
        # Legacy classifier fields kept at 0 for backwards-compat with the
        # existing NOT NULL constraint on those columns.
        train_accuracy=0.0,
        test_accuracy=0.0,
        test_auc=None,
        label_kind="r_multiple",
        cv_mae=result.cv_mae,
        cv_r2=result.cv_r2,
        cv_n_folds=result.cv_n_folds,
        model_path=result.model_path,
        feature_importances=result.feature_importances,
        notes=f"buckets={result.bucket_row_counts or {}}",
    )
    await SignalIterRepository.write_ml_model_run(run)

    # Hot-reload the inference service so live signals start being scored
    # against the new model on the next iter — no restart required.
    if state is not None and result.model_path is not None:
        try:
            from fef_sidecar.engine.ml_inference import MLInferenceService
            state.ml_inference = await MLInferenceService.from_latest_run()
        except Exception:
            logger.exception("ML inference hot-reload after train failed")

    return {
        "ok": True,
        "n_training_rows": result.n_training_rows,
        "n_features": result.n_features,
        "label_kind": "r_multiple",
        "cv_mae": result.cv_mae,
        "cv_r2": result.cv_r2,
        "cv_n_folds": result.cv_n_folds,
        "model_path": result.model_path,
        "bucket_row_counts": result.bucket_row_counts or {},
        "feature_importances": result.feature_importances[:10],
    }
