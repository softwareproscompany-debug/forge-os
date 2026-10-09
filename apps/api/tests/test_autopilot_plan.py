"""API tests for the Card 4 autopilot plan review flow.

Covers:
  GET  /api/v1/autopilot/plan          -> 404 when none, 200 with items
  POST /api/v1/autopilot/plan/approve  -> creates the scheduled campaign +
                                         steps (delay_hours = day*24),
                                         re-validates asset refs, marks the
                                         plan approved; 422 on double approve;
                                         404 for another tenant's plan
"""

from __future__ import annotations

import os
import uuid
from datetime import date

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from forge_db.models import (  # noqa: E402
    Asset,
    AssetKind,
    AssetStatus,
    Base,
    Campaign,
    CampaignStep,
    ContentPlan,
    PlanStatus,
)

from app.core.config import Settings  # noqa: E402
from app.core.deps import get_db_session, get_settings_dep  # noqa: E402
from app.main import create_app  # noqa: E402

TEST_DB = "/tmp/forgeos_api_autopilot_test.db"
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
    return {"email": email, "token": body["token"], "user": body["user"]}


@pytest.fixture()
def user_a() -> dict:
    return _register("plan-alice", "Plan Acme")


@pytest.fixture()
def user_b() -> dict:
    return _register("plan-bob", "Plan Beta")


WEEK = date(2026, 10, 12)  # a Monday


def _items(email_asset_id: uuid.UUID | None = None) -> list[dict]:
    items = [
        {
            "kind": "email_copy",
            "channel": "email",
            "day": 1,
            "title": f"Autopilot: weekly email — {WEEK.isoformat()}",
            "brief": "Tuesday newsletter.",
        },
        {
            "kind": "social_post",
            "channel": "social",
            "day": 3,
            "title": f"Autopilot: weekly social — {WEEK.isoformat()}",
            "brief": "Thursday social post.",
        },
        {
            "kind": "sms",
            "channel": "sms",
            "day": 5,
            "title": f"Autopilot: weekly SMS — {WEEK.isoformat()}",
            "brief": "Saturday SMS.",
        },
    ]
    if email_asset_id is not None:
        items[0]["asset_id"] = str(email_asset_id)
    return items


def _draft_plan(user: dict, **kw) -> ContentPlan:
    db = TestingSession()
    try:
        plan = ContentPlan(
            business_id=uuid.UUID(user["user"]["business_id"]),
            week_start=kw.get("week_start", WEEK),
            status=kw.get("status", PlanStatus.draft),
            items=kw.get("items", _items()),
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)
        return plan
    finally:
        db.close()


def _asset(user: dict, **kw) -> Asset:
    db = TestingSession()
    try:
        asset = Asset(
            business_id=uuid.UUID(user["user"]["business_id"]),
            kind=kw.get("kind", AssetKind.email_copy),
            title=kw.get("title", "Weekly email"),
            body="Hi there!",
            status=kw.get("status", AssetStatus.approved),
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)
        return asset
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /autopilot/plan
# ---------------------------------------------------------------------------


def test_get_plan_404_when_none(user_a):
    r = client.get("/api/v1/autopilot/plan", headers=_headers(user_a["token"]))
    assert r.status_code == 404, r.text


def test_get_plan_200_returns_latest_with_items(user_a):
    _draft_plan(user_a, week_start=date(2026, 10, 5))
    latest = _draft_plan(user_a, week_start=WEEK)

    r = client.get("/api/v1/autopilot/plan", headers=_headers(user_a["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(latest.id)
    assert body["week_start"] == WEEK.isoformat()
    assert body["status"] == "draft"
    assert body["campaign_id"] is None
    items = body["items"]
    assert len(items) == 3
    assert items[0]["kind"] == "email_copy"
    assert items[0]["day"] == 1
    assert items[0]["asset_id"] is None


def test_get_plan_is_tenant_scoped(user_a, user_b):
    _draft_plan(user_b)
    r = client.get("/api/v1/autopilot/plan", headers=_headers(user_a["token"]))
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# POST /autopilot/plan/approve
# ---------------------------------------------------------------------------


def test_approve_creates_scheduled_campaign_and_steps(user_a):
    asset = _asset(user_a)
    plan = _draft_plan(user_a, items=_items(asset.id))

    r = client.post(
        "/api/v1/autopilot/plan/approve",
        json={"plan_id": str(plan.id)},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()

    campaign_id = body["campaign_id"]
    assert body["plan"]["id"] == str(plan.id)
    assert body["plan"]["status"] == "approved"
    assert body["plan"]["campaign_id"] == campaign_id
    assert body["plan"]["approved_by"] == user_a["user"]["id"]
    assert body["plan"]["approved_at"] is not None

    db = TestingSession()
    try:
        campaign = db.get(Campaign, uuid.UUID(campaign_id))
        assert campaign is not None
        assert campaign.business_id == uuid.UUID(user_a["user"]["business_id"])
        assert campaign.name == "Autopilot — week of 2026-10-12"
        assert campaign.status.value == "scheduled"
        assert campaign.autopilot is True
        assert campaign.created_by == uuid.UUID(user_a["user"]["id"])
        # Monday 09:00 business-local (UTC in tests).
        assert campaign.starts_at is not None
        assert str(campaign.starts_at).startswith("2026-10-12 09:00")

        steps = (
            db.query(CampaignStep)
            .filter(CampaignStep.campaign_id == campaign.id)
            .order_by(CampaignStep.position)
            .all()
        )
        assert len(steps) == 3
        assert [s.position for s in steps] == [0, 1, 2]
        assert [str(s.channel.value) for s in steps] == ["email", "social", "sms"]
        # delay_hours = day * 24 -> 24 / 72 / 120
        assert [s.delay_hours for s in steps] == [24, 72, 120]
        assert steps[0].asset_id == asset.id
        assert steps[1].asset_id is None
        assert steps[2].asset_id is None
        assert all(s.trigger_event is None for s in steps)
    finally:
        db.close()


def test_approve_revalidates_asset_refs(user_a, user_b):
    """Unapproved / foreign-tenant / unknown asset refs become null steps."""
    draft_asset = _asset(user_a, status=AssetStatus.draft)
    foreign_asset = _asset(user_b)
    plan = _draft_plan(
        user_a,
        items=[
            {
                "kind": "email_copy",
                "channel": "email",
                "day": 1,
                "title": "t",
                "brief": "b",
                "asset_id": str(draft_asset.id),  # not approved -> null
            },
            {
                "kind": "social_post",
                "channel": "social",
                "day": 3,
                "title": "t",
                "brief": "b",
                "asset_id": str(foreign_asset.id),  # wrong tenant -> null
            },
            {
                "kind": "sms",
                "channel": "sms",
                "day": 5,
                "title": "t",
                "brief": "b",
                "asset_id": str(uuid.uuid4()),  # unknown -> null
            },
        ],
    )

    r = client.post(
        "/api/v1/autopilot/plan/approve",
        json={"plan_id": str(plan.id)},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200, r.text
    campaign_id = r.json()["campaign_id"]

    db = TestingSession()
    try:
        steps = (
            db.query(CampaignStep)
            .filter(CampaignStep.campaign_id == uuid.UUID(campaign_id))
            .order_by(CampaignStep.position)
            .all()
        )
        assert [s.asset_id for s in steps] == [None, None, None]
    finally:
        db.close()


def test_double_approve_is_422(user_a):
    plan = _draft_plan(user_a)
    headers = _headers(user_a["token"])
    payload = {"plan_id": str(plan.id)}

    r1 = client.post("/api/v1/autopilot/plan/approve", json=payload, headers=headers)
    assert r1.status_code == 200, r1.text

    r2 = client.post("/api/v1/autopilot/plan/approve", json=payload, headers=headers)
    assert r2.status_code == 422, r2.text


def test_approve_foreign_plan_is_404(user_a, user_b):
    plan = _draft_plan(user_b)
    r = client.post(
        "/api/v1/autopilot/plan/approve",
        json={"plan_id": str(plan.id)},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 404, r.text


def test_approve_unknown_plan_is_404(user_a):
    r = client.post(
        "/api/v1/autopilot/plan/approve",
        json={"plan_id": str(uuid.uuid4())},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 404, r.text


def test_get_plan_returns_approved_with_campaign_link(user_a):
    plan = _draft_plan(user_a)
    r = client.post(
        "/api/v1/autopilot/plan/approve",
        json={"plan_id": str(plan.id)},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200, r.text

    r2 = client.get("/api/v1/autopilot/plan", headers=_headers(user_a["token"]))
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "approved"
    assert body["campaign_id"] == r.json()["campaign_id"]
