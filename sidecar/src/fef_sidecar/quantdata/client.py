"""Async QuantData client.

Wraps `https://core-lb-prod.quantdata.us/api` with typed Pydantic responses,
concurrent fetches via httpx + asyncio.gather, and the page-filter management
needed for per-session date/ticker scoping.

Credentials come from ~/.quantdata-mcp/config.json (written by the MCP
package's `setup` flow) — see fef_sidecar.quantdata.config.
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
    ContractStats,
    DriftSummary,
    GreekType,
    IVRankSnapshot,
    MarketSnapshot,
    MaxPainSnapshot,
    NetFlowSnapshot,
    OpenInterestSnapshot,
    StrikeExposureSnapshot,
)
from fef_sidecar.quantdata.parser import (
    parse_contract_stats,
    parse_exposure,
    parse_iv_rank,
    parse_max_pain,
    parse_net_drift,
    parse_net_flow,
    parse_oi_by_strike,
    parse_trade_side_stats,
)

logger = logging.getLogger(__name__)

ENDPOINTS: Mapping[str, str] = {
    "exposure_by_strike": "options/exposure/strike",
    "net_drift": "options/net-drift",
    "iv_rank": "options/iv-rank",
    "contract_side_stats": "options/contract/statistics/trade-side",
    "max_pain": "options/max-pain",
    "net_flow": "options/net-flow",
    "oi_by_strike": "options/open-interest/strike",
    "contract_statistics": "options/contract/statistics",
}


def _today_et() -> str:
    et = timezone(timedelta(hours=-4))
    return datetime.now(et).strftime("%Y-%m-%d")


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
        self._page_filter_set: tuple[str, str] | None = None

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
            "authorization": self.config.auth_token,
            "x-instance-id": self.config.instance_id,
            "x-qd-version": "1",
            "origin": "https://v3.quantdata.us",
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

    async def _get_json(self, path: str) -> dict[str, Any]:
        response = await self._request("GET", path)
        return response.json()

    def _tool_id(self, canonical: str) -> str:
        tid = self.config.tools.get(canonical)
        if not tid:
            raise QuantDataError(
                f"No tool ID configured for '{canonical}'. "
                "Re-run `quantdata-mcp setup` to provision tools."
            )
        return tid

    # ------------------------------------------------------------------
    # Page filter (session date / ticker scoping)
    # ------------------------------------------------------------------

    async def set_page_filter(
        self,
        session_date: str | None = None,
        ticker: str = "SPX",
        expiration_date: str | None = None,
    ) -> None:
        """Set page-level filters so subsequent fetches use the right session.

        Idempotent — skipped if the same (date, ticker) was already set.
        """
        date = session_date or _today_et()
        if self._page_filter_set == (date, ticker):
            return

        page_id = self.config.page_id
        if not page_id:
            raise QuantDataError("No page_id configured — cannot set page filter")

        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        payload = {
            "id": page_id,
            "expirationDate": {
                "filterOperationType": "EQUALS",
                "value": expiration_date or date,
            },
            "sessionDate": {"filterOperationType": "EQUALS", "value": date},
            "ticker": {"filterOperationType": "EQUALS", "value": [ticker]},
            "createdTime": now_ms,
            "lastUpdatedTime": now_ms,
        }
        await self._request("PUT", "page-filter", json=payload)
        self._page_filter_set = (date, ticker)
        logger.debug("QuantData page filter set: date=%s ticker=%s", date, ticker)

    async def _update_tool_metadata(
        self,
        tool_id: str,
        metadata_updates: dict[str, Any],
    ) -> None:
        """Fetch current tool config, merge metadata, PUT back."""
        resp = await self._get_json(f"tool/{tool_id}")
        current = resp.get("response", {}).get("toolDTO")
        if current is None:
            raise QuantDataError(f"Unexpected get_tool response for {tool_id[:8]}...")
        metadata = current.get("metadata", {})
        metadata.update(metadata_updates)
        current["metadata"] = metadata
        current["lastUpdatedTime"] = int(datetime.now(UTC).timestamp() * 1000)
        await self._request("PUT", "tool", json=current)

    # ------------------------------------------------------------------
    # Typed fetch methods
    # ------------------------------------------------------------------

    async def get_exposure_by_strike(
        self,
        greek: GreekType = GreekType.GAMMA,
    ) -> StrikeExposureSnapshot:
        tool_id = self._tool_id("exposure_by_strike")
        await self._update_tool_metadata(tool_id, {"greekModeType": greek.value})
        raw = await self._get_json(f"{ENDPOINTS['exposure_by_strike']}/{tool_id}")
        # Restore to GAMMA for next caller (matches MCP behavior)
        if greek != GreekType.GAMMA:
            await self._update_tool_metadata(tool_id, {"greekModeType": "GAMMA"})
        return parse_exposure(raw, greek)

    async def get_net_drift(self) -> DriftSummary:
        tool_id = self._tool_id("net_drift")
        raw = await self._get_json(f"{ENDPOINTS['net_drift']}/{tool_id}")
        return parse_net_drift(raw)

    async def get_max_pain(self) -> MaxPainSnapshot:
        tool_id = self._tool_id("max_pain")
        raw = await self._get_json(f"{ENDPOINTS['max_pain']}/{tool_id}")
        return parse_max_pain(raw)

    async def get_iv_rank(self, session_date: str | None = None) -> IVRankSnapshot:
        tool_id = self._tool_id("iv_rank")
        raw = await self._get_json(f"{ENDPOINTS['iv_rank']}/{tool_id}")
        return parse_iv_rank(raw, session_date or _today_et())

    async def get_trade_side_stats(self) -> AggressionStats:
        tool_id = self._tool_id("contract_side_stats")
        raw = await self._get_json(f"{ENDPOINTS['contract_side_stats']}/{tool_id}")
        return parse_trade_side_stats(raw)

    async def get_oi_by_strike(self) -> OpenInterestSnapshot:
        tool_id = self._tool_id("oi_by_strike")
        raw = await self._get_json(f"{ENDPOINTS['oi_by_strike']}/{tool_id}")
        return parse_oi_by_strike(raw)

    async def get_contract_statistics(self) -> ContractStats:
        tool_id = self._tool_id("contract_statistics")
        raw = await self._get_json(f"{ENDPOINTS['contract_statistics']}/{tool_id}")
        return parse_contract_stats(raw)

    async def get_net_flow(self) -> NetFlowSnapshot:
        tool_id = self._tool_id("net_flow")
        raw = await self._get_json(f"{ENDPOINTS['net_flow']}/{tool_id}")
        return parse_net_flow(raw)

    # ------------------------------------------------------------------
    # Composite snapshot — one call, concurrent fetches
    # ------------------------------------------------------------------

    async def fetch_market_snapshot(
        self,
        *,
        include_aggression: bool = True,
        include_iv_rank: bool = True,
        include_oi: bool = True,
    ) -> MarketSnapshot:
        """Fetch GEX + DEX + drift + max_pain (+ optional extras) concurrently."""
        tasks: dict[str, asyncio.Task[Any]] = {
            "gex": asyncio.create_task(self.get_exposure_by_strike(GreekType.GAMMA)),
            "dex": asyncio.create_task(self.get_exposure_by_strike(GreekType.DELTA)),
            "drift": asyncio.create_task(self.get_net_drift()),
            "max_pain": asyncio.create_task(self.get_max_pain()),
        }
        if include_aggression:
            tasks["aggression"] = asyncio.create_task(self.get_trade_side_stats())
        if include_iv_rank:
            tasks["iv_rank"] = asyncio.create_task(self.get_iv_rank())
        if include_oi:
            tasks["oi"] = asyncio.create_task(self.get_oi_by_strike())

        results = dict(zip(tasks.keys(), await asyncio.gather(*tasks.values()), strict=True))

        gex: StrikeExposureSnapshot = results["gex"]
        return MarketSnapshot(
            fetched_at_ms=int(time.time() * 1000),
            spx_price=gex.spx_price,
            gex=gex,
            dex=results["dex"],
            drift=results["drift"],
            max_pain=results["max_pain"],
            aggression=results.get("aggression"),
            iv_rank=results.get("iv_rank"),
            oi=results.get("oi"),
        )
