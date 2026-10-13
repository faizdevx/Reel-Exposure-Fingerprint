"""Authentication dependencies. The only place user_id enters a request (spec §7, N6).

Every endpoint that touches user data takes a Principal from one of these dependencies and scopes
its queries by principal.user_id. A user_id in a request body or path is never an identity.
"""

import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.db import get_session
from app.models import ExtensionToken, UserSession
from app.ratelimit import charge_user
from app.security import sha256_digest

SESSION_COOKIE = "ref_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "ref-dashboard"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_bearer = HTTPBearer(auto_error=False, description="Extension token")


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    via: Literal["cookie", "token"]


def _not_authenticated(*, bearer: bool = False) -> HTTPException:
    headers = {"WWW-Authenticate": "Bearer"} if bearer else None
    return HTTPException(status_code=401, detail="Not authenticated", headers=headers)


def require_csrf(request: Request) -> None:
    """Cookie-authenticated state changes must carry the custom header.

    A cross-site form or fetch cannot set it without a CORS preflight, which this API never
    allows. SameSite=Lax alone does not cover every case (CONTRIBUTING.md, ADR-004).
    """
    if request.method not in _SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=403, detail=f"Missing {CSRF_HEADER} header")


def _user_for_cookie(request: Request, db: DbSession) -> uuid.UUID:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise _not_authenticated()
    user_id = db.execute(
        select(UserSession.user_id).where(
            UserSession.id_sha256 == sha256_digest(raw),
            UserSession.expires_at > func.now(),
        )
    ).scalar_one_or_none()
    if user_id is None:
        raise _not_authenticated()
    return user_id


def _user_for_token(creds: HTTPAuthorizationCredentials | None, db: DbSession) -> uuid.UUID:
    if creds is None or not creds.credentials:
        raise _not_authenticated(bearer=True)
    user_id = db.execute(
        select(ExtensionToken.user_id).where(
            ExtensionToken.token_sha256 == sha256_digest(creds.credentials),
            ExtensionToken.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    if user_id is None:
        raise _not_authenticated(bearer=True)
    return user_id


def cookie_principal(request: Request, db: Annotated[DbSession, Depends(get_session)]) -> Principal:
    """Dashboard identity from the session cookie. Ignores any Authorization header."""
    user_id = _user_for_cookie(request, db)
    require_csrf(request)
    charge_user(request, user_id, "cookie")
    return Principal(user_id=user_id, via="cookie")


def token_principal(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[DbSession, Depends(get_session)],
) -> Principal:
    """Extension identity from a Bearer token. Revoked tokens fail like unknown ones."""
    user_id = _user_for_token(creds, db)
    charge_user(request, user_id, "token")
    return Principal(user_id=user_id, via="token")


def any_principal(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[DbSession, Depends(get_session)],
) -> Principal:
    """Either identity. A presented Bearer token is final: a bad one never uses the cookie."""
    if creds is not None:
        token_user = _user_for_token(creds, db)
        charge_user(request, token_user, "token")
        return Principal(user_id=token_user, via="token")
    user_id = _user_for_cookie(request, db)
    require_csrf(request)
    charge_user(request, user_id, "cookie")
    return Principal(user_id=user_id, via="cookie")
