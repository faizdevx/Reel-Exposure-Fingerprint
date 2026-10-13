"""Zero-shot scoring tests: softmax(cos / tau) over the taxonomy embeddings. No model, no database."""

import numpy as np
import pytest

from app.worker.embedder import l2_normalize
from app.worker.scoring import ScoringError, TopicScores, score_topics

IDS = (1, 2, 3, 4)


def unit(*values: float) -> np.ndarray:
    return l2_normalize(np.array([values], dtype=np.float32))[0]


def topics() -> np.ndarray:
    return l2_normalize(
        np.array(
            [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
            dtype=np.float32,
        )
    )


def test_probabilities_sum_to_one_and_follow_the_cosines() -> None:
    scores = score_topics(unit(0.9, 0.3, 0.1, 0.0), IDS, topics(), tau=0.1)
    assert sum(scores.probabilities) == pytest.approx(1.0, abs=1e-12)
    assert all(0.0 <= p <= 1.0 for p in scores.probabilities)
    assert list(scores.probabilities) == sorted(scores.probabilities, reverse=True)
    assert scores.ranked()[0][0] == 1


def test_matches_the_formula_computed_by_hand() -> None:
    embedding = unit(0.6, 0.8, 0.0, 0.0)
    scores = score_topics(embedding, IDS, topics(), tau=0.5)
    cos = np.array([0.6, 0.8, 0.0, 0.0])
    expected = np.exp(cos / 0.5) / np.exp(cos / 0.5).sum()
    np.testing.assert_allclose(scores.probabilities, expected, rtol=1e-6)
    np.testing.assert_allclose(scores.cosines, cos, rtol=1e-6)


def test_a_smaller_temperature_is_more_peaked() -> None:
    embedding = unit(0.9, 0.3, 0.1, 0.0)
    cold = max(score_topics(embedding, IDS, topics(), tau=0.01).probabilities)
    warm = max(score_topics(embedding, IDS, topics(), tau=1.0).probabilities)
    assert cold > warm
    assert cold > 0.99


def test_equal_cosines_give_a_uniform_distribution_and_ties_go_to_the_lower_id() -> None:
    scores = score_topics(unit(1, 1, 1, 1), IDS, topics(), tau=0.02)
    assert scores.probabilities == pytest.approx((0.25,) * 4)
    assert [i for i, _ in scores.ranked()] == [1, 2, 3, 4]


def test_extreme_logits_do_not_overflow() -> None:
    scores = score_topics(unit(1, 0, 0, 0), IDS, topics(), tau=1e-4)
    assert scores.probabilities[0] == pytest.approx(1.0)
    assert all(np.isfinite(p) for p in scores.probabilities)
    assert sum(scores.probabilities) == pytest.approx(1.0)


def test_scoring_is_deterministic_and_exact() -> None:
    embedding = unit(0.2, 0.7, 0.4, 0.1)
    assert score_topics(embedding, IDS, topics(), 0.01) == score_topics(
        embedding, IDS, topics(), 0.01
    )


def test_a_stored_embedding_scores_exactly_like_a_fresh_one() -> None:
    fresh = unit(0.2, 0.7, 0.4, 0.1)
    stored = np.array(fresh.tolist(), dtype=np.float32)  # what a float4 column hands back
    assert np.array_equal(fresh, stored)
    assert score_topics(fresh, IDS, topics(), 0.01) == score_topics(stored, IDS, topics(), 0.01)


def test_ranked_orders_by_probability_then_id() -> None:
    scores = TopicScores((5, 3, 9), (0.0, 0.0, 0.0), (0.2, 0.5, 0.5))
    assert scores.ranked() == [(3, 0.5), (9, 0.5), (5, 0.2)]


@pytest.mark.parametrize(
    "embedding",
    [
        np.array([1.0, 0.0, 0.0], dtype=np.float32),  # wrong width
        np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32),  # wrong rank
        np.array([2.0, 0.0, 0.0, 0.0], dtype=np.float32),  # not unit length
        np.array([np.nan, 0.0, 0.0, 0.0], dtype=np.float32),
        np.array([np.inf, 0.0, 0.0, 0.0], dtype=np.float32),
        np.zeros(4, dtype=np.float32),
    ],
)
def test_unusable_embeddings_are_refused(embedding: np.ndarray) -> None:
    with pytest.raises(ScoringError):
        score_topics(embedding, IDS, topics(), tau=0.01)


@pytest.mark.parametrize("tau", [0.0, -0.01, float("nan"), float("inf") * 0 + float("nan")])
def test_a_bad_temperature_is_refused(tau: float) -> None:
    with pytest.raises(ScoringError):
        score_topics(unit(1, 0, 0, 0), IDS, topics(), tau=tau)


def test_topic_ids_must_match_the_matrix() -> None:
    with pytest.raises(ScoringError):
        score_topics(unit(1, 0, 0, 0), (1, 2, 3), topics(), tau=0.01)


def test_a_non_finite_topic_matrix_is_an_error_not_a_distribution() -> None:
    matrix = topics()
    matrix[2, 1] = np.nan
    with pytest.raises(Exception, match=r".*") as info:
        score_topics(unit(1, 0, 0, 0), IDS, matrix, tau=0.01)
    assert type(info.value).__name__ in {"EmbeddingError", "ScoringError"}
