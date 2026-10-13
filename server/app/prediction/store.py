"""Database edge of the prediction system: rebuild a user's predictions, read the stored result.

`rebuild_predictions` replays one user's Reels from stored assignments (`engine.replay`) and writes
everything in one go:

- `predictions`: one row per Reel and model, with the distribution as it was before the Reel, the
  observed topic, and the top-1 and top-3 hits. Rows of the same user and prediction version are
  replaced, so a rebuild leaves one set.
- `topic_transitions`: the user's counts of consecutive observed topics (replaced whole).
- `prediction_state`: the prediction for the Reel that has not happened yet, the state it came
  from, and the evaluation summary, so reading them costs one row and never a scan of history.

The caller commits. Nothing here reads the clock: the same stored Reels and settings give the same
rows.
"""

import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import delete, exists, insert, select
from sqlalchemy.orm import Session as DbSession

from app.fingerprint.engine import FingerprintConfig, fingerprint_version
from app.fingerprint.store import load_observations
from app.models import Prediction, PredictionState, Topic, TopicAssignment, TopicTransition
from app.prediction.config import (
    PREDICTION_LABEL,
    PREDICTION_NOTE,
    PredictionConfig,
    prediction_version,
)
from app.prediction.engine import EventPrediction, Replay, replay
from app.prediction.evaluation import summarize
from app.worker.fusion import OTHER_TOPIC_ID

STATE_SCHEMA = "prediction-state/1"
_CHUNK = 2000


@dataclass(frozen=True)
class PredictionRebuild:
    pipeline_version: str
    events: int
    skipped: int
    prediction_rows: int
    transition_rows: int


def universe_for(db: DbSession, assignment_version: str, user_id: uuid.UUID) -> list[int]:
    """The topics a Reel of this assignment version can have for this user: the taxonomy, plus
    `other` when the version has assigned it to one of the user's own Reels. Only the user's own
    rows count, so no other user can change the universe a user's predictions are made over."""
    ids = [
        t
        for (t,) in db.execute(
            select(Topic.id).where(Topic.id != OTHER_TOPIC_ID).order_by(Topic.id)
        )
    ]
    other_used = db.execute(
        select(
            exists().where(
                TopicAssignment.user_id == user_id,
                TopicAssignment.pipeline_version == assignment_version,
                TopicAssignment.topic_id == OTHER_TOPIC_ID,
            )
        )
    ).scalar_one()
    return [OTHER_TOPIC_ID, *ids] if other_used else ids


def _prediction_rows(
    user_id: uuid.UUID, version: str, events: Iterable[EventPrediction]
) -> Iterable[dict[str, Any]]:
    for event in events:
        for model, distribution in event.models.items():
            order = sorted(distribution, key=lambda k: (-distribution[k], k))
            yield {
                "user_id": user_id,
                "made_before_event": event.event_id,
                "model": model,
                "distribution": {str(k): v for k, v in distribution.items()},
                "actual_topic": event.actual_topic,
                "hit_top1": order[0] == event.actual_topic,
                "hit_top3": event.actual_topic in order[:3],
                "pipeline_version": version,
            }


def state_document(
    result: Replay,
    *,
    version: str,
    fp_version: str,
    assignment_version: str,
    config: PredictionConfig,
    evaluation: dict[str, Any],
    as_of: str | None,
    fingerprint_params: dict[str, Any],
) -> dict[str, Any]:
    prediction = result.next
    return {
        "schema": STATE_SCHEMA,
        "label": PREDICTION_LABEL,
        "note": PREDICTION_NOTE,
        "prediction_version": version,
        "fingerprint_version": fp_version,
        "assignment_version": assignment_version,
        # How to rebuild this exactly (deletion rebuilds predictions from these).
        "fingerprint_params": fingerprint_params,
        "prediction_params": asdict(config),
        "as_of": as_of,
        "n_events": len(result.events),
        "skipped_reels": result.skipped,
        "universe": list(result.state.universe),
        "next": {
            "last_topic": prediction.last_topic,
            "lambda_used": prediction.lambda_used,
            "backoff": prediction.backoff,
            "row_observations": prediction.row_observations,
            "min_row_observations": config.min_row_observations,
            "models": {
                model: {str(k): v for k, v in distribution.items()}
                for model, distribution in prediction.models.items()
            },
        },
        "evaluation": evaluation,
    }


def rebuild_predictions(
    db: DbSession,
    user_id: uuid.UUID,
    *,
    assignment_version: str,
    fp_config: FingerprintConfig,
    config: PredictionConfig,
) -> PredictionRebuild:
    universe = universe_for(db, assignment_version, user_id)
    taxonomy_ids = [t for t in universe if t != OTHER_TOPIC_ID]
    observations = load_observations(db, user_id, assignment_version, taxonomy_ids)
    result = replay(observations, universe, config, fp_config) if universe else None

    fp_version = fingerprint_version(fp_config, assignment_version)
    version = prediction_version(config, fp_version)

    db.execute(
        delete(Prediction).where(
            Prediction.user_id == user_id, Prediction.pipeline_version == version
        )
    )
    db.execute(delete(TopicTransition).where(TopicTransition.user_id == user_id))
    db.execute(
        delete(PredictionState).where(
            PredictionState.user_id == user_id, PredictionState.pipeline_version == version
        )
    )
    if result is None:
        return PredictionRebuild(version, 0, 0, 0, 0)

    rows = list(_prediction_rows(user_id, version, result.events))
    for start in range(0, len(rows), _CHUNK):
        db.execute(insert(Prediction), rows[start : start + _CHUNK])

    transitions = [
        {"user_id": user_id, "from_topic": a, "to_topic": b, "n": n}
        for a, row in sorted(result.state.transitions.items())
        for b, n in sorted(row.items())
        if n > 0
    ]
    if transitions:
        db.execute(insert(TopicTransition), transitions)

    last = result.events[-1].observed_at if result.events else None
    evaluation = summarize(result.events, config)
    document = state_document(
        result,
        version=version,
        fp_version=fp_version,
        assignment_version=assignment_version,
        config=config,
        evaluation=evaluation,
        as_of=last.isoformat() if last is not None else None,
        fingerprint_params=asdict(fp_config),
    )
    db.add(PredictionState(user_id=user_id, pipeline_version=version, state=document))
    db.flush()
    return PredictionRebuild(
        version, len(result.events), result.skipped, len(rows), len(transitions)
    )


def latest_state(db: DbSession, user_id: uuid.UUID) -> PredictionState | None:
    return db.execute(
        select(PredictionState)
        .where(PredictionState.user_id == user_id)
        .order_by(PredictionState.updated_at.desc(), PredictionState.pipeline_version)
        .limit(1)
    ).scalar_one_or_none()
