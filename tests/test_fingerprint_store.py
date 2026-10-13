"""Fingerprint database edge: load observations, store session snapshots.

Needs PostgreSQL + pgvector via TEST_DATABASE_URL; skipped otherwise.
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session as DbSession

from app.fingerprint.engine import FingerprintConfig, rebuild
from app.fingerprint.store import (
    load_observations,
    rebuild_user,
    store_snapshots,
    users_with_exposures,
)
from app.models import (
    FingerprintSnapshot,
    ReelExposure,
    Topic,
    TopicAssignment,
    User,
)
from app.models import Session as MonitoringSession

pytestmark = pytest.mark.db

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
AV = "pv1-test"
TOPICS = (1, 2)


def _arrange(db: DbSession) -> tuple[uuid.UUID, uuid.UUID]:
    user = User(id=uuid.uuid4(), email="fp@example.test", password_hash="x")
    db.add(user)
    db.flush()
    session = MonitoringSession(
        id=uuid.uuid4(),
        user_id=user.id,
        started_at=T0,
        ended_at=T0 + timedelta(hours=1),
        status="stopped",
        stop_reason="user",
    )
    db.add(session)
    for tid in TOPICS:
        db.add(Topic(id=tid, slug=f"t{tid}", label=f"T{tid}", taxonomy_version="t1"))
    db.flush()
    for shortcode, p in (("reelA", (1.0, 0.0)), ("reelB", (0.25, 0.75))):
        for tid, value in zip(TOPICS, p, strict=True):
            db.add(
                TopicAssignment(
                    user_id=user.id,
                    reel_shortcode=shortcode,
                    pipeline_version=AV,
                    topic_id=tid,
                    p_fused=value,
                )
            )
    for i, shortcode in enumerate(("reelA", "reelB", "reelC")):  # reelC has no assignment
        db.add(
            ReelExposure(
                event_id=uuid.UUID(int=100 + i),
                session_id=session.id,
                user_id=user.id,
                reel_shortcode=shortcode,
                observed_at=T0 + timedelta(minutes=i),
                dwell_ms=10_000,
                video_duration_ms=20_000,
                field_status={},
                adapter_version="t",
            )
        )
    db.flush()
    return user.id, session.id


def test_load_observations_marks_reels_without_a_complete_assignment(db: DbSession) -> None:
    user_id, _ = _arrange(db)
    observations = {o.event_id: o for o in load_observations(db, user_id, AV, TOPICS)}
    assert observations[uuid.UUID(int=100)].probabilities == {1: 1.0, 2: 0.0}
    assert observations[uuid.UUID(int=101)].probabilities == {1: 0.25, 2: 0.75}
    assert observations[uuid.UUID(int=102)].probabilities is None
    assert load_observations(db, user_id, "other-version", TOPICS)[0].probabilities is None


def test_store_snapshots_is_idempotent_and_carries_the_version(db: DbSession) -> None:
    user_id, session_id = _arrange(db)
    result = rebuild(load_observations(db, user_id, AV, TOPICS), FingerprintConfig(), AV)
    assert store_snapshots(db, user_id, result.snapshots) == 4  # session and day, both kinds
    assert store_snapshots(db, user_id, result.snapshots) == 4  # replay: replaced, not added
    rows = list(db.execute(select(FingerprintSnapshot)).scalars())
    assert len(rows) == 4
    assert {r.kind for r in rows} == {"exposure", "engagement"}
    assert {r.pipeline_version for r in rows} == {result.pipeline_version}
    session_rows = [r for r in rows if r.bucket == "session"]
    assert {r.session_id for r in session_rows} == {session_id}
    assert {r.session_id for r in rows if r.bucket == "day"} == {None}
    exposure = next(r for r in session_rows if r.kind == "exposure")
    assert exposure.n_reels == 2
    assert exposure.weights["raw"]["1"] > 0.0


def test_new_pipeline_version_adds_rows_next_to_the_old(db: DbSession) -> None:
    user_id, _ = _arrange(db)
    observations = load_observations(db, user_id, AV, TOPICS)
    store_snapshots(db, user_id, rebuild(observations, FingerprintConfig(), AV).snapshots)
    store_snapshots(
        db, user_id, rebuild(observations, FingerprintConfig(half_life_days=3.0), AV).snapshots
    )
    assert db.execute(select(func.count()).select_from(FingerprintSnapshot)).scalar_one() == 8


def test_a_reel_assigned_to_other_counts_in_the_fingerprint(db: DbSession) -> None:
    from app.worker.fusion import OTHER_TOPIC_ID

    user_id, _ = _arrange(db)
    db.add(Topic(id=OTHER_TOPIC_ID, slug="other", label="Other", taxonomy_version="t1"))
    db.commit()
    for tid in (*TOPICS, OTHER_TOPIC_ID):  # reelC: assigned to other
        db.add(
            TopicAssignment(
                user_id=user_id,
                reel_shortcode="reelC",
                pipeline_version=AV,
                topic_id=tid,
                p_fused=1.0 if tid == OTHER_TOPIC_ID else 0.0,
            )
        )
    db.commit()

    observations = {o.event_id: o for o in load_observations(db, user_id, AV, TOPICS)}
    other_reel = observations[uuid.UUID(int=102)].probabilities
    assert other_reel == {1: 0.0, 2: 0.0, OTHER_TOPIC_ID: 1.0}
    result = rebuild(list(observations.values()), FingerprintConfig(), AV)
    assert result.states["exposure"].weights[OTHER_TOPIC_ID] == pytest.approx(1.0, abs=1e-6)


# ---- day snapshots, replay and reproducibility (M5)


def _snapshot_rows(db: DbSession, user_id: uuid.UUID, version: str | None = None) -> list[tuple]:  # type: ignore[type-arg]
    db.expire_all()
    query = select(FingerprintSnapshot).where(FingerprintSnapshot.user_id == user_id)
    if version is not None:
        query = query.where(FingerprintSnapshot.pipeline_version == version)
    rows = db.execute(
        query.order_by(
            FingerprintSnapshot.taken_at,
            FingerprintSnapshot.bucket,
            FingerprintSnapshot.kind,
            FingerprintSnapshot.pipeline_version,
        )
    ).scalars()
    return [
        (r.bucket, r.kind, r.taken_at, r.session_id, r.n_reels, r.pipeline_version, r.weights)
        for r in rows
    ]


def _add_reel_next_day(db: DbSession, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
    db.add(
        ReelExposure(
            event_id=uuid.UUID(int=200),
            session_id=session_id,
            user_id=user_id,
            reel_shortcode="reelA",
            observed_at=T0 + timedelta(days=2),
            dwell_ms=8_000,
            video_duration_ms=10_000,
            field_status={},
            adapter_version="t",
        )
    )
    db.commit()


def test_rebuild_user_stores_session_and_day_snapshots(db: DbSession) -> None:
    user_id, _session_id = _arrange(db)
    result = rebuild_user(
        db, user_id, assignment_version=AV, topic_ids=TOPICS, config=FingerprintConfig()
    )
    db.commit()
    rows = _snapshot_rows(db, user_id)
    assert len(rows) == len(result.snapshots) == 2 * 2  # one session, one day, two kinds
    assert {(r[0], r[1]) for r in rows} == {
        ("session", "exposure"),
        ("session", "engagement"),
        ("day", "exposure"),
        ("day", "engagement"),
    }
    day = next(r for r in rows if r[0] == "day")
    assert day[3] is None  # a day snapshot belongs to no session
    assert {r[5] for r in rows} == {result.pipeline_version}


def test_replaying_reproduces_the_stored_snapshots_exactly(db: DbSession) -> None:
    user_id, _ = _arrange(db)
    kwargs = {"assignment_version": AV, "topic_ids": TOPICS, "config": FingerprintConfig()}
    rebuild_user(db, user_id, **kwargs)  # type: ignore[arg-type]
    db.commit()
    first = _snapshot_rows(db, user_id)

    rebuild_user(db, user_id, **kwargs)  # type: ignore[arg-type]
    db.commit()
    second = _snapshot_rows(db, user_id)

    assert second == first  # same rows, same numbers, nothing doubled
    assert len(second) == 4


def test_a_later_reel_adds_a_day_and_leaves_earlier_days_untouched(db: DbSession) -> None:
    user_id, session_id = _arrange(db)
    kwargs = {"assignment_version": AV, "topic_ids": TOPICS, "config": FingerprintConfig()}
    rebuild_user(db, user_id, **kwargs)  # type: ignore[arg-type]
    db.commit()
    before = [r for r in _snapshot_rows(db, user_id) if r[0] == "day"]

    _add_reel_next_day(db, user_id, session_id)
    rebuild_user(db, user_id, **kwargs)  # type: ignore[arg-type]
    db.commit()
    after = [r for r in _snapshot_rows(db, user_id) if r[0] == "day"]

    assert len(after) == len(before) + 2
    assert after[: len(before)] == before  # the past does not change when the future is added
    assert [r[4] for r in after if r[1] == "exposure"] == [2, 3]


def test_a_rebuild_replaces_the_days_of_its_version_only(db: DbSession) -> None:
    user_id, _ = _arrange(db)
    old = FingerprintConfig(half_life_days=3.0)
    new = FingerprintConfig()
    r_old = rebuild_user(db, user_id, assignment_version=AV, topic_ids=TOPICS, config=old)
    r_new = rebuild_user(db, user_id, assignment_version=AV, topic_ids=TOPICS, config=new)
    db.commit()
    assert r_old.pipeline_version != r_new.pipeline_version
    assert len(_snapshot_rows(db, user_id, r_old.pipeline_version)) == 4
    assert len(_snapshot_rows(db, user_id, r_new.pipeline_version)) == 4
    rebuild_user(db, user_id, assignment_version=AV, topic_ids=TOPICS, config=new)
    db.commit()
    assert len(_snapshot_rows(db, user_id)) == 8  # the old version's rows stayed, none doubled


def test_a_rebuild_for_one_user_does_not_touch_another(db: DbSession) -> None:
    user_id, _ = _arrange(db)
    other = User(id=uuid.uuid4(), email="other@example.test", password_hash="x")
    db.add(other)
    db.commit()
    kwargs = {"assignment_version": AV, "topic_ids": TOPICS, "config": FingerprintConfig()}
    rebuild_user(db, user_id, **kwargs)  # type: ignore[arg-type]
    db.commit()
    assert _snapshot_rows(db, other.id) == []
    rebuild_user(db, other.id, **kwargs)  # type: ignore[arg-type]
    db.commit()
    assert _snapshot_rows(db, other.id) == []  # no exposures, no snapshots
    assert len(_snapshot_rows(db, user_id)) == 4


def test_users_with_exposures_lists_only_users_who_have_some(db: DbSession) -> None:
    user_id, _ = _arrange(db)
    db.add(User(id=uuid.uuid4(), email="empty@example.test", password_hash="x"))
    db.commit()
    assert users_with_exposures(db) == [user_id]


def test_the_rebuild_command_writes_snapshots_and_prints_counts_only(
    db: DbSession,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app import manage
    from app.taxonomy import load_taxonomy

    taxonomy = load_taxonomy(Path(__file__).resolve().parents[1] / "taxonomy" / "topics.json")
    user = User(id=uuid.uuid4(), email="cli@example.test", password_hash="x")
    db.add(user)
    db.flush()
    session = MonitoringSession(
        id=uuid.uuid4(),
        user_id=user.id,
        started_at=T0,
        ended_at=T0 + timedelta(hours=1),
        status="stopped",
        stop_reason="user",
    )
    db.add(session)
    for topic in taxonomy.topics:
        db.add(Topic(id=topic.id, slug=topic.slug, label=topic.label, taxonomy_version="t1"))
    db.flush()
    first = taxonomy.topics[0].id
    for topic in taxonomy.topics:
        db.add(
            TopicAssignment(
                user_id=user.id,
                reel_shortcode="reelX",
                pipeline_version=AV,
                topic_id=topic.id,
                p_fused=1.0 if topic.id == first else 0.0,
            )
        )
    db.add(
        ReelExposure(
            event_id=uuid.uuid4(),
            session_id=session.id,
            user_id=user.id,
            reel_shortcode="reelX",
            observed_at=T0,
            dwell_ms=9_000,
            video_duration_ms=10_000,
            field_status={},
            adapter_version="t",
        )
    )
    db.commit()
    monkeypatch.setattr(manage, "get_engine", lambda: migrated_engine)

    code = manage.main(["rebuild-fingerprints", "--assignment-version", AV])

    assert code == 0
    out = capsys.readouterr().out
    assert f"user={user.id} snapshots=4" in out and "fingerprint_version=fp1-" in out
    assert "cli@example.test" not in out  # ids and counts only
    assert len(_snapshot_rows(db, user.id)) == 4


def test_a_user_with_more_distinct_reels_than_a_query_has_parameters_still_loads(
    db: DbSession,
) -> None:
    """N11 allows 100,000 exposures for one user. `reel_shortcode IN (...)` carried one bound
    parameter per Reel and failed above 65,535 (found by the performance benchmark)."""
    from sqlalchemy import insert

    user_id, session_id = _arrange(db)
    n = 66_000
    db.execute(
        insert(ReelExposure),
        [
            {
                "event_id": uuid.UUID(int=10_000 + i),
                "session_id": session_id,
                "user_id": user_id,
                "reel_shortcode": f"many{i}",
                "observed_at": T0 + timedelta(seconds=10 + i),
                "dwell_ms": 5000,
                "video_duration_ms": 10_000,
                "field_status": {},
                "adapter_version": "t",
            }
            for i in range(n)
        ],
    )
    db.execute(
        insert(TopicAssignment),
        [
            {
                "user_id": user_id,
                "reel_shortcode": f"many{i}",
                "pipeline_version": AV,
                "topic_id": tid,
                "p_fused": 0.5,
            }
            for i in range(n)
            for tid in TOPICS
        ],
    )
    db.commit()
    observations = load_observations(db, user_id, AV, TOPICS)
    with_scores = [o for o in observations if o.probabilities is not None]
    assert len(observations) == n + 3 and len(with_scores) == n + 2  # + the three of `_arrange`
    assert with_scores[0].probabilities is not None
    # and the whole rebuild works on it
    result = rebuild_user(
        db, user_id, assignment_version=AV, topic_ids=TOPICS, config=FingerprintConfig()
    )
    assert result.states["exposure"].n_reels == n + 2
