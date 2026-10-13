"""The caller's own data: DELETE /api/v1/me/data (spec F13, N7, `app.deletion`)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session as DbSession

from app.auth_deps import SESSION_COOKIE, Principal, cookie_principal
from app.config import Settings, get_settings
from app.db import get_session
from app.deletion import DeletionIncomplete, delete_all_data
from app.frames import FrameStore
from app.routers.frames import get_frame_store

router = APIRouter(prefix="/me", tags=["me"])


@router.delete("/data")
def delete_my_data(
    request: Request,
    principal: Annotated[Principal, Depends(cookie_principal)],
    db: Annotated[DbSession, Depends(get_session)],
    store: Annotated[FrameStore, Depends(get_frame_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    """Delete everything of the caller, the account included, then verify.

    Irreversible. 200 with a report only when the verification, run after the rows and the files
    are gone, finds zero rows in every table and zero files; the login cookie is then cleared.
    500 with the counts of what is left when it does not: the account is still there, and calling
    again finishes the deletion. After a 200 the login no longer exists, so a repeated call is a
    401 ("already gone"), never a second deletion of anything.
    """
    try:
        report = delete_all_data(db, store, principal.user_id)
    except DeletionIncomplete as incomplete:
        return JSONResponse(
            status_code=500,
            content={"detail": "deletion_incomplete", **incomplete.report.as_dict()},
        )
    response = JSONResponse(report.as_dict())
    if SESSION_COOKIE in request.cookies:
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
        )
    return response
