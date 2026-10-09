"""Compliance engine: compliance_issues table for bot-flagged issues.

Revision ID: 0013_compliance_engine
Revises: 0012_growth_engine_foundation
Create Date: 2026-10-09

Stores flags from the FTC/affiliate compliance checker and the daily
compliance bot. Bots flag for human review only — never auto-delete or
auto-edit. No CREATE TYPE emitted (see the 0010 duplicate-object fix).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0013_compliance_engine"
down_revision = "0012_growth_engine_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compliance_issues",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default="api_check"),
        sa.Column("pack_id", sa.String(64), nullable=False, server_default="ftc_baseline"),
        sa.Column("content_type", sa.String(64), nullable=False, server_default="social_post"),
        sa.Column("subject_type", sa.String(32), nullable=False, server_default="ad_hoc"),
        sa.Column("subject_id", UUID(as_uuid=True), nullable=True),
        sa.Column("subject_title", sa.String(500), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("violations", JSONB, nullable=False, server_default="[]"),
        sa.Column("status", sa.String(32), nullable=False, server_default="open"),
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
        "ix_compliance_issues_status",
        "compliance_issues",
        ["status"],
    )
    op.create_index(
        "ix_compliance_issues_business",
        "compliance_issues",
        ["business_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_compliance_issues_business", table_name="compliance_issues")
    op.drop_index("ix_compliance_issues_status", table_name="compliance_issues")
    op.drop_table("compliance_issues")
