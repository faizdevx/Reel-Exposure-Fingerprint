"""Topic taxonomy loader and validator (spec §5.2, roadmap M3 item 2).

The taxonomy is a versioned file, `taxonomy/topics.json`: 25 to 30 topics, each with an id, a slug,
a label, the taxonomy version, and 3 to 5 short descriptions in English and in Hindi. For zero-shot
assignment each description is embedded by the text encoder (and by the SigLIP text tower), and a
Reel is scored by cosine similarity against the topic embeddings. Nothing here fits or learns
anything: no clustering and no topic modelling are part of the MVP (spec §2).

The file lists scorable topics only. The catch-all `other` of spec §5.2 is assigned by a threshold,
not by descriptions, so it is not in the file and its slug is reserved here.

Every change to the file needs a new `taxonomy_version`: the version is part of the pipeline
version (CONTRIBUTING.md, reproducibility rules). `taxonomy_hash` gives a content hash that
tests pin to the version, so wording cannot change silently.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

MIN_TOPICS = 25
MAX_TOPICS = 30
MIN_DESCRIPTIONS = 3
MAX_DESCRIPTIONS = 5
MAX_DESCRIPTION_CHARS = 120
MAX_TOPIC_ID = 32_767  # topics.id is smallint (spec §9)

# Assigned by the fusion threshold (spec §5.2, ADR-009), never by descriptions.
RESERVED_SLUGS = frozenset({"other"})

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN = re.compile(r"[A-Za-z]")

Version = Annotated[str, StringConstraints(pattern=r"^v[0-9]+(\.[0-9]+)*$")]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,39}$")]
Label = Annotated[str, StringConstraints(min_length=2, max_length=60)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Descriptions(_Strict):
    en: list[str] = Field(min_length=MIN_DESCRIPTIONS, max_length=MAX_DESCRIPTIONS)
    hi: list[str] = Field(min_length=MIN_DESCRIPTIONS, max_length=MAX_DESCRIPTIONS)

    @model_validator(mode="after")
    def _check_text(self) -> Self:
        for language, items in (("en", self.en), ("hi", self.hi)):
            for text in items:
                if text != text.strip() or not 3 <= len(text) <= MAX_DESCRIPTION_CHARS:
                    raise ValueError(f"{language} description empty, padded or too long")
            if len({t.casefold() for t in items}) != len(items):
                raise ValueError(f"duplicate {language} description")
        if any(_DEVANAGARI.search(t) for t in self.en):
            raise ValueError("an English description contains Devanagari")
        for text in self.hi:
            # Hindi text may keep Latin acronyms (IPL, DIY), but must be mostly Devanagari.
            if len(_DEVANAGARI.findall(text)) <= len(_LATIN.findall(text)):
                raise ValueError("a Hindi description is not mostly Devanagari")
        return self


class Topic(_Strict):
    id: int = Field(ge=1, le=MAX_TOPIC_ID)
    slug: Slug
    label: Label
    taxonomy_version: Version
    descriptions: Descriptions

    @model_validator(mode="after")
    def _reserved(self) -> Self:
        if self.slug in RESERVED_SLUGS:
            raise ValueError(f"slug {self.slug!r} is reserved")
        return self


class Taxonomy(_Strict):
    taxonomy_version: Version
    topics: list[Topic] = Field(min_length=MIN_TOPICS, max_length=MAX_TOPICS)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [t.id for t in self.topics]
        slugs = [t.slug for t in self.topics]
        labels = [t.label.casefold() for t in self.topics]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate topic id")
        if len(set(slugs)) != len(slugs):
            raise ValueError("duplicate topic slug")
        if len(set(labels)) != len(labels):
            raise ValueError("duplicate topic label")
        for topic in self.topics:
            if topic.taxonomy_version != self.taxonomy_version:
                raise ValueError(f"topic {topic.slug!r} has a different taxonomy_version")
        # The same text under two topics would make those topics impossible to tell apart.
        seen: dict[str, str] = {}
        for topic in self.topics:
            for text in (*topic.descriptions.en, *topic.descriptions.hi):
                owner = seen.setdefault(text.casefold(), topic.slug)
                if owner != topic.slug:
                    raise ValueError(f"a description is shared by {owner!r} and {topic.slug!r}")
        return self


class TaxonomyError(Exception):
    """The taxonomy file is missing, unreadable or invalid."""


def parse_taxonomy(raw: str) -> Taxonomy:
    try:
        return Taxonomy.model_validate_json(raw)
    except ValueError as error:
        raise TaxonomyError(str(error)) from error


def load_taxonomy(path: Path) -> Taxonomy:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise TaxonomyError(f"cannot read {path.name}: {type(error).__name__}") from error
    return parse_taxonomy(raw)


def taxonomy_hash(taxonomy: Taxonomy) -> str:
    """SHA-256 of the canonical JSON of the validated content (key order and spacing ignored)."""
    canonical = json.dumps(
        taxonomy.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
