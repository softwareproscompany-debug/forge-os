"""Draven voice-parity tests: the voice AI can do anything the app can do.

Covers the 50 parity tools added to the typed registry:
  execution of low-risk tools against real data, tenant isolation
  (two businesses, no cross-tenant reads or writes), and approval
  gating for medium/high-risk tools (no side effects, approval_required
  with an actionable description).

Calls ``execute_tool`` directly (no HTTP) with a fresh SQLite DB.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, datetime, timezone

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forge_db.models import (
    AffiliateLink,
    AffiliateProgram,
    Asset,
    AssetKind,
    AssetStatus,
    Base,
    BrandKit,
    Business,
    Campaign,
    CampaignEnrollment,
    CampaignStatus,
    CampaignStep,
    Channel,
    Contact,
    ContentPlan,
    DravenToolRun,
    Event,
    PlanStatus,
    Send,
    SendStatus,
    Template,
    User,
    UserRole,
)

from app.draven_tools import TOOLS, execute_tool, route_intent

TEST_DB = "/tmp/forgeos_draven_parity_test.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

engine = create_engine(f"sqlite:///{TEST_DB}", connect_args={"check_same_thread": False})
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)


def _make_tenant(prefix: str) -> tuple[uuid.UUID, uuid.UUID]:
    db = TestingSession()
    b = Business(
        name=f"{prefix} Co",
        slug=f"{prefix}-co-{uuid.uuid4().hex[:8]}",
        timezone="UTC",
    )
    db.add(b)
    db.flush()
    u = User(
        email=f"{prefix}@example.com",
        password_hash="x",
        full_name=prefix.title(),
        business_id=b.id,
        role=UserRole.owner,
        is_active=True,
    )
    db.add(u)
    db.commit()
    ids = (u.id, b.id)
    db.close()
    return ids


USER_A, BIZ_A = _make_tenant("parity_alice")
USER_B, BIZ_B = _make_tenant("parity_bob")


def _user(uid: uuid.UUID) -> User:
    db = TestingSession()
    u = db.query(User).filter(User.id == uid).one()
    db.expunge(u)
    db.close()
    return u


def run(tool_id: str, uid: uuid.UUID, raw: dict | None = None) -> dict:
    user = _user(uid)
    db = TestingSession()
    try:
        return asyncio.run(execute_tool(tool_id, db, user, raw or {}))
    finally:
        db.close()


def _db():
    return TestingSession()


# ---------------------------------------------------------------------------
# registry shape
# ---------------------------------------------------------------------------


def test_registry_has_all_parity_tools():
    assert len(TOOLS) == 67
    for tid in (
        "draven.brandkit_list", "draven.brandkit_get", "draven.brandkit_create",
        "draven.brandkit_update",
        "draven.contacts_list", "draven.contact_get", "draven.contact_create",
        "draven.contact_update", "draven.contact_consent", "draven.contact_delete",
        "draven.template_list", "draven.template_get", "draven.template_create",
        "draven.template_update", "draven.template_preview", "draven.template_delete",
        "draven.assets_list", "draven.asset_get", "draven.asset_generate",
        "draven.asset_submit", "draven.asset_versions", "draven.asset_reject",
        "draven.campaign_get", "draven.campaign_create", "draven.campaign_update",
        "draven.campaign_steps_add", "draven.campaign_step_update",
        "draven.campaign_launch", "draven.campaign_enrollments",
        "draven.autopilot_update", "draven.autopilot_plan_approve",
        "draven.affiliate_programs_list", "draven.affiliate_program_get",
        "draven.affiliate_program_create", "draven.affiliate_program_update",
        "draven.affiliate_program_delete",
        "draven.affiliate_links_list", "draven.affiliate_link_get",
        "draven.affiliate_link_create", "draven.affiliate_link_update",
        "draven.affiliate_link_delete", "draven.affiliate_earnings",
        "draven.analytics_funnel", "draven.analytics_weekly_summary",
        "draven.interview_start", "draven.interview_status",
        "draven.ops_activity", "draven.ops_brain",
        "draven.business_get", "draven.business_update",
    ):
        assert tid in TOOLS, tid


def test_parity_risk_levels():
    risks = {t.id: t.risk for t in TOOLS.values()}
    for tid in (
        "draven.contact_delete", "draven.template_delete", "draven.asset_reject",
        "draven.campaign_launch", "draven.affiliate_program_delete",
        "draven.affiliate_link_delete",
    ):
        assert risks[tid] == "high", tid
    assert risks["draven.asset_generate"] == "medium"
    assert risks["draven.autopilot_plan_approve"] == "medium"
    for tid, tool in TOOLS.items():
        assert tool.risk in ("low", "medium", "high"), tid


# ---------------------------------------------------------------------------
# brand kits
# ---------------------------------------------------------------------------


def test_brandkit_crud_and_isolation():
    r = run("draven.brandkit_create", USER_A, {
        "name": "Voice Kit", "voice_description": "bold", "tone_tags": ["bold"],
    })
    assert r["status"] == "ok", r
    kit_id = r["output"]["brand_kit"]["id"]
    assert r["output"]["brand_kit"]["version"] == 1

    r = run("draven.brandkit_get", USER_A, {"brandkit_id": kit_id})
    assert r["status"] == "ok"
    assert r["output"]["brand_kit"]["voice_description"] == "bold"

    r = run("draven.brandkit_update", USER_A, {
        "brandkit_id": kit_id, "primary_color": "#ff0000",
    })
    assert r["status"] == "ok"
    assert r["output"]["brand_kit"]["primary_color"] == "#ff0000"
    assert r["output"]["brand_kit"]["version"] == 2

    r = run("draven.brandkit_list", USER_A, {})
    assert r["status"] == "ok" and r["output"]["count"] >= 1

    # tenant isolation: bob cannot read alice's kit
    r = run("draven.brandkit_get", USER_B, {"brandkit_id": kit_id})
    assert r["status"] == "error"
    r = run("draven.brandkit_update", USER_B, {"brandkit_id": kit_id, "name": "Hijack"})
    assert r["status"] == "error"
    db = _db()
    try:
        kit = db.query(BrandKit).filter(BrandKit.id == uuid.UUID(kit_id)).one()
        assert kit.name == "Voice Kit"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------


def test_contact_crud_consent_and_delete_gating():
    r = run("draven.contact_create", USER_A, {
        "email": "jane@example.com", "first_name": "Jane", "tags": ["vip"],
    })
    assert r["status"] == "ok", r
    cid = r["output"]["contact"]["id"]

    r = run("draven.contact_consent", USER_A, {
        "contact_id": cid, "channel": "email", "granted": True,
    })
    assert r["status"] == "ok"
    assert r["output"]["contact"]["consent_email"] is True

    r = run("draven.contact_update", USER_A, {
        "contact_id": cid, "phone": "+15550001111",
    })
    assert r["status"] == "ok"
    assert r["output"]["contact"]["phone"] == "+15550001111"

    r = run("draven.contacts_list", USER_A, {})
    assert r["status"] == "ok" and r["output"]["count"] >= 1

    # delete is high-risk: approval request, no side effect
    r = run("draven.contact_delete", USER_A, {"contact_id": cid})
    assert r["status"] == "approval_required"
    assert r["risk"] == "high"
    assert "Delete contact" in r["approval"]["description"]
    db = _db()
    try:
        assert db.query(Contact).filter(Contact.id == uuid.UUID(cid)).one() is not None
    finally:
        db.close()

    # isolation
    r = run("draven.contact_get", USER_B, {"contact_id": cid})
    assert r["status"] == "error"


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------


def test_template_create_preview_delete_gating():
    r = run("draven.template_create", USER_A, {
        "name": "Welcome Email",
        "channel": "email",
        "subject_template": "Welcome, {{ first_name }}!",
        "body_template": "Hi {{ first_name }}, thanks for joining.",
        "variables": ["first_name"],
    })
    assert r["status"] == "ok", r
    tid = r["output"]["template"]["id"]

    # invalid Jinja is rejected at write time, not stored
    r = run("draven.template_create", USER_A, {
        "name": "Broken", "channel": "email", "body_template": "Hi {{ ",
    })
    assert r["status"] == "error"
    assert "invalid template syntax" in r["error"]

    # preview uses the real Jinja renderer
    r = run("draven.template_preview", USER_A, {
        "template_id": tid, "variables": {"first_name": "Jane"},
    })
    assert r["status"] == "ok", r
    assert r["output"]["subject"] == "Welcome, Jane!"
    assert "Jane" in r["output"]["body"]

    # preview with a missing variable fails honestly (StrictUndefined)
    r = run("draven.template_preview", USER_A, {"template_id": tid, "variables": {}})
    assert r["status"] == "error"

    # delete is high-risk: approval request, template still present
    r = run("draven.template_delete", USER_A, {"template_id": tid})
    assert r["status"] == "approval_required"
    assert r["risk"] == "high"
    db = _db()
    try:
        assert db.query(Template).filter(Template.id == uuid.UUID(tid)).one() is not None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------


def test_asset_list_get_submit_and_generate_gating():
    db = _db()
    try:
        asset = Asset(
            business_id=BIZ_A, kind=AssetKind.email_copy, title="Draft One",
            body="hello", status=AssetStatus.draft, created_by=USER_A,
        )
        db.add(asset)
        db.commit()
        aid = str(asset.id)
    finally:
        db.close()

    r = run("draven.assets_list", USER_A, {"status": "draft"})
    assert r["status"] == "ok"
    assert any(a["id"] == aid for a in r["output"]["assets"])

    r = run("draven.asset_get", USER_A, {"asset_id": aid})
    assert r["status"] == "ok"
    assert r["output"]["asset"]["body_preview"] == "hello"

    # submit executes the real state machine: draft -> in_review
    r = run("draven.asset_submit", USER_A, {"asset_id": aid})
    assert r["status"] == "ok", r
    assert r["output"]["asset"]["status"] == "in_review"
    db = _db()
    try:
        assert db.query(Asset).filter(Asset.id == uuid.UUID(aid)).one().status == AssetStatus.in_review
    finally:
        db.close()

    # generate is medium-risk (LLM spend): validated, then approval — no row created
    before = run("draven.assets_list", USER_A, {})["output"]["count"]
    r = run("draven.asset_generate", USER_A, {
        "kind": "email_copy", "title": "Voice Draft", "prompt": "write a welcome email",
    })
    assert r["status"] == "approval_required", r
    assert r["risk"] == "medium"
    assert "worker job" in r["approval"]["description"]
    after = run("draven.assets_list", USER_A, {})["output"]["count"]
    assert after == before

    # reject is high-risk: approval request, status unchanged
    r = run("draven.asset_reject", USER_A, {"asset_id": aid, "reason": "off-brand"})
    assert r["status"] == "approval_required"
    assert r["risk"] == "high"
    db = _db()
    try:
        assert db.query(Asset).filter(Asset.id == uuid.UUID(aid)).one().status == AssetStatus.in_review
    finally:
        db.close()

    # isolation
    r = run("draven.asset_get", USER_B, {"asset_id": aid})
    assert r["status"] == "error"


def test_asset_versions():
    r = run("draven.asset_get", USER_A, {"asset_title": "Draft One"})
    assert r["status"] == "ok"
    r = run("draven.asset_versions", USER_A, {"asset_id": r["output"]["asset"]["id"]})
    assert r["status"] == "ok"
    assert r["output"]["count"] >= 1


# ---------------------------------------------------------------------------
# campaigns
# ---------------------------------------------------------------------------


def _seed_campaign_with_step(uid, biz):
    db = _db()
    try:
        tpl = Template(
            business_id=biz, name="Step Tpl", channel=Channel.email,
            body_template="Hi", variables=[],
        )
        camp = Campaign(
            business_id=biz, name="Parity Campaign", status=CampaignStatus.draft,
            created_by=uid,
        )
        db.add_all([tpl, camp])
        db.flush()
        step = CampaignStep(
            campaign_id=camp.id, position=0, channel=Channel.email,
            template_id=tpl.id, delay_hours=24,
        )
        contact = Contact(business_id=biz, email="enr@example.com")
        db.add_all([step, contact])
        db.flush()
        enr = CampaignEnrollment(campaign_id=camp.id, contact_id=contact.id)
        db.add(enr)
        db.commit()
        return str(camp.id), str(tpl.id)
    finally:
        db.close()


def test_campaign_full_lifecycle_and_launch_gating():
    r = run("draven.campaign_create", USER_A, {
        "name": "Voice Campaign", "description": "from voice",
    })
    assert r["status"] == "ok", r
    assert r["output"]["campaign"]["status"] == "draft"
    cid = r["output"]["campaign"]["id"]

    # add a step referencing an owned template
    tpl = run("draven.template_create", USER_A, {
        "name": "Voice Tpl", "channel": "email", "body_template": "Hello {{ x }}",
    })["output"]["template"]["id"]
    r = run("draven.campaign_steps_add", USER_A, {
        "campaign_id": cid,
        "steps": [{"channel": "email", "template_id": tpl, "delay_hours": 48}],
    })
    assert r["status"] == "ok", r
    assert r["output"]["added"] == 1
    step_id = r["output"]["steps"][0]["id"]

    # unowned template ref is rejected
    r = run("draven.campaign_steps_add", USER_A, {
        "campaign_id": cid,
        "steps": [{"channel": "email", "template_id": str(uuid.uuid4())}],
    })
    assert r["status"] == "error"

    r = run("draven.campaign_step_update", USER_A, {
        "campaign_id": cid, "step_id": step_id, "delay_hours": 72,
    })
    assert r["status"] == "ok"
    assert r["output"]["step"]["delay_hours"] == 72

    r = run("draven.campaign_get", USER_A, {"campaign_id": cid})
    assert r["status"] == "ok"
    assert len(r["output"]["steps"]) == 1

    # launch is high-risk: readiness validated, then approval — still draft
    r = run("draven.campaign_launch", USER_A, {"campaign_id": cid})
    assert r["status"] == "approval_required", r
    assert r["risk"] == "high"
    assert "Launch campaign" in r["approval"]["description"]
    db = _db()
    try:
        assert db.query(Campaign).filter(Campaign.id == uuid.UUID(cid)).one().status == CampaignStatus.draft
    finally:
        db.close()

    # unlaunchable campaign (unapproved asset on a step) errors honestly
    db = _db()
    try:
        bad_asset = Asset(
            business_id=BIZ_A, kind=AssetKind.email_copy, title="Unapproved",
            body="x", status=AssetStatus.draft, created_by=USER_A,
        )
        bad_camp = Campaign(
            business_id=BIZ_A, name="Blocked Campaign",
            status=CampaignStatus.draft, created_by=USER_A,
        )
        db.add_all([bad_asset, bad_camp])
        db.flush()
        db.add(CampaignStep(
            campaign_id=bad_camp.id, position=0, channel=Channel.email,
            asset_id=bad_asset.id,
        ))
        db.commit()
        blocked_id = str(bad_camp.id)
    finally:
        db.close()
    r = run("draven.campaign_launch", USER_A, {"campaign_id": blocked_id})
    assert r["status"] == "error"
    assert "not approved" in r["error"]

    # isolation
    r = run("draven.campaign_get", USER_B, {"campaign_id": cid})
    assert r["status"] == "error"


def test_campaign_enrollments():
    cid, _ = _seed_campaign_with_step(USER_A, BIZ_A)
    r = run("draven.campaign_enrollments", USER_A, {"campaign_id": cid})
    assert r["status"] == "ok", r
    assert r["output"]["count"] == 1
    assert r["output"]["enrollments"][0]["status"] == "active"


# ---------------------------------------------------------------------------
# autopilot
# ---------------------------------------------------------------------------


def test_autopilot_update_and_plan_approve_gating():
    r = run("draven.autopilot_update", USER_A, {
        "auto_approve": True, "daily_send_cap": 100,
    })
    assert r["status"] == "ok", r
    assert r["output"]["settings"]["auto_approve"] is True
    assert r["output"]["settings"]["daily_send_cap"] == 100

    db = _db()
    try:
        plan = ContentPlan(
            business_id=BIZ_A, week_start=date(2026, 10, 12),
            status=PlanStatus.draft,
            items=[{"kind": "email_copy", "channel": "email", "day": 1,
                    "title": "T", "brief": "B"}],
        )
        db.add(plan)
        db.commit()
        pid = str(plan.id)
    finally:
        db.close()

    # medium-risk: validated draft, then approval — plan stays draft
    r = run("draven.autopilot_plan_approve", USER_A, {"plan_id": pid})
    assert r["status"] == "approval_required", r
    assert r["risk"] == "medium"
    assert "scheduled" in r["approval"]["description"]
    db = _db()
    try:
        assert db.query(ContentPlan).filter(ContentPlan.id == uuid.UUID(pid)).one().status == PlanStatus.draft
        assert db.query(Campaign).filter(
            Campaign.business_id == BIZ_A, Campaign.name.like("Autopilot%")
        ).count() == 0
    finally:
        db.close()

    # non-draft plan is rejected, not approved
    db = _db()
    try:
        db.query(ContentPlan).filter(ContentPlan.id == uuid.UUID(pid)).one().status = PlanStatus.approved
        db.commit()
    finally:
        db.close()
    r = run("draven.autopilot_plan_approve", USER_A, {"plan_id": pid})
    assert r["status"] == "error"


# ---------------------------------------------------------------------------
# affiliates
# ---------------------------------------------------------------------------


def test_affiliate_crud_earnings_and_delete_gating():
    r = run("draven.affiliate_program_create", USER_A, {
        "name": "Amazon Associates", "network": "amazon",
        "default_commission_pct": 4.5,
    })
    assert r["status"] == "ok", r
    prog_id = r["output"]["program"]["id"]

    r = run("draven.affiliate_program_update", USER_A, {
        "program_id": prog_id, "cookie_days": 30,
    })
    assert r["status"] == "ok"
    assert r["output"]["program"]["cookie_days"] == 30

    r = run("draven.affiliate_link_create", USER_A, {
        "program_id": prog_id, "label": "Espresso Machine",
        "slug": "espresso-pro", "destination_url": "https://example.com/dp/1?tag=x",
    })
    assert r["status"] == "ok", r
    link_id = r["output"]["link"]["id"]
    assert r["output"]["link"]["utm_source"] == "forgeos"

    # duplicate slug rejected
    r = run("draven.affiliate_link_create", USER_A, {
        "program_id": prog_id, "label": "Dup",
        "slug": "espresso-pro", "destination_url": "https://example.com/2",
    })
    assert r["status"] == "error"

    # non-absolute destination rejected
    r = run("draven.affiliate_link_create", USER_A, {
        "program_id": prog_id, "label": "Bad",
        "slug": "bad-link", "destination_url": "not-a-url",
    })
    assert r["status"] == "error"

    # seed real click + conversion events, then check honest aggregation
    db = _db()
    try:
        now = datetime.now(timezone.utc)
        for _ in range(6):
            db.add(Event(business_id=BIZ_A, kind="affiliate_clicked",
                         payload={"link_id": link_id, "program_id": prog_id}))
        db.add(Event(business_id=BIZ_A, kind="affiliate_converted",
                     payload={"link_id": link_id, "program_id": prog_id,
                              "order_value_usd": "100.00", "commission_usd": "4.50"}))
        db.commit()
    finally:
        db.close()
    r = run("draven.affiliate_earnings", USER_A, {"days": 30})
    assert r["status"] == "ok", r
    totals = r["output"]["totals"]
    assert totals["clicks"] == 6
    assert totals["conversions"] == 1
    assert abs(totals["earnings_usd"] - 4.5) < 1e-6

    # deletes are high-risk: approval requests, rows still present
    r = run("draven.affiliate_link_delete", USER_A, {"link_id": link_id})
    assert r["status"] == "approval_required" and r["risk"] == "high"
    r = run("draven.affiliate_program_delete", USER_A, {"program_id": prog_id})
    assert r["status"] == "approval_required" and r["risk"] == "high"
    assert "cascade" in r["approval"]["description"]
    db = _db()
    try:
        assert db.query(AffiliateProgram).filter(AffiliateProgram.id == uuid.UUID(prog_id)).one() is not None
        assert db.query(AffiliateLink).filter(AffiliateLink.id == uuid.UUID(link_id)).one() is not None
    finally:
        db.close()

    # isolation
    r = run("draven.affiliate_program_get", USER_B, {"program_id": prog_id})
    assert r["status"] == "error"


# ---------------------------------------------------------------------------
# analytics / interview / ops / business
# ---------------------------------------------------------------------------


def test_analytics_funnel_and_weekly_summary():
    cid, _ = _seed_campaign_with_step(USER_A, BIZ_A)
    db = _db()
    try:
        step = db.query(CampaignStep).filter(CampaignStep.campaign_id == uuid.UUID(cid)).one()
        contact = db.query(Contact).filter(Contact.business_id == BIZ_A).first()
        now = datetime.now(timezone.utc)
        db.add(Send(
            business_id=BIZ_A, campaign_id=uuid.UUID(cid), step_id=step.id,
            contact_id=contact.id, channel=Channel.email, to_address="e@x.com",
            body="b", status=SendStatus.delivered, opened_at=now,
        ))
        db.commit()
    finally:
        db.close()
    r = run("draven.analytics_funnel", USER_A, {"campaign_id": cid})
    assert r["status"] == "ok", r
    items = r["output"]["funnel"]["items"]
    assert len(items) == 1
    assert items[0]["sent"] == 1 and items[0]["opened"] == 1

    r = run("draven.analytics_weekly_summary", USER_A, {})
    assert r["status"] == "ok"
    assert r["output"]["found"] is False  # no summary cut in this fresh DB


def test_interview_start_and_status():
    r = run("draven.interview_start", USER_A, {})
    assert r["status"] == "ok", r
    assert r["output"]["question_index"] == 0
    assert r["output"]["total_questions"] == 7
    assert r["output"]["question"]  # real first Card 0 question

    r = run("draven.interview_status", USER_A, {})
    assert r["status"] == "ok"
    assert r["output"]["found"] is True
    assert r["output"]["status"] == "active"

    r = run("draven.interview_status", USER_B, {})
    assert r["status"] == "ok"
    assert r["output"]["found"] is False  # tenant-scoped: bob has none


def test_ops_activity_and_brain():
    r = run("draven.ops_activity", USER_A, {})
    assert r["status"] == "ok", r
    out = r["output"]
    assert "as_of" in out and "stages" in out and "counters" in out
    assert set(out["stages"]) == {"foundation", "origination", "reach", "growth", "evidence"}

    r = run("draven.ops_brain", USER_A, {})
    assert r["status"] == "ok", r
    assert r["output"]["layers"]
    assert r["output"]["link_count"] >= 0


def test_business_get_update():
    r = run("draven.business_get", USER_A, {})
    assert r["status"] == "ok"
    assert r["output"]["timezone"] == "UTC"

    r = run("draven.business_update", USER_A, {"timezone": "America/Chicago"})
    assert r["status"] == "ok"
    assert r["output"]["timezone"] == "America/Chicago"
    db = _db()
    try:
        assert db.query(Business).filter(Business.id == BIZ_A).one().timezone == "America/Chicago"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# audit + routing
# ---------------------------------------------------------------------------


def test_audit_rows_for_parity_tools():
    run("draven.brandkit_list", USER_A, {})
    db = _db()
    try:
        rows = db.query(DravenToolRun).filter(
            DravenToolRun.tool == "draven.brandkit_list",
            DravenToolRun.business_id == BIZ_A,
        ).all()
        assert len(rows) >= 1
        assert rows[-1].status == "ok" and rows[-1].risk == "low"
    finally:
        db.close()

    run("draven.campaign_launch", USER_A, {"campaign_name": "nope"})
    db = _db()
    try:
        rows = db.query(DravenToolRun).filter(
            DravenToolRun.tool == "draven.campaign_launch",
        ).all()
        assert rows[-1].status in ("approval_required", "error")
        assert rows[-1].risk == "high"
    finally:
        db.close()


def test_route_intent_parity():
    cases = {
        "create a brand kit named Acme Voice": "draven.brandkit_create",
        "show my brand kits": "draven.brandkit_list",
        "list my contacts": "draven.contacts_list",
        "delete the contact jane@example.com": "draven.contact_delete",
        "preview the welcome email template with first_name=Jane": "draven.template_preview",
        "generate an sms asset about the sale": "draven.asset_generate",
        "submit the draft one asset": "draven.asset_submit",
        "launch the welcome campaign": "draven.campaign_launch",
        "create a campaign called Q4 push": "draven.campaign_create",
        "show the funnel for the welcome campaign": "draven.analytics_funnel",
        "what is the weekly summary": "draven.analytics_weekly_summary",
        "start the brand interview": "draven.interview_start",
        "open mission control": "draven.ops_activity",
        "show me the knowledge graph brain": "draven.ops_brain",
        "approve the weekly plan": "draven.autopilot_plan_approve",
        "show affiliate earnings": "draven.affiliate_earnings",
        "create an affiliate program called PartnerCo": "draven.affiliate_program_create",
    }
    for message, expected in cases.items():
        routed = {tid for tid, _ in route_intent(message)}
        assert expected in routed, f"{message!r} -> {routed}"


def test_deterministic_reply_uses_real_parity_data():
    from app.routers.draven import _deterministic_reply

    r = run("draven.brandkit_create", USER_A, {"name": "Reply Kit"})
    reply = _deterministic_reply([r])
    assert "Reply Kit" in reply and "Brand kit" in reply

    r = run("draven.contact_delete", USER_A, {})
    # missing ref -> honest error, no invented approval
    assert r["status"] == "error"
    reply = _deterministic_reply([r])
    assert "ran into a problem" in reply
