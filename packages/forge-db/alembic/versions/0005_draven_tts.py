"""Draven TTS provider columns on draven_provider_config.

Revision ID: 0005_draven_tts
Revises: 0004_draven
Create Date: 2026-10-09

Backward-compatible, additive only:

* ``draven_provider_config.tts_provider`` — TTS voice provider name
  (``"elevenlabs"``) or NULL when no TTS provider is configured. Separate
  from the chat LLM ``provider`` column on the same row.
* ``draven_provider_config.tts_api_key_enc`` — Fernet-encrypted TTS API
  key. The application never stores or returns plaintext keys.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_draven_tts"
down_revision = "0004_draven"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "draven_provider_config",
        sa.Column("tts_provider", sa.String(64), nullable=True),
    )
    op.add_column(
        "draven_provider_config",
        sa.Column("tts_api_key_enc", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("draven_provider_config", "tts_api_key_enc")
    op.drop_column("draven_provider_config", "tts_provider")
