"""API smoke tests: TestClient + SQLite (models are dialect-portable).

Covers the contract's critical paths:
  register -> login -> me
  brand kit CRUD
  asset approval state machine (incl. illegal-transition 422s)
  template preview rendering
  contact consent
  campaign launch gating on approved assets
  analytics overview math
  webhook secret check
  tenant isolation
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
    Base,
    Channel,
    Contact,
    GenerationLog,
    Send,
    SendStatus,
)

from app.core.config import Settings
from app.core.deps import get_db_session, get_settings_dep
from app.main import create_app

TEST_DB = "/tmp/forgeos_api_test.db"
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


_counter = 0


def _register(email_prefix: str, business_name: str) -> dict:
    """Register a fresh business+owner (unique email per call)."""
    global _counter
    _counter += 1
    email = f"{email_prefix}{_counter}@example.com"
    r = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "s3cret-pass",
            "full_name": email_prefix.title(),
            "business_name": f"{business_name} {_counter}",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert "token" in body and "user" in body
    assert body["user"]["role"] == "owner"
    assert "password_hash" not in body["user"]  # never leak hashes
    return {"email": email, "token": body["token"], "user": body["user"]}


@pytest.fixture()
def user_a() -> dict:
    return _register("alice", "Acme Corp")


@pytest.fixture()
def user_b() -> dict:
    return _register("bob", "Beta LLC")


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_register_login_me(user_a):
    token = user_a["token"]
    r = client.post(
        "/api/v1/auth/login",
        data={"username": user_a["email"], "password": "s3cret-pass"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["token_type"] == "bearer"

    r = client.get("/api/v1/auth/me", headers=_headers(token))
    assert r.status_code == 200
    assert r.json()["email"] == user_a["email"]

    # bad password
    r = client.post(
        "/api/v1/auth/login",
        data={"username": user_a["email"], "password": "wrong"},
    )
    assert r.status_code == 401

    # duplicate email
    r = client.post(
        "/api/v1/auth/register",
        json={
            "email": user_a["email"],
            "password": "s3cret-pass",
            "full_name": "Alice 2",
            "business_name": "Acme 2",
        },
    )
    assert r.status_code == 400


def test_business_me(user_a):
    r = client.get("/api/v1/businesses/me", headers=_headers(user_a["token"]))
    assert r.status_code == 200
    assert r.json()["name"].startswith("Acme Corp")
    r = client.put(
        "/api/v1/businesses/me",
        json={"timezone": "America/Chicago"},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200
    assert r.json()["timezone"] == "America/Chicago"


# ---------------------------------------------------------------------------
# brand kits
# ---------------------------------------------------------------------------


def test_brand_kit_crud(user_a):
    h = _headers(user_a["token"])
    r = client.post(
        "/api/v1/brand-kits",
        json={"name": "Main", "tone_tags": ["bold"], "primary_color": "#000"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    kit = r.json()
    kit_id = kit["id"]

    r = client.get("/api/v1/brand-kits", headers=h)
    assert r.status_code == 200
    assert r.json()["total"] == 1

    r = client.put(
        f"/api/v1/brand-kits/{kit_id}",
        json={"primary_color": "#fff"},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["primary_color"] == "#fff"
    assert r.json()["version"] == 2  # edits bump the version


# ---------------------------------------------------------------------------
# assets: approval state machine
# ---------------------------------------------------------------------------


def _generate(user) -> dict:
    r = client.post(
        "/api/v1/assets/generate",
        json={"kind": "email_copy", "title": "Welcome email"},
        headers=_headers(user["token"]),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # Redis is down in tests: asset still created, job not queued, never 500.
    assert body["job_id"] is None
    assert body["warning"]
    return body


def test_asset_state_machine(user_a):
    h = _headers(user_a["token"])
    asset_id = _generate(user_a)["asset_id"]

    r = client.get(f"/api/v1/assets/{asset_id}", headers=h)
    assert r.json()["status"] == "draft"

    # illegal: approve straight from draft
    r = client.post(f"/api/v1/assets/{asset_id}/approve", json={}, headers=h)
    assert r.status_code == 422

    r = client.post(f"/api/v1/assets/{asset_id}/submit", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "in_review"

    r = client.post(
        f"/api/v1/assets/{asset_id}/approve", json={"note": "looks good"}, headers=h
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved"
    assert body["approved_by"] == user_a["user"]["id"]

    # illegal: reject an approved asset
    r = client.post(
        f"/api/v1/assets/{asset_id}/reject", json={"reason": "nope"}, headers=h
    )
    assert r.status_code == 422

    # versions endpoint includes the asset itself
    r = client.get(f"/api/v1/assets/{asset_id}/versions", headers=h)
    assert r.status_code == 200
    assert r.json()["total"] >= 1

    # filtering
    r = client.get("/api/v1/assets?status=approved", headers=h)
    assert r.json()["total"] >= 1
    r = client.get("/api/v1/assets?status=draft", headers=h)
    assert all(i["status"] == "draft" for i in r.json()["items"])


def test_asset_reject_and_rework(user_a):
    h = _headers(user_a["token"])
    asset_id = _generate(user_a)["asset_id"]
    client.post(f"/api/v1/assets/{asset_id}/submit", headers=h)
    r = client.post(
        f"/api/v1/assets/{asset_id}/reject", json={"reason": "off-brand"}, headers=h
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert r.json()["rejection_reason"] == "off-brand"

    # rework: rejected -> draft via submit
    r = client.post(f"/api/v1/assets/{asset_id}/submit", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "draft"


def test_tenant_isolation(user_a, user_b):
    asset_id = _generate(user_a)["asset_id"]
    r = client.get(
        f"/api/v1/assets/{asset_id}", headers=_headers(user_b["token"])
    )
    assert r.status_code == 404  # invisible across tenants, not 403
    r = client.get("/api/v1/assets", headers=_headers(user_b["token"]))
    assert r.json()["total"] == 0


# ---------------------------------------------------------------------------
# templates + preview
# ---------------------------------------------------------------------------


def test_template_preview(user_a):
    h = _headers(user_a["token"])
    r = client.post(
        "/api/v1/templates",
        json={
            "name": "Welcome",
            "channel": "email",
            "subject_template": "Hi {{ name }}",
            "body_template": "Hello {{ name }}, welcome to {{ brand }}!",
            "variables": ["name", "brand"],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    r = client.post(
        f"/api/v1/templates/{tid}/preview",
        json={"variables": {"name": "Ada", "brand": "Acme"}},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["subject"] == "Hi Ada"
    assert r.json()["body"] == "Hello Ada, welcome to Acme!"

    # missing variable -> 422
    r = client.post(
        f"/api/v1/templates/{tid}/preview", json={"variables": {}}, headers=h
    )
    assert r.status_code == 422

    # invalid Jinja2 rejected at creation
    r = client.post(
        "/api/v1/templates",
        json={"name": "Bad", "channel": "email", "body_template": "Hello {{ "},
        headers=h,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# contacts + consent
# ---------------------------------------------------------------------------


def test_contact_consent(user_a):
    h = _headers(user_a["token"])
    r = client.post(
        "/api/v1/contacts",
        json={"email": "lead@example.com", "first_name": "Lead"},
        headers=h,
    )
    assert r.status_code == 201
    cid = r.json()["id"]
    assert r.json()["consent_email"] is False

    r = client.post(
        f"/api/v1/contacts/{cid}/consent",
        json={"channel": "email", "granted": True},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["consent_email"] is True
    assert body["consent_email_at"] is not None

    r = client.post(
        f"/api/v1/contacts/{cid}/consent",
        json={"channel": "email", "granted": False},
        headers=h,
    )
    assert r.json()["consent_email"] is False

    r = client.get("/api/v1/contacts", headers=h)
    assert r.json()["total"] == 1


# ---------------------------------------------------------------------------
# campaigns: launch gating
# ---------------------------------------------------------------------------


def _approved_asset_id(user) -> str:
    asset_id = _generate(user)["asset_id"]
    h = _headers(user["token"])
    client.post(f"/api/v1/assets/{asset_id}/submit", headers=h)
    client.post(f"/api/v1/assets/{asset_id}/approve", json={}, headers=h)
    return asset_id


def test_campaign_launch_requires_approved_assets(user_a):
    h = _headers(user_a["token"])
    draft_asset = _generate(user_a)["asset_id"]

    r = client.post(
        "/api/v1/campaigns", json={"name": "Drip"}, headers=h
    )
    assert r.status_code == 201
    cid = r.json()["id"]

    r = client.post(
        f"/api/v1/campaigns/{cid}/steps",
        json={"steps": [{"channel": "email", "asset_id": draft_asset}]},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["items"][0]["position"] == 0

    # draft asset -> launch rejected
    r = client.post(f"/api/v1/campaigns/{cid}/launch", headers=h)
    assert r.status_code == 422

    # approve the asset, point the step at it, launch succeeds
    client.post(f"/api/v1/assets/{draft_asset}/submit", headers=h)
    client.post(f"/api/v1/assets/{draft_asset}/approve", json={}, headers=h)
    r = client.post(f"/api/v1/campaigns/{cid}/launch", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"

    r = client.post(f"/api/v1/campaigns/{cid}/pause", headers=h)
    assert r.json()["status"] == "paused"

    r = client.get(f"/api/v1/campaigns/{cid}/enrollments", headers=h)
    assert r.json()["total"] == 0


def test_autopilot_settings(user_a):
    h = _headers(user_a["token"])
    r = client.get("/api/v1/autopilot", headers=h)
    assert r.status_code == 200
    assert r.json()["auto_approve"] is False
    assert r.json()["daily_send_cap"] == 500

    r = client.put(
        "/api/v1/autopilot",
        json={"auto_approve": True, "daily_send_cap": 100},
        headers=h,
    )
    assert r.json()["auto_approve"] is True
    assert r.json()["daily_send_cap"] == 100


# ---------------------------------------------------------------------------
# analytics
# ---------------------------------------------------------------------------


def _seed_sends(business_id: str, campaign_id: str, step_id: str, contact_id: str):
    db = TestingSession()
    now = datetime.now(timezone.utc)
    rows = [
        # delivered + opened + clicked
        Send(
            business_id=uuid.UUID(business_id),
            campaign_id=uuid.UUID(campaign_id),
            step_id=uuid.UUID(step_id),
            contact_id=uuid.UUID(contact_id),
            channel=Channel.email,
            to_address="a@example.com",
            subject="s",
            body="b",
            status=SendStatus.delivered,
            opened_at=now,
            clicked_at=now,
        ),
        # delivered + opened
        Send(
            business_id=uuid.UUID(business_id),
            campaign_id=uuid.UUID(campaign_id),
            step_id=uuid.UUID(step_id),
            contact_id=uuid.UUID(contact_id),
            channel=Channel.email,
            to_address="b@example.com",
            subject="s",
            body="b",
            status=SendStatus.delivered,
            opened_at=now,
        ),
        # bounced
        Send(
            business_id=uuid.UUID(business_id),
            campaign_id=uuid.UUID(campaign_id),
            step_id=uuid.UUID(step_id),
            contact_id=uuid.UUID(contact_id),
            channel=Channel.email,
            to_address="c@example.com",
            subject="s",
            body="b",
            status=SendStatus.bounced,
        ),
        # converted
        Send(
            business_id=uuid.UUID(business_id),
            campaign_id=uuid.UUID(campaign_id),
            step_id=uuid.UUID(step_id),
            contact_id=uuid.UUID(contact_id),
            channel=Channel.email,
            to_address="d@example.com",
            subject="s",
            body="b",
            status=SendStatus.delivered,
            opened_at=now,
            clicked_at=now,
            converted_at=now,
        ),
    ]
    for row in rows:
        db.add(row)
    db.commit()
    db.close()


def test_analytics_overview_and_funnel(user_a):
    h = _headers(user_a["token"])
    biz = user_a["user"]["business_id"]

    # campaign + step + contact fixtures via the API
    r = client.post("/api/v1/campaigns", json={"name": "Analytics"}, headers=h)
    cid = r.json()["id"]
    r = client.post(
        f"/api/v1/campaigns/{cid}/steps",
        json={"steps": [{"channel": "email"}]},
        headers=h,
    )
    step_id = r.json()["items"][0]["id"]
    r = client.post(
        "/api/v1/contacts", json={"email": "stat@example.com"}, headers=h
    )
    contact_id = r.json()["id"]

    _seed_sends(biz, cid, step_id, contact_id)

    db = TestingSession()
    db.add(
        GenerationLog(
            business_id=uuid.UUID(biz),
            asset_id=uuid.uuid4(),
            provider="stub",
            model="stub-1",
            prompt_hash="abc",
            tokens_in=10,
            tokens_out=20,
            cost_usd=1.25,
            latency_ms=5,
        )
    )
    db.commit()
    db.close()

    r = client.get("/api/v1/analytics/overview?days=30", headers=h)
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["sent"] == 4
    assert o["delivered"] == 3
    assert o["opened"] == 3
    assert o["clicked"] == 2
    assert o["converted"] == 1
    assert o["open_rate"] == pytest.approx(1.0)
    assert o["ctr"] == pytest.approx(2 / 3)
    assert o["conversion_rate"] == pytest.approx(1 / 3)
    assert o["spend_usd"] == pytest.approx(1.25)
    assert len(o["by_day"]) == 1
    assert o["by_day"][0]["sent"] == 4

    r = client.get(f"/api/v1/analytics/campaigns/{cid}/funnel", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["step_id"] == step_id
    assert items[0]["position"] == 0
    assert items[0]["sent"] == 4
    assert items[0]["opened"] == 3
    assert items[0]["clicked"] == 2


# ---------------------------------------------------------------------------
# webhooks + events + outbox
# ---------------------------------------------------------------------------


def test_delivery_webhook(user_a):
    h = _headers(user_a["token"])
    db = TestingSession()
    contact = Contact(business_id=uuid.UUID(user_a["user"]["business_id"]), email="w@example.com")
    db.add(contact)
    db.flush()
    send = Send(
        business_id=contact.business_id,
        contact_id=contact.id,
        channel=Channel.email,
        to_address="w@example.com",
        subject="s",
        body="b",
        status=SendStatus.sent,
        provider_message_id="pm_123",
    )
    db.add(send)
    db.commit()
    db.close()

    # wrong secret
    r = client.post(
        "/api/v1/webhooks/delivery",
        json={"provider_message_id": "pm_123", "event": "opened"},
        headers={"X-Webhook-Secret": "wrong"},
    )
    assert r.status_code == 401

    r = client.post(
        "/api/v1/webhooks/delivery",
        json={"provider_message_id": "pm_123", "event": "opened"},
        headers={"X-Webhook-Secret": "test-webhook-secret"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True

    db = TestingSession()
    got = db.query(Send).filter(Send.provider_message_id == "pm_123").one()
    assert got.opened_at is not None
    assert got.status == SendStatus.delivered
    db.close()


def test_events_and_outbox(user_a):
    h = _headers(user_a["token"])
    r = client.post(
        "/api/v1/events",
        json={"kind": "contact_added", "payload": {"source": "test"}},
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["job_id"] is None  # redis down: recorded anyway
    assert body["warning"]

    r = client.get("/api/v1/dev/outbox?limit=50", headers=h)
    assert r.status_code == 200
    assert r.json()["total"] == 0
    assert r.json()["items"] == []


def test_openapi_has_all_routes():
    # NOTE: FastAPI >= 0.118 materializes include_router() lazily; the
    # OpenAPI schema forces full route materialization.
    spec = app.openapi()
    paths = set()
    for path, methods in spec["paths"].items():
        for method in methods:
            paths.add(f"{method.upper()} {path}")
    expected = [
        "POST /api/v1/auth/register",
        "POST /api/v1/auth/login",
        "GET /api/v1/auth/me",
        "GET /api/v1/businesses/me",
        "PUT /api/v1/businesses/me",
        "GET /api/v1/brand-kits",
        "POST /api/v1/brand-kits",
        "GET /api/v1/brand-kits/{kit_id}",
        "PUT /api/v1/brand-kits/{kit_id}",
        "GET /api/v1/contacts",
        "POST /api/v1/contacts",
        "GET /api/v1/contacts/{contact_id}",
        "PUT /api/v1/contacts/{contact_id}",
        "DELETE /api/v1/contacts/{contact_id}",
        "POST /api/v1/contacts/{contact_id}/consent",
        "GET /api/v1/templates",
        "POST /api/v1/templates",
        "GET /api/v1/templates/{template_id}",
        "PUT /api/v1/templates/{template_id}",
        "DELETE /api/v1/templates/{template_id}",
        "POST /api/v1/templates/{template_id}/preview",
        "POST /api/v1/assets/generate",
        "GET /api/v1/assets",
        "GET /api/v1/assets/{asset_id}",
        "POST /api/v1/assets/{asset_id}/submit",
        "POST /api/v1/assets/{asset_id}/approve",
        "POST /api/v1/assets/{asset_id}/reject",
        "GET /api/v1/assets/{asset_id}/versions",
        "GET /api/v1/campaigns",
        "POST /api/v1/campaigns",
        "GET /api/v1/campaigns/{campaign_id}",
        "PUT /api/v1/campaigns/{campaign_id}",
        "POST /api/v1/campaigns/{campaign_id}/steps",
        "PUT /api/v1/campaigns/{campaign_id}/steps/{step_id}",
        "POST /api/v1/campaigns/{campaign_id}/launch",
        "POST /api/v1/campaigns/{campaign_id}/pause",
        "GET /api/v1/campaigns/{campaign_id}/enrollments",
        "GET /api/v1/autopilot",
        "PUT /api/v1/autopilot",
        "GET /api/v1/analytics/overview",
        "GET /api/v1/analytics/campaigns/{campaign_id}/funnel",
        "GET /api/v1/dev/outbox",
        "POST /api/v1/webhooks/delivery",
        "POST /api/v1/events",
    ]
    missing = [e for e in expected if e not in paths]
    assert not missing, f"missing routes: {missing}"
