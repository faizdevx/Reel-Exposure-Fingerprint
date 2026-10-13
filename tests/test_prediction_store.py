"""Predictions in the database, and `GET /predictions/next`. Needs PostgreSQL + pgvector."""

import json
import math
import random
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session
from support import Actor, make_actor

from app.fingerprint.engine import FingerprintConfig, fingerprint_version
from app.fingerprint.store import load_observations
from app.models import (
    Prediction,
    PredictionState,
    ReelExposure,
    Topic,
    TopicAssignment,
    TopicTransition,
)
from app.models import Session as MonitoringSession
from app.prediction.config import PREDICTION_LABEL, PredictionConfig, prediction_version
from app.prediction.engine import replay
from app.prediction.store import latest_state, rebuild_predictions, universe_for
from app.worker.fusion import OTHER_TOPIC_ID

pytestmark = pytest.mark.db

API = "/api/v1"
AV = "pv-pred"
TOPICS = (1, 2, 3)
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
FP = FingerprintConfig()
CFG = PredictionConfig(min_row_observations=3, min_events_to_tune=10)


@pytest.fixture
def alice(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "alice@example.test")


@pytest.fixture
def bob(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "bob@example.test")


def seed_topics(db: Session, *, other: bool = False) -> None:
    for tid, slug in ((1, "cricket"), (2, "tech"), (3, "food")):
        if db.get(Topic, tid) is None:
            db.add(Topic(id=tid, slug=slug, label=slug.title(), taxonomy_version="t1"))
    if other and db.get(Topic, OTHER_TOPIC_ID) is None:
        db.add(Topic(id=OTHER_TOPIC_ID, slug="other", label="Other", taxonomy_version="t1"))
    db.commit()


def assign(db: Session, user_id: str, code: str, topic: int, *, other: bool = False) -> None:
    owner = uuid.UUID(user_id)
    ids = (*TOPICS, OTHER_TOPIC_ID) if other else TOPICS
    for tid in ids:
        p = (1.0 if tid == OTHER_TOPIC_ID else 0.0) if other else 0.8 if tid == topic else 0.1
        db.add(
            TopicAssignment(
                user_id=owner, reel_shortcode=code, pipeline_version=AV, topic_id=tid, p_fused=p
            )
        )
    db.commit()


def make_user_data(
    db: Session, user_id: str, topics: Sequence[int | None], *, offset: int = 0, prefix: str = "r"
) -> list[uuid.UUID]:
    """One session with a Reel per entry; None means a Reel with no assignment."""
    seed_topics(db)
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
    ids = []
    for i, topic in enumerate(topics):
        code = f"{prefix}{i + offset}"
        if topic is not None and db.get(TopicAssignment, (uuid.UUID(user_id), code, AV, 1)) is None:
            assign(db, user_id, code, topic)
        event_id = uuid.uuid4()
        ids.append(event_id)
        db.add(
            ReelExposure(
                event_id=event_id,
                session_id=sid,
                user_id=uuid.UUID(user_id),
                reel_shortcode=code,
                observed_at=T0 + timedelta(minutes=i + offset),
                dwell_ms=9_000,
                video_duration_ms=15_000,
                field_status={},
                adapter_version="t",
            )
        )
    db.commit()
    return ids


def rebuild(db: Session, user_id: str, config: PredictionConfig = CFG, fp: FingerprintConfig = FP):  # type: ignore[no-untyped-def]
    out = rebuild_predictions(
        db, uuid.UUID(user_id), assignment_version=AV, fp_config=fp, config=config
    )
    db.commit()
    return out


def version_of(config: PredictionConfig = CFG) -> str:
    return prediction_version(config, fingerprint_version(FP, AV))


def rows_of(db: Session, user_id: str, version: str | None = None) -> list[Prediction]:
    db.expire_all()
    q = select(Prediction).where(Prediction.user_id == uuid.UUID(user_id))
    if version:
        q = q.where(Prediction.pipeline_version == version)
    return list(db.execute(q.order_by(Prediction.id)).scalars())


def pattern(n: int) -> list[int | None]:
    return [1 if i % 2 == 0 else 2 for i in range(n)]


# ------------------------------------------------------------------ stored before the next event


def test_every_event_gets_one_prediction_per_model_before_it_is_applied(
    db: Session, alice: Actor
) -> None:
    ids = make_user_data(db, alice.user_id, pattern(30))
    out = rebuild(db, alice.user_id)
    assert (out.events, out.prediction_rows) == (30, 150)
    rows = rows_of(db, alice.user_id)
    assert len(rows) == 150
    by_event: dict[uuid.UUID, list[str]] = {}
    for r in rows:
        by_event.setdefault(r.made_before_event, []).append(r.model)
    assert set(by_event) == set(ids)
    assert all(
        sorted(models) == ["decayed_fingerprint", "hybrid", "markov", "most_frequent", "uniform"]
        for models in by_event.values()
    )
    assert {r.pipeline_version for r in rows} == {version_of()}


def test_each_stored_prediction_is_exactly_what_the_earlier_history_predicts(
    db: Session, alice: Actor
) -> None:
    """The stored row of event i equals the prediction made from the Reels before i alone."""
    rng = random.Random(1)
    make_user_data(db, alice.user_id, [rng.choice(TOPICS) for _ in range(40)])
    rebuild(db, alice.user_id)
    observations = sorted(
        load_observations(db, uuid.UUID(alice.user_id), AV, TOPICS),
        key=lambda o: (o.observed_at, str(o.event_id)),
    )
    stored = {(r.made_before_event, r.model): r for r in rows_of(db, alice.user_id)}
    for i in (0, 1, 5, 20, 39):
        expected = replay(observations[:i], (1, 2, 3), CFG, FP).next.models
        for model, dist in expected.items():
            row = stored[(observations[i].event_id, model)]
            assert {int(k): v for k, v in row.distribution.items()} == pytest.approx(
                dist, abs=1e-12
            )


def test_the_observed_topic_hits_and_distributions_are_stored_and_consistent(
    db: Session, alice: Actor
) -> None:
    topics = [1, 2, 1, 3, 2, 2, 1]
    ids = make_user_data(db, alice.user_id, topics)
    rebuild(db, alice.user_id)
    actual = {ids[i]: t for i, t in enumerate(topics)}
    for r in rows_of(db, alice.user_id):
        assert r.actual_topic == actual[r.made_before_event]
        dist = {int(k): v for k, v in r.distribution.items()}
        assert math.fsum(dist.values()) == pytest.approx(1.0)
        order = sorted(dist, key=lambda k: (-dist[k], k))
        assert r.hit_top1 == (order[0] == r.actual_topic)
        assert r.hit_top3 == (r.actual_topic in order[:3])


def test_a_rebuild_leaves_one_set_of_rows_not_two(db: Session, alice: Actor) -> None:
    make_user_data(db, alice.user_id, pattern(25))
    rebuild(db, alice.user_id)
    first = [
        (r.made_before_event, r.model, json.dumps(r.distribution, sort_keys=True))
        for r in rows_of(db, alice.user_id)
    ]
    rebuild(db, alice.user_id)
    second = [
        (r.made_before_event, r.model, json.dumps(r.distribution, sort_keys=True))
        for r in rows_of(db, alice.user_id)
    ]
    assert sorted(first) == sorted(second) and len(second) == 125


def test_a_new_reel_adds_predictions_and_leaves_the_earlier_ones_unchanged(
    db: Session, alice: Actor
) -> None:
    make_user_data(db, alice.user_id, pattern(20))
    rebuild(db, alice.user_id)
    before = {
        (r.made_before_event, r.model): json.dumps(r.distribution, sort_keys=True)
        for r in rows_of(db, alice.user_id)
    }
    seed = db.query(MonitoringSession).filter_by(user_id=uuid.UUID(alice.user_id)).one()
    assign(db, alice.user_id, "extra", 3)
    db.add(
        ReelExposure(
            event_id=uuid.uuid4(),
            session_id=seed.id,
            user_id=uuid.UUID(alice.user_id),
            reel_shortcode="extra",
            observed_at=T0 + timedelta(hours=1),
            dwell_ms=1000,
            video_duration_ms=15_000,
            field_status={},
            adapter_version="t",
        )
    )
    db.commit()
    rebuild(db, alice.user_id)
    after = {
        (r.made_before_event, r.model): json.dumps(r.distribution, sort_keys=True)
        for r in rows_of(db, alice.user_id)
    }
    assert len(after) == len(before) + 5
    assert {k: after[k] for k in before} == before  # the past was not rewritten


# ------------------------------------------------------------------ transitions and state


def test_transition_counts_are_the_consecutive_observed_topics(db: Session, alice: Actor) -> None:
    make_user_data(db, alice.user_id, [1, 2, 2, 3, 1, 2])
    rebuild(db, alice.user_id)
    rows = db.query(TopicTransition).filter_by(user_id=uuid.UUID(alice.user_id)).all()
    assert {(r.from_topic, r.to_topic): r.n for r in rows} == {
        (1, 2): 2,
        (2, 2): 1,
        (2, 3): 1,
        (3, 1): 1,
    }


def test_skipped_reels_are_not_part_of_the_sequence(db: Session, alice: Actor) -> None:
    make_user_data(db, alice.user_id, [1, None, 2, None, None, 3])
    out = rebuild(db, alice.user_id)
    assert out.events == 3 and out.skipped == 3
    rows = db.query(TopicTransition).filter_by(user_id=uuid.UUID(alice.user_id)).all()
    assert {(r.from_topic, r.to_topic) for r in rows} == {(1, 2), (2, 3)}  # across the gaps


def test_the_state_row_holds_the_label_the_next_prediction_and_an_incomplete_evaluation(
    db: Session, alice: Actor
) -> None:
    make_user_data(db, alice.user_id, pattern(12))
    rebuild(db, alice.user_id)
    state = latest_state(db, uuid.UUID(alice.user_id))
    assert state is not None
    doc = state.state
    assert doc["label"] == PREDICTION_LABEL == "Predicted next observed topic"
    assert doc["n_events"] == 12 and doc["prediction_version"] == version_of()
    assert doc["as_of"] == (T0 + timedelta(minutes=11)).isoformat()
    assert set(doc["next"]["models"]) == {
        "uniform",
        "most_frequent",
        "decayed_fingerprint",
        "markov",
        "hybrid",
    }
    for dist in doc["next"]["models"].values():
        assert math.fsum(dist.values()) == pytest.approx(1.0)
    assert doc["evaluation"]["complete"] is False and doc["evaluation"]["comparisons"] is None
    json.dumps(doc, allow_nan=False)  # strict JSON, as the column needs


def test_a_user_with_no_reels_gets_a_state_and_no_predictions(db: Session, alice: Actor) -> None:
    seed_topics(db)
    out = rebuild(db, alice.user_id)
    assert out.events == 0 and rows_of(db, alice.user_id) == []
    state = latest_state(db, uuid.UUID(alice.user_id))
    assert state is not None and state.state["n_events"] == 0


def test_other_is_in_the_universe_only_when_the_version_assigned_it(
    db: Session, alice: Actor
) -> None:
    seed_topics(db, other=True)
    me = uuid.UUID(alice.user_id)
    assert universe_for(db, AV, me) == [1, 2, 3]
    assign(db, alice.user_id, "odd", 1, other=True)
    assert universe_for(db, AV, me) == [0, 1, 2, 3]
    assert universe_for(db, "another-version", me) == [1, 2, 3]


def test_a_reel_assigned_to_other_can_be_the_observed_topic(db: Session, alice: Actor) -> None:
    seed_topics(db, other=True)
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
    assign(db, alice.user_id, "a", 1)
    assign(db, alice.user_id, "b", 0, other=True)
    for i, code in enumerate(("a", "b", "a", "b")):
        db.add(
            ReelExposure(
                event_id=uuid.uuid4(),
                session_id=sid,
                user_id=uuid.UUID(alice.user_id),
                reel_shortcode=code,
                observed_at=T0 + timedelta(minutes=i),
                dwell_ms=1000,
                video_duration_ms=15000,
                field_status={},
                adapter_version="t",
            )
        )
    db.commit()
    # `a` was assigned without topic 0; a version that has `other` expects it everywhere.
    for tid in (OTHER_TOPIC_ID,):
        db.add(
            TopicAssignment(
                user_id=uuid.UUID(alice.user_id),
                reel_shortcode="a",
                pipeline_version=AV,
                topic_id=tid,
                p_fused=0.0,
            )
        )
    db.commit()
    rebuild(db, alice.user_id)
    assert {r.actual_topic for r in rows_of(db, alice.user_id)} == {0, 1}
    assert db.get(Topic, 0) is not None  # the foreign key to `other` is satisfied


# ------------------------------------------------------------------ versions and users


def test_a_changed_setting_is_a_new_version_beside_the_old_one(db: Session, alice: Actor) -> None:
    make_user_data(db, alice.user_id, pattern(20))
    rebuild(db, alice.user_id)
    other = replace(CFG, smoothing=0.5)
    rebuild(db, alice.user_id, other)
    assert len(rows_of(db, alice.user_id, version_of(CFG))) == 100
    assert len(rows_of(db, alice.user_id, version_of(other))) == 100
    assert version_of(CFG) != version_of(other)
    states = db.query(PredictionState).filter_by(user_id=uuid.UUID(alice.user_id)).count()
    assert states == 2


def test_a_rebuild_for_one_user_leaves_another_alone(db: Session, alice: Actor, bob: Actor) -> None:
    make_user_data(db, alice.user_id, pattern(15), prefix="a")
    make_user_data(db, bob.user_id, [3, 3, 2, 1, 1], prefix="b")
    rebuild(db, alice.user_id)
    rebuild(db, bob.user_id)
    assert len(rows_of(db, alice.user_id)) == 75 and len(rows_of(db, bob.user_id)) == 25
    rebuild(db, alice.user_id)
    assert len(rows_of(db, bob.user_id)) == 25
    bob_pairs = {
        (r.from_topic, r.to_topic)
        for r in db.query(TopicTransition).filter_by(user_id=uuid.UUID(bob.user_id))
    }
    assert bob_pairs == {(3, 3), (3, 2), (2, 1), (1, 1)}


# ------------------------------------------------------------------ the endpoint


def get_next(actor: Actor) -> dict[str, Any]:
    response = actor.dashboard.get(f"{API}/predictions/next")
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def test_the_endpoint_needs_a_dashboard_login(client: TestClient, alice: Actor) -> None:
    anonymous = TestClient(client.app, base_url="http://localhost")
    assert anonymous.get(f"{API}/predictions/next").status_code == 401
    assert anonymous.get(f"{API}/predictions/next", headers=alice.bearer).status_code == 401


def test_without_predictions_it_says_so(alice: Actor) -> None:
    body = get_next(alice)
    assert body["available"] is False and body["reason"] == "no_predictions"
    assert body["label"] == "Predicted next observed topic"
    assert body["hybrid"] is None and body["models"] == [] and body["evaluation"] is None


def test_the_answer_has_the_label_the_five_models_and_a_proper_distribution(
    db: Session, alice: Actor
) -> None:
    make_user_data(db, alice.user_id, pattern(40))
    rebuild(db, alice.user_id)
    body = get_next(alice)
    assert body["label"] == "Predicted next observed topic"
    assert "not a prediction of what you like" in body["note"]
    assert body["available"] and body["based_on_reels"] == 40
    assert [m["model"] for m in body["models"]] == [
        "uniform",
        "most_frequent",
        "decayed_fingerprint",
        "markov",
        "hybrid",
    ]
    assert body["hybrid"]["model"] == "hybrid"
    for m in body["models"]:
        total = sum(t["probability"] for t in m["top"]) + m["rest_probability"]
        assert total == pytest.approx(1.0) and len(m["top"]) <= 5
        probs = [t["probability"] for t in m["top"]]
        assert probs == sorted(probs, reverse=True)
    assert body["last_topic"]["label"] in ("Cricket", "Tech")
    assert body["pipeline_version"] == version_of()


def test_the_endpoint_says_when_the_fingerprint_stood_in_for_the_transitions(
    db: Session, alice: Actor
) -> None:
    make_user_data(db, alice.user_id, pattern(4))
    rebuild(db, alice.user_id, replace(CFG, min_row_observations=20))
    body = get_next(alice)
    assert body["backoff"] is True and body["min_row_observations"] == 20
    assert body["row_observations"] is not None and body["row_observations"] < 20


def test_no_success_is_claimed_before_the_evaluation_is_complete(db: Session, alice: Actor) -> None:
    make_user_data(db, alice.user_id, pattern(60))
    rebuild(db, alice.user_id, PredictionConfig())  # the real minimum: 1500
    body = get_next(alice)
    ev = body["evaluation"]
    assert ev["complete"] is False and ev["required_reels"] == 1500 and ev["scored_reels"] == 60
    assert ev["status"].startswith("not complete")
    assert ev["comparisons"] is None and ev["hybrid_beats_required_baselines"] is None
    text = json.dumps(body).lower()
    for word in ("success", "accurate", "beats", "outperform", "preference", "prefer"):
        assert word not in text.replace("hybrid_beats_required_baselines", "")


def test_the_evaluation_has_rolling_and_all_time_figures_for_every_model(
    db: Session, alice: Actor
) -> None:
    make_user_data(db, alice.user_id, pattern(50))
    rebuild(db, alice.user_id, replace(CFG, rolling_window=20))
    models = get_next(alice)["evaluation"]["models"]
    assert set(models) == {"uniform", "most_frequent", "decayed_fingerprint", "markov", "hybrid"}
    assert models["hybrid"]["all_time"]["n"] == 50 and models["hybrid"]["rolling"]["n"] == 20
    assert 0.0 <= models["hybrid"]["rolling"]["top1"] <= 1.0


def test_a_user_sees_only_their_own_prediction(db: Session, alice: Actor, bob: Actor) -> None:
    make_user_data(db, alice.user_id, pattern(30), prefix="a")
    rebuild(db, alice.user_id)
    assert get_next(bob)["available"] is False
    make_user_data(db, bob.user_id, [3] * 10, prefix="b")
    rebuild(db, bob.user_id)
    assert get_next(bob)["based_on_reels"] == 10 and get_next(alice)["based_on_reels"] == 30


def test_reading_costs_a_fixed_number_of_queries_and_never_scans_the_predictions(
    db: Session, alice: Actor, migrated_engine: Engine
) -> None:
    make_user_data(db, alice.user_id, pattern(80))
    rebuild(db, alice.user_id)
    statements: list[str] = []

    def record(_c, _cur, statement, *_r):  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(migrated_engine, "before_cursor_execute", record)
    try:
        get_next(alice)
    finally:
        event.remove(migrated_engine, "before_cursor_execute", record)
    joined = " ".join(statements).lower()
    assert len(statements) <= 5
    assert "from predictions" not in joined and "reel_exposures" not in joined
    assert db.execute(select(func.count()).select_from(Prediction)).scalar_one() == 400
