"""ForgeOS database models — SQLAlchemy 2.0 typed declarative.

Covers every table in CONTRACTS.md. Designed to run on PostgreSQL in
production (native UUID, JSONB, gen_random_uuid() server defaults) while
remaining portable to SQLite for tests.

Conventions
-----------
* UUID primary keys: ``postgresql.UUID(as_uuid=True)`` on Postgres,
  ``CHAR(32)`` on SQLite via ``with_variant``.
* Client-side ``default=uuid.uuid4`` keeps SQLite inserts working; the
  server default ``gen_random_uuid()`` still applies to raw-SQL writes
  (e.g. ``infra/seed.py``).
* JSON columns: generic ``JSON`` with a ``JSONB`` variant for PostgreSQL.
* Enum columns: Python ``str`` enums rendered as ``VARCHAR`` + ``CHECK``
  constraints (``native_enum=False``) — one consistent rule everywhere,
  portable across dialects.
* ``business_id`` scopes every tenant table; tables scoped indirectly
  (``campaign_steps``, ``campaign_enrollments``, ``asset_approvals``)
  resolve tenancy through their parent joins.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

__all__ = [
    "Base",
    "AssetKind",
    "AssetStatus",
    "UserRole",
    "CampaignStatus",
    "Channel",
    "EnrollmentStatus",
    "SendStatus",
    "ApprovalDecision",
    "EventKind",
    "Business",
    "User",
    "BrandKit",
    "Contact",
    "Asset",
    "AssetApproval",
    "Template",
    "Campaign",
    "CampaignStep",
    "CampaignEnrollment",
    "Send",
    "Event",
    "AutopilotSettings",
    "GenerationLog",
    "DevOutbox",
    "InterviewStatus",
    "PlanStatus",
    "InterviewSession",
    "WeeklySummary",
    "ContentPlan",
    "AffiliateProgram",
    "AffiliateLink",
    "DravenToolRun",
    "DravenProviderConfig",
    "MarketResearchJob",
    "MarketOpportunity",
    "MarketSnapshot",
    "MarketSource",
    "ResearchJobStatus",
    "RecommendationStatus",
    "LeadStatus",
    "QualificationVerdict",
    "RunState",
    "StepState",
    "AlphaApprovalStatus",
    "OutboundStatus",
    "DuplicateStatus",
    "Lead",
    "LeadDuplicate",
    "QualificationRules",
    "QualificationResult",
    "WorkflowRun",
    "WorkflowStep",
    "WorkflowTransition",
    "AlphaApproval",
    "OutboundMessage",
    "ActionEvidence",
    "LeadSource",
    "BusinessSecret",
]


# ---------------------------------------------------------------------------
# Dialect-portable column types
# ---------------------------------------------------------------------------

#: Native UUID on Postgres, CHAR(32) hex on SQLite (via SQLAlchemy's own
#: portable ``Uuid`` type, which also handles uuid<->hex bind conversion).
UUID = PG_UUID(as_uuid=True).with_variant(sa.Uuid(native_uuid=False), "sqlite")

#: JSONB on Postgres, plain JSON elsewhere.
JSONB = JSON().with_variant(PG_JSONB(), "postgresql")


class Base(DeclarativeBase):
    """Declarative base shared by the API, worker, and Alembic migrations."""


def _uuid_pk() -> Mapped[uuid.UUID]:
    # NOTE: the ``gen_random_uuid()`` *server* default lives in the Alembic
    # migration (postgres-only). Models carry only the client-side default so
    # ``Base.metadata.create_all`` stays portable (SQLite tests).
    return mapped_column(UUID, primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now().astimezone(),
        server_default=sa.func.now(),
    )


def _business_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID,
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def _enum_col(
    enum_cls: type[enum.Enum], *, length: int = 32, nullable: bool = False
) -> sa.Enum:
    """String enum rendered as VARCHAR + CHECK (``native_enum=False``)."""
    return sa.Enum(enum_cls, native_enum=False, length=length, validate_strings=True)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AssetKind(str, enum.Enum):
    email_copy = "email_copy"
    social_post = "social_post"
    sms = "sms"
    blog = "blog"
    ad = "ad"
    image_prompt = "image_prompt"


class AssetStatus(str, enum.Enum):
    draft = "draft"
    in_review = "in_review"
    approved = "approved"
    rejected = "rejected"


class UserRole(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    member = "member"


class CampaignStatus(str, enum.Enum):
    draft = "draft"
    scheduled = "scheduled"
    running = "running"
    paused = "paused"
    completed = "completed"


class Channel(str, enum.Enum):
    email = "email"
    sms = "sms"
    social = "social"


class EnrollmentStatus(str, enum.Enum):
    active = "active"
    paused = "paused"
    completed = "completed"
    unsubscribed = "unsubscribed"


class SendStatus(str, enum.Enum):
    queued = "queued"
    sending = "sending"
    sent = "sent"
    delivered = "delivered"
    bounced = "bounced"
    failed = "failed"


class ApprovalDecision(str, enum.Enum):
    approved = "approved"
    rejected = "rejected"


class InterviewStatus(str, enum.Enum):
    active = "active"
    completed = "completed"
    abandoned = "abandoned"


class PlanStatus(str, enum.Enum):
    draft = "draft"
    approved = "approved"
    rejected = "rejected"


class EventKind(str, enum.Enum):
    contact_added = "contact_added"
    email_opened = "email_opened"
    email_clicked = "email_clicked"
    sms_replied = "sms_replied"
    converted = "converted"
    affiliate_clicked = "affiliate_clicked"
    affiliate_converted = "affiliate_converted"


# ---------------------------------------------------------------------------
# Approval state machine (server-side, shared by API + worker)
# ---------------------------------------------------------------------------

#: Allowed status transitions for assets. ``submit`` performs draft->in_review,
#: ``approve`` performs in_review->approved, ``reject`` performs
#: in_review->rejected; a rejected asset may be reworked via rejected->draft.
ALLOWED_ASSET_TRANSITIONS: frozenset[tuple[AssetStatus, AssetStatus]] = frozenset(
    {
        (AssetStatus.draft, AssetStatus.in_review),
        (AssetStatus.in_review, AssetStatus.approved),
        (AssetStatus.in_review, AssetStatus.rejected),
        (AssetStatus.rejected, AssetStatus.draft),
    }
)


def assert_asset_transition(current: AssetStatus, target: AssetStatus) -> None:
    """Raise ``ValueError`` unless ``current -> target`` is a legal transition."""
    if (current, target) not in ALLOWED_ASSET_TRANSITIONS:
        raise ValueError(
            f"Illegal asset status transition: {current.value} -> {target.value}"
        )


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    created_at: Mapped[datetime] = _created_at()

    users: Mapped[list["User"]] = relationship(back_populates="business", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    business_id: Mapped[uuid.UUID] = _business_fk()
    role: Mapped[UserRole] = mapped_column(_enum_col(UserRole), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()

    business: Mapped["Business"] = relationship(back_populates="users")


class BrandKit(Base):
    __tablename__ = "brand_kits"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    voice_description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    tone_tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    primary_color: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    secondary_color: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    fonts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    icp_description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    do_list: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    dont_list: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _created_at()


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True, index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # CONTRACTS.md does not mark names nullable; they are nullable here so a
    # phone- or email-only lead (e.g. from the `contact_added` event) is valid.
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    consent_email: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    consent_email_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_sms: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    consent_sms_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    unsubscribed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    custom_fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    kind: Mapped[AssetKind] = mapped_column(_enum_col(AssetKind), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    status: Mapped[AssetStatus] = mapped_column(
        _enum_col(AssetStatus), nullable=False, default=AssetStatus.draft, index=True
    )
    brand_kit_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    parent_asset_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    cost_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False, default=Decimal("0"))
    tokens_in: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    llm_provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    llm_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Affiliate marketing: marks content that promotes third-party affiliate
    # offers. The guardrail pass auto-appends an FTC disclosure line when the
    # body lacks one; also auto-detected from /r/ links or program URLs.
    is_affiliate_content: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = _created_at()

    parent: Mapped[Optional["Asset"]] = relationship(
        "Asset", remote_side="Asset.id", back_populates="children"
    )
    children: Mapped[list["Asset"]] = relationship("Asset", back_populates="parent")
    approvals: Mapped[list["AssetApproval"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )


class AssetApproval(Base):
    __tablename__ = "asset_approvals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decision: Mapped[ApprovalDecision] = mapped_column(
        _enum_col(ApprovalDecision), nullable=False
    )
    note: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    asset: Mapped["Asset"] = relationship(back_populates="approvals")


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[Channel] = mapped_column(_enum_col(Channel), nullable=False)
    subject_template: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    body_template: Mapped[str] = mapped_column(sa.Text, nullable=False)
    variables: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = _created_at()


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    status: Mapped[CampaignStatus] = mapped_column(
        _enum_col(CampaignStatus), nullable=False, default=CampaignStatus.draft
    )
    autopilot: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    starts_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    created_at: Mapped[datetime] = _created_at()

    steps: Mapped[list["CampaignStep"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        order_by="CampaignStep.position",
    )
    enrollments: Mapped[list["CampaignEnrollment"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignStep(Base):
    __tablename__ = "campaign_steps"

    id: Mapped[uuid.UUID] = _uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    channel: Mapped[Channel] = mapped_column(_enum_col(Channel), nullable=False)
    template_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("templates.id", ondelete="SET NULL"), nullable=True
    )
    asset_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    delay_hours: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=24)
    trigger_event: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = _created_at()

    campaign: Mapped["Campaign"] = relationship(back_populates="steps")
    template: Mapped[Optional["Template"]] = relationship()
    asset: Mapped[Optional["Asset"]] = relationship()


class CampaignEnrollment(Base):
    __tablename__ = "campaign_enrollments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    current_step: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    status: Mapped[EnrollmentStatus] = mapped_column(
        _enum_col(EnrollmentStatus), nullable=False, default=EnrollmentStatus.active
    )
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()

    campaign: Mapped["Campaign"] = relationship(back_populates="enrollments")
    contact: Mapped["Contact"] = relationship()


class Send(Base):
    __tablename__ = "sends"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    step_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("campaign_steps.id", ondelete="SET NULL"), nullable=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[Channel] = mapped_column(_enum_col(Channel), nullable=False)
    asset_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    to_address: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[SendStatus] = mapped_column(
        _enum_col(SendStatus), nullable=False, default=SendStatus.queued, index=True
    )
    provider_message_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    error: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    scheduled_for: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    clicked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    converted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()

    contact: Mapped["Contact"] = relationship()
    asset: Mapped[Optional["Asset"]] = relationship()
    campaign: Mapped[Optional["Campaign"]] = relationship()
    step: Mapped[Optional["CampaignStep"]] = relationship()


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()


class AutopilotSettings(Base):
    __tablename__ = "autopilot_settings"

    # business_id doubles as the primary key (one settings row per business).
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True
    )
    auto_approve: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    require_approval_for_channels: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    daily_send_cap: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=500)
    quiet_hours_start: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=22)
    quiet_hours_end: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=8)
    # Card 4 planner schedule (per business; the hourly cron gates on these).
    # plan_day: local weekday 0=Monday..6=Sunday. plan_hour: local hour 0-23.
    # plan_cadence: "weekly" | "biweekly". last_planned_at: most recent draft
    # (cron or manual run-now) — the biweekly gate uses it to enforce the
    # ~13-day minimum gap between drafts.
    plan_day: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    plan_hour: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=6)
    plan_cadence: Mapped[str] = mapped_column(String(16), nullable=False, default="weekly")
    last_planned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


class GenerationLog(Base):
    __tablename__ = "generation_logs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    tokens_in: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False, default=Decimal("0"))
    latency_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created_at()


class DevOutbox(Base):
    __tablename__ = "dev_outbox"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    to_address: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="stub")
    created_at: Mapped[datetime] = _created_at()


# ---------------------------------------------------------------------------
# Card 0 / Card 4 / Card 5 — interviews, weekly summaries, content plans
# ---------------------------------------------------------------------------


class InterviewSession(Base):
    """Card 0 guided interview. One row per interview attempt.

    ``answers`` is a list of ``{"question": str, "answer": str,
    "followup": bool}`` in asked order. ``finish`` drafts the brand kit into
    ``draft_brand_kit`` (JSON shaped like a BrandKit payload); ``confirm``
    persists it as a real BrandKit row and links ``brand_kit_id``.
    """

    __tablename__ = "interview_sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[InterviewStatus] = mapped_column(
        _enum_col(InterviewStatus), nullable=False, default=InterviewStatus.active
    )
    current_index: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    answers: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    draft_brand_kit: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    brand_kit_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("brand_kits.id", ondelete="SET NULL"), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


class WeeklySummary(Base):
    """Card 5 weekly evidence summary. One row per (business, week_start).

    ``week_start`` is the Monday (date) of the week summarized. ``top_assets``
    / ``bottom_assets`` are lists of ``{"asset_id", "title", "kind",
    "delivered", "converted", "conversion_rate"}``. ``best_channel_per_segment``
    maps a contact-tag segment (or ``"untagged"``) to ``{"channel",
    "conversion_rate", "delivered"}``. ``recommendation`` is the
    "do more of this" paragraph the Origination brief reads.
    """

    __tablename__ = "weekly_summaries"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    week_start: Mapped[datetime.date] = mapped_column(sa.Date, nullable=False)
    top_assets: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    bottom_assets: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    best_channel_per_segment: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    recommendation: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    # Affiliate marketing (weekly evidence loop): top links by conversion
    # rate (min 5 clicks) as [{link_id, label, program_name, clicks,
    # conversions, conversion_rate, earnings_usd}], and total earnings_usd
    # for the week. Populated by the weekly_summary worker job.
    affiliate_top_links: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    affiliate_earnings_usd: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 4), nullable=False, default=Decimal("0")
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        sa.UniqueConstraint("business_id", "week_start", name="uq_weekly_summaries_biz_week"),
    )


class ContentPlan(Base):
    """Card 4 autopilot weekly content plan. Drafted at the business's
    configured plan moment (default local Monday 06:00, weekly), approved
    by a human, then materialized into a running campaign — approval is
    the launch gate, so the week runs itself.

    ``week_start`` is the target Monday (date). ``items`` is a list of
    ``{"kind", "channel", "day" (0=Monday), "title", "brief", "asset_id"?}``.
    Approving a draft creates the campaign and links ``campaign_id``.
    """

    __tablename__ = "content_plans"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    week_start: Mapped[datetime.date] = mapped_column(sa.Date, nullable=False)
    status: Mapped[PlanStatus] = mapped_column(
        _enum_col(PlanStatus), nullable=False, default=PlanStatus.draft
    )
    items: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


# ---------------------------------------------------------------------------
# Affiliate marketing — "we promote affiliate offers, we earn commissions"
# ---------------------------------------------------------------------------


class AffiliateProgram(Base):
    """A third-party affiliate program whose offers the business promotes.

    ``network`` is a free-form label (amazon, shareasale, cj, impact, direct,
    other, ...). ``default_commission_pct`` is the fallback rate used when a
    conversion postback does not carry an explicit commission amount.
    """

    __tablename__ = "affiliate_programs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    network: Mapped[str] = mapped_column(String(64), nullable=False, default="other")
    website_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    default_commission_pct: Mapped[Decimal] = mapped_column(
        sa.Numeric(6, 3), nullable=False, default=Decimal("0")
    )
    cookie_days: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    notes: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    links: Mapped[list["AffiliateLink"]] = relationship(
        back_populates="program", cascade="all, delete-orphan"
    )


class AffiliateLink(Base):
    """A trackable affiliate link.

    ``slug`` is unique per business and powers the public redirect
    ``GET /r/{slug}``. ``destination_url`` already contains the business's
    affiliate ID / tag. Clicks and conversions are recorded as
    ``affiliate_clicked`` / ``affiliate_converted`` rows in ``events`` with
    ``link_id`` (and ``program_id``, ``order_value_usd``, ``commission_usd``)
    in the payload.
    """

    __tablename__ = "affiliate_links"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    program_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("affiliate_programs.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    destination_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    utm_source: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    utm_medium: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    utm_campaign: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()

    program: Mapped["AffiliateProgram"] = relationship(back_populates="links")

    __table_args__ = (
        sa.UniqueConstraint(
            "business_id", "slug", name="uq_affiliate_links_biz_slug"
        ),
    )


def to_dict(obj: Base) -> dict[str, Any]:
    """Shallow-serialize a model instance (handy for debugging / logs)."""
    out: dict[str, Any] = {}
    for column in obj.__table__.columns:
        value = getattr(obj, column.name)
        if isinstance(value, uuid.UUID):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = float(value)
        elif isinstance(value, enum.Enum):
            value = value.value
        out[column.name] = value
    return out


class DravenToolRun(Base):
    """Audit trail for every Draven tool execution.

    Written for *all* outcomes — ``ok``, ``approval_required`` (medium/high
    risk tools never execute; the request is recorded instead), and
    ``error``. Lets operators see exactly what the assistant proposed,
    ran, or asked permission for.

    ``agent_id`` / ``swarm_run_id`` attribute the row to a swarm agent run
    when set (NULL for direct chat tool calls).
    """

    __tablename__ = "draven_tool_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    tool: Mapped[str] = mapped_column(String(128), nullable=False)
    input: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    output_summary: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    risk: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    agent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    swarm_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("draven_swarm_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


class DravenSwarmRunStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class DravenSwarmRun(Base):
    """One orchestrated multi-agent run.

    The supervisor decomposes the goal into subtasks, fans out to the 12
    agents (see ``app/draven_swarm.py``), and synthesizes a result. Every
    agent action executes through the real tool registry and is audit-
    logged to ``draven_tool_runs`` (attributed via ``swarm_run_id``).
    """

    __tablename__ = "draven_swarm_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    goal: Mapped[str] = mapped_column(sa.Text, nullable=False)
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[DravenSwarmRunStatus] = mapped_column(
        sa.Enum(DravenSwarmRunStatus, name="draven_swarm_run_status"),
        nullable=False,
        default=DravenSwarmRunStatus.queued,
    )
    current_phase: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    agent_results: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result_summary: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


class DravenSwarmEvent(Base):
    """Append-only live event feed for a swarm run.

    The frontend polls these (no fabricated activity — every row is written
    by the orchestrator or an agent at the moment the thing happens).
    ``seq`` gives total ordering per run for ``?after=`` polling.
    """

    __tablename__ = "draven_swarm_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    swarm_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("draven_swarm_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    business_id: Mapped[uuid.UUID] = _business_fk()
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    agent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        sa.UniqueConstraint("swarm_run_id", "seq", name="uq_swarm_events_run_seq"),
        sa.Index("ix_swarm_events_run_seq", "swarm_run_id", "seq"),
    )


class DravenProviderConfig(Base):
    """Per-business Draven LLM provider configuration.

    ``base_url_enc`` / ``api_key_enc`` hold Fernet-encrypted secrets —
    never plaintext, never returned to clients. One row per business
    (``business_id`` doubles as the primary key, like ``autopilot_settings``).
    """

    __tablename__ = "draven_provider_config"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    base_url_enc: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    api_key_enc: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    # TTS voice provider (separate from the chat LLM provider above).
    # Currently "elevenlabs" or None. Key is Fernet-encrypted like api_key_enc.
    tts_provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tts_api_key_enc: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now().astimezone(),
        onupdate=lambda: datetime.now().astimezone(),
        server_default=sa.func.now(),
    )


# ---------------------------------------------------------------------------
# Draven Market Intelligence
# ---------------------------------------------------------------------------


class ResearchJobStatus(str, enum.Enum):
    """Lifecycle of a market research job (deterministic staged pipeline)."""

    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class RecommendationStatus(str, enum.Enum):
    """Executive decision on a product opportunity."""

    prioritize = "PRIORITIZE"
    test_limited = "TEST_WITH_LIMITED_CAPITAL"
    watch = "WATCH_FOR_BETTER_TIMING"
    research_further = "RESEARCH_FURTHER"
    reject = "REJECT"
    undecided = "UNDECIDED"


class MarketResearchJob(Base):
    """One market-intelligence research run.

    The research pipeline is a deterministic staged pipeline (collect →
    normalize → score → economics → report), not an autonomous agent swarm.
    ``params`` holds the research parameters plus the inferred defaults
    shown to the user; ``audit_trail`` records each pipeline stage with
    timestamps and connector outcomes; ``results`` summarizes completed
    output (opportunity ids, counts, evidence gaps).
    """

    __tablename__ = "market_research_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    assumptions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[ResearchJobStatus] = mapped_column(
        _enum_col(ResearchJobStatus, length=16), nullable=False,
        default=ResearchJobStatus.queued,
    )
    progress: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    results: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    audit_trail: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    error: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MarketOpportunity(Base):
    """A researched product opportunity with full evidence provenance.

    Every quantitative field that comes from a connector is paired with
    provenance in the JSON columns (source, measurement period, retrieved
    at, geographic scope, confidence). Fields without provenance are
    labeled ``estimated`` in reports — never presented as measured fact.

    ``demand_indicators``: list of {kind, value, source, period, retrieved_at,
    geo, confidence} where kind ∈ {"search_interest", "marketplace_indicator",
    "confirmed_sales", "listing_count", "analyst_estimate", "forecast"}.
    Search interest is NEVER converted to units sold without a documented,
    validated estimation model (there is none in the foundation slice).

    ``price_evidence``: list of {price, currency, source, observed_at,
    kind ∈ {"advertised", "observed", "supplier_quote"}}.
    """

    __tablename__ = "market_opportunities"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    research_job_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID,
        ForeignKey("market_research_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(480), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    identifiers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_urls: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    price_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    demand_indicators: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    seasonality_profile: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    competition_summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    unit_economics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sourcing_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    risk_flags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    score_components: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    opportunity_score: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )
    confidence_score: Mapped[Optional[float]] = mapped_column(
        sa.Float, nullable=True
    )
    evidence_gaps: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    recommendation: Mapped[RecommendationStatus] = mapped_column(
        _enum_col(RecommendationStatus, length=32), nullable=False,
        default=RecommendationStatus.undecided,
    )
    saved: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now().astimezone(),
        onupdate=lambda: datetime.now().astimezone(),
        server_default=sa.func.now(),
    )


class MarketSnapshot(Base):
    """Point-in-time snapshot of an opportunity's score and economics.

    Written whenever an opportunity is created or rescored, so users can
    compare opportunities over time. Append-only; never updated.
    """

    __tablename__ = "market_snapshots"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("market_opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    research_job_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID,
        ForeignKey("market_research_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    opportunity_score: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    confidence_score: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    score_components: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    unit_economics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    recommendation: Mapped[RecommendationStatus] = mapped_column(
        _enum_col(RecommendationStatus, length=32), nullable=False,
        default=RecommendationStatus.undecided,
    )
    created_at: Mapped[datetime] = _created_at()


class MarketSource(Base):
    """Connector capability registry (one row per connector per business).

    Records what a connector can actually do — data available, auth,
    rate limits, geo/historical coverage, pricing, refresh frequency,
    blind spots — plus live health. ``configured`` is False until real
    credentials are present; the UI shows that honestly.
    """

    __tablename__ = "market_sources"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    connector: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    capability: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    configured: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    last_check_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_check_ok: Mapped[Optional[bool]] = mapped_column(sa.Boolean, nullable=True)
    last_check_note: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now().astimezone(),
        onupdate=lambda: datetime.now().astimezone(),
        server_default=sa.func.now(),
    )

    __table_args__ = (
        sa.UniqueConstraint("business_id", "connector", name="uq_market_source_biz_conn"),
    )


# ---------------------------------------------------------------------------
# Alpha workflows — lead-to-follow-up reliability slice
# ---------------------------------------------------------------------------


class LeadStatus(str, enum.Enum):
    new = "new"
    validated = "validated"
    qualified = "qualified"
    unqualified = "unqualified"
    needs_review = "needs_review"
    duplicate = "duplicate"
    archived = "archived"


class QualificationVerdict(str, enum.Enum):
    qualified = "qualified"
    unqualified = "unqualified"
    needs_review = "needs_review"


class RunState(str, enum.Enum):
    """Durable workflow state machine (see docs/ALPHA_WORKFLOWS.md).

    received → validating → (needs_review | normalizing) → qualifying →
    (needs_review | decided | drafting) → awaiting_approval →
    (rejected | drafting | rechecking) → submitting →
    (confirming | reconciling | failed) → completed.
    Terminal: needs_review, decided, rejected, completed, failed, cancelled.
    """

    received = "received"
    validating = "validating"
    normalizing = "normalizing"
    qualifying = "qualifying"
    drafting = "drafting"
    awaiting_approval = "awaiting_approval"
    rechecking = "rechecking"
    submitting = "submitting"
    confirming = "confirming"
    reconciling = "reconciling"
    needs_review = "needs_review"
    decided = "decided"
    rejected = "rejected"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class StepState(str, enum.Enum):
    pending = "pending"
    running = "running"
    ok = "ok"
    failed = "failed"
    skipped = "skipped"


class AlphaApprovalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"
    invalidated = "invalidated"


class OutboundStatus(str, enum.Enum):
    """Outbound message lifecycle. ``submitted`` means the provider accepted
    the request — it is NEVER presented as inbox delivery. ``confirmed``
    requires a positive ``verify_outcome`` read-back."""

    draft = "draft"
    approved = "approved"
    sending = "sending"
    submitted = "submitted"
    confirmed = "confirmed"
    failed = "failed"
    unknown = "unknown"


class DuplicateStatus(str, enum.Enum):
    pending = "pending"
    merged = "merged"
    dismissed = "dismissed"


class Lead(Base):
    """Normalized lead record. ``raw_payload`` preserves the original event;
    ``source_event_id`` + ``business_id`` is unique for webhook idempotency
    (NULLs are distinct on both Postgres and SQLite)."""

    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True, index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    company: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[LeadStatus] = mapped_column(
        _enum_col(LeadStatus), nullable=False, default=LeadStatus.new
    )
    duplicate_of_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now().astimezone(),
        onupdate=lambda: datetime.now().astimezone(),
        server_default=sa.func.now(),
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "business_id", "source_event_id", name="uq_leads_biz_source_event"
        ),
    )


class LeadDuplicate(Base):
    """Fuzzy duplicate candidate — always requires human review; a candidate
    never triggers a second follow-up on its own."""

    __tablename__ = "lead_duplicates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    match_reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[DuplicateStatus] = mapped_column(
        _enum_col(DuplicateStatus), nullable=False, default=DuplicateStatus.pending
    )
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


class QualificationRules(Base):
    """Versioned, immutable-once-used qualification rules per business."""

    __tablename__ = "qualification_rules"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    rules: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    threshold: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.6)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        sa.UniqueConstraint("business_id", "version", name="uq_qual_rules_biz_ver"),
    )


class QualificationResult(Base):
    """Deterministic verdict for one lead under one rules version."""

    __tablename__ = "qualification_results"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rules_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    verdict: Mapped[QualificationVerdict] = mapped_column(
        _enum_col(QualificationVerdict), nullable=False
    )
    score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    criteria: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = _created_at()


class WorkflowRun(Base):
    """One durable execution of the lead-to-follow-up state machine."""

    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[RunState] = mapped_column(
        _enum_col(RunState, length=32), nullable=False, default=RunState.received
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    current_step: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    retry_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    paused: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    cost_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    latency_ms: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now().astimezone(),
        onupdate=lambda: datetime.now().astimezone(),
        server_default=sa.func.now(),
    )


class WorkflowStep(Base):
    """One step execution inside a run (for the ledger timeline)."""

    __tablename__ = "workflow_steps"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[StepState] = mapped_column(
        _enum_col(StepState), nullable=False, default=StepState.pending
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


class WorkflowTransition(Base):
    """Append-only audit of every state transition."""

    __tablename__ = "workflow_transitions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    from_state: Mapped[str] = mapped_column(String(32), nullable=False)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class AlphaApproval(Base):
    """Approval bound to an immutable payload digest. Any material edit to
    the payload invalidates the approval (status → invalidated)."""

    __tablename__ = "alpha_approvals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[AlphaApprovalStatus] = mapped_column(
        _enum_col(AlphaApprovalStatus, length=32),
        nullable=False,
        default=AlphaApprovalStatus.pending,
    )
    requested_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approver_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


class OutboundMessage(Base):
    """One outbound follow-up. ``submitted`` = provider accepted the request
    (never presented as inbox delivery); ``confirmed`` requires a positive
    ``verify_outcome`` read-back."""

    __tablename__ = "outbound_messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    approval_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("alpha_approvals.id", ondelete="SET NULL"), nullable=True
    )
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="stub")
    provider_message_id: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True, index=True
    )
    status: Mapped[OutboundStatus] = mapped_column(
        _enum_col(OutboundStatus, length=32),
        nullable=False,
        default=OutboundStatus.draft,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    send_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now().astimezone(),
        onupdate=lambda: datetime.now().astimezone(),
        server_default=sa.func.now(),
    )


class ActionEvidence(Base):
    """Verified evidence for consequential actions (connector, result)."""

    __tablename__ = "action_evidence"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID,
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step: Mapped[str] = mapped_column(String(128), nullable=False)
    connector: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


class BusinessSecret(Base):
    """Centralized encrypted API-key vault (Settings).

    One row per ``(business_id, key_name)``. ``value_enc`` is a Fernet token
    — never plaintext, never returned to any client. Consumers read via
    ``app.settings_vault`` (server-side decrypt only).

    Known key names: ``elevenlabs.api_key``, ``anthropic.api_key``,
    ``openai.api_key``, ``openai.base_url``, ``dataforseo.login``,
    ``dataforseo.password``, ``webhook.<source>.secret``.
    """

    __tablename__ = "business_secrets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    key_name: Mapped[str] = mapped_column(String(128), nullable=False)
    value_enc: Mapped[str] = mapped_column(sa.Text, nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now().astimezone(),
        onupdate=lambda: datetime.now().astimezone(),
        server_default=sa.func.now(),
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "business_id", "key_name", name="uq_business_secrets_biz_key"
        ),
    )


class LeadSource(Base):
    """Webhook intake sources with HMAC secrets (Fernet-encrypted)."""

    __tablename__ = "lead_sources"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_enc: Mapped[str] = mapped_column(sa.Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        sa.UniqueConstraint("business_id", "source", name="uq_lead_source_biz_src"),
    )


class Opportunity(Base):
    """OS Pipeline: a deal moving through stages.

    Stages are plain strings (``new`` | ``qualified`` | ``proposal`` |
    ``negotiation`` | ``closed`` | ``lost``), validated at the API layer —
    deliberately not a Postgres enum so stages stay easy to extend.
    ``value_cents`` is the deal size in minor currency units.
    """

    __tablename__ = "opportunities"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    value_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    probability: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=50)
    expected_close_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Meeting(Base):
    """OS Meetings: scheduled business meetings with attendees and notes."""

    __tablename__ = "meetings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attendees: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    notes: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class KnowledgeDoc(Base):
    """OS Knowledge: business context documents the AI can draw on.

    Plain text store for now (company facts, FAQs, policies, product
    notes). Wiring into Draven's prompt context is a later step.
    """

    __tablename__ = "knowledge_docs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = _created_at()


# ---------------------------------------------------------------------------
# Growth Engine (P1 foundation): INBOUND partner management.
#
# Orientation: partners promote OUR products and we pay THEM commissions.
# This is the inverse of the outbound affiliate module (affiliate_programs /
# affiliate_links: we promote third-party offers, we earn). Do NOT reuse
# those tables for inbound partners.
#
# Partners are NEVER ``users`` rows — external identity lives in
# ``partner_users`` with partner-scoped JWTs (see app/core/partner_deps.py).
# ---------------------------------------------------------------------------


class PartnerTier(Base):
    """Named commission/recognition tier a partner can belong to."""

    __tablename__ = "partner_tiers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Partner(Base):
    """An inbound partner: customer referrer, affiliate, agency, strategic.

    ``type`` / ``status`` are plain strings (validated at the API layer),
    deliberately not Postgres enums so they stay easy to extend.
    ``referral_code`` is unique per business and backs the partner's
    referral link (the /p/{code} redirect lands in P2).
    """

    __tablename__ = "partners"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    tier_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("partner_tiers.id", ondelete="SET NULL"), nullable=True
    )
    referral_code: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    tier: Mapped[Optional["PartnerTier"]] = relationship()


class PartnerUser(Base):
    """External identity for partner portal logins. NOT a ``users`` row.

    A partner portal account must never see business internals; the
    partner-scoped JWT (partner_deps) only carries partner_id + business_id.
    ``partner_id`` is nullable so an account can exist before approval
    links it to a partner row.
    """

    __tablename__ = "partner_users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    partner_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("partners.id", ondelete="SET NULL"), nullable=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()

    partner: Mapped[Optional["Partner"]] = relationship()


class PartnerApplication(Base):
    """Inbound application to become a partner; reviewed by owner/admin."""

    __tablename__ = "partner_applications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    partner_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("partners.id", ondelete="SET NULL"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    form_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


class StripeOrderEvent(Base):
    """Raw Stripe webhook events: the order source of truth (P1 ingest).

    Append-only. ``stripe_event_id`` is unique per business so retried
    deliveries never double-store. Commission computation (P3) reads from
    here — it never trusts self-reported values.
    """

    __tablename__ = "stripe_order_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    stripe_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        sa.UniqueConstraint(
            "business_id", "stripe_event_id", name="uq_stripe_order_events_biz_event"
        ),
    )


class ComplianceIssue(Base):  # noqa: F811 — distinct from app.compliance.checker.ComplianceIssue
    """A compliance flag raised by the checker or the daily compliance bot.

    Bots never auto-delete or auto-edit — they flag for human review only.
    ``status``: open | acknowledged | resolved | dismissed.
    ``source``: "api_check" (user-initiated) | "bot_scan" (daily job).
    """

    __tablename__ = "compliance_issues"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="api_check")
    pack_id: Mapped[str] = mapped_column(String(64), nullable=False, default="ftc_baseline")
    content_type: Mapped[str] = mapped_column(String(64), nullable=False, default="social_post")
    # What was scanned: "asset" | "campaign" | "ad_hoc"
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False, default="ad_hoc")
    subject_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID, nullable=True)
    subject_title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    violations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", index=True)
    created_at: Mapped[datetime] = _created_at()


# ---------------------------------------------------------------------------
# Travel Agency workspace (Phase 1: Travel CRM foundation)
#
# Internal SaaS inside ForgeOS — one travel agency company == one
# ``businesses`` row. EVERY table here is scoped by ``business_id`` via
# ``_business_fk()``; all queries MUST filter on it. Never rename the
# existing tenant columns; migrations are backward-compatible (additive).
# ---------------------------------------------------------------------------


class TravelCustomerType(str, enum.Enum):
    individual = "individual"
    corporate = "corporate"


class TravelLeadStatus(str, enum.Enum):
    new = "new"
    qualified = "qualified"
    quoted = "quoted"
    booked = "booked"
    lost = "lost"


class TripRequestStatus(str, enum.Enum):
    draft = "draft"
    open = "open"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


class TravelCustomer(Base):
    """A travel agency's customer — an individual or a corporate account."""

    __tablename__ = "travel_customers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    type: Mapped[TravelCustomerType] = mapped_column(
        _enum_col(TravelCustomerType), nullable=False, default=TravelCustomerType.individual
    )
    notes: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        sa.Index("ix_travel_customers_biz_name", "business_id", "name"),
        sa.Index("ix_travel_customers_biz_email", "business_id", "email"),
    )


class TravelerProfile(Base):
    """A traveler belonging to an agency customer (passenger-level detail)."""

    __tablename__ = "traveler_profiles"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("travel_customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dob: Mapped[Optional[datetime.date]] = mapped_column(sa.Date, nullable=True)
    # Seat / meal / cabin preferences, etc. — flexible payload only.
    preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Loyalty memberships: {"airline": [{"program": ..., "number": ...}], ...}
    loyalty: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    accessibility_notes: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        sa.Index("ix_traveler_profiles_biz_name", "business_id", "full_name"),
    )


class TravelLead(Base):
    """A sales lead: someone interested in travel. Status pipeline:

    new -> qualified -> quoted -> booked | lost
    """

    __tablename__ = "travel_leads"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("travel_customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    destination: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    date_start: Mapped[Optional[datetime.date]] = mapped_column(sa.Date, nullable=True)
    date_end: Mapped[Optional[datetime.date]] = mapped_column(sa.Date, nullable=True)
    budget: Mapped[Optional[Decimal]] = mapped_column(
        sa.Numeric(12, 2), nullable=True
    )
    trip_purpose: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    assigned_to: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[TravelLeadStatus] = mapped_column(
        _enum_col(TravelLeadStatus), nullable=False, default=TravelLeadStatus.new, index=True
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        sa.Index("ix_travel_leads_biz_status", "business_id", "status"),
    )


class TripRequest(Base):
    """A concrete trip request derived from a qualified lead."""

    __tablename__ = "trip_requests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    lead_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("travel_leads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID, ForeignKey("travel_customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    party_size: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    origin: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Ordered list of destinations: ["CDG", "NCE"] or full names.
    destinations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    date_start: Mapped[Optional[datetime.date]] = mapped_column(sa.Date, nullable=True)
    date_end: Mapped[Optional[datetime.date]] = mapped_column(sa.Date, nullable=True)
    preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    flexibility: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[TripRequestStatus] = mapped_column(
        _enum_col(TripRequestStatus), nullable=False, default=TripRequestStatus.open, index=True
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        sa.Index("ix_trip_requests_biz_status", "business_id", "status"),
    )
