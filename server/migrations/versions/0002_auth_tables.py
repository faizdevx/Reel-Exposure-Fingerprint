"""Auth tables: invites and user_sessions (ADR-004, accepted by the owner 2026-10-07).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07

Only hashes are stored: the invite code and the session id are shown or sent once and kept as
SHA-256 digests, like extension tokens. created_by from the ADR-004 sketch is omitted: invites are
created by the operator CLI, which has no user.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "invites",
        sa.Column("code_sha256", sa.LargeBinary, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        # CASCADE: deleting a user removes the invite they used (zero rows after delete-all).
        sa.Column("used_by", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.CheckConstraint("(used_at IS NULL) = (used_by IS NULL)", name="ck_invites_used_pair"),
    )

    op.create_table(
        "user_sessions",
        sa.Column("id_sha256", sa.LargeBinary, primary_key=True),
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("expires_at > created_at", name="ck_user_sessions_expiry_after_create"),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_table("user_sessions")
    op.drop_table("invites")
