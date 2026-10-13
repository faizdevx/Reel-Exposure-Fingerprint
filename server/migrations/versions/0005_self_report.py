"""Self-report: the user's own interest rating of each topic (evaluation 2, spec §10).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-09

`self_report_ratings` holds one rating from 1 to 5 per user and topic. `survey_responses` says how
the survey ended: `submitted` (ratings given) or `skipped` (none given, and the user is left out of
the fingerprint-versus-self-report evaluation). Both belong to the user and go with the account.
The survey is answered once, before the user has seen any fingerprint, so that the ratings are not
shaped by what the system shows; the API refuses to change a completed survey.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "survey_responses",
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("status IN ('submitted', 'skipped')", name="ck_survey_responses_status"),
    )
    op.create_table(
        "self_report_ratings",
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("topic_id", sa.SmallInteger, sa.ForeignKey("topics.id"), primary_key=True),
        sa.Column("rating", sa.SmallInteger, nullable=False),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_self_report_ratings_rating_range"),
    )


def downgrade() -> None:
    op.drop_table("self_report_ratings")
    op.drop_table("survey_responses")
