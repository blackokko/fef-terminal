"""Parse raw QuantData REST JSON into typed models.

The upstream returns prices in cents and exposures in raw dollars; this
module is the one place that does unit conversion.
"""

from __future__ import annotations

from typing import Any

from fef_sidecar.quantdata.errors import QuantDataError
from fef_sidecar.quantdata.models import (
    AggressionStats,
    ContractAggression,
    ContractPricePoint,
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
    OrderFlowRow,
    OrderFlowSnapshot,
    StrikeExposureSnapshot,
)


def _resp(data: dict[str, Any], label: str) -> dict[str, Any]:
    if not data or "response" not in data:
        raise QuantDataError(f"Empty {label} response: {data!r}")
    return data["response"]


def parse_exposure(data: dict[str, Any], greek: GreekType) -> StrikeExposureSnapshot:
    resp = _resp(data, f"{greek.value} exposure")
    price = resp.get("stockPriceInCents", 0) / 100

    exp_map = resp.get("expirationDateToStrikePriceInCentsToContractExposureMap", {})
    rows: list[ExposureRow] = []
    for _exp, strike_map in exp_map.items():
        for strike_str, exposure in strike_map.items():
            strike = int(strike_str) / 100
            call_d = exposure.get("CALL", 0)
            put_d = exposure.get("PUT", 0)
            rows.append(
                ExposureRow(
                    strike=strike,
                    call_m=call_d / 1_000_000,
                    put_m=put_d / 1_000_000,
                    net_m=(call_d + put_d) / 1_000_000,
                )
            )
    rows.sort(key=lambda r: r.strike)
    return StrikeExposureSnapshot(greek=greek, spx_price=price, rows=rows)


def parse_net_drift(data: dict[str, Any]) -> DriftSummary:
    """Drift entries are [ts_ms, call_cents, ?, ?, put_cents, ?, ?, spx_cents]."""
    resp = _resp(data, "net drift")
    raw = resp.get("netDrift", [])

    entries: list[DriftEntry] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        ts = int(row[0])
        call_cents = row[1] or 0
        put_cents = row[4] or 0
        spx_cents = row[7] if len(row) > 7 else 0
        entries.append(
            DriftEntry(
                timestamp_ms=ts,
                call_dollars=call_cents / 100,
                put_dollars=put_cents / 100,
                spx_price=(spx_cents or 0) / 100,
            )
        )

    total_call = sum(e.call_dollars for e in entries)
    total_put = sum(e.put_dollars for e in entries)
    total_net = total_call - total_put

    # v2 fix (bug #2): the old absolute $1M threshold was arbitrary and
    # tripped near-always given typical SPX 0DTE flow magnitudes, producing
    # the structural ~88-100% BULLISH drift_bias bias. Switch to relative
    # dominance — need one side to exceed the other by >=15% of total flow
    # before declaring BULLISH/BEARISH. Self-scaling at any SPX notional.
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


def parse_max_pain(data: dict[str, Any]) -> MaxPainSnapshot:
    resp = _resp(data, "max pain")
    return MaxPainSnapshot(
        max_pain=resp.get("strikePriceInCentsWithMaxPain", 0) / 100,
        spx_price=resp.get("stockPriceInCents", 0) / 100,
    )


def parse_iv_rank(data: dict[str, Any], session_date: str) -> IVRankSnapshot:
    resp = _resp(data, "IV rank")
    session_map = resp.get("sessionDateToIVRankData", {})
    iv_data = session_map.get(session_date)
    if not iv_data and session_map:
        session_date = max(session_map.keys())
        iv_data = session_map.get(session_date)
    if not iv_data:
        return IVRankSnapshot(session_date=session_date)

    contract_data = iv_data.get("contractTypeToIVData", {})

    def _side(key: str) -> IVRankByContract | None:
        d = contract_data.get(key)
        if not d:
            return None
        return IVRankByContract(
            last_iv=d.get("lastIV", 0),
            window_min_iv=d.get("windowMinIV", 0),
            window_max_iv=d.get("windowMaxIV", 0),
        )

    return IVRankSnapshot(session_date=session_date, call=_side("CALL"), put=_side("PUT"))


def parse_trade_side_stats(data: dict[str, Any]) -> AggressionStats:
    resp = _resp(data, "trade side stats")
    m = resp.get("contractTypeOptionsContractTradeSideStatisticsSumMap", {})

    def _side(key: str) -> ContractAggression:
        d = m.get(key, {})
        return ContractAggression(
            side=key,  # type: ignore[arg-type]
            aa=d.get("AA", 0),
            a=d.get("A", 0),
            mid=d.get("M", 0),
            b=d.get("B", 0),
            bb=d.get("BB", 0),
        )

    return AggressionStats(call=_side("CALL"), put=_side("PUT"))


def parse_oi_by_strike(data: dict[str, Any]) -> OpenInterestSnapshot:
    resp = _resp(data, "OI by strike")
    price = resp.get("stockPriceInCents", 0) / 100
    oi_map = resp.get("strikePricesInCentsToPutCallOpenInterest", {})

    rows: list[OIRow] = []
    for strike_str, oi in oi_map.items():
        rows.append(
            OIRow(
                strike=int(strike_str) / 100,
                call_oi=int(oi.get("callOpenInterest", 0)),
                put_oi=int(oi.get("putOpenInterest", 0)),
            )
        )
    rows.sort(key=lambda r: r.strike)
    return OpenInterestSnapshot(spx_price=price, rows=rows)


def parse_contract_stats(data: dict[str, Any]) -> ContractStats:
    resp = _resp(data, "contract stats")
    premium = resp.get("contractTypeToTotalPremium", {})
    count = resp.get("contractTypeToTradeCount", {})
    vol = resp.get("contractTypeToVolume", {})

    def _side(key: str) -> ContractStatsBySide:
        return ContractStatsBySide(
            premium_dollars=(premium.get(key, 0) or 0) / 100,
            trade_count=int(count.get(key, 0) or 0),
            volume=int(vol.get(key, 0) or 0),
        )

    return ContractStats(call=_side("CALL"), put=_side("PUT"))


def parse_net_flow(data: dict[str, Any]) -> NetFlowSnapshot:
    resp = _resp(data, "net flow")
    raw = resp.get("netFlow", [])
    entries: list[NetFlowEntry] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        entries.append(
            NetFlowEntry(
                timestamp_ms=int(row[0]),
                call_dollars=(row[1] or 0) / 100,
                put_dollars=(row[4] or 0) / 100,
            )
        )
    return NetFlowSnapshot(entries=entries)


# ---------------------------------------------------------------------------
# New parsers — best-guess shapes; QuantData REST schema is not fully
# documented so each parser tries multiple key fallbacks defensively.
# ---------------------------------------------------------------------------


def parse_exposure_by_expiration(
    data: dict[str, Any], greek: GreekType
) -> ExpirationExposureSnapshot:
    """Parse expiration-bucketed exposure from options/exposure/expiration.

    Best-guess key names — QuantData may use any of:
      expirationToExposureMap | expDateToExposureMap | byExpiration |
      expirationDateToContractExposureMap
    Each value is expected to be a dict with CALL/PUT exposure keys (in
    raw dollars, cents, or millions — we try cents first, then dollars,
    normalise to millions at the end).

    If the top-level shape is a list of objects we try that too.
    """
    resp = _resp(data, f"{greek.value} exposure by expiration")
    price = resp.get("stockPriceInCents", 0) / 100

    # Try multiple map-key names the upstream might use
    exp_map: dict[str, Any] = (
        resp.get("expirationToExposureMap")
        or resp.get("expDateToExposureMap")
        or resp.get("byExpiration")
        or resp.get("expirationDateToContractExposureMap")
        or {}
    )

    rows: list[ExpirationExposureRow] = []

    if isinstance(exp_map, dict):
        for exp_str, exposure in exp_map.items():
            if not isinstance(exposure, dict):
                continue
            # Exposure values may be raw dollars or cents — heuristically
            # if the absolute values are very large (>1e6) they are likely
            # in cents; otherwise assume dollars and convert to millions.
            call_raw = exposure.get("CALL") or exposure.get("call") or 0
            put_raw = exposure.get("PUT") or exposure.get("put") or 0
            # If raw values look like cents (>1e7 for typical SPX exposure)
            # divide by 100 first to get dollars, then by 1e6 for millions.
            divisor = 100_000_000 if abs(call_raw or put_raw) > 1e7 else 1_000_000
            call_m = (call_raw or 0) / divisor
            put_m = (put_raw or 0) / divisor
            rows.append(ExpirationExposureRow(
                expiration=exp_str,
                call_m=call_m,
                put_m=put_m,
                net_m=call_m + put_m,
            ))
    elif isinstance(exp_map, list):
        # Alternate shape: list of {expiration, callExposure, putExposure, …}
        for item in exp_map:
            if not isinstance(item, dict):
                continue
            exp_str = (
                item.get("expiration")
                or item.get("expirationDate")
                or item.get("expDate")
                or ""
            )
            call_raw = item.get("callExposure") or item.get("CALL") or 0
            put_raw = item.get("putExposure") or item.get("PUT") or 0
            divisor = 100_000_000 if abs(call_raw or put_raw) > 1e7 else 1_000_000
            call_m = (call_raw or 0) / divisor
            put_m = (put_raw or 0) / divisor
            rows.append(ExpirationExposureRow(
                expiration=str(exp_str),
                call_m=call_m,
                put_m=put_m,
                net_m=call_m + put_m,
            ))

    rows.sort(key=lambda r: r.expiration)
    return ExpirationExposureSnapshot(greek=greek, spx_price=price, rows=rows)


def parse_order_flow(data: dict[str, Any]) -> OrderFlowSnapshot:
    """Parse consolidated order-flow from options/order-flow/consolidated.

    Best-guess key names — the upstream may return a list under any of:
      orderFlow | orders | consolidatedOrders | orderFlowList

    Each item is expected to have timestamp, strike, right (C/P), expiry,
    premium, aggression level, and classification flags (whale/sweep/etc.).
    All fields are parsed defensively; missing fields produce defaults.

    Note: QuantData's order-flow API is not fully documented. This parser
    covers the most likely shapes based on the established MCP pattern for
    other endpoints. Adjust key names if the real schema differs.
    """
    resp = _resp(data, "order flow")

    raw_orders: list[Any] = (
        resp.get("orderFlow")
        or resp.get("orders")
        or resp.get("consolidatedOrders")
        or resp.get("orderFlowList")
        or []
    )

    parsed: list[OrderFlowRow] = []
    for item in raw_orders:
        if not isinstance(item, dict):
            continue
        try:
            ts_raw = item.get("timestamp") or item.get("timestampMs") or item.get("time") or 0
            ts_ms = int(ts_raw) if ts_raw else 0
            # Strike may be in cents
            strike_raw = item.get("strikePrice") or item.get("strike") or 0
            strike = float(strike_raw) / 100 if float(strike_raw or 0) > 10000 else float(strike_raw or 0)
            right = str(item.get("contractType") or item.get("right") or item.get("optionType") or "C").upper()
            right = "C" if right.startswith("C") else "P"
            expiry = str(
                item.get("expirationDate")
                or item.get("expiration")
                or item.get("expDate")
                or ""
            )
            # Premium may be in cents
            prem_raw = item.get("premium") or item.get("premiumDollars") or item.get("totalPremium") or 0
            premium = float(prem_raw or 0) / 100 if abs(float(prem_raw or 0)) > 100_000 else float(prem_raw or 0)

            side_raw = str(
                item.get("sideAggression")
                or item.get("aggression")
                or item.get("tradeSide")
                or "M"
            ).upper()
            # Normalise to AA/A/M/B/BB
            if side_raw not in ("AA", "A", "M", "B", "BB"):
                side_raw = "M"

            flags: list[str] = list(item.get("flags") or item.get("otherFlags") or [])
            # Some endpoints embed flags as boolean keys
            is_whale = bool(item.get("isWhale") or item.get("whale") or "WHALE" in flags)
            is_sweep = bool(item.get("isSweep") or item.get("sweep") or "SWEEP" in flags)
            is_golden = bool(
                item.get("isGoldenSweep")
                or item.get("goldenSweep")
                or "GOLDEN_SWEEP" in flags
                or (is_sweep and premium >= 1_000_000)
            )
            is_block = bool(item.get("isBlock") or item.get("block") or "BLOCK" in flags)
            other = [f for f in flags if f not in ("WHALE", "SWEEP", "GOLDEN_SWEEP", "BLOCK")]

            parsed.append(OrderFlowRow(
                timestamp_ms=ts_ms,
                strike=strike,
                right=right,
                expiration=expiry,
                premium_dollars=premium,
                side_aggression=side_raw,
                is_whale=is_whale,
                is_sweep=is_sweep,
                is_golden_sweep=is_golden,
                is_block=is_block,
                other_flags=other,
            ))
        except Exception:
            continue

    # Sort most-recent first
    parsed.sort(key=lambda o: o.timestamp_ms, reverse=True)
    return OrderFlowSnapshot(orders=parsed)


def parse_contract_price_time(
    data: dict[str, Any],
    strike: float,
    right: str,
    expiration: str,
) -> ContractPriceTimeSnapshot:
    """Parse per-contract intraday price path from options/contract/price/time.

    Best-guess shape — QuantData may return a list under:
      priceTimeSeries | pricePath | priceData | contractPriceTime

    Each element is {timestamp, price, volume} or a fixed-length array.
    Prices are normalised from cents (if >1000) to dollars.
    """
    resp = _resp(data, "contract price/time")

    raw: list[Any] = (
        resp.get("priceTimeSeries")
        or resp.get("pricePath")
        or resp.get("priceData")
        or resp.get("contractPriceTime")
        or []
    )

    points: list[ContractPricePoint] = []
    for item in raw:
        try:
            if isinstance(item, dict):
                ts = int(item.get("timestamp") or item.get("timestampMs") or item.get("time") or 0)
                price_raw = float(item.get("price") or item.get("lastPrice") or 0)
                # Prices >1000 are likely in cents
                price = price_raw / 100 if price_raw > 1000 else price_raw
                vol = int(item.get("volume") or item.get("size") or 0)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                ts = int(item[0])
                price_raw = float(item[1])
                price = price_raw / 100 if price_raw > 1000 else price_raw
                vol = int(item[2]) if len(item) > 2 else 0
            else:
                continue
            points.append(ContractPricePoint(timestamp_ms=ts, price=price, volume=vol))
        except Exception:
            continue

    points.sort(key=lambda p: p.timestamp_ms)
    return ContractPriceTimeSnapshot(
        strike=strike, right=right, expiration=expiration, points=points
    )
