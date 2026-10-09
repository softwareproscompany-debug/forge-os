"""Card 0 interview flow: TestClient + SQLite (models are dialect-portable).

Covers:
  start -> answer x7 -> finish -> confirm (creates a real BrandKit)
  follow-up generation never fires on the stub provider
  tenant isolation (another business's session id -> 404)
  answering a completed session -> 422
  confirming twice -> 422
"""

from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("LLM_PROVIDER", "stub")

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forge_db.models import Base, InterviewSession

from app.core.config import Settings
from app.core.deps import get_db_session, get_settings_dep
from app.main import create_app

TEST_DB = "/tmp/forgeos_interview_test.db"
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
    return {"email": email, "token": body["token"], "user": body["user"]}


ANSWERS = [
    "We sell handmade leather wallets to men aged 25-45 who value craftsmanship.",
    "The 'Buy one, gift one' holiday bundle — 40% of December revenue.",
    "Post customer photos, email newsletter, restock alerts, reply to comments, run a weekly giveaway.",
    "Instagram and Reddit leatherworking communities.",
    "Bellroy for minimal copy, Filson for heritage storytelling, Darn Tough for their guarantee.",
    "cheap, discount, hustle, leverage, synergy",
    "The Valentine's flash sale — 20% off everything, zero urgency, flopped because we never told anyone before the day.",
]


def _run_full_interview(headers: dict[str, str], answers: list[str] | None = None) -> dict:
    answers = answers or ANSWERS
    r = client.post("/api/v1/interview/start", headers=headers)
    assert r.status_code == 201, r.text
    state = r.json()
    assert state["status"] == "active"
    assert state["question_index"] == 0
    assert state["total_questions"] == 7
    assert state["question"] and not state["done"]

    for i, answer in enumerate(answers):
        r = client.post(
            f"/api/v1/interview/{state['session_id']}/answer",
            json={"answer": answer},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        state = r.json()
        if i < 6:
            assert not state["done"], f"interview ended early at answer {i}"
            assert state["question"], f"missing question after answer {i}"
        assert state["question_index"] == i + 1, f"wrong index after answer {i}"

    assert state["done"] is True
    assert state["question"] is None
    return state


def _session_row(session_id: str) -> InterviewSession:
    db = TestingSession()
    try:
        return db.get(InterviewSession, uuid.UUID(session_id))
    finally:
        db.close()


def test_full_flow_start_answer_finish_confirm():
    acct = _register("interviewee", "Leatherworks")
    headers = _headers(acct["token"])

    state = _run_full_interview(headers)
    session_id = state["session_id"]

    r = client.post(f"/api/v1/interview/{session_id}/finish", headers=headers)
    assert r.status_code == 200, r.text
    finished = r.json()
    assert finished["status"] == "completed"
    draft = finished["draft_brand_kit"]
    assert draft["name"].endswith("— Interview Kit")
    assert draft["icp_description"] == ANSWERS[0]
    assert draft["do_list"] == [ANSWERS[1]]
    assert draft["dont_list"] == ["cheap", "discount", "hustle", "leverage", "synergy"]
    assert draft["tone_tags"] == []
    assert draft["primary_color"] is None and draft["secondary_color"] is None
    assert draft["fonts"] == {}
    assert "Bellroy" in (draft["voice_description"] or "")

    r = client.post(f"/api/v1/interview/{session_id}/confirm", json={}, headers=headers)
    assert r.status_code == 200, r.text
    kit = r.json()["brand_kit"]
    assert kit["name"] == draft["name"]
    assert kit["icp_description"] == ANSWERS[0]
    assert kit["version"] == 1
    assert kit["business_id"] == acct["user"]["business_id"]

    # The session is linked to the new kit.
    assert _session_row(session_id).brand_kit_id == uuid.UUID(kit["id"])

    # The kit is a real row: visible via the brand-kits API.
    r = client.get(f"/api/v1/brand-kits/{kit['id']}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == draft["name"]


def test_followup_never_fires_on_stub_provider():
    acct = _register("stubcheck", "Stubworks")
    headers = _headers(acct["token"])
    state = _run_full_interview(headers)
    row = _session_row(state["session_id"])
    assert len(row.answers) == 7
    assert all(entry["followup"] is False for entry in row.answers)
    assert all(entry["answer"] for entry in row.answers)


def test_confirm_with_overrides():
    acct = _register("overrider", "Overrideworks")
    headers = _headers(acct["token"])
    state = _run_full_interview(headers)

    r = client.post(f"/api/v1/interview/{state['session_id']}/finish", headers=headers)
    assert r.status_code == 200, r.text

    overrides = {"tone_tags": ["bold", "playful"], "primary_color": "#ff0000"}
    r = client.post(
        f"/api/v1/interview/{state['session_id']}/confirm",
        json={"overrides": overrides},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    kit = r.json()["brand_kit"]
    assert kit["tone_tags"] == ["bold", "playful"]
    assert kit["primary_color"] == "#ff0000"
    # Untouched draft fields survive.
    assert kit["icp_description"] == ANSWERS[0]


def test_tenant_isolation():
    alice = _register("alice", "AliceCo")
    bob = _register("bob", "BobCo")
    alice_h, bob_h = _headers(alice["token"]), _headers(bob["token"])

    r = client.post("/api/v1/interview/start", headers=alice_h)
    session_id = r.json()["session_id"]

    # Bob's business must not see Alice's session: 404, not 403.
    for method, url, kwargs in [
        ("post", f"/api/v1/interview/{session_id}/answer", {"json": {"answer": "x"}}),
        ("post", f"/api/v1/interview/{session_id}/finish", {}),
        ("post", f"/api/v1/interview/{session_id}/confirm", {"json": {}}),
    ]:
        r = getattr(client, method)(url, headers=bob_h, **kwargs)
        assert r.status_code == 404, (url, r.status_code, r.text)


def test_answer_completed_session_is_422():
    acct = _register("finisher", "Finishworks")
    headers = _headers(acct["token"])
    state = _run_full_interview(headers)
    session_id = state["session_id"]

    r = client.post(f"/api/v1/interview/{session_id}/finish", headers=headers)
    assert r.status_code == 200, r.text

    r = client.post(
        f"/api/v1/interview/{session_id}/answer",
        json={"answer": "too late"},
        headers=headers,
    )
    assert r.status_code == 422, r.text

    # And finish again is also 422 (no longer active).
    r = client.post(f"/api/v1/interview/{session_id}/finish", headers=headers)
    assert r.status_code == 422, r.text


def test_confirm_twice_is_422():
    acct = _register("twice", "Twiceworks")
    headers = _headers(acct["token"])
    state = _run_full_interview(headers)
    session_id = state["session_id"]

    r = client.post(f"/api/v1/interview/{session_id}/finish", headers=headers)
    assert r.status_code == 200, r.text

    r = client.post(f"/api/v1/interview/{session_id}/confirm", json={}, headers=headers)
    assert r.status_code == 200, r.text

    r = client.post(f"/api/v1/interview/{session_id}/confirm", json={}, headers=headers)
    assert r.status_code == 422, r.text


def test_finish_before_complete_is_422():
    acct = _register("early", "Earlyworks")
    headers = _headers(acct["token"])

    r = client.post("/api/v1/interview/start", headers=headers)
    assert r.status_code == 201, r.text
    session_id = r.json()["session_id"]

    r = client.post(
        f"/api/v1/interview/{session_id}/answer",
        json={"answer": ANSWERS[0]},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    r = client.post(f"/api/v1/interview/{session_id}/finish", headers=headers)
    assert r.status_code == 422, r.text


def test_confirm_before_finish_is_422():
    acct = _register("premature", "Prematureworks")
    headers = _headers(acct["token"])

    r = client.post("/api/v1/interview/start", headers=headers)
    session_id = r.json()["session_id"]

    r = client.post(f"/api/v1/interview/{session_id}/confirm", json={}, headers=headers)
    assert r.status_code == 422, r.text
