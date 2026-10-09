"""Customer subscription billing (Stripe Checkout).

Revision ID: 0015_billing_subscription
Revises: 0014_travel_agency_crm
Create Date: 2026-10-09

Adds Stripe subscription fields to ``businesses`` for the customer
signup flow (landing page → Stripe Checkout → webhook → account).
Additive only — all columns nullable with safe defaults.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_billing_subscription"
down_revision = "0014_travel_agency_crm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column("stripe_customer_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("stripe_subscription_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column("stripe_session_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "subscription_status",
            sa.String(32),
            nullable=False,
            server_default="none",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column("subscription_plan", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_businesses_stripe_customer_id",
        "businesses",
        ["stripe_customer_id"],
    )
    op.create_index(
        "ix_businesses_stripe_session_id",
        "businesses",
        ["stripe_session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_businesses_stripe_session_id", table_name="businesses")
    op.drop_index("ix_businesses_stripe_customer_id", table_name="businesses")
    op.drop_column("businesses", "subscription_plan")
    op.drop_column("businesses", "subscription_status")
    op.drop_column("businesses", "stripe_session_id")
    op.drop_column("businesses", "stripe_subscription_id")
    op.drop_column("businesses", "stripe_customer_id")
