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
    ContractStats,
    ContractStatsBySide,
    DarkPoolLevel,
    DarkPoolSnapshot,
    DriftEntry,
    DriftSummary,
    ExposureRow,
    GreekType,
    IVRankByContract,
    IVRankSnapshot,
    IVSkewRow,
    IVSkewSnapshot,
    MaxPainSnapshot,
    NetFlowEntry,
    NetFlowSnapshot,
    OIRow,
    OpenInterestSnapshot,
    QuantAlert,
    QuantAlertsSnapshot,
    StrikeExposureSnapshot,
    VolDriftEntry,
    VolDriftSummary,
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
# New research-only parsers (batch 2, 2026-05-21)
# Shapes are best-guess based on QuantData API conventions. All use
# defensive .get() throughout — unknown response shapes degrade to empty
# collections rather than raising.
# ---------------------------------------------------------------------------


def parse_vol_drift(data: dict[str, Any]) -> VolDriftSummary:
    """Parse volatility drift response.

    Best-guess shape mirrors net_drift: a list of time-series rows plus
    cumulative summary fields. QuantData may return IVs as raw decimals
    (0.15 = 15%) or as integers (15 = 15%); we normalise to fractional.
    """
    resp = data.get("response", {}) if data else {}
    raw = resp.get("volDrift", resp.get("ivDrift", []))

    entries: list[VolDriftEntry] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        try:
            ts = int(row[0])
            call_iv = float(row[1] or 0)
            put_iv = float(row[2] or 0)
            spx_cents = float(row[3]) if len(row) > 3 else 0
            # Normalise: if values look like percentages (>1) convert to fraction
            if call_iv > 2:
                call_iv /= 100
            if put_iv > 2:
                put_iv /= 100
            entries.append(VolDriftEntry(
                timestamp_ms=ts,
                call_iv=call_iv,
                put_iv=put_iv,
                spx_price=spx_cents / 100 if spx_cents > 500 else spx_cents,
            ))
        except (TypeError, ValueError, IndexError):
            continue

    # Compute cumulative change (last - first)
    cum_call = 0.0
    cum_put = 0.0
    if len(entries) >= 2:
        cum_call = entries[-1].call_iv - entries[0].call_iv
        cum_put = entries[-1].put_iv - entries[0].put_iv

    # Bias: expanding if IV generally rising, contracting if falling
    avg_change = (cum_call + cum_put) / 2
    if avg_change > 0.005:
        bias = "EXPANDING"
    elif avg_change < -0.005:
        bias = "CONTRACTING"
    else:
        bias = "NEUTRAL"

    # Fall back to API-provided fields if present
    bias_raw = resp.get("bias") or resp.get("ivBias")
    if bias_raw and str(bias_raw).upper() in ("EXPANDING", "CONTRACTING", "NEUTRAL"):
        bias = str(bias_raw).upper()  # type: ignore[assignment]

    return VolDriftSummary(
        entries=entries,
        cumulative_iv_change_call=cum_call,
        cumulative_iv_change_put=cum_put,
        bias=bias,  # type: ignore[arg-type]
    )


def parse_vol_skew(data: dict[str, Any]) -> IVSkewSnapshot:
    """Parse volatility skew response.

    Best-guess shape: a list of per-strike rows with IV values.
    Expects response.rows or response.strikes or similar.
    """
    resp = data.get("response", {}) if data else {}
    spx_price = (resp.get("stockPriceInCents", 0) or 0) / 100

    # Try multiple possible key names for the row list
    raw_rows = (
        resp.get("strikeToIVMap")
        or resp.get("rows")
        or resp.get("strikes")
        or []
    )

    rows: list[IVSkewRow] = []

    if isinstance(raw_rows, dict):
        # Map form: {strike_cents_str: {CALL: iv, PUT: iv}}
        for strike_str, ivs in raw_rows.items():
            try:
                strike = int(strike_str) / 100
                call_iv = float((ivs.get("CALL") or ivs.get("call") or 0))
                put_iv = float((ivs.get("PUT") or ivs.get("put") or 0))
                if call_iv > 2:
                    call_iv /= 100
                if put_iv > 2:
                    put_iv /= 100
                rows.append(IVSkewRow.from_ivs(strike=strike, call_iv=call_iv, put_iv=put_iv))
            except (TypeError, ValueError):
                continue
    elif isinstance(raw_rows, list):
        # List form: [{strike, call_iv, put_iv}, ...]
        for item in raw_rows:
            if not isinstance(item, dict):
                continue
            try:
                strike_raw = item.get("strikePriceInCents") or item.get("strike", 0)
                strike = int(strike_raw) / 100 if int(strike_raw) > 500 else float(strike_raw)
                call_iv = float(item.get("callIV") or item.get("call_iv") or 0)
                put_iv = float(item.get("putIV") or item.get("put_iv") or 0)
                if call_iv > 2:
                    call_iv /= 100
                if put_iv > 2:
                    put_iv /= 100
                rows.append(IVSkewRow.from_ivs(strike=strike, call_iv=call_iv, put_iv=put_iv))
            except (TypeError, ValueError):
                continue

    rows.sort(key=lambda r: r.strike)
    return IVSkewSnapshot(spx_price=spx_price, rows=rows)


def parse_dark_pool(data: dict[str, Any]) -> DarkPoolSnapshot:
    """Parse dark pool levels response for SPY.

    Best-guess shape: a list of price levels with notional and count.
    """
    resp = data.get("response", {}) if data else {}

    raw_levels = (
        resp.get("levels")
        or resp.get("darkPoolLevels")
        or resp.get("priceToNotionalMap")
        or []
    )

    dp_levels: list[DarkPoolLevel] = []

    if isinstance(raw_levels, dict):
        # Map form: {price_cents_str: {notional, count}}
        for price_str, info in raw_levels.items():
            try:
                price_raw = float(price_str)
                price = price_raw / 100 if price_raw > 500 else price_raw
                notional = float(info.get("notionalDollars") or info.get("notional") or 0)
                count = int(info.get("tradeCount") or info.get("count") or 0)
                dp_levels.append(DarkPoolLevel(price=price, notional_dollars=notional, trade_count=count))
            except (TypeError, ValueError):
                continue
    elif isinstance(raw_levels, list):
        for item in raw_levels:
            if not isinstance(item, dict):
                continue
            try:
                price_raw = item.get("priceCents") or item.get("price", 0)
                price = float(price_raw) / 100 if float(price_raw) > 500 else float(price_raw)
                notional = float(item.get("notionalDollars") or item.get("notional") or 0)
                count = int(item.get("tradeCount") or item.get("count") or 0)
                dp_levels.append(DarkPoolLevel(price=price, notional_dollars=notional, trade_count=count))
            except (TypeError, ValueError):
                continue

    dp_levels.sort(key=lambda l: l.price)
    return DarkPoolSnapshot(rows=dp_levels)


def parse_quant_alerts(data: dict[str, Any]) -> QuantAlertsSnapshot:
    """Parse unusual-activity alerts response.

    Best-guess shape: list of alert objects with type, contract info,
    premium, and side. QuantData may use various key names.
    """
    resp = data.get("response", {}) if data else {}

    raw_alerts = (
        resp.get("alerts")
        or resp.get("unusualActivity")
        or resp.get("list")
        or []
    )

    alerts: list[QuantAlert] = []
    for item in raw_alerts:
        if not isinstance(item, dict):
            continue
        try:
            ts = int(item.get("timestampMs") or item.get("timestamp_ms") or 0)
            alert_type = str(item.get("alertType") or item.get("alert_type") or item.get("type") or "UNKNOWN")
            # Contract: compose from parts or use pre-formatted string
            contract_str = item.get("contract") or item.get("contractDescription") or ""
            if not contract_str:
                strike = item.get("strikePriceInCents") or item.get("strike") or 0
                right = item.get("contractType") or item.get("right") or "?"
                expiry = item.get("expirationDate") or item.get("expiry") or "?"
                contract_str = f"{right}{int(strike)/100 if int(strike)>500 else strike}@{expiry}"
            premium_raw = float(item.get("premiumDollars") or item.get("premium") or 0)
            # Some endpoints return premium in cents
            if premium_raw > 0 and premium_raw < 100 and item.get("premiumCents"):
                premium_raw = float(item["premiumCents"]) / 100
            side_raw = str(item.get("side") or item.get("contractType") or "CALL").upper()
            side = "CALL" if "CALL" in side_raw or "C" == side_raw else "PUT"
            alerts.append(QuantAlert(
                timestamp_ms=ts,
                alert_type=alert_type,
                contract=contract_str,
                premium_dollars=premium_raw,
                side=side,  # type: ignore[arg-type]
            ))
        except (TypeError, ValueError):
            continue

    # Most recent first
    alerts.sort(key=lambda a: a.timestamp_ms, reverse=True)
    return QuantAlertsSnapshot(alerts=alerts)
