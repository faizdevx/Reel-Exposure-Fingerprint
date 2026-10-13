"""Visual model registry and the combined `pipeline_version` (spec N9). No model, no database."""

from dataclasses import replace
from pathlib import Path

import pytest

from app.taxonomy import Taxonomy, load_taxonomy
from app.worker.config import TextModelConfig, TextPipelineConfig, pipeline_version
from app.worker.vision_config import (
    SIGLIP2_BASE,
    SIGLIP_MULTILINGUAL,
    VISION_MODELS,
    FusionConfig,
    VisualPipelineConfig,
    VisualScoringConfig,
    canonical_combined_config,
    combined_pipeline_version,
)

TAXONOMY_FILE = Path(__file__).resolve().parents[1] / "taxonomy" / "topics.json"


@pytest.fixture(scope="module")
def taxonomy() -> Taxonomy:
    return load_taxonomy(TAXONOMY_FILE)


TEXT = TextPipelineConfig()
FUSION = FusionConfig()


def version(
    taxonomy: Taxonomy,
    visual: VisualPipelineConfig | None = None,
    fusion: FusionConfig = FUSION,
    text: TextPipelineConfig = TEXT,
) -> str:
    return combined_pipeline_version(
        text, taxonomy, visual or VisualPipelineConfig(SIGLIP2_BASE), fusion
    )


# ---- the registry


def test_both_candidates_are_registered_with_pinned_revisions_and_licences() -> None:
    assert set(VISION_MODELS) == {"siglip2-base", "siglip-multilingual"}
    for model in VISION_MODELS.values():
        assert len(model.revision) == 40
        assert model.license == "apache-2.0"
        assert model.verified_on == "2026-10-08"
        assert model.dim == 768


def test_the_revisions_are_the_ones_the_m0_benchmark_ran() -> None:
    assert SIGLIP2_BASE.model_id == "google/siglip2-base-patch16-224"
    assert SIGLIP2_BASE.revision == "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2"
    assert SIGLIP_MULTILINGUAL.model_id == "google/siglip-base-patch16-256-multilingual"
    assert SIGLIP_MULTILINGUAL.revision == "8952a4eafcde3cb7ab46b1dd629b33f8784ca9c6"


@pytest.mark.parametrize(
    "change",
    [
        {"revision": "main"},
        {"revision": "abc123"},
        {"dim": 512},
        {"threads": 0},
        {"text_template": "no placeholder"},
        {"license": ""},
        {"verified_on": ""},
    ],
)
def test_a_model_config_that_could_break_the_column_or_the_record_is_refused(
    change: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        replace(SIGLIP2_BASE, **change)  # type: ignore[arg-type]


def test_scoring_fusion_and_pipeline_configs_refuse_nonsense() -> None:
    for bad in (0.0, -1.0, 1.5):
        with pytest.raises(ValueError):
            VisualScoringConfig(tau_visual=bad)
    with pytest.raises(ValueError):
        VisualScoringConfig(description_languages=())
    with pytest.raises(ValueError):
        VisualScoringConfig(description_languages=("fr",))
    with pytest.raises(ValueError):
        FusionConfig(w_text=0.0)
    with pytest.raises(ValueError):
        VisualPipelineConfig(SIGLIP2_BASE, max_frames=6)
    with pytest.raises(ValueError):
        VisualPipelineConfig(SIGLIP2_BASE, max_frames=0)


# ---- the version records the exact model, licence and every parameter


def test_the_combined_version_is_stable_and_prefixed(taxonomy: Taxonomy) -> None:
    assert version(taxonomy) == version(taxonomy)
    assert version(taxonomy).startswith("pv2-")


def test_the_combined_version_differs_from_the_text_only_version(taxonomy: Taxonomy) -> None:
    assert pipeline_version(TEXT, taxonomy).startswith("pv1-")
    assert version(taxonomy) != pipeline_version(TEXT, taxonomy)


def test_the_canonical_config_names_the_model_revision_and_licence(taxonomy: Taxonomy) -> None:
    config = canonical_combined_config(TEXT, taxonomy, VisualPipelineConfig(SIGLIP2_BASE), FUSION)
    visual = config["visual"]["visual_model"]
    assert visual["model_id"] == "google/siglip2-base-patch16-224"
    assert visual["revision"] == "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2"
    assert visual["license"] == "apache-2.0"
    assert visual["verified_on"] == "2026-10-08"
    # The text model rides along, so one string pins both encoders.
    assert config["text"]["text_model"]["model_id"] == "intfloat/multilingual-e5-small"
    assert config["modalities"] == ["text", "visual"]


def test_the_two_candidate_models_have_different_versions(taxonomy: Taxonomy) -> None:
    a = version(taxonomy, VisualPipelineConfig(SIGLIP2_BASE))
    b = version(taxonomy, VisualPipelineConfig(SIGLIP_MULTILINGUAL))
    assert a != b


def test_every_visual_and_fusion_parameter_changes_the_version(taxonomy: Taxonomy) -> None:
    base = version(taxonomy)
    changed = {
        version(taxonomy, VisualPipelineConfig(replace(SIGLIP2_BASE, revision="0" * 40))),
        version(taxonomy, VisualPipelineConfig(replace(SIGLIP2_BASE, license="mit"))),
        version(taxonomy, VisualPipelineConfig(replace(SIGLIP2_BASE, threads=2))),
        version(taxonomy, VisualPipelineConfig(replace(SIGLIP2_BASE, text_template="a {}."))),
        version(taxonomy, VisualPipelineConfig(replace(SIGLIP2_BASE, text_max_length=32))),
        version(taxonomy, VisualPipelineConfig(SIGLIP2_BASE, VisualScoringConfig(tau_visual=0.05))),
        version(
            taxonomy,
            VisualPipelineConfig(SIGLIP2_BASE, VisualScoringConfig(description_languages=("en",))),
        ),
        version(taxonomy, VisualPipelineConfig(SIGLIP2_BASE, max_frames=3)),
        version(taxonomy, fusion=FusionConfig(w_text=0.3, w_visual=0.7)),
    }
    assert base not in changed
    assert len(changed) == 9


def test_a_text_side_change_also_changes_the_combined_version(taxonomy: Taxonomy) -> None:
    other_text = TextPipelineConfig(model=TextModelConfig(threads=2))
    assert version(taxonomy, text=other_text) != version(taxonomy)


def test_a_reworded_taxonomy_changes_the_combined_version(taxonomy: Taxonomy) -> None:
    import copy
    import json

    from app.taxonomy import parse_taxonomy

    raw = {
        "taxonomy_version": taxonomy.taxonomy_version,
        "topics": [
            {
                "id": t.id,
                "slug": t.slug,
                "label": t.label,
                "taxonomy_version": t.taxonomy_version,
                "descriptions": {"en": list(t.descriptions.en), "hi": list(t.descriptions.hi)},
            }
            for t in taxonomy.topics
        ],
    }
    reworded = copy.deepcopy(raw)
    reworded["topics"][0]["descriptions"]["en"][0] += " today"
    assert version(parse_taxonomy(json.dumps(reworded))) != version(parse_taxonomy(json.dumps(raw)))


def test_the_visual_model_choice_has_no_default() -> None:
    from app.config import Settings

    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/x")  # type: ignore[arg-type]
    assert settings.visual_model is None


# ---- the `other` threshold in the version


def test_the_threshold_changes_the_version_and_is_recorded(taxonomy: Taxonomy) -> None:
    with_threshold = version(taxonomy, fusion=FusionConfig(other_threshold=0.3))
    assert with_threshold != version(taxonomy)
    assert version(taxonomy, fusion=FusionConfig(other_threshold=0.3)) != version(
        taxonomy, fusion=FusionConfig(other_threshold=0.35)
    )
    config = canonical_combined_config(
        TEXT, taxonomy, VisualPipelineConfig(SIGLIP2_BASE), FusionConfig(other_threshold=0.3)
    )
    assert config["fusion"]["other_threshold"] == 0.3
    assert config["fusion"]["w_text"] == 0.5 and config["fusion"]["w_visual"] == 0.5


def test_a_text_only_pipeline_with_a_threshold_is_not_the_old_text_only_version(
    taxonomy: Taxonomy,
) -> None:
    plain = pipeline_version(TEXT, taxonomy)
    with_other = combined_pipeline_version(TEXT, taxonomy, None, FusionConfig(other_threshold=0.3))
    assert with_other.startswith("pv2-") and with_other != plain
    config = canonical_combined_config(TEXT, taxonomy, None, FusionConfig(other_threshold=0.3))
    assert config["modalities"] == ["text"] and config["visual"] is None
