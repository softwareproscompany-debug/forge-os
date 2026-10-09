"""Draven voice-parity tool pack: the tools that give the voice AI full app parity.

Split out of app/draven_tools.py (one module was approaching the GitHub push
single-file size cap). Everything here registers into the SAME TOOLS registry
via the shared _register imported below — import app.draven_tools (which
imports this module last), not this module directly.
"""

from app.draven_tools import (
    Any, Asset, AssetRefInput, AssetStatus, AutopilotSettings, BaseModel,
    Business, Campaign, CampaignRefInput, CampaignStatus, Contact, EmptyInput,
    Field, Literal, MarketOpportunity, Session, ToolDef, User,
    _approval_required, _error, _iso, _ok, _register, _resolve_asset,
    _resolve_campaign, datetime, field, func, timedelta, timezone, uuid,
)

# ---------------------------------------------------------------------------
# Market Intelligence tools (Draven Market Intelligence module)
# ---------------------------------------------------------------------------
#
# These tools wrap the deterministic research pipeline in
# app.market_intel. They never claim live searches that did not happen:
# every result carries connectors_used + evidence_gaps, and the chat reply
# states plainly when no live data source is configured.


class MarketResearchStartInput(BaseModel):
    target_month: int | str | None = Field(default=None)
    target_year: int | None = Field(default=None, ge=2020, le=2100)
    market: str | None = Field(default=None, max_length=32)
    category: str | None = Field(default=None, max_length=128)
    seed_terms: list[str] | None = Field(default=None, max_length=25)
    ticket_tier: str | None = Field(default=None, max_length=16)
    max_results: int = Field(default=10, ge=1, le=100)


class MarketResearchStatusInput(BaseModel):
    job_id: str | None = Field(default=None, description="Defaults to the latest job.")


class MarketTopOpportunitiesInput(BaseModel):
    limit: int = Field(default=5, ge=1, le=25)
    min_score: float | None = Field(default=None, ge=0, le=100)


def _market_settings():
    # Tools run outside the request dependency graph; settings come from
    # the environment (same source the API uses at startup).
    from app.core.config import get_settings

    return get_settings()


async def _market_research_start(
    db: Session, user: User, inp: MarketResearchStartInput
) -> dict[str, Any]:
    from app.market_intel.pipeline import normalize_params, run_research_job
    from forge_db.models import MarketResearchJob, ResearchJobStatus

    raw = {k: v for k, v in inp.model_dump().items() if v is not None}
    try:
        params, assumptions = normalize_params(raw)
    except ValueError as exc:
        return _error(str(exc))

    job = MarketResearchJob(
        business_id=user.business_id,
        user_id=user.id,
        params=params,
        assumptions=assumptions,
        status=ResearchJobStatus.queued,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    run_research_job(db, user, job, _market_settings())
    db.refresh(job)

    results = job.results or {}
    opps = (
        db.query(MarketOpportunity)
        .filter(MarketOpportunity.research_job_id == job.id)
        .order_by(MarketOpportunity.opportunity_score.desc().nullslast())
        .limit(5)
        .all()
    )
    return _ok(
        {
            "job_id": str(job.id),
            "status": job.status.value,
            "target": f"{params['target_month']:02d}/{params['target_year']}",
            "market": params["market"],
            "opportunity_count": results.get("opportunity_count", 0),
            "connectors_used": results.get("connectors_used", []),
            "evidence_gaps": results.get("evidence_gaps", []),
            "assumptions": assumptions,
            "top_opportunities": [
                {
                    "id": str(o.id),
                    "name": o.name,
                    "opportunity_score": o.opportunity_score,
                    "confidence_score": o.confidence_score,
                    "recommendation": o.recommendation.value,
                    "report_url": f"/market-intel/{o.id}",
                }
                for o in opps
            ],
        }
    )


async def _market_research_status(
    db: Session, user: User, inp: MarketResearchStatusInput
) -> dict[str, Any]:
    from forge_db.models import MarketResearchJob

    job = None
    if inp.job_id:
        try:
            jid = uuid.UUID(str(inp.job_id))
        except ValueError:
            return _error("job_id is not a valid UUID")
        job = (
            db.query(MarketResearchJob)
            .filter(
                MarketResearchJob.id == jid,
                MarketResearchJob.business_id == user.business_id,
            )
            .first()
        )
        if job is None:
            return _error("research job not found in your business")
    else:
        job = (
            db.query(MarketResearchJob)
            .filter(MarketResearchJob.business_id == user.business_id)
            .order_by(MarketResearchJob.created_at.desc())
            .first()
        )
        if job is None:
            return _ok({"found": False, "note": "no research jobs yet"})
    return _ok(
        {
            "found": True,
            "job_id": str(job.id),
            "status": job.status.value,
            "progress": job.progress,
            "results": job.results,
            "error": job.error,
        }
    )


async def _market_top_opportunities(
    db: Session, user: User, inp: MarketTopOpportunitiesInput
) -> dict[str, Any]:
    q = (
        db.query(MarketOpportunity)
        .filter(MarketOpportunity.business_id == user.business_id)
        .filter(MarketOpportunity.opportunity_score.is_not(None))
    )
    if inp.min_score is not None:
        q = q.filter(MarketOpportunity.opportunity_score >= inp.min_score)
    rows = (
        q.order_by(MarketOpportunity.opportunity_score.desc())
        .limit(inp.limit)
        .all()
    )
    return _ok(
        {
            "count": len(rows),
            "opportunities": [
                {
                    "id": str(o.id),
                    "name": o.name,
                    "category": o.category,
                    "opportunity_score": o.opportunity_score,
                    "confidence_score": o.confidence_score,
                    "recommendation": o.recommendation.value,
                    "report_url": f"/market-intel/{o.id}",
                }
                for o in rows
            ],
        }
    )


_register(
    ToolDef(
        id="market.research_start",
        description=(
            "Start a market-intelligence research run from natural-language "
            "params (target month/year, market, category or seed terms, ticket "
            "tier). Runs the deterministic pipeline and returns the job "
            "summary with top opportunities and evidence gaps."
        ),
        input_model=MarketResearchStartInput,
        risk="low",
        execute=_market_research_start,
    )
)
_register(
    ToolDef(
        id="market.research_status",
        description="Status/progress/results of a research job (defaults to the latest).",
        input_model=MarketResearchStatusInput,
        risk="low",
        execute=_market_research_status,
    )
)
_register(
    ToolDef(
        id="market.top_opportunities",
        description="Top-scoring product opportunities with report links.",
        input_model=MarketTopOpportunitiesInput,
        risk="low",
        execute=_market_top_opportunities,
    )
)


# ---------------------------------------------------------------------------
# Alpha workflow tools (lead-to-follow-up reliability slice)
# ---------------------------------------------------------------------------
#
# ``alpha.prepare_followup`` drafts and stages an approval but NEVER sends.
# ``alpha.approve_followup`` is high-risk: it returns an approval request and
# never executes — the actual approval happens through the Alpha approval
# queue (POST /alpha/runs/{id}/approve), which binds the approval to the
# exact payload digest.


class AlphaLeadIntakeInput(BaseModel):
    name: str | None = Field(default=None, max_length=256)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=64)
    company: str | None = Field(default=None, max_length=256)
    source: str = Field(default="draven", max_length=64)


class AlphaLeadRefInput(BaseModel):
    """Identify a lead by id or email fragment."""

    lead_id: str | None = None
    email: str | None = None


class AlphaRunRefInput(BaseModel):
    run_id: str | None = None
    lead_id: str | None = None
    email: str | None = None


class AlphaFollowupInput(BaseModel):
    run_id: str | None = None
    lead_id: str | None = None
    email: str | None = None
    subject: str = Field(max_length=512)
    body: str = Field(max_length=10000)


def _resolve_alpha_lead(
    db: Session, user: User, inp: AlphaLeadRefInput
):
    from forge_db.models import Lead as _Lead

    if inp.lead_id:
        try:
            lid = uuid.UUID(inp.lead_id)
        except ValueError:
            return None
        return (
            db.query(_Lead)
            .filter(_Lead.id == lid, _Lead.business_id == user.business_id)
            .first()
        )
    if inp.email:
        return (
            db.query(_Lead)
            .filter(
                _Lead.business_id == user.business_id,
                _Lead.email.ilike(f"%{inp.email}%"),
            )
            .order_by(_Lead.created_at.desc())
            .first()
        )
    return None


def _latest_alpha_run(db: Session, user: User, lead_id):
    from forge_db.models import WorkflowRun as _Run

    return (
        db.query(_Run)
        .filter(_Run.lead_id == lead_id, _Run.business_id == user.business_id)
        .order_by(_Run.created_at.desc())
        .first()
    )


def _resolve_alpha_run(
    db: Session, user: User, inp: AlphaRunRefInput
):
    from forge_db.models import WorkflowRun as _Run

    if inp.run_id:
        try:
            rid = uuid.UUID(inp.run_id)
        except ValueError:
            return None
        return (
            db.query(_Run)
            .filter(_Run.id == rid, _Run.business_id == user.business_id)
            .first()
        )
    lead_ref = AlphaLeadRefInput(lead_id=inp.lead_id, email=inp.email)
    lead = _resolve_alpha_lead(db, user, lead_ref)
    if lead is None:
        # No lead identifier given — fall back to the business's latest run.
        return (
            db.query(_Run)
            .filter(_Run.business_id == user.business_id)
            .order_by(_Run.created_at.desc())
            .first()
        )
    return _latest_alpha_run(db, user, lead.id)


async def _alpha_lead_intake(
    db: Session, user: User, inp: AlphaLeadIntakeInput
) -> dict[str, Any]:
    from app.alpha import service as _svc

    raw = {"name": inp.name, "email": inp.email, "phone": inp.phone,
           "company": inp.company}
    try:
        lead, created, _ = _svc.intake_lead(
            db, business_id=user.business_id, source=inp.source,
            raw=raw, actor=f"draven:{user.id}",
        )
    except _svc.IntakeError as exc:
        return _error(f"lead rejected: {exc}")
    from forge_db.models import WorkflowRun as _Run

    run = (
        db.query(_Run)
        .filter(_Run.lead_id == lead.id)
        .order_by(_Run.created_at.desc())
        .first()
    )
    if created and run is not None:
        _svc.process_pipeline(db, run=run, user=user, actor=f"draven:{user.id}")
    db.commit()
    from forge_db.models import QualificationResult as _QR

    qr = (
        db.query(_QR)
        .filter(_QR.lead_id == lead.id)
        .order_by(_QR.created_at.desc())
        .first()
    )
    return _ok(
        {
            "lead_id": str(lead.id),
            "created": created,
            "status": lead.status.value,
            "run_id": str(run.id) if run else None,
            "run_state": run.state.value if run else None,
            "qualification": (
                {"verdict": qr.verdict.value, "score": qr.score,
                 "confidence": qr.confidence}
                if qr else None
            ),
        }
    )


async def _alpha_qualify_lead(
    db: Session, user: User, inp: AlphaLeadRefInput
) -> dict[str, Any]:
    from app.alpha import service as _svc

    lead = _resolve_alpha_lead(db, user, inp)
    if lead is None:
        return _error("lead not found in your business")
    run = _latest_alpha_run(db, user, lead.id)
    if run is None:
        from forge_db.models import RunState as _RS
        from forge_db.models import WorkflowRun as _WR

        run = _WR(
            business_id=user.business_id, lead_id=lead.id,
            state=_RS.received, idempotency_key=f"run-{uuid.uuid4().hex}",
        )
        db.add(run)
        db.flush()
    _svc.process_pipeline(db, run=run, user=user, actor=f"draven:{user.id}")
    db.commit()
    from forge_db.models import QualificationResult as _QR

    qr = (
        db.query(_QR)
        .filter(_QR.lead_id == lead.id)
        .order_by(_QR.created_at.desc())
        .first()
    )
    return _ok(
        {
            "lead_id": str(lead.id),
            "run_id": str(run.id),
            "run_state": run.state.value,
            "qualification": (
                {"verdict": qr.verdict.value, "score": qr.score,
                 "confidence": qr.confidence,
                 "rules_version": qr.rules_version,
                 "criteria": qr.criteria}
                if qr else None
            ),
        }
    )


async def _alpha_run_status(
    db: Session, user: User, inp: AlphaRunRefInput
) -> dict[str, Any]:
    run = _resolve_alpha_run(db, user, inp)
    if run is None:
        return _error("workflow run not found in your business")
    from app.alpha.workflow import is_terminal as _is_t, transition_log as _tl

    return _ok(
        {
            "run_id": str(run.id),
            "lead_id": str(run.lead_id),
            "state": run.state.value,
            "terminal": _is_t(run.state),
            "paused": run.paused,
            "retry_count": run.retry_count,
            "transitions": _tl(db, run.id)[-10:],
        }
    )


async def _alpha_prepare_followup(
    db: Session, user: User, inp: AlphaFollowupInput
) -> dict[str, Any]:
    from app.alpha import service as _svc

    run = _resolve_alpha_run(
        db, user,
        AlphaRunRefInput(run_id=inp.run_id, lead_id=inp.lead_id, email=inp.email),
    )
    if run is None:
        return _error("workflow run not found in your business")
    from forge_db.models import Lead as _Lead

    lead = db.query(_Lead).filter(_Lead.id == run.lead_id).one()
    recipient = lead.email or ""
    if not recipient:
        return _error("lead has no email address — cannot prepare follow-up")
    try:
        message, approval = _svc.prepare_followup(
            db, run=run, user=user, recipient=recipient,
            subject=inp.subject, body=inp.body, actor=f"draven:{user.id}",
        )
    except ValueError as exc:
        return _error(f"cannot prepare follow-up: {exc}")
    db.commit()
    return _ok(
        {
            "run_id": str(run.id),
            "run_state": run.state.value,
            "message_id": str(message.id),
            "approval_id": str(approval.id),
            "approval_status": approval.status.value,
            "note": "draft created and staged for approval — nothing was sent",
        }
    )


async def _alpha_approve_followup(
    db: Session, user: User, inp: AlphaRunRefInput
) -> dict[str, Any]:
    run = _resolve_alpha_run(db, user, inp)
    if run is None:
        return _approval_required(
            action="alpha.approve_followup",
            description="No matching workflow run found — nothing to approve.",
            affected=[],
            input_echo={},
        )
    from forge_db.models import AlphaApproval as _AA

    approval = (
        db.query(_AA)
        .filter(_AA.run_id == run.id,
                _AA.business_id == user.business_id)
        .order_by(_AA.created_at.desc())
        .first()
    )
    if approval is None:
        return _error("no approval pending on this run")
    return _approval_required(
        action="alpha.approve_followup",
        description=(
            f"Approve the follow-up for run {run.id} (lead {run.lead_id}). "
            "Approving moves the run to policy recheck, then the connector "
            "submits the email. The approval is bound to the exact "
            f"recipient/subject/body (digest {approval.payload_digest[:12]}…)."
        ),
        affected=[f"workflow_run:{run.id}", f"approval:{approval.id}"],
        estimated_cost_usd=0.0,
        input_echo={"run_id": str(run.id), "approval_id": str(approval.id)},
    )


_register(
    ToolDef(
        id="alpha.lead_intake",
        description="Create a lead from name/email/phone/company and run it "
                    "through validation and qualification.",
        input_model=AlphaLeadIntakeInput,
        risk="low",
        execute=_alpha_lead_intake,
    )
)
_register(
    ToolDef(
        id="alpha.qualify_lead",
        description="Run deterministic qualification on a lead (by id or email).",
        input_model=AlphaLeadRefInput,
        risk="low",
        execute=_alpha_qualify_lead,
    )
)
_register(
    ToolDef(
        id="alpha.run_status",
        description="Workflow run state and recent transitions (by run, lead, or email).",
        input_model=AlphaRunRefInput,
        risk="low",
        execute=_alpha_run_status,
    )
)
_register(
    ToolDef(
        id="alpha.prepare_followup",
        description="Draft a follow-up and stage it for approval. Never sends.",
        input_model=AlphaFollowupInput,
        risk="low",
        execute=_alpha_prepare_followup,
    )
)
_register(
    ToolDef(
        id="alpha.approve_followup",
        description="Request approval to approve a staged follow-up "
                    "(approval-gated; never executes directly).",
        input_model=AlphaRunRefInput,
        risk="high",
        execute=_alpha_approve_followup,
    )
)


# ---------------------------------------------------------------------------
# Voice parity tools — every app capability, conversational
# ---------------------------------------------------------------------------
#
# This section expands the registry so the voice AI can do anything the
# app can do. Risk discipline:
#
# * low    — reads + reversible internal writes (CRUD, consent, submit,
#            plan/asset state-machine steps): execute immediately.
# * medium — real spend or scheduled future sends (asset generation bills
#            the LLM provider; autopilot plan approval materializes a
#            scheduled campaign): validate everything up front, then return
#            an approval request. Never executes.
# * high   — launch/pause, publish, delete, negative approval actions:
#            never executes; returns an approval request.
#
# Reuse rule: where a router already owns the logic (template rendering,
# asset state machine, affiliate validators, earnings aggregation, ops
# aggregates), the tool calls it instead of re-implementing it.

from fastapi import HTTPException as _HTTPException  # noqa: E402

from forge_db.models import (  # noqa: E402
    AffiliateLink,
    AffiliateProgram,
    AssetKind,
    BrandKit,
    CampaignEnrollment,
    CampaignStep,
    Channel,
    ContentPlan,
    Event,
    InterviewSession,
    InterviewStatus,
    PlanStatus,
    Template,
    WeeklySummary,
)

from app.routers.affiliates import _earnings_stats as _affiliate_earnings_stats  # noqa: E402
from app.routers.affiliates import _check_program_owned as _aff_check_program_owned  # noqa: E402
from app.routers.affiliates import _check_slug_unique as _aff_check_slug_unique  # noqa: E402
from app.routers.affiliates import _validate_destination_url as _aff_validate_url  # noqa: E402
from app.routers.affiliates import _validate_program_status as _aff_validate_status  # noqa: E402
from app.routers.analytics import funnel as _analytics_funnel_ep  # noqa: E402
from app.routers.assets import _transition as _asset_transition  # noqa: E402
from app.routers.assets import asset_versions as _asset_versions_ep  # noqa: E402
from app.routers.campaigns import _assert_launchable as _campaign_assert_launchable  # noqa: E402
from app.routers.interview import start_interview as _interview_start_ep  # noqa: E402
from app.routers.ops import ops_activity as _ops_activity_ep  # noqa: E402
from app.routers.ops import ops_brain as _ops_brain_ep  # noqa: E402
from app.routers.templates import check_template_syntax as _tpl_check_syntax  # noqa: E402
from app.routers.templates import render_template_string as _tpl_render  # noqa: E402


# ---------------------------------------------------------------------------
# Shared input schemas
# ---------------------------------------------------------------------------


class PaginationInput(BaseModel):
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0)


class DaysInput(BaseModel):
    days: int = Field(default=30, ge=1, le=365)


class BrandKitRefInput(BaseModel):
    brandkit_id: str | None = None
    name: str | None = None


class BrandKitCreateInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    voice_description: str | None = None
    tone_tags: list[str] = Field(default_factory=list)
    primary_color: str | None = Field(default=None, max_length=32)
    secondary_color: str | None = Field(default=None, max_length=32)
    fonts: dict[str, Any] = Field(default_factory=dict)
    icp_description: str | None = None
    do_list: list[str] = Field(default_factory=list)
    dont_list: list[str] = Field(default_factory=list)


class BrandKitUpdateInput(BaseModel):
    brandkit_id: str | None = None
    name: str | None = None  # lookup by name when no id
    new_name: str | None = Field(default=None, max_length=255)
    voice_description: str | None = None
    tone_tags: list[str] | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    fonts: dict[str, Any] | None = None
    icp_description: str | None = None
    do_list: list[str] | None = None
    dont_list: list[str] | None = None


class ContactRefInput(BaseModel):
    contact_id: str | None = None
    email: str | None = None


class ContactCreateInput(BaseModel):
    email: str | None = None
    phone: str | None = None
    first_name: str | None = Field(default=None, max_length=255)
    last_name: str | None = Field(default=None, max_length=255)
    source: str | None = Field(default=None, max_length=255)
    tags: list[str] = Field(default_factory=list)
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class ContactUpdateInput(BaseModel):
    contact_id: str | None = None
    email: str | None = None  # lookup by email when no id
    new_email: str | None = None
    phone: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    source: str | None = None
    tags: list[str] | None = None
    custom_fields: dict[str, Any] | None = None
    unsubscribed: bool | None = None


class ContactConsentInput(BaseModel):
    contact_id: str | None = None
    email: str | None = None
    channel: Literal["email", "sms"]
    granted: bool


class TemplateRefInput(BaseModel):
    template_id: str | None = None
    name: str | None = None


class TemplateCreateInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    channel: Literal["email", "sms", "social"]
    subject_template: str | None = None
    body_template: str = Field(min_length=1)
    variables: list[str] = Field(default_factory=list)


class TemplateUpdateInput(BaseModel):
    template_id: str | None = None
    name: str | None = None  # lookup by name when no id
    new_name: str | None = Field(default=None, max_length=255)
    channel: Literal["email", "sms", "social"] | None = None
    subject_template: str | None = None
    body_template: str | None = None
    variables: list[str] | None = None


class TemplatePreviewInput(BaseModel):
    template_id: str | None = None
    name: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)


class AssetListInput(BaseModel):
    status: str | None = None
    kind: str | None = None
    limit: int = Field(default=20, ge=1, le=50)


class AssetGenerateInput(BaseModel):
    kind: Literal["email_copy", "social_post", "sms", "blog", "ad", "image_prompt"]
    title: str = Field(min_length=1, max_length=500)
    template_id: str | None = None
    prompt: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    is_affiliate_content: bool = False


class AssetRejectInput(BaseModel):
    asset_id: str | None = None
    asset_title: str | None = None
    reason: str = Field(min_length=1, max_length=2000)


class CampaignCreateInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    autopilot: bool = False
    starts_at: datetime | None = None
    timezone: str = Field(default="UTC", max_length=64)


class CampaignUpdateInput(BaseModel):
    campaign_id: str | None = None
    campaign_name: str | None = None
    name: str | None = Field(default=None, max_length=255)  # new name
    description: str | None = None
    starts_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    # NOTE: status is deliberately not updatable here — launches and pauses
    # go through the dedicated (approval-gated) tools.


class CampaignStepIn(BaseModel):
    channel: Literal["email", "sms", "social"]
    position: int | None = None
    template_id: str | None = None
    asset_id: str | None = None
    delay_hours: int = Field(default=24, ge=0)
    trigger_event: str | None = Field(default=None, max_length=128)


class CampaignStepsAddInput(BaseModel):
    campaign_id: str | None = None
    campaign_name: str | None = None
    steps: list[CampaignStepIn] = Field(min_length=1, max_length=25)


class CampaignStepUpdateInput(BaseModel):
    campaign_id: str | None = None
    campaign_name: str | None = None
    step_id: str | None = None
    position: int | None = None  # lookup by position when no step_id
    channel: Literal["email", "sms", "social"] | None = None
    new_position: int | None = None
    template_id: str | None = None
    asset_id: str | None = None
    delay_hours: int | None = Field(default=None, ge=0)
    trigger_event: str | None = None


class CampaignEnrollmentsInput(BaseModel):
    campaign_id: str | None = None
    campaign_name: str | None = None
    limit: int = Field(default=20, ge=1, le=50)


class AutopilotUpdateInput(BaseModel):
    auto_approve: bool | None = None
    require_approval_for_channels: list[str] | None = None
    daily_send_cap: int | None = Field(default=None, ge=1)
    quiet_hours_start: int | None = Field(default=None, ge=0, le=23)
    quiet_hours_end: int | None = Field(default=None, ge=0, le=23)


class PlanApproveInput(BaseModel):
    plan_id: str


class AffiliateProgramRefInput(BaseModel):
    program_id: str | None = None
    name: str | None = None


class AffiliateProgramCreateInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    network: str = Field(default="other", max_length=64)
    website_url: str | None = Field(default=None, max_length=1024)
    default_commission_pct: float = Field(default=0, ge=0, le=100)
    cookie_days: int | None = Field(default=None, ge=0, le=3650)
    status: str = Field(default="active", max_length=32)
    notes: str | None = None


class AffiliateProgramUpdateInput(BaseModel):
    program_id: str | None = None
    name: str | None = None  # lookup by name when no id
    new_name: str | None = Field(default=None, min_length=1, max_length=255)
    network: str | None = Field(default=None, max_length=64)
    website_url: str | None = None
    default_commission_pct: float | None = Field(default=None, ge=0, le=100)
    cookie_days: int | None = Field(default=None, ge=0, le=3650)
    status: str | None = Field(default=None, max_length=32)
    notes: str | None = None


class AffiliateLinkRefInput(BaseModel):
    link_id: str | None = None
    slug: str | None = None


class AffiliateLinkCreateInput(BaseModel):
    program_id: str
    label: str = Field(min_length=1, max_length=255)
    slug: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    destination_url: str = Field(min_length=1, max_length=2048)
    utm_source: str | None = Field(default=None, max_length=128)
    utm_medium: str | None = Field(default=None, max_length=128)
    utm_campaign: str | None = Field(default=None, max_length=128)
    is_active: bool = True


class AffiliateLinkUpdateInput(BaseModel):
    link_id: str | None = None
    slug: str | None = None  # lookup by slug when no id
    new_slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    program_id: str | None = None
    label: str | None = Field(default=None, min_length=1, max_length=255)
    destination_url: str | None = Field(default=None, min_length=1, max_length=2048)
    utm_source: str | None = Field(default=None, max_length=128)
    utm_medium: str | None = Field(default=None, max_length=128)
    utm_campaign: str | None = Field(default=None, max_length=128)
    is_active: bool | None = None


class BusinessUpdateInput(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    timezone: str | None = Field(default=None, max_length=64)


# ---------------------------------------------------------------------------
# Tenant-scoped resolution helpers (parity section)
# ---------------------------------------------------------------------------


def _owned(db: Session, user: User, model: Any, obj_id: Any) -> Any | None:
    """Fetch one row of a ``business_id``-scoped table, or None.

    None covers both malformed ids and rows owned by another business —
    callers must not distinguish the two (no tenant probing).
    """
    try:
        oid = uuid.UUID(str(obj_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return (
        db.query(model)
        .filter(model.id == oid, model.business_id == user.business_id)
        .first()
    )


def _resolve_brandkit(db: Session, business_id: uuid.UUID, ref: BrandKitRefInput):
    if ref.brandkit_id:
        try:
            kid = uuid.UUID(str(ref.brandkit_id))
        except ValueError:
            return None
        return (
            db.query(BrandKit)
            .filter(BrandKit.id == kid, BrandKit.business_id == business_id)
            .first()
        )
    if ref.name:
        return (
            db.query(BrandKit)
            .filter(
                BrandKit.business_id == business_id,
                BrandKit.name.ilike(f"%{ref.name}%"),
            )
            .first()
        )
    return None


def _resolve_contact(db: Session, business_id: uuid.UUID, ref: Any) -> Contact | None:
    cid = getattr(ref, "contact_id", None)
    if cid:
        try:
            coid = uuid.UUID(str(cid))
        except ValueError:
            return None
        return (
            db.query(Contact)
            .filter(Contact.id == coid, Contact.business_id == business_id)
            .first()
        )
    email = getattr(ref, "email", None)
    if email:
        return (
            db.query(Contact)
            .filter(
                Contact.business_id == business_id,
                Contact.email.ilike(f"%{email}%"),
            )
            .order_by(Contact.created_at.desc())
            .first()
        )
    return None


def _resolve_template(db: Session, business_id: uuid.UUID, ref: Any) -> Template | None:
    tid = getattr(ref, "template_id", None)
    if tid:
        try:
            toid = uuid.UUID(str(tid))
        except ValueError:
            return None
        return (
            db.query(Template)
            .filter(Template.id == toid, Template.business_id == business_id)
            .first()
        )
    name = getattr(ref, "name", None)
    if name:
        return (
            db.query(Template)
            .filter(
                Template.business_id == business_id,
                Template.name.ilike(f"%{name}%"),
            )
            .first()
        )
    return None


def _resolve_affiliate_program(
    db: Session, business_id: uuid.UUID, ref: Any
) -> AffiliateProgram | None:
    pid = getattr(ref, "program_id", None)
    if pid:
        try:
            poid = uuid.UUID(str(pid))
        except ValueError:
            return None
        return (
            db.query(AffiliateProgram)
            .filter(
                AffiliateProgram.id == poid,
                AffiliateProgram.business_id == business_id,
            )
            .first()
        )
    name = getattr(ref, "name", None)
    if name:
        return (
            db.query(AffiliateProgram)
            .filter(
                AffiliateProgram.business_id == business_id,
                AffiliateProgram.name.ilike(f"%{name}%"),
            )
            .first()
        )
    return None


def _resolve_affiliate_link(
    db: Session, business_id: uuid.UUID, ref: Any
) -> AffiliateLink | None:
    lid = getattr(ref, "link_id", None)
    if lid:
        try:
            loid = uuid.UUID(str(lid))
        except ValueError:
            return None
        return (
            db.query(AffiliateLink)
            .filter(
                AffiliateLink.id == loid,
                AffiliateLink.business_id == business_id,
            )
            .first()
        )
    slug = getattr(ref, "slug", None)
    if slug:
        return (
            db.query(AffiliateLink)
            .filter(
                AffiliateLink.business_id == business_id,
                AffiliateLink.slug == slug,
            )
            .first()
        )
    return None


# ---------------------------------------------------------------------------
# Serializers (JSON-safe, no secrets)
# ---------------------------------------------------------------------------


def _s_brandkit(k: BrandKit, full: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": str(k.id),
        "name": k.name,
        "version": k.version,
        "created_at": _iso(k.created_at),
    }
    if full:
        d.update(
            {
                "voice_description": k.voice_description,
                "tone_tags": k.tone_tags,
                "primary_color": k.primary_color,
                "secondary_color": k.secondary_color,
                "fonts": k.fonts,
                "icp_description": k.icp_description,
                "do_list": k.do_list,
                "dont_list": k.dont_list,
            }
        )
    return d


def _s_contact(c: Contact) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "email": c.email,
        "phone": c.phone,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "source": c.source,
        "tags": c.tags,
        "consent_email": c.consent_email,
        "consent_sms": c.consent_sms,
        "unsubscribed": c.unsubscribed,
        "created_at": _iso(c.created_at),
    }


def _s_template(t: Template, body: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": str(t.id),
        "name": t.name,
        "channel": t.channel.value,
        "variables": t.variables,
        "created_at": _iso(t.created_at),
    }
    if body:
        d.update(
            {
                "subject_template": t.subject_template,
                "body_template": t.body_template,
            }
        )
    return d


def _s_asset(a: Asset, body: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": str(a.id),
        "kind": a.kind.value,
        "title": a.title,
        "status": a.status.value,
        "version": a.version,
        "brand_kit_version": a.brand_kit_version,
        "is_affiliate_content": a.is_affiliate_content,
        "cost_usd": float(a.cost_usd or 0),
        "created_at": _iso(a.created_at),
    }
    if body:
        d.update(
            {
                "body_preview": (a.body or "")[:2000],
                "variables": a.variables,
                "rejection_reason": a.rejection_reason,
            }
        )
    return d


def _s_campaign(c: Campaign) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "name": c.name,
        "description": c.description,
        "status": c.status.value,
        "autopilot": c.autopilot,
        "starts_at": _iso(c.starts_at),
        "timezone": c.timezone,
        "created_at": _iso(c.created_at),
    }


def _s_step(s: CampaignStep) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "position": s.position,
        "channel": s.channel.value,
        "template_id": str(s.template_id) if s.template_id else None,
        "asset_id": str(s.asset_id) if s.asset_id else None,
        "delay_hours": s.delay_hours,
        "trigger_event": s.trigger_event,
    }


def _s_program(p: AffiliateProgram) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "name": p.name,
        "network": p.network,
        "website_url": p.website_url,
        "default_commission_pct": float(p.default_commission_pct or 0),
        "cookie_days": p.cookie_days,
        "status": p.status,
        "notes": p.notes,
        "created_at": _iso(p.created_at),
    }


def _s_link(link: AffiliateLink, program_name: str | None = None) -> dict[str, Any]:
    return {
        "id": str(link.id),
        "program_id": str(link.program_id),
        "program_name": program_name,
        "label": link.label,
        "slug": link.slug,
        "destination_url": link.destination_url,
        "utm_source": link.utm_source,
        "utm_medium": link.utm_medium,
        "utm_campaign": link.utm_campaign,
        "is_active": link.is_active,
        "created_at": _iso(link.created_at),
    }


# ---------------------------------------------------------------------------
# Brand kits
# ---------------------------------------------------------------------------


async def _brandkit_list(db: Session, user: User, inp: PaginationInput) -> dict[str, Any]:
    rows = (
        db.query(BrandKit)
        .filter(BrandKit.business_id == user.business_id)
        .order_by(BrandKit.created_at.desc())
        .limit(inp.limit)
        .offset(inp.offset)
        .all()
    )
    return _ok(
        {"count": len(rows), "brand_kits": [_s_brandkit(k) for k in rows]}
    )


async def _brandkit_get(db: Session, user: User, inp: BrandKitRefInput) -> dict[str, Any]:
    kit = _resolve_brandkit(db, user.business_id, inp)
    if kit is None:
        return _error("brand kit not found in your business")
    return _ok({"brand_kit": _s_brandkit(kit, full=True)})


async def _brandkit_create(
    db: Session, user: User, inp: BrandKitCreateInput
) -> dict[str, Any]:
    kit = BrandKit(business_id=user.business_id, version=1, **inp.model_dump())
    db.add(kit)
    db.commit()
    db.refresh(kit)
    return _ok(
        {
            "brand_kit": _s_brandkit(kit, full=True),
            "note": "created as version 1",
        }
    )


async def _brandkit_update(
    db: Session, user: User, inp: BrandKitUpdateInput
) -> dict[str, Any]:
    kit = _resolve_brandkit(db, user.business_id, inp)
    if kit is None:
        return _error("brand kit not found in your business")
    updates = {
        "name": inp.new_name,
        "voice_description": inp.voice_description,
        "tone_tags": inp.tone_tags,
        "primary_color": inp.primary_color,
        "secondary_color": inp.secondary_color,
        "fonts": inp.fonts,
        "icp_description": inp.icp_description,
        "do_list": inp.do_list,
        "dont_list": inp.dont_list,
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        return _error("nothing to update — provide at least one field to change")
    for field, value in updates.items():
        setattr(kit, field, value)
    kit.version = kit.version + 1
    db.commit()
    db.refresh(kit)
    return _ok({"brand_kit": _s_brandkit(kit, full=True)})


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


async def _contacts_list(db: Session, user: User, inp: PaginationInput) -> dict[str, Any]:
    rows = (
        db.query(Contact)
        .filter(Contact.business_id == user.business_id)
        .order_by(Contact.created_at.desc())
        .limit(inp.limit)
        .offset(inp.offset)
        .all()
    )
    return _ok({"count": len(rows), "contacts": [_s_contact(c) for c in rows]})


async def _contact_get(db: Session, user: User, inp: ContactRefInput) -> dict[str, Any]:
    contact = _resolve_contact(db, user.business_id, inp)
    if contact is None:
        return _error("contact not found in your business")
    return _ok({"contact": _s_contact(contact)})


async def _contact_create(
    db: Session, user: User, inp: ContactCreateInput
) -> dict[str, Any]:
    contact = Contact(business_id=user.business_id, **inp.model_dump())
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return _ok({"contact": _s_contact(contact)})


async def _contact_update(
    db: Session, user: User, inp: ContactUpdateInput
) -> dict[str, Any]:
    contact = _resolve_contact(db, user.business_id, inp)
    if contact is None:
        return _error("contact not found in your business")
    updates = {
        "email": inp.new_email,
        "phone": inp.phone,
        "first_name": inp.first_name,
        "last_name": inp.last_name,
        "source": inp.source,
        "tags": inp.tags,
        "custom_fields": inp.custom_fields,
        "unsubscribed": inp.unsubscribed,
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        return _error("nothing to update — provide at least one field to change")
    for field, value in updates.items():
        setattr(contact, field, value)
    db.commit()
    db.refresh(contact)
    return _ok({"contact": _s_contact(contact)})


async def _contact_consent(
    db: Session, user: User, inp: ContactConsentInput
) -> dict[str, Any]:
    if not inp.contact_id and not inp.email:
        return _error("contact_id or email is required")
    contact = _resolve_contact(db, user.business_id, inp)
    if contact is None:
        return _error("contact not found in your business")
    now = datetime.now(timezone.utc)
    if inp.channel == "email":
        contact.consent_email = inp.granted
        contact.consent_email_at = now if inp.granted else None
    else:
        contact.consent_sms = inp.granted
        contact.consent_sms_at = now if inp.granted else None
    db.commit()
    db.refresh(contact)
    return _ok(
        {
            "contact": _s_contact(contact),
            "note": (
                f"{inp.channel} consent "
                f"{'granted' if inp.granted else 'revoked'} "
                f"for {contact.email or contact.phone or contact.id}"
            ),
        }
    )


async def _contact_delete(
    db: Session, user: User, inp: ContactRefInput
) -> dict[str, Any]:
    if not inp.contact_id and not inp.email:
        return _error("contact_id or email is required")
    contact = _resolve_contact(db, user.business_id, inp)
    if contact is None:
        return _error("contact not found in your business")
    label = " ".join(p for p in [contact.first_name, contact.last_name] if p) or (
        contact.email or contact.phone or str(contact.id)
    )
    return _approval_required(
        action="contact.delete",
        description=(
            f"Delete contact '{label}' ({contact.id}). Deleting removes the "
            "contact and its consent history permanently; enrollments and "
            "sends referencing it are left untouched."
        ),
        affected=[f"contact:{contact.id} ({label})"],
        estimated_cost_usd=0.0,
        input_echo={"contact_id": str(contact.id)},
    )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


async def _template_list(db: Session, user: User, inp: PaginationInput) -> dict[str, Any]:
    rows = (
        db.query(Template)
        .filter(Template.business_id == user.business_id)
        .order_by(Template.created_at.desc())
        .limit(inp.limit)
        .offset(inp.offset)
        .all()
    )
    return _ok({"count": len(rows), "templates": [_s_template(t) for t in rows]})


async def _template_get(db: Session, user: User, inp: TemplateRefInput) -> dict[str, Any]:
    template = _resolve_template(db, user.business_id, inp)
    if template is None:
        return _error("template not found in your business")
    return _ok({"template": _s_template(template, body=True)})


async def _template_create(
    db: Session, user: User, inp: TemplateCreateInput
) -> dict[str, Any]:
    try:
        _tpl_check_syntax(inp.body_template)
        if inp.subject_template:
            _tpl_check_syntax(inp.subject_template)
    except _HTTPException as exc:
        return _error(f"invalid template syntax: {exc.detail}")
    template = Template(
        business_id=user.business_id,
        name=inp.name,
        channel=Channel(inp.channel),
        subject_template=inp.subject_template,
        body_template=inp.body_template,
        variables=inp.variables,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return _ok({"template": _s_template(template, body=True)})


async def _template_update(
    db: Session, user: User, inp: TemplateUpdateInput
) -> dict[str, Any]:
    template = _resolve_template(db, user.business_id, inp)
    if template is None:
        return _error("template not found in your business")
    updates = {
        "name": inp.new_name,
        "subject_template": inp.subject_template,
        "body_template": inp.body_template,
        "variables": inp.variables,
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    if inp.channel is not None:
        updates["channel"] = Channel(inp.channel)
    if not updates:
        return _error("nothing to update — provide at least one field to change")
    if "body_template" in updates or "subject_template" in updates:
        try:
            _tpl_check_syntax(updates.get("body_template") or template.body_template)
            if "subject_template" in updates:
                _tpl_check_syntax(updates["subject_template"])
        except _HTTPException as exc:
            return _error(f"invalid template syntax: {exc.detail}")
    for field, value in updates.items():
        setattr(template, field, value)
    db.commit()
    db.refresh(template)
    return _ok({"template": _s_template(template, body=True)})


async def _template_preview(
    db: Session, user: User, inp: TemplatePreviewInput
) -> dict[str, Any]:
    if not inp.template_id and not inp.name:
        return _error("template_id or name is required")
    template = _resolve_template(db, user.business_id, inp)
    if template is None:
        return _error("template not found in your business")
    try:
        subject = _tpl_render(template.subject_template, dict(inp.variables))
        body = _tpl_render(template.body_template, dict(inp.variables)) or ""
    except _HTTPException as exc:
        return _error(f"preview failed: {exc.detail}")
    return _ok(
        {
            "template_id": str(template.id),
            "template_name": template.name,
            "subject": subject,
            "body": body,
        }
    )


async def _template_delete(
    db: Session, user: User, inp: TemplateRefInput
) -> dict[str, Any]:
    if not inp.template_id and not inp.name:
        return _error("template_id or name is required")
    template = _resolve_template(db, user.business_id, inp)
    if template is None:
        return _error("template not found in your business")
    return _approval_required(
        action="template.delete",
        description=(
            f"Delete template '{template.name}' ({template.channel.value}, "
            f"{template.id}). Campaign steps referencing it will keep a "
            "dangling template_id."
        ),
        affected=[f"template:{template.id} ({template.name})"],
        estimated_cost_usd=0.0,
        input_echo={"template_id": str(template.id)},
    )


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


async def _assets_list(db: Session, user: User, inp: AssetListInput) -> dict[str, Any]:
    q = db.query(Asset).filter(Asset.business_id == user.business_id)
    if inp.status is not None:
        try:
            q = q.filter(Asset.status == AssetStatus(inp.status))
        except ValueError:
            return _error(
                f"unknown asset status: {inp.status!r} "
                "(draft|in_review|approved|rejected)"
            )
    if inp.kind is not None:
        try:
            q = q.filter(Asset.kind == AssetKind(inp.kind))
        except ValueError:
            return _error(f"unknown asset kind: {inp.kind!r}")
    rows = q.order_by(Asset.created_at.desc()).limit(inp.limit).all()
    return _ok({"count": len(rows), "assets": [_s_asset(a) for a in rows]})


async def _asset_get(db: Session, user: User, inp: AssetRefInput) -> dict[str, Any]:
    asset = _resolve_asset(db, user.business_id, inp)
    if asset is None:
        return _error("asset not found in your business")
    return _ok({"asset": _s_asset(asset, body=True)})


async def _asset_generate(
    db: Session, user: User, inp: AssetGenerateInput
) -> dict[str, Any]:
    """MEDIUM RISK — generation bills the LLM provider, so this validates
    the request and returns an approval instead of enqueueing the worker."""
    template_note = ""
    if inp.template_id:
        template = _owned(db, user, Template, inp.template_id)
        if template is None:
            return _error("template not found in your business")
        template_note = f" from template '{template.name}'"
    return _approval_required(
        action="asset.generate",
        description=(
            f"Generate a {inp.kind} asset titled '{inp.title}'{template_note}. "
            "Approving enqueues the real generate_asset worker job, which "
            "calls the configured LLM provider and bills generation spend "
            "to your account. The asset is created in draft first, then "
            "moves to in_review (or straight to approved when autopilot "
            "auto-approve is on)."
        ),
        affected=["assets:(new draft asset)"],
        estimated_cost_usd=0.0,
        input_echo={
            "kind": inp.kind,
            "title": inp.title,
            "template_id": inp.template_id,
            "prompt": inp.prompt,
            "variables": inp.variables,
            "is_affiliate_content": inp.is_affiliate_content,
        },
    )


async def _asset_submit(db: Session, user: User, inp: AssetRefInput) -> dict[str, Any]:
    if not inp.asset_id and not inp.asset_title:
        return _error("asset_id or asset_title is required")
    asset = _resolve_asset(db, user.business_id, inp)
    if asset is None:
        return _error("asset not found in your business")
    # Same state machine the /assets endpoints enforce (draft -> in_review,
    # rejected -> draft rework).
    target = (
        AssetStatus.draft
        if asset.status is AssetStatus.rejected
        else AssetStatus.in_review
    )
    try:
        asset = _asset_transition(db, asset, target)
    except _HTTPException as exc:
        return _error(f"cannot submit: {exc.detail}")
    return _ok(
        {
            "asset": _s_asset(asset),
            "note": f"asset moved to {asset.status.value}",
        }
    )


async def _asset_versions(
    db: Session, user: User, inp: AssetRefInput
) -> dict[str, Any]:
    if not inp.asset_id and not inp.asset_title:
        return _error("asset_id or asset_title is required")
    asset = _resolve_asset(db, user.business_id, inp)
    if asset is None:
        return _error("asset not found in your business")
    try:
        page = _asset_versions_ep(asset.id, user, db)
    except _HTTPException as exc:
        return _error(f"cannot load versions: {exc.detail}")
    items = page["items"] if isinstance(page, dict) else []
    return _ok(
        {
            "count": len(items),
            "versions": [
                {
                    "id": str(a.id),
                    "version": a.version,
                    "title": a.title,
                    "status": a.status.value,
                    "created_at": _iso(a.created_at),
                }
                for a in items
            ],
        }
    )


async def _asset_reject(
    db: Session, user: User, inp: AssetRejectInput
) -> dict[str, Any]:
    if not inp.asset_id and not inp.asset_title:
        return _error("asset_id or asset_title is required")
    asset = _resolve_asset(db, user.business_id, inp)
    if asset is None:
        return _error("asset not found in your business")
    if asset.status != AssetStatus.in_review:
        return _error(
            f"asset '{asset.title}' is {asset.status.value}, not in_review — "
            "only in_review assets can be rejected"
        )
    return _approval_required(
        action="asset.reject",
        description=(
            f"Reject asset '{asset.title}' ({asset.kind.value}, v{asset.version}) "
            f"with reason: {inp.reason}. Rejected assets return to draft for "
            "rework and can never be sent."
        ),
        affected=[f"asset:{asset.id} ({asset.title})"],
        estimated_cost_usd=0.0,
        input_echo={"asset_id": str(asset.id), "reason": inp.reason},
    )


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


async def _campaign_get(db: Session, user: User, inp: CampaignRefInput) -> dict[str, Any]:
    if not inp.campaign_id and not inp.campaign_name:
        return _error("campaign_id or campaign_name is required")
    campaign = _resolve_campaign(db, user.business_id, inp)
    if campaign is None:
        return _error("campaign not found in your business")
    steps = (
        db.query(CampaignStep)
        .filter(CampaignStep.campaign_id == campaign.id)
        .order_by(CampaignStep.position.asc())
        .all()
    )
    return _ok(
        {
            "campaign": _s_campaign(campaign),
            "steps": [_s_step(s) for s in steps],
        }
    )


async def _campaign_create(
    db: Session, user: User, inp: CampaignCreateInput
) -> dict[str, Any]:
    campaign = Campaign(
        business_id=user.business_id,
        created_by=user.id,
        name=inp.name,
        description=inp.description,
        autopilot=inp.autopilot,
        starts_at=inp.starts_at,
        timezone=inp.timezone,
        status=CampaignStatus.draft,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return _ok(
        {
            "campaign": _s_campaign(campaign),
            "note": "created as draft — add steps, then launch when ready",
        }
    )


async def _campaign_update(
    db: Session, user: User, inp: CampaignUpdateInput
) -> dict[str, Any]:
    if not inp.campaign_id and not inp.campaign_name:
        return _error("campaign_id or campaign_name is required")
    campaign = _resolve_campaign(
        db, user.business_id, CampaignRefInput(
            campaign_id=inp.campaign_id, campaign_name=inp.campaign_name
        ),
    )
    if campaign is None:
        return _error("campaign not found in your business")
    updates = {
        "name": inp.name,
        "description": inp.description,
        "starts_at": inp.starts_at,
        "timezone": inp.timezone,
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        return _error("nothing to update — provide at least one field to change")
    for field, value in updates.items():
        setattr(campaign, field, value)
    db.commit()
    db.refresh(campaign)
    return _ok({"campaign": _s_campaign(campaign)})


async def _campaign_steps_add(
    db: Session, user: User, inp: CampaignStepsAddInput
) -> dict[str, Any]:
    if not inp.campaign_id and not inp.campaign_name:
        return _error("campaign_id or campaign_name is required")
    campaign = _resolve_campaign(
        db, user.business_id, CampaignRefInput(
            campaign_id=inp.campaign_id, campaign_name=inp.campaign_name
        ),
    )
    if campaign is None:
        return _error("campaign not found in your business")
    max_position = (
        db.query(func.coalesce(func.max(CampaignStep.position), -1))
        .filter(CampaignStep.campaign_id == campaign.id)
        .scalar()
    )
    created: list[CampaignStep] = []
    for i, item in enumerate(inp.steps):
        template_id = None
        if item.template_id is not None:
            if _owned(db, user, Template, item.template_id) is None:
                return _error(f"template not found in your business: {item.template_id}")
            template_id = uuid.UUID(str(item.template_id))
        asset_id = None
        if item.asset_id is not None:
            if _owned(db, user, Asset, item.asset_id) is None:
                return _error(f"asset not found in your business: {item.asset_id}")
            asset_id = uuid.UUID(str(item.asset_id))
        step = CampaignStep(
            campaign_id=campaign.id,
            position=item.position if item.position is not None else max_position + 1 + i,
            channel=Channel(item.channel),
            template_id=template_id,
            asset_id=asset_id,
            delay_hours=item.delay_hours,
            trigger_event=item.trigger_event,
        )
        db.add(step)
        created.append(step)
    db.commit()
    for step in created:
        db.refresh(step)
    return _ok(
        {
            "campaign_id": str(campaign.id),
            "added": len(created),
            "steps": [_s_step(s) for s in created],
        }
    )


def _resolve_step(
    db: Session, campaign: Campaign, inp: CampaignStepUpdateInput
) -> CampaignStep | None:
    q = db.query(CampaignStep).filter(CampaignStep.campaign_id == campaign.id)
    if inp.step_id:
        try:
            sid = uuid.UUID(str(inp.step_id))
        except ValueError:
            return None
        return q.filter(CampaignStep.id == sid).first()
    if inp.position is not None:
        return q.filter(CampaignStep.position == inp.position).first()
    return None


async def _campaign_step_update(
    db: Session, user: User, inp: CampaignStepUpdateInput
) -> dict[str, Any]:
    if not inp.campaign_id and not inp.campaign_name:
        return _error("campaign_id or campaign_name is required")
    campaign = _resolve_campaign(
        db, user.business_id, CampaignRefInput(
            campaign_id=inp.campaign_id, campaign_name=inp.campaign_name
        ),
    )
    if campaign is None:
        return _error("campaign not found in your business")
    step = _resolve_step(db, campaign, inp)
    if step is None:
        return _error("step not found on this campaign")
    updates: dict[str, Any] = {}
    if inp.channel is not None:
        updates["channel"] = Channel(inp.channel)
    if inp.new_position is not None:
        updates["position"] = inp.new_position
    if inp.template_id is not None:
        if _owned(db, user, Template, inp.template_id) is None:
            return _error(f"template not found in your business: {inp.template_id}")
        updates["template_id"] = uuid.UUID(str(inp.template_id))
    if inp.asset_id is not None:
        if _owned(db, user, Asset, inp.asset_id) is None:
            return _error(f"asset not found in your business: {inp.asset_id}")
        updates["asset_id"] = uuid.UUID(str(inp.asset_id))
    if inp.delay_hours is not None:
        updates["delay_hours"] = inp.delay_hours
    if inp.trigger_event is not None:
        updates["trigger_event"] = inp.trigger_event
    if not updates:
        return _error("nothing to update — provide at least one field to change")
    for field, value in updates.items():
        setattr(step, field, value)
    db.commit()
    db.refresh(step)
    return _ok({"step": _s_step(step)})


async def _campaign_launch(
    db: Session, user: User, inp: CampaignRefInput
) -> dict[str, Any]:
    """HIGH RISK — launching starts real sends. Never executes via voice."""
    if not inp.campaign_id and not inp.campaign_name:
        return _error("campaign_id or campaign_name is required")
    campaign = _resolve_campaign(db, user.business_id, inp)
    if campaign is None:
        return _error("campaign not found in your business")
    if campaign.status not in (
        CampaignStatus.draft,
        CampaignStatus.scheduled,
        CampaignStatus.paused,
    ):
        return _error(
            f"campaign '{campaign.name}' is {campaign.status.value} — "
            "only draft, scheduled, or paused campaigns can launch"
        )
    try:
        _campaign_assert_launchable(db, campaign)
    except _HTTPException as exc:
        return _error(f"cannot launch: {exc.detail}")
    step_count = (
        db.query(func.count(CampaignStep.id))
        .filter(CampaignStep.campaign_id == campaign.id)
        .scalar()
        or 0
    )
    return _approval_required(
        action="campaign.launch",
        description=(
            f"Launch campaign '{campaign.name}' ({campaign.id}) with "
            f"{step_count} step{'s' if step_count != 1 else ''}. Launching "
            "sets it running: contacts enroll and real messages start "
            "sending on the campaign schedule (quiet hours and the daily "
            "send cap still apply). All asset-bearing steps reference "
            "approved assets."
        ),
        affected=[f"campaign:{campaign.id} ({campaign.name})"],
        estimated_cost_usd=0.0,
        input_echo={"campaign_id": str(campaign.id)},
    )


async def _campaign_enrollments(
    db: Session, user: User, inp: CampaignEnrollmentsInput
) -> dict[str, Any]:
    if not inp.campaign_id and not inp.campaign_name:
        return _error("campaign_id or campaign_name is required")
    campaign = _resolve_campaign(
        db, user.business_id, CampaignRefInput(
            campaign_id=inp.campaign_id, campaign_name=inp.campaign_name
        ),
    )
    if campaign is None:
        return _error("campaign not found in your business")
    rows = (
        db.query(CampaignEnrollment)
        .filter(CampaignEnrollment.campaign_id == campaign.id)
        .order_by(CampaignEnrollment.created_at.desc())
        .limit(inp.limit)
        .all()
    )
    return _ok(
        {
            "campaign_id": str(campaign.id),
            "count": len(rows),
            "enrollments": [
                {
                    "id": str(e.id),
                    "contact_id": str(e.contact_id),
                    "current_step": e.current_step,
                    "status": e.status.value,
                    "next_run_at": _iso(e.next_run_at),
                    "created_at": _iso(e.created_at),
                }
                for e in rows
            ],
        }
    )


# ---------------------------------------------------------------------------
# Autopilot
# ---------------------------------------------------------------------------


async def _autopilot_update(
    db: Session, user: User, inp: AutopilotUpdateInput
) -> dict[str, Any]:
    settings = db.get(AutopilotSettings, user.business_id)
    if settings is None:
        settings = AutopilotSettings(business_id=user.business_id)
        db.add(settings)
        db.flush()
    updates = inp.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(settings, field, value)
    db.commit()
    db.refresh(settings)
    return _ok(
        {
            "settings": {
                "auto_approve": settings.auto_approve,
                "require_approval_for_channels": settings.require_approval_for_channels,
                "daily_send_cap": settings.daily_send_cap,
                "quiet_hours": [
                    settings.quiet_hours_start,
                    settings.quiet_hours_end,
                ],
            }
        }
    )


async def _autopilot_plan_approve(
    db: Session, user: User, inp: PlanApproveInput
) -> dict[str, Any]:
    """MEDIUM RISK — approving materializes a scheduled campaign with
    future sends. Validates, then returns an approval request."""
    try:
        pid = uuid.UUID(str(inp.plan_id))
    except ValueError:
        return _error("plan_id is not a valid UUID")
    plan = (
        db.query(ContentPlan)
        .filter(
            ContentPlan.id == pid,
            ContentPlan.business_id == user.business_id,
        )
        .first()
    )
    if plan is None:
        return _error("content plan not found in your business")
    if plan.status is not PlanStatus.draft:
        return _error(
            f"plan for week of {plan.week_start} is {plan.status.value} — "
            "only draft plans can be approved"
        )
    items = plan.items or []
    return _approval_required(
        action="autopilot.plan_approve",
        description=(
            f"Approve the weekly content plan for {plan.week_start} "
            f"({len(items)} items) and materialize it into a scheduled "
            "autopilot campaign starting Monday 09:00 business-local "
            "(one step per item). Asset references are re-validated at "
            "approve time — unapproved or missing assets become "
            "template-only steps."
        ),
        affected=[
            f"content_plan:{plan.id}",
            f"campaigns:(new scheduled 'Autopilot — week of {plan.week_start}')",
        ],
        estimated_cost_usd=0.0,
        input_echo={"plan_id": str(plan.id)},
    )


# ---------------------------------------------------------------------------
# Affiliates
# ---------------------------------------------------------------------------


async def _affiliate_programs_list(
    db: Session, user: User, inp: PaginationInput
) -> dict[str, Any]:
    rows = (
        db.query(AffiliateProgram)
        .filter(AffiliateProgram.business_id == user.business_id)
        .order_by(AffiliateProgram.created_at.desc())
        .limit(inp.limit)
        .offset(inp.offset)
        .all()
    )
    return _ok(
        {"count": len(rows), "programs": [_s_program(p) for p in rows]}
    )


async def _affiliate_program_get(
    db: Session, user: User, inp: AffiliateProgramRefInput
) -> dict[str, Any]:
    program = _resolve_affiliate_program(db, user.business_id, inp)
    if program is None:
        return _error("affiliate program not found in your business")
    link_count = (
        db.query(func.count(AffiliateLink.id))
        .filter(AffiliateLink.program_id == program.id)
        .scalar()
        or 0
    )
    d = _s_program(program)
    d["link_count"] = link_count
    return _ok({"program": d})


async def _affiliate_program_create(
    db: Session, user: User, inp: AffiliateProgramCreateInput
) -> dict[str, Any]:
    try:
        _aff_validate_status(inp.status)
    except _HTTPException as exc:
        return _error(f"invalid status: {exc.detail}")
    program = AffiliateProgram(
        business_id=user.business_id,
        name=inp.name.strip(),
        network=inp.network.strip().lower() or "other",
        website_url=(inp.website_url or "").strip() or None,
        default_commission_pct=inp.default_commission_pct,
        cookie_days=inp.cookie_days,
        status=inp.status,
        notes=(inp.notes or "").strip() or None,
    )
    db.add(program)
    db.commit()
    db.refresh(program)
    return _ok({"program": _s_program(program)})


async def _affiliate_program_update(
    db: Session, user: User, inp: AffiliateProgramUpdateInput
) -> dict[str, Any]:
    program = _resolve_affiliate_program(db, user.business_id, inp)
    if program is None:
        return _error("affiliate program not found in your business")
    updates = {
        "name": inp.new_name,
        "network": inp.network,
        "website_url": inp.website_url,
        "default_commission_pct": inp.default_commission_pct,
        "cookie_days": inp.cookie_days,
        "status": inp.status,
        "notes": inp.notes,
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        return _error("nothing to update — provide at least one field to change")
    if "status" in updates:
        try:
            _aff_validate_status(updates["status"])
        except _HTTPException as exc:
            return _error(f"invalid status: {exc.detail}")
    for field, value in updates.items():
        if field in ("name", "network", "website_url", "notes") and isinstance(value, str):
            value = value.strip()
            if field == "network":
                value = value.lower() or "other"
            if field in ("website_url", "notes") and not value:
                value = None
        setattr(program, field, value)
    db.commit()
    db.refresh(program)
    return _ok({"program": _s_program(program)})


async def _affiliate_program_delete(
    db: Session, user: User, inp: AffiliateProgramRefInput
) -> dict[str, Any]:
    if not inp.program_id and not inp.name:
        return _error("program_id or name is required")
    program = _resolve_affiliate_program(db, user.business_id, inp)
    if program is None:
        return _error("affiliate program not found in your business")
    link_count = (
        db.query(func.count(AffiliateLink.id))
        .filter(AffiliateLink.program_id == program.id)
        .scalar()
        or 0
    )
    return _approval_required(
        action="affiliate.program_delete",
        description=(
            f"Delete affiliate program '{program.name}' ({program.id}). "
            f"Its {link_count} trackable link{'s' if link_count != 1 else ''} "
            "are deleted with it (cascade) — their short URLs stop working."
        ),
        affected=[
            f"affiliate_program:{program.id} ({program.name})",
            f"affiliate_links:{link_count} (cascade)",
        ],
        estimated_cost_usd=0.0,
        input_echo={"program_id": str(program.id)},
    )


async def _affiliate_links_list(
    db: Session, user: User, inp: PaginationInput
) -> dict[str, Any]:
    rows = (
        db.query(AffiliateLink)
        .filter(AffiliateLink.business_id == user.business_id)
        .order_by(AffiliateLink.created_at.desc())
        .limit(inp.limit)
        .offset(inp.offset)
        .all()
    )
    programs = {
        p.id: p.name
        for p in db.query(AffiliateProgram)
        .filter(AffiliateProgram.business_id == user.business_id)
        .all()
    }
    return _ok(
        {
            "count": len(rows),
            "links": [
                _s_link(link, programs.get(link.program_id)) for link in rows
            ],
        }
    )


async def _affiliate_link_get(
    db: Session, user: User, inp: AffiliateLinkRefInput
) -> dict[str, Any]:
    link = _resolve_affiliate_link(db, user.business_id, inp)
    if link is None:
        return _error("affiliate link not found in your business")
    program = _owned(db, user, AffiliateProgram, link.program_id)
    return _ok({"link": _s_link(link, program.name if program else None)})


async def _affiliate_link_create(
    db: Session, user: User, inp: AffiliateLinkCreateInput
) -> dict[str, Any]:
    program = _owned(db, user, AffiliateProgram, inp.program_id)
    if program is None:
        return _error("affiliate program not found in your business")
    try:
        _aff_check_slug_unique(db, user, inp.slug)
        destination = _aff_validate_url(inp.destination_url)
    except _HTTPException as exc:
        return _error(f"invalid link: {exc.detail}")
    link = AffiliateLink(
        business_id=user.business_id,
        program_id=program.id,
        label=inp.label.strip(),
        slug=inp.slug,
        destination_url=destination,
        utm_source=(inp.utm_source or "").strip() or "forgeos",
        utm_medium=(inp.utm_medium or "").strip() or "affiliate",
        utm_campaign=(inp.utm_campaign or "").strip() or None,
        is_active=inp.is_active,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return _ok({"link": _s_link(link, program.name)})


async def _affiliate_link_update(
    db: Session, user: User, inp: AffiliateLinkUpdateInput
) -> dict[str, Any]:
    link = _resolve_affiliate_link(db, user.business_id, inp)
    if link is None:
        return _error("affiliate link not found in your business")
    updates: dict[str, Any] = {}
    if inp.program_id is not None:
        program = _owned(db, user, AffiliateProgram, inp.program_id)
        if program is None:
            return _error("affiliate program not found in your business")
        updates["program_id"] = program.id
    if inp.new_slug is not None:
        updates["slug"] = inp.new_slug
    if inp.label is not None:
        updates["label"] = inp.label
    if inp.destination_url is not None:
        updates["destination_url"] = inp.destination_url
    if inp.utm_source is not None:
        updates["utm_source"] = inp.utm_source
    if inp.utm_medium is not None:
        updates["utm_medium"] = inp.utm_medium
    if inp.utm_campaign is not None:
        updates["utm_campaign"] = inp.utm_campaign
    if inp.is_active is not None:
        updates["is_active"] = inp.is_active
    if not updates:
        return _error("nothing to update — provide at least one field to change")
    try:
        if "slug" in updates:
            _aff_check_slug_unique(db, user, updates["slug"], exclude_id=link.id)
        if "destination_url" in updates:
            updates["destination_url"] = _aff_validate_url(updates["destination_url"])
    except _HTTPException as exc:
        return _error(f"invalid link: {exc.detail}")
    for field, value in updates.items():
        if field in ("label", "utm_source", "utm_medium", "utm_campaign") and isinstance(
            value, str
        ):
            value = value.strip() or None
            if field == "label" and not value:
                return _error("label must not be blank")
        setattr(link, field, value)
    db.commit()
    db.refresh(link)
    program = _owned(db, user, AffiliateProgram, link.program_id)
    return _ok({"link": _s_link(link, program.name if program else None)})


async def _affiliate_link_delete(
    db: Session, user: User, inp: AffiliateLinkRefInput
) -> dict[str, Any]:
    if not inp.link_id and not inp.slug:
        return _error("link_id or slug is required")
    link = _resolve_affiliate_link(db, user.business_id, inp)
    if link is None:
        return _error("affiliate link not found in your business")
    return _approval_required(
        action="affiliate.link_delete",
        description=(
            f"Delete affiliate link '{link.label}' (/{link.slug}). "
            "The short URL stops working immediately."
        ),
        affected=[f"affiliate_link:{link.id} (/{link.slug})"],
        estimated_cost_usd=0.0,
        input_echo={"link_id": str(link.id)},
    )


async def _affiliate_earnings(
    db: Session, user: User, inp: DaysInput
) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=inp.days)
    totals, per_program, per_link = _affiliate_earnings_stats(
        db, user.business_id, since
    )
    return _ok(
        {
            "days": inp.days,
            "totals": totals.model_dump(mode="json"),
            "per_program": [p.model_dump(mode="json") for p in per_program],
            "per_link": [link.model_dump(mode="json") for link in per_link],
        }
    )


# ---------------------------------------------------------------------------
# Analytics (funnel + weekly summary; overview already covered)
# ---------------------------------------------------------------------------


async def _analytics_funnel(
    db: Session, user: User, inp: CampaignRefInput
) -> dict[str, Any]:
    if not inp.campaign_id and not inp.campaign_name:
        return _error("campaign_id or campaign_name is required")
    campaign = _resolve_campaign(db, user.business_id, inp)
    if campaign is None:
        return _error("campaign not found in your business")
    funnel = _analytics_funnel_ep(campaign.id, user, db)
    return _ok(
        {
            "campaign_id": str(campaign.id),
            "campaign_name": campaign.name,
            "funnel": funnel.model_dump(mode="json"),
        }
    )


async def _analytics_weekly_summary(
    db: Session, user: User, inp: EmptyInput
) -> dict[str, Any]:
    row = (
        db.query(WeeklySummary)
        .filter(WeeklySummary.business_id == user.business_id)
        .order_by(WeeklySummary.week_start.desc())
        .first()
    )
    if row is None:
        return _ok({"found": False, "note": "no weekly summary cut yet"})
    return _ok(
        {
            "found": True,
            "week_start": str(row.week_start),
            "top_assets": row.top_assets or [],
            "bottom_assets": row.bottom_assets or [],
            "best_channel_per_segment": row.best_channel_per_segment or {},
            "recommendation": row.recommendation,
            "created_at": _iso(row.created_at),
        }
    )


# ---------------------------------------------------------------------------
# Interview
# ---------------------------------------------------------------------------


async def _interview_start(
    db: Session, user: User, inp: EmptyInput
) -> dict[str, Any]:
    turn = _interview_start_ep(user, db)
    return _ok(
        {
            "session_id": str(turn["session_id"]),
            "status": turn["status"],
            "question_index": turn["question_index"],
            "total_questions": turn["total_questions"],
            "question": turn["question"],
            "done": turn["done"],
        }
    )


async def _interview_status(
    db: Session, user: User, inp: EmptyInput
) -> dict[str, Any]:
    session = (
        db.query(InterviewSession)
        .filter(InterviewSession.business_id == user.business_id)
        .order_by(InterviewSession.created_at.desc())
        .first()
    )
    if session is None:
        return _ok({"found": False, "note": "no interview session yet"})
    done = session.current_index >= 7
    return _ok(
        {
            "found": True,
            "session_id": str(session.id),
            "status": session.status.value,
            "question_index": min(session.current_index, 7),
            "total_questions": 7,
            "done": done,
            "brand_kit_id": str(session.brand_kit_id) if session.brand_kit_id else None,
        }
    )


# ---------------------------------------------------------------------------
# Ops (mission control + brain — read-only, reuse the router aggregates)
# ---------------------------------------------------------------------------


async def _ops_activity(
    db: Session, user: User, inp: PaginationInput
) -> dict[str, Any]:
    resp = _ops_activity_ep(user, db, min(inp.limit, 30))
    return _ok(resp.model_dump(mode="json"))


async def _ops_brain(db: Session, user: User, inp: EmptyInput) -> dict[str, Any]:
    resp = _ops_brain_ep(user, db)
    data = resp.model_dump(mode="json")
    layers = []
    for layer in data.get("layers", []):
        layers.append(
            {
                "key": layer["key"],
                "label": layer["label"],
                "count": layer["count"],
                "nodes": [
                    {"id": n["id"], "label": n.get("label")}
                    for n in (layer.get("nodes") or [])[:10]
                ],
            }
        )
    links = data.get("links", [])
    timeline = data.get("timeline", [])
    return _ok(
        {
            "as_of": data.get("as_of"),
            "layers": layers,
            "link_count": len(links),
            "timeline_count": len(timeline),
            "recent_timeline": [
                {"at": t.get("at"), "kind": t.get("kind"), "label": t.get("label")}
                for t in timeline[-10:]
            ],
        }
    )


# ---------------------------------------------------------------------------
# Businesses
# ---------------------------------------------------------------------------


async def _business_get(db: Session, user: User, inp: EmptyInput) -> dict[str, Any]:
    business = db.get(Business, user.business_id)
    if business is None:
        return _error("business not found")
    return _ok(
        {
            "id": str(business.id),
            "name": business.name,
            "slug": business.slug,
            "timezone": business.timezone,
        }
    )


async def _business_update(
    db: Session, user: User, inp: BusinessUpdateInput
) -> dict[str, Any]:
    business = db.get(Business, user.business_id)
    if business is None:
        return _error("business not found")
    updates = {
        k: v for k, v in inp.model_dump(exclude_unset=True).items() if v is not None
    }
    if not updates:
        return _error("nothing to update — provide a name or timezone")
    for field, value in updates.items():
        setattr(business, field, value)
    db.commit()
    db.refresh(business)
    return _ok(
        {
            "id": str(business.id),
            "name": business.name,
            "slug": business.slug,
            "timezone": business.timezone,
        }
    )


# ---------------------------------------------------------------------------
# Registry — voice parity tools
# ---------------------------------------------------------------------------

_register(
    ToolDef(
        id="draven.brandkit_list",
        description="List the business's brand kits (id, name, version).",
        input_model=PaginationInput,
        risk="low",
        execute=_brandkit_list,
    )
)
_register(
    ToolDef(
        id="draven.brandkit_get",
        description="Full brand-kit detail (voice, tone, colors, ICP, do/don't lists) by id or name.",
        input_model=BrandKitRefInput,
        risk="low",
        execute=_brandkit_get,
    )
)
_register(
    ToolDef(
        id="draven.brandkit_create",
        description="Create a brand kit (version 1) from voice/tone/color/ICP fields.",
        input_model=BrandKitCreateInput,
        risk="low",
        execute=_brandkit_create,
    )
)
_register(
    ToolDef(
        id="draven.brandkit_update",
        description="Update a brand kit's fields by id or name (bumps version).",
        input_model=BrandKitUpdateInput,
        risk="low",
        execute=_brandkit_update,
    )
)
_register(
    ToolDef(
        id="draven.contacts_list",
        description="List contacts (newest first) with consent flags.",
        input_model=PaginationInput,
        risk="low",
        execute=_contacts_list,
    )
)
_register(
    ToolDef(
        id="draven.contact_get",
        description="Full contact detail by id or email.",
        input_model=ContactRefInput,
        risk="low",
        execute=_contact_get,
    )
)
_register(
    ToolDef(
        id="draven.contact_create",
        description="Create a contact (email/phone, name, tags, source).",
        input_model=ContactCreateInput,
        risk="low",
        execute=_contact_create,
    )
)
_register(
    ToolDef(
        id="draven.contact_update",
        description="Update a contact's fields by id or email.",
        input_model=ContactUpdateInput,
        risk="low",
        execute=_contact_update,
    )
)
_register(
    ToolDef(
        id="draven.contact_consent",
        description="Grant or revoke a contact's email/SMS consent (timestamps recorded).",
        input_model=ContactConsentInput,
        risk="low",
        execute=_contact_consent,
    )
)
_register(
    ToolDef(
        id="draven.contact_delete",
        description="HIGH RISK — propose deleting a contact. Never executes; returns an approval request.",
        input_model=ContactRefInput,
        risk="high",
        execute=_contact_delete,
    )
)
_register(
    ToolDef(
        id="draven.template_list",
        description="List message templates (id, name, channel, variables).",
        input_model=PaginationInput,
        risk="low",
        execute=_template_list,
    )
)
_register(
    ToolDef(
        id="draven.template_get",
        description="Full template detail incl. Jinja2 source by id or name.",
        input_model=TemplateRefInput,
        risk="low",
        execute=_template_get,
    )
)
_register(
    ToolDef(
        id="draven.template_create",
        description="Create a Jinja2 template (syntax validated at write time).",
        input_model=TemplateCreateInput,
        risk="low",
        execute=_template_create,
    )
)
_register(
    ToolDef(
        id="draven.template_update",
        description="Update a template's fields by id or name.",
        input_model=TemplateUpdateInput,
        risk="low",
        execute=_template_update,
    )
)
_register(
    ToolDef(
        id="draven.template_preview",
        description="Render a template's subject/body with supplied variables (same renderer as the UI).",
        input_model=TemplatePreviewInput,
        risk="low",
        execute=_template_preview,
    )
)
_register(
    ToolDef(
        id="draven.template_delete",
        description="HIGH RISK — propose deleting a template. Never executes; returns an approval request.",
        input_model=TemplateRefInput,
        risk="high",
        execute=_template_delete,
    )
)
_register(
    ToolDef(
        id="draven.assets_list",
        description="List assets, filterable by status and kind.",
        input_model=AssetListInput,
        risk="low",
        execute=_assets_list,
    )
)
_register(
    ToolDef(
        id="draven.asset_get",
        description="Full asset detail incl. body by id or title.",
        input_model=AssetRefInput,
        risk="low",
        execute=_asset_get,
    )
)
_register(
    ToolDef(
        id="draven.asset_generate",
        description="MEDIUM RISK — propose generating an asset via the real worker job (LLM spend). Validates the request, then returns an approval request. Never executes.",
        input_model=AssetGenerateInput,
        risk="medium",
        execute=_asset_generate,
    )
)
_register(
    ToolDef(
        id="draven.asset_submit",
        description="Submit a draft asset for review (draft -> in_review; rejected -> draft rework). Same state machine as the UI.",
        input_model=AssetRefInput,
        risk="low",
        execute=_asset_submit,
    )
)
_register(
    ToolDef(
        id="draven.asset_versions",
        description="Full version lineage of an asset (root + descendants).",
        input_model=AssetRefInput,
        risk="low",
        execute=_asset_versions,
    )
)
_register(
    ToolDef(
        id="draven.asset_reject",
        description="HIGH RISK — propose rejecting an in_review asset with a reason. Never executes; returns an approval request.",
        input_model=AssetRejectInput,
        risk="high",
        execute=_asset_reject,
    )
)
_register(
    ToolDef(
        id="draven.campaign_get",
        description="Campaign detail with ordered steps by id or name.",
        input_model=CampaignRefInput,
        risk="low",
        execute=_campaign_get,
    )
)
_register(
    ToolDef(
        id="draven.campaign_create",
        description="Create a campaign as draft (add steps, then launch when ready).",
        input_model=CampaignCreateInput,
        risk="low",
        execute=_campaign_create,
    )
)
_register(
    ToolDef(
        id="draven.campaign_update",
        description="Update a campaign's name/description/schedule (status changes go through launch/pause tools).",
        input_model=CampaignUpdateInput,
        risk="low",
        execute=_campaign_update,
    )
)
_register(
    ToolDef(
        id="draven.campaign_steps_add",
        description="Append steps to a campaign (template/asset refs validated as owned).",
        input_model=CampaignStepsAddInput,
        risk="low",
        execute=_campaign_steps_add,
    )
)
_register(
    ToolDef(
        id="draven.campaign_step_update",
        description="Update one campaign step by step id or position.",
        input_model=CampaignStepUpdateInput,
        risk="low",
        execute=_campaign_step_update,
    )
)
_register(
    ToolDef(
        id="draven.campaign_launch",
        description="HIGH RISK — propose launching a campaign (starts real sends). Validates launch-readiness, then returns an approval request. Never executes.",
        input_model=CampaignRefInput,
        risk="high",
        execute=_campaign_launch,
    )
)
_register(
    ToolDef(
        id="draven.campaign_enrollments",
        description="List a campaign's enrollments (contact, step, status, next run).",
        input_model=CampaignEnrollmentsInput,
        risk="low",
        execute=_campaign_enrollments,
    )
)
_register(
    ToolDef(
        id="draven.autopilot_update",
        description="Update autopilot settings (auto-approve, channel approval requirements, daily cap, quiet hours).",
        input_model=AutopilotUpdateInput,
        risk="low",
        execute=_autopilot_update,
    )
)
_register(
    ToolDef(
        id="draven.autopilot_plan_approve",
        description="MEDIUM RISK — propose approving the weekly content plan (materializes a scheduled campaign). Validates the draft, then returns an approval request. Never executes.",
        input_model=PlanApproveInput,
        risk="medium",
        execute=_autopilot_plan_approve,
    )
)
_register(
    ToolDef(
        id="draven.affiliate_programs_list",
        description="List affiliate programs.",
        input_model=PaginationInput,
        risk="low",
        execute=_affiliate_programs_list,
    )
)
_register(
    ToolDef(
        id="draven.affiliate_program_get",
        description="Affiliate program detail incl. link count, by id or name.",
        input_model=AffiliateProgramRefInput,
        risk="low",
        execute=_affiliate_program_get,
    )
)
_register(
    ToolDef(
        id="draven.affiliate_program_create",
        description="Create an affiliate program (name, network, commission %, status).",
        input_model=AffiliateProgramCreateInput,
        risk="low",
        execute=_affiliate_program_create,
    )
)
_register(
    ToolDef(
        id="draven.affiliate_program_update",
        description="Update an affiliate program by id or name.",
        input_model=AffiliateProgramUpdateInput,
        risk="low",
        execute=_affiliate_program_update,
    )
)
_register(
    ToolDef(
        id="draven.affiliate_program_delete",
        description="HIGH RISK — propose deleting an affiliate program (cascades to its links). Never executes; returns an approval request.",
        input_model=AffiliateProgramRefInput,
        risk="high",
        execute=_affiliate_program_delete,
    )
)
_register(
    ToolDef(
        id="draven.affiliate_links_list",
        description="List trackable affiliate links with program names.",
        input_model=PaginationInput,
        risk="low",
        execute=_affiliate_links_list,
    )
)
_register(
    ToolDef(
        id="draven.affiliate_link_get",
        description="Affiliate link detail by id or slug.",
        input_model=AffiliateLinkRefInput,
        risk="low",
        execute=_affiliate_link_get,
    )
)
_register(
    ToolDef(
        id="draven.affiliate_link_create",
        description="Create a trackable affiliate link (slug validated, destination must be absolute http(s)).",
        input_model=AffiliateLinkCreateInput,
        risk="low",
        execute=_affiliate_link_create,
    )
)
_register(
    ToolDef(
        id="draven.affiliate_link_update",
        description="Update an affiliate link by id or slug.",
        input_model=AffiliateLinkUpdateInput,
        risk="low",
        execute=_affiliate_link_update,
    )
)
_register(
    ToolDef(
        id="draven.affiliate_link_delete",
        description="HIGH RISK — propose deleting an affiliate link (short URL stops working). Never executes; returns an approval request.",
        input_model=AffiliateLinkRefInput,
        risk="high",
        execute=_affiliate_link_delete,
    )
)
_register(
    ToolDef(
        id="draven.affiliate_earnings",
        description="Affiliate earnings aggregated honestly from stored click/conversion events (totals + per program/link).",
        input_model=DaysInput,
        risk="low",
        execute=_affiliate_earnings,
    )
)
_register(
    ToolDef(
        id="draven.analytics_funnel",
        description="Per-step funnel (sent/opened/clicked) for a campaign, same math as the UI.",
        input_model=CampaignRefInput,
        risk="low",
        execute=_analytics_funnel,
    )
)
_register(
    ToolDef(
        id="draven.analytics_weekly_summary",
        description="Latest weekly evidence summary (top/bottom assets, best channel per segment, recommendation).",
        input_model=EmptyInput,
        risk="low",
        execute=_analytics_weekly_summary,
    )
)
_register(
    ToolDef(
        id="draven.interview_start",
        description="Start a Card 0 brand-kit interview session (7 questions, one at a time).",
        input_model=EmptyInput,
        risk="low",
        execute=_interview_start,
    )
)
_register(
    ToolDef(
        id="draven.interview_status",
        description="Latest interview session status (question progress, done, brand kit link).",
        input_model=EmptyInput,
        risk="low",
        execute=_interview_status,
    )
)
_register(
    ToolDef(
        id="draven.ops_activity",
        description="Mission-control activity aggregate (pipeline stages, counters, feed) — read-only, same data as /ops.",
        input_model=PaginationInput,
        risk="low",
        execute=_ops_activity,
    )
)
_register(
    ToolDef(
        id="draven.ops_brain",
        description="Knowledge-graph summary (brand kits -> assets -> campaigns -> sends -> events) — read-only, same data as /brain.",
        input_model=EmptyInput,
        risk="low",
        execute=_ops_brain,
    )
)
_register(
    ToolDef(
        id="draven.business_get",
        description="The caller's own business profile (name, slug, timezone).",
        input_model=EmptyInput,
        risk="low",
        execute=_business_get,
    )
)
_register(
    ToolDef(
        id="draven.business_update",
        description="Update the business profile (name, timezone).",
        input_model=BusinessUpdateInput,
        risk="low",
        execute=_business_update,
    )
)
