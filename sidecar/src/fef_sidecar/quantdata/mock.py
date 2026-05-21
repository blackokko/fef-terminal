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
    DriftEntry,
    DriftSummary,
    ExposureRow,
    GreekType,
    IVRankByContract,
    IVRankSnapshot,
    MarketSnapshot,
    MaxPainSnapshot,
    OIRow,
    OpenInterestSnapshot,
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
        rows = _GEX_ROWS if greek == GreekType.GAMMA else _DEX_ROWS
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

    async def fetch_market_snapshot(
        self,
        *,
        include_aggression: bool = True,
        include_iv_rank: bool = True,
        include_oi: bool = True,
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
        )
