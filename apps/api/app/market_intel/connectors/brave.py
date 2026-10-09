"""Brave Search connector — free web search for Draven's live research.

Wraps the Brave Search API (https://api.search.brave.com/res/v1):

* ``GET /web/search`` — web results: titles, URLs, descriptions.

Auth: ``X-Subscription-Token`` header with the API key (NOT
Authorization: Bearer). ``Cache-Control: no-cache`` is required —
omitting it is a 422.

Free tier: 2,000 queries/month, 1 query/second. Sign up at
https://api.search.brave.com — no credit card required.

What this connector does NOT provide (documented blind spots):
* No keyword search volume, CPC, or competition data (that's DataForSEO).
* No Amazon/product structured data.
* Brave has its own independent index — results differ from Google.
* Snippets are untrusted third-party content, same as any SERP source.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .base import (
    BaseConnector,
    Capability,
    ConnectorAuthError,
    ConnectorError,
    ConnectorNotConfigured,
    DemandSignal,
    KeywordDemand,
    ProductCandidate,
)

log = logging.getLogger(__name__)

API_BASE = "https://api.search.brave.com/res/v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BraveConnector(BaseConnector):
    """Free web search via the Brave Search API."""

    name = "brave"

    # ------------------------------------------------------------------
    # Capability
    # ------------------------------------------------------------------

    @property
    def capability(self) -> Capability:
        return Capability(
            connector="brave",
            display_name="Brave Search",
            data_available=[
                "Web search results: titles, URLs, descriptions/snippets",
                "Independent Brave index (not Google)",
            ],
            access_requirements=(
                "Brave Search API key. Free tier: 2,000 queries/month. "
                "Sign up at https://api.search.brave.com (no credit card)."
            ),
            auth_requirements=(
                "X-Subscription-Token header. Configure per-business in "
                "Settings → Market Intel (vault, encrypted at rest), or set "
                "BRAVE_API_KEY in the API environment as a fallback."
            ),
            rate_limits=(
                "Free tier: 1 query/second, 2,000 queries/month. The "
                "connector makes one HTTP call per search."
            ),
            geo_coverage=(
                "Country parameter (default US) and search_lang for "
                "language targeting."
            ),
            historical_coverage="None — live results only.",
            pricing=(
                "Free tier: 2,000 queries/month. Paid: ~$3-5 per 1,000 "
                "queries. Verify at https://api.search.brave.com."
            ),
            refresh_frequency="On demand — every search calls the live API.",
            blind_spots=[
                "No keyword search volume, CPC, or competition data.",
                "No structured product/Amazon data.",
                "Brave's independent index differs from Google's — "
                "results are not Google results.",
                "Snippets are untrusted third-party content.",
            ],
        )

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def is_configured(self, settings: Any) -> bool:
        return bool(getattr(settings, "BRAVE_API_KEY", ""))

    def missing_config_hint(self, settings: Any) -> str:
        return (
            "credentials not configured — save a Brave Search API key in "
            "Settings → Market Intel (encrypted vault) or set BRAVE_API_KEY "
            "in the API environment. Free: 2,000 queries/month at "
            "https://api.search.brave.com. No live web data was retrieved."
        )

    def _require_configured(self, settings: Any) -> None:
        if not self.is_configured(settings):
            raise ConnectorNotConfigured(
                "Brave Search API key is not configured. Save it in "
                "Settings → Market Intel (encrypted vault) or set "
                "BRAVE_API_KEY in the API environment (free: 2,000 "
                "queries/month at https://api.search.brave.com). "
                "No live web data was retrieved."
            )

    def _headers(self, settings: Any) -> dict[str, str]:
        # X-Subscription-Token is the auth; Cache-Control: no-cache is
        # required by Brave (omitting it returns 422).
        return {
            "X-Subscription-Token": settings.BRAVE_API_KEY,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Cache-Control": "no-cache",
        }

    # ------------------------------------------------------------------
    # BaseConnector interface (web-search-only connector)
    # ------------------------------------------------------------------

    def keyword_demand(
        self, term: str, *, geo: str, settings: Any
    ) -> KeywordDemand:
        raise ConnectorError(
            "Brave Search does not provide keyword demand data — "
            "use the DataForSEO connector for search volume/CPC."
        )

    def search_products(
        self,
        query: str,
        *,
        category: str,
        limit: int,
        geo: str,
        settings: Any,
    ) -> list[ProductCandidate]:
        raise ConnectorError(
            "Brave Search does not provide structured product data — "
            "use the DataForSEO connector for Amazon product evidence."
        )

    # ------------------------------------------------------------------
    # Web search (SERP) — live results for Draven research
    # ------------------------------------------------------------------

    async def serp_search(
        self,
        settings: Any,
        query: str,
        num_results: int = 10,
        country: str = "US",
    ) -> list[dict[str, Any]]:
        """Live web results via Brave Search API.

        Returns [{title, url, description}] — untrusted third-party content;
        callers must treat it as evidence, never verified fact.
        Raises ConnectorNotConfigured / ConnectorAuthError / ConnectorError.
        """
        self._require_configured(settings)
        q = query.strip()
        if not q:
            raise ConnectorError("empty query")
        count = min(max(num_results, 1), 20)
        params = {
            "q": q[:400],
            "count": count,
            "country": country or "US",
            "search_lang": "en",
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0)
            ) as client:
                resp = await client.get(
                    API_BASE + "/web/search",
                    params=params,
                    headers=self._headers(settings),
                )
        except httpx.HTTPError as exc:
            raise ConnectorError(
                f"Brave Search request failed: {exc}"
            ) from exc

        if resp.status_code == 401:
            raise ConnectorAuthError(
                "Brave Search authentication failed (401). Verify the API "
                "key in Settings → Market Intel."
            )
        if resp.status_code == 422:
            raise ConnectorError(
                "Brave Search rejected the request (422) — this usually "
                "means a missing required header or parameter."
            )
        if resp.status_code == 429:
            raise ConnectorError(
                "Brave Search rate limit hit (429) — free tier is "
                "1 query/second. Back off and retry."
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ConnectorError(
                f"Brave Search HTTP {exc.response.status_code}"
            ) from exc

        body = resp.json()
        results = (body.get("web") or {}).get("results") or []
        out: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            if not url:
                continue
            out.append(
                {
                    "title": item.get("title") or "",
                    "url": url,
                    "description": item.get("description") or "",
                }
            )
            if len(out) >= num_results:
                break
        return out
