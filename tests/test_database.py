"""Schema tests: spec §9, ADR-020 additions and the ADR-004 auth tables.

Need PostgreSQL + pgvector via TEST_DATABASE_URL; skipped otherwise.
Autogenerate comparison does not cover CHECK constraints, so those are tested explicitly.
"""

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from support import alembic_config

from app.db import check_connection
from app.models import Base

pytestmark = pytest.mark.db

SPEC_TABLES = {
    "users",
    "extension_tokens",
    "consents",
    "sessions",
    "reel_exposures",
    "content_features",
    "topics",
    "topic_assignments",
    "fingerprint_snapshots",
    "topic_transitions",
    "predictions",
    "prediction_state",
    "survey_responses",
    "self_report_ratings",
    "jobs",
    # ADR-004 (accepted 2026-10-07)
    "invites",
    "user_sessions",
}

USER_SCOPED_TABLES = (
    "user_sessions",
    "extension_tokens",
    "consents",
    "sessions",
    "reel_exposures",
    "fingerprint_snapshots",
    "topic_transitions",
    "predictions",
    "prediction_state",
    "survey_responses",
    "self_report_ratings",
)

# (table, column) -> (referenced table, ON DELETE rule)
EXPECTED_FKS = {
    ("extension_tokens", "user_id"): ("users", "CASCADE"),
    ("consents", "user_id"): ("users", "CASCADE"),
    ("sessions", "user_id"): ("users", "CASCADE"),
    ("reel_exposures", "session_id"): ("sessions", "CASCADE"),
    ("reel_exposures", "user_id"): ("users", "CASCADE"),
    ("topic_assignments", "topic_id"): ("topics", "NO ACTION"),
    ("content_features", "user_id"): ("users", "CASCADE"),
    ("topic_assignments", "user_id"): ("users", "CASCADE"),
    ("fingerprint_snapshots", "user_id"): ("users", "CASCADE"),
    ("fingerprint_snapshots", "session_id"): ("sessions", "CASCADE"),
    ("topic_transitions", "user_id"): ("users", "CASCADE"),
    ("topic_transitions", "from_topic"): ("topics", "NO ACTION"),
    ("topic_transitions", "to_topic"): ("topics", "NO ACTION"),
    ("predictions", "user_id"): ("users", "CASCADE"),
    ("prediction_state", "user_id"): ("users", "CASCADE"),
    ("survey_responses", "user_id"): ("users", "CASCADE"),
    ("self_report_ratings", "user_id"): ("users", "CASCADE"),
    ("self_report_ratings", "topic_id"): ("topics", "NO ACTION"),
    ("predictions", "made_before_event"): ("reel_exposures", "CASCADE"),
    ("predictions", "actual_topic"): ("topics", "NO ACTION"),
    ("invites", "used_by"): ("users", "CASCADE"),
    ("user_sessions", "user_id"): ("users", "CASCADE"),
}

EXPECTED_CHECKS = {
    "ck_reel_exposures_dwell_ms_non_negative",
    "ck_reel_exposures_video_duration_ms_non_negative",
    "ck_content_features_ocr_confidence_unit_interval",
    "ck_topic_assignments_p_text_unit_interval",
    "ck_topic_assignments_p_visual_unit_interval",
    "ck_topic_assignments_p_fused_unit_interval",
    "ck_fingerprint_snapshots_kind",
    "ck_fingerprint_snapshots_bucket",
    "ck_fingerprint_snapshots_n_reels_non_negative",
    "ck_topic_transitions_n_non_negative",
    "ck_jobs_attempts_non_negative",
    "ck_invites_used_pair",
    "ck_user_sessions_expiry_after_create",
    # ADR-014 vocabularies and session rules (migration 0003)
    "ck_sessions_status",
    "ck_sessions_stop_reason",
    "ck_sessions_state_consistent",
    "ck_sessions_end_after_start",
    "ck_consents_revoke_after_grant",
    "ck_survey_responses_status",
    "ck_self_report_ratings_rating_range",
}

USER = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER_USER = uuid.UUID("00000000-0000-0000-0000-000000000002")
SESSION = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_SESSION = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
EVENT = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
OTHER_EVENT = uuid.UUID("00000000-0000-0000-0000-0000000000e2")


# ---------------------------------------------------------------- helpers


def add_user(conn: Connection, user_id: uuid.UUID = USER) -> None:
    conn.execute(
        text("INSERT INTO users (id, email, password_hash) VALUES (:id, :email, 'x')"),
        {"id": user_id, "email": f"{user_id}@example.test"},
    )


def add_session(
    conn: Connection,
    session_id: uuid.UUID = SESSION,
    user_id: uuid.UUID = USER,
    *,
    active: bool = True,
) -> None:
    """At most one active session per user, so extra sessions must be stopped ones."""
    if active:
        sql = (
            "INSERT INTO sessions (id, user_id, started_at, status) "
            "VALUES (:id, :user_id, now(), 'active')"
        )
    else:
        sql = (
            "INSERT INTO sessions (id, user_id, started_at, ended_at, status, stop_reason) "
            "VALUES (:id, :user_id, now() - interval '1 hour', now(), 'stopped', 'user')"
        )
    conn.execute(text(sql), {"id": session_id, "user_id": user_id})


def add_exposure(
    conn: Connection,
    event_id: uuid.UUID = EVENT,
    session_id: uuid.UUID = SESSION,
    user_id: uuid.UUID = USER,
    dwell_ms: int | None = 1000,
) -> None:
    conn.execute(
        text(
            "INSERT INTO reel_exposures (event_id, session_id, user_id, reel_shortcode, "
            "observed_at, dwell_ms, field_status, adapter_version) VALUES "
            "(:event_id, :session_id, :user_id, 'Cabc123', now(), :dwell_ms, '{}', 'test')"
        ),
        {"event_id": event_id, "session_id": session_id, "user_id": user_id, "dwell_ms": dwell_ms},
    )


def add_topic(conn: Connection, topic_id: int = 1) -> None:
    conn.execute(
        text(
            "INSERT INTO topics (id, slug, label, taxonomy_version) "
            "VALUES (:id, :slug, :slug, 'test-v1')"
        ),
        {"id": topic_id, "slug": f"topic-{topic_id}"},
    )


def add_prediction(
    conn: Connection, event_id: uuid.UUID = EVENT, user_id: uuid.UUID = USER
) -> None:
    conn.execute(
        text(
            "INSERT INTO predictions (user_id, made_before_event, model, distribution, "
            "pipeline_version) VALUES (:user_id, :event_id, 'uniform', '{}', 'pv-test')"
        ),
        {"user_id": user_id, "event_id": event_id},
    )


def add_snapshot(
    conn: Connection,
    kind: str = "exposure",
    bucket: str = "session",
    session_id: uuid.UUID | None = SESSION,
    user_id: uuid.UUID = USER,
    n_reels: int = 1,
) -> None:
    conn.execute(
        text(
            "INSERT INTO fingerprint_snapshots (user_id, kind, bucket, taken_at, session_id, "
            "weights, n_reels, pipeline_version) VALUES "
            "(:user_id, :kind, :bucket, now(), :session_id, '{}', :n_reels, 'pv-test')"
        ),
        {
            "user_id": user_id,
            "kind": kind,
            "bucket": bucket,
            "session_id": session_id,
            "n_reels": n_reels,
        },
    )


def count(conn: Connection, table: str) -> int:
    # Table names come from the constants above, never from input.
    return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def rejected(engine: Engine, sql: str, params: dict[str, object] | None = None) -> bool:
    """True if the statement violates a constraint. Runs in its own rolled-back transaction."""
    with engine.connect() as conn, conn.begin():
        try:
            conn.execute(text(sql), params or {})
        except IntegrityError:
            return True
    return False


@pytest.fixture
def seeded(migrated_engine: Engine) -> Iterator[Engine]:
    """One user with one session, one exposure and one topic."""
    with migrated_engine.begin() as conn:
        add_user(conn)
        add_session(conn)
        add_exposure(conn)
        add_topic(conn)
    yield migrated_engine


# ---------------------------------------------------------------- structure


def test_check_connection_true_for_live_db(migrated_engine: Engine) -> None:
    assert check_connection(migrated_engine) is True


def test_check_connection_false_for_dead_db() -> None:
    dead = create_engine(
        "postgresql+psycopg://x:y@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    assert check_connection(dead) is False


def test_migration_creates_exactly_the_spec_tables(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names()) - {"alembic_version"}
    assert tables == SPEC_TABLES


def test_pgvector_extension_installed(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        found = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar()
    assert found == 1


def test_models_match_migration(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migration disagree: {diff}"


def test_vector_columns_have_expected_widths(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.attname, a.atttypmod FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "WHERE c.relname = 'content_features' AND a.attname LIKE '%embedding'"
            )
        ).all()
    assert {name: width for name, width in rows} == {"text_embedding": 384, "visual_embedding": 768}


def test_uuid_primary_keys(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    expected = {
        "users": "id",
        "extension_tokens": "id",
        "sessions": "id",
        "reel_exposures": "event_id",
    }
    for table, column in expected.items():
        assert insp.get_pk_constraint(table)["constrained_columns"] == [column]
        col = next(c for c in insp.get_columns(table) if c["name"] == column)
        assert str(col["type"]) == "UUID", table


def test_composite_primary_keys(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    assert insp.get_pk_constraint("content_features")["constrained_columns"] == [
        "user_id",
        "reel_shortcode",
        "pipeline_version",
    ]
    assert insp.get_pk_constraint("topic_assignments")["constrained_columns"] == [
        "user_id",
        "reel_shortcode",
        "pipeline_version",
        "topic_id",
    ]
    assert insp.get_pk_constraint("topic_transitions")["constrained_columns"] == [
        "user_id",
        "from_topic",
        "to_topic",
    ]


def test_foreign_keys_and_delete_rules(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT kcu.table_name, kcu.column_name, ccu.table_name, rc.delete_rule "
                "FROM information_schema.referential_constraints rc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON kcu.constraint_name = rc.constraint_name "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON ccu.constraint_name = rc.constraint_name "
                "WHERE kcu.table_schema = 'public'"
            )
        ).all()
    actual = {(t, c): (ref, rule) for t, c, ref, rule in rows}
    assert actual == EXPECTED_FKS


def test_check_constraints_present(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        names = set(
            conn.execute(
                text(
                    "SELECT conname FROM pg_constraint c JOIN pg_namespace n "
                    "ON n.oid = c.connamespace WHERE c.contype = 'c' AND n.nspname = 'public'"
                )
            ).scalars()
        )
    assert names == EXPECTED_CHECKS


def test_indexes_present(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    indexes = {
        (table, tuple(ix["column_names"]))
        for table in ("reel_exposures", "fingerprint_snapshots", "jobs")
        for ix in insp.get_indexes(table)
    }
    assert ("reel_exposures", ("user_id", "observed_at")) in indexes
    assert ("reel_exposures", ("reel_shortcode",)) in indexes
    assert ("fingerprint_snapshots", ("user_id", "kind", "taken_at")) in indexes
    assert ("jobs", ("status", "run_after")) in indexes
    sessions_ix = {ix["name"]: ix for ix in insp.get_indexes("sessions")}
    one_active = sessions_ix["uq_sessions_one_active_per_user"]
    assert one_active["unique"] and one_active["column_names"] == ["user_id"]
    assert ("user_sessions", ("user_id",)) in {
        ("user_sessions", tuple(ix["column_names"])) for ix in insp.get_indexes("user_sessions")
    }


def test_pipeline_version_required_on_every_pipeline_result(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    for table in ("content_features", "topic_assignments", "fingerprint_snapshots", "predictions"):
        col = next(c for c in insp.get_columns(table) if c["name"] == "pipeline_version")
        assert col["nullable"] is False, table


def test_downgrade_then_upgrade_round_trip(migrated_engine: Engine, db_url: str) -> None:
    command.downgrade(alembic_config(db_url), "base")
    assert set(inspect(migrated_engine).get_table_names()) - {"alembic_version"} == set()
    command.upgrade(alembic_config(db_url), "head")
    assert set(inspect(migrated_engine).get_table_names()) - {"alembic_version"} == SPEC_TABLES


# ---------------------------------------------------------------- defaults


def test_defaults(seeded: Engine) -> None:
    with seeded.begin() as conn:
        add_prediction(conn)
        conn.execute(text("INSERT INTO jobs (kind, payload) VALUES ('features', '{}')"))
        process_status = conn.execute(text("SELECT process_status FROM reel_exposures")).scalar()
        job = conn.execute(text("SELECT status, attempts, run_after, created_at FROM jobs")).one()
        prediction_created = conn.execute(text("SELECT created_at FROM predictions")).scalar()
        user_created = conn.execute(text("SELECT created_at FROM users")).scalar()
    assert process_status == "pending"
    assert job.status == "queued"
    assert job.attempts == 0
    assert job.run_after is not None and job.created_at is not None
    assert prediction_created is not None and user_created is not None
    assert user_created.tzinfo is not None  # timestamptz


def test_event_id_is_idempotent(seeded: Engine) -> None:
    with seeded.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO reel_exposures (event_id, session_id, user_id, reel_shortcode, "
                "observed_at, field_status, adapter_version) VALUES "
                "(:e, :s, :u, 'Cabc123', now(), '{}', 'test') ON CONFLICT (event_id) DO NOTHING"
            ),
            {"e": EVENT, "s": SESSION, "u": USER},
        )
        assert count(conn, "reel_exposures") == 1
    assert rejected(
        seeded,
        "INSERT INTO reel_exposures (event_id, session_id, user_id, reel_shortcode, observed_at, "
        "field_status, adapter_version) VALUES (:e, :s, :u, 'X', now(), '{}', 'test')",
        {"e": EVENT, "s": SESSION, "u": USER},
    )


def test_unique_email_and_token(seeded: Engine) -> None:
    assert rejected(
        seeded,
        "INSERT INTO users (id, email, password_hash) VALUES (:id, :email, 'x')",
        {"id": OTHER_USER, "email": f"{USER}@example.test"},
    )
    with seeded.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO extension_tokens (id, user_id, token_sha256) VALUES (:id, :u, '\\x01')"
            ),
            {"id": uuid.uuid4(), "u": USER},
        )
    assert rejected(
        seeded,
        "INSERT INTO extension_tokens (id, user_id, token_sha256) VALUES (:id, :u, '\\x01')",
        {"id": uuid.uuid4(), "u": USER},
    )


# ---------------------------------------------------------------- CHECK constraints


@pytest.mark.parametrize(("kind", "bucket"), [("preference", "session"), ("exposure", "week")])
def test_snapshot_kind_and_bucket_enums(seeded: Engine, kind: str, bucket: str) -> None:
    with seeded.connect() as conn, conn.begin(), pytest.raises(IntegrityError):
        add_snapshot(conn, kind=kind, bucket=bucket)


@pytest.mark.parametrize(
    ("kind", "bucket", "session_id"),
    [("exposure", "session", SESSION), ("engagement", "day", None)],
)
def test_snapshot_valid_values_accepted(
    seeded: Engine, kind: str, bucket: str, session_id: uuid.UUID | None
) -> None:
    with seeded.begin() as conn:
        add_snapshot(conn, kind=kind, bucket=bucket, session_id=session_id)
        assert count(conn, "fingerprint_snapshots") == 1


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE reel_exposures SET dwell_ms = -1",
        "UPDATE reel_exposures SET video_duration_ms = -1",
        "INSERT INTO jobs (kind, payload, attempts) VALUES ('x', '{}', -1)",
        "INSERT INTO content_features (user_id, reel_shortcode, pipeline_version, ocr_confidence, "
        "modalities) VALUES ('00000000-0000-0000-0000-000000000001', 'C1', 'pv', 1.5, '{}')",
        "INSERT INTO content_features (user_id, reel_shortcode, pipeline_version, ocr_confidence, "
        "modalities) VALUES ('00000000-0000-0000-0000-000000000001', 'C1', 'pv', -0.1, '{}')",
        "INSERT INTO topic_assignments (user_id, reel_shortcode, pipeline_version, topic_id, p_fused) "
        "VALUES ('00000000-0000-0000-0000-000000000001', 'C1', 'pv', 1, 1.01)",
        "INSERT INTO topic_assignments (user_id, reel_shortcode, pipeline_version, topic_id, p_text, "
        "p_fused) VALUES ('00000000-0000-0000-0000-000000000001', 'C1', 'pv', 1, -0.5, 0.5)",
        "INSERT INTO topic_assignments (user_id, reel_shortcode, pipeline_version, topic_id, p_visual, "
        "p_fused) VALUES ('00000000-0000-0000-0000-000000000001', 'C1', 'pv', 1, 2, 0.5)",
    ],
)
def test_range_checks_reject_bad_values(seeded: Engine, sql: str) -> None:
    assert rejected(seeded, sql)


def test_negative_counts_rejected(seeded: Engine) -> None:
    with seeded.connect() as conn, conn.begin(), pytest.raises(IntegrityError):
        add_snapshot(conn, n_reels=-1)
    assert rejected(
        seeded,
        "INSERT INTO topic_transitions (user_id, from_topic, to_topic, n) VALUES (:u, 1, 1, -1)",
        {"u": USER},
    )


def test_range_checks_accept_bounds_and_nulls(seeded: Engine) -> None:
    with seeded.begin() as conn:
        conn.execute(text("UPDATE reel_exposures SET dwell_ms = 0, video_duration_ms = NULL"))
        conn.execute(
            text(
                "INSERT INTO content_features (user_id, reel_shortcode, pipeline_version, ocr_confidence, "
                "modalities) VALUES ('00000000-0000-0000-0000-000000000001', 'C1', 'pv', 0, '{text}'), ('00000000-0000-0000-0000-000000000001', 'C2', 'pv', 1, '{text}'), "
                "('00000000-0000-0000-0000-000000000001', 'C3', 'pv', NULL, '{}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO topic_assignments (user_id, reel_shortcode, pipeline_version, topic_id, "
                "p_text, p_visual, p_fused) VALUES ('00000000-0000-0000-0000-000000000001', 'C1', 'pv', 1, NULL, 1, 0)"
            )
        )
        assert count(conn, "content_features") == 3
        assert count(conn, "topic_assignments") == 1


# ---------------------------------------------------------------- foreign keys


def test_unknown_topic_rejected(seeded: Engine) -> None:
    assert rejected(
        seeded,
        "INSERT INTO topic_transitions (user_id, from_topic, to_topic) VALUES (:u, 1, 99)",
        {"u": USER},
    )
    assert rejected(
        seeded,
        "INSERT INTO topic_transitions (user_id, from_topic, to_topic) VALUES (:u, 99, 1)",
        {"u": USER},
    )
    assert rejected(
        seeded,
        "INSERT INTO topic_assignments (user_id, reel_shortcode, pipeline_version, topic_id, p_fused) "
        "VALUES ('00000000-0000-0000-0000-000000000001', 'C1', 'pv', 99, 0.5)",
    )
    assert rejected(
        seeded,
        "INSERT INTO predictions (user_id, made_before_event, model, distribution, actual_topic, "
        "pipeline_version) VALUES (:u, :e, 'm', '{}', 99, 'pv')",
        {"u": USER, "e": EVENT},
    )


def test_referenced_topic_cannot_be_deleted(seeded: Engine) -> None:
    with seeded.begin() as conn:
        conn.execute(
            text("INSERT INTO topic_transitions (user_id, from_topic, to_topic) VALUES (:u, 1, 1)"),
            {"u": USER},
        )
    assert rejected(seeded, "DELETE FROM topics WHERE id = 1")


def test_prediction_needs_existing_event(seeded: Engine) -> None:
    assert rejected(
        seeded,
        "INSERT INTO predictions (user_id, made_before_event, model, distribution, "
        "pipeline_version) VALUES (:u, :e, 'm', '{}', 'pv')",
        {"u": USER, "e": uuid.uuid4()},
    )


def test_snapshot_needs_existing_session(seeded: Engine) -> None:
    with seeded.connect() as conn, conn.begin(), pytest.raises(IntegrityError):
        add_snapshot(conn, session_id=uuid.uuid4())


# ---------------------------------------------------------------- cascades (F13)


def test_session_delete_removes_its_derived_rows_only(seeded: Engine) -> None:
    with seeded.begin() as conn:
        add_session(conn, OTHER_SESSION, active=False)
        add_exposure(conn, OTHER_EVENT, OTHER_SESSION)
        add_prediction(conn, EVENT)
        add_prediction(conn, OTHER_EVENT)
        add_snapshot(conn, session_id=SESSION)
        add_snapshot(conn, session_id=OTHER_SESSION)
        add_snapshot(conn, bucket="day", session_id=None)

        conn.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": SESSION})

        assert conn.execute(text("SELECT event_id FROM reel_exposures")).scalars().all() == [
            OTHER_EVENT
        ]
        assert conn.execute(text("SELECT made_before_event FROM predictions")).scalars().all() == [
            OTHER_EVENT
        ]
        remaining = conn.execute(text("SELECT session_id FROM fingerprint_snapshots")).all()
        assert sorted(remaining, key=str) == sorted([(OTHER_SESSION,), (None,)], key=str)


def test_user_delete_leaves_zero_user_rows(seeded: Engine) -> None:
    with seeded.begin() as conn:
        add_user(conn, OTHER_USER)
        add_session(conn, OTHER_SESSION, OTHER_USER)
        add_exposure(conn, OTHER_EVENT, OTHER_SESSION, OTHER_USER)
        for user, event, session in (
            (USER, EVENT, SESSION),
            (OTHER_USER, OTHER_EVENT, OTHER_SESSION),
        ):
            conn.execute(
                text(
                    "INSERT INTO extension_tokens (id, user_id, token_sha256) VALUES (:id, :u, :h)"
                ),
                {"id": uuid.uuid4(), "u": user, "h": user.bytes},
            )
            conn.execute(
                text(
                    "INSERT INTO consents (user_id, policy_version, granted_at) "
                    "VALUES (:u, 'p1', now())"
                ),
                {"u": user},
            )
            conn.execute(
                text(
                    "INSERT INTO topic_transitions (user_id, from_topic, to_topic, n) "
                    "VALUES (:u, 1, 1, 3)"
                ),
                {"u": user},
            )
            add_prediction(conn, event, user)
            conn.execute(
                text(
                    "INSERT INTO prediction_state (user_id, pipeline_version, state) "
                    "VALUES (:u, 'pr1-x', '{}'::jsonb)"
                ),
                {"u": user},
            )
            conn.execute(
                text("INSERT INTO survey_responses (user_id, status) VALUES (:u, 'submitted')"),
                {"u": user},
            )
            conn.execute(
                text(
                    "INSERT INTO self_report_ratings (user_id, topic_id, rating) VALUES (:u, 1, 4)"
                ),
                {"u": user},
            )
            add_snapshot(conn, session_id=session, user_id=user)
            conn.execute(
                text(
                    "INSERT INTO user_sessions (id_sha256, user_id, expires_at) "
                    "VALUES (:h, :u, now() + interval '1 day')"
                ),
                {"h": user.bytes + b"s", "u": user},
            )
            conn.execute(
                text(
                    "INSERT INTO invites (code_sha256, expires_at, used_at, used_by) "
                    "VALUES (:h, now() + interval '1 day', now(), :u)"
                ),
                {"h": user.bytes + b"i", "u": user},
            )

        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": USER})

        for table in USER_SCOPED_TABLES:
            n = conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE user_id = :u"),
                {"u": USER},
            ).scalar_one()
            assert n == 0, f"{table} still has rows for the deleted user"
            assert count(conn, table) == 1, f"{table} lost the other user's row"
        assert count(conn, "users") == 1
        assert count(conn, "invites") == 1  # only the other user's used invite remains


def test_invite_used_pair_check(seeded: Engine) -> None:
    assert rejected(
        seeded,
        "INSERT INTO invites (code_sha256, expires_at, used_at) "
        "VALUES ('\x01', now() + interval '1 day', now())",
    )
    assert rejected(
        seeded,
        "INSERT INTO invites (code_sha256, expires_at, used_by) "
        "VALUES ('\x02', now() + interval '1 day', :u)",
        {"u": USER},
    )


def test_session_must_expire_after_creation(seeded: Engine) -> None:
    assert rejected(
        seeded,
        "INSERT INTO user_sessions (id_sha256, user_id, expires_at) "
        "VALUES ('\x01', :u, now() - interval '1 day')",
        {"u": USER},
    )


# ---------------------------------------------------------------- session rules (ADR-014)


def insert_session(**overrides: object) -> tuple[str, dict[str, object]]:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "u": USER,
        "status": "stopped",
        "reason": "user",
        "start": "now() - interval '1 hour'",
        "end": "now()",
    }
    values.update(overrides)
    end = "NULL" if values["end"] is None else values["end"]
    reason = ":reason" if values["reason"] is not None else "NULL"
    sql = (
        "INSERT INTO sessions (id, user_id, started_at, ended_at, status, stop_reason) "
        f"VALUES (:id, :u, {values['start']}, {end}, :status, {reason})"
    )
    params = {k: v for k, v in values.items() if k in ("id", "u", "status", "reason")}
    if values["reason"] is None:
        params.pop("reason")
    return sql, params


@pytest.mark.parametrize(
    "reason",
    ["user", "left_surface", "tab_closed", "time_cap", "consent_revoked", "browser_restart"],
)
def test_every_stop_reason_is_accepted(seeded: Engine, reason: str) -> None:
    sql, params = insert_session(reason=reason)
    with seeded.begin() as conn:
        conn.execute(text(sql), params)


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": "bored"},  # not in the vocabulary
        {"status": "paused", "reason": None, "end": None},  # not in the vocabulary
        {"status": "stopped", "reason": None},  # stopped needs a reason
        {"status": "stopped", "end": None},  # stopped needs an end
        {"status": "active", "reason": "user", "end": None},  # active has no reason
        {"status": "active", "reason": None},  # active has no end
        {"start": "now()", "end": "now() - interval '1 second'"},  # ends before it starts
    ],
)
def test_invalid_session_states_are_rejected(seeded: Engine, overrides: dict[str, object]) -> None:
    # seeded already holds an active session for USER, so build these for OTHER_USER
    with seeded.begin() as conn:
        add_user(conn, OTHER_USER)
    sql, params = insert_session(u=OTHER_USER, **overrides)
    assert rejected(seeded, sql, params)


def test_only_one_active_session_per_user(seeded: Engine) -> None:
    # seeded holds one active session for USER
    sql, params = insert_session(status="active", reason=None, end=None)
    assert rejected(seeded, sql, params)


def test_a_new_session_is_allowed_after_the_old_one_stops(seeded: Engine) -> None:
    with seeded.begin() as conn:
        conn.execute(
            text(
                "UPDATE sessions SET status = 'stopped', ended_at = now(), stop_reason = 'user' "
                "WHERE id = :id"
            ),
            {"id": SESSION},
        )
        sql, params = insert_session(status="active", reason=None, end=None)
        conn.execute(text(sql), params)
        assert count(conn, "sessions") == 2


def test_different_users_may_each_have_an_active_session(seeded: Engine) -> None:
    with seeded.begin() as conn:
        add_user(conn, OTHER_USER)
        add_session(conn, OTHER_SESSION, OTHER_USER)
        assert count(conn, "sessions") == 2


def test_consent_cannot_be_revoked_before_it_was_granted(seeded: Engine) -> None:
    assert rejected(
        seeded,
        "INSERT INTO consents (user_id, policy_version, granted_at, revoked_at) "
        "VALUES (:u, 'p1', now(), now() - interval '1 day')",
        {"u": USER},
    )
