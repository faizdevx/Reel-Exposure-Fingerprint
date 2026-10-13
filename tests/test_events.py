"""Event ingest tests: POST /api/v1/events (spec F8, N5, §8 ingest rules). Need a database."""

import json
import logging
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from support import CSRF, Actor, make_actor

from app.models import Job, ReelExposure
from app.routers.events import EXPOSURE_JOB, MAX_BATCH, MAX_BODY_BYTES

pytestmark = pytest.mark.db

API = "/api/v1"
POLICY = "2026-10-07.1"
PRIVATE_CAPTION = "a private caption that must never be echoed"


@pytest.fixture
def alice(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "alice@example.test")


@pytest.fixture
def bob(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "bob@example.test")


def grant(client: TestClient, actor: Actor) -> None:
    response = client.put(f"{API}/consent", json={"policy_version": POLICY}, headers=actor.bearer)
    assert response.status_code == 200


def start(client: TestClient, actor: Actor) -> str:
    response = client.post(f"{API}/sessions", headers=actor.bearer)
    assert response.status_code == 201, response.text
    session_id: str = response.json()["id"]
    return session_id


def stop(client: TestClient, actor: Actor, session_id: str, reason: str = "user") -> None:
    response = client.patch(
        f"{API}/sessions/{session_id}",
        json={"status": "stopped", "stop_reason": reason},
        headers=actor.bearer,
    )
    assert response.status_code == 200


@pytest.fixture
def running(client: TestClient, alice: Actor) -> str:
    """Alice has consented and has a running session; returns its id."""
    grant(client, alice)
    return start(client, alice)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def event(session_id: str, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "session_id": session_id,
        "reel_shortcode": "Cabc123",
        "observed_at": iso(datetime.now(UTC)),
        "dwell_ms": 8420,
        "video_duration_ms": 15000,
        "video_duration_status": "available",
        "creator_handle": {"value": "somecreator", "status": "available"},
        "caption": {"value": PRIVATE_CAPTION, "status": "available"},
        "caption_truncated": False,
        "hashtags": {"value": ["ai", "coding"], "status": "available"},
        "frame_count": 0,
        "adapter_version": "ig-web-2026.10.1",
    }
    body.update(over)
    return body


def post(client: TestClient, actor: Actor, events: list[dict[str, Any]]) -> Any:
    return client.post(f"{API}/events", json={"events": events}, headers=actor.bearer)


def results(response: Any) -> list[tuple[str, str | None]]:
    assert response.status_code == 200, response.text
    return [(r["status"], r["reason"]) for r in response.json()["results"]]


def exposure_count(db: Session) -> int:
    db.expire_all()
    return db.execute(select(func.count()).select_from(ReelExposure)).scalar_one()


def job_ids(db: Session) -> list[str]:
    db.expire_all()
    rows = db.execute(select(Job.payload).where(Job.kind == EXPOSURE_JOB)).scalars()
    return sorted(p["event_id"] for p in rows)


def age_session(db: Session, session_id: str, minutes: int) -> None:
    db.execute(
        text(
            "UPDATE sessions SET started_at = started_at - make_interval(mins => :m) WHERE id = :id"
        ),
        {"m": minutes, "id": session_id},
    )
    db.commit()


# ------------------------------------------------------------------ happy path


def test_stores_a_batch_maps_fields_and_enqueues_one_job_per_event(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    full = event(running)
    sparse = event(
        running,
        video_duration_ms=None,
        video_duration_status="unavailable",
        creator_handle={"value": None, "status": "unavailable"},
        caption={"value": None, "status": "unavailable"},
        caption_truncated=True,
        hashtags={"value": None, "status": "unavailable"},
        frame_count=3,
    )
    response = post(client, alice, [full, sparse])
    assert results(response) == [("stored", None), ("stored", None)]
    assert [r["event_id"] for r in response.json()["results"]] == [
        full["event_id"],
        sparse["event_id"],
    ]

    db.expire_all()
    row = db.get(ReelExposure, uuid.UUID(full["event_id"]))
    assert row is not None
    assert str(row.user_id) == alice.user_id  # from the token, never the body
    assert str(row.session_id) == running
    assert (row.reel_shortcode, row.dwell_ms, row.video_duration_ms) == ("Cabc123", 8420, 15000)
    assert (row.creator_handle, row.caption, row.hashtags) == (
        "somecreator",
        PRIVATE_CAPTION,
        ["ai", "coding"],
    )
    assert row.observed_at == datetime.fromisoformat(full["observed_at"])
    assert row.adapter_version == "ig-web-2026.10.1"
    assert row.process_status == "pending"
    assert row.field_status == {
        "reel_shortcode": "available",
        "observed_at": "available",
        "creator_handle": "available",
        "caption": "available",
        "hashtags": "available",
        "video_duration_ms": "available",
        "caption_truncated": False,
        "frame_count": 0,
    }

    empty = db.get(ReelExposure, uuid.UUID(sparse["event_id"]))
    assert empty is not None
    # Unavailable fields are NULL, never a made-up default.
    assert (empty.creator_handle, empty.caption, empty.hashtags, empty.video_duration_ms) == (
        None,
        None,
        None,
        None,
    )
    assert empty.field_status["caption"] == "unavailable"
    assert empty.field_status["video_duration_ms"] == "unavailable"
    assert empty.field_status["caption_truncated"] is True
    assert empty.field_status["frame_count"] == 3

    assert job_ids(db) == sorted([full["event_id"], sparse["event_id"]])
    jobs = db.execute(select(Job)).scalars().all()
    # The job payload holds an id only: no caption or other content.
    assert all(set(j.payload) == {"event_id"} and j.status == "queued" for j in jobs)


def test_available_empty_hashtag_list_is_kept_as_an_empty_list(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    e = event(running, hashtags={"value": [], "status": "available"})
    assert results(post(client, alice, [e])) == [("stored", None)]
    row = db.get(ReelExposure, uuid.UUID(e["event_id"]))
    assert row is not None and row.hashtags == []


def test_accepts_a_full_batch_of_fifty(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    batch = [event(running) for _ in range(MAX_BATCH)]
    assert results(post(client, alice, batch)) == [("stored", None)] * MAX_BATCH
    assert exposure_count(db) == MAX_BATCH
    assert len(job_ids(db)) == MAX_BATCH


# ------------------------------------------------------------------ idempotency


def test_replaying_the_same_event_creates_no_duplicate_row_or_job(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    e = event(running)
    assert results(post(client, alice, [e])) == [("stored", None)]
    for _ in range(3):
        assert results(post(client, alice, [e])) == [("duplicate", None)]
    assert exposure_count(db) == 1
    assert job_ids(db) == [e["event_id"]]


def test_a_replay_with_changed_content_keeps_the_first_version(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    e = event(running)
    post(client, alice, [e])
    assert results(post(client, alice, [{**e, "dwell_ms": 1}])) == [("duplicate", None)]
    row = db.get(ReelExposure, uuid.UUID(e["event_id"]))
    assert row is not None and row.dwell_ms == 8420


def test_the_same_event_twice_in_one_batch_is_stored_once(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    e = event(running)
    assert results(post(client, alice, [e, e])) == [("stored", None), ("duplicate", None)]
    assert exposure_count(db) == 1
    assert len(job_ids(db)) == 1


def test_retries_after_a_two_minute_outage_lose_nothing_and_duplicate_nothing(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    """Server half of the N5 acceptance check (the client half is extension/test/delivery.test.ts).

    A client that lost its connection resends whatever it still holds: batches that partly reached
    the server before the outage, the same batch several times, and overlapping batches. Every
    event ends up stored exactly once with exactly one job.
    """
    start_at = datetime.now(UTC) - timedelta(minutes=3)
    age_session(db, running, 4)
    # One event every 4 s across a 3 minute stretch that includes the 2 minute outage.
    events = [
        event(running, observed_at=iso(start_at + timedelta(seconds=4 * i))) for i in range(45)
    ]

    # Before the outage: the first batch arrived, but the client never saw the reply.
    post(client, alice, events[:10])
    # During the outage nothing arrives. Afterwards the client resends all it holds, in batches,
    # and a second worker run (or a retry after a lost reply) sends overlapping batches again.
    for chunk in (events[0:20], events[20:45], events[10:30], events[0:45][:MAX_BATCH]):
        response = post(client, alice, chunk)
        assert all(status in ("stored", "duplicate") for status, _ in results(response))

    assert exposure_count(db) == len(events)
    assert job_ids(db) == sorted(e["event_id"] for e in events)
    stored = db.execute(select(ReelExposure.event_id)).scalars().all()
    assert sorted(str(i) for i in stored) == sorted(e["event_id"] for e in events)


def test_concurrent_identical_batches_store_each_event_once(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    batch = [event(running) for _ in range(20)]
    errors: list[BaseException] = []
    outcomes: list[list[tuple[str, str | None]]] = []

    def send() -> None:
        try:
            with TestClient(client.app, base_url="http://localhost") as other:
                outcomes.append(results(post(other, alice, batch)))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=send) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert exposure_count(db) == 20
    assert len(job_ids(db)) == 20
    stored = sum(status == "stored" for outcome in outcomes for status, _ in outcome)
    assert stored == 20  # every event was "stored" by exactly one request


# ------------------------------------------------------------------ authentication and consent


def test_unauthenticated_requests_get_401_and_store_nothing(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    body = {"events": [event(running)]}
    assert client.post(f"{API}/events", json=body).status_code == 401
    bad = {"Authorization": "Bearer not-a-real-token"}
    assert client.post(f"{API}/events", json=body, headers=bad).status_code == 401
    # The dashboard cookie is not an extension identity.
    assert alice.dashboard.post(f"{API}/events", json=body, headers=CSRF).status_code == 401
    assert exposure_count(db) == 0


def test_unauthenticated_oversized_request_is_401_not_413(client: TestClient) -> None:
    response = client.post(f"{API}/events", content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 401


def test_without_consent_events_are_refused(client: TestClient, db: Session, alice: Actor) -> None:
    response = post(client, alice, [event(str(uuid.uuid4()))])
    assert response.status_code == 403
    assert response.json()["detail"] == "consent_required"
    assert exposure_count(db) == 0


def test_after_revocation_events_of_the_old_session_are_refused(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    assert client.delete(f"{API}/consent", headers=alice.bearer).status_code == 204
    response = post(client, alice, [event(running)])
    assert response.status_code == 403
    assert response.json()["detail"] == "consent_required"
    assert exposure_count(db) == 0
    assert job_ids(db) == []


def test_after_re_granting_consent_the_revoked_session_stays_closed(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    assert client.delete(f"{API}/consent", headers=alice.bearer).status_code == 204
    grant(client, alice)
    assert results(post(client, alice, [event(running)])) == [("rejected", "session_inactive")]


# ------------------------------------------------------------------ sessions


def test_events_for_another_users_session_are_refused_like_unknown_ones(
    client: TestClient, db: Session, alice: Actor, bob: Actor, running: str
) -> None:
    grant(client, bob)
    start(client, bob)
    stolen = post(client, bob, [event(running)])
    unknown = post(client, bob, [event(str(uuid.uuid4()))])
    assert results(stolen) == [("rejected", "session_not_found")]
    assert results(unknown) == results(stolen)  # no hint that Alice's session exists
    assert exposure_count(db) == 0


def test_another_user_cannot_overwrite_or_claim_an_event_id(
    client: TestClient, db: Session, alice: Actor, bob: Actor, running: str
) -> None:
    e = event(running)
    post(client, alice, [e])
    grant(client, bob)
    bob_session = start(client, bob)
    response = post(client, bob, [{**e, "session_id": bob_session, "dwell_ms": 1}])
    assert results(response) == [("rejected", "event_id_conflict")]
    db.expire_all()
    row = db.get(ReelExposure, uuid.UUID(e["event_id"]))
    assert row is not None
    assert (str(row.user_id), row.dwell_ms) == (alice.user_id, 8420)
    assert len(job_ids(db)) == 1


def test_a_body_user_id_is_refused(client: TestClient, alice: Actor, running: str) -> None:
    response = post(client, alice, [event(running, user_id=alice.user_id)])
    assert response.status_code == 422


def test_events_for_a_stopped_session_are_refused(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    stop(client, alice, running)
    assert results(post(client, alice, [event(running)])) == [("rejected", "session_inactive")]
    assert exposure_count(db) == 0


def test_events_for_a_session_closed_by_a_newer_one_are_refused(
    client: TestClient, alice: Actor, running: str
) -> None:
    newer = start(client, alice)  # closes `running` as browser_restart
    response = post(client, alice, [event(running), event(newer)])
    assert results(response) == [("rejected", "session_inactive"), ("stored", None)]


def test_expired_session_stopped_at_the_cap_refuses_events(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    age_session(db, running, 61)
    stop(client, alice, running, reason="user")  # the server ends it at the cap: time_cap
    in_window = iso(datetime.now(UTC) - timedelta(minutes=30))
    response = post(client, alice, [event(running, observed_at=in_window)])
    assert results(response) == [("rejected", "session_inactive")]


def test_never_stopped_session_past_its_cap_accepts_only_events_from_its_window(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    """A browser that died never sent a stop. Its queued events from the session still count."""
    age_session(db, running, 120)  # started 2 h ago, cap was 1 h ago
    now = datetime.now(UTC)
    inside = event(running, observed_at=iso(now - timedelta(minutes=70)))
    near_cap = event(running, observed_at=iso(now - timedelta(minutes=57)))  # cap + 3 min
    after = event(running, observed_at=iso(now - timedelta(minutes=50)))  # cap + 10 min
    assert results(post(client, alice, [inside, near_cap, after])) == [
        ("stored", None),
        ("stored", None),
        ("rejected", "observed_at_out_of_window"),
    ]


def test_mixed_batch_reports_each_event_in_order(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    ok = event(running)
    post(client, alice, [ok])
    batch = [
        event(running),
        ok,
        event(str(uuid.uuid4())),
        event(running, observed_at=iso(datetime.now(UTC) + timedelta(minutes=10))),
    ]
    response = post(client, alice, batch)
    assert results(response) == [
        ("stored", None),
        ("duplicate", None),
        ("rejected", "session_not_found"),
        ("rejected", "observed_at_out_of_window"),
    ]
    assert [r["event_id"] for r in response.json()["results"]] == [e["event_id"] for e in batch]
    assert exposure_count(db) == 2


# ------------------------------------------------------------------ timestamps


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(minutes=-4, seconds=-50), ("stored", None)),  # just inside the 5 min slack
        (timedelta(minutes=-5, seconds=-30), ("rejected", "observed_at_out_of_window")),
        (timedelta(minutes=-60), ("rejected", "observed_at_out_of_window")),
    ],
)
def test_observed_at_before_the_session_start(
    client: TestClient,
    db: Session,
    alice: Actor,
    running: str,
    offset: timedelta,
    expected: tuple[str, str | None],
) -> None:
    db.expire_all()
    started = db.execute(
        text("SELECT started_at FROM sessions WHERE id = :id"), {"id": running}
    ).scalar_one()
    response = post(client, alice, [event(running, observed_at=iso(started + offset))])
    assert results(response) == [expected]


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(minutes=4), ("stored", None)),  # clock skew within the slack
        (timedelta(minutes=6), ("rejected", "observed_at_out_of_window")),
        (timedelta(days=365), ("rejected", "observed_at_out_of_window")),
    ],
)
def test_observed_at_in_the_future(
    client: TestClient,
    alice: Actor,
    running: str,
    offset: timedelta,
    expected: tuple[str, str | None],
) -> None:
    response = post(client, alice, [event(running, observed_at=iso(datetime.now(UTC) + offset))])
    assert results(response) == [expected]


@pytest.mark.parametrize(
    "observed_at",
    ["2026-10-07T12:00:00", "yesterday", "", 1759838400000, "2026-13-40T99:00:00Z"],
)
def test_malformed_or_naive_timestamps_are_422(
    client: TestClient, alice: Actor, running: str, observed_at: object
) -> None:
    assert post(client, alice, [event(running, observed_at=observed_at)]).status_code == 422


def test_timestamps_with_offsets_are_compared_as_instants(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    e = event(running, observed_at=ist.isoformat())
    assert e["observed_at"].endswith("+05:30")
    assert results(post(client, alice, [e])) == [("stored", None)]


# ------------------------------------------------------------------ size limits


def test_oversized_body_is_413_and_stores_nothing(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    huge = event(running, caption={"value": "x" * MAX_BODY_BYTES, "status": "available"})
    response = post(client, alice, [huge])
    assert response.status_code == 413
    assert PRIVATE_CAPTION not in response.text
    assert exposure_count(db) == 0


def test_oversized_streamed_body_without_content_length_is_413(
    client: TestClient, alice: Actor, running: str
) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(MAX_BODY_BYTES // 1024 + 2):
            yield b" " * 1024

    response = client.post(
        f"{API}/events",
        content=chunks(),
        headers={**alice.bearer, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_body_at_the_limit_is_accepted(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    e = event(running)
    raw = json.dumps({"events": [e]}).encode()
    padded = raw + b" " * (MAX_BODY_BYTES - len(raw))  # JSON allows trailing whitespace
    assert len(padded) == MAX_BODY_BYTES
    response = client.post(
        f"{API}/events",
        content=padded,
        headers={**alice.bearer, "Content-Type": "application/json"},
    )
    assert results(response) == [("stored", None)]


def test_more_than_fifty_events_is_422(
    client: TestClient, db: Session, alice: Actor, running: str
) -> None:
    response = post(client, alice, [event(running) for _ in range(MAX_BATCH + 1)])
    assert response.status_code == 422
    assert exposure_count(db) == 0


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    "change",
    [
        {"event_id": "not-a-uuid"},
        {"event_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8"},  # a version 1 UUID
        {"session_id": None},
        {"reel_shortcode": ""},
        {"reel_shortcode": "../etc"},
        {"dwell_ms": -1},
        {"dwell_ms": "8420"},  # strict: no strings for numbers
        {"dwell_ms": 1.5},
        {"dwell_ms": 24 * 60 * 60 * 1000 + 1},
        {"video_duration_ms": None},  # status says available
        {"video_duration_ms": 15000, "video_duration_status": "unavailable"},
        {"video_duration_status": "maybe"},
        {"creator_handle": {"value": None, "status": "available"}},
        {"creator_handle": {"value": "x", "status": "unavailable"}},
        {"creator_handle": {"value": "has space", "status": "available"}},
        {"creator_handle": "somecreator"},
        {"caption": {"value": "nul\x00byte", "status": "available"}},
        {"caption": {"value": "x", "status": "available", "extra": 1}},
        {"caption_truncated": "false"},
        {"hashtags": {"value": "ai", "status": "available"}},
        {"hashtags": {"value": [""], "status": "available"}},
        {"frame_count": 6},
        {"adapter_version": ""},
        {"unexpected": True},
    ],
)
def test_malformed_events_are_422_and_nothing_of_the_batch_is_stored(
    client: TestClient, db: Session, alice: Actor, running: str, change: dict[str, Any]
) -> None:
    good = event(running)
    response = post(client, alice, [good, {**event(running), **change}])
    assert response.status_code == 422
    assert exposure_count(db) == 0


@pytest.mark.parametrize("field", ["event_id", "session_id", "observed_at", "caption", "hashtags"])
def test_missing_fields_are_422(client: TestClient, alice: Actor, running: str, field: str) -> None:
    e = event(running)
    del e[field]
    assert post(client, alice, [e]).status_code == 422


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'{"events": []}', b'{"events": {}}', b'{"event": []}'],
)
def test_malformed_envelopes_are_422(client: TestClient, alice: Actor, body: bytes) -> None:
    grant(client, alice)
    response = client.post(
        f"{API}/events", content=body, headers={**alice.bearer, "Content-Type": "application/json"}
    )
    assert response.status_code == 422


def test_validation_errors_and_logs_never_echo_content(
    client: TestClient, alice: Actor, running: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    bad = event(running, dwell_ms="lots", caption={"value": PRIVATE_CAPTION, "status": "nope"})
    response = post(client, alice, [bad])
    assert response.status_code == 422
    assert PRIVATE_CAPTION not in response.text
    assert "lots" not in response.text
    assert all(set(item) == {"loc", "type"} for item in response.json()["detail"])

    ok = post(client, alice, [event(running)])
    assert ok.status_code == 200
    assert PRIVATE_CAPTION not in caplog.text
    assert "somecreator" not in caplog.text
