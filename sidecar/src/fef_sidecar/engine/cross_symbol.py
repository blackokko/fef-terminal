"""Cross-symbol correlation — when multiple FEF engines are active on
different symbols (SPX, QQQ, IWM), look for alignment.

If SPX FEF says LONG_CALLS and QQQ FEF also says LONG_CALLS in the same
~minute, that's stronger conviction than either alone. Conversely, if
SPX is LONG_CALLS but QQQ is LONG_PUTS, sector rotation may be at play
and either signal might be a head-fake.

Two outputs:
  - alignment_score: -1.0 (perfectly opposed) to +1.0 (perfectly aligned)
  - composite_direction: aggregated bias across all active symbols

This MVP works with the verdict history we already persist — no new
loops needed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlmodel import select

from fef_sidecar.db.engine import get_session
from fef_sidecar.db.models import SignalIter


@dataclass(frozen=True)
class CrossSymbolView:
    symbols_seen: list[str]
    alignment_score: float        # -1 to +1
    composite_direction: str      # LONG_CALLS / LONG_PUTS / MIXED / WAIT
    composite_confidence: str
    per_symbol: dict[str, dict]   # symbol -> {direction, confidence, age_seconds}


async def compute_cross_symbol_view(window_seconds: int = 120) -> CrossSymbolView:
    """Aggregate the most recent verdict across all symbols seen in the
    last `window_seconds`. Currently SignalIter doesn't have a `symbol`
    column (added later) so we treat all rows as the active symbol.
    The composite is still useful for the future multi-symbol case."""

    cutoff = datetime.now(UTC) - timedelta(seconds=window_seconds)
    async with get_session() as session:
        rows = list((await session.exec(
            select(SignalIter)
            .where(SignalIter.timestamp >= cutoff)
            .order_by(SignalIter.timestamp.desc())  # type: ignore[attr-defined]
            .limit(50)
        )).all())

    if not rows:
        return CrossSymbolView(
            symbols_seen=[], alignment_score=0.0,
            composite_direction="WAIT", composite_confidence="NONE",
            per_symbol={},
        )

    # Group by symbol (use "SPX" as default since older rows lack the field)
    per_symbol: dict[str, dict] = {}
    for r in rows:
        sym = getattr(r, "symbol", None) or "SPX"
        if sym in per_symbol:
            continue
        age = (datetime.now(UTC) - r.timestamp).total_seconds()
        per_symbol[sym] = {
            "direction": r.direction.value,
            "confidence": r.confidence.value,
            "age_seconds": round(age, 1),
            "spx_price": r.spx_price,
        }

    # Alignment math
    directions = [v["direction"] for v in per_symbol.values()]
    longs = sum(1 for d in directions if d == "LONG_CALLS")
    puts = sum(1 for d in directions if d == "LONG_PUTS")
    waits = sum(1 for d in directions if d == "WAIT")
    n = len(directions)

    if n == 0 or n == waits:
        alignment = 0.0
        composite = "WAIT"
    elif longs == n - waits:
        alignment = 1.0
        composite = "LONG_CALLS"
    elif puts == n - waits:
        alignment = -1.0
        composite = "LONG_PUTS"
    else:
        # Mixed — alignment = net bias / total non-wait
        active = longs + puts
        alignment = round((longs - puts) / active, 3) if active else 0
        composite = "MIXED"

    high_count = sum(1 for v in per_symbol.values() if v["confidence"] == "HIGH")
    composite_confidence = (
        "HIGH" if high_count >= max(1, n // 2)
        else "MEDIUM" if high_count > 0
        else "LOW"
    )

    return CrossSymbolView(
        symbols_seen=sorted(per_symbol.keys()),
        alignment_score=round(alignment, 3),
        composite_direction=composite,
        composite_confidence=composite_confidence,
        per_symbol=per_symbol,
    )
