"""Draven Market Intelligence tables.

Revision ID: 0006_market_intel
Revises: 0005_draven_tts
Create Date: 2026-10-09

New tables only, backward-compatible:

* ``market_research_jobs`` — one row per research run: params + inferred
  assumptions, status/progress, results summary, stage-by-stage audit trail.
  The pipeline is deterministic (collect → normalize → score → economics
  → report), recorded in ``audit_trail``.
* ``market_opportunities`` — researched product opportunities with full
  evidence provenance. Quantitative fields carry source/period/retrieved
  metadata in the JSON columns; anything without provenance is labeled
  ``estimated`` in reports, never presented as measured fact.
* ``market_snapshots`` — append-only point-in-time score/economics per
  opportunity for historical comparison.
* ``market_sources`` — connector capability registry + health per business.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_market_intel"
down_revision = "0005_draven_tts"
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


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
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


def _job_fk(nullable: bool = True) -> sa.Column:
    return sa.Column(
        "research_job_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("market_research_jobs.id", ondelete="SET NULL"),
        nullable=nullable,
    )


def _status_col() -> sa.Column:
    # Rendered as VARCHAR + CHECK (native_enum=False), matching models.
    return sa.Column(
        "status",
        sa.Enum(
            "queued",
            "running",
            "done",
            "failed",
            name="researchjobstatus",
            native_enum=False,
            length=16,
            validate_strings=True,
        ),
        nullable=False,
        server_default="queued",
    )


def _recommendation_col(nullable: bool = False, default: str = "UNDECIDED") -> sa.Column:
    return sa.Column(
        "recommendation",
        sa.Enum(
            "PRIORITIZE",
            "TEST_WITH_LIMITED_CAPITAL",
            "WATCH_FOR_BETTER_TIMING",
            "RESEARCH_FURTHER",
            "REJECT",
            "UNDECIDED",
            name="recommendationstatus",
            native_enum=False,
            length=32,
            validate_strings=True,
        ),
        nullable=nullable,
        server_default=default,
    )


def upgrade() -> None:
    op.create_table(
        "market_research_jobs",
        _uuid_pk(),
        _business_fk(),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("params", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "assumptions", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        _status_col(),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("results", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "audit_trail", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column("error", sa.Text(), nullable=True),
        _created_at(),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Index("ix_market_research_jobs_business", "business_id", "created_at"),
    )

    op.create_table(
        "market_opportunities",
        _uuid_pk(),
        _business_fk(),
        _job_fk(nullable=True),
        sa.Column("name", sa.String(480), nullable=False),
        sa.Column("category", sa.String(128), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "identifiers", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "source_urls", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "price_evidence", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "demand_indicators", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "seasonality_profile",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "competition_summary",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "unit_economics", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "sourcing_evidence",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "risk_flags", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "score_components",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("opportunity_score", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column(
            "evidence_gaps", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        _recommendation_col(),
        sa.Column("saved", sa.Boolean(), nullable=False, server_default="false"),
        _created_at(),
        _updated_at(),
        sa.Index("ix_market_opportunities_business", "business_id", "created_at"),
        sa.Index("ix_market_opportunities_score", "business_id", "opportunity_score"),
    )

    op.create_table(
        "market_snapshots",
        _uuid_pk(),
        _business_fk(),
        sa.Column(
            "opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_opportunities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _job_fk(nullable=True),
        sa.Column("opportunity_score", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column(
            "score_components",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "unit_economics", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        _recommendation_col(),
        _created_at(),
        sa.Index("ix_market_snapshots_opportunity", "opportunity_id", "created_at"),
    )

    op.create_table(
        "market_sources",
        _uuid_pk(),
        _business_fk(),
        sa.Column("connector", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column(
            "capability", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column("configured", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_ok", sa.Boolean(), nullable=True),
        sa.Column("last_check_note", sa.Text(), nullable=True),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "business_id", "connector", name="uq_market_source_biz_conn"
        ),
    )


def downgrade() -> None:
    op.drop_table("market_sources")
    op.drop_table("market_snapshots")
    op.drop_table("market_opportunities")
    op.drop_table("market_research_jobs")
    # NOTE: the VARCHAR CHECK enums are left in place on downgrade; they are
    # not native enums and are recreated idempotently on upgrade.
