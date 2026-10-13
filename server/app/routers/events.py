"""Event ingest: POST /api/v1/events (spec F8, §8 ingest rules, ADR-022 envelope).

A batch of at most 50 exposure events. Checks run in this order, each before any work the next one
needs:

1. 401 without a valid extension token.
2. 403 `consent_required` without active consent.
3. 413 when the body is over 256 KB. The body is read as a stream and abandoned at the limit, so
   an oversized request costs at most 256 KB of memory.
4. 422 when the envelope or any event is malformed. The error lists locations and error types
   only, never the submitted values (captions must not echo into responses or logs).
5. Per event, in one transaction, a result of `stored`, `duplicate` or `rejected` with a reason:
   - `session_not_found`: no session with that id belongs to the caller (another user's session
     looks exactly like a missing one);
   - `session_inactive`: the session has been stopped (by the user, an auto-stop rule, the time
     cap, a newer session or consent revocation);
   - `observed_at_out_of_window`: more than 5 minutes outside [started_at, end], where end is the
     earlier of now and the 60 minute cap;
   - `event_id_conflict`: the event_id already belongs to another user's event.

Idempotency: event_id is the primary key and rows are inserted with ON CONFLICT DO NOTHING. A
replayed event changes nothing and is reported as `duplicate`, so a client may resend a batch any
number of times. A processing job is enqueued in the same transaction, only for a newly stored
event, so a replay never adds a job either.

Races: the consent row and the sessions are read FOR SHARE. A concurrent revocation or stop waits
for this transaction (or this one waits for it and then sees the new state), so no event is stored
after the revocation or stop that should have refused it committed.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import (
    UUID4,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession

from app.auth_deps import Principal
from app.consent import CONSENT_REQUIRED, require_active_consent
from app.db import get_session
from app.models import Consent, Job, ReelExposure
from app.models import Session as MonitoringSession
from app.session_rules import cap_time

log = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

MAX_BODY_BYTES = 256 * 1024  # spec §8: body over 256 KB is rejected
# The middleware's ceiling for this route. The exact 256 KB is enforced after authentication (the
# documented order is 401, 403, then 413); this only stops a gross body whatever the credentials.
HARD_BODY_CEILING = 4 * MAX_BODY_BYTES
MAX_BATCH = 50  # spec §8: batch of at most 50 events
WINDOW_SLACK = timedelta(minutes=5)  # spec §8: observed_at within the session window +- 5 min
MAX_DURATION_MS = 24 * 60 * 60 * 1000  # same bound as the extension's body guard
MAX_FRAMES = 5  # spec F7
EXPOSURE_JOB = "exposure"

# ---------- request schema ----------


def _no_nul(value: str) -> str:
    # PostgreSQL text cannot hold NUL; refuse it here rather than fail at insert time.
    if "\x00" in value:
        raise ValueError("NUL character")
    return value


Shortcode = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Handle = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._]{1,30}$")]
Caption = Annotated[str, StringConstraints(max_length=10_000)]
Hashtag = Annotated[str, StringConstraints(min_length=1, max_length=150)]
AdapterVersion = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,64}$")]
FieldStatus = Literal["available", "unavailable"]


class _Strict(BaseModel):
    # strict: no "12" for 12 or "true" for true. extra=forbid: no user_id or anything else.
    model_config = ConfigDict(extra="forbid", strict=True)


def _check_field(value: object, status: str) -> None:
    """Spec §8: `value` is null exactly when the status is `unavailable`."""
    if (status == "available") != (value is not None):
        raise ValueError("value must be null exactly when status is unavailable")


class HandleField(_Strict):
    value: Handle | None
    status: FieldStatus

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_field(self.value, self.status)
        return self


class CaptionField(_Strict):
    value: Caption | None
    status: FieldStatus

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_field(self.value, self.status)
        if self.value is not None:
            _no_nul(self.value)
        return self


class HashtagsField(_Strict):
    value: Annotated[list[Hashtag], Field(max_length=200)] | None
    status: FieldStatus

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_field(self.value, self.status)
        for tag in self.value or []:
            _no_nul(tag)
        return self


class EventIn(_Strict):
    """Spec §8 event payload plus the two ADR-022 fields (duration status, caption_truncated)."""

    event_id: UUID4
    session_id: uuid.UUID
    reel_shortcode: Shortcode
    observed_at: AwareDatetime
    dwell_ms: int = Field(ge=0, le=MAX_DURATION_MS)
    video_duration_ms: int | None = Field(ge=0, le=MAX_DURATION_MS)
    video_duration_status: FieldStatus
    creator_handle: HandleField
    caption: CaptionField
    caption_truncated: bool
    hashtags: HashtagsField
    frame_count: int = Field(ge=0, le=MAX_FRAMES)
    adapter_version: AdapterVersion

    @model_validator(mode="after")
    def _duration_consistent(self) -> Self:
        _check_field(self.video_duration_ms, self.video_duration_status)
        return self


class EventBatch(_Strict):
    events: list[EventIn] = Field(min_length=1, max_length=MAX_BATCH)


# ---------- response schema ----------

RejectReason = Literal[
    "session_not_found", "session_inactive", "observed_at_out_of_window", "event_id_conflict"
]


class EventResult(BaseModel):
    event_id: uuid.UUID
    status: Literal["stored", "duplicate", "rejected"]
    reason: RejectReason | None = None


class IngestResponse(BaseModel):
    """One result per submitted event, in request order."""

    results: list[EventResult]


# ---------- body handling ----------


async def limited_body(request: Request) -> bytes:
    """The raw body, refused with 413 as soon as it is known to exceed MAX_BODY_BYTES."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="payload_too_large")
        except ValueError:
            raise HTTPException(status_code=400, detail="bad_content_length") from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="payload_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_batch(raw: bytes) -> EventBatch:
    try:
        return EventBatch.model_validate_json(raw)
    except ValidationError as error:
        # Locations and error types only: never echo submitted values (captions, handles).
        detail = [
            {"loc": list(e["loc"]), "type": e["type"]}
            for e in error.errors(include_url=False, include_input=False, include_context=False)
        ]
        raise HTTPException(status_code=422, detail=detail) from None


# ---------- rules ----------


def _window(session: MonitoringSession, now: datetime) -> tuple[datetime, datetime]:
    end = min(now, cap_time(session))
    return session.started_at - WINDOW_SLACK, end + WINDOW_SLACK


def _row(event: EventIn, user_id: uuid.UUID) -> dict[str, Any]:
    def value(field: HandleField | CaptionField | HashtagsField) -> Any:
        return field.value if field.status == "available" else None

    return {
        "event_id": event.event_id,
        "session_id": event.session_id,
        "user_id": user_id,
        "reel_shortcode": event.reel_shortcode,
        "observed_at": event.observed_at,
        "dwell_ms": event.dwell_ms,
        "video_duration_ms": event.video_duration_ms,
        "creator_handle": value(event.creator_handle),
        "caption": value(event.caption),
        "hashtags": value(event.hashtags),
        # Statuses of the captured fields, plus the two facts §9 has no column for.
        "field_status": {
            "reel_shortcode": "available",
            "observed_at": "available",
            "creator_handle": event.creator_handle.status,
            "caption": event.caption.status,
            "hashtags": event.hashtags.status,
            "video_duration_ms": event.video_duration_status,
            "caption_truncated": event.caption_truncated,
            "frame_count": event.frame_count,
        },
        "adapter_version": event.adapter_version,
    }


def _lock_consent(db: DbSession, user_id: uuid.UUID) -> None:
    revoked = db.execute(
        select(Consent.revoked_at).where(Consent.user_id == user_id).with_for_update(read=True)
    ).first()
    if revoked is None or revoked[0] is not None:
        raise HTTPException(status_code=403, detail=CONSENT_REQUIRED)


def _lock_sessions(
    db: DbSession, user_id: uuid.UUID, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, MonitoringSession]:
    rows = db.execute(
        select(MonitoringSession)
        .where(MonitoringSession.id.in_(set(ids)), MonitoringSession.user_id == user_id)
        .order_by(MonitoringSession.id)
        .with_for_update(read=True)
    ).scalars()
    return {row.id: row for row in rows}


def _store(db: DbSession, event: EventIn, user_id: uuid.UUID) -> EventResult:
    inserted = db.execute(
        pg_insert(ReelExposure)
        .values(**_row(event, user_id))
        .on_conflict_do_nothing(index_elements=[ReelExposure.event_id])
        .returning(ReelExposure.event_id)
    ).first()
    if inserted is not None:
        db.add(Job(kind=EXPOSURE_JOB, payload={"event_id": str(event.event_id)}))
        return EventResult(event_id=event.event_id, status="stored")
    owner = db.execute(
        select(ReelExposure.user_id).where(ReelExposure.event_id == event.event_id)
    ).scalar_one()
    if owner != user_id:
        return EventResult(event_id=event.event_id, status="rejected", reason="event_id_conflict")
    return EventResult(event_id=event.event_id, status="duplicate")


def ingest(db: DbSession, user_id: uuid.UUID, batch: EventBatch, now: datetime) -> IngestResponse:
    _lock_consent(db, user_id)
    sessions = _lock_sessions(db, user_id, [e.session_id for e in batch.events])
    results: list[EventResult] = []
    for event in batch.events:
        session = sessions.get(event.session_id)
        reason: RejectReason | None = None
        if session is None:
            reason = "session_not_found"
        elif session.status != "active":
            reason = "session_inactive"
        else:
            low, high = _window(session, now)
            if not low <= event.observed_at <= high:
                reason = "observed_at_out_of_window"
        if reason is not None:
            results.append(EventResult(event_id=event.event_id, status="rejected", reason=reason))
        else:
            results.append(_store(db, event, user_id))
        db.flush()  # the job row of this event is written before the next event is looked at
    db.commit()
    return IngestResponse(results=results)


@router.post("", response_model=IngestResponse)
def ingest_events(
    principal: Annotated[Principal, Depends(require_active_consent)],
    raw: Annotated[bytes, Depends(limited_body)],
    db: Annotated[DbSession, Depends(get_session)],
) -> IngestResponse:
    """Store a batch of exposure events. Safe to retry: see the module docstring."""
    batch = parse_batch(raw)
    response = ingest(db, principal.user_id, batch, datetime.now(UTC))
    counts = {s: sum(r.status == s for r in response.results) for s in ("stored", "duplicate")}
    log.info(
        "ingest user=%s events=%d stored=%d duplicate=%d rejected=%d",
        principal.user_id,
        len(response.results),
        counts["stored"],
        counts["duplicate"],
        len(response.results) - counts["stored"] - counts["duplicate"],
    )
    return response
