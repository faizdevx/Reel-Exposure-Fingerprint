"""The five next-topic models and the prequential replay. Pure: no database, clock or randomness.

State per user: topic counts, transition counts between consecutive observed topics, the last
topic, the exposure fingerprint, and the running log loss of each `lambda` of the grid. Every
Reel goes through the same two steps, in this order:

    1. predict(state)           uses only what earlier Reels left in the state
    2. update(state, the Reel)  only now does the Reel change the state

so a prediction can never contain information from the Reel it is about (a test changes that Reel
and checks that its prediction does not move). The observed topic of a Reel is the top topic of its
fused distribution (ties go to the lower topic id); `other` (id 0) is a topic when the universe has
it. Reels with no topic distribution are not part of the sequence, as in the fingerprint.

Add-one smoothing (spec §5.2): `p(k) = (count_k + a) / (total + a * |U|)`, `a = 1`.
Backoff (spec §5.2): when the row of the last topic has fewer than `min_row_observations`
observations, or there is no last topic, the transition models use the fingerprint distribution.

Log loss uses `-ln(max(p, 1e-12))`: a model that gives an event zero probability is charged a large
but finite loss.
"""

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.fingerprint.engine import (
    FingerprintConfig,
    FingerprintState,
    Observation,
    apply_observation,
    order_key,
)
from app.prediction.config import MODELS, PredictionConfig

LOSS_FLOOR = 1e-12


class PredictionError(Exception):
    """Input the prediction models cannot use."""


def hard_topic(probabilities: Mapping[int, float]) -> int:
    """The observed topic of a Reel: its most likely topic, ties to the lower id."""
    return min(probabilities, key=lambda k: (-probabilities[k], k))


def log_loss(distribution: Mapping[int, float], actual: int) -> float:
    return -math.log(max(distribution.get(actual, 0.0), LOSS_FLOOR))


def ranked(distribution: Mapping[int, float]) -> list[int]:
    """Topic ids, most likely first, ties to the lower id."""
    return sorted(distribution, key=lambda k: (-distribution[k], k))


def smoothed(counts: Mapping[int, int], universe: Sequence[int], a: float) -> dict[int, float]:
    """`(count + a) / (total + a * |U|)` over the universe; uniform when there is no data."""
    total = sum(counts.get(k, 0) for k in universe)
    denominator = total + a * len(universe)
    return {k: (counts.get(k, 0) + a) / denominator for k in universe}


def fingerprint_distribution(fp: FingerprintState, universe: Sequence[int]) -> dict[int, float]:
    """The exposure fingerprint's share of each topic, over the universe (uniform if empty)."""
    weights = {k: max(0.0, fp.weights.get(k, 0.0)) for k in universe}
    total = math.fsum(weights[k] for k in sorted(weights))
    if total <= 0.0:
        return {k: 1.0 / len(universe) for k in universe}
    return {k: weights[k] / total for k in universe}


@dataclass
class ModelState:
    universe: tuple[int, ...]
    counts: dict[int, int] = field(default_factory=dict)
    transitions: dict[int, dict[int, int]] = field(default_factory=dict)
    last_topic: int | None = None
    fingerprint: FingerprintState = field(default_factory=lambda: FingerprintState("exposure"))
    #: Cumulative hybrid log loss of each lambda over the Reels so far.
    lambda_loss: dict[float, float] = field(default_factory=dict)
    n: int = 0


@dataclass(frozen=True)
class Prediction:
    models: dict[str, dict[int, float]]
    lambda_used: float
    #: True when the transition models fell back to the fingerprint.
    backoff: bool
    #: Observations in the row of the last topic, None when there is no last topic.
    row_observations: int | None
    last_topic: int | None
    #: The hybrid under every lambda of the grid, to score them once the topic is known.
    hybrid_by_lambda: dict[float, dict[int, float]]


def new_state(universe: Sequence[int], config: PredictionConfig) -> ModelState:
    if not universe or len(set(universe)) != len(universe):
        raise PredictionError("the universe must be a non-empty set of topics")
    return ModelState(
        universe=tuple(sorted(universe)),
        lambda_loss={value: 0.0 for value in config.lambda_grid},
    )


def choose_lambda(state: ModelState, config: PredictionConfig) -> float:
    """Walk-forward choice: the grid value with the lowest log loss on the Reels already seen.

    Until `min_events_to_tune` Reels have been seen the default is used. Ties go to the value
    nearest the default, then to the smaller one.
    """
    if state.n < config.min_events_to_tune:
        return config.lambda_default
    return min(
        config.lambda_grid,
        key=lambda v: (state.lambda_loss[v], round(abs(v - config.lambda_default), 9), v),
    )


def predict(state: ModelState, config: PredictionConfig) -> Prediction:
    universe = state.universe
    a = config.smoothing
    uniform = {k: 1.0 / len(universe) for k in universe}
    frequent = smoothed(state.counts, universe, a)
    fingerprint = fingerprint_distribution(state.fingerprint, universe)

    row = state.transitions.get(state.last_topic, {}) if state.last_topic is not None else None
    row_n = None if row is None else sum(row.values())
    backoff = row_n is None or row_n < config.min_row_observations
    transition = fingerprint if backoff else smoothed(row or {}, universe, a)

    def hybrid(value: float) -> dict[int, float]:
        if backoff:
            return dict(fingerprint)
        return {k: value * fingerprint[k] + (1.0 - value) * transition[k] for k in universe}

    by_lambda = {value: hybrid(value) for value in config.lambda_grid}
    lam = choose_lambda(state, config)
    models = {
        "uniform": uniform,
        "most_frequent": frequent,
        "decayed_fingerprint": fingerprint,
        "markov": transition,
        "hybrid": by_lambda.get(lam) or hybrid(lam),
    }
    if tuple(models) != MODELS:  # the order is part of the contract
        raise PredictionError("model order changed")
    return Prediction(models, lam, backoff, row_n, state.last_topic, by_lambda)


def update(
    state: ModelState,
    obs: Observation,
    actual: int,
    prediction: Prediction,
    fp_config: FingerprintConfig,
) -> None:
    """Apply a Reel to the state, after its prediction was made."""
    for value, distribution in prediction.hybrid_by_lambda.items():
        state.lambda_loss[value] += log_loss(distribution, actual)
    if state.last_topic is not None:
        row = state.transitions.setdefault(state.last_topic, {})
        row[actual] = row.get(actual, 0) + 1
    state.counts[actual] = state.counts.get(actual, 0) + 1
    state.last_topic = actual
    state.fingerprint = apply_observation(state.fingerprint, obs, fp_config).state
    state.n += 1


@dataclass(frozen=True)
class EventPrediction:
    """What was predicted for one Reel, before it happened, and what it turned out to be."""

    event_id: uuid.UUID
    session_id: uuid.UUID
    observed_at: datetime
    actual_topic: int
    models: dict[str, dict[int, float]]
    lambda_used: float
    backoff: bool
    row_observations: int | None
    last_topic: int | None


@dataclass
class Replay:
    events: list[EventPrediction]
    state: ModelState
    #: The prediction for the Reel that has not happened yet.
    next: Prediction
    #: Reels left out because they have no topic distribution.
    skipped: int


def replay(
    observations: Sequence[Observation],
    universe: Sequence[int],
    config: PredictionConfig,
    fp_config: FingerprintConfig,
) -> Replay:
    """Walk a user's Reels in time order, predicting each before applying it."""
    ordered = sorted(observations, key=order_key)
    if len({o.event_id for o in ordered}) != len(ordered):
        raise PredictionError("duplicate event_id")
    state = new_state(universe, config)
    allowed = set(state.universe)
    events: list[EventPrediction] = []
    skipped = 0
    for obs in ordered:
        if obs.probabilities is None:
            skipped += 1
            continue
        if not set(obs.probabilities) <= allowed:
            raise PredictionError("a Reel has a topic outside the universe")
        prediction = predict(state, config)  # 1. from the state before this Reel
        actual = hard_topic(obs.probabilities)
        events.append(
            EventPrediction(
                event_id=obs.event_id,
                session_id=obs.session_id,
                observed_at=obs.observed_at,
                actual_topic=actual,
                models=prediction.models,
                lambda_used=prediction.lambda_used,
                backoff=prediction.backoff,
                row_observations=prediction.row_observations,
                last_topic=prediction.last_topic,
            )
        )
        update(state, obs, actual, prediction, fp_config)  # 2. only now does it change anything
    return Replay(events, state, predict(state, config), skipped)
