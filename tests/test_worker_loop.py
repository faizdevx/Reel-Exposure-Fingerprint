"""Job loop: claim, process, retry, fail, rebuild. Fake encoders; needs PostgreSQL + pgvector."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from worker_fakes import FakeEmbedder

from app.models import (
    FingerprintSnapshot,
    Job,
    ReelExposure,
    TopicAssignment,
    User,
)
from app.models import Session as MonitoringSession
from app.taxonomy import load_taxonomy
from app.worker.config import TextPipelineConfig
from app.worker.loop import MAX_ATTEMPTS, claim_job, run_forever, run_one
from app.worker.reel_processor import ReelPipeline
from app.worker.text_pipeline import TextPipeline
from app.worker.vision_config import FusionConfig

pytestmark = pytest.mark.db

TAXONOMY_FILE = Path(__file__).resolve().parents[1] / "taxonomy" / "topics.json"
T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def make_pipeline(embedder: FakeEmbedder | None = None) -> ReelPipeline:
    taxonomy = load_taxonomy(TAXONOMY_FILE)
    text = TextPipeline.create(TextPipelineConfig(), taxonomy, embedder or FakeEmbedder(), None)
    return ReelPipeline(text, None, FusionConfig())


def queue_reels(db: Session, captions: list[str | None]) -> tuple[uuid.UUID, list[uuid.UUID]]:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@example.test", password_hash="x")
    session = MonitoringSession(
        id=uuid.uuid4(),
        user_id=user.id,
        started_at=T0 - timedelta(hours=1),
        ended_at=T0 + timedelta(hours=1),
        status="stopped",
        stop_reason="user",
    )
    db.add(user)
    db.commit()
    db.add(session)
    db.commit()
    events: list[uuid.UUID] = []
    for i, caption in enumerate(captions):
        event_id = uuid.uuid4()
        events.append(event_id)
        db.add(
            ReelExposure(
                event_id=event_id,
                session_id=session.id,
                user_id=user.id,
                reel_shortcode=f"reel{i}",
                observed_at=T0 + timedelta(minutes=i),
                dwell_ms=5_000 + i,
                video_duration_ms=20_000,
                caption=caption,
                hashtags=["cricket"] if caption else None,
                field_status={},
                adapter_version="t",
            )
        )
        db.add(Job(kind="exposure", payload={"event_id": str(event_id)}))
    db.commit()
    return user.id, events


def job_statuses(db: Session) -> list[str]:
    db.expire_all()
    return [s for (s,) in db.execute(select(Job.status).order_by(Job.id))]


def test_drain_processes_every_job_and_builds_fingerprints(db: Session) -> None:
    user, events = queue_reels(db, ["cricket six over", "football goal", "ai coding python"])
    run_forever(db, make_pipeline(), once=True)

    assert job_statuses(db) == ["done", "done", "done"]
    db.expire_all()
    rows = db.execute(select(ReelExposure).where(ReelExposure.user_id == user)).scalars().all()
    assert {r.process_status for r in rows} == {"done"}
    assigned = db.execute(select(TopicAssignment.reel_shortcode).distinct()).all()
    assert len(assigned) == 3
    snaps = (
        db.execute(select(FingerprintSnapshot).where(FingerprintSnapshot.user_id == user))
        .scalars()
        .all()
    )
    assert snaps, "fingerprint snapshots are rebuilt once the queue is empty"
    assert len(events) == 3


def test_a_claimed_job_is_not_claimed_twice(db: Session) -> None:
    queue_reels(db, ["cricket six"])
    first = claim_job(db)
    assert first is not None
    assert claim_job(db) is None
    assert job_statuses(db) == ["running"]


def test_a_job_whose_exposure_was_deleted_is_closed_not_retried(db: Session) -> None:
    _, events = queue_reels(db, ["cricket six"])
    db.query(ReelExposure).filter(ReelExposure.event_id == events[0]).delete()
    db.commit()
    assert run_one(db, make_pipeline()) == ("gone", None)
    assert job_statuses(db) == ["done"]


def test_a_failing_job_is_retried_then_failed_with_the_class_name_only(db: Session) -> None:
    queue_reels(db, ["cricket six"])
    embedder = FakeEmbedder()
    embedder.fail = lambda kind, texts: (
        RuntimeError("secret caption text") if kind == "query" else None
    )
    pipeline = make_pipeline(embedder)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = run_one(db, pipeline)
        assert result is not None
        expected = "failed" if attempt == MAX_ATTEMPTS else "retry"
        assert result[0] == expected
        if attempt < MAX_ATTEMPTS:  # make the retry due now
            db.query(Job).update({"run_after": datetime.now(UTC) - timedelta(seconds=1)})
            db.commit()

    db.expire_all()
    job = db.execute(select(Job)).scalar_one()
    assert job.status == "failed"
    assert job.attempts == MAX_ATTEMPTS
    assert job.last_error == "RuntimeError"
    assert "secret" not in (job.last_error or "")


def test_a_reel_without_text_ends_without_signal_not_failed(db: Session) -> None:
    queue_reels(db, [None])
    run_forever(db, make_pipeline(), once=True)
    assert job_statuses(db) == ["done"]
    db.expire_all()
    exposure = db.execute(select(ReelExposure)).scalar_one()
    assert exposure.process_status == "no_signal"
