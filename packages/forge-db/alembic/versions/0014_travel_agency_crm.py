"""Travel Agency workspace Phase 1: travel CRM foundation tables.

Revision ID: 0014_travel_agency_crm
Revises: 0013_compliance_engine
Create Date: 2026-10-09

Additive only — no existing tables or columns touched. All new tables are
scoped by ``business_id`` (FK to businesses.id, CASCADE delete). One travel
agency company == one ``businesses`` row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0014_travel_agency_crm"
down_revision = "0013_compliance_engine"
branch_labels = None
depends_on = None


def _tenant_table(
    name: str,
    *columns: sa.Column,
    indexes: tuple[tuple[str, list[str]], ...] = (),
) -> None:
    op.create_table(
        name,
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", UUID(as_uuid=True), nullable=False),
        *columns,
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
    )
    op.create_index(f"ix_{name}_business", name, ["business_id"])
    for idx_name, cols in indexes:
        op.create_index(idx_name, name, list(cols))


def upgrade() -> None:
    _tenant_table(
        "travel_customers",
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(64), nullable=True),
        sa.Column("type", sa.String(32), nullable=False, server_default="individual"),
        sa.Column("notes", sa.Text, nullable=True),
        indexes=(
            ("ix_travel_customers_biz_name", ["business_id", "name"]),
            ("ix_travel_customers_biz_email", ["business_id", "email"]),
        ),
    )

    op.create_table(
        "traveler_profiles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=True),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("dob", sa.Date, nullable=True),
        sa.Column("preferences", JSONB, nullable=False, server_default="{}"),
        sa.Column("loyalty", JSONB, nullable=False, server_default="{}"),
        sa.Column("accessibility_notes", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["travel_customers.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_traveler_profiles_business", "traveler_profiles", ["business_id"])
    op.create_index(
        "ix_traveler_profiles_biz_name", "traveler_profiles", ["business_id", "full_name"]
    )

    op.create_table(
        "travel_leads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("destination", sa.String(255), nullable=True),
        sa.Column("date_start", sa.Date, nullable=True),
        sa.Column("date_end", sa.Date, nullable=True),
        sa.Column("budget", sa.Numeric(12, 2), nullable=True),
        sa.Column("trip_purpose", sa.String(255), nullable=True),
        sa.Column("assigned_to", UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="new"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["travel_customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assigned_to"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_travel_leads_business", "travel_leads", ["business_id"])
    op.create_index("ix_travel_leads_biz_status", "travel_leads", ["business_id", "status"])

    op.create_table(
        "trip_requests",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("business_id", UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", UUID(as_uuid=True), nullable=True),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=True),
        sa.Column("party_size", sa.Integer, nullable=False, server_default="1"),
        sa.Column("origin", sa.String(255), nullable=True),
        sa.Column("destinations", JSONB, nullable=False, server_default="[]"),
        sa.Column("date_start", sa.Date, nullable=True),
        sa.Column("date_end", sa.Date, nullable=True),
        sa.Column("preferences", JSONB, nullable=False, server_default="{}"),
        sa.Column("flexibility", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="open"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["travel_leads.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["customer_id"], ["travel_customers.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_trip_requests_business", "trip_requests", ["business_id"])
    op.create_index("ix_trip_requests_biz_status", "trip_requests", ["business_id", "status"])


def downgrade() -> None:
    for table in ("trip_requests", "travel_leads", "traveler_profiles", "travel_customers"):
        op.drop_table(table)
