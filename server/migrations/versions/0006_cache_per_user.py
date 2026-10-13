"""Reel cache per user (security audit H-3, ADR-024).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09

`content_features` and `topic_assignments` were keyed by `(reel_shortcode, pipeline_version)`
(plus the topic) and shared by all users. The text behind a row is what the first user's client
sent, so one user could decide what another user's Reel is about. Both tables now start their
key with `user_id` (foreign key to `users`, `ON DELETE CASCADE`).

Existing rows are kept: each row is copied to every user who has an exposure of that Reel, so
stored snapshots still replay exactly (`replay-fingerprint` reads these assignments), and the
original shared rows are deleted. A row that no exposure refers to is dropped (it had no owner).
Downgrade keeps one row per old key (the lowest user id), which is the shared-cache behaviour and
so reintroduces the weakness.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("content_features", sa.Column("user_id", pg.UUID(as_uuid=True), nullable=True))
    op.add_column("topic_assignments", sa.Column("user_id", pg.UUID(as_uuid=True), nullable=True))
    # The old key goes first: the copies below repeat each (shortcode, version) once per viewer.
    for table in ("content_features", "topic_assignments"):
        op.drop_constraint(f"{table}_pkey", table, type_="primary")

    op.execute(
        """
        INSERT INTO content_features (user_id, reel_shortcode, pipeline_version, ocr_text,
            ocr_confidence, text_embedding, visual_embedding, modalities, created_at)
        SELECT e.user_id, c.reel_shortcode, c.pipeline_version, c.ocr_text, c.ocr_confidence,
            c.text_embedding, c.visual_embedding, c.modalities, c.created_at
        FROM content_features c
        JOIN (SELECT DISTINCT user_id, reel_shortcode FROM reel_exposures) e
          ON e.reel_shortcode = c.reel_shortcode
        WHERE c.user_id IS NULL
        """
    )
    op.execute("DELETE FROM content_features WHERE user_id IS NULL")
    op.execute(
        """
        INSERT INTO topic_assignments (user_id, reel_shortcode, pipeline_version, topic_id,
            p_text, p_visual, p_fused)
        SELECT e.user_id, a.reel_shortcode, a.pipeline_version, a.topic_id,
            a.p_text, a.p_visual, a.p_fused
        FROM topic_assignments a
        JOIN (SELECT DISTINCT user_id, reel_shortcode FROM reel_exposures) e
          ON e.reel_shortcode = a.reel_shortcode
        WHERE a.user_id IS NULL
        """
    )
    op.execute("DELETE FROM topic_assignments WHERE user_id IS NULL")

    for table, key in (
        ("content_features", ["user_id", "reel_shortcode", "pipeline_version"]),
        ("topic_assignments", ["user_id", "reel_shortcode", "pipeline_version", "topic_id"]),
    ):
        op.alter_column(table, "user_id", nullable=False)
        op.create_primary_key(f"{table}_pkey", table, key)
        op.create_foreign_key(
            f"fk_{table}_user_id", table, "users", ["user_id"], ["id"], ondelete="CASCADE"
        )


def downgrade() -> None:
    for table in ("content_features", "topic_assignments"):
        op.drop_constraint(f"fk_{table}_user_id", table, type_="foreignkey")
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
    # One row per old key, from the same user in both tables (the lowest user id).
    op.execute(
        """
        DELETE FROM content_features a USING content_features b
        WHERE a.reel_shortcode = b.reel_shortcode AND a.pipeline_version = b.pipeline_version
          AND a.user_id > b.user_id
        """
    )
    op.execute(
        """
        DELETE FROM topic_assignments a USING topic_assignments b
        WHERE a.reel_shortcode = b.reel_shortcode AND a.pipeline_version = b.pipeline_version
          AND a.topic_id = b.topic_id AND a.user_id > b.user_id
        """
    )
    for table, key in (
        ("content_features", ["reel_shortcode", "pipeline_version"]),
        ("topic_assignments", ["reel_shortcode", "pipeline_version", "topic_id"]),
    ):
        op.drop_column(table, "user_id")
        op.create_primary_key(f"{table}_pkey", table, key)
