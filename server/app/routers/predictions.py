"""Next-topic prediction read API (spec §8, F12): `GET /api/v1/predictions/next`.

Returns, from one stored row, the distribution for the Reel that has not happened yet from each
of the five models, the headline (hybrid) one, and the evaluation of all earlier predictions.

Wording is fixed: the prediction is named `Predicted next observed topic` and is about which topic
the feed shows next, never about what the user likes. The evaluation says `complete: false` and
carries no comparison and no verdict until the minimum number of Reels has been scored (spec §10),
and then states only what the intervals show. Nothing is computed here: the numbers were stored
when the predictions were rebuilt, so a request costs two small reads and no history scan.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.auth_deps import Principal, cookie_principal
from app.db import get_session
from app.models import Topic
from app.prediction.config import MODELS, PREDICTION_LABEL, PREDICTION_NOTE
from app.prediction.store import latest_state

router = APIRouter(prefix="/predictions", tags=["predictions"])

TOP_SHOWN = 5

MODEL_NAMES = {
    "uniform": "Every topic equally likely",
    "most_frequent": "Your most frequent topics",
    "decayed_fingerprint": "Your fingerprint (recent Reels weigh more)",
    "markov": "What usually follows the last topic",
    "hybrid": "Fingerprint and what follows the last topic, combined",
}


class PredictedTopic(BaseModel):
    topic_id: int
    slug: str
    label: str
    probability: float


class ModelPrediction(BaseModel):
    model: str
    name: str
    #: The most likely topics, most likely first.
    top: list[PredictedTopic]
    #: Probability of all the topics not listed in `top`. `top` and this sum to 1.
    rest_probability: float


class NextPrediction(BaseModel):
    #: Always "Predicted next observed topic".
    label: str
    note: str
    available: bool
    reason: str | None
    pipeline_version: str | None
    as_of: datetime | None
    #: Reels the predictions were built from.
    based_on_reels: int
    last_topic: PredictedTopic | None
    #: True when the fingerprint stood in for the transition history (too few observations).
    backoff: bool
    row_observations: int | None
    min_row_observations: int | None
    hybrid_lambda: float | None
    hybrid: ModelPrediction | None
    models: list[ModelPrediction]
    #: Scored predictions per model (all time and rolling) and, once complete, the comparison.
    evaluation: dict[str, Any] | None


def _unavailable(reason: str) -> NextPrediction:
    return NextPrediction(
        label=PREDICTION_LABEL,
        note=PREDICTION_NOTE,
        available=False,
        reason=reason,
        pipeline_version=None,
        as_of=None,
        based_on_reels=0,
        last_topic=None,
        backoff=False,
        row_observations=None,
        min_row_observations=None,
        hybrid_lambda=None,
        hybrid=None,
        models=[],
        evaluation=None,
    )


@router.get("/next", response_model=NextPrediction)
def next_prediction(
    principal: Annotated[Principal, Depends(cookie_principal)],
    db: Annotated[DbSession, Depends(get_session)],
) -> NextPrediction:
    row = latest_state(db, principal.user_id)
    if row is None:
        return _unavailable("no_predictions")
    doc = row.state
    ids = {int(k) for dist in doc["next"]["models"].values() for k in dist}
    last = doc["next"]["last_topic"]
    if last is not None:
        ids.add(int(last))
    names = {t.id: t for t in db.execute(select(Topic).where(Topic.id.in_(ids))).scalars()}

    def topic(topic_id: int, probability: float) -> PredictedTopic:
        t = names.get(topic_id)
        return PredictedTopic(
            topic_id=topic_id,
            slug=t.slug if t else f"topic-{topic_id}",
            label=t.label if t else f"Topic {topic_id}",
            probability=probability,
        )

    def model(name: str) -> ModelPrediction:
        dist = {int(k): float(v) for k, v in doc["next"]["models"][name].items()}
        order = sorted(dist, key=lambda k: (-dist[k], k))
        shown = order[:TOP_SHOWN]
        return ModelPrediction(
            model=name,
            name=MODEL_NAMES[name],
            top=[topic(k, dist[k]) for k in shown],
            rest_probability=max(0.0, 1.0 - sum(dist[k] for k in shown)),
        )

    n_events = int(doc["n_events"])
    nxt = doc["next"]
    return NextPrediction(
        label=PREDICTION_LABEL,
        note=PREDICTION_NOTE,
        available=n_events > 0,
        reason=None if n_events > 0 else "no_observed_reels",
        pipeline_version=doc["prediction_version"],
        as_of=datetime.fromisoformat(doc["as_of"]) if doc.get("as_of") else None,
        based_on_reels=n_events,
        last_topic=None if last is None else topic(int(last), 1.0),
        backoff=bool(nxt["backoff"]),
        row_observations=nxt["row_observations"],
        min_row_observations=nxt["min_row_observations"],
        hybrid_lambda=float(nxt["lambda_used"]),
        hybrid=model("hybrid") if n_events > 0 else None,
        models=[model(m) for m in MODELS] if n_events > 0 else [],
        evaluation=doc["evaluation"],
    )
