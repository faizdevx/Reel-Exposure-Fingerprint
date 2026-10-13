"""Builds the text pipeline from deployment settings. The only place that reads `Settings`."""

from app.config import Settings, get_settings
from app.taxonomy import load_taxonomy
from app.worker.config import TextPipelineConfig
from app.worker.embedder import E5Embedder
from app.worker.reel_processor import ReelPipeline
from app.worker.text_pipeline import TextPipeline
from app.worker.vision_config import VISION_MODELS, FusionConfig, VisualPipelineConfig
from app.worker.visual_embedder import SigLipEmbedder
from app.worker.visual_pipeline import VisualPipeline


def build_text_pipeline(
    settings: Settings | None = None, config: TextPipelineConfig | None = None
) -> TextPipeline:
    """Load the taxonomy and wire the e5 embedder. The model itself loads on first use."""
    settings = settings or get_settings()
    config = config or TextPipelineConfig()
    taxonomy = load_taxonomy(settings.taxonomy_file)
    embedder = E5Embedder(
        config.model,
        cache_dir=settings.model_cache_dir,
        local_files_only=settings.hf_local_files_only,
    )
    return TextPipeline.create(config, taxonomy, embedder, settings.embedding_cache_dir)


def build_reel_pipeline(
    settings: Settings | None = None,
    text_config: TextPipelineConfig | None = None,
    fusion: FusionConfig | None = None,
) -> ReelPipeline:
    """Text pipeline, plus the visual one when `REF_VISUAL_MODEL` names a model, plus fusion.

    No visual model chosen means text only; there is no default model.
    """
    settings = settings or get_settings()
    text = build_text_pipeline(settings, text_config)
    visual: VisualPipeline | None = None
    if settings.visual_model is not None:
        config = VisualPipelineConfig(VISION_MODELS[settings.visual_model])
        embedder = SigLipEmbedder(
            config.model,
            cache_dir=settings.model_cache_dir,
            local_files_only=settings.hf_local_files_only,
        )
        visual = VisualPipeline.create(
            config, text.taxonomy, embedder, settings.embedding_cache_dir
        )
    return ReelPipeline(text, visual, fusion or FusionConfig())
