"""Draven backend foundation — tool-run audit log + provider config.

Revision ID: 0004_draven
Revises: 0003_affiliates
Create Date: 2026-10-09

Adds the Draven autonomous-assistant control plane tables (new tables only,
backward-compatible):

* ``draven_tool_runs`` — audit row for every tool execution the assistant
  proposes, runs, or asks approval for (tool, input, output summary, risk,
  status, duration).
* ``draven_provider_config`` — per-business LLM provider configuration for
  Draven chat composition. ``base_url_enc`` / ``api_key_enc`` hold
  Fernet-encrypted secrets; the application never stores plaintext keys.
  One row per business (``business_id`` is the primary key).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_draven"
down_revision = "0003_affiliates"
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


def upgrade() -> None:
    op.create_table(
        "draven_tool_runs",
        _uuid_pk(),
        _business_fk(),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tool", sa.String(128), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("output_summary", sa.Text(), nullable=True),
        sa.Column("risk", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        _created_at(),
        sa.Index("ix_draven_tool_runs_business_created", "business_id", "created_at"),
    )

    op.create_table(
        "draven_provider_config",
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("base_url_enc", sa.Text(), nullable=True),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("draven_provider_config")
    op.drop_table("draven_tool_runs")
