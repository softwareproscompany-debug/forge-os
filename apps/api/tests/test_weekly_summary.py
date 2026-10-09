"""Tests for GET /api/v1/analytics/weekly-summary (Card 5).

Covers, via TestClient + SQLite (models are dialect-portable):

* 404 {"detail": "no weekly summary yet"} when the worker job has not cut
  one yet
* 200 with the latest summary (ordered by week_start desc) when several
  weeks exist
* tenant isolation: another business's summaries are never returned
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forge_db.models import Base, WeeklySummary

from app.core.config import Settings
from app.core.deps import get_db_session, get_settings_dep
from app.main import create_app

TEST_DB = "/tmp/forgeos_weekly_summary_test.db"
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


def _register(email_prefix: str) -> dict:
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
            "business_name": f"Weekly Biz {_counter}",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _add_summary(business_id: uuid.UUID, week_start: date, recommendation: str | None):
    db = TestingSession()
    try:
        db.add(
            WeeklySummary(
                business_id=business_id,
                week_start=week_start,
                top_assets=[
                    {
                        "asset_id": str(uuid.uuid4()),
                        "title": "Launch email",
                        "kind": "email_copy",
                        "delivered": 10,
                        "converted": 9,
                        "conversion_rate": 0.9,
                    }
                ],
                bottom_assets=[],
                best_channel_per_segment={
                    "vip": {"channel": "email", "conversion_rate": 0.75, "delivered": 4}
                },
                recommendation=recommendation,
            )
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def user_a() -> dict:
    return _register("weeklyalice")


@pytest.fixture()
def user_b() -> dict:
    return _register("weeklybob")


# ---------------------------------------------------------------------------
# GET /analytics/weekly-summary
# ---------------------------------------------------------------------------


def test_weekly_summary_404_when_none(user_a):
    r = client.get("/api/v1/analytics/weekly-summary", headers=_headers(user_a["token"]))
    assert r.status_code == 404
    assert r.json() == {"detail": "no weekly summary yet"}


def test_weekly_summary_returns_latest(user_a):
    business_id = uuid.UUID(user_a["user"]["business_id"])
    _add_summary(business_id, date(2026, 9, 28), "Older week: do more email.")
    _add_summary(business_id, date(2026, 10, 5), "Newer week: do more SMS.")

    r = client.get("/api/v1/analytics/weekly-summary", headers=_headers(user_a["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["week_start"] == "2026-10-05"
    assert body["recommendation"] == "Newer week: do more SMS."
    assert body["top_assets"] == [
        {
            "asset_id": body["top_assets"][0]["asset_id"],
            "title": "Launch email",
            "kind": "email_copy",
            "delivered": 10,
            "converted": 9,
            "conversion_rate": 0.9,
        }
    ]
    assert body["bottom_assets"] == []
    assert body["best_channel_per_segment"] == {
        "vip": {"channel": "email", "conversion_rate": 0.75, "delivered": 4}
    }
    assert body["created_at"]  # timestamp present


def test_weekly_summary_tenant_isolation(user_a, user_b):
    business_b = uuid.UUID(user_b["user"]["business_id"])
    _add_summary(business_b, date(2026, 10, 5), "Bob's secret evidence.")

    # user_a has no summaries of their own in this test run's slice... but
    # other tests in this file may have added rows for other businesses; the
    # point is a's response must never contain b's recommendation.
    r = client.get("/api/v1/analytics/weekly-summary", headers=_headers(user_a["token"]))
    if r.status_code == 200:
        assert r.json()["recommendation"] != "Bob's secret evidence."

    r = client.get("/api/v1/analytics/weekly-summary", headers=_headers(user_b["token"]))
    assert r.status_code == 200
    assert r.json()["recommendation"] == "Bob's secret evidence."


def test_weekly_summary_requires_auth():
    r = client.get("/api/v1/analytics/weekly-summary")
    assert r.status_code in (401, 403)
