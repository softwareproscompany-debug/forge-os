"""Immutable audit log.

Revision ID: 0017_audit_log
Revises: 0016_affiliate_automation
Create Date: 2026-10-10

Adds ``audit_log``: an append-only security audit trail. True
immutability is enforced by a Postgres trigger
(``audit_log_no_update_delete``) that raises on any UPDATE or DELETE —
the trigger is created only on PostgreSQL and skipped on SQLite (dev /
tests). Application code must only INSERT and SELECT; there are no
UPDATE/DELETE paths anywhere.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_audit_log"
down_revision = "0016_affiliate_automation"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "actor_type",
            sa.String(16),
            nullable=False,
            server_default="user",
        ),
        sa.Column("actor_id", sa.String(64), nullable=True),
        sa.Column("actor_email", sa.String(255), nullable=True),
        sa.Column("business_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "actor_type IN ('user', 'system', 'api_key')",
            name="ck_audit_log_actor_type",
        ),
    )
    op.create_index(
        "ix_audit_log_biz_created", "audit_log", ["business_id", "created_at"]
    )
    op.create_index(
        "ix_audit_log_action_created", "audit_log", ["action", "created_at"]
    )

    # True immutability: block UPDATE and DELETE at the database level.
    # SQLite has no plpgsql — the trigger is Postgres-only; the
    # application layer simply never issues UPDATE/DELETE against this
    # table on any dialect.
    if _is_postgres():
        op.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION audit_log_no_update_delete()
                RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION 'audit_log is immutable: % not allowed', TG_OP;
                    RETURN NULL;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE TRIGGER audit_log_no_update_delete
                BEFORE UPDATE OR DELETE ON audit_log
                FOR EACH ROW EXECUTE FUNCTION audit_log_no_update_delete();
                """
            )
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log"
            )
        )
        op.execute(
            sa.text("DROP FUNCTION IF EXISTS audit_log_no_update_delete()")
        )
    op.drop_index("ix_audit_log_action_created", table_name="audit_log")
    op.drop_index("ix_audit_log_biz_created", table_name="audit_log")
    op.drop_table("audit_log")
