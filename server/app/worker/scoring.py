"""Zero-shot topic scoring: `p(k) = softmax_k( cos(e, t_k) / tau )` (spec §5.2).

`e` is the Reel's text embedding, `t_k` topic k's embedding, both unit length, so the cosine is a
dot product. Nothing is fitted. The result is a probability for each listed topic. The catch-all
`other` of spec §5.2 comes from the fusion threshold (M4), so it is not produced here.

The arithmetic runs in float64 on float32 inputs and has no randomness. The same embedding always
gives the same probabilities, whether it was just computed or read back from `content_features`
(pgvector stores float32 exactly, and the vector is not re-normalised on the way).
"""

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from app.worker.embedder import EmbeddingError, Vectors

_UNIT_TOLERANCE = 1e-3


class ScoringError(Exception):
    """Inputs the scorer cannot use (wrong shape, not finite, not unit length)."""


@dataclass(frozen=True)
class TopicScores:
    topic_ids: tuple[int, ...]
    cosines: tuple[float, ...]
    probabilities: tuple[float, ...]

    def ranked(self) -> list[tuple[int, float]]:
        """`(topic_id, probability)`, highest first; ties go to the lower topic id."""
        pairs = zip(self.topic_ids, self.probabilities, strict=True)
        return sorted(pairs, key=lambda pair: (-pair[1], pair[0]))


def check_unit_vector(vector: npt.NDArray[np.float32], dim: int) -> None:
    if vector.shape != (dim,):
        raise ScoringError("embedding has the wrong shape")
    if not bool(np.isfinite(vector).all()):
        raise ScoringError("embedding is not finite")
    if abs(float(np.linalg.norm(vector.astype(np.float64))) - 1.0) > _UNIT_TOLERANCE:
        raise ScoringError("embedding is not unit length")


def score_topics(
    embedding: Vectors,
    topic_ids: tuple[int, ...],
    topic_matrix: Vectors,
    tau: float,
) -> TopicScores:
    if tau <= 0.0 or not np.isfinite(tau):
        raise ScoringError("tau must be positive")
    if topic_matrix.ndim != 2 or topic_matrix.shape[0] != len(topic_ids):
        raise ScoringError("topic matrix does not match the topic ids")
    check_unit_vector(embedding, topic_matrix.shape[1])

    cosines = topic_matrix.astype(np.float64) @ embedding.astype(np.float64)
    logits = cosines / tau
    logits -= logits.max()  # numerical stability; does not change the result
    weights = np.exp(logits)
    probabilities = weights / weights.sum()
    if not bool(np.isfinite(probabilities).all()):
        raise EmbeddingError("scores were not finite")
    return TopicScores(
        topic_ids=topic_ids,
        cosines=tuple(float(c) for c in cosines),
        probabilities=tuple(float(p) for p in probabilities),
    )
