"""Visual embedding and scoring: frame pooling, taxonomy embeddings in the image space, caching,
independent failure. Uses a fake SigLIP-shaped encoder (no model, no database)."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from worker_fakes import FakeClock, FakeImageEmbedder, fake_frame

from app.taxonomy import Taxonomy, load_taxonomy, parse_taxonomy
from app.worker.embedder import EmbeddingError
from app.worker.stages import StageRunner
from app.worker.vision_config import (
    SIGLIP2_BASE,
    VisualPipelineConfig,
    VisualScoringConfig,
)
from app.worker.visual_pipeline import (
    STAGE_EMBED_FRAMES,
    STAGE_POOL,
    STAGE_SCORE_VISUAL,
    STAGE_VISUAL_TAXONOMY,
    VisualPipeline,
    pool_embeddings,
    select_frames,
)
from app.worker.visual_topics import (
    VisualTopicProvider,
    build_visual_topic_embeddings,
    load_or_build_visual_topic_embeddings,
    visual_topic_key,
)

TAXONOMY_FILE = Path(__file__).resolve().parents[1] / "taxonomy" / "topics.json"
CONFIG = VisualPipelineConfig(SIGLIP2_BASE)


@pytest.fixture(scope="module")
def taxonomy() -> Taxonomy:
    return load_taxonomy(TAXONOMY_FILE)


def make(
    taxonomy: Taxonomy,
    embedder: FakeImageEmbedder | None = None,
    cache_dir: Path | None = None,
    config: VisualPipelineConfig = CONFIG,
) -> tuple[VisualPipeline, FakeImageEmbedder]:
    embedder = embedder or FakeImageEmbedder()
    return VisualPipeline.create(config, taxonomy, embedder, cache_dir), embedder


def statuses(reports: tuple) -> dict[str, str]:  # type: ignore[type-arg]
    return {r.stage: r.status for r in reports}


# ------------------------------------------------------------------ taxonomy in the image space


def test_one_unit_vector_per_topic_in_the_image_space(taxonomy: Taxonomy) -> None:
    matrix = build_visual_topic_embeddings(FakeImageEmbedder(), CONFIG, taxonomy)
    assert matrix.shape == (len(taxonomy.topics), 768)
    assert matrix.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)


def test_english_descriptions_get_the_prompt_template_and_hindi_ones_do_not(
    taxonomy: Taxonomy,
) -> None:
    embedder = FakeImageEmbedder()
    build_visual_topic_embeddings(embedder, CONFIG, taxonomy)
    first = embedder.text_calls[0]
    topic = taxonomy.topics[0]
    assert len(first) == len(topic.descriptions.en) + len(topic.descriptions.hi)
    english, hindi = first[: len(topic.descriptions.en)], first[len(topic.descriptions.en) :]
    assert all(t.startswith("this is a photo of ") and t.endswith(".") for t in english)
    assert not any(t.startswith("this is a photo of ") for t in hindi)


def test_the_languages_setting_selects_which_descriptions_are_used(taxonomy: Taxonomy) -> None:
    only_en = replace(CONFIG, scoring=VisualScoringConfig(description_languages=("en",)))
    embedder = FakeImageEmbedder()
    build_visual_topic_embeddings(embedder, only_en, taxonomy)
    assert len(embedder.text_calls[0]) == len(taxonomy.topics[0].descriptions.en)


def test_a_wrongly_shaped_text_tower_is_an_error(taxonomy: Taxonomy) -> None:
    class Short(FakeImageEmbedder):
        def embed_texts(self, texts):  # type: ignore[no-untyped-def]
            return super().embed_texts(texts)[:-1]

    with pytest.raises(EmbeddingError):
        build_visual_topic_embeddings(Short(), CONFIG, taxonomy)


def test_the_key_depends_on_the_model_the_template_and_the_taxonomy(taxonomy: Taxonomy) -> None:
    base = visual_topic_key(CONFIG, taxonomy)
    assert base == visual_topic_key(CONFIG, taxonomy)
    other_model = replace(CONFIG, model=replace(SIGLIP2_BASE, revision="1" * 40))
    other_template = replace(CONFIG, model=replace(SIGLIP2_BASE, text_template="a {}."))
    other_langs = replace(CONFIG, scoring=VisualScoringConfig(description_languages=("en",)))
    raw = json.loads(TAXONOMY_FILE.read_text(encoding="utf-8"))
    reworded = copy.deepcopy(raw)
    reworded["topics"][0]["descriptions"]["en"][0] += " today"
    keys = {
        base,
        visual_topic_key(other_model, taxonomy),
        visual_topic_key(other_template, taxonomy),
        visual_topic_key(other_langs, taxonomy),
        visual_topic_key(CONFIG, parse_taxonomy(json.dumps(reworded))),
    }
    assert len(keys) == 5


def test_the_key_ignores_settings_that_do_not_change_the_vectors(taxonomy: Taxonomy) -> None:
    other_tau = replace(CONFIG, scoring=VisualScoringConfig(tau_visual=0.1))
    other_frames = replace(CONFIG, max_frames=3)
    assert visual_topic_key(other_tau, taxonomy) == visual_topic_key(CONFIG, taxonomy)
    assert visual_topic_key(other_frames, taxonomy) == visual_topic_key(CONFIG, taxonomy)


def test_the_taxonomy_embeddings_are_cached_on_disk_and_reused(
    taxonomy: Taxonomy, tmp_path: Path
) -> None:
    first_embedder = FakeImageEmbedder()
    first = load_or_build_visual_topic_embeddings(first_embedder, CONFIG, taxonomy, tmp_path)
    assert not first.from_cache
    assert first_embedder.text_calls
    files = list(tmp_path.glob("visual-topic-embeddings-*.npz"))
    assert len(files) == 1

    second_embedder = FakeImageEmbedder()
    second = load_or_build_visual_topic_embeddings(second_embedder, CONFIG, taxonomy, tmp_path)
    assert second.from_cache
    assert second_embedder.text_calls == []  # the text tower did not run
    np.testing.assert_array_equal(first.matrix, second.matrix)


def test_a_different_model_does_not_reuse_the_cache_file(
    taxonomy: Taxonomy, tmp_path: Path
) -> None:
    load_or_build_visual_topic_embeddings(FakeImageEmbedder(), CONFIG, taxonomy, tmp_path)
    other = replace(CONFIG, model=replace(SIGLIP2_BASE, revision="2" * 40))
    result = load_or_build_visual_topic_embeddings(FakeImageEmbedder(), other, taxonomy, tmp_path)
    assert not result.from_cache
    assert len(list(tmp_path.glob("visual-topic-embeddings-*.npz"))) == 2


def test_the_text_and_visual_caches_do_not_collide(taxonomy: Taxonomy, tmp_path: Path) -> None:
    load_or_build_visual_topic_embeddings(FakeImageEmbedder(), CONFIG, taxonomy, tmp_path)
    assert not list(tmp_path.glob("topic-embeddings-*.npz"))  # the e5 cache uses another name


def test_a_damaged_cache_file_is_rebuilt(taxonomy: Taxonomy, tmp_path: Path) -> None:
    load_or_build_visual_topic_embeddings(FakeImageEmbedder(), CONFIG, taxonomy, tmp_path)
    (path,) = tmp_path.glob("visual-topic-embeddings-*.npz")
    path.write_bytes(b"not a zip file")
    again = load_or_build_visual_topic_embeddings(FakeImageEmbedder(), CONFIG, taxonomy, tmp_path)
    assert not again.from_cache


def test_the_provider_loads_once_and_does_not_remember_a_failure(
    taxonomy: Taxonomy, tmp_path: Path
) -> None:
    embedder = FakeImageEmbedder()
    embedder.fail_texts = lambda _t: RuntimeError("boom")
    provider = VisualTopicProvider(embedder, CONFIG, taxonomy, tmp_path)
    with pytest.raises(RuntimeError):
        provider.get()
    embedder.fail_texts = None
    first = provider.get()
    assert provider.get() is first
    calls = len(embedder.text_calls)
    provider.get()
    assert len(embedder.text_calls) == calls


# ------------------------------------------------------------------ frame selection and pooling


def test_frames_are_ordered_by_hash_deduplicated_and_capped() -> None:
    frames = [fake_frame(f"scene {i}") for i in range(8)]
    chosen = select_frames(frames + frames[:3], 5)
    assert len(chosen) == 5
    assert select_frames(list(reversed(frames)), 5) == chosen  # upload order does not matter
    assert len(select_frames([frames[0], frames[0]], 5)) == 1
    assert select_frames([b"", b""], 5) == []


def test_pooling_is_the_mean_of_unit_vectors_scaled_to_unit_length() -> None:
    vectors = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    pooled = pool_embeddings(vectors)
    np.testing.assert_allclose(pooled, [2**-0.5, 2**-0.5], atol=1e-6)
    np.testing.assert_allclose(np.linalg.norm(pooled), 1.0, atol=1e-6)


def test_pooling_does_not_depend_on_order() -> None:
    rng = np.random.default_rng(1)
    raw = rng.standard_normal((5, 16)).astype(np.float32)
    unit = (raw / np.linalg.norm(raw, axis=1, keepdims=True)).astype(np.float32)
    np.testing.assert_allclose(pool_embeddings(unit), pool_embeddings(unit[::-1]), atol=1e-6)


def test_one_frame_pools_to_itself() -> None:
    v = np.array([[0.6, 0.8]], dtype=np.float32)
    np.testing.assert_allclose(pool_embeddings(v), [0.6, 0.8], atol=1e-6)


def test_opposite_frames_that_cancel_are_an_error_not_a_vector() -> None:
    with pytest.raises(EmbeddingError):
        pool_embeddings(np.array([[1.0, 0.0], [-1.0, 0.0]], dtype=np.float32))
    with pytest.raises(EmbeddingError):
        pool_embeddings(np.zeros((0, 4), dtype=np.float32))


# ------------------------------------------------------------------ the pipeline


def test_a_reel_gets_one_pooled_unit_embedding_and_a_distribution(taxonomy: Taxonomy) -> None:
    pipeline, _ = make(taxonomy)
    result = pipeline.run([fake_frame("cricket bat ball stadium"), fake_frame("cricket match six")])
    assert result.embedding is not None
    assert result.embedding.shape == (768,)
    np.testing.assert_allclose(np.linalg.norm(result.embedding), 1.0, atol=1e-5)
    assert result.scores is not None
    assert sum(result.scores.probabilities) == pytest.approx(1.0)
    assert len(result.scores.probabilities) == len(taxonomy.topics)
    assert result.frames_used == 2 and result.frames_failed == 0
    assert result.modalities == ("visual",)


def test_the_pooled_embedding_is_the_pooled_per_frame_embeddings(taxonomy: Taxonomy) -> None:
    pipeline, embedder = make(taxonomy)
    frames = [fake_frame("cricket bat"), fake_frame("cricket ball"), fake_frame("stadium crowd")]
    result = pipeline.run(frames)
    singles = np.concatenate([embedder.embed_images([f]) for f in select_frames(frames, 5)])
    np.testing.assert_allclose(result.embedding, pool_embeddings(singles), atol=1e-6)


def test_frame_order_and_duplicates_do_not_change_the_result(taxonomy: Taxonomy) -> None:
    pipeline, _ = make(taxonomy)
    frames = [fake_frame("cricket bat"), fake_frame("cricket ball"), fake_frame("stadium crowd")]
    a = pipeline.run(frames)
    b = pipeline.run([*reversed(frames), frames[0]])
    np.testing.assert_array_equal(a.embedding, b.embedding)
    assert a.scores == b.scores


def test_frames_are_embedded_one_at_a_time(taxonomy: Taxonomy) -> None:
    pipeline, embedder = make(taxonomy)
    pipeline.run([fake_frame("a"), fake_frame("b"), fake_frame("c")])
    assert [len(call) for call in embedder.image_calls] == [1, 1, 1]


def test_at_most_five_frames_are_used(taxonomy: Taxonomy) -> None:
    pipeline, embedder = make(taxonomy)
    result = pipeline.run([fake_frame(f"scene {i}") for i in range(9)])
    assert result.frames_used == 5
    assert len(embedder.image_calls) == 5


def test_scoring_is_zero_shot_against_the_same_taxonomy(taxonomy: Taxonomy) -> None:
    # A frame whose content words are those of a topic's descriptions lands on that topic.
    pipeline, _ = make(taxonomy)
    topic = next(t for t in taxonomy.topics if t.slug == "cricket")
    words = " ".join(topic.descriptions.en[:2])
    result = pipeline.run([fake_frame(words)])
    assert result.scores is not None
    top_topic, top_p = result.scores.ranked()[0]
    assert top_topic == topic.id
    assert top_p > 1 / len(taxonomy.topics)


def test_visual_probabilities_use_the_visual_temperature(taxonomy: Taxonomy) -> None:
    sharp = replace(CONFIG, scoring=VisualScoringConfig(tau_visual=0.01))
    flat = replace(CONFIG, scoring=VisualScoringConfig(tau_visual=0.5))
    frame = [fake_frame("cricket bat ball")]
    top_sharp = make(taxonomy, config=sharp)[0].run(frame).scores.ranked()[0][1]  # type: ignore[union-attr]
    top_flat = make(taxonomy, config=flat)[0].run(frame).scores.ranked()[0][1]  # type: ignore[union-attr]
    assert top_sharp > top_flat


def test_the_image_and_text_spaces_are_not_mixed(taxonomy: Taxonomy) -> None:
    # The e5 text embedder has 384 numbers, SigLIP 768: scoring them against each other fails
    # loudly instead of producing a number.
    from worker_fakes import FakeEmbedder

    from app.worker.scoring import ScoringError, score_topics

    image_vec = FakeImageEmbedder().embed_images([fake_frame("cricket")])[0]
    text_topics = FakeEmbedder(384).embed(["cricket"] * 3, "passage")
    with pytest.raises(ScoringError):
        score_topics(image_vec, (1, 2, 3), text_topics, 0.02)


# ------------------------------------------------------------------ stage reports


def test_every_stage_reports_status_times_and_duration(taxonomy: Taxonomy) -> None:
    clock = FakeClock()
    pipeline, _ = make(taxonomy)
    result = pipeline.run([fake_frame("cricket")], runner=StageRunner(clock.clocks()))
    assert [r.stage for r in result.reports] == [
        STAGE_EMBED_FRAMES,
        STAGE_POOL,
        STAGE_VISUAL_TAXONOMY,
        STAGE_SCORE_VISUAL,
    ]
    for report in result.reports:
        assert report.status == "ok"
        assert report.started_at <= report.finished_at
        assert report.duration_ms >= 0
        assert report.error is None


def test_no_frames_skips_every_visual_stage_without_error(taxonomy: Taxonomy) -> None:
    pipeline, embedder = make(taxonomy)
    result = pipeline.run([])
    assert result.embedding is None and result.scores is None
    assert all(r.status == "skipped" and r.reason == "no_frames" for r in result.reports)
    assert embedder.image_calls == [] and embedder.text_calls == []  # no model work at all


def test_empty_byte_strings_count_as_no_frames(taxonomy: Taxonomy) -> None:
    pipeline, _ = make(taxonomy)
    result = pipeline.run([b"", b""])
    assert result.embedding is None
    assert result.reports[0].reason == "no_frames"


def test_one_undecodable_frame_is_dropped_and_the_reel_is_embedded_from_the_rest(
    taxonomy: Taxonomy,
) -> None:
    pipeline, _ = make(taxonomy)
    result = pipeline.run([fake_frame("cricket bat"), fake_frame("CORRUPT"), fake_frame("cricket")])
    assert result.embedding is not None and result.scores is not None
    assert (result.frames_used, result.frames_failed) == (2, 1)
    assert statuses(result.reports)[STAGE_EMBED_FRAMES] == "ok"


def test_when_no_frame_works_embedding_fails_and_the_dependents_are_skipped(
    taxonomy: Taxonomy,
) -> None:
    pipeline, _ = make(taxonomy)
    result = pipeline.run([fake_frame("CORRUPT one"), fake_frame("CORRUPT two")])
    by_stage = {r.stage: r for r in result.reports}
    assert by_stage[STAGE_EMBED_FRAMES].status == "failed"
    assert by_stage[STAGE_EMBED_FRAMES].error == "EmbeddingError"
    assert by_stage[STAGE_POOL].reason == f"upstream_{STAGE_EMBED_FRAMES}"
    assert by_stage[STAGE_SCORE_VISUAL].reason == f"upstream_{STAGE_POOL}"
    assert result.embedding is None and result.scores is None
    assert result.frames_failed == 2


def test_the_taxonomy_loads_even_when_the_frames_fail(taxonomy: Taxonomy) -> None:
    pipeline, _ = make(taxonomy)
    result = pipeline.run([fake_frame("CORRUPT")])
    assert statuses(result.reports)[STAGE_VISUAL_TAXONOMY] == "ok"


def test_a_failing_taxonomy_load_does_not_cost_the_embedding(taxonomy: Taxonomy) -> None:
    embedder = FakeImageEmbedder()
    embedder.fail_texts = lambda _t: RuntimeError("text tower down")
    pipeline, _ = make(taxonomy, embedder)
    result = pipeline.run([fake_frame("cricket")])
    by_stage = {r.stage: r for r in result.reports}
    assert by_stage[STAGE_VISUAL_TAXONOMY].status == "failed"
    assert by_stage[STAGE_VISUAL_TAXONOMY].error == "RuntimeError"
    assert by_stage[STAGE_SCORE_VISUAL].reason == f"upstream_{STAGE_VISUAL_TAXONOMY}"
    assert result.embedding is not None  # kept: it can be stored and scored later
    assert result.scores is None


def test_a_model_that_returns_the_wrong_shape_for_every_frame_is_a_failed_stage(
    taxonomy: Taxonomy,
) -> None:
    class Wrong(FakeImageEmbedder):
        def embed_images(self, jpegs):  # type: ignore[no-untyped-def]
            return super().embed_images(jpegs)[:, :10]

    pipeline, _ = make(taxonomy, Wrong())
    result = pipeline.run([fake_frame("cricket")])
    assert statuses(result.reports)[STAGE_EMBED_FRAMES] == "failed"


def test_reports_never_carry_frame_content(taxonomy: Taxonomy) -> None:
    secret = "PRIVATE-SCENE-DETAILS"
    embedder = FakeImageEmbedder()
    embedder.fail_images = lambda _b: RuntimeError(secret)
    pipeline, _ = make(taxonomy, embedder)
    result = pipeline.run([fake_frame(secret)])
    assert secret not in repr(result.reports)
    assert secret not in str([r.as_dict() for r in result.reports])


# ------------------------------------------------------------------ cached embedding


def test_a_cached_embedding_skips_the_image_tower_and_only_scores(taxonomy: Taxonomy) -> None:
    pipeline, embedder = make(taxonomy)
    first = pipeline.run([fake_frame("cricket bat ball")])
    embedder.image_calls.clear()

    again = pipeline.run([], cached_embedding=first.embedding)

    assert embedder.image_calls == []
    assert [(r.stage, r.status, r.reason) for r in again.reports[:2]] == [
        (STAGE_EMBED_FRAMES, "skipped", "cached"),
        (STAGE_POOL, "skipped", "cached"),
    ]
    assert again.scores == first.scores
    np.testing.assert_array_equal(again.embedding, first.embedding)
