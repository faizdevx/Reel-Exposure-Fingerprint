"""Evaluation of text only, visual only and fused: metrics, split, tuning, guards, files, CLI.

No model and no database: cosines are generated, or come from the fake encoders.
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest
from worker_fakes import FakeEmbedder, FakeImageEmbedder, fake_frame

from app.evaluation.__main__ import main as cli_main
from app.evaluation.cosines import (
    FORMAT,
    CacheError,
    CosineSet,
    build_cosines,
    file_sha256,
    load_cosines,
    save_cosines,
)
from app.evaluation.labels import LabelFileError, load_labels
from app.evaluation.metrics import (
    cluster_bootstrap,
    fuse_rows,
    other_accuracy,
    per_reel,
    softmax_rows,
)
from app.evaluation.tuning import (
    MIN_LABELLED,
    W_GRID,
    Setting,
    compare_modes,
    default_setting,
    evaluate,
    group_codes,
    has_both,
    probabilities,
    split_groups,
)
from app.taxonomy import Taxonomy, load_taxonomy
from app.worker.config import TextPipelineConfig
from app.worker.fusion import OTHER_TOPIC_ID, fuse
from app.worker.scoring import score_topics
from app.worker.text_pipeline import TextPipeline
from app.worker.vision_config import SIGLIP2_BASE, FusionConfig, VisualPipelineConfig
from app.worker.visual_pipeline import VisualPipeline

TAXONOMY_FILE = Path(__file__).resolve().parents[1] / "taxonomy" / "topics.json"
TOPICS = (1, 2, 3, 4, 5, 6)


# ------------------------------------------------------------------ synthetic labelled sets


def make_set(
    n: int = 400,
    groups: int = 20,
    *,
    text_signal: float = 0.3,
    visual_signal: float = 0.3,
    noise: float = 0.1,
    other_share: float = 0.0,
    missing_text: float = 0.0,
    missing_visual: float = 0.0,
    seed: int = 1,
) -> CosineSet:
    """Cosines with a known amount of signal: the right topic gets `signal` added, all get noise."""
    rng = np.random.default_rng(seed)
    k = len(TOPICS)
    labels = rng.choice(TOPICS, size=n)
    other = rng.random(n) < other_share
    labels = np.where(other, OTHER_TOPIC_ID, labels).astype(np.int64)

    def cosines(signal: float, missing: float) -> np.ndarray:
        out = rng.normal(0.0, noise, size=(n, k))
        for i in range(n):
            if labels[i] != OTHER_TOPIC_ID:
                out[i, TOPICS.index(int(labels[i]))] += signal
        out[rng.random(n) < missing] = np.nan
        return out

    return CosineSet(
        ids=tuple(f"r{i}" for i in range(n)),
        groups=tuple(f"g{i % groups}" for i in range(n)),
        label_ids=labels,
        topic_ids=TOPICS,
        cos_text=cosines(text_signal, missing_text),
        cos_visual=cosines(visual_signal, missing_visual),
        meta={
            "format": FORMAT,
            "text_tau_default": 0.01,
            "visual_tau_default": 0.02,
            "labels_sha256": "x",
        },
    )


DEFAULT = Setting(0.5, 0.01, 0.02)


# ------------------------------------------------------------------ probabilities and fusion


def test_softmax_rows_matches_the_pipeline_scorer() -> None:
    rng = np.random.default_rng(0)
    topics = rng.standard_normal((6, 8)).astype(np.float32)
    topics /= np.linalg.norm(topics, axis=1, keepdims=True)
    vector = rng.standard_normal(8).astype(np.float32)
    vector /= np.linalg.norm(vector)
    scores = score_topics(vector, TOPICS, topics, 0.02)
    ours = softmax_rows(np.asarray([scores.cosines], dtype=np.float64), 0.02)[0]
    np.testing.assert_allclose(ours, scores.probabilities, atol=1e-9)


def test_softmax_rows_keeps_missing_rows_missing_and_rejects_a_bad_tau() -> None:
    cos = np.array([[0.1, 0.2], [np.nan, np.nan]])
    out = softmax_rows(cos, 0.05)
    assert out[0].sum() == pytest.approx(1.0) and np.isnan(out[1]).all()
    for bad in (0.0, -1.0, 2.0):
        with pytest.raises(ValueError):
            softmax_rows(cos, bad)


@pytest.mark.parametrize("weights", [(0.5, 0.5), (0.2, 0.8), (0.8, 0.2)])
def test_fuse_rows_agrees_with_the_pipeline_fusion_row_by_row(weights: tuple[float, float]) -> None:
    data = make_set(60, 6, missing_text=0.2, missing_visual=0.2, seed=3)
    p_t, p_v = softmax_rows(data.cos_text, 0.01), softmax_rows(data.cos_visual, 0.02)
    ours = fuse_rows(p_t, p_v, *weights)
    config = FusionConfig(w_text=weights[0], w_visual=weights[1])
    for i in range(len(data.ids)):
        row_t = None if np.isnan(p_t[i]).any() else dict(zip(TOPICS, p_t[i], strict=True))
        row_v = None if np.isnan(p_v[i]).any() else dict(zip(TOPICS, p_v[i], strict=True))
        expected = fuse(row_t, row_v, config)
        if expected is None:
            assert np.isnan(ours[i]).all()
        else:
            np.testing.assert_allclose(ours[i], [expected[k] for k in TOPICS], atol=1e-12)


def test_fusion_renormalises_the_weights_when_a_modality_is_missing() -> None:
    p_t = np.array([[0.7, 0.2, 0.1]])
    nothing = np.full((1, 3), np.nan)
    np.testing.assert_allclose(fuse_rows(p_t, nothing, 0.2, 0.8), p_t)  # weight 1 for the one left
    np.testing.assert_allclose(fuse_rows(nothing, p_t, 0.9, 0.1), p_t)
    assert np.isnan(fuse_rows(nothing, nothing, 0.5, 0.5)).all()


# ------------------------------------------------------------------ metrics, by hand


def test_top1_top3_and_log_loss_by_hand() -> None:
    topics = (1, 2, 3, 4)
    probs = np.array(
        [
            [0.6, 0.2, 0.1, 0.1],  # label 1: top-1 right, loss -ln 0.6
            [0.1, 0.2, 0.3, 0.4],  # label 3: ranked 2nd -> top-3 yes, top-1 no, loss -ln 0.3
            [0.4, 0.3, 0.2, 0.1],  # label 4: ranked last -> neither, loss -ln 0.1
            [0.25, 0.25, 0.25, 0.25],  # label other: not scored here
            [np.nan] * 4,  # no distribution: not scored
        ]
    )
    labels = np.array([1, 3, 4, OTHER_TOPIC_ID, 2], dtype=np.int64)
    pr = per_reel(probs, labels, topics)
    assert pr.covered.tolist() == [True, True, True, False, False]
    assert pr.top1[:3].tolist() == [1.0, 0.0, 0.0]
    assert pr.top3[:3].tolist() == [1.0, 1.0, 0.0]
    np.testing.assert_allclose(pr.loss[:3], [-math.log(0.6), -math.log(0.3), -math.log(0.1)])


def test_ties_go_to_the_lower_topic_id() -> None:
    probs = np.array([[0.5, 0.5]])
    assert per_reel(probs, np.array([1], dtype=np.int64), (1, 2)).top1[0] == 1.0
    assert per_reel(probs, np.array([2], dtype=np.int64), (1, 2)).top1[0] == 0.0


def test_other_is_a_class_when_a_threshold_is_set() -> None:
    topics = (1, 2, 3)
    probs = np.array(
        [
            [0.8, 0.1, 0.1],  # confident -> topic 1
            [0.4, 0.3, 0.3],  # unsure -> other
            [0.4, 0.3, 0.3],  # unsure -> other
        ]
    )
    labels = np.array([1, OTHER_TOPIC_ID, 2], dtype=np.int64)
    _, correct, assigned = other_accuracy(probs, labels, topics, 0.5)
    assert correct.tolist() == [1.0, 1.0, 0.0]  # topic right; other right; other for a topic wrong
    assert assigned.tolist() == [1.0, 0.0, 0.0]
    _, correct_none, assigned_none = other_accuracy(probs, labels, topics, None)
    assert correct_none.tolist() == [1.0, 0.0, 0.0] and assigned_none.tolist() == [1.0, 1.0, 1.0]


def test_the_threshold_is_strict_below() -> None:
    probs = np.array([[0.5, 0.5]])
    _, _, assigned = other_accuracy(probs, np.array([1], dtype=np.int64), (1, 2), 0.5)
    assert assigned[0] == 1.0  # max == threshold is not below it


# ------------------------------------------------------------------ bootstrap intervals


def test_the_bootstrap_is_seeded_and_brackets_the_estimate() -> None:
    values = np.random.default_rng(0).random(120)
    groups = np.repeat(np.arange(12), 10)
    mask = np.ones(120, dtype=bool)
    a = cluster_bootstrap(values, mask, groups, resamples=500, seed=7)
    b = cluster_bootstrap(values, mask, groups, resamples=500, seed=7)
    assert a == b
    assert a.low <= a.estimate <= a.high and a.se > 0 and a.n == 120
    assert cluster_bootstrap(values, mask, groups, resamples=500, seed=8) != a


def test_one_group_or_no_reels_gives_no_interval_instead_of_a_false_one() -> None:
    values, mask = np.ones(10), np.ones(10, dtype=bool)
    one = cluster_bootstrap(values, mask, np.zeros(10, dtype=np.int64), resamples=100, seed=0)
    assert one.estimate == 1.0 and math.isnan(one.low) and math.isnan(one.se)
    none = cluster_bootstrap(values, ~mask, np.zeros(10, dtype=np.int64), resamples=100, seed=0)
    assert none.n == 0 and math.isnan(none.estimate)


def test_clusters_widen_the_interval_compared_with_independent_reels() -> None:
    # Reels within a group are identical, so 12 groups are 12 observations, not 120.
    group_means = np.random.default_rng(1).random(12)
    values = np.repeat(group_means, 10)
    groups = np.repeat(np.arange(12), 10)
    mask = np.ones(120, dtype=bool)
    clustered = cluster_bootstrap(values, mask, groups, resamples=800, seed=0)
    independent = cluster_bootstrap(values, mask, np.arange(120), resamples=800, seed=0)
    assert clustered.se > 1.5 * independent.se


# ------------------------------------------------------------------ the split


def test_a_group_is_never_on_both_sides_of_the_split() -> None:
    groups = group_codes(make_set(300, 15))
    dev, test = split_groups(groups, 0.3, seed=4)
    assert not (dev & test).any() and (dev | test).all()
    assert not set(groups[dev]) & set(groups[test])
    assert 0.2 < test.mean() < 0.45


def test_the_split_is_seeded_and_always_leaves_development_reels() -> None:
    groups = group_codes(make_set(300, 15))
    assert (split_groups(groups, 0.3, 4)[1] == split_groups(groups, 0.3, 4)[1]).all()
    assert not (split_groups(groups, 0.3, 4)[1] == split_groups(groups, 0.3, 5)[1]).all()
    two = np.array([0, 0, 1, 1])
    dev, test = split_groups(two, 0.9, seed=0)
    assert dev.any() and test.any()


# ------------------------------------------------------------------ comparing the modes


def test_fusion_beats_either_modality_when_both_carry_independent_signal() -> None:
    data = make_set(400, 20, text_signal=0.12, visual_signal=0.12, noise=0.1, seed=11)
    groups = group_codes(data)
    out = compare_modes(data, DEFAULT, groups, np.ones(400, dtype=bool), seed=0)
    paired = out["paired"]
    assert paired["n_reels"] == 400
    assert paired["fused_minus_text_log_loss"]["high"] < 0
    assert paired["fused_minus_visual_log_loss"]["high"] < 0
    assert paired["fused"]["top1"]["estimate"] > paired["text_only"]["top1"]["estimate"]
    assert paired["fused"]["top1"]["estimate"] > paired["visual_only"]["top1"]["estimate"]


def test_paired_scores_cover_the_same_reels_and_all_available_do_not() -> None:
    data = make_set(300, 15, missing_text=0.3, missing_visual=0.3, seed=2)
    groups = group_codes(data)
    out = compare_modes(data, DEFAULT, groups, np.ones(300, dtype=bool), seed=0)
    both = int(has_both(data).sum())
    assert out["paired"]["n_reels"] == both
    assert out["paired"]["text_only"]["top1"]["n"] == both
    assert out["paired"]["visual_only"]["top1"]["n"] == both
    counts = {m: out["all_available"][m]["n_reels"] for m in ("text_only", "visual_only", "fused")}
    assert counts["fused"] > counts["text_only"] and counts["fused"] > counts["visual_only"]


def test_a_reel_with_one_modality_is_still_scored_in_the_fused_mode() -> None:
    data = make_set(100, 10, missing_text=1.0, seed=5)  # no text at all
    p = probabilities(data, DEFAULT)
    assert np.isnan(p.text).all()
    assert not np.isnan(p.fused).any()
    np.testing.assert_allclose(p.fused, p.visual)


# ------------------------------------------------------------------ guards against over-fitting


def test_too_little_data_gives_metrics_but_no_recommendation_and_no_tuning() -> None:
    data = make_set(60, 6, seed=7)
    report = evaluate(data, seed=0)
    assert report["recommendation"]["status"] == "insufficient_data"
    assert report["recommendation"]["settings"] is None
    assert report["tuning"] is None and report["held_out"] is None
    assert report["sufficiency"]["enough_to_tune_fusion"] is False
    assert any("labelled_reels" in r for r in report["recommendation"]["reasons"])
    assert report["defaults_on_all_reels"]["paired"]["fused"]["top1"]["n"] == 60  # still reported


@pytest.mark.parametrize(
    ("kwargs", "failed"),
    [
        ({"n": MIN_LABELLED - 1, "groups": 20}, "labelled_reels"),
        ({"n": 400, "groups": 6}, "groups"),
        ({"n": 400, "groups": 20, "missing_visual": 0.8}, "reels_with_text_and_frames"),
    ],
)
def test_each_minimum_is_enforced(kwargs: dict[str, object], failed: str) -> None:
    report = evaluate(make_set(seed=9, **kwargs), seed=0)  # type: ignore[arg-type]
    assert report["recommendation"]["status"] == "insufficient_data"
    assert failed in report["sufficiency"]["failed"]


def test_with_enough_data_there_is_a_report_a_held_out_check_and_a_recommendation() -> None:
    report = evaluate(make_set(400, 20, seed=12), seed=0)
    assert report["sufficiency"]["enough_to_tune_fusion"]
    assert report["recommendation"]["status"] == "recommended"
    assert report["held_out"]["selected"]["paired"]["n_reels"] >= 40
    assert report["tuning"]["points_within_one_se"] >= 1
    assert report["tuning"]["grid_points"] == 5 * 4 * 4
    settings = report["recommendation"]["settings"]
    assert settings["w_text"] in W_GRID
    assert report["recommendation"]["settings"]["other_threshold"] is None  # no `other` Reels


def test_the_report_is_reproducible_and_records_its_inputs() -> None:
    data = make_set(300, 15, seed=13)
    a, b = evaluate(data, seed=3), evaluate(data, seed=3)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["seed"] == 3 and a["inputs"]["reels"] == 300 and a["inputs"]["groups"] == 15
    assert a["inputs"]["labels_sha256"] == "x"
    assert a["format"] == "fusion-evaluation/1"
    c = evaluate(data, seed=4)
    assert a["split"] != c["split"] or a["held_out"] != c["held_out"]


@pytest.mark.parametrize("seed", range(6))
def test_pure_noise_keeps_the_weights_and_the_threshold_at_their_defaults(seed: int) -> None:
    """No signal at all: nothing can justify a weight or a threshold.

    A flatter temperature is the one change noise does justify (with no information the honest
    answer is "unsure", and flatter probabilities score better), so only that may move, and only
    toward flatter, never sharper.
    """
    data = make_set(400, 20, text_signal=0.0, visual_signal=0.0, noise=0.1, seed=100 + seed)
    report = evaluate(data, seed=seed)
    settings = report["recommendation"]["settings"]
    assert settings is not None
    default = default_setting(data)
    assert settings["w_text"] == default.w_text
    assert settings["other_threshold"] is None
    assert settings["tau_text"] >= default.tau_text
    assert settings["tau_visual"] >= default.tau_visual


@pytest.mark.parametrize("seed", range(4))
def test_one_modality_of_pure_noise_does_not_earn_a_different_weight_by_luck(seed: int) -> None:
    """Both informative equally: the weight must stay 0.5 unless the data clearly disagree."""
    data = make_set(400, 20, text_signal=0.15, visual_signal=0.15, noise=0.1, seed=200 + seed)
    settings = evaluate(data, seed=seed)["recommendation"]["settings"]
    assert settings["w_text"] == 0.5


def test_a_clearly_better_modality_pulls_the_weight_toward_it() -> None:
    data = make_set(600, 30, text_signal=0.35, visual_signal=0.04, noise=0.1, seed=21)
    report = evaluate(data, seed=0)
    assert report["held_out"]["confirmed"] is True
    assert report["recommendation"]["settings"]["w_text"] > 0.5


def test_the_selection_prefers_the_defaults_inside_one_standard_error() -> None:
    report = evaluate(make_set(400, 20, text_signal=0.2, visual_signal=0.2, seed=31), seed=0)
    tuning = report["tuning"]
    assert (
        tuning["selected"]["log_loss"] <= tuning["best"]["log_loss"] + tuning["best"]["se"] + 1e-9
    )
    assert "nearest to the defaults" in tuning["rule"]


def test_the_held_out_reels_play_no_part_in_choosing() -> None:
    data = make_set(400, 20, seed=41)
    base = evaluate(data, seed=0)
    groups = group_codes(data)
    _, test = split_groups(groups, 0.3, 0)
    corrupted = CosineSet(
        data.ids,
        data.groups,
        np.where(test, np.roll(data.label_ids, 1), data.label_ids),  # scramble held-out labels
        data.topic_ids,
        data.cos_text,
        data.cos_visual,
        data.meta,
    )
    changed = evaluate(corrupted, seed=0)
    assert changed["tuning"] == base["tuning"]  # selection saw only the development Reels
    assert changed["held_out"] != base["held_out"]


# ------------------------------------------------------------------ the `other` threshold


def test_a_threshold_is_found_when_unsure_reels_are_labelled_other() -> None:
    data = make_set(500, 25, text_signal=0.3, visual_signal=0.3, other_share=0.3, seed=51)
    report = evaluate(data, seed=0)
    assert report["sufficiency"]["enough_to_tune_threshold"]
    settings = report["recommendation"]["settings"]
    assert report["other_threshold"]["confirmed"] is True
    assert settings["other_threshold"] is not None and 0.0 < settings["other_threshold"] <= 0.7
    assert report["other_threshold"]["held_out_gain_in_accuracy_with_other"]["low"] > 0


def test_no_threshold_is_set_without_enough_other_labels() -> None:
    data = make_set(400, 20, other_share=0.02, seed=52)
    report = evaluate(data, seed=0)
    assert not report["sufficiency"]["enough_to_tune_threshold"]
    assert report["recommendation"]["settings"]["other_threshold"] is None
    assert any("labelled `other`" in r for r in report["recommendation"]["reasons"])


def test_the_threshold_curve_is_reported_for_a_human_to_read() -> None:
    data = make_set(500, 25, other_share=0.3, seed=53)
    curve = evaluate(data, seed=0)["other_threshold"]["development"]["curve"]
    assert curve[0]["threshold"] is None and curve[0]["share_assigned_to_topic"] == 1.0
    shares = [row["share_assigned_to_topic"] for row in curve]
    assert shares == sorted(shares, reverse=True)  # a higher threshold assigns `other` more often


# ------------------------------------------------------------------ the labelled-set file


@pytest.fixture(scope="module")
def taxonomy() -> Taxonomy:
    return load_taxonomy(TAXONOMY_FILE)


def write_labels(folder: Path, rows: list[dict[str, object]]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "labels.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return path


def row(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "r1",
        "label": "cricket",
        "group": "a",
        "caption": "IPL match highlights",
        "hashtags": ["ipl"],
    }
    return {**base, **over}


def test_a_valid_file_loads_with_missing_fields_left_missing(
    tmp_path: Path, taxonomy: Taxonomy
) -> None:
    (tmp_path / "frames").mkdir()
    (tmp_path / "frames" / "f.jpg").write_bytes(fake_frame("x"))
    path = write_labels(
        tmp_path,
        [
            row(frames=["frames/f.jpg"], annotator="A"),
            row(id="r2", label="other", caption=None, hashtags=None),
            row(id="r3", label="travel", group="b", frames=[]),
        ],
    )
    reels = load_labels(path, taxonomy)
    assert [r.id for r in reels] == ["r1", "r2", "r3"]
    assert reels[1].label_id == OTHER_TOPIC_ID and reels[1].caption is None
    assert reels[1].hashtags is None and reels[2].frames == ()
    assert reels[0].label_id == next(t.id for t in taxonomy.topics if t.slug == "cricket")


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([], "empty"),
        ([row(), row()], "unique"),
        ([row(label="not-a-topic")], "label"),
        ([row(group=None)], "group"),
        ([row(group="")], "group"),
        ([row(hashtags="ipl")], "hashtags"),
        ([row(caption=5)], "caption"),
        ([row(extra="x")], "unknown fields"),
        ([row(frames=["a.jpg"] * 6)], "at most 5"),
        ([row(frames=["missing.jpg"])], "does not exist"),
    ],
)
def test_a_bad_file_is_refused_with_the_line_number(
    tmp_path: Path, taxonomy: Taxonomy, rows: list[dict[str, object]], message: str
) -> None:
    path = write_labels(tmp_path, rows)
    with pytest.raises(LabelFileError, match=message):
        load_labels(path, taxonomy)


def test_errors_never_echo_the_reel_text(tmp_path: Path, taxonomy: Taxonomy) -> None:
    path = write_labels(tmp_path, [row(caption="PRIVATE-WORDS", label="nope")])
    with pytest.raises(LabelFileError) as error:
        load_labels(path, taxonomy)
    assert "PRIVATE-WORDS" not in str(error.value) and "line 1" in str(error.value)


def test_not_json_and_frames_outside_the_folder_are_refused(
    tmp_path: Path, taxonomy: Taxonomy
) -> None:
    bad = tmp_path / "labels.jsonl"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(LabelFileError, match="not valid JSON"):
        load_labels(bad, taxonomy)
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(fake_frame("x"))
    sub = tmp_path / "set"
    path = write_labels(sub, [row(frames=["../secret.jpg"])])
    with pytest.raises(LabelFileError, match="under the labels folder"):
        load_labels(path, taxonomy)


# ------------------------------------------------------------------ the cosine cache


def test_the_cache_round_trips_and_keeps_missing_modalities_missing(tmp_path: Path) -> None:
    data = make_set(40, 4, missing_text=0.3, seed=61)
    path = tmp_path / "c.npz"
    save_cosines(data, path)
    back = load_cosines(path)
    assert back.ids == data.ids and back.groups == data.groups and back.topic_ids == data.topic_ids
    np.testing.assert_array_equal(back.label_ids, data.label_ids)
    np.testing.assert_array_equal(back.cos_text, data.cos_text)  # NaN positions included
    assert back.meta == data.meta


def test_a_cache_from_another_labelled_set_is_refused(tmp_path: Path) -> None:
    data = make_set(20, 4, seed=62)
    path = tmp_path / "c.npz"
    save_cosines(data, path)
    load_cosines(path, labels_sha256="x")
    with pytest.raises(CacheError, match="different labelled set"):
        load_cosines(path, labels_sha256="other")


def test_cosines_come_from_the_real_pipeline_stages(tmp_path: Path, taxonomy: Taxonomy) -> None:
    text = TextPipeline.create(TextPipelineConfig(), taxonomy, FakeEmbedder(), None)
    visual = VisualPipeline.create(
        VisualPipelineConfig(SIGLIP2_BASE), taxonomy, FakeImageEmbedder(), None
    )
    frame = tmp_path / "f.jpg"
    frame.write_bytes(fake_frame("cricket bat ball stadium"))
    labels = write_labels(
        tmp_path,
        [
            row(frames=["f.jpg"]),
            row(id="r2", caption=None, hashtags=None, frames=["f.jpg"]),  # frames only
            row(id="r3", frames=[]),  # text only
            row(id="r4", caption=None, hashtags=None, frames=[]),  # nothing
        ],
    )
    reels = load_labels(labels, taxonomy)
    data = build_cosines(
        reels, text, visual, labels_sha256=file_sha256(labels), visual_meta={"key": "siglip2-base"}
    )
    assert data.cos_text.shape == (4, len(taxonomy.topics))
    have_text = ~np.isnan(data.cos_text).any(axis=1)
    have_visual = ~np.isnan(data.cos_visual).any(axis=1)
    assert have_text.tolist() == [True, False, True, False]
    assert have_visual.tolist() == [True, True, False, False]  # missing stays missing
    expected = text.run(
        __import__("app.worker.text_normalize", fromlist=["TextInput"]).TextInput(
            caption=reels[0].caption, hashtags=reels[0].hashtags
        )
    ).scores
    assert expected is not None
    np.testing.assert_allclose(data.cos_text[0], expected.cosines)
    assert data.meta["text_pipeline_version"] == text.version
    assert data.meta["visual"] == {"key": "siglip2-base"}


# ------------------------------------------------------------------ the command line


def test_the_evaluate_command_writes_a_report_and_checks_the_labels_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = tmp_path / "labels.jsonl"
    labels.write_text("{}", encoding="utf-8")
    data = make_set(60, 6, seed=71)
    data = CosineSet(
        data.ids,
        data.groups,
        data.label_ids,
        data.topic_ids,
        data.cos_text,
        data.cos_visual,
        {**data.meta, "labels_sha256": file_sha256(labels)},
    )
    cache, out = tmp_path / "c.npz", tmp_path / "report.json"
    save_cosines(data, cache)

    code = cli_main(["evaluate", "--cache", str(cache), "--labels", str(labels), "--out", str(out)])

    assert code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["recommendation"]["status"] == "insufficient_data"
    assert "insufficient_data" in capsys.readouterr().out

    labels.write_text('{"changed": true}', encoding="utf-8")
    code = cli_main(["evaluate", "--cache", str(cache), "--labels", str(labels), "--out", str(out)])
    assert code == 2
    assert "different labelled set" in capsys.readouterr().err
