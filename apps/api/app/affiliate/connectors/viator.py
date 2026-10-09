"""Viator Partner API connector — affiliate product search and import.

Wraps ``https://api.viator.com/partner`` (v2). Auth is the partner API key
sent in the ``exp-api-key`` header (NOT ``Authorization: Bearer``). Every
request also carries ``Accept: application/json;version=2.0`` and
``Accept-Language``.

Endpoints used:
* ``POST /products/search`` — search tours/activities by destination.
  Body: filtering.destination, sorting, pagination, currency.
  Response: ``{products: [ProductSummary...], totalCount}``.
* ``GET /products/{product-code}`` — full product detail (affiliate
  ``productUrl`` included).

The API key is read per-business from the encrypted settings vault
(``viator.affiliate_api_key``); it is never logged or returned. All
network I/O is async (httpx). Tests must mock httpx — never hit the live
API.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.viator.com/partner"
_API_VERSION = "application/json;version=2.0"

#: Hard cap per request — the API rejects counts above its own limit.
_MAX_COUNT = 50


class ViatorError(Exception):
    """A Viator API call failed (network, HTTP error, or bad payload)."""


class ViatorNotConfigured(ViatorError):
    """No Viator affiliate API key is configured for this business."""


class ViatorAuthError(ViatorError):
    """The Viator API rejected the key (401/403)."""


def _headers(api_key: str) -> dict[str, str]:
    return {
        "exp-api-key": api_key,
        "Accept": _API_VERSION,
        "Accept-Language": "en-US",
        "Content-Type": "application/json",
    }


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick_thumbnail(images: list[dict[str, Any]]) -> str | None:
    """Prefer the cover image's largest variant URL."""
    if not images:
        return None
    cover = next((i for i in images if i.get("isCover")), images[0])
    variants = cover.get("variants") or []
    if variants:
        best = max(variants, key=lambda v: (v.get("width") or 0) * (v.get("height") or 0))
        if best.get("url"):
            return str(best["url"])
    return cover.get("imageSource")


def normalize_product(raw: dict[str, Any]) -> dict[str, Any]:
    """Map a Viator ProductSummary to ForgeOS's flat product shape."""
    pricing = raw.get("pricing") or {}
    summary = pricing.get("summary") or {}
    reviews = raw.get("reviews") or {}
    return {
        "productCode": raw.get("productCode"),
        "title": raw.get("title") or "",
        "description": (raw.get("description") or "")[:500],
        "price": _num(summary.get("fromPrice")),
        "price_before_discount": _num(summary.get("fromPriceBeforeDiscount")),
        "currency": pricing.get("currency") or summary.get("currency"),
        "rating": _num(reviews.get("combinedAverageRating")),
        "review_count": reviews.get("totalReviews"),
        "productUrl": raw.get("productUrl"),
        "thumbnail": _pick_thumbnail(raw.get("images") or []),
        "destinations": [
            d.get("ref") for d in (raw.get("destinations") or []) if d.get("ref")
        ],
    }


class ViatorConnector:
    """Async Viator Partner API client for one business."""

    name = "viator"

    def __init__(self, api_key: str, timeout_s: float = 30.0):
        if not api_key:
            raise ViatorNotConfigured(
                "Viator affiliate API key is not configured. Save it in "
                "Settings → Affiliates (encrypted vault) as "
                "viator.affiliate_api_key."
            )
        self._api_key = api_key
        self._timeout = httpx.Timeout(timeout_s)

    async def search_products(
        self,
        destination_id: int,
        count: int = 25,
        currency: str = "USD",
    ) -> list[dict[str, Any]]:
        """Search products for a destination, best-rated first.

        Returns normalized product dicts (see ``normalize_product``).
        """
        count = max(1, min(int(count), _MAX_COUNT))
        payload = {
            "filtering": {"destination": str(destination_id)},
            "sorting": {"sort": "TRAVELER_RATING", "order": "DESCENDING"},
            "pagination": {"start": 1, "count": count},
            "currency": currency,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    API_BASE + "/products/search",
                    headers=_headers(self._api_key),
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise ViatorError(f"Viator product search request failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ViatorAuthError(
                "Viator rejected the API key (HTTP "
                f"{resp.status_code}). Check Settings → Affiliates."
            )
        if resp.status_code >= 400:
            raise ViatorError(f"Viator product search HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ViatorError("Viator returned non-JSON response") from exc
        products = body.get("products") or []
        return [normalize_product(p) for p in products if p.get("productCode")]

    async def freetext_search(
        self,
        keyword: str,
        count: int = 25,
        currency: str = "USD",
    ) -> list[dict[str, Any]]:
        """Freetext product search (no destination ID needed).

        Falls back for destinations not in the affiliate's coverage
        (e.g. Japan). Returns normalized product dicts.
        """
        kw = (keyword or "").strip()
        if not kw:
            raise ViatorError("keyword is required for freetext search")
        count = max(1, min(int(count), _MAX_COUNT))
        payload = {
            "searchTerm": kw,
            "searchTypes": [
                {"searchType": "PRODUCTS", "pagination": {"start": 1, "count": count}}
            ],
            "currency": currency,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    API_BASE + "/search/freetext",
                    headers=_headers(self._api_key),
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise ViatorError(f"Viator freetext search request failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ViatorAuthError(
                "Viator rejected the API key (HTTP "
                f"{resp.status_code}). Check Settings → Affiliates."
            )
        if resp.status_code >= 400:
            raise ViatorError(f"Viator freetext search HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ViatorError("Viator returned non-JSON response") from exc
        products = body.get("products") or []
        return [normalize_product(p) for p in products if p.get("productCode")]

    async def get_product(self, product_code: str) -> dict[str, Any]:
        """Full detail for one product code (affiliate productUrl included)."""
        code = (product_code or "").strip()
        if not code:
            raise ViatorError("product_code is required")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{API_BASE}/products/{code}",
                    headers=_headers(self._api_key),
                )
        except httpx.HTTPError as exc:
            raise ViatorError(f"Viator product detail request failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ViatorAuthError(
                "Viator rejected the API key (HTTP "
                f"{resp.status_code}). Check Settings → Affiliates."
            )
        if resp.status_code == 404:
            raise ViatorError(f"Viator product not found: {code}")
        if resp.status_code >= 400:
            raise ViatorError(f"Viator product detail HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ViatorError("Viator returned non-JSON response") from exc
        return normalize_product(body)


def connector_for_business(db: Any, business_id: Any, settings: Any) -> ViatorConnector:
    """Build a connector with the business's vault key (vault-first).

    Raises ``ViatorNotConfigured`` when no key is stored.
    """
    from app.settings_vault import service as vault

    ck = getattr(settings, "DRAVEN_CONFIG_KEY", "") or None
    key = vault.get_secret(db, business_id, "viator.affiliate_api_key", ck)
    if not key:
        raise ViatorNotConfigured(
            "Viator affiliate API key is not configured. Save it in "
            "Settings → Affiliates (encrypted vault) as "
            "viator.affiliate_api_key."
        )
    return ViatorConnector(api_key=key)
