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

    Actual QuantData shape (verified 2026-05-22):
        response.expirationDateToStrikePriceInCentsToContractExposureMap
            = { "<expDate>": { "<strikeCents>": {"CALL": <raw>, "PUT": <raw>} } }

    Same nested map as the exposure-by-strike endpoint — the upstream
    reuses the same key shape and the only difference is *what data* it
    contains. Strike values in cents, exposures in raw dollars (sum across
    all strikes per expiration). Output is normalised to millions.

    Fallback keys retained so a future schema change won't silently break.
    """
    resp = _resp(data, f"{greek.value} exposure by expiration")
    price = resp.get("stockPriceInCents", 0) / 100

    # Primary key — verified against live API. Same envelope as the
    # exposure-by-strike endpoint; we sum across all strikes per expiration
    # to get the bucket totals.
    nested_map = resp.get("expirationDateToStrikePriceInCentsToContractExposureMap")

    rows: list[ExpirationExposureRow] = []

    if isinstance(nested_map, dict) and nested_map:
        for exp_str, strike_map in nested_map.items():
            if not isinstance(strike_map, dict):
                continue
            call_sum = 0.0
            put_sum = 0.0
            for _strike_cents, exposure in strike_map.items():
                if not isinstance(exposure, dict):
                    continue
                call_sum += exposure.get("CALL", 0) or 0
                put_sum += exposure.get("PUT", 0) or 0
            # Raw exposures are dollars (not cents) for this endpoint —
            # same convention as parse_exposure().
            call_m = call_sum / 1_000_000
            put_m = put_sum / 1_000_000
            rows.append(ExpirationExposureRow(
                expiration=str(exp_str),
                call_m=call_m,
                put_m=put_m,
                net_m=call_m + put_m,
            ))
    else:
        # Defensive fallbacks for legacy / alternate shapes the upstream
        # might switch to. Only fire if the primary key is absent.
        exp_map: Any = (
            resp.get("expirationToExposureMap")
            or resp.get("expDateToExposureMap")
            or resp.get("byExpiration")
            or resp.get("expirationDateToContractExposureMap")
            or {}
        )
        if isinstance(exp_map, dict):
            for exp_str, exposure in exp_map.items():
                if not isinstance(exposure, dict):
                    continue
                call_raw = exposure.get("CALL") or exposure.get("call") or 0
                put_raw = exposure.get("PUT") or exposure.get("put") or 0
                divisor = 100_000_000 if abs(call_raw or put_raw) > 1e7 else 1_000_000
                call_m = (call_raw or 0) / divisor
                put_m = (put_raw or 0) / divisor
                rows.append(ExpirationExposureRow(
                    expiration=str(exp_str),
                    call_m=call_m,
                    put_m=put_m,
                    net_m=call_m + put_m,
                ))
        elif isinstance(exp_map, list):
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

    Actual QuantData shape (verified 2026-05-22):
        response.trades = [
            {
              "tradeTime": 1779459336947,
              "ticker": "SPX",
              "expirationDate": "2026-05-22",
              "strikePriceInCents": 752000,
              "contractType": "CALL",       # CALL or PUT
              "tradeSideCode": "M",          # AA / A / M / B / BB
              "premiumInCents": 4256000,
              "tradeType": "AUTO",           # AUTO / BLOCK
              "tradeConsolidationType": "SPLIT",  # SPLIT = swept across exchanges
              "isGoldenSweep": false,
              "isUnusual": false,
              "isVolumeGreaterThanOpenInterest": true,
              ...
            }, ...
        ]
        response.statistics.optionsOrderFlowContractTradeSideStatisticsSumMap = {...}

    QuantData doesn't expose an "isWhale" flag — we derive it as
    premium >= $1M. "Sweep" is derived from tradeConsolidationType == SPLIT
    (multi-exchange split fills). "Block" comes from tradeType == BLOCK.

    Fallback keys retained for the legacy guessed shape so a future API
    change doesn't silently produce empty results.
    """
    resp = _resp(data, "order flow")

    # Primary key — verified against live API.
    raw_orders: list[Any] = (
        resp.get("trades")
        or resp.get("orderFlow")
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
            # Timestamp — QuantData uses tradeTime (ms since epoch)
            ts_raw = (
                item.get("tradeTime")
                or item.get("timestamp")
                or item.get("timestampMs")
                or item.get("time")
                or 0
            )
            ts_ms = int(ts_raw) if ts_raw else 0

            # Strike — primary key is strikePriceInCents (always cents).
            # Fallbacks cover alternate shapes the API might switch to.
            strike_cents_raw = item.get("strikePriceInCents")
            if strike_cents_raw is not None:
                strike = float(strike_cents_raw) / 100
            else:
                # Fallback for legacy field names
                strike_raw = item.get("strikePrice") or item.get("strike") or 0
                # Heuristic: dollars vs cents
                strike = (
                    float(strike_raw) / 100
                    if float(strike_raw or 0) > 10000
                    else float(strike_raw or 0)
                )

            # Contract type — primary is contractType ("CALL"/"PUT")
            right_raw = str(
                item.get("contractType")
                or item.get("right")
                or item.get("optionType")
                or "C"
            ).upper()
            right = "C" if right_raw.startswith("C") else "P"

            expiry = str(
                item.get("expirationDate")
                or item.get("expiration")
                or item.get("expDate")
                or ""
            )

            # Premium — primary key is premiumInCents
            prem_cents_raw = item.get("premiumInCents")
            if prem_cents_raw is not None:
                premium = float(prem_cents_raw) / 100
            else:
                # Fallback for alternate shapes
                prem_raw = (
                    item.get("premium")
                    or item.get("premiumDollars")
                    or item.get("totalPremium")
                    or 0
                )
                premium = (
                    float(prem_raw or 0) / 100
                    if abs(float(prem_raw or 0)) > 100_000
                    else float(prem_raw or 0)
                )

            # Aggression side — primary key is tradeSideCode
            side_raw = str(
                item.get("tradeSideCode")
                or item.get("sideAggression")
                or item.get("aggression")
                or item.get("tradeSide")
                or "M"
            ).upper()
            if side_raw not in ("AA", "A", "M", "B", "BB"):
                side_raw = "M"

            # Classification flags — derived from QuantData fields.
            #   isGoldenSweep is a direct field.
            #   tradeConsolidationType == "SPLIT" indicates a sweep
            #     (split across multiple exchanges).
            #   tradeType == "BLOCK" indicates a block trade.
            #   Whale = premium >= $1M (QuantData doesn't expose this flag).
            flags_raw: list[Any] = list(item.get("flags") or item.get("otherFlags") or [])
            flags: list[str] = [str(f) for f in flags_raw]

            consolidation = str(item.get("tradeConsolidationType") or "").upper()
            trade_type = str(item.get("tradeType") or "").upper()

            is_golden = bool(
                item.get("isGoldenSweep")
                or item.get("goldenSweep")
                or "GOLDEN_SWEEP" in flags
            )
            is_sweep = bool(
                consolidation == "SPLIT"
                or item.get("isSweep")
                or item.get("sweep")
                or "SWEEP" in flags
                or is_golden  # a golden sweep is also a sweep
            )
            is_block = bool(
                trade_type == "BLOCK"
                or item.get("isBlock")
                or item.get("block")
                or "BLOCK" in flags
            )
            # Whale: $1M+ premium — also pick up explicit flag if upstream
            # ever adds one.
            is_whale = bool(
                premium >= 1_000_000
                or item.get("isWhale")
                or item.get("whale")
                or "WHALE" in flags
            )

            # Preserve any additional flags we didn't already account for
            # (e.g. Lightning / Turbo / Bargain / Momentum / Ditto from the
            # docs). Also surface isUnusual and isVolumeGreaterThanOpenInterest
            # as text tags for downstream research consumers.
            other: list[str] = [
                f for f in flags
                if f.upper() not in ("WHALE", "SWEEP", "GOLDEN_SWEEP", "BLOCK")
            ]
            if item.get("isUnusual"):
                other.append("UNUSUAL")
            if item.get("isVolumeGreaterThanOpenInterest"):
                other.append("VOL_GT_OI")
            if item.get("isOpeningPosition"):
                other.append("OPENING")

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

    Actual QuantData shape (verified 2026-05-22):
        response.optionPriceOverTime = [
            [ts_ms, open_cents, high_cents, low_cents, close_cents,
             volume, spx_open_cents, spx_high_cents, spx_low_cents, spx_close_cents],
            ...
        ]

    Each bar is a fixed-length 10-element array. We extract close price
    (index 4) as the canonical price, sum volume from index 5. The trailing
    SPX OHLC fields are ignored — they're already captured elsewhere.

    Empty response is normal:
      - Filter not yet set (strike/contractType/expirationDate must be
        configured on the tool's metadata.filter before fetching).
      - Contract has no trades yet for the session.

    Fallback shape keys retained for safety.
    """
    resp = _resp(data, "contract price/time")

    # Primary key — verified against live API.
    raw: list[Any] = (
        resp.get("optionPriceOverTime")
        or resp.get("priceTimeSeries")
        or resp.get("pricePath")
        or resp.get("priceData")
        or resp.get("contractPriceTime")
        or []
    )

    points: list[ContractPricePoint] = []
    for item in raw:
        try:
            if isinstance(item, (list, tuple)) and len(item) >= 5:
                # QuantData OHLCV bar: [ts_ms, o, h, l, c, vol, ...]
                # Use close as the canonical price (index 4).
                ts = int(item[0])
                close_cents = float(item[4])
                price = close_cents / 100  # cents → dollars
                vol = int(item[5]) if len(item) > 5 else 0
            elif isinstance(item, dict):
                # Fallback shape — defensive coverage for future API change.
                ts = int(
                    item.get("timestamp")
                    or item.get("timestampMs")
                    or item.get("time")
                    or item.get("tradeTime")
                    or 0
                )
                price_raw = float(
                    item.get("close")
                    or item.get("closePrice")
                    or item.get("price")
                    or item.get("lastPrice")
                    or 0
                )
                # Heuristic for cents vs dollars
                price = price_raw / 100 if price_raw > 1000 else price_raw
                vol = int(item.get("volume") or item.get("size") or 0)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                # Minimal fallback: [ts, price, vol?]
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
