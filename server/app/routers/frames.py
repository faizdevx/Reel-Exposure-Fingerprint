"""Frame upload: POST /api/v1/events/{event_id}/frames (spec F7, F15, §8).

Multipart form, one JPEG per file part, at most 5 parts and 150 KB each, body at most 800 KB
(`BodyLimitMiddleware`, before parsing). Checks run in this order, each before any work the next
one needs:

1. 401 without a valid extension token.
2. 403 `consent_required` without active consent.
3. 413 `payload_too_large` when the body is over the cap (middleware, before this code runs).
4. 404 `event_not_found` when the event does not exist or belongs to someone else (the two look
   the same), 409 `session_revoked` when its session was closed by a consent revocation,
   409 `frames_expired` when the event is older than the 24 h frame retention.
5. The form: 400 `malformed_multipart`; 422 `no_frames`, `too_many_frames` (more than 5 parts) or
   `unexpected_field` (a part that is not a file); 413 `frame_too_large` (over 150 KB);
   415 `unsupported_media_type` (part type is not `image/jpeg`, or the bytes are not a JPEG).
6. Under a lock on the consent row and the exposure row: 409 `frame_limit_exceeded` when the event
   would hold more distinct frames than it declared in `frame_count` (never more than 5).

A frame is stored under its SHA-256, so a retry of the same upload stores nothing twice and the
response marks it `duplicate`. Sessions that are already stopped still accept frames: the extension
sends an event first and its frames afterwards, possibly after Stop. Frames never go into the
database; only the local volume holds them (`app.frames`).

Responses and logs carry ids, counts and hashes only.
"""

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth_deps import Principal
from app.body_limit import BodyTooLarge
from app.config import Settings, get_settings
from app.consent import CONSENT_REQUIRED, require_active_consent
from app.db import get_session
from app.frames import (
    MAX_FRAME_BYTES,
    MAX_FRAMES_PER_EVENT,
    FrameFormatError,
    FrameStore,
    check_jpeg,
)
from app.models import Consent, ReelExposure
from app.models import Session as MonitoringSession

log = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["frames"])

JPEG = "image/jpeg"
_FORM_PART_LIMIT = MAX_FRAMES_PER_EVENT + 1  # one more than allowed, so "too many" is detectable


class FrameResult(BaseModel):
    sha256: str
    bytes: int
    status: Literal["stored", "duplicate"]


class FramesResponse(BaseModel):
    event_id: uuid.UUID
    frames: list[FrameResult]
    #: Distinct frames now held for this event.
    total_frames: int


def get_frame_store(settings: Annotated[Settings, Depends(get_settings)]) -> FrameStore:
    return FrameStore(settings.frame_store_dir)


def _check_event(
    db: DbSession, user_id: uuid.UUID, event_id: uuid.UUID, ttl: timedelta, *, lock: bool
) -> ReelExposure:
    query = select(ReelExposure).where(
        ReelExposure.event_id == event_id, ReelExposure.user_id == user_id
    )
    if lock:
        query = query.with_for_update()
    event = db.execute(query).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="event_not_found")
    session = db.get(MonitoringSession, event.session_id)
    if session is not None and session.stop_reason == "consent_revoked":
        raise HTTPException(status_code=409, detail="session_revoked")
    if event.observed_at < datetime.now(UTC) - ttl:
        raise HTTPException(status_code=409, detail="frames_expired")
    return event


def _precheck(db: DbSession, user_id: uuid.UUID, event_id: uuid.UUID, ttl: timedelta) -> None:
    _check_event(db, user_id, event_id, ttl, lock=False)


def _declared_limit(event: ReelExposure) -> int:
    declared = (event.field_status or {}).get("frame_count", MAX_FRAMES_PER_EVENT)
    if not isinstance(declared, int) or isinstance(declared, bool):
        return 0
    return max(0, min(declared, MAX_FRAMES_PER_EVENT))


def _store(
    db: DbSession,
    store: FrameStore,
    user_id: uuid.UUID,
    event_id: uuid.UUID,
    frames: list[bytes],
    ttl: timedelta,
) -> FramesResponse:
    # Same lock order as event ingest: consent row, then the rows that depend on it. A concurrent
    # revocation waits for this transaction, or this one waits and then sees it.
    revoked = db.execute(
        select(Consent.revoked_at).where(Consent.user_id == user_id).with_for_update(read=True)
    ).first()
    if revoked is None or revoked[0] is not None:
        raise HTTPException(status_code=403, detail=CONSENT_REQUIRED)
    event = _check_event(db, user_id, event_id, ttl, lock=True)

    existing = store.list_event(user_id, event_id)

    incoming = {hashlib.sha256(data).hexdigest(): data for data in frames}
    if len(existing | incoming.keys()) > _declared_limit(event):
        raise HTTPException(status_code=409, detail="frame_limit_exceeded")

    results: list[FrameResult] = []
    for data in frames:
        digest, created = store.save(user_id, event_id, data)
        results.append(
            FrameResult(sha256=digest, bytes=len(data), status="stored" if created else "duplicate")
        )
    total = len(store.list_event(user_id, event_id))
    db.commit()  # releases the locks
    log.info(
        "frames event=%s received=%d stored=%d total=%d",
        event_id,
        len(frames),
        sum(r.status == "stored" for r in results),
        total,
    )
    return FramesResponse(event_id=event_id, frames=results, total_frames=total)


async def read_frames(request: Request) -> list[bytes]:
    """Parse and validate the multipart body. Raises HTTPException with a code, never content."""
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(status_code=415, detail="unsupported_media_type")
    try:
        form = await request.form(max_files=_FORM_PART_LIMIT, max_fields=_FORM_PART_LIMIT)
    except StarletteHTTPException as error:
        # Starlette refuses a form with more parts than the limits above.
        if error.status_code == 400 and "Too many" in str(error.detail):
            raise HTTPException(status_code=422, detail="too_many_frames") from None
        raise HTTPException(status_code=400, detail="malformed_multipart") from None
    except BodyTooLarge:
        raise  # the middleware answers 413
    except Exception:
        raise HTTPException(status_code=400, detail="malformed_multipart") from None

    parts = list(form.multi_items())
    try:
        if not parts:
            raise HTTPException(status_code=422, detail="no_frames")
        if len(parts) > MAX_FRAMES_PER_EVENT:
            raise HTTPException(status_code=422, detail="too_many_frames")
        frames: list[bytes] = []
        for _name, value in parts:
            if not isinstance(value, UploadFile):
                raise HTTPException(status_code=422, detail="unexpected_field")
            declared_type = (value.content_type or "").split(";")[0].strip().lower()
            if declared_type != JPEG:
                raise HTTPException(status_code=415, detail="unsupported_media_type")
            data = await value.read(MAX_FRAME_BYTES + 1)
            if len(data) > MAX_FRAME_BYTES:
                raise HTTPException(status_code=413, detail="frame_too_large")
            try:
                check_jpeg(data)
            except FrameFormatError:
                raise HTTPException(status_code=415, detail="unsupported_media_type") from None
            frames.append(data)
        return frames
    finally:
        await form.close()


@router.post("/{event_id}/frames", response_model=FramesResponse)
async def upload_frames(
    event_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_active_consent)],
    db: Annotated[DbSession, Depends(get_session)],
    store: Annotated[FrameStore, Depends(get_frame_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FramesResponse:
    """Store up to 5 JPEG frames for one of the caller's events. Safe to retry."""
    ttl = timedelta(hours=settings.frame_ttl_hours)
    await run_in_threadpool(_precheck, db, principal.user_id, event_id, ttl)
    frames = await read_frames(request)
    return await run_in_threadpool(_store, db, store, principal.user_id, event_id, frames, ttl)
