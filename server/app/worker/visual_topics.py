"""Taxonomy embeddings in the image encoder's space, cached on disk (spec §5.2).

Each topic's vector is the mean of the unit-length text-tower embeddings of its descriptions
(English ones wrapped in the model's prompt template, Hindi ones as they are), scaled to unit
length. These are SigLIP vectors, not e5 vectors: they are compared with SigLIP image vectors only.
The cache key hashes everything that determines them (model id, revision, threads, template,
languages, taxonomy content), so a different model or one reworded description never reuses a
stale file. The file format and its checks are those of `topic_embeddings`.
"""

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.taxonomy import Taxonomy, taxonomy_hash
from app.worker.config import canonical_json
from app.worker.embedder import EmbeddingError, Vectors, l2_normalize
from app.worker.text_normalize import normalize_fragment
from app.worker.topic_embeddings import read_matrix_cache, write_matrix_cache
from app.worker.vision_config import VisualPipelineConfig
from app.worker.visual_embedder import ImageEmbedder


@dataclass(frozen=True)
class VisualTopicEmbeddings:
    topic_ids: tuple[int, ...]
    #: `(topics, dim)`, float32, unit rows, in `topic_ids` order. Image-encoder space.
    matrix: Vectors
    key: str
    from_cache: bool


def visual_topic_key(config: VisualPipelineConfig, taxonomy: Taxonomy) -> str:
    model = config.model
    material: dict[str, Any] = {
        "kind": "visual-topic-embeddings",
        "model_id": model.model_id,
        "revision": model.revision,
        "dim": model.dim,
        "threads": model.threads,
        "text_max_length": model.text_max_length,
        "text_template": model.text_template,
        "lowercase_text": model.lowercase_text,
        "languages": list(config.scoring.description_languages),
        "unicode_version": unicodedata.unidata_version,
        "taxonomy_hash": taxonomy_hash(taxonomy),
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()[:16]


def _descriptions(config: VisualPipelineConfig, taxonomy_topic: Any) -> list[str]:
    template = config.model.text_template
    texts: list[str] = []
    for lang in config.scoring.description_languages:
        for raw in getattr(taxonomy_topic.descriptions, lang):
            text = normalize_fragment(raw)
            texts.append(template.format(text) if lang == "en" else text)
    return texts


def build_visual_topic_embeddings(
    embedder: ImageEmbedder, config: VisualPipelineConfig, taxonomy: Taxonomy
) -> Vectors:
    rows: list[Vectors] = []
    for topic in taxonomy.topics:
        texts = _descriptions(config, topic)
        vectors = embedder.embed_texts(texts)
        if vectors.shape != (len(texts), config.model.dim):
            raise EmbeddingError("text tower returned the wrong shape")
        rows.append(l2_normalize(vectors.mean(axis=0, keepdims=True).astype(np.float32)))
    return np.concatenate(rows, axis=0)


def load_or_build_visual_topic_embeddings(
    embedder: ImageEmbedder,
    config: VisualPipelineConfig,
    taxonomy: Taxonomy,
    cache_dir: Path | None,
) -> VisualTopicEmbeddings:
    key = visual_topic_key(config, taxonomy)
    topic_ids = tuple(t.id for t in taxonomy.topics)
    path = None if cache_dir is None else cache_dir / f"visual-topic-embeddings-{key}.npz"
    if path is not None and path.exists():
        cached = read_matrix_cache(path, key, topic_ids, config.model.dim)
        if cached is not None:
            return VisualTopicEmbeddings(topic_ids, cached, key, from_cache=True)
    matrix = build_visual_topic_embeddings(embedder, config, taxonomy)
    if matrix.shape != (len(topic_ids), config.model.dim):
        raise EmbeddingError("visual topic embeddings have the wrong shape")
    if path is not None:
        write_matrix_cache(path, key, topic_ids, matrix)
    return VisualTopicEmbeddings(topic_ids, matrix, key, from_cache=False)


class VisualTopicProvider:
    """Loads the visual taxonomy embeddings once per process; a failure is not remembered."""

    def __init__(
        self,
        embedder: ImageEmbedder,
        config: VisualPipelineConfig,
        taxonomy: Taxonomy,
        cache_dir: Path | None,
    ) -> None:
        self._args = (embedder, config, taxonomy, cache_dir)
        self._value: VisualTopicEmbeddings | None = None

    def get(self) -> VisualTopicEmbeddings:
        if self._value is None:
            self._value = load_or_build_visual_topic_embeddings(*self._args)
        return self._value
