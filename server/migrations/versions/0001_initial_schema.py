"""Initial schema: spec section 9 plus owner-approved additions (ADR-020).

Revision ID: 0001
Revises:
Create Date: 2026-10-07

Additions over spec section 9, approved 2026-10-07 (docs/architecture-decisions.md, ADR-020):
- CHECK constraints: snapshot kind/bucket enums, non-negative counts and durations,
  probabilities and OCR confidence in [0, 1].
- Foreign keys: fingerprint_snapshots.session_id and predictions.made_before_event (CASCADE);
  topic_transitions.from_topic/to_topic and predictions.actual_topic (to topics).
- predictions.pipeline_version; created_at on predictions and jobs.

visual_embedding is vector(768): provisional until the M0 visual-model decision.
Never edit this file once applied anywhere; add a new migration instead.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")


def _non_negative(table: str, column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} >= 0", name=f"ck_{table}_{column}_non_negative")


def _unit_interval(table: str, column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} >= 0 AND {column} <= 1", name=f"ck_{table}_{column}_unit_interval"
    )


def _user_fk() -> sa.Column[Any]:
    return sa.Column(
        "user_id",
        pg.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.Text, nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )

    op.create_table(
        "extension_tokens",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        _user_fk(),
        sa.Column("token_sha256", sa.LargeBinary, nullable=False, unique=True),
        sa.Column("label", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "consents",
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("policy_version", sa.Text, nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "sessions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        _user_fk(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("stop_reason", sa.Text),
    )

    op.create_table(
        "reel_exposures",
        sa.Column("event_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _user_fk(),
        sa.Column("reel_shortcode", sa.Text, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dwell_ms", sa.Integer),
        sa.Column("video_duration_ms", sa.Integer),
        sa.Column("creator_handle", sa.Text),
        sa.Column("caption", sa.Text),
        sa.Column("hashtags", pg.ARRAY(sa.Text)),
        sa.Column("field_status", pg.JSONB, nullable=False),
        sa.Column("adapter_version", sa.Text, nullable=False),
        sa.Column("process_status", sa.Text, nullable=False, server_default="pending"),
        _non_negative("reel_exposures", "dwell_ms"),
        _non_negative("reel_exposures", "video_duration_ms"),
    )
    op.create_index(
        "ix_reel_exposures_user_id_observed_at", "reel_exposures", ["user_id", "observed_at"]
    )
    op.create_index("ix_reel_exposures_reel_shortcode", "reel_exposures", ["reel_shortcode"])

    op.create_table(
        "content_features",
        sa.Column("reel_shortcode", sa.Text, primary_key=True),
        sa.Column("pipeline_version", sa.Text, primary_key=True),
        sa.Column("ocr_text", sa.Text),
        sa.Column("ocr_confidence", sa.Float),
        sa.Column("text_embedding", Vector(384)),
        sa.Column("visual_embedding", Vector(768)),
        sa.Column("modalities", pg.ARRAY(sa.Text), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        _unit_interval("content_features", "ocr_confidence"),
    )

    op.create_table(
        "topics",
        sa.Column("id", sa.SmallInteger, primary_key=True, autoincrement=False),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("taxonomy_version", sa.Text, nullable=False),
    )

    op.create_table(
        "topic_assignments",
        sa.Column("reel_shortcode", sa.Text, primary_key=True),
        sa.Column("pipeline_version", sa.Text, primary_key=True),
        sa.Column("topic_id", sa.SmallInteger, sa.ForeignKey("topics.id"), primary_key=True),
        sa.Column("p_text", sa.Float),
        sa.Column("p_visual", sa.Float),
        sa.Column("p_fused", sa.Float, nullable=False),
        _unit_interval("topic_assignments", "p_text"),
        _unit_interval("topic_assignments", "p_visual"),
        _unit_interval("topic_assignments", "p_fused"),
    )

    op.create_table(
        "fingerprint_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        _user_fk(),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("bucket", sa.Text, nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "session_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
        ),
        sa.Column("weights", pg.JSONB, nullable=False),
        sa.Column("n_reels", sa.Integer, nullable=False),
        sa.Column("pipeline_version", sa.Text, nullable=False),
        sa.CheckConstraint(
            "kind IN ('exposure', 'engagement')", name="ck_fingerprint_snapshots_kind"
        ),
        sa.CheckConstraint("bucket IN ('session', 'day')", name="ck_fingerprint_snapshots_bucket"),
        _non_negative("fingerprint_snapshots", "n_reels"),
    )
    op.create_index(
        "ix_fingerprint_snapshots_user_id_kind_taken_at",
        "fingerprint_snapshots",
        ["user_id", "kind", "taken_at"],
    )

    op.create_table(
        "topic_transitions",
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("from_topic", sa.SmallInteger, sa.ForeignKey("topics.id"), primary_key=True),
        sa.Column("to_topic", sa.SmallInteger, sa.ForeignKey("topics.id"), primary_key=True),
        sa.Column("n", sa.Integer, nullable=False, server_default=sa.text("0")),
        _non_negative("topic_transitions", "n"),
    )

    op.create_table(
        "predictions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        _user_fk(),
        sa.Column(
            "made_before_event",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("reel_exposures.event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("distribution", pg.JSONB, nullable=False),
        sa.Column("actual_topic", sa.SmallInteger, sa.ForeignKey("topics.id")),
        sa.Column("hit_top1", sa.Boolean),
        sa.Column("hit_top3", sa.Boolean),
        sa.Column("pipeline_version", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("payload", pg.JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        _non_negative("jobs", "attempts"),
    )
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"])


def downgrade() -> None:
    for table in (
        "jobs",
        "predictions",
        "topic_transitions",
        "fingerprint_snapshots",
        "topic_assignments",
        "topics",
        "content_features",
        "reel_exposures",
        "sessions",
        "consents",
        "extension_tokens",
        "users",
    ):
        op.drop_table(table)
    # The vector extension is left installed: other database objects may depend on it.
