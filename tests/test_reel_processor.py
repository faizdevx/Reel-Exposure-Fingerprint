"""Whole-Reel pipeline: text + visual + late fusion + cache by (reel_shortcode, pipeline_version).

Needs PostgreSQL + pgvector via TEST_DATABASE_URL; skipped otherwise. Uses fake encoders: the real
models are covered by `test_visual_model.py`.
"""

import math
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from worker_fakes import FakeEmbedder, FakeImageEmbedder, fake_frame

from app.models import ContentFeature, TopicAssignment, User
from app.taxonomy import Taxonomy, load_taxonomy
from app.worker.config import TextPipelineConfig
from app.worker.reel_processor import ReelOutcome, ReelPipeline, process_reel
from app.worker.text_normalize import TextInput
from app.worker.text_pipeline import TextPipeline
from app.worker.vision_config import (
    SIGLIP2_BASE,
    SIGLIP_MULTILINGUAL,
    FusionConfig,
    VisualPipelineConfig,
)
from app.worker.visual_pipeline import VisualPipeline

pytestmark = pytest.mark.db

TAXONOMY_FILE = Path(__file__).resolve().parents[1] / "taxonomy" / "topics.json"
CRICKET_TEXT = TextInput(
    caption="Kohli hits a six in the last over, IPL cricket highlights 🏏", hashtags=("cricket",)
)
CRICKET_FRAMES = [fake_frame("cricket bat ball stadium"), fake_frame("cricket match six")]


@pytest.fixture(scope="module")
def taxonomy() -> Taxonomy:
    return load_taxonomy(TAXONOMY_FILE)


class Rig:
    """A ReelPipeline wired to fake encoders, with handles on them."""

    def __init__(
        self,
        taxonomy: Taxonomy,
        *,
        visual_model: object | None = SIGLIP2_BASE,
        fusion: FusionConfig | None = None,
    ) -> None:
        self.text_embedder = FakeEmbedder()
        self.image_embedder = FakeImageEmbedder()
        text = TextPipeline.create(TextPipelineConfig(), taxonomy, self.text_embedder, None)
        visual = None
        if visual_model is not None:
            visual = VisualPipeline.create(
                VisualPipelineConfig(visual_model),  # type: ignore[arg-type]
                taxonomy,
                self.image_embedder,
                None,
            )
        self.pipeline = ReelPipeline(text, visual, fusion or FusionConfig())
        self._users: dict[str, uuid.UUID] = {}

    def queries(self) -> int:
        return len(self.text_embedder.calls_of("query"))

    def user(self, db: Session, name: str = "default") -> uuid.UUID:
        """A user row for the cache (rows belong to a user), created once per name."""
        if name not in self._users:
            email = f"{name}@example.test"
            row = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if row is None:  # rigs in one test share the user, so their rows share the key
                row = User(id=uuid.uuid4(), email=email, password_hash="x")
                db.add(row)
                db.commit()
            self._users[name] = row.id
        return self._users[name]

    def process(
        self,
        db: Session,
        shortcode: str,
        inp: TextInput,
        frames: Sequence[bytes] = (),
        *,
        user: str = "default",
    ) -> ReelOutcome:
        return process_reel(db, self.pipeline, self.user(db, user), shortcode, inp, frames)


def assignments(db: Session, shortcode: str, version: str) -> list[TopicAssignment]:
    db.expire_all()
    return list(
        db.execute(
            select(TopicAssignment)
            .where(
                TopicAssignment.reel_shortcode == shortcode,
                TopicAssignment.pipeline_version == version,
            )
            .order_by(TopicAssignment.topic_id)
        ).scalars()
    )


def features(db: Session, shortcode: str, version: str) -> ContentFeature | None:
    db.expire_all()
    return db.execute(
        select(ContentFeature).where(
            ContentFeature.reel_shortcode == shortcode, ContentFeature.pipeline_version == version
        )
    ).scalar_one_or_none()


def stages(outcome) -> dict[str, tuple[str, str | None]]:  # type: ignore[no-untyped-def]
    return {r.stage: (r.status, r.reason) for r in outcome.reports}


# ------------------------------------------------------------------ both modalities


def test_both_modalities_are_stored_separately_with_a_fused_distribution(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = Rig(taxonomy)
    out = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)

    assert out.modalities == ("text", "visual")
    assert not out.from_cache
    version = rig.pipeline.version
    assert version.startswith("pv2-")

    row = features(db, "Creel1", version)
    assert row is not None
    assert row.modalities == ["text", "visual"]
    assert len(row.text_embedding) == 384  # the two encoders keep their own sizes
    assert len(row.visual_embedding) == 768

    rows = assignments(db, "Creel1", version)
    assert len(rows) == len(taxonomy.topics)
    assert all(r.p_text is not None and r.p_visual is not None for r in rows)
    # Stored separately, not merged: the columns differ, and the fused one is their mean.
    assert any(abs(r.p_text - r.p_visual) > 1e-6 for r in rows)
    for r in rows:
        assert r.p_fused == pytest.approx(0.5 * r.p_text + 0.5 * r.p_visual, abs=1e-9)
    assert math.fsum(r.p_text for r in rows) == pytest.approx(1.0, abs=1e-9)
    assert math.fsum(r.p_visual for r in rows) == pytest.approx(1.0, abs=1e-9)
    assert math.fsum(r.p_fused for r in rows) == pytest.approx(1.0, abs=1e-9)


def test_the_outcome_carries_the_same_numbers_that_were_stored(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = Rig(taxonomy)
    out = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    rows = assignments(db, "Creel1", rig.pipeline.version)
    assert out.p_text is not None and out.p_visual is not None and out.p_fused is not None
    for r in rows:
        assert out.p_text[r.topic_id] == pytest.approx(r.p_text, abs=1e-9)
        assert out.p_visual[r.topic_id] == pytest.approx(r.p_visual, abs=1e-9)
        assert out.p_fused[r.topic_id] == pytest.approx(r.p_fused, abs=1e-9)


def test_fusion_weights_change_the_fused_column_not_the_modality_columns(
    db: Session, taxonomy: Taxonomy
) -> None:
    even = Rig(taxonomy)
    visual_heavy = Rig(taxonomy, fusion=FusionConfig(w_text=0.2, w_visual=0.8))
    a = even.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    b = visual_heavy.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    assert even.pipeline.version != visual_heavy.pipeline.version
    assert a.p_text == b.p_text and a.p_visual == b.p_visual
    assert a.p_fused != b.p_fused


# ------------------------------------------------------------------ cache by shortcode + version


def test_a_second_run_reads_the_cache_and_runs_no_model(db: Session, taxonomy: Taxonomy) -> None:
    rig = Rig(taxonomy)
    first = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    queries, image_calls = rig.queries(), len(rig.image_embedder.image_calls)
    text_calls = len(rig.image_embedder.text_calls)

    second = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)

    assert second.from_cache
    assert rig.queries() == queries  # e5 did not embed the Reel again
    assert len(rig.image_embedder.image_calls) == image_calls  # nor did SigLIP
    assert len(rig.image_embedder.text_calls) == text_calls
    assert second.p_text == pytest.approx(first.p_text)
    assert second.p_visual == pytest.approx(first.p_visual)
    assert second.p_fused == pytest.approx(first.p_fused)
    assert second.modalities == ("text", "visual")
    cached_stage = {r.status for r in second.reports if r.stage != "cache_lookup"}
    assert cached_stage == {"skipped"}
    assert {r.reason for r in second.reports if r.stage != "cache_lookup"} == {"cached"}


def test_the_cache_ignores_what_the_second_viewer_brings(db: Session, taxonomy: Taxonomy) -> None:
    rig = Rig(taxonomy)
    first = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    other_caption = TextInput(caption="completely different words about cooking", hashtags=None)
    second = rig.process(db, "Creel1", other_caption, [fake_frame("pasta kitchen")])
    assert second.from_cache and second.p_fused == pytest.approx(first.p_fused)


def test_rows_are_keyed_by_shortcode(db: Session, taxonomy: Taxonomy) -> None:
    rig = Rig(taxonomy)
    rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    other = rig.process(
        db,
        "Creel2",
        TextInput(caption="pasta recipe at home", hashtags=None),
        [fake_frame("pasta")],
    )
    assert not other.from_cache
    version = rig.pipeline.version
    assert len(assignments(db, "Creel1", version)) == len(taxonomy.topics)
    assert len(assignments(db, "Creel2", version)) == len(taxonomy.topics)


def test_a_different_visual_model_is_a_new_version_stored_next_to_the_old(
    db: Session, taxonomy: Taxonomy
) -> None:
    a, b = Rig(taxonomy, visual_model=SIGLIP2_BASE), Rig(taxonomy, visual_model=SIGLIP_MULTILINGUAL)
    out_a = a.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    out_b = b.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    assert a.pipeline.version != b.pipeline.version
    assert not out_a.from_cache and not out_b.from_cache  # no stale hit across versions
    assert len(assignments(db, "Creel1", a.pipeline.version)) == len(taxonomy.topics)
    assert len(assignments(db, "Creel1", b.pipeline.version)) == len(taxonomy.topics)
    count = db.execute(select(func.count()).select_from(ContentFeature)).scalar_one()
    assert count == 2


def test_with_no_visual_model_the_version_and_the_rows_are_the_text_only_ones(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = Rig(taxonomy, visual_model=None)
    assert rig.pipeline.version.startswith("pv1-")
    assert rig.pipeline.version == rig.pipeline.text.version
    out = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)  # frames are ignored
    assert out.modalities == ("text",)
    assert out.p_visual is None
    assert rig.image_embedder.image_calls == [] and rig.image_embedder.text_calls == []
    rows = assignments(db, "Creel1", rig.pipeline.version)
    assert all(r.p_visual is None for r in rows)
    assert all(r.p_text == pytest.approx(r.p_fused, abs=1e-12) for r in rows)
    row = features(db, "Creel1", rig.pipeline.version)
    assert row is not None and row.visual_embedding is None and row.modalities == ["text"]


def test_a_partly_stored_reel_is_scored_again_from_the_stored_embeddings(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = Rig(taxonomy)
    first = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    version = rig.pipeline.version
    db.execute(TopicAssignment.__table__.delete())  # as if the assignment write had failed
    db.commit()
    queries = rig.queries()
    images = len(rig.image_embedder.image_calls)

    again = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)

    assert not again.from_cache
    assert rig.queries() == queries and len(rig.image_embedder.image_calls) == images
    assert {s for s, _ in (stages(again)[n] for n in ("embed_text", "embed_frames"))} == {"skipped"}
    assert again.p_fused == pytest.approx(first.p_fused, abs=1e-6)
    assert len(assignments(db, "Creel1", version)) == len(taxonomy.topics)


# ------------------------------------------------------------------ missing and failing modalities


def test_a_reel_without_frames_is_text_only_and_says_so(db: Session, taxonomy: Taxonomy) -> None:
    rig = Rig(taxonomy)
    out = rig.process(db, "Creel1", CRICKET_TEXT, [])
    assert out.modalities == ("text",)
    assert out.p_visual is None
    assert out.p_fused == pytest.approx(out.p_text)
    row = features(db, "Creel1", rig.pipeline.version)
    assert row is not None and row.modalities == ["text"] and row.visual_embedding is None
    rows = assignments(db, "Creel1", rig.pipeline.version)
    assert all(r.p_visual is None for r in rows)
    assert stages(out)["embed_frames"] == ("skipped", "no_frames")


def test_a_reel_without_text_still_gets_a_topic_from_its_frames(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = Rig(taxonomy)
    out = rig.process(db, "Creel1", TextInput(caption=None, hashtags=None), CRICKET_FRAMES)
    assert out.modalities == ("visual",)
    assert out.p_text is None and out.p_fused == pytest.approx(out.p_visual)
    row = features(db, "Creel1", rig.pipeline.version)
    assert row is not None and row.text_embedding is None and row.modalities == ["visual"]
    rows = assignments(db, "Creel1", rig.pipeline.version)
    assert all(r.p_text is None and r.p_visual is not None for r in rows)
    assert stages(out)["normalize"] == ("skipped", "no_text")


def test_a_reel_with_neither_stores_nothing_and_invents_nothing(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = Rig(taxonomy)
    out = rig.process(db, "Creel1", TextInput(caption=None, hashtags=None), [])
    assert out.p_text is None and out.p_visual is None and out.p_fused is None
    assert out.modalities == ()
    assert features(db, "Creel1", rig.pipeline.version) is None
    assert assignments(db, "Creel1", rig.pipeline.version) == []
    assert stages(out)["fuse"] == ("skipped", "no_modality")
    assert stages(out)["store_features"] == ("skipped", "no_embedding")
    assert stages(out)["store_assignments"] == ("skipped", "no_scores")


def test_a_failing_visual_stage_does_not_cost_the_reel_its_text(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = Rig(taxonomy)
    rig.image_embedder.fail_images = lambda _b: RuntimeError("image tower down")
    out = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    assert stages(out)["embed_frames"][0] == "failed"
    assert out.modalities == ("text",)
    assert out.p_fused == pytest.approx(out.p_text)
    row = features(db, "Creel1", rig.pipeline.version)
    assert row is not None and row.modalities == ["text"]
    assert stages(out)["store_assignments"][0] == "ok"
    failed = [r for r in out.reports if r.status == "failed"]
    assert [r.error for r in failed] == ["RuntimeError"]


def test_a_failing_text_stage_does_not_cost_the_reel_its_frames(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = Rig(taxonomy)
    rig.text_embedder.fail = lambda kind, _t: (
        RuntimeError("text tower down") if kind == "query" else None
    )
    out = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    assert stages(out)["embed_text"][0] == "failed"
    assert out.modalities == ("visual",)
    assert out.p_fused == pytest.approx(out.p_visual)
    row = features(db, "Creel1", rig.pipeline.version)
    assert row is not None and row.modalities == ["visual"]


def test_a_failing_fusion_keeps_both_modalities_but_stores_no_assignment(
    db: Session, taxonomy: Taxonomy, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.worker import reel_processor
    from app.worker.fusion import FusionError

    def boom(*_a: object, **_k: object) -> None:
        raise FusionError("topic sets differ")

    monkeypatch.setattr(reel_processor, "fuse", boom)
    rig = Rig(taxonomy)
    out = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    assert stages(out)["fuse"][0] == "failed"
    assert out.p_text is not None and out.p_visual is not None and out.p_fused is None
    assert assignments(db, "Creel1", rig.pipeline.version) == []
    assert stages(out)["store_assignments"] == ("skipped", "no_scores")


def test_a_reel_stored_without_visual_stays_that_way_under_this_version(
    db: Session, taxonomy: Taxonomy
) -> None:
    """First writer wins (ADR-010, Proposed): frames that arrive later are not added."""
    rig = Rig(taxonomy)
    rig.process(db, "Creel1", CRICKET_TEXT, [])
    db.execute(TopicAssignment.__table__.delete())
    db.commit()  # force a re-score so the processor looks at the stored row again

    again = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)

    assert rig.image_embedder.image_calls == []  # the row has no visual embedding: not computed
    assert stages(again)["embed_frames"] == ("skipped", "cached_row")
    assert again.modalities == ("text",)
    row = features(db, "Creel1", rig.pipeline.version)
    assert row is not None and row.visual_embedding is None


def test_stored_probabilities_are_valid_and_the_version_is_recorded(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = Rig(taxonomy)
    rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    for r in assignments(db, "Creel1", rig.pipeline.version):
        assert r.pipeline_version == rig.pipeline.version
        for p in (r.p_text, r.p_visual, r.p_fused):
            assert p is not None and 0.0 <= p <= 1.0
    row = features(db, "Creel1", rig.pipeline.version)
    assert row is not None and row.pipeline_version == rig.pipeline.version
    np.testing.assert_allclose(np.linalg.norm(np.asarray(row.visual_embedding)), 1.0, atol=1e-4)
    np.testing.assert_allclose(np.linalg.norm(np.asarray(row.text_embedding)), 1.0, atol=1e-4)


def test_every_stage_reports_its_timing_and_status(db: Session, taxonomy: Taxonomy) -> None:
    rig = Rig(taxonomy)
    out = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    names = [r.stage for r in out.reports]
    assert names == [
        "cache_lookup",
        "normalize",
        "embed_text",
        "load_taxonomy_embeddings",
        "score_topics",
        "embed_frames",
        "pool_frames",
        "load_visual_taxonomy_embeddings",
        "score_visual",
        "fuse",
        "assign_other",
        "store_features",
        "store_assignments",
    ]
    for r in out.reports:
        if r.stage == "assign_other":  # no threshold is configured here
            assert (r.status, r.reason) == ("skipped", "no_threshold")
            continue
        assert r.status == "ok", r
        assert r.started_at <= r.finished_at and r.duration_ms >= 0 and r.error is None


def test_frame_counts_are_reported_without_frame_content(db: Session, taxonomy: Taxonomy) -> None:
    rig = Rig(taxonomy)
    out = rig.process(db, "Creel1", CRICKET_TEXT, [*CRICKET_FRAMES, fake_frame("CORRUPT scene")])
    assert (out.frames_used, out.frames_failed) == (2, 1)
    assert "CORRUPT" not in repr(out.reports)
    assert replace(rig.pipeline.fusion) == FusionConfig()


# ------------------------------------------------------------------ the `other` threshold

from app.models import Topic  # noqa: E402
from app.worker.fusion import OTHER_TOPIC_ID  # noqa: E402


def rig_with_threshold(taxonomy: Taxonomy, threshold: float | None, **kw: object) -> Rig:
    return Rig(taxonomy, fusion=FusionConfig(other_threshold=threshold), **kw)  # type: ignore[arg-type]


def test_an_unsure_reel_is_assigned_to_other_and_the_modality_evidence_is_kept(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = rig_with_threshold(taxonomy, 1.0)  # every fused maximum is below 1.0
    out = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)

    assert out.is_other
    assert out.p_fused is not None and out.p_fused[OTHER_TOPIC_ID] == 1.0
    assert sum(v for k, v in out.p_fused.items() if k != OTHER_TOPIC_ID) == 0.0
    assert out.p_fused_before_other is not None and OTHER_TOPIC_ID not in out.p_fused_before_other
    assert stages(out)["assign_other"][0] == "ok"

    rows = assignments(db, "Creel1", rig.pipeline.version)
    assert len(rows) == len(taxonomy.topics) + 1
    other = next(r for r in rows if r.topic_id == OTHER_TOPIC_ID)
    assert other.p_fused == 1.0 and other.p_text is None and other.p_visual is None
    topic_rows = [r for r in rows if r.topic_id != OTHER_TOPIC_ID]
    assert all(r.p_fused == 0.0 for r in topic_rows)
    # What each modality thought is still stored, separately, for evidence.
    assert math.fsum(r.p_text for r in topic_rows) == pytest.approx(1.0)
    assert math.fsum(r.p_visual for r in topic_rows) == pytest.approx(1.0)
    assert math.fsum(r.p_fused for r in rows) == pytest.approx(1.0)


def test_a_confident_reel_keeps_its_topics_and_gets_no_other_row(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = rig_with_threshold(taxonomy, 0.0001)  # nothing is below this
    out = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    assert not out.is_other
    assert len(assignments(db, "Creel1", rig.pipeline.version)) == len(taxonomy.topics)
    assert out.p_fused == pytest.approx(out.p_fused_before_other)


def test_with_no_threshold_the_stage_is_skipped_and_nothing_is_other(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = Rig(taxonomy)
    out = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    assert stages(out)["assign_other"] == ("skipped", "no_threshold")
    assert not out.is_other


def test_the_other_decision_is_cached_and_read_back(db: Session, taxonomy: Taxonomy) -> None:
    rig = rig_with_threshold(taxonomy, 1.0)
    rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    queries = rig.queries()
    again = rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    assert again.from_cache and again.is_other
    assert rig.queries() == queries
    assert again.p_fused is not None and again.p_fused[OTHER_TOPIC_ID] == 1.0
    assert again.modalities == ("text", "visual")  # the evidence survives the round trip
    assert again.p_text is not None and again.p_visual is not None


def test_a_reel_with_nothing_is_not_other_it_is_unprocessed(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = rig_with_threshold(taxonomy, 1.0)
    out = rig.process(db, "Creel1", TextInput(caption=None, hashtags=None), [])
    assert not out.is_other and out.p_fused is None
    assert stages(out)["assign_other"] == ("skipped", "no_modality")
    assert assignments(db, "Creel1", rig.pipeline.version) == []


def test_a_threshold_is_a_new_version_so_old_rows_stay(db: Session, taxonomy: Taxonomy) -> None:
    plain, strict = Rig(taxonomy), rig_with_threshold(taxonomy, 1.0)
    assert plain.pipeline.version != strict.pipeline.version
    a = plain.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    b = strict.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    assert not a.is_other and b.is_other and not b.from_cache
    assert len(assignments(db, "Creel1", plain.pipeline.version)) == len(taxonomy.topics)
    assert len(assignments(db, "Creel1", strict.pipeline.version)) == len(taxonomy.topics) + 1


def test_a_text_only_pipeline_with_a_threshold_uses_the_combined_version(
    db: Session, taxonomy: Taxonomy
) -> None:
    rig = rig_with_threshold(taxonomy, 1.0, visual_model=None)
    assert rig.pipeline.version.startswith("pv2-")
    out = rig.process(db, "Creel1", CRICKET_TEXT, [])
    assert out.is_other and out.modalities == ("text",)


def test_the_other_topic_exists_in_the_topics_table(db: Session, taxonomy: Taxonomy) -> None:
    rig = rig_with_threshold(taxonomy, 1.0)
    rig.process(db, "Creel1", CRICKET_TEXT, CRICKET_FRAMES)
    db.expire_all()
    row = db.get(Topic, OTHER_TOPIC_ID)
    assert row is not None and row.slug == "other"
    assert (
        db.execute(select(func.count()).select_from(Topic)).scalar_one() == len(taxonomy.topics) + 1
    )
