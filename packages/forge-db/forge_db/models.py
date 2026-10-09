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
    """Card 4 autopilot weekly content plan. Drafted Monday 06:00, approved
    by a human, then materialized into a scheduled campaign.

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
