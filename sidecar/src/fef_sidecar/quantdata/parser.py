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
    DriftEntry,
    DriftSummary,
    ExposureRow,
    GreekType,
    IVRankByContract,
    IVRankSnapshot,
    MaxPainSnapshot,
    NetFlowEntry,
    NetFlowSnapshot,
    OIRow,
    OpenInterestSnapshot,
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
