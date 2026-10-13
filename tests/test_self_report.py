"""Self-report survey API, its deletion, and the report built from the database. Needs PostgreSQL."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from support import CSRF, Actor, make_actor

from app.evaluation.__main__ import main as evaluation_main
from app.evaluation.collect import collect
from app.fingerprint.engine import FingerprintConfig
from app.models import (
    ReelExposure,
    SelfReportRating,
    SurveyResponse,
    Topic,
    TopicAssignment,
)
from app.models import Session as MonitoringSession
from app.prediction.config import PredictionConfig

pytestmark = pytest.mark.db

API = "/api/v1/self-report"
AV = "pv-eval"
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def alice(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "alice@example.test")


@pytest.fixture
def bob(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "bob@example.test")


def seed_topics(db: Session) -> None:
    for tid, slug in ((1, "cricket"), (2, "tech"), (3, "food")):
        if db.get(Topic, tid) is None:
            db.add(Topic(id=tid, slug=slug, label=slug.title(), taxonomy_version="t1"))
    if db.get(Topic, 0) is None:
        db.add(Topic(id=0, slug="other", label="Other", taxonomy_version="t1"))
    db.commit()


def put(actor: Actor, ratings: dict[int, int]):  # type: ignore[no-untyped-def]
    return actor.dashboard.put(
        API, json={"ratings": {str(k): v for k, v in ratings.items()}}, headers=CSRF
    )


# ------------------------------------------------------------------ the API


def test_it_needs_a_dashboard_login(client: TestClient, alice: Actor, db: Session) -> None:
    seed_topics(db)
    anonymous = TestClient(client.app, base_url="http://localhost")
    assert anonymous.get(API).status_code == 401
    assert anonymous.put(API, json={"ratings": {}}, headers=CSRF).status_code == 401
    assert anonymous.post(f"{API}/skip", headers=CSRF).status_code == 401
    # an extension token is not a dashboard login
    assert anonymous.get(API, headers=alice.bearer).status_code == 401


def test_a_new_user_is_pending_and_gets_the_topics_without_other(alice: Actor, db: Session) -> None:
    seed_topics(db)
    body = alice.dashboard.get(API).json()
    assert body["status"] == "pending" and body["ratings"] == {} and body["completed_at"] is None
    assert [t["slug"] for t in body["topics"]] == ["cricket", "tech", "food"]


def test_ratings_are_stored_and_read_back(alice: Actor, db: Session) -> None:
    seed_topics(db)
    response = put(alice, {1: 5, 2: 3, 3: 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "submitted" and body["ratings"] == {"1": 5, "2": 3, "3": 1}
    assert alice.dashboard.get(API).json()["ratings"] == {"1": 5, "2": 3, "3": 1}
    db.expire_all()
    assert db.query(SelfReportRating).filter_by(user_id=uuid.UUID(alice.user_id)).count() == 3


@pytest.mark.parametrize("bad", [0, 6, -1, 100])
def test_a_rating_must_be_a_whole_number_from_1_to_5(alice: Actor, db: Session, bad: int) -> None:
    seed_topics(db)
    assert put(alice, {1: bad, 2: 3, 3: 3}).status_code == 422
    assert alice.dashboard.get(API).json()["status"] == "pending"


def test_a_fractional_or_text_rating_is_refused(alice: Actor, db: Session) -> None:
    seed_topics(db)
    for value in (3.5, "4", None, True):
        response = alice.dashboard.put(
            API, json={"ratings": {"1": value, "2": 3, "3": 3}}, headers=CSRF
        )
        assert response.status_code == 422, value


def test_every_topic_must_be_rated_and_no_other_ones(alice: Actor, db: Session) -> None:
    seed_topics(db)
    assert put(alice, {1: 3, 2: 3}).status_code == 422  # one missing
    assert put(alice, {1: 3, 2: 3, 3: 3, 4: 3}).status_code == 422  # an unknown topic
    assert (
        put(alice, {0: 3, 1: 3, 2: 3, 3: 3}).status_code == 422
    )  # `other` is not a topic of interest
    assert alice.dashboard.get(API).json()["status"] == "pending"


def test_a_completed_survey_cannot_be_changed_but_the_same_answer_is_accepted(
    alice: Actor, db: Session
) -> None:
    seed_topics(db)
    assert put(alice, {1: 5, 2: 3, 3: 1}).status_code == 200
    assert put(alice, {1: 5, 2: 3, 3: 1}).status_code == 200  # a retry of the same request
    changed = put(alice, {1: 1, 2: 3, 3: 5})
    assert changed.status_code == 409 and changed.json()["detail"] == "already_completed"
    assert alice.dashboard.get(API).json()["ratings"] == {"1": 5, "2": 3, "3": 1}
    assert alice.dashboard.post(f"{API}/skip", headers=CSRF).status_code == 409


def test_skipping_is_recorded_and_final(alice: Actor, db: Session) -> None:
    seed_topics(db)
    body = alice.dashboard.post(f"{API}/skip", headers=CSRF).json()
    assert body["status"] == "skipped" and body["ratings"] == {}
    assert alice.dashboard.post(f"{API}/skip", headers=CSRF).status_code == 200  # idempotent
    assert put(alice, {1: 3, 2: 3, 3: 3}).status_code == 409
    db.expire_all()
    assert db.query(SelfReportRating).filter_by(user_id=uuid.UUID(alice.user_id)).count() == 0


def test_writes_need_the_csrf_header(alice: Actor, db: Session) -> None:
    seed_topics(db)
    assert alice.dashboard.put(API, json={"ratings": {"1": 3, "2": 3, "3": 3}}).status_code in (
        400,
        403,
    )
    assert alice.dashboard.post(f"{API}/skip").status_code in (400, 403)
    assert alice.dashboard.get(API).json()["status"] == "pending"


def test_a_user_id_in_the_body_is_not_trusted(alice: Actor, bob: Actor, db: Session) -> None:
    seed_topics(db)
    response = alice.dashboard.put(
        API,
        json={"ratings": {"1": 3, "2": 3, "3": 3}, "user_id": bob.user_id},
        headers=CSRF,
    )
    assert response.status_code == 422  # unknown field refused
    assert bob.dashboard.get(API).json()["status"] == "pending"


def test_users_see_only_their_own_answers(alice: Actor, bob: Actor, db: Session) -> None:
    seed_topics(db)
    put(alice, {1: 5, 2: 4, 3: 3})
    put(bob, {1: 1, 2: 1, 3: 2})
    assert alice.dashboard.get(API).json()["ratings"] == {"1": 5, "2": 4, "3": 3}
    assert bob.dashboard.get(API).json()["ratings"] == {"1": 1, "2": 1, "3": 2}


# ------------------------------------------------------------------ deletion


def test_delete_all_removes_the_survey_and_a_session_delete_keeps_it(
    alice: Actor, bob: Actor, db: Session
) -> None:
    seed_topics(db)
    put(alice, {1: 5, 2: 4, 3: 3})
    put(bob, {1: 2, 2: 2, 3: 2})
    sid = uuid.uuid4()
    db.add(
        MonitoringSession(
            id=sid,
            user_id=uuid.UUID(alice.user_id),
            started_at=T0,
            ended_at=T0 + timedelta(hours=1),
            status="stopped",
            stop_reason="user",
        )
    )
    db.commit()
    assert alice.dashboard.delete(f"/api/v1/sessions/{sid}", headers=CSRF).status_code == 200
    assert alice.dashboard.get(API).json()["status"] == "submitted"  # a session is not the survey

    response = alice.dashboard.delete("/api/v1/me/data", headers=CSRF)
    assert response.status_code == 200, response.text
    assert response.json()["verified"] is True
    db.expire_all()
    for model in (SelfReportRating, SurveyResponse):
        assert db.query(model).filter_by(user_id=uuid.UUID(alice.user_id)).count() == 0
        assert db.query(model).filter_by(user_id=uuid.UUID(bob.user_id)).count() >= 1


# ------------------------------------------------------------------ the report from the database


def add_reels(
    db: Session, user_id: str, topics: list[int], *, prefix: str, dwell: int = 15_000
) -> None:
    sid = uuid.uuid4()
    db.add(
        MonitoringSession(
            id=sid,
            user_id=uuid.UUID(user_id),
            started_at=T0,
            ended_at=T0 + timedelta(hours=2),
            status="stopped",
            stop_reason="user",
        )
    )
    db.commit()
    for i, topic in enumerate(topics):
        code = f"{prefix}{i}"
        for tid in (0, 1, 2, 3):
            db.add(
                TopicAssignment(
                    user_id=uuid.UUID(user_id),
                    reel_shortcode=code,
                    pipeline_version=AV,
                    topic_id=tid,
                    p_fused=1.0 if tid == topic else 0.0,
                )
            )
        db.add(
            ReelExposure(
                event_id=uuid.uuid4(),
                session_id=sid,
                user_id=uuid.UUID(user_id),
                reel_shortcode=code,
                observed_at=T0 + timedelta(minutes=i),
                dwell_ms=dwell,
                video_duration_ms=15_000,
                field_status={},
                adapter_version="t",
            )
        )
    db.commit()


def test_the_report_reads_survey_and_reels_from_the_database(
    alice: Actor, bob: Actor, db: Session
) -> None:
    seed_topics(db)
    add_reels(db, alice.user_id, [1] * 6 + [2] * 3 + [3] * 1, prefix="a")
    add_reels(db, bob.user_id, [3] * 5 + [1] * 2, prefix="b")
    put(alice, {1: 5, 2: 3, 3: 1})
    bob.dashboard.post(f"{API}/skip", headers=CSRF)
    # the survey was answered before the Reels: make `at survey` empty-handed
    db.query(SurveyResponse).update({"completed_at": T0 - timedelta(days=1)})
    db.commit()

    report = collect(
        db,
        assignment_version=AV,
        labelling=None,
        labelling_file=None,
        fp_config=FingerprintConfig(),
        prediction_config=PredictionConfig(),
        seed=0,
    )
    sr = report["self_report"]
    assert sr["users_total"] == 2 and sr["users_with_ratings"] == 1
    mine = next(r for r in sr["per_user"] if r["user"] == alice.user_id)
    assert mine["all_data"]["exposure"] == pytest.approx(1.0)
    assert mine["all_data"]["reels"] == 10
    assert mine["at_survey"]["reels"] == 0  # nothing had been observed when the survey was done
    assert mine["at_survey"]["exposure"] is None
    skipped = next(r for r in sr["per_user"] if r["user"] == bob.user_id)
    assert skipped["status"] == "no_ratings"
    assert report["prediction"]["scored_reels"] == 17
    assert set(report["stability"]["per_user"]) == {alice.user_id, bob.user_id}
    assert report["labelling"]["status"] == "not_run"
    json.dumps(report, allow_nan=False)


def test_the_command_writes_the_markdown_and_json_and_changes_nothing(
    alice: Actor,
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_engine: Engine,
) -> None:
    seed_topics(db)
    add_reels(db, alice.user_id, [1, 2, 3, 1, 2, 1], prefix="c")
    put(alice, {1: 5, 2: 3, 3: 1})
    before = (db.query(ReelExposure).count(), db.query(SelfReportRating).count())

    import app.evaluation.__main__ as cli

    monkeypatch.setattr(cli, "get_engine", lambda: migrated_engine)
    out, js = tmp_path / "r.md", tmp_path / "r.json"
    code = evaluation_main(
        ["report", "--assignment-version", AV, "--out", str(out), "--json", str(js), "--seed", "3"]
    )
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Evaluation report") and "## 4. Fingerprint stability" in text
    assert json.loads(js.read_text(encoding="utf-8"))["seed"] == 3
    db.expire_all()
    assert (db.query(ReelExposure).count(), db.query(SelfReportRating).count()) == before


def test_the_same_data_and_seed_give_the_same_report(alice: Actor, db: Session) -> None:
    seed_topics(db)
    add_reels(db, alice.user_id, [1, 2, 3, 1, 2, 1, 3, 3, 1, 2], prefix="d")
    put(alice, {1: 5, 2: 3, 3: 1})

    def run() -> str:
        return json.dumps(
            collect(
                db,
                assignment_version=AV,
                labelling=None,
                labelling_file=None,
                fp_config=FingerprintConfig(),
                prediction_config=PredictionConfig(),
                seed=1,
            ),
            sort_keys=True,
        )

    assert run() == run()
