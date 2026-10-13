"""The four evaluations of spec §10 (`app.evaluation.research`) and their report.

Pure: no database, no model. Statistics are checked against hand-computed values; the honesty rules
are checked directly: nothing is stated without the data, a miss is printed as a miss.
"""

import itertools
import json
import math
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.evaluation.render import render
from app.evaluation.research import (
    SelfReportUser,
    average_ranks,
    build_report,
    kendall_tau,
    labelling_section,
    pearson,
    shuffle_sessions,
    spearman,
    stability_user,
    top_topics,
)
from app.fingerprint.engine import FingerprintConfig, Observation, rebuild
from app.prediction.config import PredictionConfig
from app.prediction.engine import replay

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
FP = FingerprintConfig()
TOPICS = (1, 2, 3, 4, 5, 6)


def obs(topic: int, minutes: float, session: int = 1, *, dwell: int = 15_000) -> Observation:
    return Observation(
        event_id=uuid.uuid4(),
        session_id=uuid.UUID(int=session),
        observed_at=T0 + timedelta(minutes=minutes),
        dwell_ms=dwell,
        video_duration_ms=15_000,
        probabilities={k: (1.0 if k == topic else 0.0) for k in TOPICS},
    )


def history(sessions: int = 6, per_session: int = 40, seed: int = 0) -> list[Observation]:
    """Skewed: topic 1 most common, then 2, 3 ... so a ranking exists."""
    rng = random.Random(seed)
    weights = [30, 25, 20, 12, 8, 5]
    out = []
    for s in range(sessions):
        for i in range(per_session):
            topic = rng.choices(TOPICS, weights)[0]
            out.append(obs(topic, s * 24 * 60 + i, session=s + 1))
    return out


# ------------------------------------------------------------------ statistics


def test_average_ranks_give_ties_the_mean_rank() -> None:
    assert average_ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]
    assert average_ranks([3, 1, 2]) == [3.0, 1.0, 2.0]
    assert average_ranks([5, 5, 5]) == [2.0, 2.0, 2.0]


def test_spearman_perfect_reversed_and_a_hand_computed_value() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    # y ranks are [1, 2, 3.5, 5, 3.5] (a tie); sxy = 8, sxx = 10, syy = 9.5: 8 / sqrt(95)
    assert spearman([1, 2, 3, 4, 5], [5, 6, 7, 8, 7]) == pytest.approx(8 / math.sqrt(95))


def test_spearman_only_depends_on_order() -> None:
    assert spearman([1, 2, 3, 4], [1, 4, 9, 1000]) == pytest.approx(1.0)


def test_a_constant_side_has_no_correlation_not_zero() -> None:
    assert spearman([1, 2, 3], [4, 4, 4]) is None
    assert spearman([2, 2, 2], [1, 2, 3]) is None
    assert pearson([1.0], [1.0]) is None
    assert spearman([], []) is None


def test_kendall_tau_hand_values() -> None:
    assert kendall_tau([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert kendall_tau([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert kendall_tau([1, 2, 3], [1, 3, 2]) == pytest.approx(1 / 3)  # 2 concordant, 1 discordant
    assert kendall_tau([1, 1, 1], [1, 2, 3]) is None


def test_top_topics_rank_by_share_with_ties_to_the_lower_id_and_skip_unseen_topics() -> None:
    assert top_topics({1: 0.1, 2: 0.4, 3: 0.4, 4: 0.1, 5: 0.0}, 3) == [2, 3, 1]
    assert top_topics({1: 0.5, 2: 0.0, 3: 0.0}) == [1]  # unseen topics are not "second" and "third"


# ------------------------------------------------------------------ 1. labelling


def test_without_a_labelled_set_no_figure_is_given() -> None:
    section = labelling_section(None)
    assert (
        section["status"] == "not_run" and section["figures"] is None and section["passed"] is None
    )
    assert "does not exist" in section["reason"]


def fake_report(top1: float, top3: float, reels: int) -> dict[str, Any]:
    def iv(x: float) -> dict[str, float]:
        return {"estimate": x, "low": x - 0.05, "high": x + 0.05}

    def mode(a: float, b: float) -> dict[str, Any]:
        return {"top1": iv(a), "top3": iv(b), "log_loss": iv(1.0)}

    return {
        "format": "x",
        "inputs": {"reels": reels},
        "sufficiency": {"failed": []},
        "defaults_on_all_reels": {
            "paired": {
                "n_reels": reels,
                "text_only": mode(0.5, 0.7),
                "visual_only": mode(0.4, 0.6),
                "fused": mode(top1, top3),
            }
        },
    }


def test_labelling_reports_all_six_figures_and_judges_the_bar_on_the_fused_ones() -> None:
    good = labelling_section(fake_report(0.75, 0.9, 200))
    assert good["status"] == "complete" and good["passed"] is True
    assert set(good["figures"]) == {"text_only", "visual_only", "fused"}
    assert good["figures"]["fused"]["top1"]["estimate"] == 0.75
    bad = labelling_section(fake_report(0.69, 0.9, 200))
    assert bad["passed"] is False  # 69% is below 70%: no rounding up
    low3 = labelling_section(fake_report(0.8, 0.84, 200))
    assert low3["passed"] is False


def test_a_small_labelled_set_is_below_the_minimum_and_not_judged() -> None:
    section = labelling_section(fake_report(0.95, 0.99, 60))
    assert section["status"] == "below_minimum" and section["passed"] is None
    assert "60" in section["reason"]


# ------------------------------------------------------------------ 2. self-report


def user_with(
    ratings: dict[int, int], observations: list[Observation], at: datetime | None = None
) -> SelfReportUser:
    return SelfReportUser(uuid.uuid4(), ratings, at, observations)


def section_for(users: list[SelfReportUser]) -> dict[str, Any]:
    report: dict[str, Any] = build_report(
        labelling=None,
        self_report=users,
        predictions={},
        stability_inputs={},
        topics=TOPICS,
        fp_config=FP,
        prediction_config=PredictionConfig(),
        seed=0,
        meta={},
    )
    section: dict[str, Any] = report["self_report"]
    return section


def test_ratings_in_the_order_of_exposure_correlate_one_with_exposure() -> None:
    reels = [obs(1, i) for i in range(6)] + [obs(2, 10 + i) for i in range(3)] + [obs(3, 20)]
    s = section_for([user_with({1: 5, 2: 3, 3: 1}, reels)])
    row = s["per_user"][0]["all_data"]
    assert row["exposure"] == pytest.approx(1.0) and row["reels"] == 10
    assert row["below_minimum"] is True  # 10 Reels is under 500
    assert s["status"] == "computed" and s["users_with_ratings"] == 1


def test_engagement_and_exposure_are_correlated_separately() -> None:
    # Topic 1 is shown most but skipped; topic 3 is shown least but watched fully.
    reels = (
        [obs(1, i, dwell=300) for i in range(6)]
        + [obs(2, 10 + i, dwell=7_500) for i in range(3)]
        + [obs(3, 20, dwell=15_000)]
    )
    s = section_for([user_with({1: 1, 2: 2, 3: 3}, reels)])
    row = s["per_user"][0]["all_data"]
    assert row["exposure"] == pytest.approx(-1.0)  # shown 6 > 3 > 1, rated 1 < 2 < 3
    assert row["engagement"] is not None and row["engagement"] > row["exposure"]
    d = s["all_data"]["engagement_minus_exposure"]
    assert d["users_engagement_higher"] == 1 and d["users_exposure_higher"] == 0


def test_a_topic_the_fingerprint_never_saw_counts_as_zero_not_unknown() -> None:
    reels = [obs(1, i) for i in range(4)] + [obs(2, 10 + i) for i in range(2)]
    s = section_for([user_with({1: 5, 2: 4, 3: 1}, reels)])
    assert s["per_user"][0]["all_data"]["exposure"] == pytest.approx(1.0)


def test_at_survey_uses_only_the_reels_before_the_survey() -> None:
    reels = [obs(1, i) for i in range(5)] + [obs(2, 100 + i) for i in range(20)]
    survey_time = T0 + timedelta(minutes=50)
    s = section_for([user_with({1: 5, 2: 1}, reels, survey_time)])
    row = s["per_user"][0]
    assert row["at_survey"]["reels"] == 5 and row["all_data"]["reels"] == 25
    assert row["at_survey"]["exposure"] == pytest.approx(1.0)  # only topic 1 existed then
    assert row["all_data"]["exposure"] == pytest.approx(-1.0)  # later topic 2 dominates


def test_a_skipped_survey_is_left_out_and_nothing_is_stated() -> None:
    s = section_for([user_with({}, history(1, 10))])
    assert s["status"] == "not_run" and s["users_with_ratings"] == 0
    assert s["per_user"][0]["status"] == "no_ratings"


def test_a_user_with_no_variation_has_an_undefined_correlation_not_zero() -> None:
    s = section_for([user_with({1: 3, 2: 3, 3: 3}, [obs(1, i) for i in range(5)])])
    assert s["per_user"][0]["all_data"]["exposure"] is None
    assert s["all_data"]["exposure"] == {"n": 0, "mean": None}


# ------------------------------------------------------------------ 4. stability


def test_shuffling_keeps_every_reel_each_session_intact_and_the_total_span() -> None:
    original = history(5, 8)
    shuffled = shuffle_sessions(original, random.Random(3))
    assert {o.event_id for o in shuffled} == {o.event_id for o in original}
    for session in {o.session_id for o in original}:
        a = sorted((o for o in original if o.session_id == session), key=lambda o: o.observed_at)
        b = sorted((o for o in shuffled if o.session_id == session), key=lambda o: o.observed_at)
        assert [o.event_id for o in a] == [o.event_id for o in b]  # order inside the session
        assert [(y.observed_at - x.observed_at) for x, y in itertools.pairwise(a)] == [
            (y.observed_at - x.observed_at) for x, y in itertools.pairwise(b)
        ]  # spacing inside the session

    def span(xs: list[Observation]) -> timedelta:
        return max(o.observed_at for o in xs) - min(o.observed_at for o in xs)

    assert span(shuffled) == span(original)
    assert min(o.observed_at for o in shuffled) == min(o.observed_at for o in original)
    rebuild(shuffled, FP, "x")  # it is a valid history: nothing out of order


def test_shuffling_changes_the_order_of_sessions_and_is_reproducible() -> None:
    original = history(6, 5)
    first = shuffle_sessions(original, random.Random(1))
    again = shuffle_sessions(original, random.Random(1))
    assert [(o.event_id, o.observed_at) for o in first] == [
        (o.event_id, o.observed_at) for o in again
    ]

    def order(xs: list[Observation]) -> list[uuid.UUID]:
        return [o.session_id for o in sorted(xs, key=lambda o: o.observed_at)]

    assert order(first) != order(original)


def test_sessions_stay_contiguous_after_shuffling() -> None:
    shuffled = shuffle_sessions(history(5, 6), random.Random(2))
    ordered = [o.session_id for o in sorted(shuffled, key=lambda o: o.observed_at)]
    changes = sum(1 for a, b in itertools.pairwise(ordered) if a != b)
    assert changes == 4  # five sessions, each in one piece


def test_one_session_cannot_be_shuffled() -> None:
    one = history(1, 10)
    assert shuffle_sessions(one, random.Random(0)) == one
    result = stability_user(one, TOPICS, FP, seed=0, shuffles=5)
    assert result["shuffled_session_order"]["exposure"]["shuffles"] == 0


def test_stability_covers_every_variant_for_both_fingerprints() -> None:
    result = stability_user(history(), TOPICS, FP, seed=0, shuffles=8)
    assert set(result["half_life"]) == {"3", "7", "14"}
    for kind in ("exposure", "engagement"):
        seven = result["half_life"]["7"][kind]
        assert seven["same_top5_order"] and seven["same_top5_set"]  # the baseline against itself
        assert seven["kendall_tau_all_topics"] == pytest.approx(1.0)
        assert seven["max_share_difference"] == 0.0
        assert result["shuffled_session_order"][kind]["shuffles"] == 8
        assert len(result["baseline_top5"][kind]) == 5
    assert result["below_minimum"] is True  # 240 Reels is under 500


def test_a_flipped_order_is_reported_as_a_flip() -> None:
    # 30 Reels of topic 1, then 8 of topic 2 seven days later. Baseline (7 days): 30/2 = 15 > 8,
    # so topic 1 leads. At 3 days: 30 / 2^(7/3) = 5.96 < 8, so topic 2 leads: the order flips.
    early = [obs(1, i, session=1) for i in range(30)]
    late = [obs(2, 7 * 24 * 60 + i, session=2) for i in range(8)]
    result = stability_user(early + late, TOPICS, FP, seed=0, shuffles=4)
    exposure = result["half_life"]
    assert result["baseline_top5"]["exposure"] == [1, 2]
    assert exposure["3"]["exposure"]["top5"] == [2, 1]
    assert exposure["3"]["exposure"]["same_top5_order"] is False
    assert exposure["3"]["exposure"]["same_top5_set"] is True
    assert exposure["14"]["exposure"]["same_top5_order"] is True


def test_stability_is_deterministic_for_a_seed() -> None:
    a = stability_user(history(), TOPICS, FP, seed=5, shuffles=6)
    b = stability_user(history(), TOPICS, FP, seed=5, shuffles=6)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ------------------------------------------------------------------ 3 and the whole report


def report_for(
    observations: list[Observation], ratings: dict[int, int] | None = None
) -> dict[str, Any]:
    events = replay(observations, TOPICS, PredictionConfig(), FP).events
    return build_report(
        labelling=None,
        self_report=[user_with(ratings or {}, observations)],
        predictions={"u1": events},
        stability_inputs={"u1": observations},
        topics=TOPICS,
        fp_config=FP,
        prediction_config=PredictionConfig(),
        seed=0,
        meta={"assignment_version": "av", "users": 1},
    )


def test_the_prediction_section_scores_all_five_models_and_claims_nothing_early() -> None:
    p = report_for(history())["prediction"]
    assert set(p["pooled"]) == {
        "uniform",
        "most_frequent",
        "decayed_fingerprint",
        "markov",
        "hybrid",
    }
    for scores in p["pooled"].values():
        assert scores["n"] == 240 and 0.0 <= scores["top1"] <= scores["top3"] <= 1.0
        assert scores["log_loss"] > 0
    assert p["status"] == "not_complete" and p["users_complete"] == 0
    only = next(iter(p["per_user"].values()))
    assert only["comparisons"] is None and only["hybrid_beats_required_baselines"] is None


def test_the_report_is_strict_json() -> None:
    json.dumps(report_for(history(), {1: 5, 2: 4, 3: 3, 4: 2, 5: 1, 6: 1}), allow_nan=False)


def test_the_markdown_says_what_was_not_run_and_what_is_below_the_minimum() -> None:
    text = render(report_for(history()))
    assert text.startswith("# Evaluation report")
    for heading in (
        "## 1. Topic labelling",
        "## 2. Fingerprint against self-report",
        "## 3. Prediction",
        "## 4. Fingerprint stability",
    ):
        assert heading in text
    assert "**Not run.**" in text  # no labelled set, no ratings
    assert "No comparison and no verdict: the minimum was not reached." in text
    assert "below the 500 Reel minimum" in text
    lowered = text.lower()
    for word in ("outperform", "successful", "works well", "accurate"):
        assert word not in lowered


def test_the_markdown_prints_a_miss_as_a_miss() -> None:
    bad = labelling_section(fake_report(0.5, 0.7, 200))
    report = report_for(history())
    report["labelling"] = bad
    text = render(report)
    assert "**Fused figures do not meet the bar**" in text
    assert "50.0%" in text


def test_a_complete_prediction_that_does_not_beat_the_baselines_says_so() -> None:
    config = PredictionConfig(min_events_for_conclusion=200, bootstrap_resamples=200)
    observations = history(8, 40)
    events = replay(observations, TOPICS, config, FP).events
    report = build_report(
        labelling=None,
        self_report=[],
        predictions={"u1": events},
        stability_inputs={},
        topics=TOPICS,
        fp_config=FP,
        prediction_config=config,
        seed=0,
        meta={},
    )
    p = report["prediction"]
    assert p["status"] == "complete"
    summary = p["per_user"]["u1"]
    assert summary["comparisons"] is not None
    text = render(report)
    if summary["hybrid_beats_required_baselines"]:
        assert "clearly lower than both required baselines" in text
    else:
        assert "did not clearly beat both required baselines" in text
    for name in ("uniform", "most_frequent", "decayed_fingerprint", "markov"):
        assert summary["comparisons"][name]["log_loss_verdict"] in {
            "hybrid_lower",
            "hybrid_higher",
            "no_clear_difference",
            "no_interval",
        }


def test_pure_noise_is_not_reported_as_a_win_for_the_hybrid() -> None:
    config = PredictionConfig(min_events_for_conclusion=300, bootstrap_resamples=300)
    rng = random.Random(7)
    observations = [
        obs(rng.choice(TOPICS), s * 1440 + i, session=s + 1) for s in range(10) for i in range(40)
    ]
    events = replay(observations, TOPICS, config, FP).events
    p = build_report(
        labelling=None,
        self_report=[],
        predictions={"u1": events},
        stability_inputs={},
        topics=TOPICS,
        fp_config=FP,
        prediction_config=config,
        seed=0,
        meta={},
    )["prediction"]
    assert p["per_user"]["u1"]["hybrid_beats_required_baselines"] is False


# ------------------------------------------------------------------ a figure that cannot be computed


def nan_report(reels: int) -> dict[str, Any]:
    nan = float("nan")

    def iv() -> dict[str, float]:
        return {"estimate": nan, "low": nan, "high": nan}

    mode = {"top1": iv(), "top3": iv(), "log_loss": iv()}
    return {
        "format": "x",
        "inputs": {"reels": reels},
        "sufficiency": {"failed": []},
        "defaults_on_all_reels": {
            "paired": {"n_reels": 0, "text_only": mode, "visual_only": mode, "fused": mode}
        },
    }


def test_nothing_scored_is_missing_not_nan_and_not_a_failed_bar() -> None:
    section = labelling_section(nan_report(200))
    assert section["passed"] is None  # not "failed": nothing was measured
    assert section["figures"]["fused"]["top1"]["estimate"] is None
    json.dumps(section, allow_nan=False)
    report = report_for(history())
    report["labelling"] = section
    text = render(report)
    assert "nan" not in text.lower()
    assert "No figure." in text and "do not meet the bar" not in text


def test_a_report_with_a_nan_anywhere_is_still_strict_json() -> None:
    report = build_report(
        labelling=nan_report(200),
        self_report=[],
        predictions={},
        stability_inputs={},
        topics=TOPICS,
        fp_config=FP,
        prediction_config=PredictionConfig(),
        seed=0,
        meta={},
    )
    json.dumps(report, allow_nan=False)
