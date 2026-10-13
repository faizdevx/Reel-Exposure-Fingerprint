"""Prediction state: the latest next-topic prediction and evaluation of a user (M6).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09

`predictions` keeps one row per Reel and model, written before the Reel is applied to the model
state; its `made_before_event` must name an existing event, so the prediction for the Reel that has
not happened yet cannot live there. `prediction_state` holds that one: per user and prediction
version, the distributions for the next Reel, the state they came from, and the evaluation summary
computed when the predictions were rebuilt, so reading them costs one row.

Also an index on `predictions (user_id, pipeline_version)`, for rebuilding and deleting a user's
predictions without scanning the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prediction_state",
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("pipeline_version", sa.Text, primary_key=True),
        sa.Column("state", pg.JSONB, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_predictions_user_id_pipeline_version", "predictions", ["user_id", "pipeline_version"]
    )


def downgrade() -> None:
    op.drop_index("ix_predictions_user_id_pipeline_version", table_name="predictions")
    op.drop_table("prediction_state")
