"""Helpers shared by tests."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from alembic.config import Config
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth_deps import Principal, any_principal, cookie_principal, token_principal
from app.models import Invite
from app.security import new_secret, sha256_digest

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
CSRF = {"X-Requested-With": "ref-dashboard"}
PASSWORD = "correct horse battery staple"


def alembic_config(url: str) -> Config:
    cfg = Config(str(SERVER_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


# Test-only routes that exercise the real auth dependencies. The production API exposes only the
# endpoints in spec §8, so these live here and are mounted by the `client` fixture alone.
probe = APIRouter(prefix="/_probe")


@probe.get("/cookie")
def probe_cookie(p: Annotated[Principal, Depends(cookie_principal)]) -> dict[str, str]:
    return {"user_id": str(p.user_id), "via": p.via}


@probe.post("/cookie")
def probe_cookie_post(p: Annotated[Principal, Depends(cookie_principal)]) -> dict[str, str]:
    return {"user_id": str(p.user_id), "via": p.via}


@probe.get("/token")
def probe_token(p: Annotated[Principal, Depends(token_principal)]) -> dict[str, str]:
    return {"user_id": str(p.user_id), "via": p.via}


@probe.get("/any")
def probe_any(p: Annotated[Principal, Depends(any_principal)]) -> dict[str, str]:
    return {"user_id": str(p.user_id), "via": p.via}


@probe.post("/echo")
def probe_echo(
    body: dict[str, Any], p: Annotated[Principal, Depends(any_principal)]
) -> dict[str, Any]:
    """Takes whatever the client sends, including a user_id, and answers with the real identity."""
    return {"user_id": str(p.user_id), "ignored_body_keys": sorted(body)}


def make_invite(db: Session, *, expires_in: timedelta = timedelta(hours=1)) -> str:
    code = new_secret()
    db.add(Invite(code_sha256=sha256_digest(code), expires_at=datetime.now(UTC) + expires_in))
    db.commit()
    return code


def register(
    client: TestClient, db: Session, email: str, password: str = PASSWORD
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "invite_code": make_invite(db)},
        headers=CSRF,
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    assert uuid.UUID(body["id"])
    return body


def login(client: TestClient, email: str, password: str = PASSWORD) -> Any:
    return client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}, headers=CSRF
    )


@dataclass
class Actor:
    """A user with an extension token (bearer) and a dashboard login (cookie client)."""

    user_id: str
    bearer: dict[str, str]
    dashboard: TestClient


def make_actor(client: TestClient, db: Session, email: str) -> Actor:
    dashboard = TestClient(client.app, base_url="http://localhost")
    user = register(dashboard, db, email)
    assert login(dashboard, email).status_code == 200
    token = dashboard.post("/api/v1/extension-tokens", headers=CSRF).json()["token"]
    return Actor(user["id"], {"Authorization": f"Bearer {token}"}, dashboard)
