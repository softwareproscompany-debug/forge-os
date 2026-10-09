"""OS screens: opportunities, meetings, knowledge_docs tables.

Revision ID: 0011_os_screens
Revises: 0010_autopilot_schedule
Create Date: 2026-10-09

Additive tables for the business-OS screens (Pipeline, Meetings,
Knowledge). All tenant-scoped via ``business_id`` FK. Stages are plain
text (not a Postgres enum) so they stay easy to extend.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0011_os_screens"
down_revision = "0010_autopilot_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opportunities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("contact_id", UUID(as_uuid=True), nullable=True),
        sa.Column("value_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage", sa.String(32), nullable=False, server_default="new"),
        sa.Column("probability", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("expected_close_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"], ["contacts.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_opportunities_business_id", "opportunities", ["business_id"]
    )

    op.create_table(
        "meetings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attendees", JSONB(), nullable=False, server_default="[]"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_meetings_business_id", "meetings", ["business_id"])

    op.create_table(
        "knowledge_docs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_knowledge_docs_business_id", "knowledge_docs", ["business_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_docs_business_id", table_name="knowledge_docs")
    op.drop_table("knowledge_docs")
    op.drop_index("ix_meetings_business_id", table_name="meetings")
    op.drop_table("meetings")
    op.drop_index("ix_opportunities_business_id", table_name="opportunities")
    op.drop_table("opportunities")
