"""Pydantic v2 request/response schemas for every endpoint in CONTRACTS.md."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Pagination envelope
# ---------------------------------------------------------------------------


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)
    business_name: str = Field(min_length=1, max_length=255)


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    business_id: uuid.UUID
    role: str
    is_active: bool
    created_at: datetime


class RegisterResponse(BaseModel):
    token: str
    user: UserOut


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


# ---------------------------------------------------------------------------
# Businesses
# ---------------------------------------------------------------------------


class BusinessOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    timezone: str
    created_at: datetime


class BusinessUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    timezone: str | None = Field(default=None, max_length=64)


# ---------------------------------------------------------------------------
# Brand kits
# ---------------------------------------------------------------------------


class BrandKitBase(BaseModel):
    name: str = Field(max_length=255)
    voice_description: str | None = None
    tone_tags: list[str] = Field(default_factory=list)
    primary_color: str | None = Field(default=None, max_length=32)
    secondary_color: str | None = Field(default=None, max_length=32)
    fonts: dict[str, Any] = Field(default_factory=dict)
    icp_description: str | None = None
    do_list: list[str] = Field(default_factory=list)
    dont_list: list[str] = Field(default_factory=list)


class BrandKitCreate(BrandKitBase):
    pass


class BrandKitUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    voice_description: str | None = None
    tone_tags: list[str] | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    fonts: dict[str, Any] | None = None
    icp_description: str | None = None
    do_list: list[str] | None = None
    dont_list: list[str] | None = None


class BrandKitOut(BrandKitBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    version: int
    created_at: datetime


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


class ContactBase(BaseModel):
    email: str | None = None
    phone: str | None = None
    first_name: str | None = Field(default=None, max_length=255)
    last_name: str | None = Field(default=None, max_length=255)
    source: str | None = Field(default=None, max_length=255)
    tags: list[str] = Field(default_factory=list)
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class ContactCreate(ContactBase):
    pass


class ContactUpdate(BaseModel):
    email: str | None = None
    phone: str | None = None
    first_name: str | None = Field(default=None, max_length=255)
    last_name: str | None = Field(default=None, max_length=255)
    source: str | None = Field(default=None, max_length=255)
    tags: list[str] | None = None
    custom_fields: dict[str, Any] | None = None
    unsubscribed: bool | None = None


class ContactOut(ContactBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    consent_email: bool
    consent_email_at: datetime | None
    consent_sms: bool
    consent_sms_at: datetime | None
    unsubscribed: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# OS screens: Pipeline / Meetings / Knowledge
# ---------------------------------------------------------------------------

OPPORTUNITY_STAGES = ("new", "qualified", "proposal", "negotiation", "closed", "lost")


class OpportunityBase(BaseModel):
    title: str = Field(max_length=255)
    contact_id: uuid.UUID | None = None
    value_cents: int = Field(default=0, ge=0)
    stage: Literal["new", "qualified", "proposal", "negotiation", "closed", "lost"] = "new"
    probability: int = Field(default=50, ge=0, le=100)
    expected_close_date: datetime | None = None
    notes: str | None = None


class OpportunityCreate(OpportunityBase):
    pass


class OpportunityUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    contact_id: uuid.UUID | None = None
    value_cents: int | None = Field(default=None, ge=0)
    stage: Literal["new", "qualified", "proposal", "negotiation", "closed", "lost"] | None = None
    probability: int | None = Field(default=None, ge=0, le=100)
    expected_close_date: datetime | None = None
    notes: str | None = None


class OpportunityOut(OpportunityBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    created_at: datetime


class MeetingBase(BaseModel):
    title: str = Field(max_length=255)
    starts_at: datetime
    ends_at: datetime | None = None
    attendees: list[str] = Field(default_factory=list)
    notes: str | None = None


class MeetingCreate(MeetingBase):
    pass


class MeetingUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    attendees: list[str] | None = None
    notes: str | None = None


class MeetingOut(MeetingBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    created_at: datetime


class KnowledgeDocBase(BaseModel):
    title: str = Field(max_length=255)
    content: str = ""
    source: str | None = Field(default=None, max_length=255)


class KnowledgeDocCreate(KnowledgeDocBase):
    pass


class KnowledgeDocUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    content: str | None = None
    source: str | None = Field(default=None, max_length=255)


class KnowledgeDocOut(KnowledgeDocBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    created_at: datetime


class ConsentRequest(BaseModel):
    channel: Literal["email", "sms"]
    granted: bool


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class TemplateBase(BaseModel):
    name: str = Field(max_length=255)
    channel: Literal["email", "sms", "social"]
    subject_template: str | None = None
    body_template: str
    variables: list[str] = Field(default_factory=list)

    @field_validator("body_template", "subject_template")
    @classmethod
    def _strip_xss(cls, v: str | None) -> str | None:
        # XSS hardening: strip script tags / event handlers at input.
        from app.core.sanitize import sanitize_html

        return sanitize_html(v)


class TemplateCreate(TemplateBase):
    pass


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    channel: Literal["email", "sms", "social"] | None = None
    subject_template: str | None = None
    body_template: str | None = None
    variables: list[str] | None = None

    @field_validator("body_template", "subject_template")
    @classmethod
    def _strip_xss(cls, v: str | None) -> str | None:
        from app.core.sanitize import sanitize_html

        return sanitize_html(v)


class TemplateOut(TemplateBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    created_at: datetime


class TemplatePreviewRequest(BaseModel):
    variables: dict[str, Any] = Field(default_factory=dict)


class TemplatePreviewResponse(BaseModel):
    subject: str | None
    body: str


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


class AssetGenerateRequest(BaseModel):
    kind: Literal["email_copy", "social_post", "sms", "blog", "ad", "image_prompt"]
    title: str = Field(max_length=500)
    template_id: uuid.UUID | None = None
    prompt: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    # Marks the asset as affiliate content so the FTC disclosure guardrail
    # applies (also auto-detected from /r/ links or program URLs).
    is_affiliate_content: bool = False

    @field_validator("title", "prompt")
    @classmethod
    def _strip_xss(cls, v: str | None) -> str | None:
        # XSS hardening: user prompt/title flows into generated asset body.
        from app.core.sanitize import sanitize_html

        return sanitize_html(v)


class AssetGenerateResponse(BaseModel):
    asset_id: uuid.UUID
    job_id: str | None
    warning: str | None = None


class AssetOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    kind: str
    title: str
    body: str | None
    variables: dict[str, Any]
    version: int
    status: str
    brand_kit_version: int
    created_by: uuid.UUID | None
    approved_by: uuid.UUID | None
    rejection_reason: str | None
    parent_asset_id: uuid.UUID | None
    cost_usd: Decimal
    tokens_in: int
    tokens_out: int
    llm_provider: str | None
    llm_model: str | None
    is_affiliate_content: bool
    created_at: datetime


class AssetApprovalRequest(BaseModel):
    note: str | None = None


class AssetUpdateRequest(BaseModel):
    """Partial update for asset metadata (not the approval state machine)."""

    is_affiliate_content: bool | None = None


class AssetRejectRequest(BaseModel):
    reason: str = Field(min_length=1)


class AssetApprovalOut(ORMModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    reviewer_id: uuid.UUID | None
    decision: str
    note: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


class CampaignStepCreate(BaseModel):
    channel: Literal["email", "sms", "social"]
    position: int | None = None
    template_id: uuid.UUID | None = None
    asset_id: uuid.UUID | None = None
    delay_hours: int = Field(default=24, ge=0)
    trigger_event: str | None = Field(default=None, max_length=128)


class CampaignStepUpdate(BaseModel):
    channel: Literal["email", "sms", "social"] | None = None
    position: int | None = None
    template_id: uuid.UUID | None = None
    asset_id: uuid.UUID | None = None
    delay_hours: int | None = Field(default=None, ge=0)
    trigger_event: str | None = Field(default=None, max_length=128)


class CampaignStepOut(ORMModel):
    id: uuid.UUID
    campaign_id: uuid.UUID
    position: int
    channel: str
    template_id: uuid.UUID | None
    asset_id: uuid.UUID | None
    delay_hours: int
    trigger_event: str | None
    created_at: datetime


class CampaignStepsCreate(BaseModel):
    steps: list[CampaignStepCreate] = Field(min_length=1)


class CampaignBase(BaseModel):
    name: str = Field(max_length=255)
    description: str | None = None
    autopilot: bool = False
    starts_at: datetime | None = None
    timezone: str = Field(default="UTC", max_length=64)

    @field_validator("name", "description")
    @classmethod
    def _strip_xss(cls, v: str | None) -> str | None:
        from app.core.sanitize import sanitize_html

        return sanitize_html(v)


class CampaignCreate(CampaignBase):
    pass


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    autopilot: bool | None = None
    starts_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    status: Literal["draft", "scheduled", "running", "paused", "completed"] | None = None

    @field_validator("name", "description")
    @classmethod
    def _strip_xss(cls, v: str | None) -> str | None:
        from app.core.sanitize import sanitize_html

        return sanitize_html(v)


class CampaignOut(CampaignBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    status: str
    created_by: uuid.UUID | None
    created_at: datetime


class CampaignDetailOut(CampaignOut):
    steps: list[CampaignStepOut] = []


class EnrollmentOut(ORMModel):
    id: uuid.UUID
    campaign_id: uuid.UUID
    contact_id: uuid.UUID
    current_step: int
    status: str
    next_run_at: datetime | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Autopilot
# ---------------------------------------------------------------------------


class AutopilotOut(ORMModel):
    business_id: uuid.UUID
    auto_approve: bool
    require_approval_for_channels: list[str]
    daily_send_cap: int
    quiet_hours_start: int
    quiet_hours_end: int
    plan_day: int
    plan_hour: int
    plan_cadence: str
    last_planned_at: datetime | None


class AutopilotUpdate(BaseModel):
    auto_approve: bool | None = None
    require_approval_for_channels: list[str] | None = None
    daily_send_cap: int | None = Field(default=None, ge=1)
    quiet_hours_start: int | None = Field(default=None, ge=0, le=23)
    quiet_hours_end: int | None = Field(default=None, ge=0, le=23)
    # Planner schedule (per business). plan_day: 0=Monday..6=Sunday;
    # plan_hour: 0-23 business-local; plan_cadence: daily | weekly | biweekly.
    # Daily drafts every day at plan_hour (plan_day is ignored).
    plan_day: int | None = Field(default=None, ge=0, le=6)
    plan_hour: int | None = Field(default=None, ge=0, le=23)
    plan_cadence: Literal["daily", "weekly", "biweekly"] | None = None

    @field_validator("plan_cadence", mode="before")
    @classmethod
    def _normalize_cadence(cls, v: object) -> object:
        # Defensive: a corrupted value like "'weekly'" (literal quotes, seen
        # in the wild) would otherwise 422 forever on every save. Strip
        # surrounding whitespace/quotes so the schedule stays editable.
        if isinstance(v, str):
            v = v.strip().strip("'\"").strip()
        return v


class PlanItem(BaseModel):
    """One row of a content plan's ``items`` JSON (CONTRACTS.md Card 4)."""

    kind: Literal["email_copy", "social_post", "sms"]
    channel: Literal["email", "sms", "social"]
    day: int = Field(ge=0, le=6)  # 0 = Monday
    title: str = Field(max_length=500)
    brief: str
    asset_id: uuid.UUID | None = None


class ContentPlanOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    week_start: date
    status: str
    items: list[PlanItem]
    created_by: uuid.UUID | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    campaign_id: uuid.UUID | None
    created_at: datetime


class PlanApproveIn(BaseModel):
    plan_id: uuid.UUID


class PlanApproveOut(BaseModel):
    plan: ContentPlanOut
    campaign_id: uuid.UUID


class PlanRunNowOut(BaseModel):
    """Result of ``POST /autopilot/plan/run-now``.

    ``created`` is False when a draft/approved plan already existed for
    this week — the existing plan is returned instead of a duplicate.
    """

    plan: ContentPlanOut
    created: bool


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


class DayBucket(BaseModel):
    date: str
    sent: int
    delivered: int
    opened: int
    clicked: int
    converted: int


class AnalyticsOverview(BaseModel):
    sent: int
    delivered: int
    opened: int
    clicked: int
    converted: int
    open_rate: float
    ctr: float
    conversion_rate: float
    spend_usd: float
    by_day: list[DayBucket]


class FunnelStep(BaseModel):
    step_id: uuid.UUID
    position: int
    sent: int
    opened: int
    clicked: int


class FunnelResponse(BaseModel):
    items: list[FunnelStep]


class WeeklySummaryAsset(BaseModel):
    asset_id: str
    title: str
    kind: str
    delivered: int
    converted: int
    conversion_rate: float


class WeeklySummarySegment(BaseModel):
    channel: str
    conversion_rate: float
    delivered: int


class WeeklySummaryOut(BaseModel):
    """Latest Card 5 weekly evidence summary for the caller's business."""

    week_start: date
    top_assets: list[WeeklySummaryAsset]
    bottom_assets: list[WeeklySummaryAsset]
    best_channel_per_segment: dict[str, WeeklySummarySegment]
    recommendation: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Dev outbox
# ---------------------------------------------------------------------------


class OutboxOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    channel: str
    to_address: str
    subject: str | None
    body: str
    provider: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Webhooks / events
# ---------------------------------------------------------------------------


class DeliveryWebhookRequest(BaseModel):
    provider_message_id: str
    event: Literal["delivered", "opened", "clicked", "bounced"]
    contact: str | None = None


class DeliveryWebhookResponse(BaseModel):
    ok: bool = True


class EventCreate(BaseModel):
    kind: str = Field(max_length=64)
    contact_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventResponse(BaseModel):
    event_id: uuid.UUID
    job_id: str | None
    warning: str | None = None


# ---------------------------------------------------------------------------
# Ops / mission control (read-only aggregate for the /ops dashboard)
# ---------------------------------------------------------------------------


class OpsActivityItem(BaseModel):
    id: str
    kind: Literal["generation", "send", "event"]
    title: str
    detail: str | None
    status: str | None
    at: datetime


class OpsCounters(BaseModel):
    sends_today: int
    generations_today: int
    in_flight: int


class OpsActivityResponse(BaseModel):
    as_of: datetime
    stages: dict[str, dict[str, int]]
    counters: OpsCounters
    activity: list[OpsActivityItem]


# ---------------------------------------------------------------------------
# Brain (Card 5 knowledge-graph views — read-only, one JSON for all views)
# ---------------------------------------------------------------------------


class BrainNode(BaseModel):
    id: str
    label: str
    detail: str | None = None
    at: datetime | None = None


class BrainLink(BaseModel):
    source: str
    target: str
    kind: Literal["brand", "asset", "campaign", "engagement"]


class BrainLayer(BaseModel):
    key: str
    label: str
    count: int
    nodes: list[BrainNode]


class BrainTimelinePoint(BaseModel):
    at: datetime
    kind: Literal["sent", "opened", "clicked", "converted", "event"]
    label: str


class BrainResponse(BaseModel):
    as_of: datetime
    layers: list[BrainLayer]
    links: list[BrainLink]
    timeline: list[BrainTimelinePoint]


# ---------------------------------------------------------------------------
# Interview (Card 0 guided interview)
# ---------------------------------------------------------------------------


class InterviewAnswerRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=10000)


class InterviewQuestionResponse(BaseModel):
    """One interview turn: the next question, or done=true when finished."""

    session_id: uuid.UUID
    status: str
    question_index: int
    total_questions: int
    question: str | None
    done: bool


class InterviewFinishResponse(BaseModel):
    session_id: uuid.UUID
    status: str
    draft_brand_kit: BrandKitBase


class InterviewConfirmRequest(BaseModel):
    overrides: BrandKitUpdate | None = None


class InterviewConfirmResponse(BaseModel):
    brand_kit: BrandKitOut


# ---------------------------------------------------------------------------
# Affiliates — "we promote affiliate offers, we earn commissions"
# ---------------------------------------------------------------------------


class AffiliateProgramBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    network: str = Field(default="other", max_length=64)
    website_url: str | None = Field(default=None, max_length=1024)
    default_commission_pct: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    cookie_days: int | None = Field(default=None, ge=0, le=3650)
    status: str = Field(default="active", max_length=32)
    notes: str | None = None


class AffiliateProgramCreate(AffiliateProgramBase):
    pass


class AffiliateProgramUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    network: str | None = Field(default=None, max_length=64)
    website_url: str | None = Field(default=None, max_length=1024)
    default_commission_pct: Decimal | None = Field(default=None, ge=0, le=100)
    cookie_days: int | None = Field(default=None, ge=0, le=3650)
    status: str | None = Field(default=None, max_length=32)
    notes: str | None = None


class AffiliateProgramOut(AffiliateProgramBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    created_at: datetime


class AffiliateLinkBase(BaseModel):
    program_id: uuid.UUID
    label: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    destination_url: str = Field(min_length=1, max_length=2048)
    utm_source: str | None = Field(default=None, max_length=128)
    utm_medium: str | None = Field(default=None, max_length=128)
    utm_campaign: str | None = Field(default=None, max_length=128)
    is_active: bool = True


class AffiliateLinkCreate(AffiliateLinkBase):
    pass


class AffiliateLinkUpdate(BaseModel):
    program_id: uuid.UUID | None = None
    label: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(
        default=None, min_length=1, max_length=128,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    destination_url: str | None = Field(default=None, min_length=1, max_length=2048)
    utm_source: str | None = Field(default=None, max_length=128)
    utm_medium: str | None = Field(default=None, max_length=128)
    utm_campaign: str | None = Field(default=None, max_length=128)
    is_active: bool | None = None


class AffiliateLinkOut(AffiliateLinkBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    created_at: datetime


class AffiliateLinkStats(BaseModel):
    link_id: uuid.UUID
    label: str
    program_name: str
    clicks: int
    conversions: int
    conversion_rate: float
    earnings_usd: float


class AffiliateProgramStats(BaseModel):
    program_id: uuid.UUID
    program_name: str
    clicks: int
    conversions: int
    conversion_rate: float
    earnings_usd: float


class AffiliateTotals(BaseModel):
    clicks: int
    conversions: int
    conversion_rate: float
    earnings_usd: float


class AffiliateEarningsResponse(BaseModel):
    days: int
    totals: AffiliateTotals
    per_program: list[AffiliateProgramStats]
    per_link: list[AffiliateLinkStats]


class AffiliateDailyPoint(BaseModel):
    """One day of affiliate activity for the dashboard chart."""

    date: str  # YYYY-MM-DD
    clicks: int
    conversions: int
    earnings_usd: float


class AffiliateActivityItem(BaseModel):
    """A recent click or conversion event for the activity feed."""

    kind: str  # affiliate_clicked | affiliate_converted
    link_label: str
    program_name: str
    commission_usd: float | None = None
    created_at: datetime


class AffiliateDashboardResponse(BaseModel):
    """Everything the affiliate dashboard Overview tab needs in one call."""

    days: int
    totals: AffiliateTotals
    per_program: list[AffiliateProgramStats]
    top_links: list[AffiliateLinkStats]
    daily: list[AffiliateDailyPoint]
    recent_activity: list[AffiliateActivityItem]


class ViatorSearchRequest(BaseModel):
    """Live Viator product search. destination_id OR keyword (freetext)."""

    destination_id: int | None = Field(default=None, ge=1)
    keyword: str | None = Field(default=None, max_length=200)
    count: int = Field(default=12, ge=1, le=25)


class ViatorProductIn(BaseModel):
    """One normalized Viator product, as returned by viator/search."""

    model_config = {"extra": "allow"}

    product_code: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)


class ViatorImportRequest(BaseModel):
    products: list[ViatorProductIn] = Field(min_length=1, max_length=25)


class ViatorImportResponse(BaseModel):
    imported: int
    skipped: int
    items: list[dict[str, Any]]


class ViatorGenerateAdsRequest(BaseModel):
    program_ids: list[uuid.UUID] | None = None


class AffiliateAutomationRuleBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    rule_type: str = Field(pattern=r"^(auto_import|auto_ads|autopilot_sweep)$")
    network: str = Field(default="viator", max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)
    schedule: str = Field(default="weekly", pattern=r"^(daily|weekly)$")
    enabled: bool = True


class AffiliateAutomationRuleCreate(AffiliateAutomationRuleBase):
    pass


class AffiliateAutomationRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    config: dict[str, Any] | None = None
    schedule: str | None = Field(default=None, pattern=r"^(daily|weekly)$")
    enabled: bool | None = None


class AffiliateAutomationRuleOut(AffiliateAutomationRuleBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    last_run_at: datetime | None = None
    last_run_result: str | None = None
    created_at: datetime


class AffiliateAlert(BaseModel):
    """A smart trigger alert for the dashboard."""

    alert_type: str  # underperforming_link | earnings_spike | no_ads | stale_program
    severity: str  # info | warning
    title: str
    detail: str
    link_id: uuid.UUID | None = None
    program_id: uuid.UUID | None = None


class AffiliateConversionRequest(BaseModel):
    """Network postback stand-in: record a conversion for a link.

    ``commission_usd`` defaults to ``order_value_usd`` × the program's
    ``default_commission_pct`` / 100 when omitted.
    """

    link_slug: str = Field(min_length=1, max_length=128)
    order_value_usd: Decimal = Field(gt=0, le=10000000)
    commission_usd: Decimal | None = Field(default=None, ge=0, le=10000000)


class AffiliateConversionResponse(BaseModel):
    event_id: uuid.UUID
    link_id: uuid.UUID
    program_id: uuid.UUID
    commission_usd: float


# ---------------------------------------------------------------------------
# Growth Engine P1: partners, tiers, applications, portal, Stripe ingest
# ---------------------------------------------------------------------------

PARTNER_TYPES = ("customer_referrer", "affiliate", "agency", "strategic")
PARTNER_STATUSES = ("pending", "approved", "rejected", "suspended")
APPLICATION_STATUSES = ("pending", "approved", "rejected")


class PartnerTierBase(BaseModel):
    name: str = Field(max_length=128)
    description: str | None = None


class PartnerTierCreate(PartnerTierBase):
    pass


class PartnerTierUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    description: str | None = None


class PartnerTierOut(PartnerTierBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    created_at: datetime


class PartnerBase(BaseModel):
    type: Literal["customer_referrer", "affiliate", "agency", "strategic"]
    name: str = Field(max_length=255)
    email: str | None = Field(default=None, max_length=320)
    tier_id: uuid.UUID | None = None


class PartnerCreate(PartnerBase):
    pass


class PartnerUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    tier_id: uuid.UUID | None = None
    status: Literal["pending", "approved", "rejected", "suspended"] | None = None


class PartnerOut(PartnerBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    status: str
    referral_code: str
    created_at: datetime


class PartnerApplicationCreate(BaseModel):
    """Public application to become a partner (no auth required)."""

    type: Literal["customer_referrer", "affiliate", "agency", "strategic"]
    form_data: dict = Field(default_factory=dict)


class PartnerApplicationOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    partner_id: uuid.UUID | None
    type: str
    form_data: dict
    status: str
    reviewed_by: uuid.UUID | None
    created_at: datetime


class PartnerUserCreate(BaseModel):
    """Create a portal login for an approved partner (owner/admin)."""

    partner_id: uuid.UUID
    email: str = Field(max_length=320)
    password: str = Field(min_length=8, max_length=128)


class PartnerUserOut(ORMModel):
    id: uuid.UUID
    partner_id: uuid.UUID | None
    email: str
    is_active: bool
    created_at: datetime


class PortalLoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=128)


class PortalLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class PortalProfileOut(BaseModel):
    partner_id: uuid.UUID
    business_id: uuid.UUID
    email: str
    name: str
    type: str
    status: str
    referral_code: str
    referral_link: str
    tier_name: str | None


class StripeOrderEventOut(ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    stripe_event_id: str
    event_type: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Customer subscription billing (Stripe Checkout signup flow)
# ---------------------------------------------------------------------------

# Plan key -> (display name, monthly price in cents). Price IDs come from
# env (STRIPE_PRICE_STARTER etc.) so no secrets live in code.
SUBSCRIPTION_PLANS: dict[str, dict] = {
    "starter": {"name": "Starter", "price_cents": 4900, "businesses": 1},
    "professional": {"name": "Professional", "price_cents": 14900, "businesses": 5},
    "enterprise": {"name": "Enterprise", "price_cents": 49900, "businesses": -1},
}


class CheckoutRequest(BaseModel):
    plan: str = Field(pattern="^(starter|professional|enterprise)$")
    email: str | None = Field(default=None, max_length=320)


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str


class CompleteSignupRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=255)
    business_name: str = Field(min_length=1, max_length=255)
    full_name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=128)


class CompleteSignupResponse(BaseModel):
    token: str
    business_id: uuid.UUID
    business_name: str


# ---------------------------------------------------------------------------
# Travel Agency workspace (Phase 1: CRM foundation)
# ---------------------------------------------------------------------------


class TravelCustomerBase(BaseModel):
    name: str = Field(max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    type: str = Field(default="individual", pattern="^(individual|corporate)$")
    notes: str | None = None


class TravelCustomerCreate(TravelCustomerBase):
    pass


class TravelCustomerUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    type: str | None = Field(default=None, pattern="^(individual|corporate)$")
    notes: str | None = None


class TravelCustomerOut(TravelCustomerBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    created_at: datetime


class TravelerProfileBase(BaseModel):
    customer_id: uuid.UUID | None = None
    full_name: str = Field(max_length=255)
    dob: date | None = None
    preferences: dict[str, Any] = Field(default_factory=dict)
    loyalty: dict[str, Any] = Field(default_factory=dict)
    accessibility_notes: str | None = None


class TravelerProfileCreate(TravelerProfileBase):
    pass


class TravelerProfileUpdate(BaseModel):
    customer_id: uuid.UUID | None = None
    full_name: str | None = Field(default=None, max_length=255)
    dob: date | None = None
    preferences: dict[str, Any] | None = None
    loyalty: dict[str, Any] | None = None
    accessibility_notes: str | None = None


class TravelerProfileOut(TravelerProfileBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    created_at: datetime


class TravelLeadBase(BaseModel):
    customer_id: uuid.UUID | None = None
    source: str | None = Field(default=None, max_length=64)
    destination: str | None = Field(default=None, max_length=255)
    date_start: date | None = None
    date_end: date | None = None
    budget: Decimal | None = Field(default=None, ge=0)
    trip_purpose: str | None = Field(default=None, max_length=255)
    assigned_to: uuid.UUID | None = None
    status: str = Field(default="new")


class TravelLeadCreate(TravelLeadBase):
    pass


class TravelLeadUpdate(BaseModel):
    customer_id: uuid.UUID | None = None
    source: str | None = Field(default=None, max_length=64)
    destination: str | None = Field(default=None, max_length=255)
    date_start: date | None = None
    date_end: date | None = None
    budget: Decimal | None = Field(default=None, ge=0)
    trip_purpose: str | None = Field(default=None, max_length=255)
    assigned_to: uuid.UUID | None = None


class TravelLeadStatusUpdate(BaseModel):
    status: str = Field(pattern="^(new|qualified|quoted|booked|lost)$")


class TravelLeadOut(TravelLeadBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    created_at: datetime


class TripRequestBase(BaseModel):
    lead_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    party_size: int = Field(default=1, ge=1, le=500)
    origin: str | None = Field(default=None, max_length=255)
    destinations: list[str] = Field(default_factory=list)
    date_start: date | None = None
    date_end: date | None = None
    preferences: dict[str, Any] = Field(default_factory=dict)
    flexibility: str | None = Field(default=None, max_length=255)
    status: str = Field(default="open")


class TripRequestCreate(TripRequestBase):
    pass


class TripRequestUpdate(BaseModel):
    lead_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    party_size: int | None = Field(default=None, ge=1, le=500)
    origin: str | None = Field(default=None, max_length=255)
    destinations: list[str] | None = None
    date_start: date | None = None
    date_end: date | None = None
    preferences: dict[str, Any] | None = None
    flexibility: str | None = Field(default=None, max_length=255)
    status: str | None = Field(
        default=None, pattern="^(draft|open|in_progress|completed|cancelled)$"
    )


class TripRequestOut(TripRequestBase, ORMModel):
    id: uuid.UUID
    business_id: uuid.UUID
    created_at: datetime


class TravelDashboardOut(BaseModel):
    lead_counts: dict[str, int]
    recent_leads: list[TravelLeadOut]
    customer_count: int
    trip_request_count: int


# ---------------------------------------------------------------------------
# Audit log (admin-only read)
# ---------------------------------------------------------------------------


class AuditLogOut(ORMModel):
    id: uuid.UUID
    created_at: datetime
    actor_type: str
    actor_id: str | None = None
    actor_email: str | None = None
    business_id: uuid.UUID | None = None
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    details: dict[str, Any] = {}
    ip_address: str | None = None
    user_agent: str | None = None
