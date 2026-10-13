"""Test doubles for the worker pipeline: a deterministic embedder and a fake clock."""

import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import numpy as np

from app.worker.embedder import EmbedKind, Vectors
from app.worker.stages import Clocks
from app.worker.visual_embedder import FrameDecodeError


class FakeEmbedder:
    """A bag-of-words embedder with the real interface: needs no model.

    Every word maps to a fixed pseudo-random direction (seeded by its hash); a text is the sum of
    its words' directions, scaled to unit length. Texts that share words are therefore similar, so
    zero-shot scoring behaves plausibly, and the output is exactly reproducible.
    """

    def __init__(self, dim: int = 384) -> None:
        self._dim = dim
        self.calls: list[tuple[EmbedKind, tuple[str, ...]]] = []
        #: Set to make `embed` raise (any callable returning an exception to raise, or None).
        self.fail: Callable[[EmbedKind, Sequence[str]], Exception | None] | None = None
        #: Set to replace the returned vectors (for shape and norm failure tests).
        self.override: Callable[[Vectors], Vectors] | None = None

    @property
    def dim(self) -> int:
        return self._dim

    def _word(self, word: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.sha256(word.encode("utf-8")).digest()[:8], "big")
        return np.random.default_rng(seed).standard_normal(self._dim)

    def embed(self, texts: Sequence[str], kind: EmbedKind) -> Vectors:
        self.calls.append((kind, tuple(texts)))
        if self.fail is not None:
            error = self.fail(kind, texts)
            if error is not None:
                raise error
        rows = []
        for text in texts:
            words = text.lower().replace(",", " ").split() or ["<empty>"]
            vector = np.sum([self._word(w) for w in words], axis=0)
            rows.append(vector / np.linalg.norm(vector))
        out: Vectors = np.array(rows, dtype=np.float32).reshape(len(texts), self._dim)
        return self.override(out) if self.override is not None else out

    def calls_of(self, kind: EmbedKind) -> list[tuple[str, ...]]:
        return [texts for k, texts in self.calls if k == kind]


class FakeClock:
    """Wall and monotonic clocks that move only when told to."""

    def __init__(self) -> None:
        self.wall_time = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
        self.mono = 1000.0

    def advance(self, seconds: float) -> None:
        self.wall_time += timedelta(seconds=seconds)
        self.mono += seconds

    def clocks(self) -> Clocks:
        return Clocks(wall=lambda: self.wall_time, monotonic=lambda: self.mono)


def fake_frame(words: str = "frame") -> bytes:
    """A stand-in JPEG: valid start and end markers around the words the fake encoder reads."""
    return bytes([0xFF, 0xD8, 0xFF, 0xE0]) + words.encode("utf-8") + bytes([0xFF, 0xD9])


class FakeImageEmbedder:
    """A SigLIP-shaped fake: image and text towers share one 768-number space, unrelated to e5's.

    Both towers map words to fixed pseudo-random directions, as `FakeEmbedder` does. A fake frame's
    "pixels" are the words inside it (`fake_frame("cricket bat ball")`), so a frame about cricket
    lands near texts about cricket and zero-shot scoring behaves plausibly. A frame containing the
    word CORRUPT cannot be decoded.
    """

    def __init__(self, dim: int = 768) -> None:
        self._dim = dim
        self.image_calls: list[tuple[bytes, ...]] = []
        self.text_calls: list[tuple[str, ...]] = []
        #: Set to make `embed_images` raise for a call (return an exception or None).
        self.fail_images: Callable[[tuple[bytes, ...]], Exception | None] | None = None
        self.fail_texts: Callable[[tuple[str, ...]], Exception | None] | None = None

    @property
    def dim(self) -> int:
        return self._dim

    def _direction(self, word: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.sha256(b"visual:" + word.encode("utf-8")).digest()[:8], "big")
        return np.random.default_rng(seed).standard_normal(self._dim)

    def _vector(self, words: list[str]) -> np.ndarray:
        total = np.sum([self._direction(w) for w in (words or ["<empty>"])], axis=0)
        return total / np.linalg.norm(total)

    def embed_images(self, jpegs: Sequence[bytes]) -> Vectors:
        batch = tuple(jpegs)
        self.image_calls.append(batch)
        if self.fail_images is not None:
            error = self.fail_images(batch)
            if error is not None:
                raise error
        rows = []
        for jpeg in batch:
            text = jpeg[4:-2].decode("utf-8", errors="replace")
            if "CORRUPT" in text:
                raise FrameDecodeError("frame could not be decoded")
            rows.append(self._vector(text.lower().split()))
        return np.array(rows, dtype=np.float32).reshape(len(batch), self._dim)

    def embed_texts(self, texts: Sequence[str]) -> Vectors:
        batch = tuple(texts)
        self.text_calls.append(batch)
        if self.fail_texts is not None:
            error = self.fail_texts(batch)
            if error is not None:
                raise error
        rows = [self._vector(t.lower().replace(",", " ").replace(".", " ").split()) for t in batch]
        return np.array(rows, dtype=np.float32).reshape(len(batch), self._dim)
