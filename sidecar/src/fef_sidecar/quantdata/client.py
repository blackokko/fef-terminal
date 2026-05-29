"""Async QuantData client — official public API (api.quantdata.us/v1).

Migrated 2026-05-29 from the reverse-engineered dashboard backend
(core-lb-prod, JWT + instance_id + per-tool UUIDs + page-filter mutation),
which QuantData revoked. The official API is a clean POST-per-tool design:
each tool is `POST /options/tool/<name>` with a JSON body, authed with
`Authorization: Bearer <qd_ key>`. No instance_id, no server-side metadata
mutation — so every fetch is independent and fully concurrent.

Credentials: ~/.quantdata-mcp/config.json `auth_token` now holds the qd_ key.
Request schemas were captured live — see scripts/maintenance/probe_official_api.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from types import TracebackType
from typing import Any, Self

import httpx

from fef_sidecar.quantdata.config import QuantDataConfig, load_config
from fef_sidecar.quantdata.errors import (
    QuantDataAuthError,
    QuantDataError,
    QuantDataRateLimitError,
)
from fef_sidecar.quantdata.models import (
    AggressionStats,
    ContractPriceTimeSnapshot,
    ContractStats,
    DriftSummary,
    ExpirationExposureSnapshot,
    GreekType,
    IVRankSnapshot,
    MarketSnapshot,
    MaxPainSnapshot,
    NetFlowSnapshot,
    OpenInterestSnapshot,
    OrderFlowSnapshot,
    StrikeExposureSnapshot,
)
from fef_sidecar.quantdata.parser import (
    parse_contract_price_time,
    parse_contract_stats,
    parse_exposure,
    parse_exposure_by_expiration,
    parse_iv_rank,
    parse_max_pain,
    parse_net_drift,
    parse_net_flow,
    parse_oi_by_strike,
    parse_order_flow,
    parse_trade_side_stats,
)

logger = logging.getLogger(__name__)

# Official-API tool names (POST /options/tool/<name>).
ENDPOINTS: Mapping[str, str] = {
    "exposure_by_strike": "exposure-by-strike",
    "exposure_by_expiration": "exposure-by-expiration",
    "net_drift": "net-drift",
    "iv_rank": "iv-rank",
    "contract_side_stats": "contract-trade-side-statistics",
    "max_pain": "max-pain",
    "net_flow": "net-flow",
    "oi_by_strike": "open-interest-by-strike",
    "contract_statistics": "contract-statistics",
    "order_flow": "order-flow-consolidated",  # NOTE: 404 on public API today
    "contract_price_time": "option-price-over-time",
}

# Exposure representation: dollars per 1% underlying move (matches the old
# dashboard scale once divided by 1e6 in the parser).
_REPR_MODE = "PER_ONE_PERCENT_MOVE"


def _today_et() -> str:
    et = timezone(timedelta(hours=-4))
    return datetime.now(et).strftime("%Y-%m-%d")


def _most_recent_trading_day_et() -> str:
    """Return the most recent US equity-options trading day in YYYY-MM-DD ET.

    Weekends roll back to Friday. Before 9:30 ET on a weekday rolls back to
    the previous trading day (since today's session hasn't opened — QuantData
    has no fresh snapshot data). Used by set_page_filter so weekend/pre-open
    requests don't 404 on endpoints that key off the session_date filter."""
    et = timezone(timedelta(hours=-4))
    now = datetime.now(et)
    # weekday(): Mon=0..Sun=6
    if now.weekday() == 5:        # Saturday
        days_back = 1
    elif now.weekday() == 6:      # Sunday
        days_back = 2
    elif now.weekday() == 0 and now.hour < 9 or (now.weekday() == 0 and now.hour == 9 and now.minute < 30):
        # Monday before open → Friday
        days_back = 3
    elif now.hour < 9 or (now.hour == 9 and now.minute < 30):
        # Tue-Fri before open → previous weekday
        days_back = 1
    else:
        days_back = 0
    target = now - timedelta(days=days_back)
    return target.strftime("%Y-%m-%d")


class QuantDataClient:
    """Async HTTP client for the QuantData REST API."""

    def __init__(
        self,
        config: QuantDataConfig | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
    ) -> None:
        self.config = config or load_config()
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._http: httpx.AsyncClient | None = None
        # Session scoping (set via set_page_filter; defaults computed per-call).
        self._session_date: str | None = None
        self._ticker: str = "SPX"
        self._expiration: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        self._http = httpx.AsyncClient(
            base_url=self.config.base_url,
            headers=self._headers(),
            timeout=self._timeout,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def aclose(self) -> None:
        await self.__aexit__(None, None, None)

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {self.config.auth_token}",
        }

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("QuantDataClient must be used as an async context manager")
        return self._http

    # ------------------------------------------------------------------
    # Low-level request
    # ------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        client = self._client()
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = await client.request(method, path, **kwargs)
                if response.status_code == 401:
                    # IMPROVEMENTS #12: surface auth failure loudly. Re-auth flow
                    # requires the MCP's interactive setup, which we don't drive
                    # from here — user must re-run `quantdata-mcp setup`. We log
                    # at ERROR and let callers decide whether to broadcast a status.
                    logger.error(
                        "QuantData 401: token expired or invalid. "
                        "Re-run `quantdata-mcp setup --auth-token <NEW_TOKEN>` to refresh."
                    )
                    raise QuantDataAuthError("Authentication failed — check token and instance_id")
                if response.status_code == 429:
                    if attempt < self._max_retries - 1:
                        delay = self._retry_base_delay * (2**attempt)
                        logger.warning("QuantData rate-limited, retrying in %.1fs", delay)
                        await asyncio.sleep(delay)
                        continue
                    raise QuantDataRateLimitError("Rate limit exceeded after retries")
                response.raise_for_status()
                return response
            except httpx.HTTPError as e:
                last_exc = e
                if attempt < self._max_retries - 1:
                    delay = self._retry_base_delay * (2**attempt)
                    logger.warning(
                        "QuantData request failed (attempt %d/%d): %s",
                        attempt + 1,
                        self._max_retries,
                        e,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise QuantDataError(
                    f"Request failed after {self._max_retries} attempts: {e}"
                ) from e
        raise QuantDataError(f"Unreachable: {last_exc}")

    async def _post_tool(self, tool: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST /options/tool/<tool> with a JSON body; return parsed JSON."""
        response = await self._request("POST", f"options/tool/{tool}", json=body)
        return response.json()

    # ------------------------------------------------------------------
    # Session scoping (date / ticker / expiration)
    # ------------------------------------------------------------------

    async def set_page_filter(
        self,
        session_date: str | None = None,
        ticker: str = "SPX",
        expiration_date: str | None = None,
    ) -> None:
        """Set the session date / ticker / expiration for subsequent fetches.

        On the official API there is no server-side page filter — each request
        carries `sessionDate` + `filter`. This just stores the scope locally
        (no HTTP), preserving the call sites (poll loop, backfill scripts).
        Defaults to the most recent trading day so weekend/pre-open requests
        don't return empty.
        """
        self._session_date = session_date or _most_recent_trading_day_et()
        self._ticker = ticker
        self._expiration = expiration_date or self._session_date

    def _sd(self) -> str:
        return self._session_date or _most_recent_trading_day_et()

    def _exp(self) -> str:
        return self._expiration or self._sd()

    def _filter(self, **extra: Any) -> dict[str, Any]:
        return {"ticker": self._ticker, **extra}

    # ------------------------------------------------------------------
    # Typed fetch methods
    # ------------------------------------------------------------------

    async def get_exposure_by_strike(
        self,
        greek: GreekType = GreekType.GAMMA,
        *,
        time_minutes: int | None = None,
    ) -> StrikeExposureSnapshot:
        """Fetch the per-strike exposure chain for one greek (summed across expiries).

        ``time_minutes`` is accepted for call-site compatibility but ignored —
        the official API serves the current/EOD chain for ``sessionDate`` only.
        """
        body = {
            "sessionDate": self._sd(),
            "filter": self._filter(),
            "greekMode": greek.value,
            "representationMode": _REPR_MODE,
        }
        raw = await self._post_tool(ENDPOINTS["exposure_by_strike"], body)
        return parse_exposure(raw, greek)

    async def get_net_drift(self) -> DriftSummary:
        body = {"sessionDate": self._sd(), "filter": self._filter()}
        raw = await self._post_tool(ENDPOINTS["net_drift"], body)
        return parse_net_drift(raw)

    async def get_max_pain(self) -> MaxPainSnapshot:
        # Official max-pain requires an expirationDate (per-expiry); we use the
        # session's 0DTE expiration. No stockPrice in the response — the
        # orchestrator fills spx_price from the exposure snapshot.
        body = {"sessionDate": self._sd(), "filter": self._filter(expirationDate=self._exp())}
        raw = await self._post_tool(ENDPOINTS["max_pain"], body)
        return parse_max_pain(raw)

    async def get_iv_rank(self, session_date: str | None = None) -> IVRankSnapshot:
        # maturity=30d (VIX-equivalent ATM IV), lookBackPeriod=30d for min/max window.
        body = {
            "sessionDate": self._sd(),
            "filter": self._filter(),
            "lookBackPeriod": 30,
            "maturity": 30,
        }
        raw = await self._post_tool(ENDPOINTS["iv_rank"], body)
        return parse_iv_rank(raw, session_date or self._sd())

    async def get_trade_side_stats(self) -> AggressionStats:
        body = {"sessionDate": self._sd(), "filter": self._filter(), "dataMode": "PREMIUM"}
        raw = await self._post_tool(ENDPOINTS["contract_side_stats"], body)
        return parse_trade_side_stats(raw)

    async def get_oi_by_strike(self) -> OpenInterestSnapshot:
        # No stockPrice in the response — orchestrator fills spx_price.
        body = {"sessionDate": self._sd(), "filter": self._filter()}
        raw = await self._post_tool(ENDPOINTS["oi_by_strike"], body)
        return parse_oi_by_strike(raw)

    async def get_contract_statistics(self) -> ContractStats:
        body = {"sessionDate": self._sd(), "filter": self._filter()}
        raw = await self._post_tool(ENDPOINTS["contract_statistics"], body)
        return parse_contract_stats(raw)

    async def get_net_flow(self) -> NetFlowSnapshot:
        body = {"sessionDate": self._sd(), "filter": self._filter(), "dataMode": "NET_PREMIUM"}
        raw = await self._post_tool(ENDPOINTS["net_flow"], body)
        return parse_net_flow(raw)

    async def get_exposure_by_expiration(
        self,
        greek: GreekType = GreekType.GAMMA,
    ) -> ExpirationExposureSnapshot:
        """Fetch expiration-bucketed GEX/DEX (summed across strikes per expiry)."""
        body = {
            "sessionDate": self._sd(),
            "filter": self._filter(),
            "greekMode": greek.value,
            "representationMode": _REPR_MODE,
        }
        raw = await self._post_tool(ENDPOINTS["exposure_by_expiration"], body)
        return parse_exposure_by_expiration(raw, greek)

    async def get_order_flow(self) -> OrderFlowSnapshot:
        """Consolidated order-flow stream.

        NOTE (2026-05-29): the official public API does not expose an
        order-flow tool yet (404). Returns an empty snapshot — order flow is
        research-only (whale/golden-sweep counts). No HTTP call is made to
        avoid per-poll 404 noise.
        """
        return parse_order_flow({})

    async def get_contract_price_time(
        self,
        strike: float,
        right: str,
        expiration: str,
        *,
        ticker: str = "SPX",
    ) -> ContractPriceTimeSnapshot:
        """Intraday price path for one contract (official: option-price-over-time).

        On-demand / research-only — not part of fetch_market_snapshot and not
        yet remapped to the official response shape (returns an empty path).
        """
        return parse_contract_price_time(
            {}, strike=strike, right=right, expiration=expiration
        )

    # ------------------------------------------------------------------
    # Composite snapshot — one call, all fetches concurrent
    # ------------------------------------------------------------------

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
        include_net_flow: bool = True,
    ) -> MarketSnapshot:
        """Fetch GEX + DEX + drift + max_pain (+ optional extras) concurrently.

        The official API is stateless per request (no server-side metadata
        mutation), so every fetch runs concurrently — including all four
        greeks and the expiration-bucketed exposures.
        """
        # Each entry is a coroutine factory keyed by result name. A failed
        # optional fetch is logged and dropped (None) rather than aborting the
        # whole snapshot.
        jobs: dict[str, Any] = {
            "gex": self.get_exposure_by_strike(GreekType.GAMMA),
            "dex": self.get_exposure_by_strike(GreekType.DELTA),
            "drift": self.get_net_drift(),
            "max_pain": self.get_max_pain(),
        }
        if include_cex:
            jobs["cex"] = self.get_exposure_by_strike(GreekType.CHARM)
        if include_vex:
            jobs["vex"] = self.get_exposure_by_strike(GreekType.VANNA)
        if include_expiry_exposure:
            jobs["gex_by_expiry"] = self.get_exposure_by_expiration(GreekType.GAMMA)
            jobs["dex_by_expiry"] = self.get_exposure_by_expiration(GreekType.DELTA)
        if include_aggression:
            jobs["aggression"] = self.get_trade_side_stats()
        if include_iv_rank:
            jobs["iv_rank"] = self.get_iv_rank()
        if include_oi:
            jobs["oi"] = self.get_oi_by_strike()
        if include_order_flow:
            jobs["order_flow"] = self.get_order_flow()
        if include_net_flow:
            jobs["net_flow"] = self.get_net_flow()

        settled = await asyncio.gather(*jobs.values(), return_exceptions=True)
        results: dict[str, Any] = {}
        for name, value in zip(jobs.keys(), settled, strict=True):
            if isinstance(value, Exception):
                logger.warning("QuantData %s fetch failed: %s", name, value)
                results[name] = None
            else:
                results[name] = value

        gex: StrikeExposureSnapshot | None = results.get("gex")
        if gex is None:
            raise QuantDataError("fetch_market_snapshot: required GEX fetch failed")
        spx = gex.spx_price

        # drift / max_pain are required by the model — degrade to empty rather
        # than aborting the whole snapshot if a transient fetch failed.
        drift = results.get("drift") or DriftSummary(
            entries=[], cumulative_call_m=0.0, cumulative_put_m=0.0,
            cumulative_net_m=0.0, bias="NEUTRAL",
        )
        # max_pain / oi responses carry no stockPrice — backfill from GEX.
        max_pain = results.get("max_pain") or MaxPainSnapshot(max_pain=0.0, spx_price=spx)
        if not max_pain.spx_price:
            max_pain = max_pain.model_copy(update={"spx_price": spx})
        oi = results.get("oi")
        if oi is not None and not oi.spx_price:
            oi = oi.model_copy(update={"spx_price": spx})

        return MarketSnapshot(
            fetched_at_ms=int(time.time() * 1000),
            spx_price=spx,
            gex=gex,
            dex=results.get("dex") or StrikeExposureSnapshot(greek=GreekType.DELTA, spx_price=spx, rows=[]),
            drift=drift,
            max_pain=max_pain,
            aggression=results.get("aggression"),
            iv_rank=results.get("iv_rank"),
            oi=oi,
            cex=results.get("cex"),
            vex=results.get("vex"),
            gex_by_expiry=results.get("gex_by_expiry"),
            dex_by_expiry=results.get("dex_by_expiry"),
            order_flow=results.get("order_flow"),
            net_flow=results.get("net_flow"),
        )
