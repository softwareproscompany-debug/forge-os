"""Alpha reliability slice — lead-to-follow-up workflow tables.

Revision ID: 0007_alpha_workflows
Revises: 0006_market_intel
Create Date: 2026-10-09

New tables only, backward-compatible:

* ``leads`` — normalized lead records; (business_id, source_event_id) unique
  for webhook idempotency (NULLs distinct on Postgres and SQLite).
* ``lead_duplicates`` — fuzzy duplicate candidates pending human review.
* ``qualification_rules`` — versioned, immutable-once-used rules per business.
* ``qualification_results`` — deterministic verdicts with per-criterion evidence.
* ``workflow_runs`` — durable state-machine executions (idempotency_key unique).
* ``workflow_steps`` — per-step ledger rows for the execution timeline.
* ``workflow_transitions`` — append-only audit of every state transition.
* ``alpha_approvals`` — approvals bound to SHA256 payload digests.
* ``outbound_messages`` — follow-up lifecycle (submitted ≠ delivered).
* ``action_evidence`` — verified connector evidence per consequential action.
* ``lead_sources`` — webhook intake sources with encrypted HMAC secrets.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_alpha_workflows"
down_revision = "0006_market_intel"
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


def _enum(name: str, values: list[str], length: int = 32) -> sa.Enum:
    return sa.Enum(
        *values, name=name, native_enum=False, length=length, validate_strings=True
    )


def upgrade() -> None:
    op.create_table(
        "leads",
        _uuid_pk(),
        _business_fk(),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_event_id", sa.String(256), nullable=True),
        sa.Column("name", sa.String(256), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("phone", sa.String(64), nullable=True),
        sa.Column("company", sa.String(256), nullable=True),
        sa.Column(
            "raw_payload", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "provenance", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "status",
            _enum(
                "leadstatus",
                ["new", "validated", "qualified", "unqualified", "needs_review",
                 "duplicate", "archived"],
            ),
            nullable=False,
            server_default="new",
        ),
        sa.Column(
            "duplicate_of_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "business_id", "source_event_id", name="uq_leads_biz_source_event"
        ),
    )
    op.create_index("ix_leads_email", "leads", ["email"])
    op.create_index("ix_leads_phone", "leads", ["phone"])

    op.create_table(
        "lead_duplicates",
        _uuid_pk(),
        _business_fk(),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("match_reason", sa.Text(), nullable=False),
        sa.Column(
            "status",
            _enum("duplicatestatus", ["pending", "merged", "dismissed"]),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "resolved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _created_at(),
    )
    op.create_index("ix_lead_duplicates_lead_id", "lead_duplicates", ["lead_id"])

    op.create_table(
        "qualification_rules",
        _uuid_pk(),
        _business_fk(),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False, server_default="default"),
        sa.Column(
            "rules", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column("threshold", sa.Float(), nullable=False, server_default="0.6"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        _created_at(),
        sa.UniqueConstraint(
            "business_id", "version", name="uq_qual_rules_biz_ver"
        ),
    )

    op.create_table(
        "qualification_results",
        _uuid_pk(),
        _business_fk(),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rules_version", sa.Integer(), nullable=False),
        sa.Column(
            "verdict",
            _enum(
                "qualificationverdict",
                ["qualified", "unqualified", "needs_review"],
            ),
            nullable=False,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "criteria", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        _created_at(),
    )
    op.create_index(
        "ix_qualification_results_lead_id", "qualification_results", ["lead_id"]
    )

    op.create_table(
        "workflow_runs",
        _uuid_pk(),
        _business_fk(),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "state",
            _enum(
                "runstate",
                ["received", "validating", "normalizing", "qualifying", "drafting",
                 "awaiting_approval", "rechecking", "submitting", "confirming",
                 "reconciling", "needs_review", "decided", "rejected", "completed",
                 "failed", "cancelled"],
            ),
            nullable=False,
            server_default="received",
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("current_step", sa.String(128), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("cost_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _created_at(),
        _updated_at(),
    )
    op.create_index("ix_workflow_runs_lead_id", "workflow_runs", ["lead_id"])
    op.create_index(
        "ix_workflow_runs_idempotency_key", "workflow_runs", ["idempotency_key"]
    )

    op.create_table(
        "workflow_steps",
        _uuid_pk(),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "state",
            _enum(
                "stepstate", ["pending", "running", "ok", "failed", "skipped"]
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "evidence", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
    )
    op.create_index("ix_workflow_steps_run_id", "workflow_steps", ["run_id"])

    op.create_table(
        "workflow_transitions",
        _uuid_pk(),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("from_state", sa.String(32), nullable=False),
        sa.Column("to_state", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _created_at(),
    )
    op.create_index(
        "ix_workflow_transitions_run_id", "workflow_transitions", ["run_id"]
    )

    op.create_table(
        "alpha_approvals",
        _uuid_pk(),
        _business_fk(),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "status",
            _enum(
                "alphaapprovalstatus",
                ["pending", "approved", "rejected", "expired", "invalidated"],
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "requested_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "approver_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
    )
    op.create_index("ix_alpha_approvals_run_id", "alpha_approvals", ["run_id"])

    op.create_table(
        "outbound_messages",
        _uuid_pk(),
        _business_fk(),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "approval_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("alpha_approvals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("recipient", sa.String(320), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False, server_default="stub"),
        sa.Column("provider_message_id", sa.String(256), nullable=True),
        sa.Column(
            "status",
            _enum(
                "outboundstatus",
                ["draft", "approved", "sending", "submitted", "confirmed",
                 "failed", "unknown"],
            ),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("send_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        _created_at(),
        _updated_at(),
    )
    op.create_index("ix_outbound_messages_run_id", "outbound_messages", ["run_id"])
    op.create_index(
        "ix_outbound_messages_idem", "outbound_messages", ["idempotency_key"]
    )

    op.create_table(
        "action_evidence",
        _uuid_pk(),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step", sa.String(128), nullable=False),
        sa.Column("connector", sa.String(64), nullable=False),
        sa.Column(
            "result", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
    )
    op.create_index("ix_action_evidence_run_id", "action_evidence", ["run_id"])

    op.create_table(
        "lead_sources",
        _uuid_pk(),
        _business_fk(),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("secret_enc", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        _created_at(),
        sa.UniqueConstraint(
            "business_id", "source", name="uq_lead_source_biz_src"
        ),
    )


def downgrade() -> None:
    for table in (
        "lead_sources",
        "action_evidence",
        "outbound_messages",
        "alpha_approvals",
        "workflow_transitions",
        "workflow_steps",
        "workflow_runs",
        "qualification_results",
        "qualification_rules",
        "lead_duplicates",
        "leads",
    ):
        op.drop_table(table)
    for enum_name in (
        "leadstatus",
        "duplicatestatus",
        "qualificationverdict",
        "runstate",
        "stepstate",
        "alphaapprovalstatus",
        "outboundstatus",
    ):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
