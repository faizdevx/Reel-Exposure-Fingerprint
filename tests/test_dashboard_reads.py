"""Dashboard read API: GET /sessions and GET /fingerprint/current. Need a database."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session
from support import Actor, make_actor

from app.fingerprint.engine import FingerprintState, weights_document
from app.models import FingerprintSnapshot, Topic
from app.models import Session as MonitoringSession

pytestmark = pytest.mark.db

API = "/api/v1"
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def alice(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "alice@example.test")


@pytest.fixture
def bob(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "bob@example.test")


def _topics(db: Session) -> None:
    for tid, slug in ((1, "cricket"), (2, "tech"), (3, "food")):
        db.add(Topic(id=tid, slug=slug, label=slug.title(), taxonomy_version="t1"))
    db.commit()


def _session(db: Session, user_id: str, hours: int) -> uuid.UUID:
    sid = uuid.uuid4()
    started = T0 + timedelta(hours=hours)
    db.add(
        MonitoringSession(
            id=sid,
            user_id=uuid.UUID(user_id),
            started_at=started,
            ended_at=started + timedelta(minutes=30),
            status="stopped",
            stop_reason="user",
        )
    )
    db.commit()
    return sid


def _snapshot(
    db: Session,
    user_id: str,
    kind: str,
    weights: dict[int, float],
    *,
    minutes: int = 0,
    session_id: uuid.UUID | None = None,
    n_reels: int = 3,
    bucket: str = "session",
    version: str = "fp1-test",
) -> None:
    state = FingerprintState(kind, weights, T0)  # type: ignore[arg-type]
    db.add(
        FingerprintSnapshot(
            user_id=uuid.UUID(user_id),
            kind=kind,
            bucket=bucket,
            taken_at=T0 + timedelta(minutes=minutes),
            session_id=session_id,
            weights=weights_document(state, n_reels),
            n_reels=n_reels,
            pipeline_version=version,
        )
    )
    db.commit()


def test_endpoints_need_a_login(client: TestClient) -> None:
    anonymous = TestClient(client.app, base_url="http://localhost")
    assert anonymous.get(f"{API}/sessions").status_code == 401
    assert anonymous.get(f"{API}/fingerprint/current?kind=exposure").status_code == 401


def test_bearer_token_does_not_open_dashboard_reads(client: TestClient, alice: Actor) -> None:
    bare = TestClient(client.app, base_url="http://localhost")
    assert bare.get(f"{API}/sessions", headers=alice.bearer).status_code == 401


def test_fingerprint_with_no_snapshot_is_empty_not_invented(alice: Actor) -> None:
    for kind in ("exposure", "engagement"):
        body = alice.dashboard.get(f"{API}/fingerprint/current?kind={kind}").json()
        assert body["topics"] == []
        assert body["taken_at"] is None
        assert body["n_reels"] == 0


def test_unknown_kind_is_rejected(alice: Actor) -> None:
    assert alice.dashboard.get(f"{API}/fingerprint/current?kind=mood").status_code == 422
    assert alice.dashboard.get(f"{API}/fingerprint/current").status_code == 422


def test_shares_sum_to_one_and_come_from_raw_weights_sorted_high_to_low(
    db: Session, alice: Actor
) -> None:
    _topics(db)
    # Raw weights, not shares: 1.5 + 0.5 + 3.0 = 5.0.
    _snapshot(db, alice.user_id, "exposure", {1: 1.5, 2: 0.5, 3: 3.0})
    body = alice.dashboard.get(f"{API}/fingerprint/current?kind=exposure").json()
    assert [t["slug"] for t in body["topics"]] == ["food", "cricket", "tech"]
    assert [t["share"] for t in body["topics"]] == pytest.approx([0.6, 0.3, 0.1])
    assert sum(t["share"] for t in body["topics"]) == pytest.approx(1.0, abs=1e-12)
    assert body["pipeline_version"] == "fp1-test"
    assert body["n_reels"] == 3


def test_newest_snapshot_wins_and_kinds_are_separate(db: Session, alice: Actor) -> None:
    _topics(db)
    _snapshot(db, alice.user_id, "exposure", {1: 1.0}, minutes=0)
    _snapshot(db, alice.user_id, "exposure", {2: 1.0}, minutes=10)
    _snapshot(db, alice.user_id, "engagement", {3: 1.0}, minutes=5)
    exposure = alice.dashboard.get(f"{API}/fingerprint/current?kind=exposure").json()
    engagement = alice.dashboard.get(f"{API}/fingerprint/current?kind=engagement").json()
    assert [t["slug"] for t in exposure["topics"]] == ["tech"]
    assert [t["slug"] for t in engagement["topics"]] == ["food"]


def test_all_zero_weights_give_an_empty_list(db: Session, alice: Actor) -> None:
    _topics(db)
    _snapshot(db, alice.user_id, "exposure", {1: 0.0, 2: 0.0})
    assert alice.dashboard.get(f"{API}/fingerprint/current?kind=exposure").json()["topics"] == []


def test_another_users_snapshot_is_never_returned(db: Session, alice: Actor, bob: Actor) -> None:
    _topics(db)
    _snapshot(db, bob.user_id, "exposure", {1: 1.0})
    body = alice.dashboard.get(f"{API}/fingerprint/current?kind=exposure").json()
    assert body["topics"] == [] and body["taken_at"] is None


def test_session_list_is_newest_first_with_counts_from_snapshots(db: Session, alice: Actor) -> None:
    _topics(db)
    old = _session(db, alice.user_id, 0)
    new = _session(db, alice.user_id, 5)
    unprocessed = _session(db, alice.user_id, 9)
    _snapshot(db, alice.user_id, "exposure", {1: 1.0}, session_id=old, n_reels=7)
    _snapshot(db, alice.user_id, "exposure", {1: 1.0}, session_id=new, n_reels=12)
    rows = alice.dashboard.get(f"{API}/sessions").json()
    assert [r["id"] for r in rows] == [str(unprocessed), str(new), str(old)]
    assert [r["n_reels"] for r in rows] == [None, 12, 7]
    assert rows[0]["stop_reason"] == "user"


def test_session_list_is_empty_for_a_new_user_and_hides_other_users(
    db: Session, alice: Actor, bob: Actor
) -> None:
    assert alice.dashboard.get(f"{API}/sessions").json() == []
    _session(db, bob.user_id, 1)
    assert alice.dashboard.get(f"{API}/sessions").json() == []
    assert len(bob.dashboard.get(f"{API}/sessions").json()) == 1


# ---- history (M5)


def _history(actor: Actor, kind: str = "exposure", bucket: str = "day", **params: object) -> dict:  # type: ignore[type-arg]
    query = "&".join(f"{k}={v}" for k, v in {"kind": kind, "bucket": bucket, **params}.items())
    response = actor.dashboard.get(f"{API}/fingerprint/history?{query}")
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def test_history_needs_a_login_and_valid_parameters(client: TestClient, alice: Actor) -> None:
    anonymous = TestClient(client.app, base_url="http://localhost")
    assert anonymous.get(f"{API}/fingerprint/history?kind=exposure&bucket=day").status_code == 401
    for query in (
        "kind=exposure",
        "bucket=day",
        "kind=mood&bucket=day",
        "kind=exposure&bucket=week",
    ):
        assert alice.dashboard.get(f"{API}/fingerprint/history?{query}").status_code == 422
    for limit in (0, 367, "x"):
        response = alice.dashboard.get(
            f"{API}/fingerprint/history?kind=exposure&bucket=day&limit={limit}"
        )
        assert response.status_code == 422


def test_history_with_no_snapshots_is_empty_not_invented(alice: Actor) -> None:
    body = _history(alice)
    assert body["points"] == [] and body["topics"] == [] and body["pipeline_version"] is None


def test_history_is_oldest_first_whatever_the_insert_order(db: Session, alice: Actor) -> None:
    _topics(db)
    for minutes, top in ((300, 3), (100, 1), (200, 2)):  # inserted out of order
        _snapshot(
            db, alice.user_id, "exposure", {top: 1.0}, minutes=minutes, bucket="day", n_reels=top
        )
    points = _history(alice)["points"]
    assert [p["n_reels"] for p in points] == [1, 2, 3]
    times = [p["taken_at"] for p in points]
    assert times == sorted(times)


def test_history_shares_sum_to_one_and_come_from_raw_weights(db: Session, alice: Actor) -> None:
    _topics(db)
    _snapshot(db, alice.user_id, "exposure", {1: 1.5, 2: 0.5, 3: 3.0}, bucket="day")
    body = _history(alice)
    (point,) = body["points"]
    assert point["shares"] == pytest.approx({"1": 0.3, "2": 0.1, "3": 0.6})
    assert sum(point["shares"].values()) == pytest.approx(1.0, abs=1e-12)
    assert [(t["topic_id"], t["slug"], t["label"]) for t in body["topics"]] == [
        (1, "cricket", "Cricket"),
        (2, "tech", "Tech"),
        (3, "food", "Food"),
    ]


def test_history_keeps_day_session_and_kind_apart(db: Session, alice: Actor) -> None:
    _topics(db)
    session = _session(db, alice.user_id, 0)
    _snapshot(db, alice.user_id, "exposure", {1: 1.0}, bucket="day", minutes=10)
    _snapshot(
        db, alice.user_id, "exposure", {2: 1.0}, bucket="session", session_id=session, minutes=5
    )
    _snapshot(db, alice.user_id, "engagement", {3: 1.0}, bucket="day", minutes=10)
    day = _history(alice, "exposure", "day")["points"]
    sess = _history(alice, "exposure", "session")["points"]
    eng = _history(alice, "engagement", "day")["points"]
    assert [list(p["shares"]) for p in day] == [["1"]] and day[0]["session_id"] is None
    assert [list(p["shares"]) for p in sess] == [["2"]] and sess[0]["session_id"] == str(session)
    assert [list(p["shares"]) for p in eng] == [["3"]]


def test_history_returns_the_newest_limit_points_oldest_first(db: Session, alice: Actor) -> None:
    _topics(db)
    for day in range(10):
        _snapshot(
            db, alice.user_id, "exposure", {1: 1.0}, bucket="day", minutes=day * 1440, n_reels=day
        )
    points = _history(alice, limit=4)["points"]
    assert [p["n_reels"] for p in points] == [6, 7, 8, 9]


def test_history_uses_one_pipeline_version_the_newest(db: Session, alice: Actor) -> None:
    _topics(db)
    _snapshot(db, alice.user_id, "exposure", {1: 1.0}, bucket="day", minutes=10, version="fp1-old")
    _snapshot(db, alice.user_id, "exposure", {2: 1.0}, bucket="day", minutes=20, version="fp1-old")
    _snapshot(db, alice.user_id, "exposure", {3: 1.0}, bucket="day", minutes=15, version="fp1-new")
    _snapshot(db, alice.user_id, "exposure", {3: 1.0}, bucket="day", minutes=30, version="fp1-new")
    body = _history(alice)
    assert body["pipeline_version"] == "fp1-new"
    assert len(body["points"]) == 2  # never a chart that mixes two ways of computing


def test_history_never_shows_another_users_snapshots(db: Session, alice: Actor, bob: Actor) -> None:
    _topics(db)
    _snapshot(db, bob.user_id, "exposure", {1: 1.0}, bucket="day")
    assert _history(alice)["points"] == []
    assert len(_history(bob)["points"]) == 1


def test_history_reads_only_snapshots_in_a_constant_number_of_queries(
    db: Session, alice: Actor, migrated_engine: Engine
) -> None:
    """The cost of a read follows the number of snapshots asked for, not the Reels watched."""
    _topics(db)
    for day in range(120):
        _snapshot(db, alice.user_id, "exposure", {1: 0.6, 2: 0.4}, bucket="day", minutes=day * 1440)
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_rest):  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(migrated_engine, "before_cursor_execute", record)
    try:
        small = _history(alice, limit=10)
        small_count = len(statements)
        statements.clear()
        large = _history(alice, limit=120)
        large_count = len(statements)
    finally:
        event.remove(migrated_engine, "before_cursor_execute", record)

    assert len(small["points"]) == 10 and len(large["points"]) == 120
    assert small_count == large_count  # same number of queries however many points
    assert large_count <= 8  # login check, version, points, topics
    joined = " ".join(statements).lower()
    assert "reel_exposures" not in joined and "topic_assignments" not in joined


def test_history_points_carry_the_reels_of_their_own_day_and_the_running_total(
    db: Session, alice: Actor
) -> None:
    _topics(db)
    _snapshot(db, alice.user_id, "exposure", {1: 1.0}, bucket="day", minutes=0, n_reels=5)
    body = _history(alice)
    (point,) = body["points"]
    assert point["n_reels"] == 5 and point["reels_in_bucket"] == 5
