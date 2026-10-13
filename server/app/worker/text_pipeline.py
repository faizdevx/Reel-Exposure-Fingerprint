"""The text stages of the worker pipeline, with a report for each stage.

    caption + hashtags + OCR text  ->  normalize  ->  embed_text (e5)  \\
                                       load_taxonomy_embeddings (cached) ->  score_topics

Failure handling follows spec F9 and the project rules: a stage that fails or has nothing to do does
not stop the stages that do not depend on it.

- No usable text (no caption, no hashtags, no OCR text, or only emoji-free punctuation): `normalize`
  is skipped with reason `no_text` and so is everything after it. That is a normal outcome, not an
  error. Nothing is made up: there is no embedding and no probability.
- The taxonomy embeddings do not depend on the Reel's text, so `load_taxonomy_embeddings` runs
  whenever there is text, whether or not `embed_text` worked. The two can fail independently.
- `score_topics` needs both and is skipped, naming the missing one, if either is missing.

If an embedding for this Reel and pipeline version was stored earlier, pass it as
`cached_embedding`: `normalize` and `embed_text` are then skipped with reason `cached` and only
scoring runs.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.taxonomy import Taxonomy
from app.worker.config import TEXT_MODALITY, TextPipelineConfig, pipeline_version
from app.worker.embedder import EmbeddingError, TextEmbedder, Vectors
from app.worker.scoring import TopicScores, check_unit_vector, score_topics
from app.worker.stages import Outcome, StageReport, StageRunner, StageSkipped
from app.worker.text_normalize import NormalizedText, TextInput, normalize_text
from app.worker.topic_embeddings import TopicEmbeddingProvider, TopicEmbeddings

STAGE_NORMALIZE = "normalize"
STAGE_EMBED = "embed_text"
STAGE_TAXONOMY = "load_taxonomy_embeddings"
STAGE_SCORE = "score_topics"


@dataclass(frozen=True)
class TextResult:
    pipeline_version: str
    reports: tuple[StageReport, ...]
    normalized: NormalizedText | None
    #: Unit-length float32 vector, or None if the text modality did not produce one.
    embedding: Vectors | None
    scores: TopicScores | None

    @property
    def modalities(self) -> tuple[str, ...]:
        return (TEXT_MODALITY,) if self.embedding is not None else ()


class TextPipeline:
    def __init__(
        self,
        config: TextPipelineConfig,
        taxonomy: Taxonomy,
        embedder: TextEmbedder,
        topics: TopicEmbeddingProvider,
    ) -> None:
        self.config = config
        self.taxonomy = taxonomy
        self.embedder = embedder
        self.topics = topics
        self.version = pipeline_version(config, taxonomy)

    @classmethod
    def create(
        cls,
        config: TextPipelineConfig,
        taxonomy: Taxonomy,
        embedder: TextEmbedder,
        cache_dir: Path | None,
    ) -> "TextPipeline":
        return cls(
            config,
            taxonomy,
            embedder,
            TopicEmbeddingProvider(embedder, config, taxonomy, cache_dir),
        )

    # ---- stage bodies

    def _normalize(self, inp: TextInput) -> NormalizedText:
        result = normalize_text(inp, self.config.normalizer)
        if result.is_empty:
            raise StageSkipped("no_text")
        return result

    def _embed(self, text: str) -> Vectors:
        vectors = self.embedder.embed([text], "query")  # one text per call: see embedder.py
        if vectors.shape != (1, self.config.model.dim):
            raise EmbeddingError("unexpected embedding shape")
        vector: Vectors = vectors[0].astype(np.float32)
        check_unit_vector(vector, self.config.model.dim)
        return vector

    def _score(self, embedding: Vectors, topics: TopicEmbeddings) -> TopicScores:
        return score_topics(
            embedding, topics.topic_ids, topics.matrix, self.config.scoring.tau_text
        )

    # ---- the run

    def run(
        self,
        inp: TextInput,
        *,
        runner: StageRunner | None = None,
        cached_embedding: Vectors | None = None,
    ) -> TextResult:
        runner = runner or StageRunner()
        normalized: NormalizedText | None = None
        embedding: Vectors | None = None
        has_text = False
        why_no_text = "no_text"

        if cached_embedding is not None:
            runner.skip(STAGE_NORMALIZE, "cached")
            runner.skip(STAGE_EMBED, "cached")
            embedding, has_text = cached_embedding, True
        else:
            n: Outcome[NormalizedText] = runner.run(STAGE_NORMALIZE, lambda: self._normalize(inp))
            if n.ok and n.value is not None:
                normalized, has_text = n.value, True
                text = normalized.text
                e = runner.run(STAGE_EMBED, lambda: self._embed(text))
                embedding = e.value if e.ok else None
            else:
                why_no_text = "no_text" if n.report.status == "skipped" else "upstream_normalize"
                runner.skip(STAGE_EMBED, why_no_text)

        topics: TopicEmbeddings | None = None
        if has_text:
            t = runner.run(STAGE_TAXONOMY, self.topics.get)
            topics = t.value if t.ok else None
        else:
            runner.skip(STAGE_TAXONOMY, why_no_text)

        scores: TopicScores | None = None
        if embedding is not None and topics is not None:
            captured_embedding, captured_topics = embedding, topics
            s = runner.run(STAGE_SCORE, lambda: self._score(captured_embedding, captured_topics))
            scores = s.value if s.ok else None
        elif not has_text:
            runner.skip(STAGE_SCORE, why_no_text)
        else:
            runner.skip(
                STAGE_SCORE, f"upstream_{STAGE_EMBED if embedding is None else STAGE_TAXONOMY}"
            )

        return TextResult(
            pipeline_version=self.version,
            reports=tuple(runner.reports),
            normalized=normalized,
            embedding=embedding,
            scores=scores,
        )
