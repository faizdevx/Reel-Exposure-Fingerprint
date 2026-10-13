"""Taxonomy embedding tests: construction and the on-disk cache. Uses a fake embedder, no model."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from worker_fakes import FakeEmbedder

from app.taxonomy import Taxonomy, load_taxonomy, parse_taxonomy
from app.worker.config import ScoringConfig, TextModelConfig, TextPipelineConfig
from app.worker.embedder import EmbeddingError
from app.worker.text_normalize import NormalizeConfig, normalize_fragment
from app.worker.topic_embeddings import (
    TopicEmbeddingProvider,
    build_topic_embeddings,
    load_or_build_topic_embeddings,
    topic_embeddings_key,
)

TAXONOMY_FILE = Path(__file__).resolve().parents[1] / "taxonomy" / "topics.json"


@pytest.fixture(scope="module")
def taxonomy() -> Taxonomy:
    return load_taxonomy(TAXONOMY_FILE)


CONFIG = TextPipelineConfig()


def cache_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*"))


# ------------------------------------------------------------------ construction


def test_one_unit_vector_per_topic_in_taxonomy_order(taxonomy: Taxonomy) -> None:
    matrix = build_topic_embeddings(FakeEmbedder(), taxonomy)
    assert matrix.shape == (len(taxonomy.topics), 384)
    assert matrix.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), 1.0, rtol=1e-5)


def test_a_topic_vector_is_the_mean_of_its_unit_description_vectors(taxonomy: Taxonomy) -> None:
    fake = FakeEmbedder()
    matrix = build_topic_embeddings(fake, taxonomy)
    topic = taxonomy.topics[4]  # cricket
    descriptions = [normalize_fragment(t) for t in (*topic.descriptions.en, *topic.descriptions.hi)]
    mean = fake.embed(descriptions, "passage").mean(axis=0)
    expected = mean / np.linalg.norm(mean)
    np.testing.assert_allclose(matrix[4], expected, atol=1e-6)


def test_english_and_hindi_descriptions_are_pooled_with_the_passage_prefix(
    taxonomy: Taxonomy,
) -> None:
    fake = FakeEmbedder()
    build_topic_embeddings(fake, taxonomy)
    assert {kind for kind, _ in fake.calls} == {"passage"}  # topics are passages, never queries
    assert len(fake.calls) == len(taxonomy.topics)  # one fixed batch per topic
    first = taxonomy.topics[0]
    expected = [normalize_fragment(t) for t in (*first.descriptions.en, *first.descriptions.hi)]
    assert list(fake.calls[0][1]) == expected
    assert len(expected) == len(first.descriptions.en) + len(first.descriptions.hi)


def test_build_is_deterministic(taxonomy: Taxonomy) -> None:
    a = build_topic_embeddings(FakeEmbedder(), taxonomy)
    b = build_topic_embeddings(FakeEmbedder(), taxonomy)
    assert np.array_equal(a, b)


def test_an_embedder_that_returns_too_few_vectors_is_an_error(taxonomy: Taxonomy) -> None:
    fake = FakeEmbedder()
    fake.override = lambda v: v[:-1]
    with pytest.raises(EmbeddingError):
        build_topic_embeddings(fake, taxonomy)


# ------------------------------------------------------------------ the cache key


def test_key_depends_on_everything_that_changes_the_vectors(taxonomy: Taxonomy) -> None:
    base = topic_embeddings_key(CONFIG, taxonomy)
    assert topic_embeddings_key(CONFIG, taxonomy) == base

    def with_model(**kw: object) -> TextPipelineConfig:
        return replace(CONFIG, model=replace(CONFIG.model, **kw))

    assert topic_embeddings_key(with_model(revision="0" * 40), taxonomy) != base
    assert topic_embeddings_key(with_model(model_id="other/model"), taxonomy) != base
    assert topic_embeddings_key(with_model(passage_prefix="doc: "), taxonomy) != base
    assert topic_embeddings_key(with_model(max_length=256), taxonomy) != base
    assert topic_embeddings_key(with_model(threads=2), taxonomy) != base

    raw = json.loads(TAXONOMY_FILE.read_text(encoding="utf-8"))
    edited = copy.deepcopy(raw)
    edited["topics"][0]["descriptions"]["en"][0] += " today"
    other = parse_taxonomy(json.dumps(edited, ensure_ascii=False))
    assert topic_embeddings_key(CONFIG, other) != base


def test_key_ignores_settings_that_do_not_touch_the_vectors(taxonomy: Taxonomy) -> None:
    base = topic_embeddings_key(CONFIG, taxonomy)
    tau = replace(CONFIG, scoring=ScoringConfig(tau_text=0.5))
    assert topic_embeddings_key(tau, taxonomy) == base
    other_norm = replace(CONFIG, normalizer=NormalizeConfig(max_chars=50))
    assert topic_embeddings_key(other_norm, taxonomy) == base


# ------------------------------------------------------------------ the cache


def test_first_call_builds_and_writes_the_cache_second_call_reads_it(
    tmp_path: Path, taxonomy: Taxonomy
) -> None:
    fake = FakeEmbedder()
    first = load_or_build_topic_embeddings(fake, CONFIG, taxonomy, tmp_path)
    assert not first.from_cache
    assert len(fake.calls) == len(taxonomy.topics)
    assert [p.name for p in cache_files(tmp_path)] == [f"topic-embeddings-{first.key}.npz"]

    again = load_or_build_topic_embeddings(fake, CONFIG, taxonomy, tmp_path)
    assert again.from_cache
    assert len(fake.calls) == len(taxonomy.topics)  # the embedder was not called again
    assert again.topic_ids == first.topic_ids
    assert np.array_equal(again.matrix, first.matrix)  # exact, not approximate


def test_a_changed_model_or_taxonomy_does_not_reuse_the_old_file(
    tmp_path: Path, taxonomy: Taxonomy
) -> None:
    fake = FakeEmbedder()
    load_or_build_topic_embeddings(fake, CONFIG, taxonomy, tmp_path)
    calls = len(fake.calls)
    newer = replace(CONFIG, model=replace(CONFIG.model, revision="1" * 40))
    result = load_or_build_topic_embeddings(fake, newer, taxonomy, tmp_path)
    assert not result.from_cache
    assert len(fake.calls) == 2 * calls
    assert len(cache_files(tmp_path)) == 2  # the old file is kept, the new one is added


def test_works_without_a_cache_directory(taxonomy: Taxonomy) -> None:
    fake = FakeEmbedder()
    first = load_or_build_topic_embeddings(fake, CONFIG, taxonomy, None)
    second = load_or_build_topic_embeddings(fake, CONFIG, taxonomy, None)
    assert not first.from_cache and not second.from_cache
    assert np.array_equal(first.matrix, second.matrix)


def _cache_path(directory: Path) -> Path:
    (path,) = directory.glob("topic-embeddings-*.npz")
    return path


@pytest.mark.parametrize("damage", ["garbage", "empty", "truncated"])
def test_a_damaged_cache_file_is_rebuilt_not_trusted(
    tmp_path: Path, taxonomy: Taxonomy, damage: str
) -> None:
    fake = FakeEmbedder()
    good = load_or_build_topic_embeddings(fake, CONFIG, taxonomy, tmp_path)
    path = _cache_path(tmp_path)
    data = path.read_bytes()
    path.write_bytes(
        {"garbage": b"not an npz file", "empty": b"", "truncated": data[: len(data) // 2]}[damage]
    )

    rebuilt = load_or_build_topic_embeddings(fake, CONFIG, taxonomy, tmp_path)
    assert not rebuilt.from_cache
    assert np.array_equal(rebuilt.matrix, good.matrix)
    assert load_or_build_topic_embeddings(fake, CONFIG, taxonomy, tmp_path).from_cache  # repaired


def _write(path: Path, **arrays: object) -> None:
    with path.open("wb") as handle:
        np.savez(handle, **arrays)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m, ids, key: (m[:-1], ids[:-1], key),  # missing a topic
        lambda m, ids, key: (m, ids[::-1], key),  # topic ids in another order
        lambda m, ids, key: (m, ids, "0" * 16),  # key mismatch
        lambda m, ids, key: (m * 3.0, ids, key),  # not unit length
        lambda m, ids, key: (np.full_like(m, np.nan), ids, key),  # not finite
        lambda m, ids, key: (m[:, :10], ids, key),  # wrong width
        lambda m, ids, key: (m.astype(np.float64), ids, key),  # wrong dtype
    ],
)
def test_a_cache_file_that_does_not_match_is_ignored(
    tmp_path: Path, taxonomy: Taxonomy, mutate: object
) -> None:
    fake = FakeEmbedder()
    good = load_or_build_topic_embeddings(fake, CONFIG, taxonomy, tmp_path)
    path = _cache_path(tmp_path)
    matrix, ids, key = mutate(good.matrix, np.array(good.topic_ids), good.key)  # type: ignore[operator]
    _write(path, key=np.array(key), topic_ids=ids, matrix=matrix)
    result = load_or_build_topic_embeddings(fake, CONFIG, taxonomy, tmp_path)
    assert not result.from_cache
    assert np.array_equal(result.matrix, good.matrix)


def test_a_cache_file_holding_python_objects_is_never_loaded(
    tmp_path: Path, taxonomy: Taxonomy
) -> None:
    fake = FakeEmbedder()
    good = load_or_build_topic_embeddings(fake, CONFIG, taxonomy, tmp_path)
    obj = np.empty(1, dtype=object)
    obj[0] = {"pickle": "payload"}
    _write(_cache_path(tmp_path), key=obj, topic_ids=obj, matrix=obj)  # needs pickle to read
    result = load_or_build_topic_embeddings(fake, CONFIG, taxonomy, tmp_path)
    assert not result.from_cache
    assert np.array_equal(result.matrix, good.matrix)


def test_a_cache_that_cannot_be_written_is_not_an_error(tmp_path: Path, taxonomy: Taxonomy) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where the cache directory should be", encoding="utf-8")
    result = load_or_build_topic_embeddings(FakeEmbedder(), CONFIG, taxonomy, blocker / "cache")
    assert result.matrix.shape == (len(taxonomy.topics), 384)
    assert not result.from_cache


def test_no_temporary_files_are_left_behind(tmp_path: Path, taxonomy: Taxonomy) -> None:
    load_or_build_topic_embeddings(FakeEmbedder(), CONFIG, taxonomy, tmp_path)
    assert [p.suffix for p in cache_files(tmp_path)] == [".npz"]


# ------------------------------------------------------------------ the provider


def test_provider_loads_once_per_process(tmp_path: Path, taxonomy: Taxonomy) -> None:
    fake = FakeEmbedder()
    provider = TopicEmbeddingProvider(fake, CONFIG, taxonomy, tmp_path)
    first = provider.get()
    calls = len(fake.calls)
    assert provider.get() is first
    assert len(fake.calls) == calls


def test_provider_does_not_remember_a_failure(tmp_path: Path, taxonomy: Taxonomy) -> None:
    fake = FakeEmbedder()
    fake.fail = lambda kind, texts: RuntimeError("model unavailable")
    provider = TopicEmbeddingProvider(fake, CONFIG, taxonomy, tmp_path)
    with pytest.raises(RuntimeError):
        provider.get()
    fake.fail = None
    assert provider.get().matrix.shape[0] == len(taxonomy.topics)


def test_text_model_config_default_is_used_for_the_key() -> None:
    assert CONFIG.model == TextModelConfig()
