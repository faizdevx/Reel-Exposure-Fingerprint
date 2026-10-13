"""Evidence for one topic of a fingerprint: which Reels put it there, and how much each did.

A fingerprint weight is the sum of what every Reel added, each added amount shrinking with age:

    W_k(now) = sum over Reels i of   g_i * p_i(k) * 2^(-(t_last - t_i) / h)

with `p_i(k)` the Reel's fused score for topic k, `g_i` its gain (exposure: 1; engagement: the
dwell rule), `t_i` when it was observed, `t_last` the time of the last Reel the fingerprint applied,
and `h` the half-life. That is the update rule of `engine.apply_observation` unrolled, so the
contributions below add up to the stored weight, and a Reel's share of the topic is exactly its
contribution divided by that weight. A test checks the sum against the stored snapshot.

The snapshot says how it was built (`provenance`: the assignment version it read and the
parameters), so the evidence uses the same rows and numbers, not today's settings.

The formula is evaluated in SQL so that ordering by contribution and paging are exact over all of
a user's Reels without loading them. It mirrors `engagement_gain` and `decay_factor` in `engine.py`;
`python_contribution` is that same formula in Python, and the tests hold the two together.

Every query starts from the caller's own exposures (`user_id`); assignments are shared by Reel and
are reached only through them.
"""

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    DateTime,
    Float,
    and_,
    case,
    cast,
    func,
    literal,
    select,
)
from sqlalchemy.orm import Session as DbSession

from app.fingerprint.engine import FingerprintConfig, FingerprintKind, engagement_gain
from app.models import ReelExposure, TopicAssignment

EVIDENCE_DEFAULT_LIMIT = 20
EVIDENCE_MAX_LIMIT = 100
CAPTION_MAX_CHARS = 300


@dataclass(frozen=True)
class Provenance:
    assignment_version: str
    config: FingerprintConfig
    last_observed_at: datetime


def provenance_of(document: dict[str, Any]) -> Provenance | None:
    """What the snapshot was built from, or None if it predates this record (or is empty)."""
    raw = document.get("provenance")
    last = document.get("last_observed_at")
    if not isinstance(raw, dict) or not isinstance(last, str):
        return None
    try:
        config = FingerprintConfig(**raw["params"])
        return Provenance(str(raw["assignment_version"]), config, datetime.fromisoformat(last))
    except (KeyError, TypeError, ValueError):
        return None


def gain_expression(kind: FingerprintKind, config: FingerprintConfig) -> ColumnElement[float]:
    """SQL twin of `exposure_gain` / `engagement_gain`. NULL when the gain is undefined."""
    if kind == "exposure":
        return literal(1.0, Float)
    dwell = ReelExposure.dwell_ms
    duration = func.greatest(
        func.coalesce(ReelExposure.video_duration_ms, 0), config.min_duration_ms
    )
    ratio = cast(dwell, Float) / duration
    return case(
        (dwell.is_(None), None),
        (dwell < config.skip_dwell_ms, config.skip_gain),
        else_=func.least(config.max_gain, func.greatest(config.min_gain, ratio)),
    )


def decay_expression(last_observed_at: datetime, config: FingerprintConfig) -> ColumnElement[float]:
    """SQL twin of `decay_factor`, from the Reel's `observed_at` to the fingerprint's last Reel."""
    seconds = cast(
        func.extract(
            "epoch", literal(last_observed_at, DateTime(timezone=True)) - ReelExposure.observed_at
        ),
        Float,
    )
    return func.power(2.0, -(seconds / 86400.0) / config.half_life_days)


def python_contribution(
    kind: FingerprintKind,
    config: FingerprintConfig,
    *,
    dwell_ms: int | None,
    video_duration_ms: int | None,
    probability: float,
    seconds_before_last: float,
) -> tuple[float | None, float, float | None]:
    """`(gain, decay, contribution)` by the engine's own functions; contribution None if no gain."""
    gain = 1.0 if kind == "exposure" else engagement_gain(dwell_ms, video_duration_ms, config)
    decay = math.pow(2.0, -(seconds_before_last / 86400.0) / config.half_life_days)
    return gain, decay, None if gain is None else gain * probability * decay


def _filters(
    user_id: uuid.UUID, topic_id: int, provenance: Provenance, gain: ColumnElement[float]
) -> Any:
    return and_(
        ReelExposure.user_id == user_id,
        ReelExposure.observed_at <= provenance.last_observed_at,
        TopicAssignment.p_fused > 0,
        gain.is_not(None),
    )


def _from(topic_id: int, provenance: Provenance) -> Any:
    return ReelExposure.__table__.join(
        TopicAssignment.__table__,
        and_(
            TopicAssignment.user_id == ReelExposure.user_id,
            TopicAssignment.reel_shortcode == ReelExposure.reel_shortcode,
            TopicAssignment.pipeline_version == provenance.assignment_version,
            TopicAssignment.topic_id == topic_id,
        ),
    )


@dataclass(frozen=True)
class EvidenceItem:
    event_id: uuid.UUID
    session_id: uuid.UUID
    reel_shortcode: str
    caption: str | None
    caption_truncated: bool
    observed_at: datetime
    dwell_ms: int | None
    video_duration_ms: int | None
    topic_score: float
    p_text: float | None
    p_visual: float | None
    gain: float
    decay: float
    contribution: float


@dataclass(frozen=True)
class EvidencePage:
    total: int
    contributions_sum: float
    items: list[EvidenceItem]


def evidence_page(
    db: DbSession,
    *,
    user_id: uuid.UUID,
    topic_id: int,
    kind: FingerprintKind,
    provenance: Provenance,
    limit: int,
    offset: int,
) -> EvidencePage:
    config = provenance.config
    gain = gain_expression(kind, config).label("gain")
    decay = decay_expression(provenance.last_observed_at, config).label("decay")
    contribution = (gain * TopicAssignment.p_fused * decay).label("contribution")
    source = _from(topic_id, provenance)
    where = _filters(user_id, topic_id, provenance, gain_expression(kind, config))

    totals = db.execute(
        select(func.count(), func.coalesce(func.sum(contribution), 0.0))
        .select_from(source)
        .where(where)
    ).one()
    query = (
        select(
            ReelExposure.event_id,
            ReelExposure.session_id,
            ReelExposure.reel_shortcode,
            ReelExposure.caption,
            ReelExposure.observed_at,
            ReelExposure.dwell_ms,
            ReelExposure.video_duration_ms,
            TopicAssignment.p_fused,
            TopicAssignment.p_text,
            TopicAssignment.p_visual,
            gain,
            decay,
            contribution,
        )
        .select_from(source)
        .where(where)
        .order_by(contribution.desc(), ReelExposure.event_id)
        .limit(limit)
        .offset(offset)
    )
    items: list[EvidenceItem] = []
    for row in db.execute(query):
        caption = row.caption
        truncated = caption is not None and len(caption) > CAPTION_MAX_CHARS
        items.append(
            EvidenceItem(
                event_id=row.event_id,
                session_id=row.session_id,
                reel_shortcode=row.reel_shortcode,
                caption=caption[:CAPTION_MAX_CHARS] if caption is not None else None,
                caption_truncated=truncated,
                observed_at=row.observed_at,
                dwell_ms=row.dwell_ms,
                video_duration_ms=row.video_duration_ms,
                topic_score=float(row.p_fused),
                p_text=None if row.p_text is None else float(row.p_text),
                p_visual=None if row.p_visual is None else float(row.p_visual),
                gain=float(row.gain),
                decay=float(row.decay),
                contribution=float(row.contribution),
            )
        )
    return EvidencePage(int(totals[0]), float(totals[1]), items)
