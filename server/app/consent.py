"""Consent checks. Nothing is collected or accepted without active consent (spec F1, N7)."""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.auth_deps import Principal, token_principal
from app.db import get_session
from app.models import Consent

CONSENT_REQUIRED = "consent_required"


def has_active_consent(db: DbSession, user_id: uuid.UUID) -> bool:
    revoked_at = db.execute(select(Consent.revoked_at).where(Consent.user_id == user_id)).first()
    # No row: never granted. A row with revoked_at set: revoked.
    return revoked_at is not None and revoked_at[0] is None


def require_active_consent(
    principal: Annotated[Principal, Depends(token_principal)],
    db: Annotated[DbSession, Depends(get_session)],
) -> Principal:
    """Extension endpoints that collect data depend on this, not on token_principal alone."""
    if not has_active_consent(db, principal.user_id):
        raise HTTPException(status_code=403, detail=CONSENT_REQUIRED)
    return principal
