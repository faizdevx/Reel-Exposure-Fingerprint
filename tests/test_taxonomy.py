"""Taxonomy v1 tests: the shipped file and the validator (spec §5.2, roadmap M3). No database."""

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.taxonomy import (
    MAX_DESCRIPTIONS,
    MAX_TOPICS,
    MIN_DESCRIPTIONS,
    MIN_TOPICS,
    RESERVED_SLUGS,
    Taxonomy,
    TaxonomyError,
    load_taxonomy,
    parse_taxonomy,
    taxonomy_hash,
)

REPO = Path(__file__).resolve().parents[1]
TOPICS_FILE = REPO / "taxonomy" / "topics.json"

# Pins the wording to the version. Changing any topic, label or description changes the hash:
# bump `taxonomy_version` in taxonomy/topics.json (it is part of the pipeline version) and update
# both values here in the same commit.
PINNED_VERSION = "v1"
PINNED_HASH = "20658e3f6f033e5b3336e9e7778aade31d37981492151e26d55eb2a58848b271"


@pytest.fixture(scope="module")
def taxonomy() -> Taxonomy:
    return load_taxonomy(TOPICS_FILE)


@pytest.fixture
def raw() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
    return copy.deepcopy(data)


def invalid(data: dict[str, Any]) -> TaxonomyError:
    with pytest.raises(TaxonomyError) as info:
        parse_taxonomy(json.dumps(data, ensure_ascii=False))
    return info.value


# ------------------------------------------------------------------ the shipped file


def test_shipped_taxonomy_is_valid_and_in_range(taxonomy: Taxonomy) -> None:
    assert MIN_TOPICS <= len(taxonomy.topics) <= MAX_TOPICS


def test_version_is_present_and_every_topic_carries_it(taxonomy: Taxonomy) -> None:
    assert taxonomy.taxonomy_version == PINNED_VERSION
    assert {t.taxonomy_version for t in taxonomy.topics} == {PINNED_VERSION}


def test_ids_and_slugs_are_unique(taxonomy: Taxonomy) -> None:
    ids = [t.id for t in taxonomy.topics]
    slugs = [t.slug for t in taxonomy.topics]
    assert len(set(ids)) == len(ids)
    assert len(set(slugs)) == len(slugs)
    assert all(1 <= i <= 32_767 for i in ids)  # topics.id is smallint


def test_every_topic_has_english_and_hindi_descriptions(taxonomy: Taxonomy) -> None:
    for topic in taxonomy.topics:
        for items in (topic.descriptions.en, topic.descriptions.hi):
            assert MIN_DESCRIPTIONS <= len(items) <= MAX_DESCRIPTIONS, topic.slug
            assert all(text.strip() for text in items), topic.slug


def test_hindi_descriptions_are_devanagari_and_english_ones_are_not(taxonomy: Taxonomy) -> None:
    devanagari = re.compile(r"[ऀ-ॿ]")
    for topic in taxonomy.topics:
        assert all(devanagari.search(t) for t in topic.descriptions.hi), topic.slug
        assert not any(devanagari.search(t) for t in topic.descriptions.en), topic.slug


def test_no_description_is_shared_between_topics(taxonomy: Taxonomy) -> None:
    owner: dict[str, str] = {}
    for topic in taxonomy.topics:
        for text in (*topic.descriptions.en, *topic.descriptions.hi):
            assert owner.setdefault(text.casefold(), topic.slug) == topic.slug, text


def test_other_is_not_a_listed_topic(taxonomy: Taxonomy) -> None:
    # `other` comes from the fusion threshold (spec §5.2), not from descriptions.
    assert sorted(RESERVED_SLUGS) == ["other"]
    assert "other" not in {t.slug for t in taxonomy.topics}


def test_content_is_pinned_to_the_version(taxonomy: Taxonomy) -> None:
    assert taxonomy_hash(taxonomy) == PINNED_HASH, (
        "taxonomy content changed: bump taxonomy_version in taxonomy/topics.json and update the "
        "pinned version and hash in this test"
    )


def test_hash_ignores_key_order_and_whitespace(raw: dict[str, Any]) -> None:
    reordered = {"topics": raw["topics"], "taxonomy_version": raw["taxonomy_version"]}
    text = json.dumps(reordered, ensure_ascii=False, indent=7)
    assert taxonomy_hash(parse_taxonomy(text)) == PINNED_HASH


def test_file_is_utf8_json_with_no_other_keys() -> None:
    data = json.loads(TOPICS_FILE.read_bytes().decode("utf-8"))
    assert set(data) == {"taxonomy_version", "topics"}
    for topic in data["topics"]:
        assert set(topic) == {"id", "slug", "label", "taxonomy_version", "descriptions"}
        assert set(topic["descriptions"]) == {"en", "hi"}


# ------------------------------------------------------------------ the validator


def test_rejects_a_missing_version(raw: dict[str, Any]) -> None:
    del raw["taxonomy_version"]
    invalid(raw)


@pytest.mark.parametrize("version", ["", "1", "v", "version-1", "v1.", " v1", None, 1])
def test_rejects_a_malformed_version(raw: dict[str, Any], version: Any) -> None:
    raw["taxonomy_version"] = version
    invalid(raw)


def test_rejects_a_topic_without_a_version(raw: dict[str, Any]) -> None:
    del raw["topics"][0]["taxonomy_version"]
    invalid(raw)


def test_rejects_a_topic_with_another_version(raw: dict[str, Any]) -> None:
    raw["topics"][3]["taxonomy_version"] = "v2"
    assert "different taxonomy_version" in str(invalid(raw))


def test_rejects_duplicate_ids(raw: dict[str, Any]) -> None:
    raw["topics"][1]["id"] = raw["topics"][0]["id"]
    assert "duplicate topic id" in str(invalid(raw))


def test_rejects_duplicate_slugs(raw: dict[str, Any]) -> None:
    raw["topics"][1]["slug"] = raw["topics"][0]["slug"]
    assert "duplicate topic slug" in str(invalid(raw))


def test_rejects_duplicate_labels_ignoring_case(raw: dict[str, Any]) -> None:
    raw["topics"][1]["label"] = raw["topics"][0]["label"].upper()
    assert "duplicate topic label" in str(invalid(raw))


@pytest.mark.parametrize("topic_id", [0, -1, 32_768, "3", 1.5, None])
def test_rejects_bad_ids(raw: dict[str, Any], topic_id: Any) -> None:
    raw["topics"][0]["id"] = topic_id
    invalid(raw)


@pytest.mark.parametrize("slug", ["", "A", "Has Space", "1abc", "a", "x" * 41, "naïve", "other"])
def test_rejects_bad_or_reserved_slugs(raw: dict[str, Any], slug: str) -> None:
    raw["topics"][0]["slug"] = slug
    invalid(raw)


@pytest.mark.parametrize("language", ["en", "hi"])
def test_rejects_missing_empty_or_too_few_descriptions(raw: dict[str, Any], language: str) -> None:
    del raw["topics"][0]["descriptions"][language]
    invalid(raw)
    raw["topics"][0]["descriptions"][language] = []
    invalid(raw)
    raw["topics"][0]["descriptions"][language] = ["one", "two"]
    invalid(raw)


def test_rejects_a_topic_with_no_descriptions_key(raw: dict[str, Any]) -> None:
    del raw["topics"][0]["descriptions"]
    invalid(raw)


@pytest.mark.parametrize("language", ["en", "hi"])
def test_rejects_too_many_descriptions(raw: dict[str, Any], language: str) -> None:
    items = raw["topics"][0]["descriptions"][language]
    raw["topics"][0]["descriptions"][language] = [*items, *[f"{items[0]} {n}" for n in range(3)]]
    invalid(raw)


@pytest.mark.parametrize("bad", ["", "   ", " padded ", "ab", "x" * 121])
def test_rejects_blank_padded_or_oversized_descriptions(raw: dict[str, Any], bad: str) -> None:
    raw["topics"][0]["descriptions"]["en"][0] = bad
    invalid(raw)


def test_rejects_duplicate_descriptions_within_a_topic(raw: dict[str, Any]) -> None:
    en = raw["topics"][0]["descriptions"]["en"]
    en[1] = en[0].upper()
    invalid(raw)


def test_rejects_a_description_shared_by_two_topics(raw: dict[str, Any]) -> None:
    raw["topics"][1]["descriptions"]["en"][0] = raw["topics"][0]["descriptions"]["en"][0]
    assert "shared by" in str(invalid(raw))


def test_rejects_hindi_that_is_not_devanagari(raw: dict[str, Any]) -> None:
    raw["topics"][0]["descriptions"]["hi"][0] = "funny comedy skit"
    invalid(raw)


def test_rejects_devanagari_in_english(raw: dict[str, Any]) -> None:
    raw["topics"][0]["descriptions"]["en"][0] = "हास्य स्केच"
    invalid(raw)


def test_hindi_may_keep_latin_acronyms(raw: dict[str, Any]) -> None:
    raw["topics"][0]["descriptions"]["hi"][0] = "आईपीएल IPL के मज़ेदार पल"
    parse_taxonomy(json.dumps(raw, ensure_ascii=False))


@pytest.mark.parametrize("count", [0, MIN_TOPICS - 1, MAX_TOPICS + 1])
def test_rejects_a_topic_count_outside_25_to_30(raw: dict[str, Any], count: int) -> None:
    topics = raw["topics"]
    if count > len(topics):
        for n in range(count - len(topics)):
            extra = copy.deepcopy(topics[0])
            extra.update(id=1000 + n, slug=f"extra_{n}", label=f"Extra {n}")
            extra["descriptions"] = {
                "en": [f"extra topic {n} text {i}" for i in range(3)],
                "hi": [f"अतिरिक्त विषय {n} पाठ{'क' * i}" for i in range(3)],
            }
            topics.append(extra)
    raw["topics"] = topics[:count]
    invalid(raw)


def test_a_30_topic_taxonomy_is_accepted(raw: dict[str, Any]) -> None:
    topics = raw["topics"]
    for n in range(MAX_TOPICS - len(topics)):
        extra = copy.deepcopy(topics[0])
        extra.update(id=1000 + n, slug=f"extra_{n}", label=f"Extra {n}")
        extra["descriptions"] = {
            "en": [f"extra topic {n} text {i}" for i in range(3)],
            "hi": [f"अतिरिक्त विषय {n} पाठ{'क' * i}" for i in range(3)],
        }
        topics.append(extra)
    assert len(parse_taxonomy(json.dumps(raw, ensure_ascii=False)).topics) == MAX_TOPICS


def test_rejects_unknown_keys_and_wrong_types(raw: dict[str, Any]) -> None:
    raw["topics"][0]["weight"] = 1.0
    invalid(raw)
    del raw["topics"][0]["weight"]
    raw["topics"][0]["label"] = 7
    invalid(raw)


def test_rejects_garbage_and_missing_files(tmp_path: Path) -> None:
    with pytest.raises(TaxonomyError):
        parse_taxonomy("not json")
    with pytest.raises(TaxonomyError):
        parse_taxonomy("[]")
    with pytest.raises(TaxonomyError, match="cannot read"):
        load_taxonomy(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(TaxonomyError):
        load_taxonomy(tmp_path / "bad.json")


def test_hash_changes_with_any_description(raw: dict[str, Any], taxonomy: Taxonomy) -> None:
    raw["topics"][0]["descriptions"]["en"][0] += " today"
    changed = parse_taxonomy(json.dumps(raw, ensure_ascii=False))
    assert taxonomy_hash(changed) != taxonomy_hash(taxonomy)


# ------------------------------------------------------------------ scope guard


def test_no_clustering_or_topic_modelling_dependency() -> None:
    """KMeans, MiniBatchKMeans, BERTopic and HDBSCAN are out of the MVP (spec §2, §11)."""
    banned = re.compile(r"scikit-learn|sklearn|bertopic|hdbscan|umap|kmeans", re.IGNORECASE)
    for manifest in (REPO / "server" / "pyproject.toml", REPO / "server" / "uv.lock"):
        assert not banned.search(manifest.read_text(encoding="utf-8")), manifest.name
    for source in (REPO / "server" / "app").rglob("*.py"):
        assert not banned.search(source.read_text(encoding="utf-8")), source.name
