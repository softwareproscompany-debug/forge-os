"""Affiliate module tests: TestClient + SQLite (models are dialect-portable).

Covers programs/links CRUD, tenant isolation, the public /r/{slug}
redirect (302 + click event, no auth), conversion ingest with commission
math, earnings aggregation from events, and the asset affiliate flag.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forge_db.models import (
    AffiliateLink,
    AffiliateProgram,
    Asset,
    AssetKind,
    Base,
    Business,
    Event,
    User,
)

from app.core.config import Settings
from app.core.deps import get_db_session, get_settings_dep
from app.main import create_app

TEST_DB = "/tmp/forgeos_affiliates_test.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

engine = create_engine(f"sqlite:///{TEST_DB}", connect_args={"check_same_thread": False})
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)

test_settings = Settings(
    DATABASE_URL=f"sqlite:///{TEST_DB}",
    REDIS_URL="redis://127.0.0.1:9/0",  # closed port -> enqueue fails fast
    JWT_SECRET="test-secret",
    WEBHOOK_SECRET="test-webhook-secret",
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


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(email_prefix: str, business_name: str) -> dict:
    email = f"{email_prefix}-{uuid.uuid4().hex[:8]}@example.com"
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "password123",
            "full_name": "Aff Owner",
            "business_name": business_name,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture()
def owner() -> dict:
    return _register("aff", "Affiliate Biz")


@pytest.fixture()
def auth(owner: dict) -> dict[str, str]:
    return _headers(owner["token"])


@pytest.fixture()
def program(owner: dict, auth: dict[str, str]) -> dict:
    resp = client.post(
        "/api/v1/affiliates/programs",
        headers=auth,
        json={"name": "Demo Gear Picks", "network": "amazon",
              "default_commission_pct": "4.000"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture()
def link(owner: dict, auth: dict[str, str], program: dict) -> dict:
    # Unique slug per test: the public /r/{slug} redirect resolves by slug
    # alone (cross-business), so fixtures must not collide.
    slug = f"espresso-maker-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/affiliates/links",
        headers=auth,
        json={
            "program_id": program["id"],
            "label": "Espresso maker",
            "slug": slug,
            "destination_url": "https://www.amazon.com/dp/B000TEST?tag=mytag-20",
            "utm_campaign": "spring",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Programs CRUD
# ---------------------------------------------------------------------------


def test_program_crud(auth: dict[str, str]):
    created = client.post(
        "/api/v1/affiliates/programs",
        headers=auth,
        json={"name": "P1", "network": "shareasale",
              "default_commission_pct": "10.5", "cookie_days": 30},
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    assert created.json()["network"] == "shareasale"

    listed = client.get("/api/v1/affiliates/programs", headers=auth)
    assert listed.status_code == 200
    assert any(p["id"] == pid for p in listed.json()["items"])

    patched = client.patch(
        f"/api/v1/affiliates/programs/{pid}",
        headers=auth,
        json={"status": "paused"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "paused"

    bad = client.patch(
        f"/api/v1/affiliates/programs/{pid}",
        headers=auth,
        json={"status": "bogus"},
    )
    assert bad.status_code == 422

    deleted = client.delete(f"/api/v1/affiliates/programs/{pid}", headers=auth)
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/affiliates/programs/{pid}", headers=auth).status_code == 404


def test_program_requires_auth():
    assert client.get("/api/v1/affiliates/programs").status_code in (401, 403)


def test_program_tenant_isolation(auth: dict[str, str], program: dict):
    other = _register("aff2", "Other Biz")
    other_auth = _headers(other["token"])
    # Other tenant cannot see, fetch, or delete this program.
    assert client.get("/api/v1/affiliates/programs", headers=other_auth).json()["items"] == []
    assert client.get(f"/api/v1/affiliates/programs/{program['id']}", headers=other_auth).status_code == 404
    assert client.delete(f"/api/v1/affiliates/programs/{program['id']}", headers=other_auth).status_code == 404


# ---------------------------------------------------------------------------
# Links CRUD + validation
# ---------------------------------------------------------------------------


def test_link_slug_validation(auth: dict[str, str], program: dict):
    base = {
        "program_id": program["id"],
        "label": "L",
        "destination_url": "https://example.com/x?tag=1",
    }
    for bad_slug in ["UPPER", "has space", "under_score", "-leading", "trailing-"]:
        resp = client.post(
            "/api/v1/affiliates/links", headers=auth,
            json={**base, "slug": bad_slug},
        )
        assert resp.status_code == 422, bad_slug

    resp = client.post(
        "/api/v1/affiliates/links", headers=auth,
        json={**base, "slug": "ok-slug-1"},
    )
    assert resp.status_code == 201, resp.text

    dup = client.post(
        "/api/v1/affiliates/links", headers=auth,
        json={**base, "slug": "ok-slug-1"},
    )
    assert dup.status_code == 409

    bad_url = client.post(
        "/api/v1/affiliates/links", headers=auth,
        json={**base, "slug": "other-slug", "destination_url": "not-a-url"},
    )
    assert bad_url.status_code == 422

    bad_program = client.post(
        "/api/v1/affiliates/links", headers=auth,
        json={**base, "slug": "third-slug", "program_id": str(uuid.uuid4())},
    )
    assert bad_program.status_code == 404


def test_link_update_slug_conflict(auth: dict[str, str], program: dict):
    a = client.post(
        "/api/v1/affiliates/links", headers=auth,
        json={"program_id": program["id"], "label": "A", "slug": "slug-a",
              "destination_url": "https://example.com/a"},
    ).json()
    b = client.post(
        "/api/v1/affiliates/links", headers=auth,
        json={"program_id": program["id"], "label": "B", "slug": "slug-b",
              "destination_url": "https://example.com/b"},
    ).json()
    resp = client.patch(
        f"/api/v1/affiliates/links/{b['id']}", headers=auth, json={"slug": "slug-a"}
    )
    assert resp.status_code == 409
    # Same slug on self is fine.
    ok = client.patch(
        f"/api/v1/affiliates/links/{a['id']}", headers=auth, json={"slug": "slug-a"}
    )
    assert ok.status_code == 200


# ---------------------------------------------------------------------------
# Public redirect
# ---------------------------------------------------------------------------


def test_redirect_records_click_and_302s(auth: dict[str, str], link: dict, owner: dict):
    resp = client.get(f"/r/{link['slug']}", follow_redirects=False)
    assert resp.status_code == 302, resp.status_code
    location = resp.headers["location"]
    assert location.startswith("https://www.amazon.com/dp/B000TEST")
    assert "utm_source=forgeos" in location
    assert "utm_medium=affiliate" in location
    assert "utm_campaign=spring" in location
    assert "tag=mytag-20" in location  # merchant params preserved

    db = TestingSession()
    try:
        events = (
            db.query(Event)
            .filter(
                Event.business_id == uuid.UUID(owner["user"]["business_id"]),
                Event.kind == "affiliate_clicked",
            )
            .all()
        )
        assert len(events) == 1
        assert events[0].contact_id is None
        assert events[0].payload["link_id"] == link["id"]
        assert events[0].payload["program_id"] == link["program_id"]
    finally:
        db.close()


def test_redirect_404s_without_leaking(auth: dict[str, str], link: dict):
    assert client.get("/r/no-such-slug", follow_redirects=False).status_code == 404

    # Deactivated link -> 404.
    client.patch(f"/api/v1/affiliates/links/{link['id']}", headers=auth,
                 json={"is_active": False})
    assert client.get(f"/r/{link['slug']}", follow_redirects=False).status_code == 404
    client.patch(f"/api/v1/affiliates/links/{link['id']}", headers=auth,
                 json={"is_active": True})

    # Paused program -> 404.
    client.patch(f"/api/v1/affiliates/programs/{link['program_id']}", headers=auth,
                 json={"status": "paused"})
    assert client.get(f"/r/{link['slug']}", follow_redirects=False).status_code == 404


def test_redirect_needs_no_auth(link: dict):
    resp = client.get(f"/r/{link['slug']}", follow_redirects=False)
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Conversions + earnings
# ---------------------------------------------------------------------------


def test_conversion_commission_math(auth: dict[str, str], link: dict, program: dict):
    # Default: order value x program rate (4%).
    resp = client.post(
        "/api/v1/affiliates/conversions",
        headers=auth,
        json={"link_slug": link["slug"], "order_value_usd": "100.00"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["commission_usd"] == pytest.approx(4.0)

    # Explicit commission honored.
    resp2 = client.post(
        "/api/v1/affiliates/conversions",
        headers=auth,
        json={"link_slug": link["slug"], "order_value_usd": "100.00",
              "commission_usd": "7.50"},
    )
    assert resp2.status_code == 201
    assert resp2.json()["commission_usd"] == pytest.approx(7.5)

    # Unknown slug -> 404.
    resp3 = client.post(
        "/api/v1/affiliates/conversions",
        headers=auth,
        json={"link_slug": "nope", "order_value_usd": "10.00"},
    )
    assert resp3.status_code == 404


def test_earnings_aggregation(auth: dict[str, str], link: dict):
    # 3 clicks + 1 conversion ($100 @ 4% = $4).
    for _ in range(3):
        client.get(f"/r/{link['slug']}", follow_redirects=False)
    client.post(
        "/api/v1/affiliates/conversions",
        headers=auth,
        json={"link_slug": link["slug"], "order_value_usd": "100.00"},
    )
    resp = client.get("/api/v1/affiliates/earnings?days=30", headers=auth)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["days"] == 30
    assert data["totals"]["clicks"] == 3
    assert data["totals"]["conversions"] == 1
    assert data["totals"]["conversion_rate"] == pytest.approx(1 / 3, rel=1e-3)
    assert data["totals"]["earnings_usd"] == pytest.approx(4.0)
    assert len(data["per_link"]) == 1
    assert data["per_link"][0]["label"] == "Espresso maker"
    assert len(data["per_program"]) == 1
    assert data["per_program"][0]["program_name"] == "Demo Gear Picks"


def test_earnings_empty_honestly(auth: dict[str, str]):
    resp = client.get("/api/v1/affiliates/earnings", headers=auth)
    assert resp.status_code == 200
    totals = resp.json()["totals"]
    assert totals == {"clicks": 0, "conversions": 0, "conversion_rate": 0.0,
                      "earnings_usd": 0.0}


# ---------------------------------------------------------------------------
# Asset affiliate flag
# ---------------------------------------------------------------------------


def test_asset_affiliate_flag_on_generate_and_patch(auth: dict[str, str], owner: dict):
    created = client.post(
        "/api/v1/assets/generate",
        headers=auth,
        json={"kind": "email_copy", "title": "Aff review",
              "is_affiliate_content": True},
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["asset_id"]

    fetched = client.get(f"/api/v1/assets/{asset_id}", headers=auth)
    assert fetched.status_code == 200
    assert fetched.json()["is_affiliate_content"] is True

    patched = client.patch(
        f"/api/v1/assets/{asset_id}", headers=auth,
        json={"is_affiliate_content": False},
    )
    assert patched.status_code == 200
    assert patched.json()["is_affiliate_content"] is False

    # Default is false.
    created2 = client.post(
        "/api/v1/assets/generate",
        headers=auth,
        json={"kind": "email_copy", "title": "Plain"},
    )
    asset2 = client.get(f"/api/v1/assets/{created2.json()['asset_id']}", headers=auth)
    assert asset2.json()["is_affiliate_content"] is False
