"""Fingerprint read API (spec §8). Reads precomputed snapshots only, never raw exposures.

`GET /fingerprint/current?kind=` returns the newest snapshot of one kind with every topic's share
of the whole. Shares are recomputed here from the stored raw weights as `W_k / sum(W)`, so they sum
to 1 by construction whatever an older snapshot document held. No snapshot yet means an empty
`topics` list and `taken_at: null`; a distribution is never made up.

`GET /fingerprint/history?kind=&bucket=` returns the stored snapshots of one kind, one point per
`day` (the state after the last Reel of each UTC day) or per `session`, oldest first, newest
`limit` points. Only snapshots are read: the cost depends on the number of days or sessions asked
for, never on how many Reels a user has watched. If snapshots of several pipeline versions exist
(the pipeline was changed and replayed), the version of the newest snapshot is used, so one chart
never mixes two ways of computing a fingerprint. Days without a Reel have no point (the shares do
not change while no Reel is watched). Shares of every point sum to 1.
"""

import math
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.auth_deps import Principal, cookie_principal
from app.db import get_session
from app.models import FingerprintSnapshot, Topic

router = APIRouter(prefix="/fingerprint", tags=["fingerprint"])


class TopicShare(BaseModel):
    topic_id: int
    slug: str
    label: str
    #: Fraction of the fingerprint, 0 to 1. All shares of one response sum to 1.
    share: float


class FingerprintCurrent(BaseModel):
    kind: Literal["exposure", "engagement"]
    taken_at: datetime | None
    n_reels: int
    pipeline_version: str | None
    session_id: uuid.UUID | None
    topics: list[TopicShare]


def shares_from_raw(raw: dict[str, float]) -> dict[int, float]:
    """`W_k / sum(W)`, sorted by topic id for a stable sum. Empty if the total is not positive."""
    weights = {int(k): float(v) for k, v in raw.items()}
    total = math.fsum(weights[k] for k in sorted(weights))
    if total <= 0.0 or not math.isfinite(total):
        return {}
    return {k: weights[k] / total for k in sorted(weights)}


@router.get("/current", response_model=FingerprintCurrent)
def current_fingerprint(
    kind: Literal["exposure", "engagement"],
    principal: Annotated[Principal, Depends(cookie_principal)],
    db: Annotated[DbSession, Depends(get_session)],
) -> FingerprintCurrent:
    snapshot = db.execute(
        select(FingerprintSnapshot)
        .where(FingerprintSnapshot.user_id == principal.user_id, FingerprintSnapshot.kind == kind)
        .order_by(FingerprintSnapshot.taken_at.desc(), FingerprintSnapshot.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if snapshot is None:
        return FingerprintCurrent(
            kind=kind, taken_at=None, n_reels=0, pipeline_version=None, session_id=None, topics=[]
        )
    shares = shares_from_raw(snapshot.weights.get("raw", {}))
    labels = {
        t.id: t for t in db.execute(select(Topic).where(Topic.id.in_(list(shares)))).scalars()
    }
    # A topic row missing here would drop its share and break the sum; the FK on topic_assignments
    # makes that impossible, but fall back to the id instead of dropping anything.
    topics = [
        TopicShare(
            topic_id=k,
            slug=labels[k].slug if k in labels else f"topic-{k}",
            label=labels[k].label if k in labels else f"Topic {k}",
            share=v,
        )
        for k, v in shares.items()
    ]
    topics.sort(key=lambda t: (-t.share, t.topic_id))
    return FingerprintCurrent(
        kind=kind,
        taken_at=snapshot.taken_at,
        n_reels=snapshot.n_reels,
        pipeline_version=snapshot.pipeline_version,
        session_id=snapshot.session_id,
        topics=topics,
    )


HISTORY_DEFAULT_LIMIT = 90
HISTORY_MAX_LIMIT = 366


class HistoryTopic(BaseModel):
    topic_id: int
    slug: str
    label: str


class HistoryPoint(BaseModel):
    taken_at: datetime
    session_id: uuid.UUID | None
    #: Reels counted so far, in total.
    n_reels: int
    #: Reels counted inside this day or session alone; None for snapshots without the figure.
    reels_in_bucket: int | None
    #: Topic id (as text, JSON keys are text) to share, 0 to 1. Sums to 1 for a non-empty point.
    shares: dict[str, float]


class FingerprintHistory(BaseModel):
    kind: Literal["exposure", "engagement"]
    bucket: Literal["day", "session"]
    pipeline_version: str | None
    #: Every topic that appears in any point, with its label, by topic id.
    topics: list[HistoryTopic]
    #: Oldest first.
    points: list[HistoryPoint]


@router.get("/history", response_model=FingerprintHistory)
def fingerprint_history(
    kind: Literal["exposure", "engagement"],
    bucket: Literal["day", "session"],
    principal: Annotated[Principal, Depends(cookie_principal)],
    db: Annotated[DbSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=HISTORY_MAX_LIMIT)] = HISTORY_DEFAULT_LIMIT,
) -> FingerprintHistory:
    where = (
        FingerprintSnapshot.user_id == principal.user_id,
        FingerprintSnapshot.kind == kind,
        FingerprintSnapshot.bucket == bucket,
    )
    version = db.execute(
        select(FingerprintSnapshot.pipeline_version)
        .where(*where)
        .order_by(FingerprintSnapshot.taken_at.desc(), FingerprintSnapshot.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if version is None:
        return FingerprintHistory(
            kind=kind, bucket=bucket, pipeline_version=None, topics=[], points=[]
        )
    rows = list(
        db.execute(
            select(FingerprintSnapshot)
            .where(*where, FingerprintSnapshot.pipeline_version == version)
            .order_by(FingerprintSnapshot.taken_at.desc(), FingerprintSnapshot.id.desc())
            .limit(limit)
        ).scalars()
    )
    rows.reverse()  # newest `limit` points, oldest first
    points: list[HistoryPoint] = []
    seen: set[int] = set()
    for row in rows:
        shares = shares_from_raw(row.weights.get("raw", {}))
        seen.update(shares)
        points.append(
            HistoryPoint(
                taken_at=row.taken_at,
                session_id=row.session_id,
                n_reels=row.n_reels,
                reels_in_bucket=(
                    int(row.weights["bucket_reels"])
                    if isinstance(row.weights.get("bucket_reels"), int)
                    else None
                ),
                shares={str(k): v for k, v in shares.items()},
            )
        )
    labels = {t.id: t for t in db.execute(select(Topic).where(Topic.id.in_(seen))).scalars()}
    topics = [
        HistoryTopic(
            topic_id=k,
            slug=labels[k].slug if k in labels else f"topic-{k}",
            label=labels[k].label if k in labels else f"Topic {k}",
        )
        for k in sorted(seen)
    ]
    return FingerprintHistory(
        kind=kind, bucket=bucket, pipeline_version=version, topics=topics, points=points
    )
