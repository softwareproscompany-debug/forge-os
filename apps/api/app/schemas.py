"""Pydantic v2 request/response schemas for every endpoint in CONTRACTS.md."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

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


class TemplateCreate(TemplateBase):
    pass


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    channel: Literal["email", "sms", "social"] | None = None
    subject_template: str | None = None
    body_template: str | None = None
    variables: list[str] | None = None


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
    created_at: datetime


class AssetApprovalRequest(BaseModel):
    note: str | None = None


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


class CampaignCreate(CampaignBase):
    pass


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    autopilot: bool | None = None
    starts_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    status: Literal["draft", "scheduled", "running", "paused", "completed"] | None = None


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


class AutopilotUpdate(BaseModel):
    auto_approve: bool | None = None
    require_approval_for_channels: list[str] | None = None
    daily_send_cap: int | None = Field(default=None, ge=1)
    quiet_hours_start: int | None = Field(default=None, ge=0, le=23)
    quiet_hours_end: int | None = Field(default=None, ge=0, le=23)


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
