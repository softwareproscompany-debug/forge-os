"""ForgeOS initial schema — all tables from CONTRACTS.md.

Revision ID: 0001_initial
Revises: (none)
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def _business_fk() -> sa.Column:
    return sa.Column(
        "business_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )


def _check(table: str, column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({quoted})", name=f"ck_{table}_{column}")


ASSET_KINDS = ("email_copy", "social_post", "sms", "blog", "ad", "image_prompt")
ASSET_STATUSES = ("draft", "in_review", "approved", "rejected")
USER_ROLES = ("owner", "admin", "member")
CAMPAIGN_STATUSES = ("draft", "scheduled", "running", "paused", "completed")
CHANNELS = ("email", "sms", "social")
ENROLLMENT_STATUSES = ("active", "paused", "completed", "unsubscribed")
SEND_STATUSES = ("queued", "sending", "sent", "delivered", "bounced", "failed")
APPROVAL_DECISIONS = ("approved", "rejected")


def upgrade() -> None:
    # gen_random_uuid() is built into PostgreSQL >= 13; the extension is only
    # needed on older servers. The DO block keeps the migration transactional
    # even when contrib modules are absent.
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                CREATE EXTENSION IF NOT EXISTS pgcrypto;
            EXCEPTION WHEN OTHERS THEN
                RAISE NOTICE 'pgcrypto unavailable; assuming gen_random_uuid() is built in';
            END $$;
            """
        )
    )

    # -- businesses ---------------------------------------------------------
    op.create_table(
        "businesses",
        _uuid_pk(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        _created_at(),
        sa.UniqueConstraint("slug", name="uq_businesses_slug"),
    )

    # -- users --------------------------------------------------------------
    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        _business_fk(),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        _created_at(),
        _check("users", "role", USER_ROLES),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_business_id", "users", ["business_id"])

    # -- brand_kits ---------------------------------------------------------
    op.create_table(
        "brand_kits",
        _uuid_pk(),
        _business_fk(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("voice_description", sa.Text(), nullable=True),
        sa.Column("tone_tags", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("primary_color", sa.String(32), nullable=True),
        sa.Column("secondary_color", sa.String(32), nullable=True),
        sa.Column("fonts", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("icp_description", sa.Text(), nullable=True),
        sa.Column("do_list", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("dont_list", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        _created_at(),
    )
    op.create_index("ix_brand_kits_business_id", "brand_kits", ["business_id"])

    # -- contacts -----------------------------------------------------------
    op.create_table(
        "contacts",
        _uuid_pk(),
        _business_fk(),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("phone", sa.String(64), nullable=True),
        sa.Column("first_name", sa.String(255), nullable=True),
        sa.Column("last_name", sa.String(255), nullable=True),
        sa.Column("source", sa.String(255), nullable=True),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("consent_email", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("consent_email_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_sms", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("consent_sms_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unsubscribed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("custom_fields", postgresql.JSONB(), nullable=False, server_default="{}"),
        _created_at(),
    )
    op.create_index("ix_contacts_business_id", "contacts", ["business_id"])
    op.create_index("ix_contacts_email", "contacts", ["email"])
    op.create_index("ix_contacts_phone", "contacts", ["phone"])

    # -- templates ----------------------------------------------------------
    op.create_table(
        "templates",
        _uuid_pk(),
        _business_fk(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("subject_template", sa.Text(), nullable=True),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column("variables", postgresql.JSONB(), nullable=False, server_default="[]"),
        _check("templates", "channel", CHANNELS),
        _created_at(),
    )
    op.create_index("ix_templates_business_id", "templates", ["business_id"])

    # -- assets -------------------------------------------------------------
    op.create_table(
        "assets",
        _uuid_pk(),
        _business_fk(),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("variables", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="'draft'"),
        sa.Column("brand_kit_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "approved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column(
            "parent_asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("cost_usd", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_provider", sa.String(64), nullable=True),
        sa.Column("llm_model", sa.String(128), nullable=True),
        _check("assets", "kind", ASSET_KINDS),
        _check("assets", "status", ASSET_STATUSES),
        _created_at(),
    )
    op.create_index("ix_assets_business_id", "assets", ["business_id"])
    op.create_index("ix_assets_status", "assets", ["status"])
    op.create_index("ix_assets_kind", "assets", ["kind"])

    # -- asset_approvals ----------------------------------------------------
    op.create_table(
        "asset_approvals",
        _uuid_pk(),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _check("asset_approvals", "decision", APPROVAL_DECISIONS),
        _created_at(),
    )
    op.create_index("ix_asset_approvals_asset_id", "asset_approvals", ["asset_id"])

    # -- campaigns ----------------------------------------------------------
    op.create_table(
        "campaigns",
        _uuid_pk(),
        _business_fk(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="'draft'"),
        sa.Column("autopilot", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        _check("campaigns", "status", CAMPAIGN_STATUSES),
        _created_at(),
    )
    op.create_index("ix_campaigns_business_id", "campaigns", ["business_id"])

    # -- campaign_steps -----------------------------------------------------
    op.create_table(
        "campaign_steps",
        _uuid_pk(),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column(
            "template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("delay_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("trigger_event", sa.String(128), nullable=True),
        _check("campaign_steps", "channel", CHANNELS),
        _created_at(),
    )
    op.create_index("ix_campaign_steps_campaign_id", "campaign_steps", ["campaign_id"])

    # -- campaign_enrollments ------------------------------------------------
    op.create_table(
        "campaign_enrollments",
        _uuid_pk(),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="'active'"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        _check("campaign_enrollments", "status", ENROLLMENT_STATUSES),
        _created_at(),
    )
    op.create_index("ix_campaign_enrollments_campaign_id", "campaign_enrollments", ["campaign_id"])
    op.create_index("ix_campaign_enrollments_contact_id", "campaign_enrollments", ["contact_id"])

    # -- sends --------------------------------------------------------------
    op.create_table(
        "sends",
        _uuid_pk(),
        _business_fk(),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "step_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaign_steps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("to_address", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="'queued'"),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=False, server_default="{}"),
        _check("sends", "channel", CHANNELS),
        _check("sends", "status", SEND_STATUSES),
        _created_at(),
    )
    op.create_index("ix_sends_business_id", "sends", ["business_id"])
    op.create_index("ix_sends_status", "sends", ["status"])
    op.create_index("ix_sends_contact_id", "sends", ["contact_id"])
    op.create_index("ix_sends_provider_message_id", "sends", ["provider_message_id"])

    # -- events -------------------------------------------------------------
    op.create_table(
        "events",
        _uuid_pk(),
        _business_fk(),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        _created_at(),
    )
    op.create_index("ix_events_business_id", "events", ["business_id"])
    op.create_index("ix_events_kind", "events", ["kind"])

    # -- autopilot_settings -------------------------------------------------
    op.create_table(
        "autopilot_settings",
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("auto_approve", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "require_approval_for_channels",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("daily_send_cap", sa.Integer(), nullable=False, server_default="500"),
        sa.Column("quiet_hours_start", sa.Integer(), nullable=False, server_default="22"),
        sa.Column("quiet_hours_end", sa.Integer(), nullable=False, server_default="8"),
        _created_at(),
    )

    # -- generation_logs ----------------------------------------------------
    op.create_table(
        "generation_logs",
        _uuid_pk(),
        _business_fk(),
        sa.Column(
            "asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_hash", sa.String(128), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        _created_at(),
    )
    op.create_index("ix_generation_logs_business_id", "generation_logs", ["business_id"])
    op.create_index("ix_generation_logs_asset_id", "generation_logs", ["asset_id"])

    # -- dev_outbox ---------------------------------------------------------
    op.create_table(
        "dev_outbox",
        _uuid_pk(),
        _business_fk(),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("to_address", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False, server_default="stub"),
        _created_at(),
    )
    op.create_index("ix_dev_outbox_business_id", "dev_outbox", ["business_id"])


def downgrade() -> None:
    for table in (
        "dev_outbox",
        "generation_logs",
        "autopilot_settings",
        "events",
        "sends",
        "campaign_enrollments",
        "campaign_steps",
        "campaigns",
        "asset_approvals",
        "assets",
        "templates",
        "contacts",
        "brand_kits",
        "users",
        "businesses",
    ):
        op.drop_table(table)
