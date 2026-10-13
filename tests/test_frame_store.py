"""Frame storage on the local volume: JPEG check, save, delete, TTL sweep. No database."""

import hashlib
import os
import uuid
from pathlib import Path

import pytest

from app.frames import (
    MAX_FRAME_BYTES,
    FrameFormatError,
    FrameStore,
    check_jpeg,
)

USER = uuid.UUID(int=1)
OTHER = uuid.UUID(int=2)
EVENT = uuid.UUID(int=10)
HOUR = 3600
NOW = 1_800_000_000.0


def jpeg(payload: bytes = b"pixels") -> bytes:
    return b"\xff\xd8\xff\xe0" + payload + b"\xff\xd9"


def age(path: Path, seconds: float) -> None:
    os.utime(path, (NOW - seconds, NOW - seconds))


def only_file(store: FrameStore, user: uuid.UUID, event: uuid.UUID) -> Path:
    (name,) = store.list_event(user, event)
    return store.event_dir(user, event) / f"{name}.jpg"


# ---- JPEG check


def test_a_jpeg_passes() -> None:
    check_jpeg(jpeg())
    check_jpeg(jpeg() + b"\x00\x00")  # trailing zero padding is tolerated


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", "empty"),
        (b"\x89PNG\r\n\x1a\n" + b"x" * 20, "not_jpeg"),
        (b"GIF89a" + b"x" * 20, "not_jpeg"),
        (b"\xff\xd8\xff\xe0" + b"cut off", "truncated_jpeg"),
        (b"<html>\xff\xd8\xff</html>", "not_jpeg"),
    ],
)
def test_non_jpeg_bytes_are_refused(data: bytes, reason: str) -> None:
    with pytest.raises(FrameFormatError) as error:
        check_jpeg(data)
    assert error.value.reason == reason


# ---- save


def test_save_names_the_file_by_its_hash_and_stores_the_bytes(tmp_path: Path) -> None:
    store = FrameStore(tmp_path)
    data = jpeg(b"one")
    digest, created = store.save(USER, EVENT, data)
    assert created
    assert digest == hashlib.sha256(data).hexdigest()
    assert (tmp_path / str(USER) / str(EVENT) / f"{digest}.jpg").read_bytes() == data
    assert store.list_event(USER, EVENT) == {digest}


def test_saving_the_same_frame_again_is_a_no_op_that_keeps_its_age(tmp_path: Path) -> None:
    store = FrameStore(tmp_path)
    store.save(USER, EVENT, jpeg())
    path = only_file(store, USER, EVENT)
    age(path, 5 * HOUR)
    _, created = store.save(USER, EVENT, jpeg())
    assert not created
    assert path.stat().st_mtime == NOW - 5 * HOUR  # the retention clock did not restart
    assert store.count_files() == 1


def test_no_temporary_file_is_left_behind(tmp_path: Path) -> None:
    store = FrameStore(tmp_path)
    store.save(USER, EVENT, jpeg())
    names = [p.name for p in store.event_dir(USER, EVENT).iterdir()]
    assert all(not n.startswith(".tmp-") for n in names)


def test_a_failed_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FrameStore(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.save(USER, EVENT, jpeg())
    assert store.list_event(USER, EVENT) == set()
    assert [p.name for p in store.event_dir(USER, EVENT).iterdir()] == []


def test_events_and_users_are_separate_folders(tmp_path: Path) -> None:
    store = FrameStore(tmp_path)
    store.save(USER, EVENT, jpeg(b"a"))
    store.save(OTHER, EVENT, jpeg(b"b"))
    store.save(USER, uuid.UUID(int=11), jpeg(b"c"))
    assert len(store.list_event(USER, EVENT)) == 1
    assert store.count_files() == 3


def test_the_largest_allowed_frame_is_stored(tmp_path: Path) -> None:
    data = jpeg(b"x" * (MAX_FRAME_BYTES - 6))
    assert len(data) == MAX_FRAME_BYTES
    FrameStore(tmp_path).save(USER, EVENT, data)


# ---- delete after extraction


def test_delete_event_removes_its_frames_and_empty_folders_only(tmp_path: Path) -> None:
    store = FrameStore(tmp_path)
    for n in range(3):
        store.save(USER, EVENT, jpeg(bytes([n])))
    other_event = uuid.UUID(int=11)
    store.save(USER, other_event, jpeg(b"keep"))
    assert store.delete_event(USER, EVENT) == 3
    assert not store.event_dir(USER, EVENT).exists()
    assert store.list_event(USER, other_event) != set()  # a neighbour is untouched
    assert (tmp_path / str(USER)).exists()
    store.delete_event(USER, other_event)
    assert not (tmp_path / str(USER)).exists()  # last frame gone, the user folder goes too


def test_delete_event_with_nothing_stored_is_zero(tmp_path: Path) -> None:
    assert FrameStore(tmp_path).delete_event(USER, EVENT) == 0
    assert FrameStore(tmp_path / "missing").delete_event(USER, EVENT) == 0


# ---- TTL sweep


def test_sweep_deletes_frames_older_than_the_ttl_and_keeps_newer_ones(tmp_path: Path) -> None:
    store = FrameStore(tmp_path)
    store.save(USER, EVENT, jpeg(b"old"))
    old = only_file(store, USER, EVENT)
    store.save(USER, EVENT, jpeg(b"new"))
    new = next(p for p in store.event_dir(USER, EVENT).iterdir() if p != old)
    age(old, 24 * HOUR + 1)
    age(new, 23 * HOUR)

    result = store.sweep(24 * HOUR, now=NOW)

    assert result.files_deleted == 1
    assert not old.exists()
    assert new.exists()


def test_sweep_keeps_a_frame_just_inside_the_limit_and_removes_one_just_past_it(
    tmp_path: Path,
) -> None:
    store = FrameStore(tmp_path)
    store.save(USER, EVENT, jpeg(b"edge"))
    path = only_file(store, USER, EVENT)
    age(path, 24 * HOUR - 1)
    assert store.sweep(24 * HOUR, now=NOW).files_deleted == 0
    age(path, 24 * HOUR + 1)
    assert store.sweep(24 * HOUR, now=NOW).files_deleted == 1


def test_sweep_leaves_the_directory_empty_the_next_day(tmp_path: Path) -> None:
    store = FrameStore(tmp_path)
    for user in (USER, OTHER):
        for n in range(5):
            store.save(user, EVENT, jpeg(bytes([n])))
    for path in tmp_path.rglob("*.jpg"):
        age(path, 25 * HOUR)

    result = store.sweep(24 * HOUR, now=NOW)

    assert result.files_deleted == 10
    assert list(tmp_path.iterdir()) == []  # the root stays, everything under it is gone
    assert store.count_files() == 0


def test_sweep_removes_stale_temporary_files_too(tmp_path: Path) -> None:
    store = FrameStore(tmp_path)
    store.save(USER, EVENT, jpeg())
    stray = store.event_dir(USER, EVENT) / ".tmp-abandoned"
    stray.write_bytes(b"half a frame")
    age(stray, 30 * HOUR)
    age(only_file(store, USER, EVENT), 30 * HOUR)
    store.sweep(24 * HOUR, now=NOW)
    assert not stray.exists()


def test_sweep_on_a_missing_or_empty_root_does_nothing(tmp_path: Path) -> None:
    assert FrameStore(tmp_path / "nope").sweep(HOUR, now=NOW).files_deleted == 0
    assert FrameStore(tmp_path).sweep(HOUR, now=NOW).files_deleted == 0


def test_sweep_does_not_follow_symbolic_links(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "important.txt"
    victim.write_text("keep me")
    age(victim, 99 * HOUR)
    root = tmp_path / "frames"
    (root / str(USER)).mkdir(parents=True)
    try:
        os.symlink(outside, root / str(USER) / str(EVENT), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    FrameStore(root).sweep(HOUR, now=NOW)
    assert victim.exists()
