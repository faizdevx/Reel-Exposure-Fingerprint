"""Text pipeline configuration and `pipeline_version` (spec N9, CONTRIBUTING.md reproducibility rules).

`pipeline_version` is a short hash of the canonical configuration: the text model with its pinned
Hugging Face revision and every setting that changes its output, the normaliser and its Unicode
version, the taxonomy version and content hash, and the scoring temperature. Change any of them and
the version changes: results are never silently overwritten, they are written under the new
version. Paths, cache locations and credentials are not part of the configuration.

These values are definitions of the pipeline, not deployment settings, so they live here and not in
the environment (an environment variable that changes a model would change results without
changing the version). Deployment settings (cache directories, the taxonomy file location) are in
`app.config`.

What the version does NOT cover yet: fusion weights, the `other` threshold, half-life and the
prediction parameters. Those stages do not exist; ADR-008 (Proposed) decides how they join the
version, so this single string covers only the text stages built so far.

Known nondeterminism: the CPU kernels of torch are deterministic for a fixed thread count on a
given machine, but not guaranteed identical across CPU models or torch versions (differences of
the order of 1e-7 per value). The thread count is part of the version; the torch and transformers
versions are logged by the embedder, not hashed.
"""

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.models import TEXT_EMBEDDING_DIM
from app.taxonomy import Taxonomy, taxonomy_hash
from app.worker.text_normalize import NORMALIZER_VERSION, NormalizeConfig

PIPELINE_SCHEMA = "text-pipeline/1"
TEXT_MODALITY = "text"

# Verified 2026-10-07 on Hugging Face: MIT licence, 384 dimensions, XLM-R tokenizer. This is the
# commit the M0 benchmark ran (spikes/ml-bench/results). Check again before changing it.
E5_MODEL_ID = "intfloat/multilingual-e5-small"
E5_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"

_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class TextModelConfig:
    model_id: str = E5_MODEL_ID
    #: A full commit SHA, never a branch or tag name: those move.
    revision: str = E5_REVISION
    dim: int = TEXT_EMBEDDING_DIM
    max_length: int = 512
    #: e5 needs these prefixes: queries for Reel text, passages for topic descriptions.
    query_prefix: str = "query: "
    passage_prefix: str = "passage: "
    pooling: Literal["mean"] = "mean"
    threads: int = 1

    def __post_init__(self) -> None:
        if not _SHA.match(self.revision):
            raise ValueError("revision must be a full 40 character commit SHA")
        if self.dim != TEXT_EMBEDDING_DIM:
            raise ValueError("dim must match the content_features.text_embedding column")
        if self.threads < 1 or self.max_length < 8:
            raise ValueError("threads and max_length out of range")


@dataclass(frozen=True)
class ScoringConfig:
    #: Softmax temperature of the text modality, tau_text in spec §5.2. PROVISIONAL. 0.01 is the
    #: temperature e5 was trained with. On 35 easy synthetic captions (tests/text_samples.py, not
    #: the label audit) it gave a top-topic probability of 0.45 to 0.98; 0.02 gave 0.17 to 0.65 and
    #: 0.05 was nearly uniform. The label audit (roadmap E6, M3) tunes it. Changing it changes the
    #: pipeline version.
    tau_text: float = 0.01

    def __post_init__(self) -> None:
        if not 0.0 < self.tau_text <= 1.0:
            raise ValueError("tau_text must be in (0, 1]")


@dataclass(frozen=True)
class TextPipelineConfig:
    model: TextModelConfig = field(default_factory=TextModelConfig)
    normalizer: NormalizeConfig = field(default_factory=NormalizeConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)


def canonical_config(config: TextPipelineConfig, taxonomy: Taxonomy) -> dict[str, Any]:
    """Everything that determines the output, as plain JSON-able data."""
    return {
        "schema": PIPELINE_SCHEMA,
        "modalities": [TEXT_MODALITY],
        "text_model": asdict(config.model),
        "normalizer": {
            "version": NORMALIZER_VERSION,
            "unicode_version": unicodedata.unidata_version,
            **asdict(config.normalizer),
        },
        "taxonomy": {"version": taxonomy.taxonomy_version, "hash": taxonomy_hash(taxonomy)},
        "topic_embedding": "mean of unit description embeddings (English and Hindi), unit length",
        "scoring": {"similarity": "cosine", "normalisation": "softmax", **asdict(config.scoring)},
    }


def canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def pipeline_version(config: TextPipelineConfig, taxonomy: Taxonomy) -> str:
    digest = hashlib.sha256(canonical_json(canonical_config(config, taxonomy)).encode("utf-8"))
    return f"pv1-{digest.hexdigest()[:16]}"
