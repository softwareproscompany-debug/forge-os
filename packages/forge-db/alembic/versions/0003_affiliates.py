"""ForgeOS affiliate marketing schema — programs, links, disclosure flag.

Revision ID: 0003_affiliates
Revises: 0002_cards
Create Date: 2026-10-09

Adds the "we promote affiliate offers" module:

* ``affiliate_programs`` — third-party programs (amazon, shareasale, cj,
  impact, direct, other) with a default commission rate.
* ``affiliate_links`` — trackable links; ``slug`` unique per business,
  served by the public ``GET /r/{slug}`` redirect. Clicks/conversions ride
  the existing ``events`` table (``affiliate_clicked`` /
  ``affiliate_converted`` kinds, link ids in the payload).
* ``assets.is_affiliate_content`` — boolean flag for the FTC disclosure
  guardrail (also auto-detected from /r/ links or program URLs).
* ``weekly_summaries.affiliate_top_links`` / ``.affiliate_earnings_usd`` —
  weekly evidence loop columns for the affiliate stats.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_affiliates"
down_revision = "0002_cards"
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


def _check(table: str, column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({quoted})", name=f"ck_{table}_{column}")


PROGRAM_STATUSES = ("active", "paused")


def upgrade() -> None:
    # -- affiliate_programs --------------------------------------------------
    op.create_table(
        "affiliate_programs",
        _uuid_pk(),
        _business_fk(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "network", sa.String(64), nullable=False, server_default="'other'"
        ),
        sa.Column("website_url", sa.String(1024), nullable=True),
        sa.Column(
            "default_commission_pct",
            sa.Numeric(6, 3),
            nullable=False,
            server_default="0",
        ),
        sa.Column("cookie_days", sa.Integer(), nullable=True),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default="'active'"
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        _check("affiliate_programs", "status", PROGRAM_STATUSES),
        _created_at(),
    )
    op.create_index(
        "ix_affiliate_programs_business_id", "affiliate_programs", ["business_id"]
    )

    # -- affiliate_links ------------------------------------------------------
    op.create_table(
        "affiliate_links",
        _uuid_pk(),
        _business_fk(),
        sa.Column(
            "program_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("affiliate_programs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column("destination_url", sa.String(2048), nullable=False),
        sa.Column("utm_source", sa.String(128), nullable=True),
        sa.Column("utm_medium", sa.String(128), nullable=True),
        sa.Column("utm_campaign", sa.String(128), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        _created_at(),
    )
    op.create_index(
        "ix_affiliate_links_business_id", "affiliate_links", ["business_id"]
    )
    op.create_index("ix_affiliate_links_slug", "affiliate_links", ["slug"])
    op.create_unique_constraint(
        "uq_affiliate_links_biz_slug",
        "affiliate_links",
        ["business_id", "slug"],
    )

    # -- assets.is_affiliate_content ------------------------------------------
    op.add_column(
        "assets",
        sa.Column(
            "is_affiliate_content",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # -- weekly_summaries affiliate evidence ----------------------------------
    op.add_column(
        "weekly_summaries",
        sa.Column(
            "affiliate_top_links",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "weekly_summaries",
        sa.Column(
            "affiliate_earnings_usd",
            sa.Numeric(12, 4),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("weekly_summaries", "affiliate_earnings_usd")
    op.drop_column("weekly_summaries", "affiliate_top_links")
    op.drop_column("assets", "is_affiliate_content")
    op.drop_table("affiliate_links")
    op.drop_table("affiliate_programs")
