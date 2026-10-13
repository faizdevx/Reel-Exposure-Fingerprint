"""Consent: grant (extension) and revoke (extension or dashboard). Spec F1, §8."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, StringConstraints
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.auth_deps import Principal, any_principal, token_principal
from app.db import get_session
from app.models import Consent
from app.schemas import StrictModel
from app.session_rules import active_sessions, stop

router = APIRouter(prefix="/consent", tags=["consent"])

# Versions are chosen by whoever ships the consent text (the extension). Format-checked, stored.
PolicyVersion = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]+$"
    ),
]


class ConsentRequest(StrictModel):
    # No granted_at here: the grant time is the server's clock, never the client's.
    policy_version: PolicyVersion


class ConsentOut(BaseModel):
    policy_version: str
    granted_at: datetime
    revoked_at: datetime | None


@router.put("", response_model=ConsentOut)
def grant_consent(
    body: ConsentRequest,
    principal: Annotated[Principal, Depends(token_principal)],
    db: Annotated[DbSession, Depends(get_session)],
) -> Consent:
    """Record consent under a policy version. Repeating the same grant changes nothing."""
    now = datetime.now(UTC)
    row = db.get(Consent, principal.user_id)
    if row is None:
        row = Consent(user_id=principal.user_id, policy_version=body.policy_version, granted_at=now)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:  # a concurrent first grant won the insert
            db.rollback()
            row = db.get(Consent, principal.user_id)
            if row is None:  # cannot happen: the conflicting row exists
                raise HTTPException(status_code=409, detail="Consent changed, retry") from None
    elif row.revoked_at is not None or row.policy_version != body.policy_version:
        # Re-grant after revocation, or consent to a newer policy: a fresh grant.
        row.policy_version = body.policy_version
        row.granted_at = now
        row.revoked_at = None
        db.commit()
    return row


@router.delete("", status_code=204)
def revoke_consent(
    principal: Annotated[Principal, Depends(any_principal)],
    db: Annotated[DbSession, Depends(get_session)],
) -> Response:
    """Revoke consent and stop every active session of this user, in one transaction."""
    now = datetime.now(UTC)
    row = db.get(Consent, principal.user_id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = now
    for session in active_sessions(db, principal.user_id):
        stop(session, now=now, reason="consent_revoked")
    db.commit()
    return Response(status_code=204)
