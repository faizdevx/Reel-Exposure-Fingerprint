"""Dashboard authentication: register (invite-only), login, logout. Spec §7, ADR-004."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.auth_deps import SESSION_COOKIE, Principal, cookie_principal, require_csrf
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Invite, User, UserSession
from app.ratelimit import (
    charge_auth_attempt,
    login_failed,
    login_locked,
    login_succeeded,
    password_work,
)
from app.schemas import StrictModel
from app.security import (
    hash_password,
    new_secret,
    password_needs_rehash,
    sha256_digest,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        max_length=254,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    ),
]


class LoginRequest(StrictModel):
    email: Email
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(StrictModel):
    email: Email
    password: str = Field(min_length=12, max_length=256)
    invite_code: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: uuid.UUID
    email: str


@router.post(
    "/register",
    status_code=201,
    response_model=UserOut,
    dependencies=[Depends(require_csrf)],
)
def register(
    body: RegisterRequest, request: Request, db: Annotated[DbSession, Depends(get_session)]
) -> UserOut:
    charge_auth_attempt(request)
    code_hash = sha256_digest(body.invite_code)
    # Check the invite before touching users, so a missing invite cannot probe which emails exist.
    usable = db.execute(
        select(Invite.code_sha256).where(
            Invite.code_sha256 == code_hash,
            Invite.used_at.is_(None),
            Invite.expires_at > func.now(),
        )
    ).first()
    if usable is None:
        raise HTTPException(status_code=403, detail="Invalid or expired invite")

    with password_work(request):
        password_hash = hash_password(body.password)
    user = User(id=uuid.uuid4(), email=body.email, password_hash=password_hash)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()  # the invite stays unused
        raise HTTPException(status_code=409, detail="Email already registered") from None

    # Consume atomically: a concurrent registration with the same code gets no row back.
    consumed = db.execute(
        update(Invite)
        .where(
            Invite.code_sha256 == code_hash,
            Invite.used_at.is_(None),
            Invite.expires_at > func.now(),
        )
        .values(used_at=func.now(), used_by=user.id)
        .returning(Invite.code_sha256)
    ).first()
    if consumed is None:
        db.rollback()
        raise HTTPException(status_code=403, detail="Invalid or expired invite")
    db.commit()
    return UserOut(id=user.id, email=user.email)


@router.post("/login", response_model=UserOut, dependencies=[Depends(require_csrf)])
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[DbSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UserOut:
    charge_auth_attempt(request)
    login_locked(request, body.email)  # before any password work: a locked email costs nothing
    user = db.execute(select(User).where(User.email == body.email)).scalar_one_or_none()
    # Always verify, even for an unknown email, so timing and message do not reveal accounts.
    with password_work(request):
        verified = verify_password(user.password_hash if user else None, body.password)
    if not verified or user is None:
        login_failed(request, body.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    login_succeeded(request, body.email)

    if password_needs_rehash(user.password_hash):
        with password_work(request):
            user.password_hash = hash_password(body.password)

    session_id = new_secret()
    expires = datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)
    db.execute(
        delete(UserSession).where(
            UserSession.user_id == user.id, UserSession.expires_at <= func.now()
        )
    )
    db.add(UserSession(id_sha256=sha256_digest(session_id), user_id=user.id, expires_at=expires))
    db.commit()

    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
    )
    return UserOut(id=user.id, email=user.email)


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    principal: Annotated[Principal, Depends(cookie_principal)],
    db: Annotated[DbSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    # cookie_principal already proved the cookie is a live session for principal.user_id.
    raw = request.cookies[SESSION_COOKIE]
    db.execute(
        delete(UserSession).where(
            UserSession.id_sha256 == sha256_digest(raw), UserSession.user_id == principal.user_id
        )
    )
    db.commit()
    out = Response(status_code=204)
    out.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
    )
    return out
