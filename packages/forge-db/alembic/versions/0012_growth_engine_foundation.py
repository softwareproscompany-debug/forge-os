"""Growth Engine P1 foundation: partner_tiers, partners, partner_users,
partner_applications, stripe_order_events.

Revision ID: 0012_growth_engine_foundation
Revises: 0011_os_screens
Create Date: 2026-10-09

INBOUND partner management (partners promote us, we pay them) — the
inverse of the outbound affiliate module; these tables are new, never
reused. Partner type/status are plain VARCHAR (not Postgres enums) so
they stay easy to extend. No CREATE TYPE is emitted anywhere (see the
0010 duplicate-object fix) — checkfirst is used defensively.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0012_growth_engine_foundation"
down_revision = "0011_os_screens"
branch_labels = None
depends_on = None


def _id_col() -> sa.Column:
    return sa.Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _created_at_col() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def _business_fk_col() -> sa.Column:
    return sa.Column("business_id", UUID(as_uuid=True), nullable=False)


def upgrade() -> None:
    op.create_table(
        "partner_tiers",
        _id_col(),
        _business_fk_col(),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        _created_at_col(),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_partner_tiers_business_id", "partner_tiers", ["business_id"]
    )

    op.create_table(
        "partners",
        _id_col(),
        _business_fk_col(),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default="pending"
        ),
        sa.Column("tier_id", UUID(as_uuid=True), nullable=True),
        sa.Column("referral_code", sa.String(64), nullable=False),
        _created_at_col(),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tier_id"], ["partner_tiers.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "business_id", "referral_code", name="uq_partners_biz_code"
        ),
    )
    op.create_index("ix_partners_business_id", "partners", ["business_id"])

    op.create_table(
        "partner_users",
        _id_col(),
        sa.Column("partner_id", UUID(as_uuid=True), nullable=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default="true"
        ),
        _created_at_col(),
        sa.ForeignKeyConstraint(
            ["partner_id"], ["partners.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("email", name="uq_partner_users_email"),
    )
    op.create_index("ix_partner_users_email", "partner_users", ["email"])
    op.create_index(
        "ix_partner_users_partner_id", "partner_users", ["partner_id"]
    )

    op.create_table(
        "partner_applications",
        _id_col(),
        _business_fk_col(),
        sa.Column("partner_id", UUID(as_uuid=True), nullable=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column(
            "form_data", JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default="pending"
        ),
        sa.Column("reviewed_by", UUID(as_uuid=True), nullable=True),
        _created_at_col(),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["partner_id"], ["partners.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"], ["users.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_partner_applications_business_id",
        "partner_applications",
        ["business_id"],
    )

    op.create_table(
        "stripe_order_events",
        _id_col(),
        _business_fk_col(),
        sa.Column("stripe_event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        _created_at_col(),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "business_id",
            "stripe_event_id",
            name="uq_stripe_order_events_biz_event",
        ),
    )
    op.create_index(
        "ix_stripe_order_events_business_id",
        "stripe_order_events",
        ["business_id"],
    )


def downgrade() -> None:
    op.drop_table("stripe_order_events")
    op.drop_table("partner_applications")
    op.drop_table("partner_users")
    op.drop_table("partners")
    op.drop_table("partner_tiers")
