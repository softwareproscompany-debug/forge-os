"""Ops endpoint tests: TestClient + SQLite (models are dialect-portable).

Covers the read-only mission-control aggregate:
  shape of GET /ops/activity (as_of, stages, counters, activity)
  counts reflect created data (brand kit, draft asset, send log rows)
  tenant isolation, unauthenticated rejection
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
    Asset,
    AssetKind,
    AssetStatus,
    Base,
    BrandKit,
    Business,
    Contact,
    Event,
    GenerationLog,
    Send,
    SendStatus,
    User,
)

from app.core.config import Settings
from app.core.deps import get_db_session, get_settings_dep
from app.main import create_app

TEST_DB = "/tmp/forgeos_ops_test.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

engine = create_engine(f"sqlite:///{TEST_DB}", connect_args={"check_same_thread": False})
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)

test_settings = Settings(
    DATABASE_URL="sqlite:////tmp/forgeos_ops_test.db",
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

_counter = 0


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(email_prefix: str, business_name: str) -> dict:
    global _counter
    _counter += 1
    email = f"ops-{email_prefix}{_counter}@example.com"
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
    return {"email": email, "token": body["token"], "user": body["user"]}


@pytest.fixture()
def user_a() -> dict:
    return _register("alice", "Acme Corp")


@pytest.fixture()
def user_b() -> dict:
    return _register("bob", "Beta LLC")


def _business_id(token: str) -> uuid.UUID:
    r = client.get("/api/v1/businesses/me", headers=_headers(token))
    assert r.status_code == 200
    return uuid.UUID(r.json()["id"])


# ---------------------------------------------------------------------------
# shape
# ---------------------------------------------------------------------------


def test_ops_activity_shape(user_a):
    r = client.get("/api/v1/ops/activity", headers=_headers(user_a["token"]))
    assert r.status_code == 200, r.text
    body = r.json()

    assert "as_of" in body
    # as_of parses as a datetime (the dashboard shows it as the data timestamp)
    datetime.fromisoformat(body["as_of"])

    assert set(body["stages"].keys()) == {
        "foundation",
        "origination",
        "reach",
        "growth",
        "evidence",
    }
    assert set(body["counters"].keys()) == {
        "sends_today",
        "generations_today",
        "in_flight",
    }
    assert isinstance(body["activity"], list)


def test_ops_activity_unauthenticated():
    r = client.get("/api/v1/ops/activity")
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# counts reflect data
# ---------------------------------------------------------------------------


def test_ops_activity_counts(user_a):
    token = user_a["token"]
    h = _headers(token)
    business_id = _business_id(token)

    # one brand kit -> foundation count
    r = client.post("/api/v1/brand-kits", json={"name": "Main"}, headers=h)
    assert r.status_code == 201, r.text

    # one generated asset stays in draft (no Redis -> job not queued)
    r = client.post(
        "/api/v1/assets/generate",
        json={"kind": "email_copy", "title": "Ops probe"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["job_id"] is None  # closed-port Redis: soft failure

    # direct DB rows: one generation log + one queued send + one event
    db = TestingSession()
    try:
        asset = (
            db.query(Asset)
            .filter(Asset.business_id == business_id)
            .order_by(Asset.created_at.desc())
            .first()
        )
        assert asset is not None
        user = db.query(User).filter(User.business_id == business_id).first()
        contact = Contact(business_id=business_id, email="ops@example.com")
        db.add(contact)
        db.flush()
        db.add(
            GenerationLog(
                business_id=business_id,
                asset_id=asset.id,
                provider="stub",
                model="stub",
                prompt_hash="abc",
                tokens_in=10,
                tokens_out=20,
                cost_usd=0,
                latency_ms=1,
            )
        )
        db.add(
            Send(
                business_id=business_id,
                contact_id=contact.id,
                channel="email",
                to_address="ops@example.com",
                subject="Hello",
                body="Hi there",
                status=SendStatus.queued,
            )
        )
        db.add(Event(business_id=business_id, kind="email_opened", payload={}))
        db.commit()
    finally:
        db.close()

    r = client.get("/api/v1/ops/activity", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["stages"]["foundation"]["brand_kits"] == 1
    assert body["stages"]["origination"]["draft"] == 1
    assert body["stages"]["reach"]["in_flight"] == 1
    assert body["counters"]["sends_today"] == 1
    assert body["counters"]["generations_today"] == 1
    assert body["counters"]["in_flight"] == 1

    kinds = {item["kind"] for item in body["activity"]}
    assert kinds == {"generation", "send", "event"}
    # newest-first ordering
    ats = [item["at"] for item in body["activity"]]
    assert ats == sorted(ats, reverse=True)
    # limit respected
    r = client.get("/api/v1/ops/activity?limit=1", headers=h)
    assert len(r.json()["activity"]) == 1


def test_ops_activity_tenant_isolation(user_a, user_b):
    # user_a has data from the previous test's business? No — each _register
    # creates a fresh business, so user_b must see a clean slate.
    r = client.get("/api/v1/ops/activity", headers=_headers(user_b["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stages"]["foundation"]["brand_kits"] == 0
    assert body["counters"]["sends_today"] == 0
    assert body["activity"] == []


def test_ops_activity_limit_validation(user_a):
    h = _headers(user_a["token"])
    r = client.get("/api/v1/ops/activity?limit=0", headers=h)
    assert r.status_code == 422
    r = client.get("/api/v1/ops/activity?limit=500", headers=h)
    assert r.status_code == 422
