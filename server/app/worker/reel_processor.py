"""One Reel through every modality: text, visual, late fusion, and the database writes.

    cache_lookup
    text:   normalize -> embed_text -> load_taxonomy_embeddings -> score_topics
    visual: embed_frames -> pool_frames -> load_visual_taxonomy_embeddings -> score_visual
    fuse -> assign_other -> store_features -> store_assignments

Each stage reports `started_at`, `finished_at`, `duration_ms`, `status` and `error`. The two
modalities are independent: a failure or a missing input in one never stops the other, and a Reel
with only one modality still gets a topic distribution (spec F9). Probabilities of the two
modalities are stored separately (`p_text`, `p_visual`) next to the fused one (`p_fused`); the
embeddings are stored in their own columns and are never combined (they live in different spaces).

Caching: rows are keyed by `(user_id, reel_shortcode, pipeline_version)` (ADR-024). Spec §9 and §12
leave out the user, which let one user's text decide another user's Reel. The version covers
both models (id, revision, licence), the taxonomy, the temperatures and the fusion weights
(`app.worker.vision_config`). When no visual model is configured the version is the text pipeline's
and this module behaves like `store.process_reel_text`.

Known limits, owner decisions pending (ADR-010, Proposed):

- First writer wins. A Reel stored text-only because its frames had not arrived, or because the
  visual stage failed, stays text-only under this version; frames that arrive later are not added.
  When a features row exists, its modalities are the ones used: a modality that is not in the row is
  not computed, so the stored embeddings and probabilities always agree.
- Nothing here updates a fingerprint.

Logs and reports carry stage names, counts, ids and error classes only.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession

from app.models import ContentFeature, TopicAssignment
from app.taxonomy import Taxonomy
from app.worker.config import TEXT_MODALITY
from app.worker.embedder import Vectors
from app.worker.fusion import OTHER_TOPIC_ID, assign_other, decide_other, fuse
from app.worker.scoring import TopicScores
from app.worker.stages import StageReport, StageRunner, StageSkipped
from app.worker.store import sync_topics
from app.worker.text_normalize import TextInput
from app.worker.text_pipeline import (
    STAGE_EMBED,
    STAGE_NORMALIZE,
    STAGE_SCORE,
    STAGE_TAXONOMY,
    TextPipeline,
)
from app.worker.vision_config import (
    VISUAL_MODALITY,
    FusionConfig,
    combined_pipeline_version,
)
from app.worker.visual_pipeline import (
    STAGE_EMBED_FRAMES,
    STAGE_POOL,
    STAGE_SCORE_VISUAL,
    STAGE_VISUAL_TAXONOMY,
    VisualPipeline,
)

log = logging.getLogger("app.worker.reel")

STAGE_CACHE_LOOKUP = "cache_lookup"
STAGE_FUSE = "fuse"
STAGE_OTHER = "assign_other"
STAGE_STORE_FEATURES = "store_features"
STAGE_STORE_ASSIGNMENTS = "store_assignments"

_TEXT_STAGES = (STAGE_NORMALIZE, STAGE_EMBED, STAGE_TAXONOMY, STAGE_SCORE)
_VISUAL_STAGES = (STAGE_EMBED_FRAMES, STAGE_POOL, STAGE_VISUAL_TAXONOMY, STAGE_SCORE_VISUAL)


@dataclass(frozen=True)
class ReelPipeline:
    text: TextPipeline
    visual: VisualPipeline | None
    fusion: FusionConfig

    @property
    def taxonomy(self) -> Taxonomy:
        return self.text.taxonomy

    @property
    def version(self) -> str:
        if self.visual is None and self.fusion.other_threshold is None:
            return self.text.version  # exactly the text-only pipeline of before
        return combined_pipeline_version(
            self.text.config,
            self.text.taxonomy,
            None if self.visual is None else self.visual.config,
            self.fusion,
        )


@dataclass(frozen=True)
class ReelOutcome:
    pipeline_version: str
    reports: tuple[StageReport, ...]
    #: topic_id -> probability, per modality and fused; None when there is none.
    p_text: dict[int, float] | None
    p_visual: dict[int, float] | None
    #: The distribution stored in `p_fused`: the fusion, or all on `other` when `is_other`.
    p_fused: dict[int, float] | None
    modalities: tuple[str, ...]
    from_cache: bool
    frames_used: int = 0
    frames_failed: int = 0
    #: The Reel was assigned to `other` (largest fused probability under the threshold).
    is_other: bool = False
    #: The fusion before the `other` decision; None when read back from the cache.
    p_fused_before_other: dict[int, float] | None = None


@dataclass(frozen=True)
class CachedReel:
    has_row: bool
    text_embedding: Vectors | None
    visual_embedding: Vectors | None
    #: topic_id -> (p_text, p_visual, p_fused), when every topic already has a stored assignment.
    assignments: dict[int, tuple[float | None, float | None, float]] | None


def _vec(value: object) -> Vectors | None:
    return None if value is None else np.asarray(value, dtype=np.float32)


def lookup_reel_cache(
    db: DbSession, user_id: uuid.UUID, reel_shortcode: str, version: str, taxonomy: Taxonomy
) -> CachedReel:
    features = db.get(ContentFeature, (user_id, reel_shortcode, version))
    rows = db.execute(
        select(
            TopicAssignment.topic_id,
            TopicAssignment.p_text,
            TopicAssignment.p_visual,
            TopicAssignment.p_fused,
        ).where(
            TopicAssignment.user_id == user_id,
            TopicAssignment.reel_shortcode == reel_shortcode,
            TopicAssignment.pipeline_version == version,
        )
    ).all()
    stored = {
        int(t): (None if pt is None else float(pt), None if pv is None else float(pv), float(pf))
        for t, pt, pv, pf in rows
    }
    wanted = {t.id for t in taxonomy.topics}
    # A Reel assigned to `other` also has the row of topic 0.
    complete = set(stored) in (wanted, wanted | {OTHER_TOPIC_ID})
    return CachedReel(
        has_row=features is not None,
        text_embedding=_vec(features.text_embedding) if features else None,
        visual_embedding=_vec(features.visual_embedding) if features else None,
        assignments=stored if complete else None,
    )


def store_reel_features(
    db: DbSession,
    user_id: uuid.UUID,
    reel_shortcode: str,
    version: str,
    *,
    text_embedding: Vectors | None,
    visual_embedding: Vectors | None,
    ocr_text: str | None,
) -> None:
    modalities = [
        name
        for name, vector in ((TEXT_MODALITY, text_embedding), (VISUAL_MODALITY, visual_embedding))
        if vector is not None
    ]
    db.execute(
        pg_insert(ContentFeature)
        .values(
            user_id=user_id,
            reel_shortcode=reel_shortcode,
            pipeline_version=version,
            ocr_text=ocr_text,
            ocr_confidence=None,
            text_embedding=text_embedding,
            visual_embedding=visual_embedding,
            modalities=modalities,
        )
        .on_conflict_do_nothing(index_elements=["user_id", "reel_shortcode", "pipeline_version"])
    )


def _unit(p: float | None) -> float | None:
    return None if p is None else min(1.0, max(0.0, p))


def store_reel_assignments(
    db: DbSession,
    user_id: uuid.UUID,
    reel_shortcode: str,
    version: str,
    taxonomy: Taxonomy,
    *,
    p_text: dict[int, float] | None,
    p_visual: dict[int, float] | None,
    p_fused: dict[int, float],
) -> None:
    sync_topics(db, taxonomy)
    rows = [
        {
            "user_id": user_id,
            "reel_shortcode": reel_shortcode,
            "pipeline_version": version,
            "topic_id": topic_id,
            "p_text": _unit(None if p_text is None else p_text.get(topic_id)),
            "p_visual": _unit(None if p_visual is None else p_visual.get(topic_id)),
            "p_fused": _unit(p_fused[topic_id]),
        }
        for topic_id in sorted(p_fused)
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


def _as_dict(scores: TopicScores | None) -> dict[int, float] | None:
    if scores is None:
        return None
    return dict(zip(scores.topic_ids, scores.probabilities, strict=True))


def _modalities(p_text: object, p_visual: object) -> tuple[str, ...]:
    return tuple(
        name
        for name, present in ((TEXT_MODALITY, p_text), (VISUAL_MODALITY, p_visual))
        if present is not None
    )


def _column(
    rows: dict[int, tuple[float | None, float | None, float]], index: int
) -> dict[int, float] | None:
    values: dict[int, float] = {}
    for topic_id, row in rows.items():
        value = row[index]
        if value is None:
            return None  # the modality did not exist for this Reel
        values[topic_id] = value
    return values


def _cached_outcome(
    version: str,
    runner: StageRunner,
    rows: dict[int, tuple[float | None, float | None, float]],
) -> ReelOutcome:
    topic_rows = {k: v for k, v in rows.items() if k != OTHER_TOPIC_ID}
    cached_text = _column(topic_rows, 0)
    cached_visual = _column(topic_rows, 1)
    return ReelOutcome(
        version,
        tuple(runner.reports),
        cached_text,
        cached_visual,
        {k: v[2] for k, v in rows.items()},
        _modalities(cached_text, cached_visual),
        from_cache=True,
        is_other=OTHER_TOPIC_ID in rows,
    )


def process_reel(
    db: DbSession,
    pipeline: ReelPipeline,
    user_id: uuid.UUID,
    reel_shortcode: str,
    inp: TextInput,
    frames: Sequence[bytes] = (),
    *,
    runner: StageRunner | None = None,
) -> ReelOutcome:
    runner = runner or StageRunner()
    version = pipeline.version
    visual = pipeline.visual

    lookup = runner.run(
        STAGE_CACHE_LOOKUP,
        lambda: _in_transaction(
            db,
            lambda: lookup_reel_cache(db, user_id, reel_shortcode, version, pipeline.taxonomy),
        ),
    )
    cache = lookup.value if lookup.ok else None

    if cache is not None and cache.assignments is not None:
        skipped = (*_TEXT_STAGES, *(_VISUAL_STAGES if visual else ()), STAGE_FUSE, STAGE_OTHER)
        for stage in (*skipped, STAGE_STORE_FEATURES, STAGE_STORE_ASSIGNMENTS):
            runner.skip(stage, "cached")
        return _cached_outcome(version, runner, cache.assignments)

    has_row = cache is not None and cache.has_row
    text_cached = cache.text_embedding if cache is not None else None
    visual_cached = cache.visual_embedding if cache is not None else None

    # ---- text modality
    if has_row and text_cached is None:
        for stage in _TEXT_STAGES:
            runner.skip(stage, "cached_row")
        text_embedding, p_text = None, None
    else:
        text_result = pipeline.text.run(inp, runner=runner, cached_embedding=text_cached)
        text_embedding, p_text = text_result.embedding, _as_dict(text_result.scores)

    # ---- visual modality
    visual_embedding: Vectors | None = None
    p_visual: dict[int, float] | None = None
    frames_used = frames_failed = 0
    if visual is not None:
        if has_row and visual_cached is None:
            for stage in _VISUAL_STAGES:
                runner.skip(stage, "cached_row")
        else:
            visual_result = visual.run(frames, runner=runner, cached_embedding=visual_cached)
            visual_embedding = visual_result.embedding
            p_visual = _as_dict(visual_result.scores)
            frames_used, frames_failed = visual_result.frames_used, visual_result.frames_failed

    # ---- late fusion, on probabilities only
    def _fuse() -> dict[int, float]:
        fused = fuse(p_text, p_visual, pipeline.fusion)
        if fused is None:
            raise StageSkipped("no_modality")
        return fused

    fused_outcome = runner.run(STAGE_FUSE, _fuse)
    p_fused_before_other = fused_outcome.value if fused_outcome.ok else None

    # ---- the catch-all: no topic likely enough means `other` (spec 5.2)
    def _other() -> bool:
        if pipeline.fusion.other_threshold is None:
            raise StageSkipped("no_threshold")
        if p_fused_before_other is None:
            raise StageSkipped("no_modality")
        return decide_other(p_fused_before_other, pipeline.fusion.other_threshold)

    other_outcome = runner.run(STAGE_OTHER, _other)
    is_other = bool(other_outcome.ok and other_outcome.value)
    p_fused = (
        assign_other(p_fused_before_other)
        if is_other and p_fused_before_other is not None
        else p_fused_before_other
    )

    # ---- features
    features_ok = has_row
    if lookup.report.status != "ok":
        runner.skip(STAGE_STORE_FEATURES, f"upstream_{STAGE_CACHE_LOOKUP}")
    elif has_row:
        runner.skip(STAGE_STORE_FEATURES, "cached")
    elif text_embedding is None and visual_embedding is None:
        runner.skip(STAGE_STORE_FEATURES, "no_embedding")
    else:
        stored = runner.run(
            STAGE_STORE_FEATURES,
            lambda: _in_transaction(
                db,
                lambda: store_reel_features(
                    db,
                    user_id,
                    reel_shortcode,
                    version,
                    text_embedding=text_embedding,
                    visual_embedding=visual_embedding,
                    ocr_text=inp.ocr_text,
                ),
            ),
        )
        features_ok = stored.ok

    # ---- assignments, from the features row they belong to
    if lookup.report.status != "ok":
        runner.skip(STAGE_STORE_ASSIGNMENTS, f"upstream_{STAGE_CACHE_LOOKUP}")
    elif p_fused is None:
        runner.skip(STAGE_STORE_ASSIGNMENTS, "no_scores")
    elif not features_ok:
        runner.skip(STAGE_STORE_ASSIGNMENTS, f"upstream_{STAGE_STORE_FEATURES}")
    else:
        fused = p_fused
        runner.run(
            STAGE_STORE_ASSIGNMENTS,
            lambda: _in_transaction(
                db,
                lambda: store_reel_assignments(
                    db,
                    user_id,
                    reel_shortcode,
                    version,
                    pipeline.taxonomy,
                    p_text=p_text,
                    p_visual=p_visual,
                    p_fused=fused,
                ),
            ),
        )

    return ReelOutcome(
        version,
        tuple(runner.reports),
        p_text,
        p_visual,
        p_fused,
        _modalities(p_text, p_visual),
        from_cache=False,
        frames_used=frames_used,
        frames_failed=frames_failed,
        is_other=is_other,
        p_fused_before_other=p_fused_before_other,
    )
