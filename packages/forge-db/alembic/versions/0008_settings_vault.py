"""Settings vault — centralized encrypted API-key storage.

Revision ID: 0008_settings_vault
Revises: 0007_alpha_workflows
Create Date: 2026-10-09

New table only, backward-compatible:

* ``business_secrets`` — one row per (business_id, key_name). ``value_enc``
  holds a Fernet token (AES-128-CBC + HMAC-SHA256, see
  ``docs/SECRETS_AND_ENCRYPTION.md``) — never plaintext, never returned to
  clients. Known key names: ``elevenlabs.api_key``, ``anthropic.api_key``,
  ``openai.api_key``, ``openai.base_url``, ``dataforseo.login``,
  ``dataforseo.password``, ``webhook.<source>.secret``.

Legacy columns (``draven_provider_config.api_key_enc`` /
``base_url_enc`` / ``tts_api_key_enc``, ``lead_sources.secret_enc``) are NOT
dropped here: the vault lazily migrates their encrypted tokens on first
read, and consumers fall back to the old columns during the transition.
Dropping them is a future, separately-reviewed migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_settings_vault"
down_revision = "0007_alpha_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_secrets",
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
        sa.Column("key_name", sa.String(128), nullable=False),
        # Fernet token (base64url) — never plaintext.
        sa.Column("value_enc", sa.Text(), nullable=False),
        sa.Column("label", sa.String(128), nullable=True),
        sa.Column(
            "last_verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "business_id", "key_name", name="uq_business_secrets_biz_key"
        ),
    )
    op.create_index(
        "ix_business_secrets_business", "business_secrets", ["business_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_business_secrets_business", table_name="business_secrets")
    op.drop_table("business_secrets")
