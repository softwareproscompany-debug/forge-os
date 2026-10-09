"""Brave Search connector tests: headers, parsing, errors, provider selection.

Covers:
  - Required headers (X-Subscription-Token, Cache-Control: no-cache)
  - Response parsing to normalized {title, url, description}
  - 401 → ConnectorAuthError, 429 → ConnectorError
  - Not configured → ConnectorNotConfigured (honest, never faked)
  - Provider selection: Brave preferred, DataForSEO fallback, honest
    degradation when neither is configured
  - Vault registry contains brave.api_key
  - _test_brave connection verifier

Never hits the real Brave API — httpx is fully mocked.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

import pytest

from app.market_intel.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorNotConfigured,
)
from app.market_intel.connectors.brave import BraveConnector
from app.market_intel.connectors import registry as connector_registry


# ---------------------------------------------------------------------------
# Fake HTTP layer
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"fake {self.status_code}",
                request=None,  # type: ignore[arg-type]
                response=self,  # type: ignore[arg-type]
            )


class _FakeAsyncClient:
    """Records headers/params; returns a canned response."""

    response = _FakeResp(200, {})
    seen: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        _FakeAsyncClient.seen.append(
            {"url": url, "params": kw.get("params"), "headers": kw.get("headers")}
        )
        return _FakeAsyncClient.response


def _patch_brave_http(monkeypatch):
    _FakeAsyncClient.seen = []
    monkeypatch.setattr(
        "app.market_intel.connectors.brave.httpx.AsyncClient", _FakeAsyncClient
    )


def _settings(api_key: str | None = "test-brave-key"):
    return SimpleNamespace(BRAVE_API_KEY=api_key or "")


_BRAVE_OK = {
    "web": {
        "results": [
            {
                "title": "Example Result",
                "url": "https://example.com/article",
                "description": "A snippet about the topic.",
            },
            {
                "title": "Second Result",
                "url": "https://example.org/other",
                "description": "Another snippet.",
            },
        ]
    }
}


# ---------------------------------------------------------------------------
# Connector: headers + parsing
# ---------------------------------------------------------------------------


def test_brave_sends_required_headers(monkeypatch):
    """X-Subscription-Token auth + Cache-Control: no-cache (Brave 422s without it)."""
    _patch_brave_http(monkeypatch)
    _FakeAsyncClient.response = _FakeResp(200, _BRAVE_OK)
    connector = BraveConnector()
    asyncio.run(connector.serp_search(_settings("secret-key-123"), "test query"))
    assert _FakeAsyncClient.seen, "no HTTP call was made"
    call = _FakeAsyncClient.seen[0]
    assert call["url"] == "https://api.search.brave.com/res/v1/web/search"
    headers = call["headers"] or {}
    assert headers.get("X-Subscription-Token") == "secret-key-123"
    assert headers.get("Cache-Control") == "no-cache"
    assert headers.get("Accept") == "application/json"
    # The key must NOT appear in the URL or params.
    assert "secret-key-123" not in call["url"]
    assert "secret-key-123" not in str(call["params"])


def test_brave_parses_results(monkeypatch):
    _patch_brave_http(monkeypatch)
    _FakeAsyncClient.response = _FakeResp(200, _BRAVE_OK)
    connector = BraveConnector()
    results = asyncio.run(connector.serp_search(_settings(), "test query", num_results=5))
    assert len(results) == 2
    assert results[0]["title"] == "Example Result"
    assert results[0]["url"] == "https://example.com/article"
    assert results[0]["description"] == "A snippet about the topic."
    assert set(results[0].keys()) == {"title", "url", "description"}


def test_brave_respects_num_results(monkeypatch):
    _patch_brave_http(monkeypatch)
    _FakeAsyncClient.response = _FakeResp(200, _BRAVE_OK)
    connector = BraveConnector()
    results = asyncio.run(connector.serp_search(_settings(), "q", num_results=1))
    assert len(results) == 1


def test_brave_401_is_auth_error(monkeypatch):
    _patch_brave_http(monkeypatch)
    _FakeAsyncClient.response = _FakeResp(401, {"error": "unauthorized"})
    connector = BraveConnector()
    with pytest.raises(ConnectorAuthError):
        asyncio.run(connector.serp_search(_settings(), "q"))


def test_brave_429_is_rate_limit_error(monkeypatch):
    _patch_brave_http(monkeypatch)
    _FakeAsyncClient.response = _FakeResp(429, {"error": "too many requests"})
    connector = BraveConnector()
    with pytest.raises(ConnectorError, match="rate limit"):
        asyncio.run(connector.serp_search(_settings(), "q"))


def test_brave_not_configured_is_honest():
    connector = BraveConnector()
    assert not connector.is_configured(_settings(None))
    with pytest.raises(ConnectorNotConfigured, match="not configured"):
        asyncio.run(connector.serp_search(_settings(None), "q"))


def test_brave_empty_query_rejected(monkeypatch):
    _patch_brave_http(monkeypatch)
    connector = BraveConnector()
    with pytest.raises(ConnectorError, match="empty query"):
        asyncio.run(connector.serp_search(_settings(), "   "))


def test_brave_capability_manifest():
    cap = BraveConnector().capability
    assert cap.connector == "brave"
    assert "2,000" in cap.pricing
    assert cap.blind_spots, "blind spots must be documented"


def test_brave_registered_in_connector_registry():
    assert connector_registry.get("brave") is not None
    assert connector_registry.get("brave").name == "brave"


def test_brave_unsupported_methods_are_explicit():
    connector = BraveConnector()
    with pytest.raises(ConnectorError, match="does not provide keyword demand"):
        connector.keyword_demand("x", geo="US", settings=_settings())
    with pytest.raises(ConnectorError, match="does not provide structured product"):
        connector.search_products("x", category="", limit=5, geo="US", settings=_settings())


# ---------------------------------------------------------------------------
# Vault registry
# ---------------------------------------------------------------------------


def test_brave_api_key_in_vault_registry():
    from app.settings_vault.service import validate_key_name

    spec = validate_key_name("brave.api_key")
    assert spec is not None
    assert spec.section == "market_intel"
    assert "Brave" in spec.label


# ---------------------------------------------------------------------------
# Provider selection: Brave preferred, DataForSEO fallback
# ---------------------------------------------------------------------------


def _make_user_and_db(monkeypatch, tmp_path):
    """Minimal in-memory business/user for tool-level tests."""
    import uuid

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from forge_db.models import Base, Business, User

    db_path = tmp_path / "brave_provider_test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)
    db = TestingSession()
    business = Business(id=uuid.uuid4(), name="Brave Test Co", slug=f"brave-test-{uuid.uuid4().hex[:8]}")
    db.add(business)
    db.commit()
    user = User(
        id=uuid.uuid4(),
        business_id=business.id,
        email="brave-test@example.com",
        password_hash="test-hash",
        full_name="Brave Tester",
        role="owner",
    )
    db.add(user)
    db.commit()
    return db, user


def test_provider_selection_prefers_brave(monkeypatch, tmp_path):
    """When Brave is configured, it is used — DataForSEO is not consulted."""
    from app import draven_tools

    db, user = _make_user_and_db(monkeypatch, tmp_path)
    try:
        brave_calls: list = []
        dataforseo_calls: list = []

        async def fake_brave_serp(self, settings, query, num_results=10, country="US"):
            brave_calls.append(query)
            return [{"title": "Brave Hit", "url": "https://b.example/", "description": "d"}]

        async def fake_dfs_serp(self, settings, query, num_results=10):
            dataforseo_calls.append(query)
            return [{"title": "DFS Hit", "url": "https://d.example/", "description": "d"}]

        monkeypatch.setattr(
            "app.market_intel.connectors.brave.BraveConnector.serp_search",
            fake_brave_serp,
        )
        monkeypatch.setattr(
            "app.market_intel.connectors.dataforseo.DataForSEOConnector.serp_search",
            fake_dfs_serp,
        )
        # Brave configured via env fallback; DataForSEO absent.
        monkeypatch.setenv("BRAVE_API_KEY", "test-key")
        monkeypatch.delenv("DATAFORSEO_LOGIN", raising=False)
        monkeypatch.delenv("DATAFORSEO_PASSWORD", raising=False)

        results, provider = asyncio.run(
            draven_tools._serp_search_preferred(db, user.business_id, "test query", 5)
        )
        assert provider == "brave"
        assert brave_calls == ["test query"]
        assert dataforseo_calls == [], "DataForSEO must not be called when Brave works"
        assert results[0]["title"] == "Brave Hit"
    finally:
        db.close()


def test_provider_selection_falls_back_to_dataforseo(monkeypatch, tmp_path):
    """When Brave is NOT configured but DataForSEO is, DataForSEO is used."""
    from app import draven_tools
    from app.market_intel.connectors.base import ConnectorNotConfigured

    db, user = _make_user_and_db(monkeypatch, tmp_path)
    try:
        async def fake_brave_serp(self, settings, query, num_results=10, country="US"):
            raise ConnectorNotConfigured("brave not configured")

        async def fake_dfs_serp(self, settings, query, num_results=10):
            return [{"title": "DFS Hit", "url": "https://d.example/", "description": "d"}]

        monkeypatch.setattr(
            "app.market_intel.connectors.brave.BraveConnector.serp_search",
            fake_brave_serp,
        )
        monkeypatch.setattr(
            "app.market_intel.connectors.dataforseo.DataForSEOConnector.serp_search",
            fake_dfs_serp,
        )

        results, provider = asyncio.run(
            draven_tools._serp_search_preferred(db, user.business_id, "test query", 5)
        )
        assert provider == "dataforseo"
        assert results[0]["title"] == "DFS Hit"
    finally:
        db.close()


def test_provider_selection_honest_when_neither_configured(monkeypatch, tmp_path):
    """Neither provider → ConnectorNotConfigured (the tool reports honestly)."""
    from app import draven_tools
    from app.market_intel.connectors.base import ConnectorNotConfigured

    db, user = _make_user_and_db(monkeypatch, tmp_path)
    try:
        async def fake_brave_serp(self, settings, query, num_results=10, country="US"):
            raise ConnectorNotConfigured("brave not configured")

        async def fake_dfs_serp(self, settings, query, num_results=10):
            raise ConnectorNotConfigured("dataforseo not configured")

        monkeypatch.setattr(
            "app.market_intel.connectors.brave.BraveConnector.serp_search",
            fake_brave_serp,
        )
        monkeypatch.setattr(
            "app.market_intel.connectors.dataforseo.DataForSEOConnector.serp_search",
            fake_dfs_serp,
        )

        with pytest.raises(ConnectorNotConfigured):
            asyncio.run(
                draven_tools._serp_search_preferred(db, user.business_id, "q", 5)
            )
    finally:
        db.close()


def test_web_search_tool_reports_provider(monkeypatch, tmp_path):
    """The draven.web_search tool result names the provider used."""
    from app import draven_tools

    db, user = _make_user_and_db(monkeypatch, tmp_path)
    try:
        async def fake_brave_serp(self, settings, query, num_results=10, country="US"):
            return [{"title": "T", "url": "https://x.example/", "description": "d"}]

        monkeypatch.setattr(
            "app.market_intel.connectors.brave.BraveConnector.serp_search",
            fake_brave_serp,
        )
        monkeypatch.setenv("BRAVE_API_KEY", "test-key")

        inp = draven_tools.WebSearchToolInput(query="test query")
        result = asyncio.run(draven_tools._web_search(db, user, inp))
        assert result["status"] == "ok"
        assert result["output"]["provider"] == "brave"
    finally:
        db.close()


def test_web_search_tool_honest_when_unconfigured(monkeypatch, tmp_path):
    """No providers → the tool says so honestly, naming both options."""
    from app import draven_tools
    from app.market_intel.connectors.base import ConnectorNotConfigured

    db, user = _make_user_and_db(monkeypatch, tmp_path)
    try:
        async def fake_brave_serp(self, settings, query, num_results=10, country="US"):
            raise ConnectorNotConfigured("brave not configured")

        async def fake_dfs_serp(self, settings, query, num_results=10):
            raise ConnectorNotConfigured("dataforseo not configured")

        monkeypatch.setattr(
            "app.market_intel.connectors.brave.BraveConnector.serp_search",
            fake_brave_serp,
        )
        monkeypatch.setattr(
            "app.market_intel.connectors.dataforseo.DataForSEOConnector.serp_search",
            fake_dfs_serp,
        )
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)

        inp = draven_tools.WebSearchToolInput(query="test query")
        result = asyncio.run(draven_tools._web_search(db, user, inp))
        assert result["status"] == "error"
        assert "not configured" in result["error"].lower()
        assert "Brave" in result["error"]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# _test_brave connection verifier
# ---------------------------------------------------------------------------


def test_brave_connection_verifier_ok(monkeypatch):
    import asyncio

    from app.routers import settings as settings_router

    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            assert kw["headers"]["X-Subscription-Token"] == "k"
            assert kw["headers"]["Cache-Control"] == "no-cache"
            return FakeResp()

    monkeypatch.setattr("app.routers.settings.httpx.AsyncClient", FakeClient)
    ok, detail = asyncio.run(settings_router._test_brave("k"))
    assert ok is True
    assert "valid" in detail


def test_brave_connection_verifier_401(monkeypatch):
    import asyncio

    from app.routers import settings as settings_router

    class FakeResp:
        status_code = 401

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return FakeResp()

    monkeypatch.setattr("app.routers.settings.httpx.AsyncClient", FakeClient)
    ok, detail = asyncio.run(settings_router._test_brave("bad-key"))
    assert ok is False
    assert "401" in detail
