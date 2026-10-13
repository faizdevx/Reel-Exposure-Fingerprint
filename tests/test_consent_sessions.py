"""Consent and monitoring-session API tests (spec F1, F2, N7). Need a database."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from support import CSRF, Actor, make_actor

from app.models import Consent
from app.models import Session as MonitoringSession

pytestmark = pytest.mark.db

API = "/api/v1"
POLICY = "2026-10-07.1"


@pytest.fixture
def alice(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "alice@example.test")


@pytest.fixture
def bob(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "bob@example.test")


def grant(client: TestClient, actor: Actor, version: str = POLICY):  # type: ignore[no-untyped-def]
    return client.put(f"{API}/consent", json={"policy_version": version}, headers=actor.bearer)


def start(client: TestClient, actor: Actor):  # type: ignore[no-untyped-def]
    return client.post(f"{API}/sessions", headers=actor.bearer)


def stop(client: TestClient, actor: Actor, session_id: str, reason: str = "user"):  # type: ignore[no-untyped-def]
    return client.patch(
        f"{API}/sessions/{session_id}",
        json={"status": "stopped", "stop_reason": reason},
        headers=actor.bearer,
    )


def session_row(db: Session, session_id: str) -> MonitoringSession:
    db.expire_all()
    row = db.get(MonitoringSession, uuid.UUID(session_id))
    assert row is not None
    return row


def consent_row(db: Session, user_id: str) -> Consent | None:
    db.expire_all()
    return db.get(Consent, uuid.UUID(user_id))


def age_session(db: Session, session_id: str, minutes: int) -> None:
    """Make an active session look `minutes` old."""
    db.execute(
        text("UPDATE sessions SET started_at = now() - make_interval(mins => :m) WHERE id = :id"),
        {"m": minutes, "id": session_id},
    )
    db.commit()


# ------------------------------------------------------------------ PUT /consent


def test_grant_records_policy_version_and_server_timestamp(
    client: TestClient, db: Session, alice: Actor
) -> None:
    before = datetime.now(UTC) - timedelta(seconds=2)
    response = grant(client, alice)
    assert response.status_code == 200
    body = response.json()
    assert body["policy_version"] == POLICY
    assert body["revoked_at"] is None
    granted = datetime.fromisoformat(body["granted_at"])
    assert before <= granted <= datetime.now(UTC) + timedelta(seconds=2)
    row = consent_row(db, alice.user_id)
    assert row is not None and row.policy_version == POLICY and row.revoked_at is None


def test_client_cannot_set_the_grant_time_or_the_user(
    client: TestClient, alice: Actor, bob: Actor
) -> None:
    for extra in (
        {"granted_at": "2020-01-01T00:00:00Z"},
        {"user_id": bob.user_id},
        {"revoked_at": None},
    ):
        response = client.put(
            f"{API}/consent", json={"policy_version": POLICY, **extra}, headers=alice.bearer
        )
        assert response.status_code == 422, extra


@pytest.mark.parametrize("version", ["", "   ", "has space", "semi;colon", "x" * 33, "ünïcode"])
def test_policy_version_must_be_a_short_plain_token(
    client: TestClient, alice: Actor, version: str
) -> None:
    assert grant(client, alice, version).status_code == 422


def test_policy_version_is_required(client: TestClient, alice: Actor) -> None:
    assert client.put(f"{API}/consent", json={}, headers=alice.bearer).status_code == 422


def test_repeating_a_grant_changes_nothing(client: TestClient, db: Session, alice: Actor) -> None:
    first = grant(client, alice).json()
    second = grant(client, alice).json()
    assert first == second
    assert db.query(Consent).count() == 1


def test_a_new_policy_version_is_a_fresh_grant(client: TestClient, alice: Actor) -> None:
    first = grant(client, alice, "v1").json()
    second = grant(client, alice, "v2").json()
    assert second["policy_version"] == "v2"
    assert second["granted_at"] >= first["granted_at"]


def test_regranting_after_revocation_clears_the_revocation(
    client: TestClient, db: Session, alice: Actor
) -> None:
    grant(client, alice)
    assert client.delete(f"{API}/consent", headers=alice.bearer).status_code == 204
    row = consent_row(db, alice.user_id)
    assert row is not None and row.revoked_at is not None
    assert grant(client, alice).json()["revoked_at"] is None
    row = consent_row(db, alice.user_id)
    assert row is not None and row.revoked_at is None


def test_put_consent_needs_a_bearer_token(client: TestClient, alice: Actor) -> None:
    body = {"policy_version": POLICY}
    assert client.put(f"{API}/consent", json=body).status_code == 401
    assert (
        client.put(
            f"{API}/consent", json=body, headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )
    # The dashboard cookie is not an extension credential.
    assert alice.dashboard.put(f"{API}/consent", json=body, headers=CSRF).status_code == 401


def test_consent_is_per_user(client: TestClient, db: Session, alice: Actor, bob: Actor) -> None:
    grant(client, alice)
    assert consent_row(db, bob.user_id) is None
    assert start(client, bob).status_code == 403
    assert start(client, alice).status_code == 201


# ------------------------------------------------------------------ DELETE /consent


def test_revoke_marks_consent_revoked(client: TestClient, db: Session, alice: Actor) -> None:
    grant(client, alice)
    assert client.delete(f"{API}/consent", headers=alice.bearer).status_code == 204
    row = consent_row(db, alice.user_id)
    assert row is not None and row.revoked_at is not None
    assert row.revoked_at >= row.granted_at


def test_revoke_stops_active_sessions_with_reason_consent_revoked(
    client: TestClient, db: Session, alice: Actor
) -> None:
    grant(client, alice)
    session_id = start(client, alice).json()["id"]
    assert client.delete(f"{API}/consent", headers=alice.bearer).status_code == 204
    row = session_row(db, session_id)
    assert row.status == "stopped"
    assert row.stop_reason == "consent_revoked"
    assert row.ended_at is not None and row.ended_at >= row.started_at


def test_revoke_works_from_the_dashboard_cookie_too(
    client: TestClient, db: Session, alice: Actor
) -> None:
    grant(client, alice)
    session_id = start(client, alice).json()["id"]
    assert alice.dashboard.delete(f"{API}/consent").status_code == 403  # CSRF header missing
    assert alice.dashboard.delete(f"{API}/consent", headers=CSRF).status_code == 204
    assert session_row(db, session_id).stop_reason == "consent_revoked"


def test_revoke_is_idempotent_and_keeps_the_first_timestamp(
    client: TestClient, db: Session, alice: Actor
) -> None:
    grant(client, alice)
    client.delete(f"{API}/consent", headers=alice.bearer)
    row = consent_row(db, alice.user_id)
    assert row is not None
    first = row.revoked_at
    assert client.delete(f"{API}/consent", headers=alice.bearer).status_code == 204
    row = consent_row(db, alice.user_id)
    assert row is not None and row.revoked_at == first


def test_revoke_without_any_consent_is_fine(client: TestClient, alice: Actor) -> None:
    assert client.delete(f"{API}/consent", headers=alice.bearer).status_code == 204


def test_revoke_requires_authentication(client: TestClient) -> None:
    assert client.delete(f"{API}/consent").status_code == 401
    assert client.delete(f"{API}/consent", headers=CSRF).status_code == 401


def test_revoke_only_affects_the_caller(
    client: TestClient, db: Session, alice: Actor, bob: Actor
) -> None:
    for actor in (alice, bob):
        grant(client, actor)
    bobs_session = start(client, bob).json()["id"]
    start(client, alice)
    client.delete(f"{API}/consent", headers=alice.bearer)
    bob_consent = consent_row(db, bob.user_id)
    assert bob_consent is not None and bob_consent.revoked_at is None
    assert session_row(db, bobs_session).status == "active"


def test_revoke_leaves_stopped_sessions_alone(
    client: TestClient, db: Session, alice: Actor
) -> None:
    grant(client, alice)
    session_id = start(client, alice).json()["id"]
    stop(client, alice, session_id, "user")
    client.delete(f"{API}/consent", headers=alice.bearer)
    assert session_row(db, session_id).stop_reason == "user"


# ------------------------------------------------------------------ POST /sessions


def test_start_session(client: TestClient, db: Session, alice: Actor) -> None:
    grant(client, alice)
    response = start(client, alice)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "active"
    assert body["ended_at"] is None and body["stop_reason"] is None
    started = datetime.fromisoformat(body["started_at"])
    assert abs(datetime.now(UTC) - started) < timedelta(seconds=5)
    row = session_row(db, body["id"])
    assert str(row.user_id) == alice.user_id


@pytest.mark.parametrize("revoke_first", [False, True])
def test_start_is_refused_without_active_consent(
    client: TestClient, db: Session, alice: Actor, revoke_first: bool
) -> None:
    if revoke_first:  # consent absent vs consent revoked
        grant(client, alice)
        client.delete(f"{API}/consent", headers=alice.bearer)
    response = start(client, alice)
    assert response.status_code == 403
    assert response.json() == {"detail": "consent_required"}
    assert db.query(MonitoringSession).count() == 0


def test_start_needs_a_bearer_token(client: TestClient, alice: Actor) -> None:
    grant(client, alice)
    assert client.post(f"{API}/sessions").status_code == 401
    assert alice.dashboard.post(f"{API}/sessions", headers=CSRF).status_code == 401


def test_start_rejects_a_body_with_a_user_id(client: TestClient, alice: Actor, bob: Actor) -> None:
    grant(client, alice)
    response = client.post(f"{API}/sessions", json={"user_id": bob.user_id}, headers=alice.bearer)
    # The endpoint takes no body at all; whatever is sent cannot change whose session it is.
    assert response.status_code == 201
    assert response.json()["id"]


def test_starting_closes_the_previous_active_session(
    client: TestClient, db: Session, alice: Actor
) -> None:
    grant(client, alice)
    first = start(client, alice).json()["id"]
    second = start(client, alice).json()["id"]
    assert first != second
    old = session_row(db, first)
    assert old.status == "stopped" and old.stop_reason == "browser_restart"
    assert session_row(db, second).status == "active"
    assert db.query(MonitoringSession).filter_by(status="active").count() == 1


def test_starting_closes_an_overdue_session_at_its_cap(
    client: TestClient, db: Session, alice: Actor
) -> None:
    grant(client, alice)
    first = start(client, alice).json()["id"]
    age_session(db, first, minutes=90)
    start(client, alice)
    old = session_row(db, first)
    assert old.stop_reason == "time_cap"
    assert old.ended_at == old.started_at + timedelta(minutes=60)


def test_other_users_sessions_are_not_closed(
    client: TestClient, db: Session, alice: Actor, bob: Actor
) -> None:
    for actor in (alice, bob):
        grant(client, actor)
    bobs = start(client, bob).json()["id"]
    start(client, alice)
    start(client, alice)
    assert session_row(db, bobs).status == "active"


# ------------------------------------------------------------------ PATCH /sessions/{id}


@pytest.mark.parametrize(
    "reason", ["user", "left_surface", "tab_closed", "time_cap", "browser_restart"]
)
def test_stop_records_status_end_and_reason(
    client: TestClient, db: Session, alice: Actor, reason: str
) -> None:
    grant(client, alice)
    session_id = start(client, alice).json()["id"]
    response = stop(client, alice, session_id, reason)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "stopped" and body["stop_reason"] == reason
    assert body["ended_at"] is not None
    row = session_row(db, session_id)
    assert row.ended_at is not None and row.ended_at >= row.started_at


def test_a_client_cannot_report_consent_revoked(client: TestClient, alice: Actor) -> None:
    grant(client, alice)
    session_id = start(client, alice).json()["id"]
    assert stop(client, alice, session_id, "consent_revoked").status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"status": "active", "stop_reason": "user"},
        {"status": "stopped"},
        {"stop_reason": "user"},
        {"status": "stopped", "stop_reason": "bored"},
        {"status": "stopped", "stop_reason": "user", "ended_at": "2020-01-01T00:00:00Z"},
        {"status": "stopped", "stop_reason": "user", "user_id": str(uuid.uuid4())},
    ],
)
def test_invalid_stop_requests_are_rejected(
    client: TestClient, db: Session, alice: Actor, body: dict[str, str]
) -> None:
    grant(client, alice)
    session_id = start(client, alice).json()["id"]
    assert (
        client.patch(f"{API}/sessions/{session_id}", json=body, headers=alice.bearer).status_code
        == 422
    )
    assert session_row(db, session_id).status == "active"


def test_stopping_twice_keeps_the_first_outcome(
    client: TestClient, db: Session, alice: Actor
) -> None:
    grant(client, alice)
    session_id = start(client, alice).json()["id"]
    first = stop(client, alice, session_id, "user").json()
    second = stop(client, alice, session_id, "tab_closed")
    assert second.status_code == 200
    assert second.json() == first
    assert session_row(db, session_id).stop_reason == "user"


def test_a_server_stopped_session_stays_consent_revoked(
    client: TestClient, db: Session, alice: Actor
) -> None:
    grant(client, alice)
    session_id = start(client, alice).json()["id"]
    client.delete(f"{API}/consent", headers=alice.bearer)
    response = stop(client, alice, session_id, "user")  # the extension reports late
    assert response.status_code == 200
    assert response.json()["stop_reason"] == "consent_revoked"


def test_stopping_does_not_need_consent(client: TestClient, db: Session, alice: Actor) -> None:
    grant(client, alice)
    session_id = start(client, alice).json()["id"]
    # Revoke directly in the database so the session is still active but consent is gone.
    db.execute(text("UPDATE consents SET revoked_at = now()"))
    db.commit()
    assert stop(client, alice, session_id, "user").status_code == 200


def test_the_sixty_minute_cap_wins(client: TestClient, db: Session, alice: Actor) -> None:
    grant(client, alice)
    session_id = start(client, alice).json()["id"]
    age_session(db, session_id, minutes=75)
    response = stop(client, alice, session_id, "user")
    assert response.status_code == 200
    row = session_row(db, session_id)
    assert row.stop_reason == "time_cap"
    assert row.ended_at == row.started_at + timedelta(minutes=60)


def test_a_session_just_under_the_cap_keeps_the_reported_reason(
    client: TestClient, db: Session, alice: Actor
) -> None:
    grant(client, alice)
    session_id = start(client, alice).json()["id"]
    age_session(db, session_id, minutes=59)
    stop(client, alice, session_id, "left_surface")
    assert session_row(db, session_id).stop_reason == "left_surface"


def test_patch_needs_a_bearer_token(client: TestClient, alice: Actor) -> None:
    grant(client, alice)
    session_id = start(client, alice).json()["id"]
    body = {"status": "stopped", "stop_reason": "user"}
    assert client.patch(f"{API}/sessions/{session_id}", json=body).status_code == 401
    assert (
        alice.dashboard.patch(f"{API}/sessions/{session_id}", json=body, headers=CSRF).status_code
        == 401
    )


def test_patch_unknown_session_is_404(client: TestClient, alice: Actor) -> None:
    assert stop(client, alice, str(uuid.uuid4())).status_code == 404
    assert (
        client.patch(f"{API}/sessions/not-a-uuid", json={}, headers=alice.bearer).status_code == 422
    )


def test_cannot_stop_another_users_session(
    client: TestClient, db: Session, alice: Actor, bob: Actor
) -> None:
    grant(client, alice)
    alices = start(client, alice).json()["id"]
    response = stop(client, bob, alices)
    assert response.status_code == 404
    assert response.json() == stop(client, bob, str(uuid.uuid4())).json()  # same as a missing id
    assert session_row(db, alices).status == "active"


# ------------------------------------------------------------------ whole flow


def test_full_lifecycle(client: TestClient, db: Session, alice: Actor) -> None:
    assert start(client, alice).status_code == 403  # no consent yet
    assert grant(client, alice).status_code == 200
    first = start(client, alice).json()["id"]
    assert stop(client, alice, first, "user").status_code == 200
    second = start(client, alice).json()["id"]  # a stopped session does not block a new one
    client.delete(f"{API}/consent", headers=alice.bearer)
    assert session_row(db, second).stop_reason == "consent_revoked"
    assert start(client, alice).status_code == 403  # revoked again
    assert [
        r.stop_reason for r in db.query(MonitoringSession).order_by(MonitoringSession.started_at)
    ] == [
        "user",
        "consent_revoked",
    ]
