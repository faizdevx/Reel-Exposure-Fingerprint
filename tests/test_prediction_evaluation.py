"""Scoring the predictions and the rule that nothing is claimed before the evaluation is complete.

Pure: no database, no model.
"""

import json
import math
import random
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.fingerprint.engine import FingerprintConfig, Observation
from app.prediction.config import MODELS, PredictionConfig
from app.prediction.engine import EventPrediction, replay
from app.prediction.evaluation import scores_of, summarize

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
U = (1, 2, 3)
FP = FingerprintConfig()


def event(
    actual: int, dists: dict[str, dict[int, float]] | None = None, session: int = 1
) -> EventPrediction:
    uniform = {k: 1 / 3 for k in U}
    models = {m: dict(uniform) for m in MODELS}
    models.update(dists or {})
    return EventPrediction(
        uuid.uuid4(), uuid.UUID(int=session), T0, actual, models, 0.5, False, None, None
    )


def reels(topics: list[int], *, sessions: int = 10) -> list[Observation]:
    out = []
    for i, topic in enumerate(topics):
        probs = {k: (0.8 if k == topic else 0.1) for k in U}
        out.append(
            Observation(
                uuid.UUID(int=5000 + i),
                uuid.UUID(int=1 + i % sessions),
                T0 + timedelta(minutes=i),
                10_000,
                15_000,
                probs,
            )
        )
    return out


def alternating(n: int) -> list[int]:
    return [1 if i % 2 == 0 else 2 for i in range(n)]


SMALL = PredictionConfig(min_events_for_conclusion=60, rolling_window=10, min_row_observations=5)


# ---- one Reel, by hand


def test_scores_of_one_reel_by_hand() -> None:
    e = event(3, {"hybrid": {1: 0.5, 2: 0.3, 3: 0.2}})
    top1, top3, loss = scores_of(e, "hybrid")
    assert (top1, top3) == (0.0, 1.0)  # ranked third: not top-1, inside the top 3
    assert loss == pytest.approx(-math.log(0.2))
    top1u, top3u, lossu = scores_of(e, "uniform")
    assert top1u == 0.0  # a three-way tie goes to the lower id, which is not 3
    assert top3u == 1.0 and lossu == pytest.approx(math.log(3))


def test_top1_needs_the_most_likely_topic_and_top3_the_first_three() -> None:
    four = {1: 0.4, 2: 0.3, 3: 0.2, 4: 0.1}
    e = replace(event(4), models={m: four for m in MODELS})
    assert scores_of(e, "hybrid")[:2] == (0.0, 0.0)  # fourth: outside the top 3
    e = replace(event(1), models={m: four for m in MODELS})
    assert scores_of(e, "hybrid")[:2] == (1.0, 1.0)


# ---- all-time and rolling figures


def test_all_time_and_rolling_figures() -> None:
    certain_right = {1: 1.0, 2: 0.0, 3: 0.0}
    events = [event(1, {"hybrid": certain_right}) for _ in range(6)] + [
        event(2, {"hybrid": certain_right}) for _ in range(4)
    ]  # 6 right, then 4 wrong (the last 4)
    cfg = replace(SMALL, rolling_window=5, min_events_for_conclusion=1000)
    out = summarize(events, cfg)["models"]["hybrid"]
    assert out["all_time"]["n"] == 10 and out["all_time"]["top1"] == pytest.approx(0.6)
    # The last 5 Reels: one right (index 5), four wrong.
    assert out["rolling"]["n"] == 5 and out["rolling"]["top1"] == pytest.approx(0.2)
    # With three topics the top three is everything, so top-3 is always a hit here.
    assert out["rolling"]["top3"] == pytest.approx(1.0)


def test_a_short_history_has_a_short_window() -> None:
    out = summarize([event(1), event(2)], replace(SMALL, rolling_window=100))
    assert out["models"]["uniform"]["rolling"]["n"] == 2


def test_no_events_gives_empty_figures_not_zeros() -> None:
    out = summarize([], SMALL)
    assert out["scored_reels"] == 0 and out["complete"] is False
    assert out["models"]["hybrid"]["all_time"] == {
        "n": 0,
        "top1": None,
        "top3": None,
        "log_loss": None,
    }


# ---- nothing is claimed before the evaluation is complete


def test_before_the_minimum_there_is_no_comparison_and_no_verdict() -> None:
    result = replay(reels(alternating(59)), U, SMALL, FP)
    out = summarize(result.events, SMALL)
    assert out["complete"] is False
    assert out["comparisons"] is None and out["hybrid_beats_required_baselines"] is None
    assert out["status"] == "not complete: 59 of 60 Reels scored"
    # The figures are still there, flagged by `complete: false` for the reader to qualify.
    assert out["models"]["hybrid"]["all_time"]["n"] == 59
    assert "verdict" not in json.dumps(out)


def test_the_minimum_is_inclusive() -> None:
    assert summarize(replay(reels(alternating(60)), U, SMALL, FP).events, SMALL)["complete"] is True
    assert (
        summarize(replay(reels(alternating(59)), U, SMALL, FP).events, SMALL)["complete"] is False
    )


def test_a_clearly_better_hybrid_is_reported_with_intervals_once_complete() -> None:
    config = replace(SMALL, min_events_for_conclusion=100, min_events_to_tune=30)
    result = replay(reels(alternating(300)), U, config, FP)
    out = summarize(result.events, config)
    assert out["complete"] and out["status"] == "complete"
    comparisons = out["comparisons"]
    assert set(comparisons) == {"uniform", "most_frequent", "decayed_fingerprint", "markov"}
    for baseline in ("uniform", "most_frequent", "decayed_fingerprint"):
        c = comparisons[baseline]
        assert c["log_loss_verdict"] == "hybrid_lower"
        assert c["log_loss_difference"]["high"] < 0  # the interval excludes zero
        assert c["log_loss_difference"]["n"] == 300
    assert out["hybrid_beats_required_baselines"] is True


def test_pure_chance_is_not_called_a_win() -> None:
    rng = random.Random(3)
    config = replace(SMALL, min_events_for_conclusion=100, min_row_observations=20)
    result = replay(reels([rng.choice(U) for _ in range(400)]), U, config, FP)
    out = summarize(result.events, config)
    assert out["complete"]
    assert out["hybrid_beats_required_baselines"] is False
    for baseline in ("most_frequent", "decayed_fingerprint"):
        assert out["comparisons"][baseline]["log_loss_verdict"] != "hybrid_lower"


def test_the_summary_is_strict_json_even_with_a_single_session() -> None:
    config = replace(SMALL, min_events_for_conclusion=50)
    result = replay(reels(alternating(80), sessions=1), U, config, FP)  # one cluster: no interval
    out = summarize(result.events, config)
    text = json.dumps(out, allow_nan=False)  # raises on NaN or infinity
    assert out["comparisons"]["uniform"]["log_loss_difference"]["low"] is None
    assert out["comparisons"]["uniform"]["log_loss_verdict"] == "no_interval"
    assert "NaN" not in text
    assert out["hybrid_beats_required_baselines"] is False  # no interval, no claim


def test_the_comparison_is_reproducible() -> None:
    config = replace(SMALL, min_events_for_conclusion=100)
    events = replay(reels(alternating(150)), U, config, FP).events
    assert summarize(events, config) == summarize(events, config)
    other_seed = replace(config, bootstrap_seed=1)
    assert summarize(events, other_seed)["comparisons"] != summarize(events, config)["comparisons"]


def test_the_evaluation_names_the_models_it_scored() -> None:
    out = summarize(replay(reels(alternating(20)), U, SMALL, FP).events, SMALL)
    assert list(out["models"]) == list(MODELS)
    assert out["required_reels"] == 60 and out["rolling_window"] == 10
