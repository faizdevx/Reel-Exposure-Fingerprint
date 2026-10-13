"""Topic evidence (spec F11, §8): why a topic is in the current fingerprint.

`GET /topics/{topic_id}/evidence?kind=&limit=&offset=` lists the Reels that put the topic into the
user's current `exposure` or `engagement` fingerprint, biggest contribution first, one page at a
time. Each item carries what produced its contribution:

    contribution = fused topic score x gain x recency

so the reader can see the three reasons: the Reel was about the topic (score), it counted for much
(gain: exposure 1, engagement by dwell), and it is recent (the half-life). The contributions add up
to the topic's weight in the stored snapshot, and `share_of_topic` is each Reel's part of it
(`app.fingerprint.evidence` has the formula).

Authorisation: `user_id` comes from the login cookie, every row read starts from that user's own
exposures, and another user's Reels cannot appear because they are never selected. A topic id that
does not exist is 404. A user with no fingerprint, no weight for the topic, or a snapshot made
before provenance was recorded gets 200 with an explanation and no items, never a guess.
"""

import math
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.auth_deps import Principal, cookie_principal
from app.db import get_session
from app.fingerprint.evidence import (
    EVIDENCE_DEFAULT_LIMIT,
    EVIDENCE_MAX_LIMIT,
    evidence_page,
    provenance_of,
)
from app.models import FingerprintSnapshot, Topic

router = APIRouter(prefix="/topics", tags=["topics"])

MAX_OFFSET = 1_000_000


class EvidenceTopic(BaseModel):
    id: int
    slug: str
    label: str


class EvidenceFingerprint(BaseModel):
    taken_at: datetime
    pipeline_version: str
    #: Reels counted in this fingerprint.
    n_reels: int
    half_life_days: float
    #: The topic's weight, the sum of every weight, and their ratio: the share on the dashboard.
    topic_weight: float
    total_weight: float
    topic_share: float


class EvidenceExplanation(BaseModel):
    #: How a Reel counts in this kind of fingerprint, in words.
    gain_rule: str
    #: `contribution = score x gain x recency`, in words.
    formula: str
    #: Reels with any contribution to this topic, and the sum of what they contributed.
    contributing_reels: int
    contributions_sum: float
    #: `contributions_sum / topic_weight`: 1.0 means the list explains the whole weight.
    coverage: float


class EvidenceReel(BaseModel):
    event_id: uuid.UUID
    session_id: uuid.UUID
    reel_shortcode: str
    reel_url: str
    caption: str | None
    caption_truncated: bool
    observed_at: datetime
    dwell_ms: int | None
    video_duration_ms: int | None
    #: The Reel's fused score for this topic, 0 to 1 (its distribution over all topics sums to 1).
    topic_score: float
    p_text: float | None
    p_visual: float | None
    gain: float
    #: Recency factor, 0 to 1: 1 for the newest Reel, 0.5 after one half-life.
    recency: float
    #: score x gain x recency, in the same units as the topic weight.
    contribution: float
    #: This Reel's part of the topic's weight, 0 to 1.
    share_of_topic: float


class TopicEvidence(BaseModel):
    topic: EvidenceTopic
    kind: Literal["exposure", "engagement"]
    #: False when there is nothing to explain; `reason` says why.
    evidence_available: bool
    reason: (
        Literal["no_fingerprint", "topic_not_in_fingerprint", "snapshot_without_provenance"] | None
    )
    fingerprint: EvidenceFingerprint | None
    explanation: EvidenceExplanation | None
    total: int
    limit: int
    offset: int
    next_offset: int | None
    items: list[EvidenceReel]


GAIN_RULES = {
    "exposure": "Every Reel the feed showed you counts the same, once (gain 1).",
    "engagement": (
        "A Reel counts by how long you stayed: dwell divided by the Reel's length (at least 5 "
        "seconds), kept between 0.1 and 2.0. Leaving within 1.5 seconds counts 0.1."
    ),
}
FORMULA = (
    "contribution = topic score x gain x recency. Recency halves every half-life, counted back "
    "from the last Reel in the fingerprint."
)


def _empty(
    topic: Topic,
    kind: Literal["exposure", "engagement"],
    reason: Literal["no_fingerprint", "topic_not_in_fingerprint", "snapshot_without_provenance"],
    limit: int,
    offset: int,
    fingerprint: EvidenceFingerprint | None = None,
) -> TopicEvidence:
    return TopicEvidence(
        topic=EvidenceTopic(id=topic.id, slug=topic.slug, label=topic.label),
        kind=kind,
        evidence_available=False,
        reason=reason,
        fingerprint=fingerprint,
        explanation=None,
        total=0,
        limit=limit,
        offset=offset,
        next_offset=None,
        items=[],
    )


@router.get("/{topic_id}/evidence", response_model=TopicEvidence)
def topic_evidence(
    topic_id: Annotated[int, Path(ge=0, le=32767)],
    kind: Literal["exposure", "engagement"],
    principal: Annotated[Principal, Depends(cookie_principal)],
    db: Annotated[DbSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=EVIDENCE_MAX_LIMIT)] = EVIDENCE_DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> TopicEvidence:
    topic = db.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="topic_not_found")
    snapshot = db.execute(
        select(FingerprintSnapshot)
        .where(FingerprintSnapshot.user_id == principal.user_id, FingerprintSnapshot.kind == kind)
        .order_by(FingerprintSnapshot.taken_at.desc(), FingerprintSnapshot.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if snapshot is None:
        return _empty(topic, kind, "no_fingerprint", limit, offset)

    raw = {k: float(v) for k, v in snapshot.weights.get("raw", {}).items()}
    total_weight = math.fsum(raw[k] for k in sorted(raw))
    topic_weight = raw.get(str(topic_id), 0.0)
    provenance = provenance_of(snapshot.weights)
    fingerprint = None
    if provenance is not None and total_weight > 0.0:
        fingerprint = EvidenceFingerprint(
            taken_at=snapshot.taken_at,
            pipeline_version=snapshot.pipeline_version,
            n_reels=snapshot.n_reels,
            half_life_days=provenance.config.half_life_days,
            topic_weight=topic_weight,
            total_weight=total_weight,
            topic_share=topic_weight / total_weight,
        )
    if provenance is None:
        return _empty(topic, kind, "snapshot_without_provenance", limit, offset)
    if topic_weight <= 0.0 or fingerprint is None:
        return _empty(topic, kind, "topic_not_in_fingerprint", limit, offset, fingerprint)

    page = evidence_page(
        db,
        user_id=principal.user_id,
        topic_id=topic_id,
        kind=kind,
        provenance=provenance,
        limit=limit,
        offset=offset,
    )
    items = [
        EvidenceReel(
            event_id=i.event_id,
            session_id=i.session_id,
            reel_shortcode=i.reel_shortcode,
            reel_url=f"https://www.instagram.com/reel/{i.reel_shortcode}/",
            caption=i.caption,
            caption_truncated=i.caption_truncated,
            observed_at=i.observed_at,
            dwell_ms=i.dwell_ms,
            video_duration_ms=i.video_duration_ms,
            topic_score=i.topic_score,
            p_text=i.p_text,
            p_visual=i.p_visual,
            gain=i.gain,
            recency=i.decay,
            contribution=i.contribution,
            share_of_topic=i.contribution / topic_weight,
        )
        for i in page.items
    ]
    end = offset + len(items)
    return TopicEvidence(
        topic=EvidenceTopic(id=topic.id, slug=topic.slug, label=topic.label),
        kind=kind,
        evidence_available=True,
        reason=None,
        fingerprint=fingerprint,
        explanation=EvidenceExplanation(
            gain_rule=GAIN_RULES[kind],
            formula=FORMULA,
            contributing_reels=page.total,
            contributions_sum=page.contributions_sum,
            coverage=page.contributions_sum / topic_weight,
        ),
        total=page.total,
        limit=limit,
        offset=offset,
        next_offset=end if end < page.total else None,
        items=items,
    )
