"""Raw frame storage on the local server volume (spec F7, F15, N7). No object storage.

Layout, under `REF_FRAME_STORE_DIR`:

    <user_id>/<event_id>/<sha256 of the JPEG bytes>.jpg

- The file name is the content hash, so uploading the same frame again (a retry, a lost response)
  changes nothing: it is not written twice and its age does not restart. One event never holds more
  than a handful of files, and a name can never contain a path separator: user and event ids are
  UUIDs and the rest is hex.
- A file is written to a temporary name in its own directory and renamed into place, so a reader
  or a sweeper never sees half a frame.
- Frames live at most 24 hours (F15). They are deleted earlier by `delete_event` once the worker
  has extracted features, and `sweep` removes anything older than the TTL by file modification
  time. Raw media is never stored in PostgreSQL.

Nothing here logs a file's content. Logs carry counts and ids only.
"""

import hashlib
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("app.frames")

MAX_FRAMES_PER_EVENT = 5  # spec F7
MAX_FRAME_BYTES = 150 * 1024  # spec §8: at most 150 KB per JPEG
# Five maximal frames plus multipart boundaries and headers. The owner gave 5 and 150 KB; the body
# cap follows from them. (ADR-013, Proposed, also says 800 KB; that ADR is not applied.)
MAX_UPLOAD_BODY_BYTES = 800 * 1024
FRAME_SUFFIX = ".jpg"
_TMP_PREFIX = ".tmp-"

_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"


class FrameFormatError(Exception):
    """The bytes are not a JPEG. `reason` is a short code, never file content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check_jpeg(data: bytes) -> None:
    """Magic bytes, not the header the client sent (ADR-013 intent).

    Start of image `FF D8 FF` and end of image `FF D9` (trailing zero padding allowed), so an empty
    file, a PNG renamed to .jpg and a cut-off upload are all refused. The picture is not decoded
    here; the worker decodes it and a decode failure is a failed stage there, not a lost event.
    """
    if not data:
        raise FrameFormatError("empty")
    if not data.startswith(_JPEG_SOI):
        raise FrameFormatError("not_jpeg")
    if not data.rstrip(b"\x00").endswith(_JPEG_EOI):
        raise FrameFormatError("truncated_jpeg")


@dataclass(frozen=True)
class SweepResult:
    files_deleted: int
    dirs_removed: int


class FrameStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def event_dir(self, user_id: uuid.UUID, event_id: uuid.UUID) -> Path:
        return self.root / str(user_id) / str(event_id)

    def list_event(self, user_id: uuid.UUID, event_id: uuid.UUID) -> set[str]:
        """Hashes of the frames stored for one event."""
        folder = self.event_dir(user_id, event_id)
        try:
            with os.scandir(folder) as entries:
                return {
                    entry.name.removesuffix(FRAME_SUFFIX)
                    for entry in entries
                    if entry.name.endswith(FRAME_SUFFIX) and entry.is_file(follow_symlinks=False)
                }
        except FileNotFoundError:
            return set()

    def save(self, user_id: uuid.UUID, event_id: uuid.UUID, data: bytes) -> tuple[str, bool]:
        """Store one frame. Returns `(sha256, created)`; `created` is False for a repeat."""
        digest = hashlib.sha256(data).hexdigest()
        folder = self.event_dir(user_id, event_id)
        final = folder / f"{digest}{FRAME_SUFFIX}"
        if final.is_file():
            return digest, False
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = folder / f"{_TMP_PREFIX}{secrets.token_hex(8)}"
        try:
            with open(temp, "wb") as handle:
                handle.write(data)
            os.replace(temp, final)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return digest, True

    def delete_event(self, user_id: uuid.UUID, event_id: uuid.UUID) -> int:
        """Delete every frame of one event (the worker calls this after extraction)."""
        folder = self.event_dir(user_id, event_id)
        deleted = 0
        try:
            with os.scandir(folder) as entries:
                names = [e.name for e in entries if e.is_file(follow_symlinks=False)]
        except FileNotFoundError:
            return 0
        for name in names:
            try:
                (folder / name).unlink()
                deleted += 1
            except FileNotFoundError:
                pass
        _rmdir_if_empty(folder)
        _rmdir_if_empty(folder.parent)
        return deleted

    def user_dir(self, user_id: uuid.UUID) -> Path:
        return self.root / str(user_id)

    def user_files(self, user_id: uuid.UUID) -> int:
        """Every file below a user's folder, whatever its name (temporary files included)."""
        return _count_files(self.user_dir(user_id))

    def user_dir_exists(self, user_id: uuid.UUID) -> bool:
        return os.path.lexists(self.user_dir(user_id))

    def prune_user(self, user_id: uuid.UUID, keep: set[str]) -> int:
        """Delete everything in a user's folder except the event folders named in `keep`.

        `keep` is the ids of events that still exist. A folder for any other event is garbage (its
        exposure was deleted, or never committed), and so is a loose file: all of it goes, so a
        deletion that failed halfway is finished by calling it again. Returns files deleted.
        """
        root = self.user_dir(user_id)
        if not os.path.lexists(root):
            return 0
        if root.is_symlink() or not root.is_dir():
            root.unlink()
            return 1
        deleted = 0
        with os.scandir(root) as entries:
            children = [(Path(e.path), e.name, e.is_dir(follow_symlinks=False)) for e in entries]
        for child, name, is_dir in children:
            if is_dir and name in keep:
                continue
            if is_dir:
                deleted += _remove_tree(child)
                child.rmdir()
            else:
                child.unlink()
                deleted += 1
        _rmdir_if_empty(root)
        return deleted

    def user_files_outside(self, user_id: uuid.UUID, keep: set[str]) -> int:
        """Files (and the folder itself, if nothing is kept) that `prune_user` would remove."""
        root = self.user_dir(user_id)
        if not os.path.lexists(root):
            return 0
        if root.is_symlink() or not root.is_dir():
            return 1
        total = 0
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False) and entry.name in keep:
                    continue
                total += _count_files(Path(entry.path)) + (
                    1 if entry.is_dir(follow_symlinks=False) else 0
                )
        return total

    def delete_user(self, user_id: uuid.UUID) -> int:
        """Delete a user's whole folder: every frame, every temporary file, the folders.

        Symbolic links are removed as links and never followed. Returns the number of files
        deleted. A missing folder is zero, not an error, so a repeated call is safe.
        """
        root = self.user_dir(user_id)
        if not os.path.lexists(root):
            return 0
        if root.is_symlink() or not root.is_dir():
            root.unlink()
            return 1
        deleted = _remove_tree(root)
        _rmdir_if_empty(root)
        return deleted

    def sweep(self, max_age_seconds: float, now: float | None = None) -> SweepResult:
        """Delete every file older than `max_age_seconds` (by modification time), then empty dirs.

        Symbolic links are never followed or deleted. Safe to run while uploads happen: a file
        being written has a temporary name and a fresh modification time.
        """
        cutoff = (time.time() if now is None else now) - max_age_seconds
        files = 0
        dirs = 0
        for user_dir in _subdirs(self.root):
            for event_dir in _subdirs(user_dir):
                try:
                    with os.scandir(event_dir) as entries:
                        stale = [
                            e.path
                            for e in entries
                            if e.is_file(follow_symlinks=False) and e.stat().st_mtime < cutoff
                        ]
                except FileNotFoundError:
                    continue
                for path in stale:
                    try:
                        os.unlink(path)
                        files += 1
                    except FileNotFoundError:
                        pass
                dirs += _rmdir_if_empty(event_dir)
            dirs += _rmdir_if_empty(user_dir)
        if files or dirs:
            log.info("frame sweep files_deleted=%d dirs_removed=%d", files, dirs)
        return SweepResult(files, dirs)

    def count_files(self) -> int:
        total = 0
        for user_dir in _subdirs(self.root):
            for event_dir in _subdirs(user_dir):
                with os.scandir(event_dir) as entries:
                    total += sum(1 for e in entries if e.is_file(follow_symlinks=False))
        return total


def _subdirs(path: Path | str) -> list[Path]:
    try:
        with os.scandir(path) as entries:
            return [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]
    except FileNotFoundError:
        return []


def _rmdir_if_empty(path: Path) -> int:
    try:
        path.rmdir()
    except OSError:  # not empty, or already gone
        return 0
    return 1


def _count_files(path: Path) -> int:
    if not os.path.lexists(path):
        return 0
    if path.is_symlink() or path.is_file():
        return 1
    total = 0
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                total += _count_files(Path(entry.path))
            else:
                total += 1
    return total


def _remove_tree(path: Path) -> int:
    """Delete everything below `path` (not `path` itself), links unfollowed. Counts files."""
    deleted = 0
    with os.scandir(path) as entries:
        children = [(Path(e.path), e.is_dir(follow_symlinks=False)) for e in entries]
    for child, is_dir in children:
        if is_dir:
            deleted += _remove_tree(child)
            child.rmdir()
        else:
            child.unlink()
            deleted += 1
    return deleted
