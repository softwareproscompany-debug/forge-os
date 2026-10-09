"""Travel Agency workspace tests: travel CRM CRUD + tenant isolation.

TestClient + SQLite (models are dialect-portable). Every endpoint must be
tenant-scoped by business_id; cross-tenant IDs return 404, never 403.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forge_db.models import Base

from app.core.config import Settings
from app.core.deps import get_db_session, get_settings_dep
from app.main import create_app

TEST_DB = "/tmp/forgeos_travel_test.db"
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
            "full_name": "Travel Owner",
            "business_name": business_name,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture()
def owner() -> dict:
    return _register("travel", "Travel Biz")


@pytest.fixture()
def auth(owner: dict) -> dict[str, str]:
    return _headers(owner["token"])


@pytest.fixture()
def other_auth() -> dict[str, str]:
    return _headers(_register("travel2", "Other Travel Biz")["token"])


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_travel_dashboard_empty(auth: dict[str, str]):
    r = client.get("/api/v1/travel/dashboard", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lead_counts"] == {
        "new": 0,
        "qualified": 0,
        "quoted": 0,
        "booked": 0,
        "lost": 0,
    }
    assert body["recent_leads"] == []
    assert body["customer_count"] == 0
    assert body["trip_request_count"] == 0


def test_travel_dashboard_counts(auth: dict[str, str]):
    for dest in ("Paris", "Tokyo"):
        r = client.post(
            "/api/v1/travel/leads", headers=auth, json={"destination": dest}
        )
        assert r.status_code == 201, r.text
    r = client.get("/api/v1/travel/dashboard", headers=auth)
    assert r.status_code == 200
    assert r.json()["lead_counts"]["new"] == 2
    assert len(r.json()["recent_leads"]) == 2


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


def test_travel_customer_crud(auth: dict[str, str]):
    r = client.post(
        "/api/v1/travel/customers",
        headers=auth,
        json={
            "name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "+1-555-0100",
            "type": "individual",
            "notes": "Prefers window seats",
        },
    )
    assert r.status_code == 201, r.text
    customer = r.json()
    assert customer["name"] == "Jane Doe"
    assert customer["business_id"]
    customer_id = customer["id"]

    r = client.get(f"/api/v1/travel/customers/{customer_id}", headers=auth)
    assert r.status_code == 200
    assert r.json()["email"] == "jane@example.com"

    r = client.put(
        f"/api/v1/travel/customers/{customer_id}",
        headers=auth,
        json={"type": "corporate"},
    )
    assert r.status_code == 200
    assert r.json()["type"] == "corporate"

    r = client.get("/api/v1/travel/customers?q=jane", headers=auth)
    assert r.status_code == 200
    assert r.json()["total"] >= 1

    r = client.delete(f"/api/v1/travel/customers/{customer_id}", headers=auth)
    assert r.status_code == 204
    r = client.get(f"/api/v1/travel/customers/{customer_id}", headers=auth)
    assert r.status_code == 404


def test_travel_customer_type_validation(auth: dict[str, str]):
    r = client.post(
        "/api/v1/travel/customers",
        headers=auth,
        json={"name": "Bad Type", "type": "alien"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Travelers
# ---------------------------------------------------------------------------


def test_traveler_crud(auth: dict[str, str]):
    r = client.post(
        "/api/v1/travel/customers", headers=auth, json={"name": "Family Smith"}
    )
    assert r.status_code == 201
    customer_id = r.json()["id"]

    r = client.post(
        "/api/v1/travel/travelers",
        headers=auth,
        json={
            "customer_id": customer_id,
            "full_name": "John Smith",
            "dob": "1990-05-14",
            "preferences": {"seat": "window", "meal": "vegetarian"},
            "loyalty": {"united": [{"program": "MileagePlus", "number": "UA123"}]},
            "accessibility_notes": "Wheelchair assistance at gate",
        },
    )
    assert r.status_code == 201, r.text
    traveler = r.json()
    assert traveler["full_name"] == "John Smith"
    assert traveler["preferences"]["seat"] == "window"
    traveler_id = traveler["id"]

    r = client.get(f"/api/v1/travel/travelers/{traveler_id}", headers=auth)
    assert r.status_code == 200

    r = client.get(
        f"/api/v1/travel/customers/{customer_id}/travelers", headers=auth
    )
    assert r.status_code == 200
    assert r.json()["total"] == 1

    r = client.put(
        f"/api/v1/travel/travelers/{traveler_id}",
        headers=auth,
        json={"accessibility_notes": "None"},
    )
    assert r.status_code == 200
    assert r.json()["accessibility_notes"] == "None"

    r = client.delete(f"/api/v1/travel/travelers/{traveler_id}", headers=auth)
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


def test_travel_lead_crud_and_transitions(auth: dict[str, str]):
    r = client.post(
        "/api/v1/travel/leads",
        headers=auth,
        json={
            "destination": "Bali",
            "source": "website",
            "date_start": "2026-12-01",
            "date_end": "2026-12-14",
            "budget": "4500.00",
            "trip_purpose": "honeymoon",
        },
    )
    assert r.status_code == 201, r.text
    lead = r.json()
    assert lead["status"] == "new"
    assert float(lead["budget"]) == 4500.00
    lead_id = lead["id"]

    # Valid transition: new -> qualified
    r = client.post(
        f"/api/v1/travel/leads/{lead_id}/status",
        headers=auth,
        json={"status": "qualified"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "qualified"

    # Valid: qualified -> quoted
    r = client.post(
        f"/api/v1/travel/leads/{lead_id}/status",
        headers=auth,
        json={"status": "quoted"},
    )
    assert r.status_code == 200

    # Invalid: quoted -> new (not an allowed transition)
    r = client.post(
        f"/api/v1/travel/leads/{lead_id}/status",
        headers=auth,
        json={"status": "new"},
    )
    assert r.status_code == 422, r.text

    # Invalid status value rejected by schema
    r = client.post(
        f"/api/v1/travel/leads/{lead_id}/status",
        headers=auth,
        json={"status": "bogus"},
    )
    assert r.status_code == 422

    # booked is terminal
    r = client.post(
        f"/api/v1/travel/leads/{lead_id}/status",
        headers=auth,
        json={"status": "booked"},
    )
    assert r.status_code == 200
    r = client.post(
        f"/api/v1/travel/leads/{lead_id}/status",
        headers=auth,
        json={"status": "qualified"},
    )
    assert r.status_code == 422

    # Filter by status
    r = client.get("/api/v1/travel/leads?status=booked", headers=auth)
    assert r.status_code == 200
    assert r.json()["total"] >= 1

    r = client.delete(f"/api/v1/travel/leads/{lead_id}", headers=auth)
    assert r.status_code == 204


def test_lead_lost_can_reopen(auth: dict[str, str]):
    r = client.post(
        "/api/v1/travel/leads", headers=auth, json={"destination": "Rome"}
    )
    lead_id = r.json()["id"]
    r = client.post(
        f"/api/v1/travel/leads/{lead_id}/status", headers=auth, json={"status": "lost"}
    )
    assert r.status_code == 200
    r = client.post(
        f"/api/v1/travel/leads/{lead_id}/status", headers=auth, json={"status": "new"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "new"


# ---------------------------------------------------------------------------
# Trip requests
# ---------------------------------------------------------------------------


def test_trip_request_crud(auth: dict[str, str]):
    r = client.post(
        "/api/v1/travel/leads", headers=auth, json={"destination": "Kyoto"}
    )
    lead_id = r.json()["id"]
    r = client.post(
        "/api/v1/travel/customers", headers=auth, json={"name": "Ken Watanabe"}
    )
    customer_id = r.json()["id"]

    r = client.post(
        "/api/v1/travel/trip-requests",
        headers=auth,
        json={
            "lead_id": lead_id,
            "customer_id": customer_id,
            "party_size": 2,
            "origin": "IAH",
            "destinations": ["KIX", "NRT"],
            "date_start": "2027-03-10",
            "date_end": "2027-03-24",
            "preferences": {"hotel_class": "4-star"},
            "flexibility": "dates flexible +/- 3 days",
        },
    )
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["destinations"] == ["KIX", "NRT"]
    assert req["party_size"] == 2
    assert req["status"] == "open"
    request_id = req["id"]

    r = client.get(f"/api/v1/travel/trip-requests/{request_id}", headers=auth)
    assert r.status_code == 200

    r = client.get(
        f"/api/v1/travel/trip-requests?lead_id={lead_id}", headers=auth
    )
    assert r.status_code == 200
    assert r.json()["total"] == 1

    r = client.put(
        f"/api/v1/travel/trip-requests/{request_id}",
        headers=auth,
        json={"status": "in_progress"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"

    r = client.delete(f"/api/v1/travel/trip-requests/{request_id}", headers=auth)
    assert r.status_code == 204


def test_trip_request_rejects_other_tenant_refs(auth: dict[str, str], other_auth: dict[str, str]):
    # Create a lead in the OTHER tenant, then try to attach it here -> 404.
    r = client.post(
        "/api/v1/travel/leads", headers=other_auth, json={"destination": "Oslo"}
    )
    other_lead_id = r.json()["id"]
    r = client.post(
        "/api/v1/travel/trip-requests",
        headers=auth,
        json={"lead_id": other_lead_id, "destinations": ["OSL"]},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_travel_tenant_isolation(auth: dict[str, str], other_auth: dict[str, str]):
    # A creates a customer, traveler, lead, and trip request.
    r = client.post(
        "/api/v1/travel/customers", headers=auth, json={"name": "Secret Client"}
    )
    customer_id = r.json()["id"]
    r = client.post(
        "/api/v1/travel/travelers",
        headers=auth,
        json={"customer_id": customer_id, "full_name": "Secret Traveler"},
    )
    traveler_id = r.json()["id"]
    r = client.post(
        "/api/v1/travel/leads", headers=auth, json={"destination": "Secret Island"}
    )
    lead_id = r.json()["id"]
    r = client.post(
        "/api/v1/travel/trip-requests",
        headers=auth,
        json={"lead_id": lead_id, "destinations": ["XXX"]},
    )
    request_id = r.json()["id"]

    # B cannot see any of them by ID guessing -> 404 (not 403).
    for url in (
        f"/api/v1/travel/customers/{customer_id}",
        f"/api/v1/travel/travelers/{traveler_id}",
        f"/api/v1/travel/leads/{lead_id}",
        f"/api/v1/travel/trip-requests/{request_id}",
    ):
        r = client.get(url, headers=other_auth)
        assert r.status_code == 404, url

    # B's lists are empty of A's records.
    for url in (
        "/api/v1/travel/customers",
        "/api/v1/travel/travelers",
        "/api/v1/travel/leads",
        "/api/v1/travel/trip-requests",
    ):
        r = client.get(url, headers=other_auth)
        assert r.status_code == 200
        assert r.json()["total"] == 0, url

    # B cannot mutate A's records either.
    r = client.delete(f"/api/v1/travel/leads/{lead_id}", headers=other_auth)
    assert r.status_code == 404

    # B's dashboard shows no leads.
    r = client.get("/api/v1/travel/dashboard", headers=other_auth)
    assert r.status_code == 200
    assert sum(r.json()["lead_counts"].values()) == 0

    # Auth is still required.
    r = client.get("/api/v1/travel/leads")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Draven tools
# ---------------------------------------------------------------------------


def test_travel_draven_tools_registered():
    from app.draven_tools import TOOLS

    ids = set(TOOLS.keys()) if isinstance(TOOLS, dict) else {t.id for t in TOOLS}
    assert "travel.lead_create" in ids
    assert "travel.lead_status" in ids
    assert "travel.customer_search" in ids
    assert len(TOOLS) == 78


def test_travel_draven_routing():
    from app.draven_tools import route_intent

    assert any(t == "travel.lead_create" for t, _ in route_intent("create a new travel lead to Paris"))
    assert any(t == "travel.lead_status" for t, _ in route_intent("show my travel leads"))
    assert any(
        t == "travel.customer_search" for t, _ in route_intent("find travel customer Jane")
    )
