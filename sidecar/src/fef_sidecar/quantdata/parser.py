"""Parse QuantData *official API* (api.quantdata.us/v1) JSON into typed models.

Migration note (2026-05-29): rewritten for the official public API. The old
reverse-engineered dashboard backend (core-lb-prod) returned prices in CENTS
and wrapped payloads in a ``response`` envelope. The official API returns:
    - prices/premiums in DOLLARS (float) — no /100 conversion
    - exposures in raw dollars per representationMode — /1e6 for our "millions"
    - payloads under a top-level ``data`` key
Each endpoint's shape was captured live; see scripts/maintenance/probe_official_api.py.

Units convention (unchanged, see models.py):
    - exposure call_m/put_m/net_m in millions of dollars (signed)
    - premiums/flows in raw dollars (signed)
    - OI in raw contracts
"""

from __future__ import annotations

from typing import Any

from fef_sidecar.quantdata.errors import QuantDataError
from fef_sidecar.quantdata.models import (
    AggressionStats,
    ContractAggression,
    ContractPriceTimeSnapshot,
    ContractStats,
    ContractStatsBySide,
    DriftEntry,
    DriftSummary,
    ExpirationExposureRow,
    ExpirationExposureSnapshot,
    ExposureRow,
    GreekType,
    IVRankByContract,
    IVRankSnapshot,
    MaxPainSnapshot,
    NetFlowEntry,
    NetFlowSnapshot,
    OIRow,
    OpenInterestSnapshot,
    OrderFlowSnapshot,
    StrikeExposureSnapshot,
)


def _data(raw: dict[str, Any], label: str) -> Any:
    """Unwrap the official API's top-level ``data`` envelope."""
    if not isinstance(raw, dict) or "data" not in raw:
        raise QuantDataError(f"Empty/unexpected {label} response: {str(raw)[:200]!r}")
    return raw["data"]


def _f(x: Any) -> float:
    try:
        return float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _ticker_block(d: Any) -> dict[str, Any]:
    """exposure endpoints nest under data.<TICKER>; pick SPX or the first."""
    if not isinstance(d, dict) or not d:
        return {}
    return d.get("SPX") or next(iter(d.values()))


def parse_exposure(raw: dict[str, Any], greek: GreekType) -> StrikeExposureSnapshot:
    """Per-strike exposure (GEX/DEX/CEX/VEX), SUMMED across all expirations.

    data.<TICKER>.exposureMap.<expiration>.<strike>.{callExposure,putExposure}
    -> one ExposureRow per strike (total dealer exposure across expiries).
    """
    block = _ticker_block(_data(raw, f"{greek.value} exposure"))
    spx = _f(block.get("stockPrice"))
    emap = block.get("exposureMap", {}) or {}
    by_strike: dict[float, list[float]] = {}
    for _exp, strike_map in emap.items():
        if not isinstance(strike_map, dict):
            continue
        for strike_str, ex in strike_map.items():
            try:
                k = float(strike_str)
            except (TypeError, ValueError):
                continue
            acc = by_strike.setdefault(k, [0.0, 0.0])
            acc[0] += _f(ex.get("callExposure"))
            acc[1] += _f(ex.get("putExposure"))
    rows = [
        ExposureRow(strike=k, call_m=c / 1_000_000, put_m=p / 1_000_000, net_m=(c + p) / 1_000_000)
        for k, (c, p) in by_strike.items()
    ]
    rows.sort(key=lambda r: r.strike)
    return StrikeExposureSnapshot(greek=greek, spx_price=spx, rows=rows)


def parse_exposure_by_expiration(
    raw: dict[str, Any], greek: GreekType
) -> ExpirationExposureSnapshot:
    """Expiration-bucketed exposure, SUMMED across all strikes per expiry."""
    block = _ticker_block(_data(raw, f"{greek.value} exposure-by-expiration"))
    spx = _f(block.get("stockPrice"))
    emap = block.get("exposureMap", {}) or {}
    rows: list[ExpirationExposureRow] = []
    for exp, strike_map in emap.items():
        if not isinstance(strike_map, dict):
            continue
        c = p = 0.0
        for ex in strike_map.values():
            c += _f(ex.get("callExposure"))
            p += _f(ex.get("putExposure"))
        rows.append(
            ExpirationExposureRow(
                expiration=str(exp),
                call_m=c / 1_000_000,
                put_m=p / 1_000_000,
                net_m=(c + p) / 1_000_000,
            )
        )
    rows.sort(key=lambda r: r.expiration)
    return ExpirationExposureSnapshot(greek=greek, spx_price=spx, rows=rows)


def parse_net_drift(raw: dict[str, Any]) -> DriftSummary:
    """data.<ts_ms>.{netCallPremium,netPutPremium,stockPrice,...} (dollars)."""
    d = _data(raw, "net drift")
    entries: list[DriftEntry] = []
    if isinstance(d, dict):
        for ts, v in d.items():
            if not isinstance(v, dict):
                continue
            entries.append(
                DriftEntry(
                    timestamp_ms=int(ts),
                    call_dollars=_f(v.get("netCallPremium")),
                    put_dollars=_f(v.get("netPutPremium")),
                    spx_price=_f(v.get("stockPrice")),
                )
            )
    entries.sort(key=lambda e: e.timestamp_ms)

    total_call = sum(e.call_dollars for e in entries)
    total_put = sum(e.put_dollars for e in entries)
    total_net = total_call - total_put

    # Relative-dominance bias (unchanged from v2 fix): one side must exceed the
    # other by >=15% of total flow before declaring BULLISH/BEARISH.
    DOMINANCE_RATIO = 0.15
    total_abs = abs(total_call) + abs(total_put)
    if total_abs <= 0:
        bias = "NEUTRAL"
    elif total_net > DOMINANCE_RATIO * total_abs:
        bias = "BULLISH"
    elif total_net < -DOMINANCE_RATIO * total_abs:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return DriftSummary(
        entries=entries,
        cumulative_call_m=total_call / 1_000_000,
        cumulative_put_m=total_put / 1_000_000,
        cumulative_net_m=total_net / 1_000_000,
        bias=bias,
    )


def parse_net_flow(raw: dict[str, Any]) -> NetFlowSnapshot:
    """data.<ts_ms>.{callSum,putSum,stockPrice} (dollars, NET_PREMIUM mode)."""
    d = _data(raw, "net flow")
    entries: list[NetFlowEntry] = []
    if isinstance(d, dict):
        for ts, v in d.items():
            if not isinstance(v, dict):
                continue
            entries.append(
                NetFlowEntry(
                    timestamp_ms=int(ts),
                    call_dollars=_f(v.get("callSum")),
                    put_dollars=_f(v.get("putSum")),
                )
            )
    entries.sort(key=lambda e: e.timestamp_ms)
    return NetFlowSnapshot(entries=entries)


def parse_max_pain(raw: dict[str, Any], spx_price: float = 0.0) -> MaxPainSnapshot:
    """data.<strike>.{callIntrinsicValue,putIntrinsicValue}; max pain = argmin total.

    The official max-pain endpoint has no stockPrice field — callers pass
    spx_price (the orchestrator fills it from the exposure snapshot).
    """
    d = _data(raw, "max pain")
    best_total: float | None = None
    best_strike = 0.0
    if isinstance(d, dict):
        for strike_str, v in d.items():
            if not isinstance(v, dict):
                continue
            try:
                k = float(strike_str)
            except (TypeError, ValueError):
                continue
            total = _f(v.get("callIntrinsicValue")) + _f(v.get("putIntrinsicValue"))
            if best_total is None or total < best_total:
                best_total = total
                best_strike = k
    return MaxPainSnapshot(max_pain=best_strike, spx_price=spx_price)


def parse_iv_rank(raw: dict[str, Any], session_date: str) -> IVRankSnapshot:
    """data.<date>.contractTypeToIVData.{CALL,PUT}.{lastIv,windowMinIv,windowMaxIv}.

    The endpoint returns a date-keyed series; we take the requested
    session_date, else the most recent date present.
    """
    d = _data(raw, "IV rank")
    if not isinstance(d, dict) or not d:
        return IVRankSnapshot(session_date=session_date)
    key = session_date if session_date in d else max(d.keys())
    iv_data = d.get(key) or {}
    contract_data = iv_data.get("contractTypeToIVData", {}) or {}

    def _side(side_key: str) -> IVRankByContract | None:
        x = contract_data.get(side_key)
        if not x:
            return None
        return IVRankByContract(
            last_iv=_f(x.get("lastIv")),
            window_min_iv=_f(x.get("windowMinIv")),
            window_max_iv=_f(x.get("windowMaxIv")),
        )

    return IVRankSnapshot(session_date=key, call=_side("CALL"), put=_side("PUT"))


def parse_trade_side_stats(raw: dict[str, Any]) -> AggressionStats:
    """data.{CALL,PUT}.{ABOVE_ASK,ASK,MID_MARKET,BID,BELOW_BID}.premium (dollars).

    Maps to the AA/A/M/B/BB aggression buckets:
    ABOVE_ASK->aa (most aggressive buy) ... BELOW_BID->bb (most aggressive sell).
    """
    d = _data(raw, "trade side stats")

    def _side(side_key: str) -> ContractAggression:
        x = d.get(side_key, {}) if isinstance(d, dict) else {}

        def g(bucket: str) -> float:
            b = x.get(bucket) if isinstance(x, dict) else None
            return _f(b.get("premium")) if isinstance(b, dict) else 0.0

        return ContractAggression(
            side=side_key,  # type: ignore[arg-type]
            aa=g("ABOVE_ASK"),
            a=g("ASK"),
            mid=g("MID_MARKET"),
            b=g("BID"),
            bb=g("BELOW_BID"),
        )

    return AggressionStats(call=_side("CALL"), put=_side("PUT"))


def parse_oi_by_strike(raw: dict[str, Any], spx_price: float = 0.0) -> OpenInterestSnapshot:
    """data.<strike>.{callOpenInterest,putOpenInterest}. No stockPrice field."""
    d = _data(raw, "OI by strike")
    rows: list[OIRow] = []
    if isinstance(d, dict):
        for strike_str, v in d.items():
            if not isinstance(v, dict):
                continue
            try:
                k = float(strike_str)
            except (TypeError, ValueError):
                continue
            rows.append(
                OIRow(
                    strike=k,
                    call_oi=int(_f(v.get("callOpenInterest"))),
                    put_oi=int(_f(v.get("putOpenInterest"))),
                )
            )
    rows.sort(key=lambda r: r.strike)
    return OpenInterestSnapshot(spx_price=spx_price, rows=rows)


def parse_contract_stats(raw: dict[str, Any]) -> ContractStats:
    """data.{CALL,PUT}.{premium,tradeCount,volume} (premium in dollars)."""
    d = _data(raw, "contract stats")

    def _side(side_key: str) -> ContractStatsBySide:
        x = d.get(side_key, {}) if isinstance(d, dict) else {}
        return ContractStatsBySide(
            premium_dollars=_f(x.get("premium")),
            trade_count=int(_f(x.get("tradeCount"))),
            volume=int(_f(x.get("volume"))),
        )

    return ContractStats(call=_side("CALL"), put=_side("PUT"))


def parse_order_flow(raw: dict[str, Any]) -> OrderFlowSnapshot:
    """Consolidated order flow.

    NOTE (2026-05-29): the official public API does not currently expose an
    order-flow tool (all candidate endpoint names 404). Order flow is
    research-only (whale/golden-sweep summary counts); we return an empty
    snapshot so the rest of the pipeline keeps working. Revisit if QuantData
    adds an order-flow endpoint to the public API.
    """
    return OrderFlowSnapshot(orders=[])


def parse_contract_price_time(
    raw: dict[str, Any],
    *,
    strike: float,
    right: str,
    expiration: str,
) -> ContractPriceTimeSnapshot:
    """Intraday price path for one contract (option-price-over-time).

    On-demand / research-only — not part of the poll loop. Not yet remapped to
    the official API response shape; returns an empty path for now.
    """
    return ContractPriceTimeSnapshot(strike=strike, right=right, expiration=expiration, points=[])
