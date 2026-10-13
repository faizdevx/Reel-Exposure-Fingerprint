"""Read what the evaluation needs from the database (read-only), and run `research.build_report`."""

import hashlib
import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.evaluation.research import SelfReportUser, build_report, replay_user
from app.fingerprint.engine import FingerprintConfig, Observation, fingerprint_version
from app.fingerprint.store import load_observations, users_with_exposures
from app.models import SelfReportRating, SurveyResponse
from app.prediction.config import PredictionConfig, prediction_version
from app.prediction.engine import EventPrediction
from app.prediction.store import universe_for


def collect(
    db: DbSession,
    *,
    assignment_version: str,
    labelling: Mapping[str, Any] | None,
    labelling_file: Path | None,
    fp_config: FingerprintConfig,
    prediction_config: PredictionConfig,
    seed: int,
) -> dict[str, Any]:
    users = users_with_exposures(db)
    universes = {u: universe_for(db, assignment_version, u) for u in users}
    topics = sorted({t for universe in universes.values() for t in universe if t != 0})

    observations: dict[uuid.UUID, list[Observation]] = {
        u: load_observations(db, u, assignment_version, universes[u]) for u in users
    }
    scored = {u: [o for o in obs if o.probabilities is not None] for u, obs in observations.items()}

    survey: list[SelfReportUser] = []
    for user in users:
        response = db.get(SurveyResponse, user)
        ratings: dict[int, int] = {}
        if response is not None and response.status == "submitted":
            ratings = {
                int(r.topic_id): int(r.rating)
                for r in db.execute(
                    select(SelfReportRating).where(SelfReportRating.user_id == user)
                ).scalars()
            }
        survey.append(
            SelfReportUser(
                user_id=user,
                ratings=ratings,
                survey_completed_at=response.completed_at if response is not None else None,
                observations=scored[user],
            )
        )

    predictions: dict[str, list[EventPrediction]] = {
        str(u): replay_user(obs, universes[u], prediction_config, fp_config)
        for u, obs in scored.items()
    }

    fp_version = fingerprint_version(fp_config, assignment_version)
    meta: dict[str, Any] = {
        "assignment_version": assignment_version,
        "fingerprint_version": fp_version,
        "prediction_version": prediction_version(prediction_config, fp_version),
        "users": len(users),
        "reels_with_a_topic": sum(len(v) for v in scored.values()),
        "labelling_report": None
        if labelling_file is None
        else hashlib.sha256(labelling_file.read_bytes()).hexdigest()[:16],
    }
    return build_report(
        labelling=labelling,
        self_report=survey,
        predictions=predictions,
        stability_inputs={str(u): obs for u, obs in scored.items()},
        topics=topics,
        fp_config=fp_config,
        prediction_config=prediction_config,
        seed=seed,
        meta=meta,
    )


def load_labelling_report(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") is None or "defaults_on_all_reels" not in data:
        raise ValueError(f"{path} is not an `app.evaluation evaluate` report")
    return data
