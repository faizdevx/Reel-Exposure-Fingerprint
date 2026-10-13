"""Monitoring sessions: manual start and stop (spec F2, §8). Called by the extension."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.auth_deps import Principal, cookie_principal, token_principal
from app.consent import require_active_consent
from app.db import get_session
from app.deletion import DeletionIncomplete, delete_session
from app.frames import FrameStore
from app.models import FingerprintSnapshot
from app.models import Session as MonitoringSession
from app.routers.frames import get_frame_store
from app.schemas import StrictModel
from app.session_rules import close_previous, stop

router = APIRouter(prefix="/sessions", tags=["sessions"])


class SessionOut(BaseModel):
    id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None
    status: str
    stop_reason: str | None


class SessionSummary(SessionOut):
    #: Reels counted in this session (exposure fingerprint), or None until it is processed.
    n_reels: int | None


class StopRequest(StrictModel):
    status: Literal["stopped"]
    # consent_revoked is not accepted here: only the server sets it (DELETE /consent).
    stop_reason: Literal["user", "left_surface", "tab_closed", "time_cap", "browser_restart"]


@router.post("", status_code=201, response_model=SessionOut)
def start_session(
    principal: Annotated[Principal, Depends(require_active_consent)],
    db: Annotated[DbSession, Depends(get_session)],
) -> MonitoringSession:
    now = datetime.now(UTC)
    close_previous(db, principal.user_id, now=now)
    db.flush()  # the old session must be stopped before the new one is inserted
    session = MonitoringSession(
        id=uuid.uuid4(), user_id=principal.user_id, started_at=now, status="active"
    )
    db.add(session)
    try:
        db.commit()
    except IntegrityError:  # two starts raced: the one-active-session index refused the second
        db.rollback()
        raise HTTPException(status_code=409, detail="A session is already active") from None
    return session


@router.patch("/{session_id}", response_model=SessionOut)
def stop_session(
    session_id: uuid.UUID,
    body: StopRequest,
    principal: Annotated[Principal, Depends(token_principal)],
    db: Annotated[DbSession, Depends(get_session)],
) -> MonitoringSession:
    """Stop a session. Allowed without consent (stopping is always safe) and idempotent."""
    session = db.execute(
        select(MonitoringSession).where(
            MonitoringSession.id == session_id, MonitoringSession.user_id == principal.user_id
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    stop(session, now=datetime.now(UTC), reason=body.stop_reason)  # no-op if already stopped
    db.commit()
    return session


_LIST_LIMIT = 200


@router.get("", response_model=list[SessionSummary])
def list_sessions(
    principal: Annotated[Principal, Depends(cookie_principal)],
    db: Annotated[DbSession, Depends(get_session)],
) -> list[SessionSummary]:
    """The dashboard's session list, newest first. Reel counts come from precomputed snapshots."""
    sessions = list(
        db.execute(
            select(MonitoringSession)
            .where(MonitoringSession.user_id == principal.user_id)
            .order_by(MonitoringSession.started_at.desc(), MonitoringSession.id)
            .limit(_LIST_LIMIT)
        ).scalars()
    )
    counts: dict[uuid.UUID, int] = {}
    if sessions:
        rows = db.execute(
            select(FingerprintSnapshot.session_id, FingerprintSnapshot.weights)
            .where(
                FingerprintSnapshot.user_id == principal.user_id,
                FingerprintSnapshot.kind == "exposure",
                FingerprintSnapshot.bucket == "session",
                FingerprintSnapshot.session_id.in_([s.id for s in sessions]),
            )
            .order_by(FingerprintSnapshot.id)  # the newest snapshot of a session wins
        ).all()
        # Reels counted inside that session (the snapshot's own `n_reels` is a running total).
        counts = {
            session_id: int(doc["bucket_reels"])
            for session_id, doc in rows
            if session_id is not None and isinstance(doc.get("bucket_reels"), int)
        }
    return [
        SessionSummary(
            id=s.id,
            started_at=s.started_at,
            ended_at=s.ended_at,
            status=s.status,
            stop_reason=s.stop_reason,
            n_reels=counts.get(s.id),
        )
        for s in sessions
    ]


@router.delete("/{session_id}")
def delete_session_endpoint(
    session_id: uuid.UUID,
    principal: Annotated[Principal, Depends(cookie_principal)],
    db: Annotated[DbSession, Depends(get_session)],
    store: Annotated[FrameStore, Depends(get_frame_store)],
) -> JSONResponse:
    """Delete a session and everything derived from it, then verify (spec F13, `app.deletion`).

    200 with a report when the verification finds nothing left. Repeating the call is safe: a
    session that is gone, or that is not the caller's, is `existed: false` and nothing is touched
    (the two look the same, so the answer reveals nothing about other users). 500 with the counts
    of what is left when the verification fails; calling again finishes the job.
    """
    try:
        report = delete_session(db, store, principal.user_id, session_id)
    except DeletionIncomplete as incomplete:
        return JSONResponse(
            status_code=500,
            content={"detail": "deletion_incomplete", **incomplete.report.as_dict()},
        )
    return JSONResponse(report.as_dict())
