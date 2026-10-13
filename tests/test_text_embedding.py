"""Text embedding tests that need no model: pooling maths, config rules, and the e5 wrapper with stubs."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import numpy as np
import pytest

from app.worker.config import E5_REVISION, TextModelConfig
from app.worker.embedder import E5Embedder, EmbeddingError, l2_normalize, mean_pool

# ------------------------------------------------------------------ pooling maths


def test_mean_pool_averages_only_the_unmasked_tokens() -> None:
    hidden = np.array(
        [
            [[1.0, 2.0], [3.0, 4.0], [100.0, 100.0]],  # third token is padding
            [[5.0, 5.0], [7.0, 9.0], [9.0, 13.0]],
        ],
        dtype=np.float32,
    )
    mask = np.array([[1, 1, 0], [1, 1, 1]])
    pooled = mean_pool(hidden, mask)
    assert pooled.dtype == np.float32
    np.testing.assert_allclose(pooled, [[2.0, 3.0], [7.0, 9.0]])


def test_mean_pool_refuses_a_text_with_no_tokens() -> None:
    with pytest.raises(EmbeddingError):
        mean_pool(np.zeros((1, 3, 2), dtype=np.float32), np.zeros((1, 3), dtype=np.int64))


def test_l2_normalize_gives_unit_rows() -> None:
    out = l2_normalize(np.array([[3.0, 4.0], [0.0, 2.0]], dtype=np.float32))
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), [1.0, 1.0], rtol=1e-6)
    np.testing.assert_allclose(out[0], [0.6, 0.8], rtol=1e-6)


@pytest.mark.parametrize(
    "bad",
    [
        np.array([[0.0, 0.0]], dtype=np.float32),
        np.array([[np.nan, 1.0]], dtype=np.float32),
        np.array([[np.inf, 1.0]], dtype=np.float32),
    ],
)
def test_l2_normalize_refuses_zero_or_non_finite_vectors(bad: np.ndarray) -> None:
    with pytest.raises(EmbeddingError):
        l2_normalize(bad)


# ------------------------------------------------------------------ config rules


def test_the_model_is_pinned_to_a_full_commit_sha() -> None:
    assert TextModelConfig().revision == E5_REVISION
    assert len(E5_REVISION) == 40
    for bad in ("main", "v1", "614241f", "G" * 40, "614241F622F53C4EEFF9890BDC4F31CFECC418B3", ""):
        with pytest.raises(ValueError, match="revision"):
            TextModelConfig(revision=bad)


def test_config_rejects_values_that_would_break_the_column_or_the_model() -> None:
    with pytest.raises(ValueError, match="dim"):
        TextModelConfig(dim=768)
    with pytest.raises(ValueError, match="threads"):
        TextModelConfig(threads=0)
    with pytest.raises(ValueError, match="threads"):
        TextModelConfig(max_length=2)


def test_e5_prefixes_are_the_ones_the_model_card_requires() -> None:
    cfg = TextModelConfig()
    assert (cfg.query_prefix, cfg.passage_prefix) == ("query: ", "passage: ")


# ------------------------------------------------------------------ the e5 wrapper, with stubs


class _Array:
    def __init__(self, data: np.ndarray) -> None:
        self._data = data

    def cpu(self) -> "_Array":
        return self

    def float(self) -> "_Array":
        return self

    def numpy(self) -> np.ndarray:
        return self._data


class _Torch:
    @contextmanager
    def inference_mode(self) -> Iterator[None]:
        yield


class _Tokenizer:
    """Gives each text a number of tokens equal to its word count, padded to the longest."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, batch: list[str], **kwargs: Any) -> dict[str, Any]:
        self.seen.append(list(batch))
        self.kwargs.append(kwargs)
        lengths = [len(t.split()) for t in batch]
        width = max(lengths)
        mask = np.array([[1] * n + [0] * (width - n) for n in lengths])
        return {"attention_mask": _Array(mask), "lengths": lengths}


class _Output:
    def __init__(self, hidden: np.ndarray) -> None:
        self.last_hidden_state = _Array(hidden)


class _Model:
    def __init__(self, dim: int) -> None:
        self.dim = dim

    def __call__(self, *, attention_mask: _Array, lengths: list[int]) -> _Output:
        width = attention_mask.numpy().shape[1]
        hidden = np.zeros((len(lengths), width, self.dim), dtype=np.float32)
        for row, n in enumerate(lengths):
            hidden[row, :n, 0] = float(n)  # first axis carries the token count
            hidden[row, :n, 1] = 1.0
            hidden[row, n:, :] = 99.0  # padding must be ignored
        return _Output(hidden)


def stubbed(batch_size: int = 16) -> tuple[E5Embedder, _Tokenizer]:
    embedder = E5Embedder(TextModelConfig(), batch_size=batch_size)
    tokenizer = _Tokenizer()
    embedder._torch = _Torch()
    embedder._tokenizer = tokenizer
    embedder._model = _Model(embedder.dim)
    return embedder, tokenizer


def test_queries_and_passages_get_their_prefixes() -> None:
    embedder, tokenizer = stubbed()
    embedder.embed(["one two"], "query")
    embedder.embed(["one two"], "passage")
    assert tokenizer.seen == [["query: one two"], ["passage: one two"]]


def test_tokenizer_is_asked_to_pad_truncate_and_cap_the_length() -> None:
    embedder, tokenizer = stubbed()
    embedder.embed(["a b"], "query")
    kwargs = tokenizer.kwargs[0]
    assert kwargs["padding"] is True
    assert kwargs["truncation"] is True
    assert kwargs["max_length"] == 512
    assert kwargs["return_tensors"] == "pt"


def test_output_is_unit_length_float32_with_one_row_per_text() -> None:
    embedder, _ = stubbed()
    out = embedder.embed(["a b c", "d e"], "query")
    assert out.shape == (2, 384)
    assert out.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, rtol=1e-5)


def test_padding_does_not_change_a_vector() -> None:
    embedder, _ = stubbed()
    alone = embedder.embed(["a b"], "query")
    padded = embedder.embed(["a b", "w x y z q r s"], "query")
    np.testing.assert_allclose(alone[0], padded[0], rtol=1e-6)


def test_long_lists_are_split_into_batches() -> None:
    embedder, tokenizer = stubbed(batch_size=2)
    out = embedder.embed([f"t{i}" for i in range(5)], "passage")
    assert out.shape == (5, 384)
    assert [len(b) for b in tokenizer.seen] == [2, 2, 1]


def test_an_empty_list_returns_an_empty_matrix_without_loading_anything() -> None:
    embedder = E5Embedder(TextModelConfig())
    out = embedder.embed([], "query")
    assert out.shape == (0, 384)
    assert embedder._model is None


def test_a_degenerate_model_output_is_an_error_not_a_vector() -> None:
    embedder, _ = stubbed()

    class _Zero(_Model):
        def __call__(self, *, attention_mask: _Array, lengths: list[int]) -> _Output:
            width = attention_mask.numpy().shape[1]
            return _Output(np.zeros((len(lengths), width, self.dim), dtype=np.float32))

    embedder._model = _Zero(embedder.dim)
    with pytest.raises(EmbeddingError):
        embedder.embed(["a b"], "query")
