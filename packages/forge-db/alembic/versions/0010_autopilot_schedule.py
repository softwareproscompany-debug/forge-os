"""Per-tenant autopilot planner scheduling.

Revision ID: 0010_autopilot_schedule
Revises: 0009_draven_swarm
Create Date: 2026-10-09

Additive columns on ``autopilot_settings`` only, backward-compatible:

* ``plan_day`` — local weekday (0=Monday..6=Sunday) the weekly content
  plan is drafted. Default 0 (Monday).
* ``plan_hour`` — local hour (0-23) the plan is drafted. Default 6.
* ``plan_cadence`` — ``weekly`` | ``biweekly``. Default ``weekly``.
* ``last_planned_at`` — timestamptz of the most recent draft (cron or
  manual run-now); the biweekly gate skips drafting when the previous
  draft is less than ~13 days old. NULL until the first draft.

Existing rows pick up the defaults via server_default, so the hourly
cron behaves exactly as before (Monday 06:00, weekly) until a business
customizes its schedule.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_autopilot_schedule"
down_revision = "0009_draven_swarm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "autopilot_settings",
        sa.Column(
            "plan_day",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "autopilot_settings",
        sa.Column(
            "plan_hour",
            sa.Integer(),
            nullable=False,
            server_default="6",
        ),
    )
    op.add_column(
        "autopilot_settings",
        sa.Column(
            "plan_cadence",
            sa.String(16),
            nullable=False,
            server_default="'weekly'",
        ),
    )
    op.add_column(
        "autopilot_settings",
        sa.Column(
            "last_planned_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("autopilot_settings", "last_planned_at")
    op.drop_column("autopilot_settings", "plan_cadence")
    op.drop_column("autopilot_settings", "plan_hour")
    op.drop_column("autopilot_settings", "plan_day")
