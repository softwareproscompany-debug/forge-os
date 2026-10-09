"""Draven backend tests: TestClient + SQLite (models are dialect-portable).

Covers:
  tool registry listing (no secrets), chat routing + execution,
  tenant isolation (two businesses, no cross-tenant leakage),
  medium/high-risk approval gating without side effects,
  audit rows for every outcome, provider secret handling
  (encryption at rest, never leaked in responses, fail-closed without key),
  admin-only provider endpoints, emergency stop behavior.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forge_db.models import (
    Asset,
    AssetKind,
    AssetStatus,
    Base,
    Business,
    Campaign,
    CampaignStatus,
    Contact,
    DravenProviderConfig,
    DravenToolRun,
    User,
    UserRole,
)

from app.core.config import Settings
from app.core.deps import get_db_session, get_settings_dep
from app.main import create_app
import app.routers.draven as draven_router

TEST_DB = "/tmp/forgeos_draven_test.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

engine = create_engine(f"sqlite:///{TEST_DB}", connect_args={"check_same_thread": False})
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)

DRAVEN_KEY = Fernet.generate_key().decode()

test_settings = Settings(
    DATABASE_URL="sqlite:////tmp/forgeos_draven_test.db",
    REDIS_URL="redis://127.0.0.1:9/0",  # closed port -> enqueue fails fast
    JWT_SECRET="test-secret",
    WEBHOOK_SECRET="test-webhook-secret",
    DRAVEN_CONFIG_KEY=DRAVEN_KEY,
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


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    draven_router._kill_switch_at = None
    yield
    draven_router._kill_switch_at = None


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
            "business_name": f"{email_prefix.title()} Co {_counter}",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return {"email": email, "token": body["token"], "user": body["user"]}


def _business_id(user: dict) -> uuid.UUID:
    db = TestingSession()
    try:
        u = db.query(User).filter(User.id == uuid.UUID(user["user"]["id"])).one()
        return u.business_id
    finally:
        db.close()


@pytest.fixture()
def user_a() -> dict:
    return _register("draven_alice")


@pytest.fixture()
def user_b() -> dict:
    return _register("draven_bob")


def _seed_tenant_a(user: dict) -> dict:
    """Contact + in_review asset + running campaign for user_a's business."""
    bid = _business_id(user)
    db = TestingSession()
    try:
        contact = Contact(
            business_id=bid,
            email="sarah@example.com",
            first_name="Sarah",
            last_name="Connor",
        )
        asset = Asset(
            business_id=bid,
            kind=AssetKind.email_copy,
            title="Spring Promo Email",
            body="Hello {{first_name}}, spring sale!",
            status=AssetStatus.in_review,
            created_by=uuid.UUID(user["user"]["id"]),
        )
        campaign = Campaign(
            business_id=bid,
            name="Welcome Series",
            status=CampaignStatus.running,
            created_by=uuid.UUID(user["user"]["id"]),
        )
        db.add_all([contact, asset, campaign])
        db.commit()
        return {
            "contact_id": str(contact.id),
            "asset_id": str(asset.id),
            "campaign_id": str(campaign.id),
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# tool registry listing
# ---------------------------------------------------------------------------


def test_tools_list(user_a):
    r = client.get("/api/v1/draven/tools", headers=_headers(user_a["token"]))
    assert r.status_code == 200, r.text
    tools = r.json()
    ids = {t["id"] for t in tools}
    assert "draven.business_summary" in ids
    assert "draven.campaign_pause" in ids
    assert "draven.asset_approve" in ids
    assert "market.research_start" in ids
    assert "market.research_status" in ids
    assert "market.top_opportunities" in ids
    assert len(tools) == 67  # 59 draven.* + 3 market.* + 5 alpha.*
    for t in tools:
        assert set(t) == {"id", "description", "risk", "input_schema"}
        assert t["risk"] in ("low", "medium", "high")
    # high-risk tools are flagged as such
    risks = {t["id"]: t["risk"] for t in tools}
    assert risks["draven.campaign_pause"] == "high"
    assert risks["draven.asset_approve"] == "high"


def test_tools_requires_auth():
    assert client.get("/api/v1/draven/tools").status_code == 401


# ---------------------------------------------------------------------------
# chat: routing, execution, deterministic reply
# ---------------------------------------------------------------------------


def test_chat_business_summary(user_a):
    _seed_tenant_a(user_a)
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "give me a business summary", "history": []},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "stub"
    assert body["estimated_cost_usd"] == 0.0
    used = {t["tool"]: t for t in body["tools_used"]}
    assert used["draven.business_summary"]["status"] == "ok"
    assert used["draven.business_summary"]["risk"] == "low"
    assert "1 campaigns" in body["reply"] or "campaigns" in body["reply"]

    # audit row written
    db = TestingSession()
    try:
        runs = (
            db.query(DravenToolRun)
            .filter(DravenToolRun.tool == "draven.business_summary")
            .all()
        )
        assert len(runs) >= 1
        assert runs[-1].status == "ok"
        assert runs[-1].duration_ms >= 0
    finally:
        db.close()


def test_chat_no_intent(user_a):
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "tell me a joke about pineapples", "history": []},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tools_used"] == []
    assert body["approvals_needed"] == []


# ---------------------------------------------------------------------------
# assistant personas (draven / calcifer)
# ---------------------------------------------------------------------------


def test_chat_persona_defaults_to_draven(user_a):
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "what is your name", "history": []},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Draven" in body["reply"]
    assert "Calcifer" not in body["reply"]
    assert body["tools_used"] == []


def test_chat_persona_calcifer_identity(user_a):
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "who are you", "history": [], "persona": "calcifer"},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Calcifer" in body["reply"]
    assert body["tools_used"] == []


def test_chat_persona_calcifer_tools_still_work(user_a):
    # Persona changes identity wording only — tool behavior is identical.
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "summarize today", "history": [], "persona": "calcifer"},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tools_used"], "expected tool runs regardless of persona"


def test_chat_persona_invalid_is_rejected(user_a):
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "hi", "history": [], "persona": "gandalf"},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# conversational slot-filling (pending intents)
# ---------------------------------------------------------------------------


def _chat(user_a, message, **kw):
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": message, "history": [], **kw},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_pending_research_ask_then_fill(user_a):
    # No category -> the assistant asks, and records a pending intent.
    first = _chat(user_a, "find products that sell well")
    assert "which product category" in first["reply"]
    assert first["tools_used"], "expected the research tool to have run (and errored)"

    # The plain answer fills the slot and the original tool executes.
    second = _chat(user_a, "kitchen gadgets")
    assert "Got it \u2014 researching kitchen gadgets." in second["reply"]
    ids = [t["tool"] for t in second["tools_used"]]
    assert "market.research_start" in ids
    statuses = {t["tool"]: t["status"] for t in second["tools_used"]}
    assert statuses["market.research_start"] == "ok"


def test_pending_new_command_clears(user_a):
    _chat(user_a, "find products that sell well")
    # A real command supersedes the pending question.
    mid = _chat(user_a, "summarize today")
    assert "Got it \u2014 researching" not in mid["reply"]
    assert any(t["tool"] == "draven.business_summary" for t in mid["tools_used"])
    # The category answer afterwards is NOT treated as a fill anymore.
    after = _chat(user_a, "kitchen gadgets")
    assert "Got it \u2014 researching" not in after["reply"]
    assert after["tools_used"] == []


def test_pending_cancel(user_a):
    _chat(user_a, "find products that sell well")
    cancelled = _chat(user_a, "never mind, cancel that")
    assert "dropped" in cancelled["reply"]
    after = _chat(user_a, "kitchen gadgets")
    assert "Got it \u2014 researching" not in after["reply"]
    assert after["tools_used"] == []


def test_pending_unmappable_answer_reasks(user_a):
    from app import draven_conversation as conv

    _chat(user_a, "find products that sell well")
    # A question-shaped reply can't be mapped -> specific re-ask, pending kept.
    again = _chat(user_a, "what do you mean?")
    assert "still need a product category" in again["reply"]
    assert again["tools_used"] == []
    # And the real answer still fills afterwards.
    filled = _chat(user_a, "pet supplies")
    assert "Got it \u2014 researching pet supplies." in filled["reply"]


def test_pending_ttl_expiry():
    import time
    from app import draven_conversation as conv
    from app.draven_conversation import PendingIntent

    conv.set_pending(
        "00000000-0000-0000-0000-000000000000",
        PendingIntent(
            kind="market.research_start",
            missing_slots=["category"],
            asked_at=time.time() - conv.TTL_S - 1,
        ),
    )
    assert conv.get_pending("00000000-0000-0000-0000-000000000000") is None


def test_pending_identity_not_consumed(user_a):
    _chat(user_a, "find products that sell well")
    # Asking the assistant's name answers directly; the pending question survives.
    ident = _chat(user_a, "what is your name")
    assert "Draven" in ident["reply"]
    filled = _chat(user_a, "home fitness")
    assert "Got it \u2014 researching home fitness." in filled["reply"]


# ---------------------------------------------------------------------------
# time-aware greetings + manners
# ---------------------------------------------------------------------------


def test_time_salutation_boundaries():
    from datetime import datetime

    from app.routers.draven import time_salutation

    cases = [
        (4, 59, "Good evening"),
        (5, 0, "Good morning"),
        (11, 59, "Good morning"),
        (12, 0, "Good afternoon"),
        (16, 59, "Good afternoon"),
        (17, 0, "Good evening"),
        (23, 30, "Good evening"),
        (0, 15, "Good evening"),
    ]
    for h, m, expected in cases:
        assert time_salutation(datetime(2026, 10, 9, h, m)) == expected


def test_chat_identity_salutation_with_tz(user_a):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.routers.draven import time_salutation

    for persona, name in (("draven", "Draven"), ("calcifer", "Calcifer")):
        body = _chat(
            user_a,
            "who are you",
            persona=persona,
            client_tz="America/Chicago",
        )
        expected = time_salutation(datetime.now(ZoneInfo("America/Chicago")))
        assert body["reply"].startswith(f"{expected}! "), body["reply"][:40]
        assert name in body["reply"]


def test_chat_identity_no_tz_no_salutation(user_a):
    body = _chat(user_a, "introduce yourself")
    assert body["reply"].startswith("I'm Draven \u2014")
    assert not body["reply"].startswith("Good ")


def test_chat_identity_invalid_tz_rejected(user_a):
    r = client.post(
        "/api/v1/draven/chat",
        json={
            "message": "who are you",
            "history": [],
            "client_tz": "Mars/Olympus_Mons",
        },
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 422, r.text


def test_persona_system_prompts_carry_manners():
    from app.routers.draven import _PERSONAS

    for pid, p in _PERSONAS.items():
        sp = p["system_prompt"].lower()
        assert "manners" in sp, pid
        assert "greet" in sp, pid


# ---------------------------------------------------------------------------
# tenant isolation
# ---------------------------------------------------------------------------


def test_tenant_isolation(user_a, user_b):
    seed = _seed_tenant_a(user_a)

    # user_b searches for user_a's contact -> sees nothing
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "find contact sarah", "history": []},
        headers=_headers(user_b["token"]),
    )
    assert r.status_code == 200, r.text
    used = {t["tool"]: t for t in r.json()["tools_used"]}
    assert used["draven.contacts_search"]["status"] == "ok"
    assert "No contacts matched" in r.json()["reply"]

    # user_b checks approvals -> user_a's in_review asset is invisible
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "show me pending approvals", "history": []},
        headers=_headers(user_b["token"]),
    )
    assert r.status_code == 200, r.text
    assert "0 assets awaiting review" in r.json()["reply"]

    # user_b cannot pause user_a's campaign even by id
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "pause campaign", "history": []},
        headers=_headers(user_b["token"]),
    )
    assert r.status_code == 200

    # audit rows are tenant-scoped
    r = client.get("/api/v1/draven/audit?limit=50", headers=_headers(user_b["token"]))
    items = r.json()["items"]
    assert all(i["tool"] for i in items)
    db = TestingSession()
    try:
        bid_b = _business_id(user_b)
        leaked = (
            db.query(DravenToolRun)
            .filter(
                DravenToolRun.business_id == bid_b,
                DravenToolRun.output_summary.ilike("%sarah%"),
            )
            .count()
        )
        assert leaked == 0
    finally:
        db.close()

    # sanity: user_a still sees their own data
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "show me pending approvals", "history": []},
        headers=_headers(user_a["token"]),
    )
    assert "1 asset" in r.json()["reply"]
    assert seed["asset_id"]  # fixture sanity


# ---------------------------------------------------------------------------
# high-risk tools: approval gating, no side effects
# ---------------------------------------------------------------------------


def test_campaign_pause_requires_approval(user_a):
    seed = _seed_tenant_a(user_a)
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "pause the welcome campaign", "history": []},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["approvals_needed"]) == 1
    ap = body["approvals_needed"][0]
    assert ap["tool"] == "draven.campaign_pause"
    assert "Welcome Series" in ap["description"]
    assert ap["input"]["campaign_id"] == seed["campaign_id"]
    assert "needs your approval" in body["reply"]

    # NO side effect: campaign still running
    db = TestingSession()
    try:
        c = db.query(Campaign).filter(Campaign.id == uuid.UUID(seed["campaign_id"])).one()
        assert c.status == CampaignStatus.running
        run = (
            db.query(DravenToolRun)
            .filter(DravenToolRun.tool == "draven.campaign_pause")
            .order_by(DravenToolRun.created_at.desc())
            .first()
        )
        assert run.status == "approval_required"
        assert run.risk == "high"
    finally:
        db.close()


def test_asset_approve_requires_approval(user_a):
    seed = _seed_tenant_a(user_a)
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "approve the spring promo asset", "history": []},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["approvals_needed"]) == 1
    ap = body["approvals_needed"][0]
    assert ap["tool"] == "draven.asset_approve"
    assert "Spring Promo Email" in ap["description"]

    # NO side effect: asset still in_review
    db = TestingSession()
    try:
        a = db.query(Asset).filter(Asset.id == uuid.UUID(seed["asset_id"])).one()
        assert a.status == AssetStatus.in_review
    finally:
        db.close()


def test_high_risk_unknown_target_errors_safely(user_a):
    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "pause the nonexistent campaign xyz", "history": []},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approvals_needed"] == []
    used = {t["tool"]: t for t in body["tools_used"]}
    assert used["draven.campaign_pause"]["status"] == "error"


# ---------------------------------------------------------------------------
# audit endpoint
# ---------------------------------------------------------------------------


def test_audit_endpoint(user_a):
    client.post(
        "/api/v1/draven/chat",
        json={"message": "business summary", "history": []},
        headers=_headers(user_a["token"]),
    )
    r = client.get("/api/v1/draven/audit?limit=5", headers=_headers(user_a["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 1
    item = body["items"][0]
    assert set(item) == {
        "tool",
        "risk",
        "status",
        "duration_ms",
        "created_at",
        "output_summary",
    }
    assert len(item["output_summary"] or "") <= 300
    assert "input" not in item  # raw inputs not exposed in the list view


# ---------------------------------------------------------------------------
# provider config: secrets, roles, fail-closed
# ---------------------------------------------------------------------------


def _demote_to_member(user: dict) -> None:
    db = TestingSession()
    try:
        u = db.query(User).filter(User.id == uuid.UUID(user["user"]["id"])).one()
        u.role = UserRole.member
        db.commit()
    finally:
        db.close()


def test_provider_endpoints_admin_only(user_a):
    member = _register("draven_carol")
    _demote_to_member(member)
    h = _headers(member["token"])
    assert client.get("/api/v1/draven/provider", headers=h).status_code == 200  # read: any role
    assert (
        client.put(
            "/api/v1/draven/provider",
            json={"provider": "stub"},
            headers=h,
        ).status_code
        == 403
    )
    assert client.post("/api/v1/draven/provider/test", headers=h).status_code == 403


def test_provider_config_encrypted_and_never_leaked(user_a):
    h = _headers(user_a["token"])
    secret = "sk-anthropic-test-secret-12345"

    r = client.put(
        "/api/v1/draven/provider",
        json={"provider": "anthropic", "model": "claude-test", "api_key": secret},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "anthropic"
    assert body["has_api_key"] is True
    assert secret not in r.text  # response carries no key material

    r = client.get("/api/v1/draven/provider", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is True
    assert secret not in r.text

    # at rest: encrypted, not plaintext
    db = TestingSession()
    try:
        row = (
            db.query(DravenProviderConfig)
            .filter(DravenProviderConfig.business_id == _business_id(user_a))
            .one()
        )
        assert row.api_key_enc != secret
        assert row.api_key_enc.startswith("gAAAAA")  # Fernet token
        f = Fernet(DRAVEN_KEY.encode())
        assert f.decrypt(row.api_key_enc.encode()).decode() == secret
    finally:
        db.close()


def test_provider_put_fails_closed_without_key():
    """Without DRAVEN_CONFIG_KEY, saving secrets must 400, not store plaintext."""
    no_key_settings = Settings(
        DATABASE_URL="sqlite:////tmp/forgeos_draven_test.db",
        REDIS_URL="redis://127.0.0.1:9/0",
        JWT_SECRET="test-secret",
        WEBHOOK_SECRET="test-webhook-secret",
        DRAVEN_CONFIG_KEY="",
    )
    app2 = create_app()
    app2.dependency_overrides[get_settings_dep] = lambda: no_key_settings
    app2.dependency_overrides[get_db_session] = _override_db
    client2 = TestClient(app2)

    user = _register("draven_dave")
    r = client2.put(
        "/api/v1/draven/provider",
        json={"provider": "anthropic", "api_key": "sk-should-never-store"},
        headers=_headers(user["token"]),
    )
    assert r.status_code == 400, r.text
    assert "DRAVEN_CONFIG_KEY" in r.json()["detail"]

    # stub provider needs no key -> allowed even without DRAVEN_CONFIG_KEY
    r = client2.put(
        "/api/v1/draven/provider", json={"provider": "stub"}, headers=_headers(user["token"])
    )
    assert r.status_code == 200, r.text

    # and nothing secret was persisted for this business
    db = TestingSession()
    try:
        row = (
            db.query(DravenProviderConfig)
            .filter(DravenProviderConfig.business_id == _business_id(user))
            .one()
        )
        assert row.provider == "stub"
        assert row.api_key_enc is None
    finally:
        db.close()


def test_provider_put_rejects_bad_input(user_a):
    h = _headers(user_a["token"])
    r = client.put(
        "/api/v1/draven/provider", json={"provider": "not_a_provider"}, headers=h
    )
    assert r.status_code == 422
    r = client.put(
        "/api/v1/draven/provider", json={"provider": "anthropic"}, headers=h  # no api_key
    )
    assert r.status_code == 422
    r = client.put(
        "/api/v1/draven/provider",
        json={
            "provider": "openai_compatible",
            "base_url": "ftp://evil.example/x",
            "model": "m",
        },
        headers=h,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# emergency stop
# ---------------------------------------------------------------------------


def test_stop_blocks_tool_execution(user_a):
    _seed_tenant_a(user_a)

    r = client.post("/api/v1/draven/stop", headers=_headers(user_a["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["stopped"] is True

    r = client.post(
        "/api/v1/draven/chat",
        json={"message": "business summary", "history": []},
        headers=_headers(user_a["token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "stopped"
    assert body["tools_used"] == []
    assert "stopped" in body["reply"].lower()

    # no tool audit rows were written for the blocked chat
    db = TestingSession()
    try:
        count = (
            db.query(DravenToolRun)
            .filter(DravenToolRun.business_id == _business_id(user_a))
            .count()
        )
        assert count == 0
    finally:
        db.close()


def test_stop_requires_auth():
    assert client.post("/api/v1/draven/stop").status_code == 401


# ---------------------------------------------------------------------------
# ElevenLabs TTS tests (HTTP layer mocked via monkeypatch — no real network)
# ---------------------------------------------------------------------------

TTS_TEST_KEY = "elevenlabs-test-key-do-not-leak"

_FAKE_VOICES_PAYLOAD = {
    "voices": [
        {
            "voice_id": "21m00Tcm4TlvDq8ikWAM",
            "name": "Rachel",
            "category": "premade",
            "labels": {"language": "en", "accent": "american"},
        },
        {
            "voice_id": "AZnzlk1XvdvUeBnXmlld",
            "name": "Domi",
            "category": "premade",
            "labels": {"language": "en", "accent": "american"},
        },
    ]
}
_FAKE_AUDIO = b"\xff\xfb\x90\x00" + b"\x00" * 64  # fake mp3 frame bytes


class _FakeElevenLabsResp:
    def __init__(self, status_code: int, json_data=None, content: bytes = b""):
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.text = "" if json_data is None else str(json_data)[:200]

    def json(self):
        return self._json


def _fake_elevenlabs_factory(seen: dict, fail_voices: bool = False):
    """Monkeypatch target replacing draven_router._elevenlabs_api.

    Records every call in ``seen`` (including the api_key it received) and
    serves canned voices / audio responses. The key is asserted server-side
    only — it must never surface in any HTTP response body.
    """

    async def _fake(method, path, api_key, *, json_body=None, timeout=15.0):
        seen.setdefault("calls", []).append(
            {"method": method, "path": path, "json_body": json_body}
        )
        seen["api_key"] = api_key
        assert api_key == TTS_TEST_KEY  # real decrypted key flows server-side
        if path == "/v1/voices":
            if fail_voices:
                raise RuntimeError("upstream exploded")
            return _FakeElevenLabsResp(200, _FAKE_VOICES_PAYLOAD)
        if path.startswith("/v1/text-to-speech/"):
            assert json_body and json_body["text"]
            return _FakeElevenLabsResp(200, None, _FAKE_AUDIO)
        raise AssertionError(f"unexpected ElevenLabs path: {path}")

    return _fake


def _configure_elevenlabs(user: dict, monkeypatch, seen: dict, **kw) -> None:
    monkeypatch.setattr(
        draven_router, "_elevenlabs_api", _fake_elevenlabs_factory(seen, **kw)
    )
    r = client.put(
        "/api/v1/draven/provider",
        headers=_headers(user["token"]),
        json={"provider": "elevenlabs", "api_key": TTS_TEST_KEY},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tts"]["provider"] == "elevenlabs"
    assert body["tts"]["configured"] is True
    # Key material must not appear in the PUT response.
    assert TTS_TEST_KEY not in r.text


def _tts_business(monkeypatch, seen: dict, **kw) -> dict:
    user = _register("tts")
    _configure_elevenlabs(user, monkeypatch, seen, **kw)
    return user


def test_tts_voices_lists_live_voices(monkeypatch):
    seen: dict = {}
    user = _tts_business(monkeypatch, seen)
    r = client.get("/api/v1/draven/tts/voices", headers=_headers(user["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert len(body["voices"]) == 2
    first = body["voices"][0]
    assert first["voice_id"] == "21m00Tcm4TlvDq8ikWAM"
    assert first["name"] == "Rachel"
    assert first["language"] == "en"
    assert first["category"] == "premade"
    assert set(first) == {"voice_id", "name", "language", "category"}
    assert TTS_TEST_KEY not in r.text  # key never in responses
    # second call is served from the 1h in-process cache (no new HTTP)
    calls_before = len(seen["calls"])
    r2 = client.get("/api/v1/draven/tts/voices", headers=_headers(user["token"]))
    assert r2.status_code == 200
    assert len(seen["calls"]) == calls_before


def test_tts_voices_not_configured():
    user = _register("ttsplain")
    r = client.get("/api/v1/draven/tts/voices", headers=_headers(user["token"]))
    assert r.status_code == 200, r.text
    assert r.json() == {"configured": False, "voices": []}


def test_tts_voices_upstream_failure_is_502_not_fabricated(monkeypatch):
    seen: dict = {}
    user = _tts_business(monkeypatch, seen, fail_voices=True)
    r = client.get("/api/v1/draven/tts/voices", headers=_headers(user["token"]))
    assert r.status_code == 502, r.text
    # 502 carries only an error detail — never a fabricated voice list.
    assert "voices" not in r.json()


def test_tts_speak_success_returns_audio_and_audits_cost(monkeypatch):
    seen: dict = {}
    user = _tts_business(monkeypatch, seen)
    r = client.post(
        "/api/v1/draven/tts/speak",
        headers=_headers(user["token"]),
        json={
            "text": "Hello from Draven",
            "voice_id": "21m00Tcm4TlvDq8ikWAM",
        },
    )
    assert r.status_code == 200, r.text
    # default is MP3 HD: universal device compatibility, passes the raw
    # payload straight through (no WAV container)
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content == _FAKE_AUDIO
    # default model + default format applied server-side
    tts_calls = [c for c in seen["calls"] if c["path"].startswith("/v1/text-to-speech/")]
    assert len(tts_calls) == 1
    assert tts_calls[0]["path"] == "/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM"
    assert tts_calls[0]["json_body"]["model_id"] == "eleven_multilingual_v2"
    assert tts_calls[0]["json_body"]["output_format"] == "mp3_44100_192"

    db = TestingSession()
    try:
        row = (
            db.query(DravenToolRun)
            .filter(
                DravenToolRun.business_id == _business_id(user),
                DravenToolRun.tool == "draven.tts_speak",
            )
            .order_by(DravenToolRun.created_at.desc())
            .first()
        )
        assert row is not None
        assert row.status == "ok"
        assert row.input["chars"] == len("Hello from Draven")
        assert row.input["voice_id"] == "21m00Tcm4TlvDq8ikWAM"
        # raw text is not stored in the audit log (PII)
        assert "Hello from Draven" not in (row.output_summary or "")
        assert "estimated_cost_usd" in (row.output_summary or "")
        assert TTS_TEST_KEY not in (row.output_summary or "")
        assert TTS_TEST_KEY not in str(row.input)
    finally:
        db.close()


def test_tts_speak_mp3_format_returns_mpeg(monkeypatch):
    seen: dict = {}
    user = _tts_business(monkeypatch, seen)
    r = client.post(
        "/api/v1/draven/tts/speak",
        headers=_headers(user["token"]),
        json={
            "text": "Hello from Draven",
            "voice_id": "21m00Tcm4TlvDq8ikWAM",
            "output_format": "mp3_44100_192",
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content == _FAKE_AUDIO  # MP3 passes through unwrapped
    tts_calls = [c for c in seen["calls"] if c["path"].startswith("/v1/text-to-speech/")]
    assert tts_calls[0]["json_body"]["output_format"] == "mp3_44100_192"


def test_tts_speak_rejects_unknown_output_format(monkeypatch):
    seen: dict = {}
    user = _tts_business(monkeypatch, seen)
    r = client.post(
        "/api/v1/draven/tts/speak",
        headers=_headers(user["token"]),
        json={
            "text": "hi",
            "voice_id": "21m00Tcm4TlvDq8ikWAM",
            "output_format": "flac_96000",
        },
    )
    assert r.status_code == 422, r.text
    assert "output_format" in r.json()["detail"]
    # rejected before any upstream call
    assert not [
        c for c in seen.get("calls", []) if c["path"].startswith("/v1/text-to-speech/")
    ]


def test_tts_speak_rejects_unknown_voice(monkeypatch):
    seen: dict = {}
    user = _tts_business(monkeypatch, seen)
    r = client.post(
        "/api/v1/draven/tts/speak",
        headers=_headers(user["token"]),
        json={"text": "hi", "voice_id": "AAAAAAAAAAAAAAAAAAAA"},
    )
    assert r.status_code == 400, r.text
    assert "unknown voice_id" in r.json()["detail"]
    # no upstream TTS call was proxied for the unknown id
    assert not [
        c for c in seen["calls"] if c["path"].startswith("/v1/text-to-speech/")
    ]


def test_tts_speak_rejects_bad_voice_id_format(monkeypatch):
    seen: dict = {}
    user = _tts_business(monkeypatch, seen)
    for bad in ["../../etc/passwd", "voice id!", "x" * 65, ""]:
        r = client.post(
            "/api/v1/draven/tts/speak",
            headers=_headers(user["token"]),
            json={"text": "hi", "voice_id": bad},
        )
        assert r.status_code in (400, 422), (bad, r.text)
    # format rejection happens before any upstream call is attempted
    assert not [
        c
        for c in seen.get("calls", [])
        if c["path"].startswith("/v1/text-to-speech/")
    ]


def test_tts_speak_rejects_text_too_long(monkeypatch):
    seen: dict = {}
    user = _tts_business(monkeypatch, seen)
    r = client.post(
        "/api/v1/draven/tts/speak",
        headers=_headers(user["token"]),
        json={"text": "x" * 2001, "voice_id": "21m00Tcm4TlvDq8ikWAM"},
    )
    assert r.status_code == 422, r.text
    r = client.post(
        "/api/v1/draven/tts/speak",
        headers=_headers(user["token"]),
        json={"text": "   ", "voice_id": "21m00Tcm4TlvDq8ikWAM"},
    )
    assert r.status_code == 422, r.text


def test_tts_speak_not_configured():
    user = _register("ttsnone")
    r = client.post(
        "/api/v1/draven/tts/speak",
        headers=_headers(user["token"]),
        json={"text": "hi", "voice_id": "21m00Tcm4TlvDq8ikWAM"},
    )
    assert r.status_code == 400, r.text
    assert "not configured" in r.json()["detail"]


def test_tts_rate_limit(monkeypatch):
    seen: dict = {}
    user = _tts_business(monkeypatch, seen)
    payload = {"text": "pace yourself", "voice_id": "21m00Tcm4TlvDq8ikWAM"}
    statuses = []
    for _ in range(21):
        r = client.post(
            "/api/v1/draven/tts/speak",
            headers=_headers(user["token"]),
            json=payload,
        )
        statuses.append(r.status_code)
    assert statuses[:20] == [200] * 20
    assert statuses[20] == 429


def test_tts_host_is_hard_allowlisted():
    # The router must only ever talk to api.elevenlabs.io — no custom URLs.
    assert draven_router._ELEVENLABS_BASE == "https://api.elevenlabs.io"
    assert draven_router._ELEVENLABS_VOICES_PATH == "/v1/voices"
    assert "{voice_id}" in draven_router._ELEVENLABS_TTS_PATH
    assert not draven_router._ELEVENLABS_TTS_PATH.startswith("http")


def test_tts_provider_status_section(monkeypatch, caplog):
    seen: dict = {}
    user = _tts_business(monkeypatch, seen)
    # populate the in-process voice cache first
    rv = client.get("/api/v1/draven/tts/voices", headers=_headers(user["token"]))
    assert rv.status_code == 200
    with caplog.at_level("INFO"):
        r = client.get("/api/v1/draven/provider", headers=_headers(user["token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tts"]["provider"] == "elevenlabs"
    assert body["tts"]["configured"] is True
    assert body["tts"]["voice_count"] == 2  # from the cached voice list
    assert TTS_TEST_KEY not in r.text
    # nothing in the captured logs carries the key either
    assert TTS_TEST_KEY not in caplog.text


def test_tts_clear_with_none(monkeypatch):
    seen: dict = {}
    user = _tts_business(monkeypatch, seen)
    r = client.put(
        "/api/v1/draven/provider",
        headers=_headers(user["token"]),
        json={"provider": "stub", "tts_provider": "none"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tts"]["provider"] == "none"
    assert r.json()["tts"]["configured"] is False
    r = client.get("/api/v1/draven/tts/voices", headers=_headers(user["token"]))
    assert r.json() == {"configured": False, "voices": []}
