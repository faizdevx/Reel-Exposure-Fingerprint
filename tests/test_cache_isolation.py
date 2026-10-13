"""Security audit H-3: the Reel cache is per user, so one user cannot decide another's Reel.

The text behind a cache row is what the viewer's client sent. Before ADR-024 the row was keyed by
shortcode alone and the first writer won, so a user could pre-seed the assignment of a Reel that
others would watch. Needs PostgreSQL + pgvector (`TEST_DATABASE_URL`) and the fake encoders.
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.orm import Session
from support import alembic_config
from worker_fakes import FakeEmbedder

from app.fingerprint.engine import FingerprintConfig
from app.fingerprint.store import load_observations, rebuild_user
from app.models import ContentFeature, ReelExposure, Topic, TopicAssignment, User
from app.models import Session as MonitoringSession
from app.prediction.store import universe_for
from app.taxonomy import load_taxonomy
from app.worker.config import TextPipelineConfig
from app.worker.reel_processor import ReelPipeline, process_reel
from app.worker.text_normalize import TextInput
from app.worker.text_pipeline import TextPipeline
from app.worker.vision_config import FusionConfig

pytestmark = pytest.mark.db

TAXONOMY_FILE = Path(__file__).resolve().parents[1] / "taxonomy" / "topics.json"
CRICKET = TextInput(
    caption="Kohli hits a six in the last over, IPL cricket highlights", hashtags=("cricket",)
)
COOKING = TextInput(
    caption="Easy paneer butter masala recipe, cook dinner in 20 minutes", hashtags=("recipe",)
)
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
AV = "pv-iso"


@pytest.fixture(scope="module")
def pipeline() -> ReelPipeline:
    taxonomy = load_taxonomy(TAXONOMY_FILE)
    text_pipeline = TextPipeline.create(TextPipelineConfig(), taxonomy, FakeEmbedder(), None)
    return ReelPipeline(text_pipeline, None, FusionConfig())


def user(db: Session, name: str) -> uuid.UUID:
    row = User(id=uuid.uuid4(), email=f"{name}@example.test", password_hash="x")
    db.add(row)
    db.commit()
    return row.id


def rows(db: Session, owner: uuid.UUID, code: str, version: str) -> dict[int, float]:
    db.expire_all()
    return {
        r.topic_id: r.p_fused
        for r in db.execute(
            select(TopicAssignment).where(
                TopicAssignment.user_id == owner,
                TopicAssignment.reel_shortcode == code,
                TopicAssignment.pipeline_version == version,
            )
        ).scalars()
    }


# ------------------------------------------------------------------ the attack, and why it fails


def test_a_user_cannot_seed_the_assignment_of_a_reel_someone_else_watches(
    db: Session, pipeline: ReelPipeline
) -> None:
    attacker, victim = user(db, "mallory"), user(db, "victim")
    # The attacker sends a cooking caption for the shortcode of a cricket Reel, and is processed first.
    process_reel(db, pipeline, attacker, "CricketReel1", COOKING)
    # The victim then watches that Reel, with its real caption.
    outcome = process_reel(db, pipeline, victim, "CricketReel1", CRICKET)

    assert outcome.from_cache is False  # the attacker's row was not used for the victim
    honest = process_reel(db, pipeline, victim, "HonestReference", CRICKET)  # no attacker involved
    victims_rows = rows(db, victim, "CricketReel1", pipeline.version)
    assert victims_rows == pytest.approx(rows(db, victim, "HonestReference", pipeline.version))
    assert victims_rows == pytest.approx(honest.p_fused)
    assert victims_rows != pytest.approx(rows(db, attacker, "CricketReel1", pipeline.version))


def test_the_same_holds_in_the_other_order(db: Session, pipeline: ReelPipeline) -> None:
    attacker, victim = user(db, "mallory"), user(db, "victim")
    first = process_reel(db, pipeline, victim, "R1", CRICKET)
    before = rows(db, victim, "R1", pipeline.version)
    process_reel(db, pipeline, attacker, "R1", COOKING)  # a later, hostile caption
    assert rows(db, victim, "R1", pipeline.version) == before  # not overwritten
    assert before == pytest.approx(first.p_fused)


def test_each_user_has_their_own_features_and_assignment_rows(
    db: Session, pipeline: ReelPipeline
) -> None:
    a, b = user(db, "a"), user(db, "b")
    process_reel(db, pipeline, a, "Shared1", CRICKET)
    process_reel(db, pipeline, b, "Shared1", COOKING)
    owners = {r.user_id for r in db.execute(select(ContentFeature)).scalars()}
    assert owners == {a, b}
    topics = len(pipeline.taxonomy.topics)
    assert db.query(TopicAssignment).filter_by(user_id=a).count() == topics
    assert db.query(TopicAssignment).filter_by(user_id=b).count() == topics


def test_the_same_user_still_gets_the_cache_for_a_repeated_reel(
    db: Session, pipeline: ReelPipeline
) -> None:
    owner = user(db, "a")
    first = process_reel(db, pipeline, owner, "Again1", CRICKET)
    second = process_reel(db, pipeline, owner, "Again1", CRICKET)
    assert first.from_cache is False and second.from_cache is True
    assert second.p_fused == pytest.approx(first.p_fused)


# ------------------------------------------------------------------ what the fingerprint reads


def exposure(db: Session, owner: uuid.UUID, session: uuid.UUID, code: str, minute: int) -> None:
    db.add(
        ReelExposure(
            event_id=uuid.uuid4(),
            session_id=session,
            user_id=owner,
            reel_shortcode=code,
            observed_at=T0 + timedelta(minutes=minute),
            dwell_ms=9_000,
            video_duration_ms=15_000,
            field_status={},
            adapter_version="t",
        )
    )
    db.commit()


def session_for(db: Session, owner: uuid.UUID) -> uuid.UUID:
    sid = uuid.uuid4()
    db.add(
        MonitoringSession(
            id=sid,
            user_id=owner,
            started_at=T0,
            ended_at=T0 + timedelta(hours=1),
            status="stopped",
            stop_reason="user",
        )
    )
    db.commit()
    return sid


def seed_topics(db: Session) -> None:
    for tid in (1, 2):
        db.add(Topic(id=tid, slug=f"t{tid}", label=f"T{tid}", taxonomy_version="t1"))
    db.commit()


def test_another_users_assignment_is_never_read_for_my_exposure(db: Session) -> None:
    seed_topics(db)
    me, other = user(db, "me"), user(db, "other")
    for tid in (1, 2):  # only the other user has an assignment for this shortcode
        db.add(
            TopicAssignment(
                user_id=other, reel_shortcode="X1", pipeline_version=AV, topic_id=tid, p_fused=0.5
            )
        )
    db.commit()
    exposure(db, me, session_for(db, me), "X1", 0)
    exposure(db, other, session_for(db, other), "X1", 0)

    mine = load_observations(db, me, AV, (1, 2))
    theirs = load_observations(db, other, AV, (1, 2))
    assert [o.probabilities for o in mine] == [None]  # skipped, not borrowed
    assert [o.probabilities for o in theirs] == [{1: 0.5, 2: 0.5}]
    result = rebuild_user(
        db, me, assignment_version=AV, topic_ids=(1, 2), config=FingerprintConfig()
    )
    assert result.snapshots == () or all(s.n_reels == 0 for s in result.snapshots)


def test_another_users_other_topic_does_not_change_my_prediction_universe(db: Session) -> None:
    seed_topics(db)
    db.add(Topic(id=0, slug="other", label="Other", taxonomy_version="t1"))
    db.commit()
    me, other = user(db, "me"), user(db, "other")
    assert universe_for(db, AV, me) == [1, 2]
    for tid in (0, 1, 2):
        db.add(
            TopicAssignment(
                user_id=other,
                reel_shortcode="odd",
                pipeline_version=AV,
                topic_id=tid,
                p_fused=1.0 if tid == 0 else 0.0,
            )
        )
    db.commit()
    assert universe_for(db, AV, other) == [0, 1, 2]
    assert universe_for(db, AV, me) == [1, 2]  # unchanged by someone else's rows


# ------------------------------------------------------------------ the schema


def test_the_keys_start_with_the_user_and_cascade_on_user_delete(
    db: Session, migrated_engine: Engine
) -> None:
    insp = inspect(migrated_engine)
    assert insp.get_pk_constraint("content_features")["constrained_columns"][0] == "user_id"
    assert insp.get_pk_constraint("topic_assignments")["constrained_columns"][0] == "user_id"
    seed_topics(db)
    owner = user(db, "gone")
    db.add(
        ContentFeature(user_id=owner, reel_shortcode="C", pipeline_version=AV, modalities=["text"])
    )
    db.add(
        TopicAssignment(
            user_id=owner, reel_shortcode="C", pipeline_version=AV, topic_id=1, p_fused=1.0
        )
    )
    db.commit()
    db.execute(text("DELETE FROM users WHERE id = :u"), {"u": owner})
    db.commit()
    assert db.query(ContentFeature).count() == db.query(TopicAssignment).count() == 0


def test_a_row_without_a_user_is_refused(db: Session) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO content_features (reel_shortcode, pipeline_version, modalities) VALUES ('x', 'v', '{}')"
            )
        )
    db.rollback()


# ------------------------------------------------------------------ the migration keeps replayable data


def test_migration_0006_copies_shared_rows_to_each_viewer_and_drops_unowned_ones(
    migrated_engine: Engine, db_url: str
) -> None:
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "0005")
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uuid.uuid4()
    with migrated_engine.begin() as conn:
        for uid, name in ((a, "a"), (b, "b"), (c, "c")):
            conn.execute(
                text("INSERT INTO users (id, email, password_hash) VALUES (:i, :e, 'x')"),
                {"i": uid, "e": f"{name}@example.test"},
            )
        conn.execute(
            text(
                "INSERT INTO topics (id, slug, label, taxonomy_version) VALUES (1, 't1', 'T1', 'v')"
            )
        )
        for uid in (a, b):
            conn.execute(
                text(
                    "INSERT INTO sessions (id, user_id, started_at, ended_at, status, stop_reason) "
                    "VALUES (:s, :u, now(), now(), 'stopped', 'user')"
                ),
                {"s": uuid.uuid4(), "u": uid},
            )
        for uid, code in ((a, "Shared"), (b, "Shared"), (a, "OnlyA")):
            session_id = conn.execute(
                text("SELECT id FROM sessions WHERE user_id = :u"), {"u": uid}
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO reel_exposures (event_id, session_id, user_id, reel_shortcode, observed_at, dwell_ms, field_status, adapter_version) "
                    "VALUES (:e, :s, :u, :c, now(), 1000, '{}', 't')"
                ),
                {"e": uuid.uuid4(), "s": session_id, "u": uid, "c": code},
            )
        for code in ("Shared", "OnlyA", "Orphan"):
            conn.execute(
                text(
                    "INSERT INTO content_features (reel_shortcode, pipeline_version, modalities) VALUES (:c, 'v', '{text}')"
                ),
                {"c": code},
            )
            conn.execute(
                text(
                    "INSERT INTO topic_assignments (reel_shortcode, pipeline_version, topic_id, p_fused) VALUES (:c, 'v', 1, 0.7)"
                ),
                {"c": code},
            )

    command.upgrade(cfg, "head")
    with migrated_engine.begin() as conn:
        got = {
            (str(r[0]), r[1])
            for r in conn.execute(text("SELECT user_id, reel_shortcode FROM topic_assignments"))
        }
        features = {
            (str(r[0]), r[1])
            for r in conn.execute(text("SELECT user_id, reel_shortcode FROM content_features"))
        }
    expected = {(str(a), "Shared"), (str(b), "Shared"), (str(a), "OnlyA")}
    assert (
        got == features == expected
    )  # copied per viewer; the unowned "Orphan" is gone; c has nothing
    assert all(str(c) != uid for uid, _ in got)
