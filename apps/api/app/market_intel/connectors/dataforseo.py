"""DataForSEO connector — live keyword demand + Amazon product evidence.

Mirrors the provider pattern from the ProductScout project (same endpoints,
same defensive field mapping), adapted to the ForgeOS connector interface
with full provenance on every signal.

Endpoints used (https://api.dataforseo.com/v3):
* ``/keywords_data/google_ads/search_volume/live`` — Google Ads search
  volume, CPC, competition index, 12-month monthly history.
* ``/dataforseo_labs/google/bulk_keyword_difficulty/live`` — 0-100 keyword
  difficulty (competition proxy).
* ``/merchant/amazon/products/live/advanced`` — Amazon product search by
  keyword: titles, advertised prices, ratings/votes, ASINs.

Auth: HTTP Basic with DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD (pay-as-you-go;
indicative costs ~$0.075 per search-volume request for up to 1000 keywords,
~$0.004 per 10 SERP results — verify at https://dataforseo.com/pricing).

What this connector does NOT provide (documented blind spots):
* No confirmed transaction/sales data — search volume measures interest,
  Amazon ratings measure social proof. The connector never emits
  ``confirmed_sales`` signals.
* No supplier/wholesale pricing or MOQs.
* No historical Amazon pricing.
"""

from __future__ import annotations

import hashlib
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
    PricePoint,
    ProductCandidate,
)

log = logging.getLogger(__name__)

API_BASE = "https://api.dataforseo.com/v3"

_STATUS_OK = 20000
_STATUS_AUTH_FAILED = 40100
_STATUS_NO_CREDITS = 40200


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DataForSEOConnector(BaseConnector):
    """Live keyword + Amazon product data from DataForSEO."""

    name = "dataforseo"

    # ------------------------------------------------------------------
    # Capability
    # ------------------------------------------------------------------

    @property
    def capability(self) -> Capability:
        return Capability(
            connector="dataforseo",
            display_name="DataForSEO",
            data_available=[
                "Google Ads search volume (monthly, per keyword)",
                "Cost-per-click (CPC) and competition index",
                "12-month monthly search history per keyword",
                "Keyword difficulty score (0-100, DataForSEO Labs)",
                "Related keyword suggestions",
                "Amazon product search: titles, advertised prices, ratings, review counts, ASINs",
            ],
            access_requirements=(
                "DataForSEO account with API access enabled and a positive "
                "balance (pay-as-you-go). Sign up at https://dataforseo.com/"
            ),
            auth_requirements=(
                "HTTP Basic auth. Configure per-business in Settings → "
                "Market Intel (vault, encrypted at rest), or set "
                "DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD in the API "
                "environment as a fallback."
            ),
            rate_limits=(
                "DataForSEO enforces per-account API throughput limits "
                "(varies by plan). The connector makes one HTTP call per "
                "keyword lookup and caches nothing server-side by default; "
                "callers should batch keywords (up to 1000 per request)."
            ),
            geo_coverage=(
                "Google Ads data: 200+ countries via location_code "
                "(default 2840 = United States). Amazon product data: "
                "marketplaces supported by the Merchant API."
            ),
            historical_coverage=(
                "12 months of monthly Google Ads search volume per keyword. "
                "No historical Amazon pricing or sales history."
            ),
            pricing=(
                "Pay-as-you-go per API call (indicative: ~$0.075 per "
                "search-volume request for up to 1000 keywords, ~$0.004 per "
                "10 SERP results). Verify current prices at "
                "https://dataforseo.com/pricing."
            ),
            refresh_frequency=(
                "On demand — every research job calls the live API. Google "
                "Ads data itself refreshes monthly."
            ),
            blind_spots=[
                "No confirmed sales/transaction data — search volume measures "
                "interest, not purchases.",
                "No wholesale/supplier pricing, MOQs, or lead times.",
                "No historical Amazon pricing.",
                "Amazon review counts are social proof, not sales velocity.",
                "Keyword difficulty is a competition proxy, not a demand signal.",
            ],
        )

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def is_configured(self, settings: Any) -> bool:
        return bool(getattr(settings, "DATAFORSEO_LOGIN", "") and getattr(
            settings, "DATAFORSEO_PASSWORD", ""
        ))

    def missing_config_hint(self, settings: Any) -> str:
        return (
            "credentials not configured — save them in Settings → "
            "Market Intel (encrypted vault) or set DATAFORSEO_LOGIN and "
            "DATAFORSEO_PASSWORD in the API environment (pay-as-you-go API "
            "access at https://dataforseo.com/). No live market data was "
            "retrieved."
        )

    def _require_configured(self, settings: Any) -> None:
        if not self.is_configured(settings):
            raise ConnectorNotConfigured(
                "DataForSEO credentials are not configured. Save them in "
                "Settings → Market Intel (encrypted vault) or set "
                "DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD in the API "
                "environment (get API access at https://dataforseo.com/). "
                "No live market data was retrieved."
            )

    def _client(self, settings: Any) -> httpx.Client:
        return httpx.Client(
            auth=(
                settings.DATAFORSEO_LOGIN,
                settings.DATAFORSEO_PASSWORD,
            ),
            timeout=httpx.Timeout(30.0),
        )

    def _geo(self, settings: Any) -> dict[str, Any]:
        return {
            "location_code": int(
                getattr(settings, "DATAFORSEO_LOCATION_CODE", 2840) or 2840
            ),
            "language_code": getattr(settings, "DATAFORSEO_LANGUAGE_CODE", "en")
            or "en",
        }

    # ------------------------------------------------------------------
    # HTTP plumbing (mirrors the ProductScout provider)
    # ------------------------------------------------------------------

    def _post(
        self, client: httpx.Client, path: str, payload: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        try:
            resp = client.post(API_BASE + path, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ConnectorError(
                f"DataForSEO HTTP {exc.response.status_code} on {path}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ConnectorError(f"DataForSEO request failed on {path}: {exc}") from exc

        body = resp.json()
        code = body.get("status_code")
        if code == _STATUS_AUTH_FAILED:
            raise ConnectorAuthError(
                f"DataForSEO authentication failed ({body.get('status_message')}). "
                "Verify DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD and account "
                "verification at app.dataforseo.com."
            )
        if code == _STATUS_NO_CREDITS:
            raise ConnectorAuthError(
                "DataForSEO balance exhausted — top up at "
                "https://app.dataforseo.com/."
            )
        if code != _STATUS_OK:
            raise ConnectorError(
                f"DataForSEO error {code}: {body.get('status_message')}"
            )

        tasks = body.get("tasks") or []
        if not tasks or tasks[0].get("status_code") not in (_STATUS_OK, 20100):
            raise ConnectorError(
                f"DataForSEO task failed: "
                f"{tasks[0].get('status_code') if tasks else 'no tasks'} "
                f"{tasks[0].get('status_message') if tasks else ''}"
            )
        return tasks[0].get("result") or []

    # ------------------------------------------------------------------
    # Keyword demand
    # ------------------------------------------------------------------

    def keyword_demand(
        self, term: str, *, geo: str, settings: Any
    ) -> KeywordDemand:
        """Google Ads volume/CPC/competition + difficulty for one keyword."""
        self._require_configured(settings)
        t = term.strip()
        if not t:
            raise ConnectorError("empty keyword")
        retrieved = _now_iso()
        geo_params = self._geo(settings)
        signals: list[DemandSignal] = []
        competition: dict[str, Any] = {}
        monthly_trend: list[dict[str, Any]] = []

        with self._client(settings) as client:
            vol_result = self._post(
                client,
                "/keywords_data/google_ads/search_volume/live",
                [{"keywords": [t], **geo_params}],
            )
            item = (vol_result[0] if vol_result else {}) or {}
            volume = _int(item.get("search_volume"))
            cpc = round(_num(item.get("cpc")), 2)
            competition_index = item.get("competition_index")

            monthly = item.get("monthly_searches") or []
            monthly_sorted = sorted(
                monthly, key=lambda m: (m.get("year", 0), m.get("month", 0))
            )[-12:]
            for m in monthly_sorted:
                monthly_trend.append(
                    {
                        "year": m.get("year"),
                        "month": m.get("month"),
                        "volume": _int(m.get("search_volume")),
                    }
                )

            # Search interest — explicitly NOT sales.
            signals.append(
                DemandSignal(
                    kind="search_interest",
                    value=float(volume),
                    unit="searches/month",
                    source="dataforseo",
                    period="latest-month",
                    retrieved_at=retrieved,
                    geo=geo,
                    confidence=0.85,
                    note=(
                        "Google Ads estimated monthly search volume. Measures "
                        "search interest only — it is not units sold, revenue, "
                        "or conversion."
                    ),
                )
            )

            competition["cpc_usd"] = cpc
            competition["cpc_note"] = (
                "Advertiser competition proxy: higher CPC usually means more "
                "sellers bidding, not more buyers buying."
            )
            if competition_index is not None:
                competition["ads_competition_index"] = _num(competition_index)

            # Difficulty (competition proxy, best-effort).
            difficulty: float | None = None
            try:
                d_result = self._post(
                    client,
                    "/dataforseo_labs/google/bulk_keyword_difficulty/live",
                    [{"keywords": [t], **geo_params}],
                )
                if d_result:
                    difficulty = _num((d_result[0] or {}).get("keyword_difficulty"))
            except ConnectorError as exc:
                log.warning("dataforseo: difficulty lookup failed: %s", exc)
            if difficulty:
                competition["keyword_difficulty"] = round(difficulty, 1)
                competition["keyword_difficulty_note"] = (
                    "0-100 index of how hard it is to rank organically; a "
                    "competition proxy, not a demand signal."
                )

        return KeywordDemand(
            term=t, signals=signals, competition=competition,
            monthly_trend=monthly_trend,
        )

    # ------------------------------------------------------------------
    # Product search (Amazon via Merchant API)
    # ------------------------------------------------------------------

    @staticmethod
    def _weak_listing_ratio(items: list[dict[str, Any]]) -> float:
        """Heuristic: fraction of results with thin social proof.

        Listings with <100 ratings or <4.0 stars are typically beatable with
        a better listing. Documented as a heuristic, not measured data.
        """
        if not items:
            return 0.0
        weak = sum(
            1
            for it in items
            if _int((it.get("rating") or {}).get("votes_count")) < 100
            or (0 < _num((it.get("rating") or {}).get("value")) < 4.0)
        )
        return round(weak / len(items), 2)

    def search_products(
        self,
        query: str,
        *,
        category: str,
        limit: int,
        geo: str,
        settings: Any,
    ) -> list[ProductCandidate]:
        self._require_configured(settings)
        q = query.strip()
        if not q:
            return []
        retrieved = _now_iso()
        geo_params = self._geo(settings)

        with self._client(settings) as client:
            result = self._post(
                client,
                "/merchant/amazon/products/live/advanced",
                [
                    {
                        "keyword": q,
                        "depth": min(max(limit, 10), 50),
                        **geo_params,
                    }
                ],
            )
        items = [
            i
            for i in ((result[0] or {}).get("items") or [])
            if isinstance(i, dict) and i.get("type") in ("amazon_product", None)
        ]
        weak_ratio = self._weak_listing_ratio(items)
        prices = [
            _num((it.get("price") or {}).get("current"))
            for it in items
            if _num((it.get("price") or {}).get("current")) > 0
        ]
        prices_sorted = sorted(prices)

        candidates: list[ProductCandidate] = []
        for it in items[:limit]:
            title = (it.get("title") or "").strip()
            if not title:
                continue
            asin = it.get("asin") or it.get("data_asin") or ""
            rating = it.get("rating") or {}
            price = it.get("price") or {}
            price_val = _num(price.get("current"))
            price_evidence = (
                [
                    PricePoint(
                        price=round(price_val, 2),
                        currency=str(price.get("currency") or "USD"),
                        source="dataforseo:amazon",
                        observed_at=retrieved,
                        kind="advertised",
                    )
                ]
                if price_val > 0
                else []
            )
            review_count = _int(rating.get("votes_count"))
            candidates.append(
                ProductCandidate(
                    name=title[:480],
                    category=category
                    or str(it.get("department") or ""),
                    identifiers={
                        "asin": asin,
                        "candidate_id": (
                            f"asin-{asin}"
                            if asin
                            else "amz-"
                            + hashlib.sha256(title.encode()).hexdigest()[:12]
                        ),
                    },
                    price_evidence=price_evidence,
                    demand_signals=[
                        DemandSignal(
                            kind="marketplace_indicator",
                            value=float(review_count),
                            unit="reviews",
                            source="dataforseo:amazon",
                            period="current",
                            retrieved_at=retrieved,
                            geo=geo,
                            confidence=0.7,
                            note=(
                                "Amazon review count is social proof / listing "
                                "age proxy — not sales velocity."
                            ),
                        )
                    ],
                    competition={
                        "listing_count": len(items),
                        "listing_count_note": (
                            "Number of Amazon results returned — a supply-side "
                            "measure, not demand."
                        ),
                        "weak_listing_ratio": weak_ratio,
                        "weak_listing_ratio_note": (
                            "Heuristic: fraction of results with <100 reviews "
                            "or <4.0 stars; higher = easier to out-list."
                        ),
                        "price_min": round(prices_sorted[0], 2)
                        if prices_sorted
                        else None,
                        "price_median": round(
                            prices_sorted[len(prices_sorted) // 2], 2
                        )
                        if prices_sorted
                        else None,
                        "price_max": round(prices_sorted[-1], 2)
                        if prices_sorted
                        else None,
                        "avg_rating": round(
                            sum(
                                _num((i.get("rating") or {}).get("value"))
                                for i in items
                            )
                            / max(len(items), 1),
                            2,
                        ),
                    },
                    source_urls=(
                        [f"https://www.amazon.com/dp/{asin}"] if asin else []
                    ),
                    retrieved_at=retrieved,
                    connector="dataforseo",
                )
            )
        return candidates

    # ------------------------------------------------------------------
    # Organic web search (SERP) — live web results for Draven research
    # ------------------------------------------------------------------

    async def serp_search(
        self, settings: Any, query: str, num_results: int = 10
    ) -> list[dict[str, Any]]:
        """Live Google organic results via DataForSEO SERP API.

        Returns [{title, url, description}] — untrusted third-party content;
        callers must treat it as evidence, never verified fact.
        Raises ConnectorNotConfigured / ConnectorError on failure.
        """
        self._require_configured(settings)
        geo = self._geo(settings)
        payload = [
            {
                "keyword": query[:200],
                "location_code": geo["location_code"],
                "language_code": geo["language_code"],
                "depth": min(max(num_results, 10), 100),
            }
        ]
        auth = (
            settings.DATAFORSEO_LOGIN,
            settings.DATAFORSEO_PASSWORD,
        )
        try:
            async with httpx.AsyncClient(auth=auth, timeout=httpx.Timeout(30.0)) as client:
                resp = await client.post(
                    API_BASE + "/serp/google/organic/live", json=payload
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ConnectorError(
                f"DataForSEO SERP HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ConnectorError(f"DataForSEO SERP request failed: {exc}") from exc

        code = body.get("status_code")
        if code == _STATUS_AUTH_FAILED:
            raise ConnectorAuthError("DataForSEO authentication failed.")
        if code == _STATUS_NO_CREDITS:
            raise ConnectorAuthError(
                "DataForSEO balance exhausted — top up at https://app.dataforseo.com/."
            )
        if code != _STATUS_OK:
            raise ConnectorError(
                f"DataForSEO SERP error {code}: {body.get('status_message')}"
            )
        tasks = body.get("tasks") or []
        if not tasks or tasks[0].get("status_code") not in (_STATUS_OK, 20100):
            raise ConnectorError("DataForSEO SERP task failed.")
        results = (tasks[0].get("result") or [{}])[0].get("items") or []
        out: list[dict[str, Any]] = []
        for item in results:
            if item.get("type") != "organic":
                continue
            out.append(
                {
                    "title": item.get("title") or "",
                    "url": item.get("url") or "",
                    "description": item.get("description") or "",
                }
            )
            if len(out) >= num_results:
                break
        return out
