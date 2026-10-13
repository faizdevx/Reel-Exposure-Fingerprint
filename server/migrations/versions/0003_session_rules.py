"""Session and consent rules (ADR-014 vocabularies and one-active-session policy, owner-accepted).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07

- sessions.status is active | stopped; stop_reason is one of six values (ADR-014).
- An active session has no end and no reason; a stopped one has both; the end is not before the
  start.
- At most one active session per user, enforced by a partial unique index.
- A consent revocation cannot precede its grant.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STOP_REASONS = (
    "user",
    "left_surface",
    "tab_closed",
    "time_cap",
    "consent_revoked",
    "browser_restart",
)


def upgrade() -> None:
    reasons = ", ".join(f"'{r}'" for r in STOP_REASONS)
    op.create_check_constraint("ck_sessions_status", "sessions", "status IN ('active', 'stopped')")
    op.create_check_constraint(
        "ck_sessions_stop_reason",
        "sessions",
        f"stop_reason IS NULL OR stop_reason IN ({reasons})",
    )
    op.create_check_constraint(
        "ck_sessions_state_consistent",
        "sessions",
        "(status = 'active' AND ended_at IS NULL AND stop_reason IS NULL) OR "
        "(status = 'stopped' AND ended_at IS NOT NULL AND stop_reason IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_sessions_end_after_start", "sessions", "ended_at IS NULL OR ended_at >= started_at"
    )
    op.create_index(
        "uq_sessions_one_active_per_user",
        "sessions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_check_constraint(
        "ck_consents_revoke_after_grant",
        "consents",
        "revoked_at IS NULL OR revoked_at >= granted_at",
    )


def downgrade() -> None:
    op.drop_constraint("ck_consents_revoke_after_grant", "consents", type_="check")
    op.drop_index("uq_sessions_one_active_per_user", table_name="sessions")
    op.drop_constraint("ck_sessions_end_after_start", "sessions", type_="check")
    op.drop_constraint("ck_sessions_state_consistent", "sessions", type_="check")
    op.drop_constraint("ck_sessions_stop_reason", "sessions", type_="check")
    op.drop_constraint("ck_sessions_status", "sessions", type_="check")
