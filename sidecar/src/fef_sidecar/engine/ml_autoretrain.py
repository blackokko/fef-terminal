"""Auto-retrain scheduler — debounces ML retrains after ticket closes.

Multiple closes in rapid succession (e.g. a stop hitting on three legs of a
spread) should produce ONE retrain, not three. We coalesce by deferring the
actual training call until N seconds after the last call to
`schedule_retrain`. The grace window also lets the DB transaction commit
fully before we read the new row.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


_pending_task: asyncio.Task | None = None
_debounce_sec = float(os.environ.get("FEF_ML_RETRAIN_DEBOUNCE_SEC", "10"))


def schedule_retrain(state) -> None:
    """Fire-and-forget: schedule a debounced retrain.

    Cancels any in-flight pending retrain so the timer restarts from now.
    Cheap to call — most calls just bump the timer.
    """
    global _pending_task

    async def _run_after_debounce():
        try:
            await asyncio.sleep(_debounce_sec)
        except asyncio.CancelledError:
            return
        try:
            from fef_sidecar.engine.ml_patterns import train_and_persist
            result = await train_and_persist(state=state)
            logger.info(
                "Auto-retrain done: rows=%s cv_mae=%s buckets=%s",
                result.get("n_training_rows"),
                result.get("cv_mae"),
                result.get("bucket_row_counts"),
            )
        except Exception:
            logger.exception("Auto-retrain failed")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("schedule_retrain called outside event loop — skipping")
        return

    if _pending_task is not None and not _pending_task.done():
        _pending_task.cancel()
    _pending_task = loop.create_task(_run_after_debounce(), name="ml-auto-retrain")
