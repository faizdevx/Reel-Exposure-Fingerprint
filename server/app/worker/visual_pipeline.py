"""The visual stages of the worker pipeline, each with a report.

    JPEG frames  ->  embed_frames (SigLIP image tower, one frame at a time)
                 ->  pool_frames (mean of unit vectors, scaled to unit length)
    load_visual_taxonomy_embeddings (SigLIP text tower, cached)  ->  score_visual

Failure handling follows spec F9 and the project rules: the visual modality may fail without
touching the text modality, and inside it:

- No frames: every stage is skipped with reason `no_frames`. A normal outcome, not an error.
- A frame that cannot be decoded or embedded is dropped and counted (`frames_failed`); the Reel is
  embedded from the others. Only when no frame works does `embed_frames` fail.
- The taxonomy embeddings do not depend on the Reel's frames, so they load whenever there are
  frames, even if `embed_frames` failed. `score_visual` needs both and says which one is missing.

If a pooled embedding for this Reel and pipeline version was stored earlier, pass it as
`cached_embedding`: `embed_frames` and `pool_frames` are then skipped with reason `cached` and only
scoring runs; no frame is decoded.

Frames are put in a fixed order (sorted by the SHA-256 of their bytes, identical frames counted
once, at most `max_frames`), so the pooled vector does not depend on the order they were uploaded.
Reports and logs carry stage names, counts and error classes only, never image data.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.taxonomy import Taxonomy
from app.worker.embedder import EmbeddingError, Vectors, l2_normalize
from app.worker.scoring import TopicScores, check_unit_vector, score_topics
from app.worker.stages import Outcome, StageReport, StageRunner
from app.worker.vision_config import VISUAL_MODALITY, VisualPipelineConfig
from app.worker.visual_embedder import ImageEmbedder
from app.worker.visual_topics import VisualTopicEmbeddings, VisualTopicProvider

STAGE_EMBED_FRAMES = "embed_frames"
STAGE_POOL = "pool_frames"
STAGE_VISUAL_TAXONOMY = "load_visual_taxonomy_embeddings"
STAGE_SCORE_VISUAL = "score_visual"

_MIN_POOLED_NORM = 1e-6


@dataclass(frozen=True)
class VisualResult:
    reports: tuple[StageReport, ...]
    #: Unit-length float32 vector, or None if the visual modality did not produce one.
    embedding: Vectors | None
    scores: TopicScores | None
    frames_used: int
    frames_failed: int

    @property
    def modalities(self) -> tuple[str, ...]:
        return (VISUAL_MODALITY,) if self.embedding is not None else ()


def select_frames(frames: Sequence[bytes], max_frames: int) -> list[bytes]:
    """Distinct frames in a fixed order, at most `max_frames`."""
    unique = {hashlib.sha256(f).hexdigest(): f for f in frames if f}
    return [unique[digest] for digest in sorted(unique)][:max_frames]


def pool_embeddings(vectors: Vectors) -> Vectors:
    """Mean of unit vectors, scaled back to unit length. Order does not matter."""
    if vectors.ndim != 2 or vectors.shape[0] == 0:
        raise EmbeddingError("nothing to pool")
    mean = vectors.astype(np.float64).mean(axis=0, keepdims=True)
    if float(np.linalg.norm(mean)) < _MIN_POOLED_NORM:
        raise EmbeddingError("frame embeddings cancel out")
    pooled: Vectors = l2_normalize(mean.astype(np.float32))[0]
    return pooled


class VisualPipeline:
    def __init__(
        self,
        config: VisualPipelineConfig,
        taxonomy: Taxonomy,
        embedder: ImageEmbedder,
        topics: VisualTopicProvider,
    ) -> None:
        self.config = config
        self.taxonomy = taxonomy
        self.embedder = embedder
        self.topics = topics

    @classmethod
    def create(
        cls,
        config: VisualPipelineConfig,
        taxonomy: Taxonomy,
        embedder: ImageEmbedder,
        cache_dir: Path | None,
    ) -> "VisualPipeline":
        return cls(
            config, taxonomy, embedder, VisualTopicProvider(embedder, config, taxonomy, cache_dir)
        )

    # ---- stage bodies

    def _embed_frames(self, frames: list[bytes]) -> tuple[Vectors, int]:
        good: list[Vectors] = []
        failed = 0
        for frame in frames:
            try:
                vector = self.embedder.embed_images([frame])
            except EmbeddingError:  # one bad frame must not cost the others
                failed += 1
                continue
            if vector.shape != (1, self.config.model.dim):
                failed += 1
                continue
            good.append(vector)
        if not good:
            raise EmbeddingError("no frame could be embedded")
        return np.concatenate(good, axis=0), failed

    def _pool(self, vectors: Vectors) -> Vectors:
        pooled = pool_embeddings(vectors)
        check_unit_vector(pooled, self.config.model.dim)
        return pooled

    def _score(self, embedding: Vectors, topics: VisualTopicEmbeddings) -> TopicScores:
        return score_topics(
            embedding, topics.topic_ids, topics.matrix, self.config.scoring.tau_visual
        )

    # ---- the run

    def run(
        self,
        frames: Sequence[bytes],
        *,
        runner: StageRunner | None = None,
        cached_embedding: Vectors | None = None,
    ) -> VisualResult:
        runner = runner or StageRunner()
        embedding: Vectors | None = None
        used = failed = 0
        chosen = select_frames(frames, self.config.max_frames)
        have_input = cached_embedding is not None or bool(chosen)

        if cached_embedding is not None:
            runner.skip(STAGE_EMBED_FRAMES, "cached")
            runner.skip(STAGE_POOL, "cached")
            embedding = cached_embedding
        elif not chosen:
            runner.skip(STAGE_EMBED_FRAMES, "no_frames")
            runner.skip(STAGE_POOL, "no_frames")
        else:
            e: Outcome[tuple[Vectors, int]] = runner.run(
                STAGE_EMBED_FRAMES, lambda: self._embed_frames(chosen)
            )
            if e.ok and e.value is not None:
                vectors, failed = e.value
                used = int(vectors.shape[0])
                p = runner.run(STAGE_POOL, lambda: self._pool(vectors))
                embedding = p.value if p.ok else None
            else:
                failed = len(chosen)
                runner.skip(STAGE_POOL, f"upstream_{STAGE_EMBED_FRAMES}")

        topics: VisualTopicEmbeddings | None = None
        if have_input:
            t = runner.run(STAGE_VISUAL_TAXONOMY, self.topics.get)
            topics = t.value if t.ok else None
        else:
            runner.skip(STAGE_VISUAL_TAXONOMY, "no_frames")

        scores: TopicScores | None = None
        if embedding is not None and topics is not None:
            captured_embedding, captured_topics = embedding, topics
            s = runner.run(
                STAGE_SCORE_VISUAL, lambda: self._score(captured_embedding, captured_topics)
            )
            scores = s.value if s.ok else None
        elif not have_input:
            runner.skip(STAGE_SCORE_VISUAL, "no_frames")
        else:
            missing = STAGE_POOL if embedding is None else STAGE_VISUAL_TAXONOMY
            runner.skip(STAGE_SCORE_VISUAL, f"upstream_{missing}")

        return VisualResult(
            reports=tuple(r for r in runner.reports if r.stage in _VISUAL_STAGES),
            embedding=embedding,
            scores=scores,
            frames_used=used,
            frames_failed=failed,
        )


_VISUAL_STAGES = frozenset(
    {STAGE_EMBED_FRAMES, STAGE_POOL, STAGE_VISUAL_TAXONOMY, STAGE_SCORE_VISUAL}
)

__all__ = ["VisualPipeline", "VisualResult", "pool_embeddings", "select_frames"]
