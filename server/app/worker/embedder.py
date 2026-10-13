"""Text embeddings with multilingual-e5-small (spec §5.2, §6, ADR-009).

`TextEmbedder` is the interface the pipeline depends on; `E5Embedder` is the real model. Tests use a
fake with the same interface, so the pipeline logic needs no model download.

e5 rules, from the model card: texts must carry a prefix (`query: ` for the thing being classified,
`passage: ` for the documents it is compared with); the sentence vector is the mean of the last
hidden states over the non-padding tokens; vectors are L2-normalised so a dot product is a cosine.
Inference runs in eval mode under `torch.inference_mode()`, with a fixed thread count and no
sampling, so the same text gives the same vector.

The Reel text is embedded one text per call (batch of one), so a vector never depends on which
other Reels happened to be processed with it. Padding inside a batch changes values at the 1e-7
level; the taxonomy embeddings use one fixed batch per topic.

torch and transformers are optional dependencies (`uv sync --group ml`), imported only when the
model is first used.
"""

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
import numpy.typing as npt

from app.worker.config import TextModelConfig

log = logging.getLogger("app.worker.embedder")

EmbedKind = Literal["query", "passage"]
Vectors = npt.NDArray[np.float32]


class EmbeddingError(Exception):
    """The model produced something unusable (wrong shape, NaN, zero vector) or could not load."""


class TextEmbedder(Protocol):
    @property
    def dim(self) -> int: ...

    def embed(self, texts: Sequence[str], kind: EmbedKind) -> Vectors:
        """One unit-length float32 vector per text, shape `(len(texts), dim)`."""
        ...


def mean_pool(hidden: npt.NDArray[np.float32], mask: npt.NDArray[np.integer[Any]]) -> Vectors:
    """Mean of token vectors over the tokens the mask keeps. `hidden` is (n, tokens, dim)."""
    weights = mask[..., None].astype(np.float32)
    counts = weights.sum(axis=1)
    if bool((counts == 0).any()):
        raise EmbeddingError("a text had no tokens")
    pooled: Vectors = ((hidden * weights).sum(axis=1) / counts).astype(np.float32)
    return pooled


def l2_normalize(vectors: npt.NDArray[np.float32]) -> Vectors:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if not bool(np.isfinite(norms).all()) or bool((norms == 0).any()):
        raise EmbeddingError("a vector was zero or not finite")
    unit: Vectors = (vectors / norms).astype(np.float32)
    return unit


class E5Embedder:
    def __init__(
        self,
        config: TextModelConfig,
        *,
        cache_dir: Path | None = None,
        local_files_only: bool = False,
        batch_size: int = 16,
    ) -> None:
        self._config = config
        self._cache_dir = cache_dir
        self._local_only = local_files_only
        self._batch_size = max(1, batch_size)
        self._torch: Any = None
        self._tokenizer: Any = None
        self._model: Any = None

    @property
    def dim(self) -> int:
        return self._config.dim

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            import transformers
            from transformers import AutoModel, AutoTokenizer
        except ImportError as error:
            raise EmbeddingError("torch and transformers are not installed") from error

        torch.set_num_threads(self._config.threads)
        kwargs: dict[str, Any] = {
            "revision": self._config.revision,
            "cache_dir": None if self._cache_dir is None else str(self._cache_dir),
            "local_files_only": self._local_only,
        }
        tokenizer = AutoTokenizer.from_pretrained(self._config.model_id, **kwargs)
        model = AutoModel.from_pretrained(self._config.model_id, **kwargs).eval()
        if int(model.config.hidden_size) != self._config.dim:
            raise EmbeddingError("model hidden size does not match the configured dimension")
        self._torch, self._tokenizer, self._model = torch, tokenizer, model
        log.info(
            "text model loaded model=%s revision=%s torch=%s transformers=%s threads=%d",
            self._config.model_id,
            self._config.revision,
            torch.__version__,
            transformers.__version__,
            self._config.threads,
        )

    def embed(self, texts: Sequence[str], kind: EmbedKind) -> Vectors:
        if not texts:
            return np.zeros((0, self._config.dim), dtype=np.float32)
        self._load()
        prefix = self._config.query_prefix if kind == "query" else self._config.passage_prefix
        batches: list[Vectors] = []
        for start in range(0, len(texts), self._batch_size):
            batch = [f"{prefix}{t}" for t in texts[start : start + self._batch_size]]
            batches.append(self._embed_batch(batch))
        vectors = np.concatenate(batches, axis=0)
        if vectors.shape != (len(texts), self._config.dim):
            raise EmbeddingError("unexpected embedding shape")
        return vectors

    def _embed_batch(self, batch: list[str]) -> Vectors:
        encoded = self._tokenizer(
            batch,
            padding=True,
            truncation=True,
            max_length=self._config.max_length,
            return_tensors="pt",
        )
        with self._torch.inference_mode():
            hidden = self._model(**encoded).last_hidden_state
        hidden_np = hidden.float().cpu().numpy()
        mask_np = encoded["attention_mask"].cpu().numpy()
        return l2_normalize(mean_pool(hidden_np, mask_np))
