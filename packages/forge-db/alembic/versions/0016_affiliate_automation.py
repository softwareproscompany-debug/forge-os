"""Affiliate automation rules.

Revision ID: 0016_affiliate_automation
Revises: 0015_billing_subscription
Create Date: 2026-10-09

Adds ``affiliate_automation_rules`` for scheduled affiliate pipeline
automation: auto-import products, auto-generate draft ads, and full
autopilot sweeps (discover → import → ads → draft campaign).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_affiliate_automation"
down_revision = "0015_billing_subscription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "affiliate_automation_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("rule_type", sa.String(32), nullable=False),
        sa.Column("network", sa.String(64), nullable=False, server_default="viator"),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("schedule", sa.String(16), nullable=False, server_default="weekly"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_result", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "rule_type IN ('auto_import', 'auto_ads', 'autopilot_sweep')",
            name="ck_affiliate_rules_type",
        ),
        sa.CheckConstraint(
            "schedule IN ('daily', 'weekly')",
            name="ck_affiliate_rules_schedule",
        ),
    )
    op.create_index(
        "ix_affiliate_rules_biz_enabled",
        "affiliate_automation_rules",
        ["business_id", "enabled"],
    )


def downgrade() -> None:
    op.drop_index("ix_affiliate_rules_biz_enabled", "affiliate_automation_rules")
    op.drop_table("affiliate_automation_rules")
