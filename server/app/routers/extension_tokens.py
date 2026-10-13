"""Extension tokens: issued and revoked from the dashboard (cookie session). Spec §7."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.auth_deps import Principal, cookie_principal
from app.db import get_session
from app.models import ExtensionToken
from app.security import new_secret, sha256_digest

router = APIRouter(prefix="/extension-tokens", tags=["extension-tokens"])


class CreateTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=100)


class TokenCreated(BaseModel):
    id: uuid.UUID
    label: str | None
    created_at: datetime
    # The only time the token is ever returned. The server keeps just its SHA-256.
    token: str


@router.post("", status_code=201, response_model=TokenCreated)
def create_token(
    principal: Annotated[Principal, Depends(cookie_principal)],
    db: Annotated[DbSession, Depends(get_session)],
    response: Response,
    body: Annotated[CreateTokenRequest | None, Body()] = None,
) -> TokenCreated:
    token = new_secret()
    row = ExtensionToken(
        id=uuid.uuid4(),
        user_id=principal.user_id,  # from the session, never from the body
        token_sha256=sha256_digest(token),
        label=body.label if body else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    response.headers["Cache-Control"] = "no-store"
    return TokenCreated(id=row.id, label=row.label, created_at=row.created_at, token=token)


@router.delete("/{token_id}", status_code=204)
def revoke_token(
    token_id: uuid.UUID,
    principal: Annotated[Principal, Depends(cookie_principal)],
    db: Annotated[DbSession, Depends(get_session)],
) -> Response:
    # Ownership is part of the query: another user's token is indistinguishable from a missing one.
    row = db.execute(
        select(ExtensionToken).where(
            ExtensionToken.id == token_id, ExtensionToken.user_id == principal.user_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Token not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        db.commit()
    return Response(status_code=204)
