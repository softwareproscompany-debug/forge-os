"""ForgeOS cards schema — interview sessions, weekly summaries, content plans.

Revision ID: 0002_cards
Revises: 0001_initial
Create Date: 2026-10-09

Adds the three tables backing the remaining FORGE cards:

* ``interview_sessions`` — Card 0 guided interview (Q&A + drafted brand kit).
* ``weekly_summaries`` — Card 5 weekly evidence summary (one per business/week).
* ``content_plans`` — Card 4 autopilot weekly plan (draft -> approved -> campaign).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_cards"
down_revision = "0001_initial"
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


def _user_fk_nullable(column: str) -> sa.Column:
    return sa.Column(
        column,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


def _check(table: str, column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({quoted})", name=f"ck_{table}_{column}")


INTERVIEW_STATUSES = ("active", "completed", "abandoned")
PLAN_STATUSES = ("draft", "approved", "rejected")


def upgrade() -> None:
    # -- interview_sessions -------------------------------------------------
    op.create_table(
        "interview_sessions",
        _uuid_pk(),
        _business_fk(),
        _user_fk_nullable("user_id"),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default="'active'"
        ),
        sa.Column("current_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "answers", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column("draft_brand_kit", postgresql.JSONB(), nullable=True),
        sa.Column(
            "brand_kit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("brand_kits.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        _check("interview_sessions", "status", INTERVIEW_STATUSES),
        _created_at(),
    )
    op.create_index(
        "ix_interview_sessions_business_id", "interview_sessions", ["business_id"]
    )
    op.create_index(
        "ix_interview_sessions_status", "interview_sessions", ["status"]
    )

    # -- weekly_summaries ---------------------------------------------------
    op.create_table(
        "weekly_summaries",
        _uuid_pk(),
        _business_fk(),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column(
            "top_assets", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "bottom_assets", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "best_channel_per_segment",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("recommendation", sa.Text(), nullable=True),
        _created_at(),
    )
    op.create_index(
        "ix_weekly_summaries_business_id", "weekly_summaries", ["business_id"]
    )
    op.create_unique_constraint(
        "uq_weekly_summaries_biz_week",
        "weekly_summaries",
        ["business_id", "week_start"],
    )

    # -- content_plans ------------------------------------------------------
    op.create_table(
        "content_plans",
        _uuid_pk(),
        _business_fk(),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="'draft'"),
        sa.Column("items", postgresql.JSONB(), nullable=False, server_default="[]"),
        _user_fk_nullable("created_by"),
        _user_fk_nullable("approved_by"),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _check("content_plans", "status", PLAN_STATUSES),
        _created_at(),
    )
    op.create_index(
        "ix_content_plans_business_id", "content_plans", ["business_id"]
    )
    op.create_index("ix_content_plans_status", "content_plans", ["status"])


def downgrade() -> None:
    for table in ("content_plans", "weekly_summaries", "interview_sessions"):
        op.drop_table(table)
