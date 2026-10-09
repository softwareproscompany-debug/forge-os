"""Viator affiliate integration tests: TestClient + SQLite, httpx mocked.

Covers:
  connector headers (exp-api-key, Accept version, Accept-Language) and
  product normalization — never hits the live Viator API;
  viator.product_search / product_import / viator.generate_ads tools;
  keyword routing; tenant isolation of imported programs.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")
_TEST_FERNET_KEY = Fernet.generate_key().decode()
os.environ.setdefault("DRAVEN_CONFIG_KEY", _TEST_FERNET_KEY)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forge_db.models import (
    AffiliateLink,
    AffiliateProgram,
    Asset,
    AssetKind,
    AssetStatus,
    Base,
    Business,
    ComplianceIssue,
    User,
    UserRole,
)

from app.core.config import Settings
from app.core.deps import get_db_session, get_settings_dep
from app.main import create_app

TEST_DB = "/tmp/forgeos_viator_test.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

engine = create_engine(f"sqlite:///{TEST_DB}", connect_args={"check_same_thread": False})
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)

test_settings = Settings(
    DATABASE_URL=f"sqlite:///{TEST_DB}",
    REDIS_URL="redis://127.0.0.1:9/0",
    JWT_SECRET="test-secret",
    WEBHOOK_SECRET="test-webhook-secret",
    DRAVEN_CONFIG_KEY=_TEST_FERNET_KEY,
)

app = create_app()
app.dependency_overrides[get_settings_dep] = lambda: test_settings


def _override_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db_session] = _override_db

client = TestClient(app)


def _make_user(db, email_prefix="viator") -> User:
    business = Business(
        id=uuid.uuid4(),
        name=f"{email_prefix} biz",
        slug=f"{email_prefix}-{uuid.uuid4().hex[:8]}",
    )
    db.add(business)
    db.flush()
    user = User(
        id=uuid.uuid4(),
        business_id=business.id,
        email=f"{email_prefix}-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
        full_name="Viator Owner",
        role=UserRole.owner,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture()
def db():
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def user(db):
    return _make_user(db)


# ---------------------------------------------------------------------------
# Fixture payloads (shaped like the real Viator Partner API)
# ---------------------------------------------------------------------------

SEARCH_RESPONSE = {
    "products": [
        {
            "productCode": "12345P1",
            "title": "Sunset Food Tour of Rome",
            "description": "Taste your way through Trastevere at golden hour.",
            "pricing": {
                "currency": "USD",
                "summary": {"fromPrice": 89.0, "fromPriceBeforeDiscount": 99.0},
            },
            "reviews": {"totalReviews": 1204, "combinedAverageRating": 4.8},
            "productUrl": "https://www.viator.com/tours/Rome/x?pid=P00209028",
            "images": [
                {
                    "isCover": True,
                    "imageSource": "https://example.com/cover.jpg",
                    "variants": [
                        {"width": 100, "height": 100, "url": "https://example.com/s.jpg"},
                        {"width": 800, "height": 600, "url": "https://example.com/l.jpg"},
                    ],
                }
            ],
            "destinations": [{"ref": "77", "primary": True}],
        },
        {
            "productCode": "67890P2",
            "title": "Colosseum Skip-the-Line Entry",
            "description": "Walk straight in, no queues.",
            "pricing": {"currency": "USD", "summary": {"fromPrice": 54.5}},
            "reviews": {"totalReviews": 300, "combinedAverageRating": 4.5},
            "productUrl": "https://www.viator.com/tours/Rome/y?pid=P00209028",
            "images": [],
            "destinations": [{"ref": "77", "primary": True}],
        },
    ],
    "totalCount": 2,
}


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _mock_client(post_resp=None, get_resp=None):
    """Build an AsyncMock httpx.AsyncClient capturing call kwargs."""
    captured = {}

    async def fake_post(url, **kwargs):
        captured["post_url"] = url
        captured["post_kwargs"] = kwargs
        return post_resp or _FakeResp()

    async def fake_get(url, **kwargs):
        captured["get_url"] = url
        captured["get_kwargs"] = kwargs
        return get_resp or _FakeResp()

    mock = AsyncMock()
    mock.post.side_effect = fake_post
    mock.get.side_effect = fake_get
    mock.__aenter__.return_value = mock
    mock.__aexit__.return_value = False
    return mock, captured


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


def test_connector_sends_required_headers():
    from app.affiliate.connectors.viator import ViatorConnector

    mock, captured = _mock_client(post_resp=_FakeResp(200, SEARCH_RESPONSE))
    with patch("httpx.AsyncClient", return_value=mock):
        import asyncio

        products = asyncio.run(
            ViatorConnector(api_key="test-key-123").search_products(77, count=10)
        )
    headers = captured["post_kwargs"]["headers"]
    assert headers["exp-api-key"] == "test-key-123"
    assert headers["Accept"] == "application/json;version=2.0"
    assert headers["Accept-Language"] == "en-US"
    # No Authorization header — Viator uses exp-api-key.
    assert "Authorization" not in headers
    body = captured["post_kwargs"]["json"]
    assert body["filtering"] == {"destination": "77"}
    assert body["pagination"] == {"start": 1, "count": 10}
    assert body["currency"] == "USD"
    assert len(products) == 2


def test_connector_normalizes_products():
    from app.affiliate.connectors.viator import normalize_product

    p = normalize_product(SEARCH_RESPONSE["products"][0])
    assert p["productCode"] == "12345P1"
    assert p["title"] == "Sunset Food Tour of Rome"
    assert p["price"] == 89.0
    assert p["price_before_discount"] == 99.0
    assert p["currency"] == "USD"
    assert p["rating"] == 4.8
    assert p["review_count"] == 1204
    assert p["productUrl"].startswith("https://www.viator.com/")
    # Largest variant wins.
    assert p["thumbnail"] == "https://example.com/l.jpg"

    p2 = normalize_product(SEARCH_RESPONSE["products"][1])
    assert p2["thumbnail"] is None  # no images → None, not a crash


def test_connector_auth_error():
    from app.affiliate.connectors.viator import ViatorAuthError, ViatorConnector

    mock, _ = _mock_client(post_resp=_FakeResp(401, {}))
    with patch("httpx.AsyncClient", return_value=mock):
        import asyncio

        with pytest.raises(ViatorAuthError):
            asyncio.run(ViatorConnector(api_key="bad-key").search_products(77))


def test_connector_requires_key():
    from app.affiliate.connectors.viator import ViatorConnector, ViatorNotConfigured

    with pytest.raises(ViatorNotConfigured):
        ViatorConnector(api_key="")


def test_get_product_detail():
    from app.affiliate.connectors.viator import ViatorConnector

    detail = dict(SEARCH_RESPONSE["products"][0])
    mock, captured = _mock_client(get_resp=_FakeResp(200, detail))
    with patch("httpx.AsyncClient", return_value=mock):
        import asyncio

        p = asyncio.run(ViatorConnector(api_key="k").get_product("12345P1"))
    assert captured["get_url"].endswith("/products/12345P1")
    assert p["productCode"] == "12345P1"
    assert p["rating"] == 4.8


# ---------------------------------------------------------------------------
# Tools (execute directly with a db session + user)
# ---------------------------------------------------------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _seed_vault_key(db, business_id):
    """Store a dummy Viator key in the vault (encrypted at rest).

    Uses the same key resolution as the tool under test
    (``get_settings().DRAVEN_CONFIG_KEY``) so encryption/decryption agree
    even when other test modules set the env var first.
    """
    from app.core.config import get_settings
    from app.settings_vault import service as vault

    ck = getattr(get_settings(), "DRAVEN_CONFIG_KEY", "") or None
    vault.put_secret(
        db,
        business_id,
        "viator.affiliate_api_key",
        "vault-test-key",
        actor="test",
        config_key=ck,
    )
    db.commit()


def test_product_search_tool(db, user):
    from app.draven_tools import TOOLS, ViatorProductSearchInput

    _seed_vault_key(db, user.business_id)
    mock, _ = _mock_client(post_resp=_FakeResp(200, SEARCH_RESPONSE))
    with patch("httpx.AsyncClient", return_value=mock):
        result = _run(
            TOOLS["viator.product_search"].execute(
                db, user, ViatorProductSearchInput(destination_id=77, count=5)
            )
        )
    assert result["status"] == "ok"
    assert result["output"]["count"] == 2
    assert result["output"]["products"][0]["productCode"] == "12345P1"


def test_product_search_tool_not_configured(db, user):
    from app.draven_tools import TOOLS, ViatorProductSearchInput

    result = _run(
        TOOLS["viator.product_search"].execute(
            db, user, ViatorProductSearchInput(destination_id=77)
        )
    )
    assert result["status"] == "error"
    assert "not configured" in result["error"].lower()


def test_product_import_tool_creates_programs_and_links(db, user):
    from app.draven_tools import TOOLS, ViatorProductImportInput

    _seed_vault_key(db, user.business_id)
    mock, _ = _mock_client(post_resp=_FakeResp(200, SEARCH_RESPONSE))
    with patch("httpx.AsyncClient", return_value=mock):
        result = _run(
            TOOLS["viator.product_import"].execute(
                db, user, ViatorProductImportInput(destination_id=77)
            )
        )
    assert result["status"] == "ok"
    assert result["output"]["programs_new"] == 2

    programs = (
        db.query(AffiliateProgram)
        .filter(
            AffiliateProgram.business_id == user.business_id,
            AffiliateProgram.network == "viator",
        )
        .all()
    )
    assert len(programs) == 2
    links = (
        db.query(AffiliateLink)
        .filter(AffiliateLink.business_id == user.business_id)
        .all()
    )
    assert len(links) == 2
    assert links[0].slug.startswith("viator-")
    assert links[0].destination_url.startswith("https://www.viator.com/")

    # Idempotent re-import: no duplicates.
    with patch("httpx.AsyncClient", return_value=mock):
        result2 = _run(
            TOOLS["viator.product_import"].execute(
                db, user, ViatorProductImportInput(destination_id=77)
            )
        )
    assert result2["output"]["programs_new"] == 0
    assert (
        db.query(AffiliateProgram)
        .filter(
            AffiliateProgram.business_id == user.business_id,
            AffiliateProgram.network == "viator",
        )
        .count()
        == 2
    )


def test_generate_ads_tool_creates_draft_assets(db, user):
    from app.draven_tools import TOOLS, ViatorGenerateAdsInput, ViatorProductImportInput

    _seed_vault_key(db, user.business_id)
    mock, _ = _mock_client(post_resp=_FakeResp(200, SEARCH_RESPONSE))
    with patch("httpx.AsyncClient", return_value=mock):
        _run(
            TOOLS["viator.product_import"].execute(
                db, user, ViatorProductImportInput(destination_id=77)
            )
        )
    result = _run(
        TOOLS["viator.generate_ads"].execute(db, user, ViatorGenerateAdsInput())
    )
    assert result["status"] == "ok"
    assert result["output"]["ads_created"] == 2

    assets = (
        db.query(Asset)
        .filter(
            Asset.business_id == user.business_id,
            Asset.kind == AssetKind.ad,
        )
        .all()
    )
    assert len(assets) == 2
    for a in assets:
        # Draft-only, never auto-published.
        assert a.status == AssetStatus.draft
        assert a.is_affiliate_content is True
        # FTC disclosure present.
        assert "#ad" in (a.body or "")


def test_import_tenant_isolation(db, user):
    """Business A's import is invisible to business B."""
    from app.draven_tools import TOOLS, ViatorProductImportInput

    other = _make_user(db, email_prefix="viator-b")
    _seed_vault_key(db, user.business_id)
    mock, _ = _mock_client(post_resp=_FakeResp(200, SEARCH_RESPONSE))
    with patch("httpx.AsyncClient", return_value=mock):
        _run(
            TOOLS["viator.product_import"].execute(
                db, user, ViatorProductImportInput(destination_id=77)
            )
        )
    assert (
        db.query(AffiliateProgram)
        .filter(AffiliateProgram.business_id == other.business_id)
        .count()
        == 0
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_viator_routing():
    from app.draven_tools import route_intent

    assert ("viator.product_search", {"destination_id": 77}) == next(
        (t, i) for t, i in route_intent("find viator tours in Rome")
        if t == "viator.product_search"
    )
    routed = route_intent("import viator products for destination 77")
    assert any(
        t == "viator.product_import" and i.get("destination_id") == 77
        for t, i in routed
    )
    routed = route_intent("create ads for viator products")
    assert any(t == "viator.generate_ads" for t, i in routed)
    # Viator wins over the generic contact search.
    routed = route_intent("find viator tours in Rome")
    assert not any(t == "draven.contacts_search" for t, i in routed)
