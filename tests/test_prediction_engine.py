"""Next-topic prediction maths (spec §5.2): the five models, smoothing, backoff, the hybrid, the
walk-forward choice of lambda, and the prequential replay. Pure: no database, no model.

Expected numbers are worked out by hand in the comments.
"""

import math
import random
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.fingerprint.engine import FingerprintConfig, FingerprintState, Observation
from app.prediction.config import (
    BASELINES,
    MODELS,
    PREDICTION_LABEL,
    PredictionConfig,
    prediction_version,
)
from app.prediction.engine import (
    LOSS_FLOOR,
    ModelState,
    PredictionError,
    Replay,
    choose_lambda,
    fingerprint_distribution,
    hard_topic,
    log_loss,
    new_state,
    predict,
    ranked,
    replay,
    smoothed,
    update,
)

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
FP = FingerprintConfig()
CFG = PredictionConfig()
U = (1, 2, 3)


def reel(
    n: int,
    topic: int,
    *,
    universe: tuple[int, ...] = U,
    at: datetime | None = None,
    session: int = 1,
    sharp: float = 0.8,
) -> Observation:
    """A Reel whose top topic is `topic`."""
    rest = (1.0 - sharp) / (len(universe) - 1)
    probs = {k: (sharp if k == topic else rest) for k in universe}
    return Observation(
        uuid.UUID(int=1000 + n),
        uuid.UUID(int=session),
        at or (T0 + timedelta(minutes=n)),
        10_000,
        15_000,
        probs,
    )


def run(
    topics: list[int], *, config: PredictionConfig = CFG, universe: tuple[int, ...] = U
) -> Replay:
    return replay(
        [reel(i, t, universe=universe) for i, t in enumerate(topics)], universe, config, FP
    )


def close(a: dict[int, float], b: dict[int, float]) -> None:
    assert set(a) == set(b)
    for k in a:
        assert a[k] == pytest.approx(b[k], abs=1e-12)


# ---- the label


def test_the_label_is_the_one_the_task_requires() -> None:
    assert PREDICTION_LABEL == "Predicted next observed topic"


def test_there_are_five_models_in_a_fixed_order() -> None:
    assert MODELS == ("uniform", "most_frequent", "decayed_fingerprint", "markov", "hybrid")
    assert BASELINES == ("uniform", "most_frequent", "decayed_fingerprint")


# ---- add-one smoothing


def test_add_one_smoothing_by_hand() -> None:
    # counts 3, 1, 0 over three topics: (3+1)/(4+3), (1+1)/(4+3), (0+1)/(4+3).
    out = smoothed({1: 3, 2: 1}, U, 1.0)
    close(out, {1: 4 / 7, 2: 2 / 7, 3: 1 / 7})
    assert math.fsum(out.values()) == pytest.approx(1.0)


def test_smoothing_with_no_data_is_uniform_and_never_gives_zero() -> None:
    close(smoothed({}, U, 1.0), {k: 1 / 3 for k in U})
    assert all(p > 0 for p in smoothed({1: 1000}, U, 1.0).values())


def test_a_different_smoothing_constant_changes_the_numbers() -> None:
    # (3+0.5)/(4+1.5), (1+0.5)/(5.5), (0+0.5)/(5.5)
    close(smoothed({1: 3, 2: 1}, U, 0.5), {1: 3.5 / 5.5, 2: 1.5 / 5.5, 3: 0.5 / 5.5})


# ---- 1 uniform, 2 most frequent


def test_uniform_gives_every_topic_the_same_probability_always() -> None:
    result = run([1, 1, 1, 2, 3, 1])
    for event in result.events:
        close(event.models["uniform"], {k: 1 / 3 for k in U})


def test_most_frequent_is_the_smoothed_counts_of_the_topics_seen_so_far() -> None:
    result = run([1, 1, 2, 3])
    # Before the 4th Reel the counts are {1: 2, 2: 1}: (2+1)/6, (1+1)/6, (0+1)/6.
    close(result.events[3].models["most_frequent"], {1: 3 / 6, 2: 2 / 6, 3: 1 / 6})
    # Before the 1st Reel there is nothing to count: uniform.
    close(result.events[0].models["most_frequent"], {k: 1 / 3 for k in U})


def test_most_frequent_top_topic_and_its_tie_break() -> None:
    result = run([2, 2, 1, 1, 3])
    last = result.events[-1].models["most_frequent"]
    assert ranked(last)[0] == 1  # 1 and 2 both have 2: the lower id wins
    assert hard_topic({1: 0.4, 2: 0.4, 3: 0.2}) == 1


# ---- 3 decayed fingerprint


def test_the_decayed_fingerprint_is_the_exposure_share_of_each_topic() -> None:
    result = run([1])
    # After one Reel with p = (0.8, 0.1, 0.1): exposure weights are those, share the same.
    close(result.next.models["decayed_fingerprint"], {1: 0.8, 2: 0.1, 3: 0.1})


def test_a_recent_reel_weighs_more_than_an_old_one_in_the_fingerprint() -> None:
    old = reel(0, 1, at=T0)
    new = reel(1, 2, at=T0 + timedelta(days=7))
    out = replay([old, new], U, CFG, FP)
    share = out.next.models["decayed_fingerprint"]
    # Old Reel halves (one half-life): weights 0.5*(0.8,0.1,0.1) + (0.1,0.8,0.1) = (0.5, 0.85, 0.15).
    close(share, {1: 0.5 / 1.5, 2: 0.85 / 1.5, 3: 0.15 / 1.5})
    assert share[2] > share[1]


def test_an_empty_fingerprint_is_uniform_not_made_up() -> None:
    close(fingerprint_distribution(FingerprintState("exposure"), U), {k: 1 / 3 for k in U})


# ---- 4 Markov with backoff


def alternating(n: int) -> list[int]:
    return [1 if i % 2 == 0 else 2 for i in range(n)]


def test_the_transition_row_is_smoothed_by_hand() -> None:
    config = replace(CFG, min_row_observations=2)
    result = run([1, 2, 1, 2, 1], config=config)
    # Consecutive pairs: 1->2, 2->1, 1->2, 2->1. The last topic is 1; its row is {2: 2}.
    # P(next | 1) = (n+1)/(2+3): topic 1: 1/5, topic 2: 3/5, topic 3: 1/5.
    nxt = result.next
    assert not nxt.backoff and nxt.row_observations == 2 and nxt.last_topic == 1
    close(nxt.models["markov"], {1: 1 / 5, 2: 3 / 5, 3: 1 / 5})


def test_backoff_to_the_fingerprint_below_twenty_observations() -> None:
    result = run(alternating(20))  # 19 transitions: 1->2 x10... the row of the last topic is short
    nxt = result.next
    assert nxt.row_observations is not None and nxt.row_observations < 20
    assert nxt.backoff
    close(nxt.models["markov"], nxt.models["decayed_fingerprint"])
    close(nxt.models["hybrid"], nxt.models["decayed_fingerprint"])


def test_the_row_is_trusted_from_exactly_twenty_observations() -> None:
    # 40 alternating Reels: the last topic is 2 (index 39); its row (2 -> 1) has 19 observations.
    # 41 Reels: the last topic is 1 (index 40); its row (1 -> 2) has 20.
    short = run(alternating(40)).next
    enough = run(alternating(41)).next
    assert short.row_observations == 19 and short.backoff
    assert enough.row_observations == 20 and not enough.backoff
    # With 20 observations all to topic 2, smoothing gives (20+1)/(20+3) to it.
    assert enough.models["markov"][2] == pytest.approx(21 / 23)
    assert enough.models["markov"][1] == pytest.approx(1 / 23)


def test_no_last_topic_means_backoff() -> None:
    nxt = replay([], U, CFG, FP).next
    assert nxt.backoff and nxt.row_observations is None and nxt.last_topic is None
    close(nxt.models["markov"], {k: 1 / 3 for k in U})


def test_the_markov_model_learns_a_deterministic_pattern() -> None:
    result = run(alternating(61))
    assert ranked(result.next.models["markov"])[0] == 2  # after a 1 comes a 2
    late = result.events[-1]
    assert ranked(late.models["markov"])[0] == late.actual_topic


# ---- 5 hybrid


def test_the_hybrid_is_lambda_times_the_fingerprint_plus_the_rest_times_the_transition() -> None:
    result = run(alternating(61))
    nxt = result.next
    assert not nxt.backoff
    lam = nxt.lambda_used
    expected = {
        k: lam * nxt.models["decayed_fingerprint"][k] + (1 - lam) * nxt.models["markov"][k]
        for k in U
    }
    close(nxt.models["hybrid"], expected)
    assert math.fsum(nxt.models["hybrid"].values()) == pytest.approx(1.0)


def test_every_model_returns_a_distribution_over_the_whole_universe() -> None:
    result = run(alternating(30))
    for event in result.events:
        for model in MODELS:
            dist = event.models[model]
            assert set(dist) == set(U)
            assert math.fsum(dist.values()) == pytest.approx(1.0)
            assert all(0.0 <= p <= 1.0 for p in dist.values())


def test_lambda_is_the_default_until_there_is_enough_history() -> None:
    result = run(alternating(60), config=replace(CFG, min_events_to_tune=100))
    assert {e.lambda_used for e in result.events} == {CFG.lambda_default}


def test_lambda_is_chosen_from_earlier_reels_only_once_there_are_enough() -> None:
    config = replace(CFG, min_events_to_tune=50, min_row_observations=5)
    result = run(alternating(200), config=config)
    # A perfectly regular pattern: the transition model is far better, so the smallest weight on
    # the fingerprint wins as soon as it is allowed to be chosen.
    before = [e.lambda_used for e in result.events[:50]]
    after = [e.lambda_used for e in result.events[60:]]
    assert set(before) == {config.lambda_default}
    assert set(after) == {min(config.lambda_grid)}


def test_lambda_ties_go_to_the_value_nearest_the_default() -> None:
    state = new_state(U, CFG)
    state.n = 500
    state.lambda_loss = {0.1: 5.0, 0.3: 5.0, 0.5: 9.0, 0.7: 5.0, 0.9: 5.0}
    assert choose_lambda(state, CFG) == 0.3  # 0.3 and 0.7 are equally near 0.5: the smaller wins
    state.lambda_loss = {v: 1.0 for v in CFG.lambda_grid}
    assert choose_lambda(state, CFG) == CFG.lambda_default


def test_the_lambda_used_never_depends_on_the_reel_it_is_used_for() -> None:
    config = replace(CFG, min_events_to_tune=10, min_row_observations=3)
    base = [reel(i, 1 + (i % 2)) for i in range(40)]
    changed = [*base[:20], reel(20, 3), *base[21:]]  # a different topic at index 20
    a, b = replay(base, U, config, FP), replay(changed, U, config, FP)
    assert a.events[20].lambda_used == b.events[20].lambda_used
    assert a.events[20].models == b.events[20].models


# ---- prequential: every prediction is made before its Reel is applied


def test_a_prediction_equals_the_next_prediction_of_the_history_before_it() -> None:
    """Strong form of "made before the Reel": rebuild only the earlier Reels and predict the next."""
    rng = random.Random(5)
    config = replace(CFG, min_events_to_tune=15, min_row_observations=4)
    reels = [reel(i, rng.choice(U)) for i in range(60)]
    full = replay(reels, U, config, FP)
    for i in (0, 1, 7, 25, 59):
        prefix = replay(reels[:i], U, config, FP)
        assert full.events[i].models == prefix.next.models
        assert full.events[i].lambda_used == prefix.next.lambda_used
        assert full.events[i].backoff == prefix.next.backoff


def test_changing_a_reel_changes_only_later_predictions() -> None:
    rng = random.Random(6)
    config = replace(CFG, min_row_observations=3)
    reels = [reel(i, rng.choice(U)) for i in range(40)]
    other = [
        *reels[:25],
        reel(
            25,
            1 + (reels[25].probabilities is not None and hard_topic(reels[25].probabilities) % 3),
        ),
        *reels[26:],
    ]
    a, b = replay(reels, U, config, FP), replay(other, U, config, FP)
    assert [e.models for e in a.events[:26]] == [e.models for e in b.events[:26]]
    assert a.events[25].actual_topic != b.events[25].actual_topic
    assert any(a.events[i].models != b.events[i].models for i in range(26, 40))


def test_the_observed_topic_is_recorded_with_each_prediction() -> None:
    result = run([3, 1, 2])
    assert [e.actual_topic for e in result.events] == [3, 1, 2]
    assert result.events[0].last_topic is None and result.events[1].last_topic == 3


def test_the_replay_is_independent_of_input_order_and_reproducible() -> None:
    reels = [reel(i, 1 + (i * 7) % 3) for i in range(50)]
    shuffled = reels[:]
    random.Random(2).shuffle(shuffled)
    a, b, c = replay(reels, U, CFG, FP), replay(shuffled, U, CFG, FP), replay(reels, U, CFG, FP)
    assert a.events == b.events == c.events
    assert a.next == b.next


def test_reels_without_a_topic_distribution_are_not_part_of_the_sequence() -> None:
    reels = [reel(0, 1), replace(reel(1, 2), probabilities=None), reel(2, 3)]
    result = replay(reels, U, CFG, FP)
    assert [e.actual_topic for e in result.events] == [1, 3] and result.skipped == 1
    assert result.events[1].last_topic == 1  # the skipped Reel is not "the last topic"


def test_duplicate_events_and_topics_outside_the_universe_are_refused() -> None:
    with pytest.raises(PredictionError):
        replay([reel(0, 1), reel(0, 2)], U, CFG, FP)
    with pytest.raises(PredictionError):
        replay([reel(0, 1, universe=(1, 2, 3, 4))], U, CFG, FP)
    with pytest.raises(PredictionError):
        new_state((), CFG)
    with pytest.raises(PredictionError):
        new_state((1, 1, 2), CFG)


def test_other_is_a_topic_when_the_universe_has_it() -> None:
    universe = (0, 1, 2)
    reels = [reel(i, t, universe=universe) for i, t in enumerate([0, 1, 0, 0])]
    result = replay(reels, universe, CFG, FP)
    assert [e.actual_topic for e in result.events] == [0, 1, 0, 0]
    assert set(result.next.models["hybrid"]) == {0, 1, 2}


# ---- scoring helpers


def test_log_loss_is_the_negative_log_of_the_probability_with_a_floor() -> None:
    assert log_loss({1: 0.5, 2: 0.5}, 1) == pytest.approx(math.log(2))
    assert log_loss({1: 1.0, 2: 0.0}, 2) == pytest.approx(-math.log(LOSS_FLOOR))
    assert log_loss({1: 1.0}, 9) == pytest.approx(-math.log(LOSS_FLOOR))


def test_ranking_breaks_ties_toward_the_lower_topic() -> None:
    assert ranked({3: 0.3, 1: 0.3, 2: 0.4}) == [2, 1, 3]


# ---- config and version


def test_config_refuses_nonsense() -> None:
    for bad in (
        {"smoothing": 0.0},
        {"min_row_observations": 0},
        {"lambda_default": 0.0},
        {"lambda_default": 1.0},
        {"lambda_grid": ()},
        {"lambda_grid": (0.0, 0.5)},
        {"rolling_window": 0},
    ):
        with pytest.raises(ValueError):
            PredictionConfig(**bad)


def test_the_version_tracks_every_setting_and_the_fingerprint_it_builds_on() -> None:
    base = prediction_version(CFG, "fp1-a")
    assert base == prediction_version(PredictionConfig(), "fp1-a") and base.startswith("pr1-")
    changed = {
        prediction_version(replace(CFG, smoothing=0.5), "fp1-a"),
        prediction_version(replace(CFG, min_row_observations=10), "fp1-a"),
        prediction_version(replace(CFG, lambda_default=0.4), "fp1-a"),
        prediction_version(replace(CFG, lambda_grid=(0.2, 0.8)), "fp1-a"),
        prediction_version(replace(CFG, min_events_to_tune=50), "fp1-a"),
        prediction_version(replace(CFG, rolling_window=50), "fp1-a"),
        prediction_version(replace(CFG, min_events_for_conclusion=100), "fp1-a"),
        prediction_version(CFG, "fp1-b"),
    }
    assert base not in changed and len(changed) == 8


def test_update_applies_a_reel_only_after_its_prediction() -> None:
    state: ModelState = new_state(U, CFG)
    first = reel(0, 2)
    prediction = predict(state, CFG)
    assert state.n == 0 and state.last_topic is None
    update(state, first, 2, prediction, FP)
    assert state.n == 1 and state.last_topic == 2 and state.counts == {2: 1}
