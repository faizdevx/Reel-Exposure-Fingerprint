"""SQLAlchemy models for the spec §9 schema plus the owner-approved additions in ADR-020.

ADR-020 adds CHECK constraints, five foreign keys, predictions.pipeline_version and created_at on
predictions and jobs. Other gaps in §9 (ADR-007, 008, 011, 014, 016) are still Proposed, so their
schema additions do not appear here. Embedding widths: 384 is fixed by multilingual-e5-small. 768
is the output width of both SigLIP base variants named in the spec and stays provisional until the
M0 visual-model decision (spec §9 last note).

ADR-004 (accepted 2026-10-07) adds the invites and user_sessions tables (migration 0002).
ADR-014 vocabularies and the one-active-session rule add CHECKs and a partial unique index
(migration 0003).

Keep in sync with migrations/versions; tests/test_database.py compares the two.
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TEXT_EMBEDDING_DIM = 384
VISUAL_EMBEDDING_DIM = 768  # provisional until M0 picks the SigLIP variant


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True)


def _user_fk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _non_negative(table: str, column: str) -> CheckConstraint:
    return CheckConstraint(f"{column} >= 0", name=f"ck_{table}_{column}_non_negative")


def _unit_interval(table: str, column: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} >= 0 AND {column} <= 1", name=f"ck_{table}_{column}_unit_interval"
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class ExtensionToken(Base):
    __tablename__ = "extension_tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    token_sha256: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Consent(Base):
    __tablename__ = "consents"
    __table_args__ = (
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name="ck_consents_revoke_after_grant",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    policy_version: Mapped[str] = mapped_column(Text)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


SESSION_STATUSES = ("active", "stopped")
STOP_REASONS = (
    "user",
    "left_surface",
    "tab_closed",
    "time_cap",
    "consent_revoked",
    "browser_restart",
)


class Session(Base):
    """A monitoring session (F2). Not the dashboard login: see UserSession."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'stopped')", name="ck_sessions_status"),
        CheckConstraint(
            "stop_reason IS NULL OR stop_reason IN ("
            + ", ".join(f"'{r}'" for r in STOP_REASONS)
            + ")",
            name="ck_sessions_stop_reason",
        ),
        CheckConstraint(
            "(status = 'active' AND ended_at IS NULL AND stop_reason IS NULL) OR "
            "(status = 'stopped' AND ended_at IS NOT NULL AND stop_reason IS NOT NULL)",
            name="ck_sessions_state_consistent",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at", name="ck_sessions_end_after_start"
        ),
        Index(
            "uq_sessions_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    stop_reason: Mapped[str | None] = mapped_column(Text)


class ReelExposure(Base):
    __tablename__ = "reel_exposures"
    __table_args__ = (
        Index("ix_reel_exposures_user_id_observed_at", "user_id", "observed_at"),
        Index("ix_reel_exposures_reel_shortcode", "reel_shortcode"),
        _non_negative("reel_exposures", "dwell_ms"),
        _non_negative("reel_exposures", "video_duration_ms"),
    )

    event_id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = _user_fk()
    reel_shortcode: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dwell_ms: Mapped[int | None] = mapped_column(Integer)
    video_duration_ms: Mapped[int | None] = mapped_column(Integer)
    creator_handle: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    hashtags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    field_status: Mapped[dict[str, Any]] = mapped_column(JSONB)
    adapter_version: Mapped[str] = mapped_column(Text)
    process_status: Mapped[str] = mapped_column(Text, server_default="pending")


class ContentFeature(Base):
    """Keyed by user and Reel, not by event: the same Reel seen twice by one user is analysed once.

    Per user on purpose (security audit H-3): the text behind a row is whatever the client sent, so
    a row shared across users would let one user decide what another user's Reel is about.
    """

    __tablename__ = "content_features"
    __table_args__ = (_unit_interval("content_features", "ocr_confidence"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    reel_shortcode: Mapped[str] = mapped_column(Text, primary_key=True)
    pipeline_version: Mapped[str] = mapped_column(Text, primary_key=True)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    text_embedding: Mapped[list[float] | None] = mapped_column(Vector(TEXT_EMBEDDING_DIM))
    visual_embedding: Mapped[list[float] | None] = mapped_column(Vector(VISUAL_EMBEDDING_DIM))
    modalities: Mapped[list[str]] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = _created_at()


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    label: Mapped[str] = mapped_column(Text)
    taxonomy_version: Mapped[str] = mapped_column(Text)


class TopicAssignment(Base):
    __tablename__ = "topic_assignments"
    __table_args__ = (
        _unit_interval("topic_assignments", "p_text"),
        _unit_interval("topic_assignments", "p_visual"),
        _unit_interval("topic_assignments", "p_fused"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    reel_shortcode: Mapped[str] = mapped_column(Text, primary_key=True)
    pipeline_version: Mapped[str] = mapped_column(Text, primary_key=True)
    topic_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("topics.id"), primary_key=True)
    p_text: Mapped[float | None] = mapped_column(Float)
    p_visual: Mapped[float | None] = mapped_column(Float)
    p_fused: Mapped[float] = mapped_column(Float)


class FingerprintSnapshot(Base):
    __tablename__ = "fingerprint_snapshots"
    __table_args__ = (
        Index("ix_fingerprint_snapshots_user_id_kind_taken_at", "user_id", "kind", "taken_at"),
        CheckConstraint("kind IN ('exposure', 'engagement')", name="ck_fingerprint_snapshots_kind"),
        CheckConstraint("bucket IN ('session', 'day')", name="ck_fingerprint_snapshots_bucket"),
        _non_negative("fingerprint_snapshots", "n_reels"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = _user_fk()
    kind: Mapped[str] = mapped_column(Text)  # exposure | engagement
    bucket: Mapped[str] = mapped_column(Text)  # session | day
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE")
    )
    weights: Mapped[dict[str, Any]] = mapped_column(JSONB)
    n_reels: Mapped[int] = mapped_column(Integer)
    pipeline_version: Mapped[str] = mapped_column(Text)


class TopicTransition(Base):
    __tablename__ = "topic_transitions"
    __table_args__ = (_non_negative("topic_transitions", "n"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    from_topic: Mapped[int] = mapped_column(SmallInteger, ForeignKey("topics.id"), primary_key=True)
    to_topic: Mapped[int] = mapped_column(SmallInteger, ForeignKey("topics.id"), primary_key=True)
    n: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        Index("ix_predictions_user_id_pipeline_version", "user_id", "pipeline_version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = _user_fk()
    made_before_event: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reel_exposures.event_id", ondelete="CASCADE")
    )
    model: Mapped[str] = mapped_column(Text)
    distribution: Mapped[dict[str, Any]] = mapped_column(JSONB)
    actual_topic: Mapped[int | None] = mapped_column(SmallInteger, ForeignKey("topics.id"))
    hit_top1: Mapped[bool | None] = mapped_column(Boolean)
    hit_top3: Mapped[bool | None] = mapped_column(Boolean)
    pipeline_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class PredictionState(Base):
    """The next-topic prediction of a user (for the Reel that has not happened yet) and the
    evaluation summary of all past predictions, rebuilt together (`app.prediction.store`)."""

    __tablename__ = "prediction_state"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    pipeline_version: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SurveyResponse(Base):
    """How the self-report survey ended: `submitted` or `skipped` (spec §10, evaluation 2)."""

    __tablename__ = "survey_responses"
    __table_args__ = (
        CheckConstraint("status IN ('submitted', 'skipped')", name="ck_survey_responses_status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(Text)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SelfReportRating(Base):
    """The user's own interest in one topic, 1 to 5."""

    __tablename__ = "self_report_ratings"
    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_self_report_ratings_rating_range"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("topics.id"), primary_key=True)
    rating: Mapped[int] = mapped_column(SmallInteger)


class Invite(Base):
    """Single-use invite code, stored as SHA-256 (ADR-004). Created by the operator CLI."""

    __tablename__ = "invites"
    __table_args__ = (
        CheckConstraint("(used_at IS NULL) = (used_by IS NULL)", name="ck_invites_used_pair"),
    )

    code_sha256: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )


class UserSession(Base):
    """Dashboard login session. The cookie holds the random id; only its SHA-256 is stored."""

    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("ix_user_sessions_user_id", "user_id"),
        CheckConstraint("expires_at > created_at", name="ck_user_sessions_expiry_after_create"),
    )

    id_sha256: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[uuid.UUID] = _user_fk()
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status_run_after", "status", "run_after"),
        _non_negative("jobs", "attempts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
