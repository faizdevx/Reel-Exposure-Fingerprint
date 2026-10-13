"""Visual model, visual scoring, fusion, and the combined `pipeline_version` (spec §5.2, N9).

Which SigLIP model? Not decided yet. M0 ran both candidates on 16 synthetic and stock photos
(`docs/m0-ml-bench-summary.md`): SigLIP 2 base was faster and the multilingual SigLIP scored
higher top-1, on a sample far too small to choose. So there is no default here: the model is picked
by name in the deployment settings (`REF_VISUAL_MODEL`), and with no choice the visual stage is off
and the pipeline stays text only. Both candidates below were checked on Hugging Face on 2026-10-08:
licence Apache-2.0, not gated, and the commit SHA is the one the M0 benchmark ran.

Versions:

- Text only (no visual model chosen, no `other` threshold): `pipeline_version` is exactly the text
  pipeline's `pv1-...`. A text-only pipeline that does use an `other` threshold is `pv2-...` too,
  because the threshold changes what is stored.
- Text and visual: `pv2-...`, a hash of the text configuration (which already holds the text model
  id, revision and the taxonomy), the visual model id, revision, licence and dimension, how frames
  are prepared and pooled, how the taxonomy is embedded for the image encoder, `tau_visual`, and the
  fusion weights. Change any of them and the version changes; rows are never overwritten.

ADR-008 (Proposed) would split this into feature, assignment and fingerprint versions. It is not
applied: one string covers the whole chain, as N9 asks.

Known nondeterminism: the CPU kernels of torch are deterministic for a fixed thread count on one
machine, not guaranteed identical across CPU models or torch versions (differences of about 1e-7).
The thread count is part of the version; torch and transformers versions are logged, not hashed.
"""

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from app.models import VISUAL_EMBEDDING_DIM
from app.taxonomy import Taxonomy
from app.worker.config import TextPipelineConfig, canonical_config, canonical_json

VISUAL_MODALITY = "visual"
COMBINED_SCHEMA = "pipeline/2"
MAX_FRAMES_PER_REEL = 5  # spec F7

_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class VisionModelConfig:
    #: Short name used in the settings.
    key: str
    model_id: str
    #: A full commit SHA, never a branch or tag: those move.
    revision: str
    #: SPDX identifier of the licence, as shown on the model card when it was verified.
    license: str
    #: Date the id, revision and licence were checked on Hugging Face (YYYY-MM-DD).
    verified_on: str
    dim: int = VISUAL_EMBEDDING_DIM
    threads: int = 1
    #: Text tower input: padded to this many tokens, as the model cards require.
    text_max_length: int = 64
    #: Wrapped around each English topic description before the text tower. Hindi descriptions are
    #: used as they are.
    text_template: str = "this is a photo of {}."
    #: SigLIP 2 is trained on lower-cased text.
    lowercase_text: bool = True

    def __post_init__(self) -> None:
        if not _SHA.match(self.revision):
            raise ValueError("revision must be a full 40 character commit SHA")
        if self.dim != VISUAL_EMBEDDING_DIM:
            raise ValueError("dim must match the content_features.visual_embedding column")
        if self.threads < 1 or self.text_max_length < 8:
            raise ValueError("threads and text_max_length out of range")
        if "{}" not in self.text_template:
            raise ValueError("text_template needs a {} placeholder")
        if not self.license or not self.verified_on:
            raise ValueError("licence and verification date are required")


# Verified 2026-10-08 on Hugging Face (API): licence apache-2.0, not gated, sha = M0 revision.
SIGLIP2_BASE = VisionModelConfig(
    key="siglip2-base",
    model_id="google/siglip2-base-patch16-224",
    revision="75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2",
    license="apache-2.0",
    verified_on="2026-10-08",
)
SIGLIP_MULTILINGUAL = VisionModelConfig(
    key="siglip-multilingual",
    model_id="google/siglip-base-patch16-256-multilingual",
    revision="8952a4eafcde3cb7ab46b1dd629b33f8784ca9c6",
    license="apache-2.0",
    verified_on="2026-10-08",
    lowercase_text=False,
)
VISION_MODELS: dict[str, VisionModelConfig] = {
    m.key: m for m in (SIGLIP2_BASE, SIGLIP_MULTILINGUAL)
}


@dataclass(frozen=True)
class VisualScoringConfig:
    #: Softmax temperature of the visual modality, tau_visual in spec §5.2. PROVISIONAL: set from
    #: the M0 photo sample and `docs/visual-evaluation.md`; the label audit (M4) tunes it.
    tau_visual: float = 0.02
    #: Languages of the taxonomy descriptions embedded with the image encoder's text tower.
    description_languages: tuple[str, ...] = ("en", "hi")

    def __post_init__(self) -> None:
        if not 0.0 < self.tau_visual <= 1.0:
            raise ValueError("tau_visual must be in (0, 1]")
        if not self.description_languages or any(
            lang not in ("en", "hi") for lang in self.description_languages
        ):
            raise ValueError("description_languages must be a non-empty subset of en, hi")


@dataclass(frozen=True)
class FusionConfig:
    """Late fusion at the probability level: `p(k) = sum_m w_m p_m(k)` over the modalities present,
    with the weights renormalised to 1 over those (spec §5.2). Embeddings are never combined: they
    live in different spaces and have different sizes."""

    w_text: float = 0.5
    w_visual: float = 0.5
    #: Spec §5.2: when `max_k p(k)` is below this, the Reel is assigned to `other`. None means no
    #: Reel is ever assigned to `other`. There is no default value on purpose: a threshold only
    #: means something once the labelled set exists (`app.evaluation`), and a made-up one would
    #: quietly decide what the fingerprints contain. The value is part of the pipeline version.
    other_threshold: float | None = None

    def __post_init__(self) -> None:
        for w in (self.w_text, self.w_visual):
            if not 0.0 < w <= 1.0:
                raise ValueError("fusion weights must be in (0, 1]")
        if self.other_threshold is not None and not 0.0 < self.other_threshold <= 1.0:
            raise ValueError("other_threshold must be in (0, 1]")


@dataclass(frozen=True)
class VisualPipelineConfig:
    model: VisionModelConfig
    scoring: VisualScoringConfig = field(default_factory=VisualScoringConfig)
    max_frames: int = MAX_FRAMES_PER_REEL

    def __post_init__(self) -> None:
        if not 1 <= self.max_frames <= MAX_FRAMES_PER_REEL:
            raise ValueError("max_frames must be between 1 and 5")


def canonical_visual_config(config: VisualPipelineConfig) -> dict[str, Any]:
    return {
        "visual_model": asdict(config.model),
        "frames": {
            "max": config.max_frames,
            "order": "sorted by SHA-256 of the JPEG bytes, so upload order does not matter",
            "preprocessing": "the model's own image processor (resize and normalise), RGB",
        },
        "pooling": "mean of unit frame embeddings, scaled to unit length",
        "topic_embedding": "mean of unit text-tower embeddings of the descriptions, unit length",
        "scoring": {
            "similarity": "cosine",
            "normalisation": "softmax",
            **asdict(config.scoring),
        },
        "unicode_version": unicodedata.unidata_version,
    }


def canonical_combined_config(
    text: TextPipelineConfig,
    taxonomy: Taxonomy,
    visual: VisualPipelineConfig | None,
    fusion: FusionConfig,
) -> dict[str, Any]:
    return {
        "schema": COMBINED_SCHEMA,
        "modalities": ["text", VISUAL_MODALITY] if visual is not None else ["text"],
        "text": canonical_config(text, taxonomy),
        "visual": None if visual is None else canonical_visual_config(visual),
        "fusion": {
            "method": "weighted mean of per-modality probabilities, renormalised over present",
            "other": "p_fused one-hot on `other` when max(p_fused) < other_threshold",
            **asdict(fusion),
        },
    }


def combined_pipeline_version(
    text: TextPipelineConfig,
    taxonomy: Taxonomy,
    visual: VisualPipelineConfig | None,
    fusion: FusionConfig,
) -> str:
    blob = canonical_json(canonical_combined_config(text, taxonomy, visual, fusion))
    return f"pv2-{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]}"
