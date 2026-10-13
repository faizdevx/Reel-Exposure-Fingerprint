"""Persistence for the text pipeline: `topics`, `content_features` and `topic_assignments`.

Rows are keyed by `(user_id, reel_shortcode, pipeline_version)` (ADR-024; spec §9 has no user in
the key), so the same Reel is analysed once per user and version, however many times that user
saw it. A new version never overwrites an old one: its rows are added next to them.

`process_reel_text` runs the whole text path for one Reel as reported stages:

    cache_lookup -> normalize -> embed_text -> load_taxonomy_embeddings -> score_topics
                 -> store_features -> store_assignments

Each database stage commits on its own and rolls back on failure, and a failure in one does not
stop the others that do not depend on it:

- the embedding is stored even if scoring or the assignment write fails, and the next run re-scores
  from the stored embedding without running the model again;
- assignments are written only once the features row they come from exists;
- nothing is written for a Reel with no usable text (`no_text`): there is nothing to store.

First writer wins (`ON CONFLICT DO NOTHING`), so two workers on the same Reel of one user cannot
disagree or duplicate. The text of that user's first event for the Reel is the text that is cached;
a later, fuller caption does not replace it (ADR-010's one-time upgrade is Proposed and not built).

Every row belongs to one user (ADR-024, security audit H-3): the text behind a row is what that
user's client sent, so it must never decide what another user's Reel is about.

This module never touches `fingerprint_*`, `topic_transitions` or `predictions`.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession

from app.models import ContentFeature, ReelExposure, Topic, TopicAssignment
from app.taxonomy import Taxonomy
from app.worker.config import TEXT_MODALITY
from app.worker.embedder import Vectors
from app.worker.fusion import OTHER_LABEL, OTHER_SLUG, OTHER_TOPIC_ID
from app.worker.scoring import TopicScores
from app.worker.stages import StageReport, StageRunner
from app.worker.text_normalize import TextInput
from app.worker.text_pipeline import (
    STAGE_EMBED,
    STAGE_NORMALIZE,
    STAGE_SCORE,
    STAGE_TAXONOMY,
    TextPipeline,
)

log = logging.getLogger("app.worker.store")

STAGE_CACHE_LOOKUP = "cache_lookup"
STAGE_STORE_FEATURES = "store_features"
STAGE_STORE_ASSIGNMENTS = "store_assignments"


class TopicConflictError(Exception):
    """A topic id in the database means a different topic than the taxonomy file says."""


@dataclass(frozen=True)
class CacheState:
    embedding: Vectors | None
    #: topic_id -> p_fused, when every topic of the taxonomy already has a stored assignment.
    probabilities: dict[int, float] | None


@dataclass(frozen=True)
class ProcessOutcome:
    pipeline_version: str
    reports: tuple[StageReport, ...]
    #: topic_id -> probability for this Reel and version, or None if there is none.
    probabilities: dict[int, float] | None
    #: True when the result was already stored and nothing had to be computed.
    from_cache: bool


def text_input_from_exposure(row: ReelExposure, ocr_text: str | None = None) -> TextInput:
    """Map an exposure row to pipeline input. Unavailable fields stay `None`, never a default."""
    status = row.field_status or {}
    caption = row.caption if status.get("caption") == "available" else None
    hashtags = (
        tuple(row.hashtags)
        if status.get("hashtags") == "available" and row.hashtags is not None
        else None
    )
    return TextInput(caption=caption, hashtags=hashtags, ocr_text=ocr_text)


def sync_topics(db: DbSession, taxonomy: Taxonomy) -> None:
    """Make the `topics` table contain the taxonomy's topics. Never changes what an id means.

    A missing topic is inserted. An existing id with the same slug has its label and version
    updated. An id that exists with another slug, or a slug that exists under another id, raises
    `TopicConflictError`: old assignments refer to that id and would silently change meaning.
    """
    existing = list(db.execute(select(Topic)).scalars())
    by_id = {t.id: t for t in existing}
    by_slug = {t.slug: t for t in existing}
    # The catch-all `other` is not in the taxonomy file; it always exists as topic 0.
    wanted = [(t.id, t.slug, t.label, t.taxonomy_version) for t in taxonomy.topics] + [
        (OTHER_TOPIC_ID, OTHER_SLUG, OTHER_LABEL, taxonomy.taxonomy_version)
    ]
    for topic_id, slug, label, version in wanted:
        row = by_id.get(topic_id)
        owner = by_slug.get(slug)
        if (row is not None and row.slug != slug) or (owner is not None and owner.id != topic_id):
            raise TopicConflictError(f"topic id {topic_id}")
        if row is None:
            db.add(Topic(id=topic_id, slug=slug, label=label, taxonomy_version=version))
        else:
            row.label = label
            row.taxonomy_version = version
    db.flush()


def lookup_cache(
    db: DbSession, user_id: uuid.UUID, reel_shortcode: str, version: str, taxonomy: Taxonomy
) -> CacheState:
    features = db.get(ContentFeature, (user_id, reel_shortcode, version))
    embedding: Vectors | None = None
    if features is not None and features.text_embedding is not None:
        embedding = np.asarray(features.text_embedding, dtype=np.float32)
    rows = db.execute(
        select(TopicAssignment.topic_id, TopicAssignment.p_fused).where(
            TopicAssignment.user_id == user_id,
            TopicAssignment.reel_shortcode == reel_shortcode,
            TopicAssignment.pipeline_version == version,
        )
    ).all()
    wanted = {t.id for t in taxonomy.topics}
    stored = {int(topic_id): float(p) for topic_id, p in rows}
    complete = set(stored) == wanted
    return CacheState(embedding, stored if complete else None)


def store_features(
    db: DbSession,
    user_id: uuid.UUID,
    reel_shortcode: str,
    version: str,
    embedding: Vectors,
    ocr_text: str | None,
) -> None:
    db.execute(
        pg_insert(ContentFeature)
        .values(
            user_id=user_id,
            reel_shortcode=reel_shortcode,
            pipeline_version=version,
            ocr_text=ocr_text,
            ocr_confidence=None,
            text_embedding=embedding,
            visual_embedding=None,
            modalities=[TEXT_MODALITY],
        )
        .on_conflict_do_nothing(index_elements=["user_id", "reel_shortcode", "pipeline_version"])
    )


def store_assignments(
    db: DbSession,
    user_id: uuid.UUID,
    reel_shortcode: str,
    version: str,
    taxonomy: Taxonomy,
    scores: TopicScores,
) -> None:
    sync_topics(db, taxonomy)
    rows = [
        {
            "user_id": user_id,
            "reel_shortcode": reel_shortcode,
            "pipeline_version": version,
            "topic_id": topic_id,
            "p_text": p,
            "p_visual": None,
            "p_fused": p,  # one modality, so the renormalised fusion is the text probability
        }
        for topic_id, p in zip(
            scores.topic_ids, (min(1.0, max(0.0, q)) for q in scores.probabilities), strict=True
        )
    ]
    db.execute(
        pg_insert(TopicAssignment)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["user_id", "reel_shortcode", "pipeline_version", "topic_id"]
        )
    )


def _in_transaction[T](db: DbSession, work: Callable[[], T]) -> T:
    try:
        result = work()
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return result


def process_reel_text(
    db: DbSession,
    pipeline: TextPipeline,
    user_id: uuid.UUID,
    reel_shortcode: str,
    inp: TextInput,
    *,
    runner: StageRunner | None = None,
) -> ProcessOutcome:
    runner = runner or StageRunner()
    version = pipeline.version

    lookup = runner.run(
        STAGE_CACHE_LOOKUP,
        lambda: _in_transaction(
            db, lambda: lookup_cache(db, user_id, reel_shortcode, version, pipeline.taxonomy)
        ),
    )
    cache = lookup.value if lookup.ok else None

    if cache is not None and cache.probabilities is not None:
        for stage in (
            STAGE_NORMALIZE,
            STAGE_EMBED,
            STAGE_TAXONOMY,
            STAGE_SCORE,
            STAGE_STORE_FEATURES,
            STAGE_STORE_ASSIGNMENTS,
        ):
            runner.skip(stage, "cached")
        return ProcessOutcome(version, tuple(runner.reports), cache.probabilities, from_cache=True)

    result = pipeline.run(
        inp,
        runner=runner,
        cached_embedding=None if cache is None else cache.embedding,
    )
    had_embedding = cache is not None and cache.embedding is not None

    # ---- features
    features_ok = had_embedding
    if lookup.report.status != "ok":
        runner.skip(STAGE_STORE_FEATURES, f"upstream_{STAGE_CACHE_LOOKUP}")
    elif result.embedding is None:
        runner.skip(STAGE_STORE_FEATURES, "no_embedding")
    elif had_embedding:
        runner.skip(STAGE_STORE_FEATURES, "cached")
    else:
        embedding = result.embedding
        stored = runner.run(
            STAGE_STORE_FEATURES,
            lambda: _in_transaction(
                db,
                lambda: store_features(
                    db, user_id, reel_shortcode, version, embedding, inp.ocr_text
                ),
            ),
        )
        features_ok = stored.ok

    # ---- assignments, from the features row they belong to
    if lookup.report.status != "ok":
        runner.skip(STAGE_STORE_ASSIGNMENTS, f"upstream_{STAGE_CACHE_LOOKUP}")
    elif result.scores is None:
        runner.skip(STAGE_STORE_ASSIGNMENTS, "no_scores")
    elif not features_ok:
        runner.skip(STAGE_STORE_ASSIGNMENTS, f"upstream_{STAGE_STORE_FEATURES}")
    else:
        scores = result.scores
        runner.run(
            STAGE_STORE_ASSIGNMENTS,
            lambda: _in_transaction(
                db,
                lambda: store_assignments(
                    db, user_id, reel_shortcode, version, pipeline.taxonomy, scores
                ),
            ),
        )

    probabilities = (
        dict(zip(result.scores.topic_ids, result.scores.probabilities, strict=True))
        if result.scores is not None
        else None
    )
    return ProcessOutcome(version, tuple(runner.reports), probabilities, from_cache=False)


__all__ = [
    "CacheState",
    "ProcessOutcome",
    "TopicConflictError",
    "lookup_cache",
    "process_reel_text",
    "store_assignments",
    "store_features",
    "sync_topics",
    "text_input_from_exposure",
]
