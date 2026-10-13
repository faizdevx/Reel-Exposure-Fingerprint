"""Frame upload: POST /api/v1/events/{event_id}/frames (spec F7, F15, §8). Need a database."""

import hashlib
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from support import CSRF, Actor, make_actor
from test_events import event, grant, iso, start, stop

from app.config import Settings, get_settings
from app.frames import MAX_FRAME_BYTES, MAX_UPLOAD_BODY_BYTES, FrameStore
from app.main import _sweep_frames_forever

pytestmark = pytest.mark.db

API = "/api/v1"


def jpeg(tag: int = 0, size: int = 2_000) -> bytes:
    """A small stand-in JPEG: valid start and end markers, `tag` makes it unique."""
    body = bytes([tag % 256]) * (size - 6)
    return b"\xff\xd8\xff\xe0" + body + b"\xff\xd9"


def files(
    *frames: bytes, content_type: str = "image/jpeg"
) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("frames", (f"f{i}.jpg", data, content_type)) for i, data in enumerate(frames)]


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


def ingest(client: TestClient, actor: Actor, session_id: str, **over: Any) -> str:
    body = event(session_id, **over)
    response = client.post(f"{API}/events", json={"events": [body]}, headers=actor.bearer)
    assert response.json()["results"][0]["status"] == "stored", response.text
    event_id: str = body["event_id"]
    return event_id


@pytest.fixture
def ready(client: TestClient, alice: Actor, frame_dir: Path) -> tuple[str, str]:
    """Alice consented, has a running session and one stored event declaring 5 frames."""
    grant(client, alice)
    session_id = start(client, alice)
    return session_id, ingest(client, alice, session_id, frame_count=5)


def post(client: TestClient, actor: Actor, event_id: str, parts: Any, **kw: Any) -> Any:
    return client.post(f"{API}/events/{event_id}/frames", files=parts, headers=actor.bearer, **kw)


def stored_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.jpg"))


# ---- valid upload


def test_valid_upload_stores_the_frames_on_the_local_volume(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    frames = [jpeg(1), jpeg(2), jpeg(3)]
    response = post(client, alice, event_id, files(*frames))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["event_id"] == event_id
    assert body["total_frames"] == 3
    assert [f["status"] for f in body["frames"]] == ["stored"] * 3
    assert [f["sha256"] for f in body["frames"]] == [hashlib.sha256(f).hexdigest() for f in frames]
    assert [f["bytes"] for f in body["frames"]] == [2000] * 3
    on_disk = stored_files(frame_dir)
    assert len(on_disk) == 3
    assert {p.read_bytes() for p in on_disk} == set(frames)
    assert all(alice.user_id in p.parts and event_id in p.parts for p in on_disk)


def test_five_frames_of_the_largest_allowed_size_are_accepted(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    frames = [jpeg(n, MAX_FRAME_BYTES) for n in range(5)]
    assert sum(len(f) for f in frames) < MAX_UPLOAD_BODY_BYTES
    assert post(client, alice, event_id, files(*frames)).status_code == 200
    assert len(stored_files(frame_dir)) == 5


def test_retrying_the_same_upload_stores_nothing_twice(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    first = post(client, alice, event_id, files(jpeg(1), jpeg(2)))
    again = post(client, alice, event_id, files(jpeg(1), jpeg(2)))
    assert [f["status"] for f in first.json()["frames"]] == ["stored", "stored"]
    assert [f["status"] for f in again.json()["frames"]] == ["duplicate", "duplicate"]
    assert again.json()["total_frames"] == 2
    assert len(stored_files(frame_dir)) == 2


def test_frames_can_arrive_in_several_requests_up_to_the_limit(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    assert post(client, alice, event_id, files(jpeg(1), jpeg(2), jpeg(3))).status_code == 200
    assert post(client, alice, event_id, files(jpeg(4), jpeg(5))).json()["total_frames"] == 5
    over = post(client, alice, event_id, files(jpeg(6)))
    assert over.status_code == 409
    assert over.json()["detail"] == "frame_limit_exceeded"
    assert len(stored_files(frame_dir)) == 5


def test_an_event_cannot_receive_more_frames_than_it_declared(
    client: TestClient, alice: Actor, frame_dir: Path
) -> None:
    grant(client, alice)
    event_id = ingest(client, alice, start(client, alice), frame_count=2)
    response = post(client, alice, event_id, files(jpeg(1), jpeg(2), jpeg(3)))
    assert response.status_code == 409
    assert response.json()["detail"] == "frame_limit_exceeded"
    assert stored_files(frame_dir) == []  # all or nothing


def test_an_event_that_declared_no_frames_accepts_none(
    client: TestClient, alice: Actor, frame_dir: Path
) -> None:
    grant(client, alice)
    event_id = ingest(client, alice, start(client, alice), frame_count=0)
    assert post(client, alice, event_id, files(jpeg(1))).status_code == 409


def test_frames_are_accepted_after_the_session_was_stopped(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    session_id, event_id = ready
    stop(client, alice, session_id)
    assert post(client, alice, event_id, files(jpeg(1))).status_code == 200


# ---- invalid MIME and content


@pytest.mark.parametrize(
    "content_type", ["image/png", "image/gif", "text/plain", "application/octet-stream", ""]
)
def test_a_part_that_is_not_declared_as_jpeg_is_refused(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path, content_type: str
) -> None:
    _, event_id = ready
    response = post(client, alice, event_id, files(jpeg(1), content_type=content_type))
    assert response.status_code == 415
    assert response.json()["detail"] == "unsupported_media_type"
    assert stored_files(frame_dir) == []


@pytest.mark.parametrize(
    "data",
    [
        b"\x89PNG\r\n\x1a\n" + b"x" * 100,  # a PNG sent with a JPEG header
        b"not an image at all",
        b"\xff\xd8\xff\xe0" + b"cut off before the end",
        b"",
    ],
)
def test_the_bytes_must_be_a_jpeg_whatever_the_header_says(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path, data: bytes
) -> None:
    _, event_id = ready
    response = post(client, alice, event_id, files(data))
    assert response.status_code == 415
    assert stored_files(frame_dir) == []


def test_one_bad_part_rejects_the_whole_request(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    response = post(client, alice, event_id, files(jpeg(1), b"junk", jpeg(2)))
    assert response.status_code == 415
    assert stored_files(frame_dir) == []


def test_a_body_that_is_not_multipart_is_refused(
    client: TestClient, alice: Actor, ready: tuple[str, str]
) -> None:
    _, event_id = ready
    response = client.post(
        f"{API}/events/{event_id}/frames",
        content=jpeg(1),
        headers={**alice.bearer, "Content-Type": "image/jpeg"},
    )
    assert response.status_code == 415


def test_a_form_field_that_is_not_a_file_is_refused(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    response = client.post(
        f"{API}/events/{event_id}/frames",
        data={"user_id": str(uuid.uuid4())},
        files=files(jpeg(1)),
        headers=alice.bearer,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "unexpected_field"
    assert stored_files(frame_dir) == []


def test_an_empty_form_has_no_frames(
    client: TestClient, alice: Actor, ready: tuple[str, str]
) -> None:
    _, event_id = ready
    response = client.post(
        f"{API}/events/{event_id}/frames", data={}, files={}, headers=alice.bearer
    )
    assert response.status_code in (415, 422)


# ---- size and count limits


def test_a_frame_over_150_kb_is_refused(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    response = post(client, alice, event_id, files(jpeg(1, MAX_FRAME_BYTES + 1)))
    assert response.status_code == 413
    assert response.json()["detail"] == "frame_too_large"
    assert stored_files(frame_dir) == []


def test_six_images_are_too_many(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    response = post(client, alice, event_id, files(*[jpeg(n) for n in range(6)]))
    assert response.status_code == 422
    assert response.json()["detail"] == "too_many_frames"
    assert stored_files(frame_dir) == []


def test_a_huge_part_count_is_refused_without_storing_anything(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    response = post(client, alice, event_id, files(*[jpeg(n, 200) for n in range(40)]))
    assert response.status_code == 422
    assert stored_files(frame_dir) == []


def test_a_body_over_the_cap_is_refused_before_it_is_read(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    big = b"\xff\xd8\xff" + b"x" * (MAX_UPLOAD_BODY_BYTES + 10)
    response = post(client, alice, event_id, files(big))
    assert response.status_code == 413
    assert response.json()["detail"] == "payload_too_large"
    assert stored_files(frame_dir) == []


def test_a_streamed_body_over_the_cap_is_cut_off(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    """No Content-Length (chunked): the middleware counts the bytes as they arrive."""
    _, event_id = ready

    def chunks() -> Any:
        yield b"--b\r\nContent-Disposition: form-data; name=frames; filename=f.jpg\r\n"
        yield b"Content-Type: image/jpeg\r\n\r\n\xff\xd8\xff"
        for _ in range(MAX_UPLOAD_BODY_BYTES // 1024 + 5):
            yield b"x" * 1024
        yield b"\r\n--b--\r\n"

    response = client.post(
        f"{API}/events/{event_id}/frames",
        content=chunks(),
        headers={**alice.bearer, "Content-Type": "multipart/form-data; boundary=b"},
    )
    assert response.status_code == 413
    assert stored_files(frame_dir) == []


# ---- who may upload


def test_no_token_is_401(client: TestClient, ready: tuple[str, str]) -> None:
    _, event_id = ready
    anonymous = TestClient(client.app, base_url="http://localhost")
    assert (
        anonymous.post(f"{API}/events/{event_id}/frames", files=files(jpeg(1))).status_code == 401
    )


def test_a_dashboard_cookie_is_not_enough(
    client: TestClient, alice: Actor, ready: tuple[str, str]
) -> None:
    _, event_id = ready
    response = alice.dashboard.post(
        f"{API}/events/{event_id}/frames", files=files(jpeg(1)), headers=CSRF
    )
    assert response.status_code == 401


def test_an_event_that_does_not_exist_is_404(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    response = post(client, alice, str(uuid.uuid4()), files(jpeg(1)))
    assert response.status_code == 404
    assert response.json()["detail"] == "event_not_found"
    assert stored_files(frame_dir) == []


def test_an_event_id_that_is_not_a_uuid_is_422(
    client: TestClient, alice: Actor, ready: tuple[str, str]
) -> None:
    assert post(client, alice, "not-a-uuid", files(jpeg(1))).status_code == 422


def test_another_users_event_looks_like_a_missing_one(
    client: TestClient, alice: Actor, bob: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    grant(client, bob)
    response = post(client, bob, event_id, files(jpeg(1)))
    assert response.status_code == 404
    assert response.json() == {"detail": "event_not_found"}
    assert stored_files(frame_dir) == []
    # and Alice's own upload still works
    assert post(client, alice, event_id, files(jpeg(1))).status_code == 200


def test_the_owner_comes_from_the_token_never_from_the_request(
    client: TestClient, alice: Actor, bob: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    grant(client, bob)
    response = client.post(
        f"{API}/events/{event_id}/frames?user_id={alice.user_id}",
        files=files(jpeg(1)),
        headers=bob.bearer,
    )
    assert response.status_code == 404
    assert stored_files(frame_dir) == []


# ---- consent


def test_without_consent_nothing_is_stored(
    client: TestClient, alice: Actor, bob: Actor, frame_dir: Path
) -> None:
    response = post(client, bob, str(uuid.uuid4()), files(jpeg(1)))
    assert response.status_code == 403
    assert response.json()["detail"] == "consent_required"
    assert stored_files(frame_dir) == []


def test_revoked_consent_stops_uploads_for_an_event_that_already_exists(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    assert client.delete(f"{API}/consent", headers=alice.bearer).status_code in (200, 204)
    response = post(client, alice, event_id, files(jpeg(1)))
    assert response.status_code == 403
    assert response.json()["detail"] == "consent_required"
    assert stored_files(frame_dir) == []


def test_a_regranted_consent_does_not_reopen_a_revoked_session(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    client.delete(f"{API}/consent", headers=alice.bearer)
    grant(client, alice)
    response = post(client, alice, event_id, files(jpeg(1)))
    assert response.status_code == 409
    assert response.json()["detail"] == "session_revoked"
    assert stored_files(frame_dir) == []


# ---- session verification and age


def test_frames_for_an_event_older_than_the_retention_are_refused(
    client: TestClient, db: Session, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    db.execute(
        text("UPDATE reel_exposures SET observed_at = :t WHERE event_id = :e"),
        {"t": datetime.now(UTC) - timedelta(hours=25), "e": event_id},
    )
    db.commit()
    response = post(client, alice, event_id, files(jpeg(1)))
    assert response.status_code == 409
    assert response.json()["detail"] == "frames_expired"
    assert stored_files(frame_dir) == []


def test_the_events_session_must_belong_to_the_caller(
    client: TestClient, alice: Actor, bob: Actor, frame_dir: Path
) -> None:
    """Bob cannot attach frames to Alice's event through Alice's session."""
    grant(client, alice)
    grant(client, bob)
    event_id = ingest(client, alice, start(client, alice), frame_count=5)
    bob_session = start(client, bob)
    ingest(client, bob, bob_session, frame_count=5)
    assert post(client, bob, event_id, files(jpeg(1))).status_code == 404
    assert stored_files(frame_dir) == []


# ---- privacy of responses and logs


def test_logs_carry_ids_and_counts_only(
    client: TestClient,
    alice: Actor,
    ready: tuple[str, str],
    frame_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, event_id = ready
    secret = b"SECRET-PIXELS" * 20
    data = b"\xff\xd8\xff\xe0" + secret + b"\xff\xd9"
    with caplog.at_level(logging.DEBUG):
        post(client, alice, event_id, files(data))
    assert "SECRET-PIXELS" not in caplog.text
    assert "frames event=" in caplog.text


# ---- TTL deletion


def test_frames_are_deleted_after_extraction_by_the_worker_call(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    post(client, alice, event_id, files(jpeg(1), jpeg(2)))
    removed = FrameStore(frame_dir).delete_event(uuid.UUID(alice.user_id), uuid.UUID(event_id))
    assert removed == 2
    assert stored_files(frame_dir) == []


def test_frames_older_than_24_hours_are_deleted_by_the_sweep(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    post(client, alice, event_id, files(jpeg(1), jpeg(2), jpeg(3)))
    paths = stored_files(frame_dir)
    assert len(paths) == 3
    old = datetime.now(UTC).timestamp() - 25 * 3600
    os.utime(paths[0], (old, old))
    os.utime(paths[1], (old, old))

    result = FrameStore(frame_dir).sweep(24 * 3600)

    assert result.files_deleted == 2
    assert stored_files(frame_dir) == [paths[2]]  # the fresh frame stays


def test_the_directory_is_empty_the_day_after(
    client: TestClient, alice: Actor, ready: tuple[str, str], frame_dir: Path
) -> None:
    _, event_id = ready
    post(client, alice, event_id, files(*[jpeg(n) for n in range(5)]))
    long_ago = datetime.now(UTC).timestamp() - 24 * 3600 - 60
    for path in stored_files(frame_dir):
        os.utime(path, (long_ago, long_ago))
    FrameStore(frame_dir).sweep(24 * 3600)
    assert list(frame_dir.iterdir()) == []


@pytest.mark.anyio
async def test_the_api_sweeper_removes_expired_frames_in_the_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    root = tmp_path / "frames"
    store = FrameStore(root)
    store.save(uuid.UUID(int=1), uuid.UUID(int=2), jpeg(1))
    (path,) = stored_files(root)
    old = datetime.now(UTC).timestamp() - 30 * 3600
    os.utime(path, (old, old))

    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/x", frame_store_dir=root)  # type: ignore[arg-type]
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    task = asyncio.create_task(_sweep_frames_forever(3600))
    for _ in range(50):
        if not stored_files(root):
            break
        await asyncio.sleep(0.05)
    task.cancel()
    assert stored_files(root) == []


def test_the_retention_setting_cannot_exceed_24_hours() -> None:
    with pytest.raises(ValueError):
        Settings(database_url="postgresql+psycopg://u:p@localhost/x", frame_ttl_hours=25)  # type: ignore[arg-type]


def test_ingest_still_works_without_frames(
    client: TestClient, alice: Actor, frame_dir: Path
) -> None:
    grant(client, alice)
    session_id = start(client, alice)
    body = event(session_id, observed_at=iso(datetime.now(UTC)), frame_count=0)
    assert (
        client.post(f"{API}/events", json={"events": [body]}, headers=alice.bearer).status_code
        == 200
    )
