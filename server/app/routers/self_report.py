"""Self-report survey (evaluation 2, spec §10): the user rates each topic from 1 to 5.

The survey is shown before the user's first look at a fingerprint and answered once, so the
ratings are not shaped by what the system displays. Submitting needs a rating for every topic (the
evaluation correlates whole rankings); skipping is allowed and removes the user from that
evaluation. A completed survey cannot be changed: repeating the same answer is accepted, anything
else is 409. Deleting all data removes it.

`GET /self-report` -> status (`pending`, `submitted`, `skipped`), the topics to rate, and the
ratings once submitted. `PUT /self-report` submits. `POST /self-report/skip` skips.
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, StrictInt, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.auth_deps import Principal, cookie_principal
from app.db import get_session
from app.models import SelfReportRating, SurveyResponse, Topic
from app.schemas import StrictModel
from app.worker.fusion import OTHER_TOPIC_ID

router = APIRouter(prefix="/self-report", tags=["self-report"])

MIN_RATING, MAX_RATING = 1, 5


class SurveyTopic(BaseModel):
    topic_id: int
    slug: str
    label: str


class SurveyState(BaseModel):
    status: Literal["pending", "submitted", "skipped"]
    completed_at: datetime | None
    #: The topics to rate (the taxonomy; `other` is not a topic of interest).
    topics: list[SurveyTopic]
    #: Topic id (as text) to rating, only once submitted.
    ratings: dict[str, int]


class SubmitRatings(StrictModel):
    #: Topic id (as text, JSON keys are text) to a whole number from 1 to 5. `StrictInt` refuses
    #: "4", 4.0 and true, which lax parsing would turn into ratings.
    ratings: dict[str, StrictInt]

    @field_validator("ratings")
    @classmethod
    def _range(cls, value: dict[str, StrictInt]) -> dict[str, StrictInt]:
        for key, rating in value.items():
            if not key.isdigit():
                raise ValueError("keys must be topic ids")
            if not MIN_RATING <= rating <= MAX_RATING:
                raise ValueError("ratings are whole numbers from 1 to 5")
        return value


def _topics(db: DbSession) -> list[Topic]:
    return list(
        db.execute(select(Topic).where(Topic.id != OTHER_TOPIC_ID).order_by(Topic.id)).scalars()
    )


def _state(db: DbSession, user_id: object) -> SurveyState:
    topics = _topics(db)
    response = db.get(SurveyResponse, user_id)
    ratings: dict[str, int] = {}
    if response is not None and response.status == "submitted":
        ratings = {
            str(r.topic_id): int(r.rating)
            for r in db.execute(
                select(SelfReportRating).where(SelfReportRating.user_id == user_id)
            ).scalars()
        }
    return SurveyState(
        status="pending" if response is None else response.status,
        completed_at=None if response is None else response.completed_at,
        topics=[SurveyTopic(topic_id=t.id, slug=t.slug, label=t.label) for t in topics],
        ratings=ratings,
    )


@router.get("", response_model=SurveyState)
def get_survey(
    principal: Annotated[Principal, Depends(cookie_principal)],
    db: Annotated[DbSession, Depends(get_session)],
) -> SurveyState:
    return _state(db, principal.user_id)


@router.put("", response_model=SurveyState)
def submit_survey(
    body: SubmitRatings,
    principal: Annotated[Principal, Depends(cookie_principal)],
    db: Annotated[DbSession, Depends(get_session)],
) -> SurveyState:
    topics = _topics(db)
    wanted = {str(t.id) for t in topics}
    if not wanted:
        raise HTTPException(status_code=409, detail="no_topics_to_rate")
    if set(body.ratings) != wanted:
        raise HTTPException(status_code=422, detail="rate_every_topic")

    existing = db.get(SurveyResponse, principal.user_id)
    if existing is not None:
        current = _state(db, principal.user_id)
        if existing.status == "submitted" and current.ratings == body.ratings:
            return current  # the same answer again: nothing changes
        raise HTTPException(status_code=409, detail="already_completed")

    db.add(SurveyResponse(user_id=principal.user_id, status="submitted"))
    db.flush()
    for topic in topics:
        db.add(
            SelfReportRating(
                user_id=principal.user_id, topic_id=topic.id, rating=body.ratings[str(topic.id)]
            )
        )
    db.commit()
    return _state(db, principal.user_id)


@router.post("/skip", response_model=SurveyState)
def skip_survey(
    principal: Annotated[Principal, Depends(cookie_principal)],
    db: Annotated[DbSession, Depends(get_session)],
) -> SurveyState:
    existing = db.get(SurveyResponse, principal.user_id)
    if existing is not None:
        if existing.status == "skipped":
            return _state(db, principal.user_id)
        raise HTTPException(status_code=409, detail="already_completed")
    db.add(SurveyResponse(user_id=principal.user_id, status="skipped"))
    db.commit()
    return _state(db, principal.user_id)
