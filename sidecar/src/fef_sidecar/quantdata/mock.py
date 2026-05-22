"""Mock QuantData provider with frozen 2026-05-15 session data.

Same async interface as QuantDataClient so the rest of the sidecar can run
offline (no QuantData credentials, no network). Useful for FEF engine tests
and frontend development.
"""

from __future__ import annotations

import time
from types import TracebackType
from typing import Self

from fef_sidecar.quantdata.models import (
    AggressionStats,
    ContractAggression,
    ContractPricePoint,
    ContractPriceTimeSnapshot,
    DriftEntry,
    DriftSummary,
    ExpirationExposureRow,
    ExpirationExposureSnapshot,
    ExposureRow,
    GreekType,
    IVRankByContract,
    IVRankSnapshot,
    MarketSnapshot,
    MaxPainSnapshot,
    OIRow,
    OpenInterestSnapshot,
    OrderFlowRow,
    OrderFlowSnapshot,
    StrikeExposureSnapshot,
)  # noqa: F401  AggressionStats + ContractAggression used inside fetch_market_snapshot

# Captured from live session 2026-05-15 around 4pm ET (SPX $7,409).
SPX_PRICE = 7409.18

# Top GEX walls (per the 5/15 snapshot we pulled in the prior session)
_GEX_ROWS = [
    ExposureRow(strike=7405.0, call_m=3585.16, put_m=-41679.29, net_m=-38094.13),
    ExposureRow(strike=7400.0, call_m=55511.32, put_m=-22808.10, net_m=32703.22),
    ExposureRow(strike=7410.0, call_m=17259.25, put_m=0.0, net_m=17259.25),
    ExposureRow(strike=7415.0, call_m=3330.53, put_m=0.0, net_m=3330.53),
    ExposureRow(strike=7450.0, call_m=490.91, put_m=-3144.80, net_m=-2653.89),
    ExposureRow(strike=7420.0, call_m=1236.48, put_m=-3456.41, net_m=-2219.94),
    ExposureRow(strike=7390.0, call_m=0.0, put_m=-1370.98, net_m=-1370.98),
    ExposureRow(strike=7395.0, call_m=543.92, put_m=-1850.68, net_m=-1306.76),
    ExposureRow(strike=7440.0, call_m=1102.01, put_m=0.0, net_m=1102.01),
    ExposureRow(strike=7300.0, call_m=1105.89, put_m=-136.84, net_m=969.04),
]

_DEX_ROWS = [
    ExposureRow(strike=7400.0, call_m=1377032.50, put_m=-40699.12, net_m=1336333.38),
    ExposureRow(strike=7500.0, call_m=6378.55, put_m=-982145.18, net_m=-975766.62),
    ExposureRow(strike=6800.0, call_m=746760.53, put_m=-471.52, net_m=746289.01),
    ExposureRow(strike=7200.0, call_m=722415.05, put_m=-445.27, net_m=721969.78),
    ExposureRow(strike=7300.0, call_m=660853.68, put_m=-1906.55, net_m=658947.13),
    ExposureRow(strike=7450.0, call_m=2766.66, put_m=-631842.99, net_m=-629076.33),
    ExposureRow(strike=7100.0, call_m=608675.40, put_m=-719.81, net_m=607955.59),
    ExposureRow(strike=6950.0, call_m=559371.62, put_m=-131.75, net_m=559239.87),
    ExposureRow(strike=6900.0, call_m=469062.66, put_m=-196.99, net_m=468865.67),
    ExposureRow(strike=7415.0, call_m=8671.86, put_m=-470269.35, net_m=-461597.49),
]

_CEX_ROWS = [
    ExposureRow(strike=7400.0, call_m=-12450.30, put_m=-8120.10, net_m=-20570.40),
    ExposureRow(strike=7410.0, call_m=-9876.50, put_m=-2310.20, net_m=-12186.70),
    ExposureRow(strike=7415.0, call_m=-7234.20, put_m=-1856.40, net_m=-9090.60),
    ExposureRow(strike=7405.0, call_m=-8101.30, put_m=-540.10, net_m=-8641.40),
    ExposureRow(strike=7395.0, call_m=-3210.40, put_m=4823.50, net_m=1613.10),
    ExposureRow(strike=7420.0, call_m=-2987.10, put_m=-3456.40, net_m=-6443.50),
    ExposureRow(strike=7390.0, call_m=-1840.20, put_m=5210.30, net_m=3370.10),
    ExposureRow(strike=7385.0, call_m=-1120.50, put_m=6234.10, net_m=5113.60),
    ExposureRow(strike=7425.0, call_m=-2456.30, put_m=-1834.60, net_m=-4290.90),
    ExposureRow(strike=7380.0, call_m=-756.40, put_m=7234.50, net_m=6478.10),
]

_VEX_ROWS = [
    ExposureRow(strike=7400.0, call_m=15234.50, put_m=-12876.30, net_m=2358.20),
    ExposureRow(strike=7450.0, call_m=18934.20, put_m=-3456.10, net_m=15478.10),
    ExposureRow(strike=7350.0, call_m=2876.10, put_m=-17234.50, net_m=-14358.40),
    ExposureRow(strike=7500.0, call_m=12345.80, put_m=-987.30, net_m=11358.50),
    ExposureRow(strike=7300.0, call_m=1234.50, put_m=-13876.40, net_m=-12641.90),
    ExposureRow(strike=7425.0, call_m=14567.30, put_m=-7234.10, net_m=7333.20),
    ExposureRow(strike=7375.0, call_m=5234.20, put_m=-11456.30, net_m=-6222.10),
    ExposureRow(strike=7475.0, call_m=8765.40, put_m=-2123.50, net_m=6641.90),
    ExposureRow(strike=7325.0, call_m=2345.60, put_m=-9876.20, net_m=-7530.60),
    ExposureRow(strike=7410.0, call_m=11234.50, put_m=-9876.30, net_m=1358.20),
]

_OI_ROWS = [
    OIRow(strike=7400.0, call_oi=30921, put_oi=9240),
    OIRow(strike=7450.0, call_oi=8333, put_oi=6618),
    OIRow(strike=7425.0, call_oi=2314, put_oi=3783),
    OIRow(strike=7375.0, call_oi=3905, put_oi=1312),
    OIRow(strike=7460.0, call_oi=1206, put_oi=3626),
    OIRow(strike=7360.0, call_oi=3077, put_oi=1564),
    OIRow(strike=7440.0, call_oi=1593, put_oi=3024),
    OIRow(strike=7420.0, call_oi=1695, put_oi=1986),
    OIRow(strike=7455.0, call_oi=646, put_oi=2833),
    OIRow(strike=7390.0, call_oi=908, put_oi=2318),
    OIRow(strike=7410.0, call_oi=812, put_oi=2269),
    OIRow(strike=7395.0, call_oi=622, put_oi=2185),
    OIRow(strike=7445.0, call_oi=582, put_oi=1929),
    OIRow(strike=7415.0, call_oi=275, put_oi=1985),
    OIRow(strike=7405.0, call_oi=920, put_oi=1196),
]

_DRIFT_ENTRIES = [
    DriftEntry(timestamp_ms=1747929360000, call_dollars=-63556, put_dollars=804447, spx_price=7407.66),
    DriftEntry(timestamp_ms=1747929420000, call_dollars=7601, put_dollars=-307498, spx_price=7410.77),
    DriftEntry(timestamp_ms=1747929480000, call_dollars=12697165, put_dollars=282831, spx_price=7408.77),
    DriftEntry(timestamp_ms=1747929540000, call_dollars=-45701, put_dollars=-456257, spx_price=7407.26),
    DriftEntry(timestamp_ms=1747929600000, call_dollars=0, put_dollars=-7845, spx_price=7407.26),
]

# Expiration-bucketed GEX mock (exposure_by_expiration, GAMMA).
# 0DTE = 2026-05-21 (today); next expirations trail off in exposure.
_GEX_BY_EXPIRY_ROWS = [
    ExpirationExposureRow(expiration="2026-05-21", call_m=18230.50, put_m=-31456.20, net_m=-13225.70),
    ExpirationExposureRow(expiration="2026-05-23", call_m=8340.10, put_m=-4120.40, net_m=4219.70),
    ExpirationExposureRow(expiration="2026-05-30", call_m=5820.30, put_m=-9870.60, net_m=-4050.30),
    ExpirationExposureRow(expiration="2026-06-20", call_m=11430.80, put_m=-6230.10, net_m=5200.70),
    ExpirationExposureRow(expiration="2026-07-17", call_m=3210.40, put_m=-2870.90, net_m=339.50),
    ExpirationExposureRow(expiration="2026-09-18", call_m=1980.20, put_m=-1340.60, net_m=639.60),
]

# Expiration-bucketed DEX mock (DELTA). Same expirations, different magnitudes.
_DEX_BY_EXPIRY_ROWS = [
    ExpirationExposureRow(expiration="2026-05-21", call_m=342180.30, put_m=-189430.60, net_m=152749.70),
    ExpirationExposureRow(expiration="2026-05-23", call_m=124560.80, put_m=-98230.40, net_m=26330.40),
    ExpirationExposureRow(expiration="2026-05-30", call_m=198340.20, put_m=-231450.80, net_m=-33110.60),
    ExpirationExposureRow(expiration="2026-06-20", call_m=431200.50, put_m=-312680.30, net_m=118520.20),
    ExpirationExposureRow(expiration="2026-07-17", call_m=89430.70, put_m=-76210.40, net_m=13220.30),
    ExpirationExposureRow(expiration="2026-09-18", call_m=52340.10, put_m=-47890.60, net_m=4449.50),
]

# Mock order-flow: mix of whales, sweeps, blocks, and regular orders.
# Timestamps: ~1h before session snapshot (approx 3pm ET on 5/15/2026).
_ORDER_FLOW_ROWS = [
    OrderFlowRow(
        timestamp_ms=1747926600000, strike=7400.0, right="C", expiration="2026-05-21",
        premium_dollars=1_250_000.0, side_aggression="AA",
        is_whale=True, is_sweep=True, is_golden_sweep=True,
        other_flags=["GOLDEN_SWEEP"],
    ),
    OrderFlowRow(
        timestamp_ms=1747926480000, strike=7390.0, right="P", expiration="2026-05-21",
        premium_dollars=345_000.0, side_aggression="A",
        is_sweep=True,
    ),
    OrderFlowRow(
        timestamp_ms=1747926360000, strike=7450.0, right="C", expiration="2026-05-21",
        premium_dollars=2_100_000.0, side_aggression="AA",
        is_whale=True, is_block=True,
        other_flags=["BLOCK"],
    ),
    OrderFlowRow(
        timestamp_ms=1747926240000, strike=7410.0, right="P", expiration="2026-05-21",
        premium_dollars=87_000.0, side_aggression="M",
    ),
    OrderFlowRow(
        timestamp_ms=1747926120000, strike=7405.0, right="C", expiration="2026-05-21",
        premium_dollars=54_000.0, side_aggression="B",
    ),
    OrderFlowRow(
        timestamp_ms=1747926000000, strike=7400.0, right="P", expiration="2026-05-21",
        premium_dollars=1_450_000.0, side_aggression="AA",
        is_whale=True, is_sweep=True, is_golden_sweep=True,
    ),
    OrderFlowRow(
        timestamp_ms=1747925880000, strike=7420.0, right="C", expiration="2026-05-21",
        premium_dollars=230_000.0, side_aggression="A",
        is_sweep=True,
    ),
    OrderFlowRow(
        timestamp_ms=1747925760000, strike=7380.0, right="P", expiration="2026-05-21",
        premium_dollars=3_200_000.0, side_aggression="AA",
        is_whale=True, is_block=True, is_sweep=True, is_golden_sweep=True,
    ),
    OrderFlowRow(
        timestamp_ms=1747925640000, strike=7415.0, right="C", expiration="2026-05-21",
        premium_dollars=42_000.0, side_aggression="BB",
    ),
    OrderFlowRow(
        timestamp_ms=1747925520000, strike=7395.0, right="P", expiration="2026-05-21",
        premium_dollars=175_000.0, side_aggression="B",
    ),
    OrderFlowRow(
        timestamp_ms=1747925400000, strike=7400.0, right="C", expiration="2026-05-21",
        premium_dollars=890_000.0, side_aggression="A",
        is_sweep=True,
    ),
    OrderFlowRow(
        timestamp_ms=1747925280000, strike=7425.0, right="P", expiration="2026-05-21",
        premium_dollars=61_000.0, side_aggression="M",
    ),
    OrderFlowRow(
        timestamp_ms=1747925160000, strike=7300.0, right="C", expiration="2026-06-20",
        premium_dollars=1_780_000.0, side_aggression="AA",
        is_whale=True, is_golden_sweep=True,
    ),
]


class MockQuantDataClient:
    """In-memory provider matching QuantDataClient's interface."""

    def __init__(self, spx_price: float = SPX_PRICE) -> None:
        self.spx_price = spx_price
        self._fetch_count = 0
        self._cum_drift_m = 0.0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def set_page_filter(
        self,
        session_date: str | None = None,
        ticker: str = "SPX",
        expiration_date: str | None = None,
    ) -> None:
        return None

    async def get_exposure_by_strike(
        self, greek: GreekType = GreekType.GAMMA
    ) -> StrikeExposureSnapshot:
        rows_by_greek = {
            GreekType.GAMMA: _GEX_ROWS,
            GreekType.DELTA: _DEX_ROWS,
            GreekType.CHARM: _CEX_ROWS,
            GreekType.VANNA: _VEX_ROWS,
        }
        rows = rows_by_greek.get(greek, _GEX_ROWS)
        return StrikeExposureSnapshot(greek=greek, spx_price=self.spx_price, rows=list(rows))

    async def get_net_drift(self) -> DriftSummary:
        entries = list(_DRIFT_ENTRIES)
        cum_call = sum(e.call_dollars for e in entries)
        cum_put = sum(e.put_dollars for e in entries)
        cum_net = cum_call - cum_put
        _DOMINANCE_RATIO = 0.15
        _total_abs = abs(cum_call) + abs(cum_put)
        if _total_abs <= 0:
            _bias = "NEUTRAL"
        elif cum_net > _DOMINANCE_RATIO * _total_abs:
            _bias = "BULLISH"
        elif cum_net < -_DOMINANCE_RATIO * _total_abs:
            _bias = "BEARISH"
        else:
            _bias = "NEUTRAL"
        return DriftSummary(
            entries=entries,
            cumulative_call_m=cum_call / 1_000_000,
            cumulative_put_m=cum_put / 1_000_000,
            cumulative_net_m=cum_net / 1_000_000,
            bias=_bias,
        )

    async def get_max_pain(self) -> MaxPainSnapshot:
        # Frozen reference: raw chain-wide MP was $7,000 on 5/15 (per memory)
        return MaxPainSnapshot(max_pain=7000.0, spx_price=self.spx_price)

    async def get_iv_rank(self, session_date: str | None = None) -> IVRankSnapshot:
        return IVRankSnapshot(
            session_date=session_date or "2026-05-15",
            call=IVRankByContract(last_iv=0.14, window_min_iv=0.10, window_max_iv=0.30),
            put=IVRankByContract(last_iv=0.16, window_min_iv=0.11, window_max_iv=0.32),
        )

    async def get_trade_side_stats(self) -> AggressionStats:
        return AggressionStats(
            call=ContractAggression(
                side="CALL",
                aa=1_124_100_000,
                a=47_625_000_000,
                mid=25_874_100_000,
                b=45_404_400_000,
                bb=808_700_000,
            ),
            put=ContractAggression(
                side="PUT",
                aa=1_685_300_000,
                a=39_735_600_000,
                mid=24_675_100_000,
                b=39_606_500_000,
                bb=2_297_900_000,
            ),
        )

    async def get_oi_by_strike(self) -> OpenInterestSnapshot:
        return OpenInterestSnapshot(spx_price=self.spx_price, rows=list(_OI_ROWS))

    async def get_exposure_by_expiration(
        self, greek: GreekType = GreekType.GAMMA
    ) -> ExpirationExposureSnapshot:
        rows_by_greek = {
            GreekType.GAMMA: _GEX_BY_EXPIRY_ROWS,
            GreekType.DELTA: _DEX_BY_EXPIRY_ROWS,
        }
        rows = rows_by_greek.get(greek, _GEX_BY_EXPIRY_ROWS)
        return ExpirationExposureSnapshot(greek=greek, spx_price=self.spx_price, rows=list(rows))

    async def get_order_flow(self) -> OrderFlowSnapshot:
        return OrderFlowSnapshot(orders=list(_ORDER_FLOW_ROWS))

    async def get_contract_price_time(
        self,
        strike: float,
        right: str,
        expiration: str,
    ) -> ContractPriceTimeSnapshot:
        """Synthesize a mock intraday price path for any contract spec."""
        import math as _math
        base_price = 3.50 + abs(strike - self.spx_price) * 0.01
        # 390 1-min bars starting at 9:30 ET (approx)
        session_open_ms = 1747916400000  # 2026-05-21 09:30 ET
        points = []
        for i in range(60):
            ts = session_open_ms + i * 60_000
            # Simulate slight intraday drift + noise
            price = round(base_price * (1 + 0.005 * _math.sin(i / 8)) + 0.02 * (i % 7 - 3), 2)
            price = max(0.01, price)
            points.append(ContractPricePoint(timestamp_ms=ts, price=price, volume=15 + i % 12))
        return ContractPriceTimeSnapshot(
            strike=strike, right=right, expiration=expiration, points=points
        )

    async def fetch_market_snapshot(
        self,
        *,
        include_aggression: bool = True,
        include_iv_rank: bool = True,
        include_oi: bool = True,
        include_cex: bool = True,
        include_vex: bool = True,
        include_expiry_exposure: bool = True,
        include_order_flow: bool = True,
    ) -> MarketSnapshot:
        """Mock snapshot with per-call variation.

        Two modes:
        - Default (sinusoidal): cycles SPX +/-10pts, accumulates bullish drift
        - Scripted day (FEF_MOCK_SCRIPTED_DAY=true): walks through a 165-iter
          full-day script that hits every regime state (OPEN_DRIVE / TREND /
          ABSORPTION / PIN / WALL_TEST / HIGH_VOL / CHOP / POWER_HOUR). The
          simulated_minutes_since_open is also set so the regime detector
          uses sim time. Pair with FEF_LOOP_INTERVAL_SEC=3 to replay the
          full day in ~8 minutes.
        """
        import math as _math
        import os as _os

        self._fetch_count += 1

        # ----- Coherent mock mode (production default) -----
        # When FEF_MOCK_COHERENT=true (set automatically by make_quantdata_client),
        # use the shared PriceTrack so this snapshot's spx_price matches
        # the live chart + ticks + microstructure at the same moment.
        _coherent = _os.environ.get("FEF_MOCK_COHERENT", "false").lower() in ("true", "1", "yes", "on")
        if _coherent:
            from fef_sidecar.mock import MockClock, get_track
            from fef_sidecar.quantdata.mock_day import frame_at

            track = get_track()
            sim_min = MockClock.simulated_minutes_since_open()
            spx_now = round(track.price_now(), 2)
            frame_idx = max(0, int(sim_min / 2))
            frame = frame_at(frame_idx)
            self._cum_drift_m = frame.drift_cum_net_m
            drift = DriftSummary(
                entries=list(_DRIFT_ENTRIES),
                cumulative_call_m=max(0.0, frame.drift_cum_net_m) + 5.0,
                cumulative_put_m=min(0.0, -frame.drift_cum_net_m / 2) - 5.0,
                cumulative_net_m=frame.drift_cum_net_m,
                bias=frame.drift_bias,
            )
            gex = StrikeExposureSnapshot(greek=GreekType.GAMMA, spx_price=spx_now, rows=list(_GEX_ROWS))
            dex = StrikeExposureSnapshot(greek=GreekType.DELTA, spx_price=spx_now, rows=list(_DEX_ROWS))
            # Per-frame aggression dynamics from the archetype generator
            agg_call = getattr(frame, "aggression_call_pct", 41.0)
            agg_put = getattr(frame, "aggression_put_pct", 41.0)
            return MarketSnapshot(
                fetched_at_ms=int(time.time() * 1000),
                spx_price=spx_now,
                gex=gex,
                dex=dex,
                drift=drift,
                max_pain=MaxPainSnapshot(max_pain=7000.0, spx_price=spx_now),
                aggression=AggressionStats(
                    call=ContractAggression(
                        side="CALL",
                        aa=agg_call * 1e8,  # synthesize raw $ to satisfy schema
                        a=agg_call * 5e8,
                        mid=2.5e10,
                        b=(100 - agg_call) * 4e8,
                        bb=(100 - agg_call) * 1e7,
                    ),
                    put=ContractAggression(
                        side="PUT",
                        aa=agg_put * 1e8,
                        a=agg_put * 5e8,
                        mid=2.5e10,
                        b=(100 - agg_put) * 4e8,
                        bb=(100 - agg_put) * 1e7,
                    ),
                ) if include_aggression else None,
                iv_rank=await self.get_iv_rank() if include_iv_rank else None,
                oi=OpenInterestSnapshot(spx_price=spx_now, rows=list(_OI_ROWS)) if include_oi else None,
                cex=StrikeExposureSnapshot(greek=GreekType.CHARM, spx_price=spx_now, rows=list(_CEX_ROWS)) if include_cex else None,
                vex=StrikeExposureSnapshot(greek=GreekType.VANNA, spx_price=spx_now, rows=list(_VEX_ROWS)) if include_vex else None,
                gex_by_expiry=ExpirationExposureSnapshot(greek=GreekType.GAMMA, spx_price=spx_now, rows=list(_GEX_BY_EXPIRY_ROWS)) if include_expiry_exposure else None,
                dex_by_expiry=ExpirationExposureSnapshot(greek=GreekType.DELTA, spx_price=spx_now, rows=list(_DEX_BY_EXPIRY_ROWS)) if include_expiry_exposure else None,
                order_flow=OrderFlowSnapshot(orders=list(_ORDER_FLOW_ROWS)) if include_order_flow else None,
                simulated_minutes_since_open=frame.minutes_since_open,
                simulated_phase_label=frame.label,
                simulated_vix=frame.vix,
            )

        # ----- Scripted day mode (legacy, fetch-count-paced) -----
        if _os.environ.get("FEF_MOCK_SCRIPTED_DAY", "false").lower() in ("true", "1", "yes"):
            from fef_sidecar.quantdata.mock_day import frame_at

            frame = frame_at(self._fetch_count - 1)
            spx_now = frame.spx
            self._cum_drift_m = frame.drift_cum_net_m
            drift = DriftSummary(
                entries=list(_DRIFT_ENTRIES),
                cumulative_call_m=max(0.0, frame.drift_cum_net_m) + 5.0,
                cumulative_put_m=min(0.0, -frame.drift_cum_net_m / 2) - 5.0,
                cumulative_net_m=frame.drift_cum_net_m,
                bias=frame.drift_bias,
            )
            gex = StrikeExposureSnapshot(greek=GreekType.GAMMA, spx_price=spx_now, rows=list(_GEX_ROWS))
            dex = StrikeExposureSnapshot(greek=GreekType.DELTA, spx_price=spx_now, rows=list(_DEX_ROWS))
            agg_call = getattr(frame, "aggression_call_pct", 41.0)
            agg_put = getattr(frame, "aggression_put_pct", 41.0)
            return MarketSnapshot(
                fetched_at_ms=int(time.time() * 1000),
                spx_price=spx_now, gex=gex, dex=dex, drift=drift,
                max_pain=MaxPainSnapshot(max_pain=7000.0, spx_price=spx_now),
                aggression=AggressionStats(
                    call=ContractAggression(side="CALL", aa=agg_call * 1e8, a=agg_call * 5e8, mid=2.5e10, b=(100 - agg_call) * 4e8, bb=(100 - agg_call) * 1e7),
                    put=ContractAggression(side="PUT", aa=agg_put * 1e8, a=agg_put * 5e8, mid=2.5e10, b=(100 - agg_put) * 4e8, bb=(100 - agg_put) * 1e7),
                ) if include_aggression else None,
                iv_rank=await self.get_iv_rank() if include_iv_rank else None,
                oi=OpenInterestSnapshot(spx_price=spx_now, rows=list(_OI_ROWS)) if include_oi else None,
                cex=StrikeExposureSnapshot(greek=GreekType.CHARM, spx_price=spx_now, rows=list(_CEX_ROWS)) if include_cex else None,
                vex=StrikeExposureSnapshot(greek=GreekType.VANNA, spx_price=spx_now, rows=list(_VEX_ROWS)) if include_vex else None,
                gex_by_expiry=ExpirationExposureSnapshot(greek=GreekType.GAMMA, spx_price=spx_now, rows=list(_GEX_BY_EXPIRY_ROWS)) if include_expiry_exposure else None,
                dex_by_expiry=ExpirationExposureSnapshot(greek=GreekType.DELTA, spx_price=spx_now, rows=list(_DEX_BY_EXPIRY_ROWS)) if include_expiry_exposure else None,
                order_flow=OrderFlowSnapshot(orders=list(_ORDER_FLOW_ROWS)) if include_order_flow else None,
                simulated_minutes_since_open=frame.minutes_since_open,
                simulated_phase_label=frame.label,
                simulated_vix=frame.vix,
            )

        # ----- Default sinusoidal mode -----
        spx_offset = _math.sin(self._fetch_count / 2.5) * 10.0
        spx_now = round(self.spx_price + spx_offset, 2)
        self._cum_drift_m += 5.0 + _math.sin(self._fetch_count / 1.5) * 3.0

        drift = DriftSummary(
            entries=list(_DRIFT_ENTRIES),
            cumulative_call_m=self._cum_drift_m + 5.0,
            cumulative_put_m=-5.0,
            cumulative_net_m=self._cum_drift_m,
            bias="BULLISH" if self._cum_drift_m > 5 else "NEUTRAL",
        )
        gex = StrikeExposureSnapshot(greek=GreekType.GAMMA, spx_price=spx_now, rows=list(_GEX_ROWS))
        dex = StrikeExposureSnapshot(greek=GreekType.DELTA, spx_price=spx_now, rows=list(_DEX_ROWS))
        return MarketSnapshot(
            fetched_at_ms=int(time.time() * 1000),
            spx_price=spx_now,
            gex=gex,
            dex=dex,
            drift=drift,
            max_pain=MaxPainSnapshot(max_pain=7000.0, spx_price=spx_now),
            aggression=await self.get_trade_side_stats() if include_aggression else None,
            iv_rank=await self.get_iv_rank() if include_iv_rank else None,
            oi=OpenInterestSnapshot(spx_price=spx_now, rows=list(_OI_ROWS)) if include_oi else None,
            cex=StrikeExposureSnapshot(greek=GreekType.CHARM, spx_price=spx_now, rows=list(_CEX_ROWS)) if include_cex else None,
            vex=StrikeExposureSnapshot(greek=GreekType.VANNA, spx_price=spx_now, rows=list(_VEX_ROWS)) if include_vex else None,
            gex_by_expiry=ExpirationExposureSnapshot(greek=GreekType.GAMMA, spx_price=spx_now, rows=list(_GEX_BY_EXPIRY_ROWS)) if include_expiry_exposure else None,
            dex_by_expiry=ExpirationExposureSnapshot(greek=GreekType.DELTA, spx_price=spx_now, rows=list(_DEX_BY_EXPIRY_ROWS)) if include_expiry_exposure else None,
            order_flow=OrderFlowSnapshot(orders=list(_ORDER_FLOW_ROWS)) if include_order_flow else None,
        )
