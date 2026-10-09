"""Draven 12-agent swarm tables.

Revision ID: 0009_draven_swarm
Revises: 0008_settings_vault
Create Date: 2026-10-09

New tables + additive columns only, backward-compatible:

* ``draven_swarm_runs`` — one row per orchestrated multi-agent run:
  goal, context, status (queued/running/completed/failed), current_phase,
  per-agent results JSON, result summary, error, timestamps.
* ``draven_swarm_events`` — append-only live event feed per run
  (agent_start / agent_done / tool_call / note / error). ``(swarm_run_id,
  seq)`` is unique for total ordering; the frontend polls with ``?after=``.
* ``draven_tool_runs.agent_id`` + ``draven_tool_runs.swarm_run_id`` —
  attribute audit rows to the swarm agent that executed them (NULL for
  direct chat tool calls).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_draven_swarm"
down_revision = "0008_settings_vault"
branch_labels = None
depends_on = None

_SWARM_STATUS = sa.Enum(
    "queued", "running", "completed", "failed",
    name="draven_swarm_run_status",
)


def upgrade() -> None:
    _SWARM_STATUS.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "draven_swarm_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            # create_type=False: the type is created explicitly above (line 38).
            # Without this, create_table re-emits CREATE TYPE and the migration
            # fails with DuplicateObject on every Postgres database.
            postgresql.ENUM(
                "queued",
                "running",
                "completed",
                "failed",
                name="draven_swarm_run_status",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column("current_phase", sa.String(128), nullable=True),
        sa.Column(
            "agent_results",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "draven_swarm_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "swarm_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("draven_swarm_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "swarm_run_id", "seq", name="uq_swarm_events_run_seq"
        ),
    )
    op.create_index(
        "ix_swarm_events_run_seq", "draven_swarm_events",
        ["swarm_run_id", "seq"],
    )

    op.add_column(
        "draven_tool_runs",
        sa.Column("agent_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "draven_tool_runs",
        sa.Column(
            "swarm_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("draven_swarm_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("draven_tool_runs", "swarm_run_id")
    op.drop_column("draven_tool_runs", "agent_id")
    op.drop_index("ix_swarm_events_run_seq", table_name="draven_swarm_events")
    op.drop_table("draven_swarm_events")
    op.drop_table("draven_swarm_runs")
    _SWARM_STATUS.drop(op.get_bind(), checkfirst=True)
