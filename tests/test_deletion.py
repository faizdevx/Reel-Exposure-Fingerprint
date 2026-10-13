"""Deletion verification: DELETE /sessions/{id} and DELETE /me/data. Needs a database.

The checks that matter do not trust the endpoint's own report. After each deletion the tests count
rows in EVERY table of the schema and walk the frame directory, and they break the deletion on
purpose (a step that quietly does nothing) to show that the endpoint refuses to call it done.
"""

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from support import CSRF, Actor, make_actor
from test_events import event, grant, iso, start, stop

from app.config import get_settings
from app.fingerprint.engine import FingerprintConfig
from app.fingerprint.store import rebuild_user
from app.frames import FrameStore
from app.models import (
    Base,
    ContentFeature,
    FingerprintSnapshot,
    Job,
    Prediction,
    PredictionState,
    ReelExposure,
    Topic,
    TopicAssignment,
    TopicTransition,
    User,
)
from app.models import Session as MonitoringSession
from app.prediction.config import PredictionConfig
from app.prediction.store import rebuild_predictions

pytestmark = pytest.mark.db

API = "/api/v1"
AV = "pv-deletion"
TOPICS = (1, 2, 3)

# Every table of the schema, and how a deletion has to treat it. A table added later that is not
# listed here fails `test_every_table_is_accounted_for`, so deletion cannot silently miss it.
USER_KEYED = {
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
    "content_features",
    "topic_assignments",
    "user_sessions",
}
SPECIAL = {
    "users": "the account itself",
    "invites": "used_by names the user",
    "jobs": "no user column: payload event_id names the exposure",
    "topics": "the taxonomy: global, not personal",
}


def jpeg(tag: int) -> bytes:
    return bytes([0xFF, 0xD8, 0xFF, 0xE0]) + bytes([tag % 256]) * 400 + bytes([0xFF, 0xD9])


@pytest.fixture
def frame_dir(client: TestClient, tmp_path: Path) -> Path:
    root = tmp_path / "frames"
    settings = get_settings().model_copy(update={"frame_store_dir": root})
    client.app.dependency_overrides[get_settings] = lambda: settings  # type: ignore[attr-defined]
    return root


@pytest.fixture
def alice(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "alice@example.test")


@pytest.fixture
def bob(client: TestClient, db: Session) -> Actor:
    return make_actor(client, db, "bob@example.test")


# ------------------------------------------------------------------ building a user with everything


class World:
    """What a user accumulated: ids to look for after the deletion."""

    def __init__(self) -> None:
        self.sessions: list[str] = []
        self.events: dict[str, list[str]] = {}  # session id -> event ids
        self.shortcodes: dict[str, str] = {}  # event id -> shortcode

    @property
    def all_events(self) -> list[str]:
        return [e for events in self.events.values() for e in events]

    @property
    def shortcodes_list(self) -> list[str]:
        return list(self.shortcodes.values())


def build_world(
    client: TestClient,
    db: Session,
    actor: Actor,
    frame_dir: Path,
    *,
    sessions: int = 2,
    reels_per_session: int = 3,
    prefix: str = "r",
) -> World:
    """Consent, sessions, events (with their jobs), frames, cache rows, snapshots, predictions."""
    world = World()
    grant(client, actor)
    for topic_id, slug in ((1, "cricket"), (2, "tech"), (3, "food")):
        if db.get(Topic, topic_id) is None:
            db.add(Topic(id=topic_id, slug=slug, label=slug, taxonomy_version="t1"))
    db.commit()

    for s in range(sessions):
        session_id = start(client, actor)
        world.sessions.append(session_id)
        world.events[session_id] = []
        batch = []
        for i in range(reels_per_session):
            code = f"{prefix}{s}x{i}"
            body = event(
                session_id,
                reel_shortcode=code,
                frame_count=2,
                dwell_ms=5000 + 1000 * i,
                caption={"value": f"caption of {code}", "status": "available"},
            )
            batch.append(body)
            world.events[session_id].append(body["event_id"])
            world.shortcodes[body["event_id"]] = code
        response = client.post(f"{API}/events", json={"events": batch}, headers=actor.bearer)
        assert [r["status"] for r in response.json()["results"]] == ["stored"] * reels_per_session
        for body in batch:
            frames = [("frames", (f"f{k}.jpg", jpeg(k + s), "image/jpeg")) for k in range(2)]
            upload = client.post(
                f"{API}/events/{body['event_id']}/frames", files=frames, headers=actor.bearer
            )
            assert upload.status_code == 200, upload.text
            code = body["reel_shortcode"]
            owner = uuid.UUID(actor.user_id)
            if db.get(ContentFeature, (owner, code, AV)) is None:
                db.add(
                    ContentFeature(
                        user_id=owner,
                        reel_shortcode=code,
                        pipeline_version=AV,
                        modalities=["text"],
                        ocr_text="t",
                    )
                )
                for topic_id in TOPICS:
                    db.add(
                        TopicAssignment(
                            user_id=owner,
                            reel_shortcode=code,
                            pipeline_version=AV,
                            topic_id=topic_id,
                            p_fused=0.8 if topic_id == 1 + (sum(map(ord, code)) % 3) else 0.1,
                        )
                    )
                db.commit()
        if s < sessions - 1:
            stop(client, actor, session_id)

    uid = uuid.UUID(actor.user_id)
    rebuild_user(db, uid, assignment_version=AV, topic_ids=TOPICS, config=FingerprintConfig())
    rebuild_predictions(
        db,
        uid,
        assignment_version=AV,
        fp_config=FingerprintConfig(),
        config=PredictionConfig(min_row_observations=2),
    )
    db.commit()
    return world


# ------------------------------------------------------------------ counting everything


def count_user_rows(db: Session, user_id: str, events: list[str]) -> dict[str, int]:
    """Rows that belong to the user, in EVERY table, by independent queries."""
    db.rollback()
    uid = uuid.UUID(user_id)
    counts: dict[str, int] = {}
    for name in sorted(USER_KEYED):
        table = Base.metadata.tables[name]
        counts[name] = db.execute(
            select(func.count()).select_from(table).where(table.c.user_id == uid)
        ).scalar_one()
    counts["users"] = db.execute(
        select(func.count()).select_from(User).where(User.id == uid)
    ).scalar_one()
    counts["invites"] = db.execute(
        text("SELECT count(*) FROM invites WHERE used_by = :u"), {"u": uid}
    ).scalar_one()
    counts["jobs"] = (
        db.execute(
            text("SELECT count(*) FROM jobs WHERE payload->>'event_id' = ANY(:e)"), {"e": events}
        ).scalar_one()
        if events
        else 0
    )
    return counts


def count_cache_for(db: Session, shortcodes: list[str]) -> int:
    db.rollback()
    return int(
        db.execute(
            text(
                "SELECT (SELECT count(*) FROM content_features WHERE reel_shortcode = ANY(:s))"
                " + (SELECT count(*) FROM topic_assignments WHERE reel_shortcode = ANY(:s))"
            ),
            {"s": shortcodes},
        ).scalar_one()
    )


def files_under(root: Path) -> list[Path]:
    return [Path(dirpath) / name for dirpath, _dirs, names in os.walk(root) for name in names]


def delete_session(actor: Actor, session_id: str) -> Any:
    return actor.dashboard.delete(f"{API}/sessions/{session_id}", headers=CSRF)


def delete_all(actor: Actor) -> Any:
    return actor.dashboard.delete(f"{API}/me/data", headers=CSRF)


# ------------------------------------------------------------------ the schema is covered


def test_every_table_is_accounted_for() -> None:
    """A new table must be classified here, so that deletion cannot silently miss it."""
    known = USER_KEYED | set(SPECIAL)
    assert set(Base.metadata.tables) == known, set(Base.metadata.tables) ^ known
    for name in USER_KEYED:
        assert "user_id" in Base.metadata.tables[name].c  # the independent count relies on it


# ------------------------------------------------------------------ delete a session


def test_deleting_a_session_removes_everything_of_it_and_nothing_else(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir)
    gone, kept = world.sessions
    gone_events, kept_events = world.events[gone], world.events[kept]
    assert len(files_under(frame_dir)) == 12  # 2 sessions x 3 reels x 2 frames

    response = delete_session(alice, gone)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verified"] is True and body["existed"] is True
    assert all(v == 0 for v in body["remaining"].values())
    assert body["deleted"]["sessions"] == 1 and body["deleted"]["reel_exposures"] == 3
    assert body["deleted"]["jobs"] == 3 and body["frame_files_deleted"] == 6

    db.rollback()
    uid = uuid.UUID(alice.user_id)
    # Independent checks, not the report's word.
    assert db.get(MonitoringSession, uuid.UUID(gone)) is None
    assert (
        db.execute(
            text("SELECT count(*) FROM reel_exposures WHERE session_id = :s"),
            {"s": uuid.UUID(gone)},
        ).scalar_one()
        == 0
    )
    assert count_user_rows(db, alice.user_id, gone_events)["jobs"] == 0
    for e in gone_events:
        assert not FrameStore(frame_dir).event_dir(uid, uuid.UUID(e)).exists()
    assert count_cache_for(db, [world.shortcodes[e] for e in gone_events]) == 0

    # The other session is whole.
    assert db.get(MonitoringSession, uuid.UUID(kept)) is not None
    assert (
        db.execute(
            text("SELECT count(*) FROM reel_exposures WHERE session_id = :s"),
            {"s": uuid.UUID(kept)},
        ).scalar_one()
        == 3
    )
    assert count_user_rows(db, alice.user_id, kept_events)["jobs"] == 3
    assert len(files_under(frame_dir)) == 6
    assert count_cache_for(db, [world.shortcodes[e] for e in kept_events]) > 0


def test_derived_data_of_the_user_is_regenerated_without_the_deleted_session(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir)
    gone, kept = world.sessions
    uid = uuid.UUID(alice.user_id)
    before = db.query(FingerprintSnapshot).filter_by(user_id=uid).count()
    assert before > 0 and db.query(Prediction).filter_by(user_id=uid).count() == 6 * 5
    assert db.query(PredictionState).filter_by(user_id=uid).count() == 1

    body = delete_session(alice, gone).json()
    db.rollback()
    assert body["verified"] is True and body["predictions_rebuilt"] == 1

    # Predictions and transitions were cumulative over the whole history: they are rebuilt from
    # what remains, so none refers to a deleted Reel and the rest equals a fresh rebuild.
    kept_events = {uuid.UUID(e) for e in world.events[kept]}
    prediction_rows = db.query(Prediction).filter_by(user_id=uid).all()
    assert {r.made_before_event for r in prediction_rows} == kept_events
    assert len(prediction_rows) == 3 * 5
    state = db.query(PredictionState).filter_by(user_id=uid).one().state
    assert state["n_events"] == 3

    def photo() -> Any:
        stored = sorted(
            (str(r.made_before_event), r.model, json.dumps(r.distribution, sort_keys=True))
            for r in db.query(Prediction).filter_by(user_id=uid)
        )
        transitions = sorted(
            (t.from_topic, t.to_topic, t.n)
            for t in db.query(TopicTransition).filter_by(user_id=uid)
        )
        return stored, transitions

    after_delete = photo()
    rebuild_predictions(
        db,
        uid,
        assignment_version=AV,
        fp_config=FingerprintConfig(),
        config=PredictionConfig(min_row_observations=2),
    )
    db.commit()
    assert photo() == after_delete  # identical to a rebuild from scratch of the remaining Reels

    # No snapshot of the deleted session, and the rest equals a fresh rebuild of what remains.
    assert (
        db.query(FingerprintSnapshot)
        .filter(FingerprintSnapshot.session_id == uuid.UUID(gone))
        .count()
        == 0
    )
    rows = db.query(FingerprintSnapshot).filter_by(user_id=uid).all()
    assert rows and {r.session_id for r in rows if r.bucket == "session"} == {uuid.UUID(kept)}
    stored = sorted(
        (r.bucket, r.kind, r.n_reels, json.dumps(r.weights, sort_keys=True)) for r in rows
    )

    db.query(FingerprintSnapshot).filter_by(user_id=uid).delete()
    db.commit()
    rebuild_user(db, uid, assignment_version=AV, topic_ids=TOPICS, config=FingerprintConfig())
    db.commit()
    fresh = sorted(
        (r.bucket, r.kind, r.n_reels, json.dumps(r.weights, sort_keys=True))
        for r in db.query(FingerprintSnapshot).filter_by(user_id=uid)
    )
    assert stored == fresh
    assert all(
        r.n_reels == 3
        for r in db.query(FingerprintSnapshot).filter_by(user_id=uid, kind="exposure")
    )


def test_snapshots_that_cannot_be_rebuilt_are_dropped_not_left_stale(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir)
    db.execute(text("UPDATE fingerprint_snapshots SET weights = weights - 'provenance'"))
    db.commit()
    body = delete_session(alice, world.sessions[0]).json()
    assert (
        body["verified"] is True
        and body["fingerprints_rebuilt"] == 0
        and body["fingerprints_dropped"] > 0
    )
    assert db.query(FingerprintSnapshot).filter_by(user_id=uuid.UUID(alice.user_id)).count() == 0


def test_the_last_session_takes_every_snapshot_with_it(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir, sessions=1)
    assert delete_session(alice, world.sessions[0]).json()["verified"] is True
    db.rollback()
    assert db.query(FingerprintSnapshot).count() == 0
    assert files_under(frame_dir) == []


def count_cache_of(db: Session, user_id: str, shortcodes: list[str]) -> int:
    db.rollback()
    return int(
        db.execute(
            text(
                "SELECT (SELECT count(*) FROM content_features"
                "        WHERE user_id = :u AND reel_shortcode = ANY(:s))"
                " + (SELECT count(*) FROM topic_assignments"
                "        WHERE user_id = :u AND reel_shortcode = ANY(:s))"
            ),
            {"u": user_id, "s": shortcodes},
        ).scalar_one()
    )


def test_cache_rows_are_per_user_so_one_deletion_never_touches_another_users_rows(
    client: TestClient, db: Session, alice: Actor, bob: Actor, frame_dir: Path
) -> None:
    mine = build_world(client, db, alice, frame_dir, prefix="a", sessions=1)
    theirs = build_world(client, db, bob, frame_dir, prefix="b", sessions=1)
    # Bob also watched one of Alice's Reels, and has his own cache rows for it.
    shared = mine.shortcodes[mine.all_events[0]]
    body = event(theirs.sessions[-1], reel_shortcode=shared, frame_count=0)
    assert (
        client.post(f"{API}/events", json={"events": [body]}, headers=bob.bearer).status_code == 200
    )
    owner = uuid.UUID(bob.user_id)
    db.add(
        ContentFeature(
            user_id=owner, reel_shortcode=shared, pipeline_version=AV, modalities=["text"]
        )
    )
    for topic_id in TOPICS:
        db.add(
            TopicAssignment(
                user_id=owner,
                reel_shortcode=shared,
                pipeline_version=AV,
                topic_id=topic_id,
                p_fused=1 / 3,
            )
        )
    db.commit()
    bobs_before = count_cache_of(db, bob.user_id, [*theirs.shortcodes_list, shared])

    assert delete_session(alice, mine.sessions[0]).json()["verified"] is True
    assert count_cache_of(db, alice.user_id, list(mine.shortcodes.values())) == 0
    assert count_cache_of(db, bob.user_id, [*theirs.shortcodes_list, shared]) == bobs_before > 0
    assert count_user_rows(db, alice.user_id, mine.all_events)["reel_exposures"] == 0

    assert delete_all(bob).json()["verified"] is True
    assert count_cache_for(db, [shared, *theirs.shortcodes_list]) == 0


def test_deleting_an_active_session_stops_it_and_later_events_are_refused(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir)
    active = world.sessions[-1]
    assert db.get(MonitoringSession, uuid.UUID(active)).status == "active"  # type: ignore[union-attr]
    assert delete_session(alice, active).json()["verified"] is True
    late = event(active, frame_count=0)
    result = client.post(f"{API}/events", json={"events": [late]}, headers=alice.bearer).json()
    assert result["results"][0] == {
        "event_id": late["event_id"],
        "status": "rejected",
        "reason": "session_not_found",
    }
    upload = client.post(
        f"{API}/events/{world.events[active][0]}/frames",
        files=[("frames", ("f.jpg", jpeg(1), "image/jpeg"))],
        headers=alice.bearer,
    )
    assert upload.status_code == 404  # the event is gone, so are its frames


def test_deleting_a_session_is_idempotent(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir)
    gone = world.sessions[0]
    first = delete_session(alice, gone)
    snapshot = (
        db.query(FingerprintSnapshot).count(),
        db.query(ReelExposure).count(),
        len(files_under(frame_dir)),
    )
    for _ in range(3):
        again = delete_session(alice, gone)
        assert again.status_code == 200
        body = again.json()
        assert body["existed"] is False and body["verified"] is True
        assert all(v == 0 for v in body["deleted"].values())
        db.rollback()
        assert (
            db.query(FingerprintSnapshot).count(),
            db.query(ReelExposure).count(),
            len(files_under(frame_dir)),
        ) == snapshot  # nothing else was touched
    assert first.json()["existed"] is True


def test_a_session_id_that_never_existed_is_the_same_harmless_answer(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    build_world(client, db, alice, frame_dir)
    body = delete_session(alice, str(uuid.uuid4())).json()
    assert body["existed"] is False and body["verified"] is True
    assert db.query(MonitoringSession).count() == 2


# ------------------------------------------------------------------ authorisation


def test_a_user_cannot_delete_another_users_session(
    client: TestClient, db: Session, alice: Actor, bob: Actor, frame_dir: Path
) -> None:
    mine = build_world(client, db, alice, frame_dir, prefix="a")
    build_world(client, db, bob, frame_dir, prefix="b")
    files_before = len(files_under(frame_dir))
    response = delete_session(bob, mine.sessions[0])
    assert response.status_code == 200
    assert response.json()["existed"] is False  # looks exactly like a session that is not there
    db.rollback()
    assert db.get(MonitoringSession, uuid.UUID(mine.sessions[0])) is not None
    assert count_user_rows(db, alice.user_id, mine.all_events)["reel_exposures"] == 6
    assert len(files_under(frame_dir)) == files_before


def test_deletion_needs_a_dashboard_login_with_the_csrf_header(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir, sessions=1)
    anonymous = TestClient(client.app, base_url="http://localhost")
    assert anonymous.delete(f"{API}/sessions/{world.sessions[0]}", headers=CSRF).status_code == 401
    assert anonymous.delete(f"{API}/me/data", headers=CSRF).status_code == 401
    # An extension token is not a dashboard login.
    assert anonymous.delete(f"{API}/me/data", headers={**CSRF, **alice.bearer}).status_code == 401
    # A logged-in browser that omits the CSRF header (a cross-site request would) is refused.
    assert alice.dashboard.delete(f"{API}/sessions/{world.sessions[0]}").status_code == 403
    assert alice.dashboard.delete(f"{API}/me/data").status_code == 403
    assert alice.dashboard.delete(f"{API}/sessions/not-a-uuid", headers=CSRF).status_code == 422
    db.rollback()
    assert db.get(MonitoringSession, uuid.UUID(world.sessions[0])) is not None
    assert count_user_rows(db, alice.user_id, world.all_events)["users"] == 1


# ------------------------------------------------------------------ delete everything


def test_delete_all_leaves_zero_rows_in_every_table_and_zero_files(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir)
    before = count_user_rows(db, alice.user_id, world.all_events)
    assert all(
        before[t] > 0
        for t in (
            "users",
            "sessions",
            "reel_exposures",
            "jobs",
            "predictions",
            "fingerprint_snapshots",
            "extension_tokens",
            "consents",
            "user_sessions",
        )
    )
    assert len(files_under(frame_dir)) == 12

    response = delete_all(alice)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verified"] is True and body["existed"] is True
    assert all(v == 0 for v in body["remaining"].values())
    # The account's cookie is cleared on the way out.
    assert "ref_session=" in response.headers.get("set-cookie", "")

    after = count_user_rows(db, alice.user_id, world.all_events)
    assert after == {k: 0 for k in after}, after  # every table, counted independently
    assert count_cache_for(db, list(world.shortcodes.values())) == 0
    assert files_under(frame_dir) == []
    assert not (frame_dir / alice.user_id).exists()
    for table in (
        "reel_exposures",
        "sessions",
        "jobs",
        "predictions",
        "fingerprint_snapshots",
        "topic_transitions",
        "user_sessions",
        "extension_tokens",
        "consents",
        "content_features",
        "topic_assignments",
    ):
        assert db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0, (
            table
        )  # alone in the DB


def test_delete_all_touches_no_one_else(
    client: TestClient, db: Session, alice: Actor, bob: Actor, frame_dir: Path
) -> None:
    build_world(client, db, alice, frame_dir, prefix="a")
    theirs = build_world(client, db, bob, frame_dir, prefix="b")
    bob_before = count_user_rows(db, bob.user_id, theirs.all_events)
    bob_files = {p for p in files_under(frame_dir) if bob.user_id in p.parts}
    assert bob_files

    assert delete_all(alice).json()["verified"] is True

    assert count_user_rows(db, bob.user_id, theirs.all_events) == bob_before
    assert {p for p in files_under(frame_dir)} == bob_files
    assert count_cache_for(db, list(theirs.shortcodes.values())) > 0
    # Bob can still use the dashboard.
    assert bob.dashboard.get(f"{API}/sessions").status_code == 200


def test_after_delete_all_the_login_and_the_extension_are_cut_off(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir)
    assert delete_all(alice).status_code == 200
    assert alice.dashboard.get(f"{API}/sessions").status_code == 401
    assert client.post(f"{API}/sessions", headers=alice.bearer).status_code == 401
    late = event(world.sessions[0], frame_count=0)
    assert (
        client.post(f"{API}/events", json={"events": [late]}, headers=alice.bearer).status_code
        == 401
    )


def test_delete_all_is_safe_to_repeat(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir)
    assert delete_all(alice).json()["verified"] is True
    # The login is gone, so the repeat is "already gone", never a second deletion.
    again = delete_all(alice)
    assert again.status_code == 401
    after = count_user_rows(db, alice.user_id, world.all_events)
    assert after == {k: 0 for k in after}


def test_the_service_itself_is_idempotent_when_called_again_directly(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    from app.deletion import delete_all_data

    world = build_world(client, db, alice, frame_dir)
    store = FrameStore(frame_dir)
    uid = uuid.UUID(alice.user_id)
    first = delete_all_data(db, store, uid)
    second = delete_all_data(db, store, uid)
    third = delete_all_data(db, store, uid)
    assert first.existed and first.verified
    for later in (second, third):
        assert later.verified and later.existed is False
        assert all(v == 0 for v in later.deleted.values()) and later.frame_files_deleted == 0
    after = count_user_rows(db, alice.user_id, world.all_events)
    assert after == {k: 0 for k in after}


def test_a_user_with_nothing_but_an_account_is_deleted_cleanly(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    body = delete_all(alice).json()
    assert body["verified"] is True and body["existed"] is True
    assert count_user_rows(db, alice.user_id, [])["users"] == 0


# ------------------------------------------------------------------ not fooled by a partial deletion


def test_a_failing_file_deletion_is_not_reported_as_success_and_a_retry_finishes_it(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(client, db, alice, frame_dir)
    original = FrameStore.delete_user

    def broken(self: FrameStore, user_id: uuid.UUID) -> int:
        raise OSError("disk unplugged")

    monkeypatch.setattr(FrameStore, "delete_user", broken)
    response = delete_all(alice)

    assert response.status_code == 500 and response.json()["detail"] == "deletion_incomplete"
    assert response.json()["remaining"]["frame_files"] > 0 and response.json()["verified"] is False
    db.rollback()
    # The rows are gone, but the account is kept so the same login can finish the job.
    assert count_user_rows(db, alice.user_id, world.all_events)["reel_exposures"] == 0
    assert count_user_rows(db, alice.user_id, [])["users"] == 1
    assert len(files_under(frame_dir)) == 12
    assert alice.dashboard.get(f"{API}/sessions").status_code == 200  # still logged in

    monkeypatch.setattr(FrameStore, "delete_user", original)
    done = delete_all(alice)
    assert done.status_code == 200 and done.json()["verified"] is True
    assert files_under(frame_dir) == []
    assert count_user_rows(db, alice.user_id, world.all_events)["users"] == 0


def test_files_that_quietly_survive_make_the_deletion_fail_the_verification(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database rows are gone, but a step that does nothing must not be mistaken for success."""
    world = build_world(client, db, alice, frame_dir)
    monkeypatch.setattr(
        FrameStore, "delete_user", lambda self, user_id: 0
    )  # claims zero, deletes none
    response = delete_all(alice)
    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "deletion_incomplete" and body["verified"] is False
    assert body["remaining"]["frame_files"] > 0
    assert (
        count_user_rows(db, alice.user_id, world.all_events)["reel_exposures"] == 0
    )  # rows are gone anyway

    monkeypatch.undo()
    assert delete_all(alice).json()["verified"] is True  # a retry finishes the job
    assert files_under(frame_dir) == []


def test_session_files_that_survive_fail_the_verification_too(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_world(client, db, alice, frame_dir)
    monkeypatch.setattr(FrameStore, "prune_user", lambda self, user_id, keep: 0)
    response = delete_session(alice, world.sessions[0])
    assert response.status_code == 500 and response.json()["remaining"]["frame_files"] > 0
    assert len(files_under(frame_dir)) == 12  # nothing was removed from disk

    monkeypatch.undo()
    # The session's rows are already gone, so the retry has no session to name: it still finds the
    # frame folders no existing event owns, and removes them.
    retry = delete_session(alice, world.sessions[0])
    assert retry.status_code == 200 and retry.json()["verified"] is True
    assert retry.json()["existed"] is False and retry.json()["frame_files_deleted"] == 6
    assert len(files_under(frame_dir)) == 6


def test_a_skipped_cache_sweep_fails_the_verification_and_a_retry_finishes_it(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import deletion

    world = build_world(client, db, alice, frame_dir, sessions=1)
    monkeypatch.setattr(
        deletion, "_sweep_cache", lambda db_: {"content_features": 0, "topic_assignments": 0}
    )
    response = delete_all(alice)
    assert response.status_code == 500
    remaining = response.json()["remaining"]
    assert remaining["content_features"] > 0 and remaining["topic_assignments"] > 0
    assert count_cache_for(db, list(world.shortcodes.values())) > 0

    monkeypatch.undo()
    done = delete_all(alice)  # the sweep covers every unreferenced cache row, not just this call's
    assert done.status_code == 200 and done.json()["verified"] is True
    assert count_cache_for(db, list(world.shortcodes.values())) == 0


def test_a_job_that_survives_fails_the_verification_and_a_retry_finishes_it(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import deletion

    world = build_world(client, db, alice, frame_dir, sessions=1)
    monkeypatch.setattr(deletion, "_jobs_for_events", lambda db_, ids: 0)
    monkeypatch.setattr(deletion, "_sweep_orphan_jobs", lambda db_: 0)
    response = delete_session(alice, world.sessions[0])
    assert response.status_code == 500 and response.json()["remaining"]["jobs"] == 3
    assert db.query(Job).count() == 3

    monkeypatch.undo()
    retry = delete_session(alice, world.sessions[0])
    assert retry.status_code == 200 and retry.json()["verified"] is True
    assert retry.json()["deleted"]["jobs"] == 3  # orphans of exposures that no longer exist
    db.rollback()
    assert db.query(Job).count() == 0


# ------------------------------------------------------------------ consent revocation


def test_revoking_consent_stops_the_active_session_and_refuses_more_data(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir)
    active = world.sessions[-1]
    assert db.get(MonitoringSession, uuid.UUID(active)).status == "active"  # type: ignore[union-attr]

    assert client.delete(f"{API}/consent", headers=alice.bearer).status_code == 204

    db.rollback()
    row = db.get(MonitoringSession, uuid.UUID(active))
    assert row is not None and row.status == "stopped" and row.stop_reason == "consent_revoked"
    assert row.ended_at is not None
    late = event(active, frame_count=0, observed_at=iso(datetime.now(UTC)))
    refused = client.post(f"{API}/events", json={"events": [late]}, headers=alice.bearer)
    assert refused.status_code == 403 and refused.json()["detail"] == "consent_required"
    upload = client.post(
        f"{API}/events/{world.events[active][0]}/frames",
        files=[("frames", ("f.jpg", jpeg(1), "image/jpeg"))],
        headers=alice.bearer,
    )
    assert upload.status_code == 403
    # Revoking does not delete what was collected; deletion is its own explicit act.
    assert count_user_rows(db, alice.user_id, world.all_events)["reel_exposures"] == 6


def test_deleting_everything_after_revoking_consent_works(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir)
    client.delete(f"{API}/consent", headers=alice.bearer)
    assert delete_all(alice).json()["verified"] is True
    after = count_user_rows(db, alice.user_id, world.all_events)
    assert after == {k: 0 for k in after}
    assert files_under(frame_dir) == []


# ------------------------------------------------------------------ what the answer contains


def test_the_answer_carries_counts_and_ids_only(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir)
    text_of = delete_session(alice, world.sessions[0]).text + delete_all(alice).text
    for event_id in world.all_events:
        assert event_id not in text_of
    assert "caption of" not in text_of and "alice@example.test" not in text_of


def test_a_frame_folder_that_is_a_link_is_removed_without_following_it(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path, tmp_path: Path
) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("not a frame")
    frame_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(precious, frame_dir / alice.user_id, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    assert delete_all(alice).json()["verified"] is True
    assert (precious / "keep.txt").exists()  # the link went, its target did not
    assert not os.path.lexists(frame_dir / alice.user_id)


def test_stray_temporary_files_in_the_user_folder_are_deleted_too(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    build_world(client, db, alice, frame_dir)
    stray = frame_dir / alice.user_id / "orphan-event" / ".tmp-abandoned"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"half a frame")
    assert delete_all(alice).json()["verified"] is True
    assert files_under(frame_dir) == []


def test_old_data_is_deleted_too_not_only_recent_data(
    client: TestClient, db: Session, alice: Actor, frame_dir: Path
) -> None:
    world = build_world(client, db, alice, frame_dir, sessions=1)
    long_ago = datetime.now(UTC) - timedelta(days=400)
    db.execute(text("UPDATE reel_exposures SET observed_at = :t"), {"t": long_ago})
    db.commit()
    assert delete_session(alice, world.sessions[0]).json()["verified"] is True
    assert db.query(ReelExposure).count() == 0
