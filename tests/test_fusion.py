"""Late fusion at the probability level (spec §5.2). Pure maths, no model, no database."""

import inspect
import math

import pytest

from app.worker import fusion
from app.worker.fusion import OTHER_TOPIC_ID, FusionError, assign_other, decide_other, fuse
from app.worker.vision_config import FusionConfig

CFG = FusionConfig()  # 0.5 / 0.5
TEXT = {1: 0.7, 2: 0.2, 3: 0.1}
VISUAL = {1: 0.1, 2: 0.6, 3: 0.3}


def test_two_modalities_are_the_weighted_mean_of_their_probabilities() -> None:
    fused = fuse(TEXT, VISUAL, CFG)
    assert fused == pytest.approx({1: 0.4, 2: 0.4, 3: 0.2})


def test_the_weights_matter() -> None:
    fused = fuse(TEXT, VISUAL, FusionConfig(w_text=0.75, w_visual=0.25))
    assert fused == pytest.approx({1: 0.55, 2: 0.3, 3: 0.15})


def test_weights_that_do_not_sum_to_one_are_renormalised() -> None:
    assert fuse(TEXT, VISUAL, FusionConfig(w_text=0.2, w_visual=0.2)) == pytest.approx(
        fuse(TEXT, VISUAL, CFG)
    )
    assert fuse(TEXT, VISUAL, FusionConfig(w_text=1.0, w_visual=1.0)) == pytest.approx(
        fuse(TEXT, VISUAL, CFG)
    )


def test_a_missing_modality_drops_out_and_the_other_is_returned_as_it_is() -> None:
    # The weights are renormalised over what exists: a lone modality has weight 1.
    assert fuse(TEXT, None, CFG) == pytest.approx(TEXT)
    assert fuse(None, VISUAL, FusionConfig(w_text=0.9, w_visual=0.1)) == pytest.approx(VISUAL)


def test_no_modality_gives_no_distribution_not_a_made_up_one() -> None:
    assert fuse(None, None, CFG) is None


def test_the_fused_distribution_sums_to_one() -> None:
    for weights in (FusionConfig(), FusionConfig(0.3, 0.9), FusionConfig(1.0, 0.01)):
        fused = fuse(TEXT, VISUAL, weights)
        assert fused is not None
        assert math.fsum(fused.values()) == pytest.approx(1.0, abs=1e-12)
        assert all(0.0 <= p <= 1.0 for p in fused.values())


def test_fusing_a_distribution_with_itself_changes_nothing() -> None:
    assert fuse(TEXT, TEXT, FusionConfig(0.3, 0.7)) == pytest.approx(TEXT)


def test_a_confident_modality_can_outvote_an_unsure_one() -> None:
    sharp = {1: 0.97, 2: 0.02, 3: 0.01}
    flat = {1: 0.30, 2: 0.35, 3: 0.35}
    fused = fuse(sharp, flat, CFG)
    assert fused is not None
    assert max(fused, key=fused.__getitem__) == 1


def test_the_result_does_not_depend_on_dict_order() -> None:
    reordered = {3: 0.1, 1: 0.7, 2: 0.2}
    assert fuse(reordered, VISUAL, CFG) == fuse(TEXT, VISUAL, CFG)


@pytest.mark.parametrize(
    "bad",
    [{}, {1: 0.5}, {1: 1.2, 2: -0.2}, {1: math.nan, 2: 1.0}, {1: math.inf}, {1: 0.6, 2: 0.6}],
)
def test_a_modality_that_is_not_a_distribution_is_refused(bad: dict[int, float]) -> None:
    with pytest.raises(FusionError):
        fuse(bad, VISUAL, CFG)
    with pytest.raises(FusionError):
        fuse(TEXT, bad, CFG)


def test_modalities_that_score_different_topic_sets_are_refused() -> None:
    with pytest.raises(FusionError):
        fuse({1: 0.5, 2: 0.5}, {1: 0.5, 3: 0.5}, CFG)


def test_fusion_takes_probabilities_only_never_embeddings() -> None:
    # The two encoders have different spaces and sizes; the only inputs here are topic->probability
    # maps, and the module has no way to combine vectors.
    assert list(inspect.signature(fuse).parameters) == ["p_text", "p_visual", "config"]
    source = inspect.getsource(fusion)
    assert "numpy" not in source and "np." not in source


# ---- the catch-all `other` (spec §5.2)


def test_other_is_assigned_only_when_the_largest_probability_is_under_the_threshold() -> None:
    unsure = {1: 0.4, 2: 0.35, 3: 0.25}
    sure = {1: 0.8, 2: 0.15, 3: 0.05}
    assert decide_other(unsure, 0.5) is True
    assert decide_other(sure, 0.5) is False
    assert decide_other({1: 0.5, 2: 0.5}, 0.5) is False  # equal to the threshold is not below it
    assert decide_other(unsure, 0.4) is False


def test_without_a_threshold_nothing_is_ever_other() -> None:
    assert decide_other({k: 1 / 28 for k in range(1, 29)}, None) is False


def test_an_other_reel_puts_everything_on_other_and_nothing_on_the_topics() -> None:
    out = assign_other({1: 0.4, 2: 0.35, 3: 0.25})
    assert out == {1: 0.0, 2: 0.0, 3: 0.0, OTHER_TOPIC_ID: 1.0}
    assert math.fsum(out.values()) == 1.0


def test_topic_zero_is_reserved_for_other() -> None:
    assert OTHER_TOPIC_ID == 0
    with pytest.raises(FusionError):
        assign_other({0: 0.5, 1: 0.5})


def test_the_threshold_is_validated() -> None:
    for bad in (0.0, -0.1, 1.1):
        with pytest.raises(ValueError):
            FusionConfig(other_threshold=bad)
    assert FusionConfig(other_threshold=1.0).other_threshold == 1.0
    assert FusionConfig().other_threshold is None  # no made-up default
