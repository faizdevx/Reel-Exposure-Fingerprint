"""Taxonomy embeddings for zero-shot scoring, cached on disk (spec §5.2, ADR-009 proposed details).

Each topic's vector is the mean of the unit-length embeddings of its English and Hindi
descriptions, taken with the `passage: ` prefix, scaled back to unit length. A topic's vector
depends only on the model, the descriptions and the normaliser, so it is computed once and cached
as `topic-embeddings-<key>.npz`. The key is a hash of everything that determines the vectors (model
id and revision, prefix, max length, thread count, normaliser version, and the taxonomy content
hash): a different model or one reworded description is a different key, so a stale file is never
read. A cache file is checked on load (shape, topic ids, finite, unit length, key); anything
unexpected is ignored and rebuilt. A cache that cannot be written is not an error: the vectors are
still returned.
"""

import hashlib
import logging
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from app.taxonomy import Taxonomy, taxonomy_hash
from app.worker.config import TextPipelineConfig, canonical_json
from app.worker.embedder import EmbeddingError, TextEmbedder, Vectors, l2_normalize
from app.worker.text_normalize import NORMALIZER_VERSION, normalize_fragment

log = logging.getLogger("app.worker.topics")

_UNIT_TOLERANCE = 1e-3


@dataclass(frozen=True)
class TopicEmbeddings:
    topic_ids: tuple[int, ...]
    #: `(topics, dim)`, float32, each row unit length, rows in `topic_ids` order.
    matrix: Vectors
    key: str
    from_cache: bool


def topic_embeddings_key(config: TextPipelineConfig, taxonomy: Taxonomy) -> str:
    model = config.model
    material: dict[str, Any] = {
        "model_id": model.model_id,
        "revision": model.revision,
        "dim": model.dim,
        "max_length": model.max_length,
        "passage_prefix": model.passage_prefix,
        "threads": model.threads,
        "normalizer": NORMALIZER_VERSION,
        "unicode_version": unicodedata.unidata_version,
        "taxonomy_hash": taxonomy_hash(taxonomy),
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()[:16]


def build_topic_embeddings(embedder: TextEmbedder, taxonomy: Taxonomy) -> Vectors:
    """Embed every description (one batch per topic) and average them per topic."""
    rows: list[Vectors] = []
    for topic in taxonomy.topics:
        descriptions = [
            normalize_fragment(text) for text in (*topic.descriptions.en, *topic.descriptions.hi)
        ]
        vectors = embedder.embed(descriptions, "passage")
        if vectors.shape[0] != len(descriptions):
            raise EmbeddingError("embedder returned the wrong number of vectors")
        rows.append(l2_normalize(vectors.mean(axis=0, keepdims=True).astype(np.float32)))
    return np.concatenate(rows, axis=0)


def _cache_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"topic-embeddings-{key}.npz"


def read_matrix_cache(path: Path, key: str, topic_ids: tuple[int, ...], dim: int) -> Vectors | None:
    try:
        with np.load(path, allow_pickle=False) as data:
            stored_key = str(data["key"])
            stored_ids = tuple(int(i) for i in data["topic_ids"])
            matrix: npt.NDArray[Any] = data["matrix"]
    except Exception:  # any unreadable file (not a zip, truncated, bad pickle flag): rebuild
        return None
    if stored_key != key or stored_ids != topic_ids:
        return None
    if matrix.shape != (len(topic_ids), dim) or matrix.dtype != np.float32:
        return None
    if not bool(np.isfinite(matrix).all()):
        return None
    if not bool(np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=_UNIT_TOLERANCE)):
        return None
    return matrix


def write_matrix_cache(path: Path, key: str, topic_ids: tuple[int, ...], matrix: Vectors) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("wb") as handle:
            np.savez(handle, key=np.array(key), topic_ids=np.array(topic_ids), matrix=matrix)
        os.replace(tmp, path)  # atomic: a reader sees the old file or the whole new one
    except OSError as error:
        log.warning("topic embedding cache not written error=%s", type(error).__name__)
        tmp.unlink(missing_ok=True)


def load_or_build_topic_embeddings(
    embedder: TextEmbedder,
    config: TextPipelineConfig,
    taxonomy: Taxonomy,
    cache_dir: Path | None,
) -> TopicEmbeddings:
    key = topic_embeddings_key(config, taxonomy)
    topic_ids = tuple(t.id for t in taxonomy.topics)
    path = None if cache_dir is None else _cache_path(cache_dir, key)
    if path is not None and path.exists():
        cached = read_matrix_cache(path, key, topic_ids, config.model.dim)
        if cached is not None:
            return TopicEmbeddings(topic_ids, cached, key, from_cache=True)
        log.warning("topic embedding cache ignored key=%s", key)
    matrix = build_topic_embeddings(embedder, taxonomy)
    if matrix.shape != (len(topic_ids), config.model.dim):
        raise EmbeddingError("topic embeddings have the wrong shape")
    if path is not None:
        write_matrix_cache(path, key, topic_ids, matrix)
    return TopicEmbeddings(topic_ids, matrix, key, from_cache=False)


class TopicEmbeddingProvider:
    """Loads the taxonomy embeddings once per process and hands out the same object after that."""

    def __init__(
        self,
        embedder: TextEmbedder,
        config: TextPipelineConfig,
        taxonomy: Taxonomy,
        cache_dir: Path | None,
    ) -> None:
        self._args = (embedder, config, taxonomy, cache_dir)
        self._value: TopicEmbeddings | None = None

    def get(self) -> TopicEmbeddings:
        if self._value is None:  # a failure is not remembered: the next Reel tries again
            self._value = load_or_build_topic_embeddings(*self._args)
        return self._value
