"""Shared pytest fixtures. Server tests run from server/ (see server/pyproject.toml)."""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from support import alembic_config, probe

# Settings must be resolvable at import time of app.main. Tests that touch a real database use
# TEST_DATABASE_URL instead and are skipped when it is unset.
os.environ.setdefault(
    "REF_DATABASE_URL", "postgresql+psycopg://unused:unused@localhost:5432/unused"
)
os.environ.setdefault("REF_ENVIRONMENT", "ci")
# No background sweeper in tests; the sweep itself is tested directly.
os.environ.setdefault("REF_FRAME_SWEEP_INTERVAL_SECONDS", "0")


def _test_database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def db_url() -> str:
    url = _test_database_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL not set: needs PostgreSQL with pgvector")
    return url


@pytest.fixture
def clean_engine(db_url: str) -> Iterator[Engine]:
    """Engine on an empty public schema, with the schema dropped again afterwards."""
    engine = create_engine(db_url)

    def reset() -> None:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))

    reset()
    yield engine
    reset()
    engine.dispose()


@pytest.fixture
def migrated_engine(clean_engine: Engine, db_url: str) -> Engine:
    command.upgrade(alembic_config(db_url), "head")
    return clean_engine


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Session]:
    """A session for arranging and inspecting data directly."""
    with sessionmaker(bind=migrated_engine, expire_on_commit=False)() as session:
        yield session


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    """App wired to the test database, plus the test-only /_probe routes."""
    from app.db import get_session
    from app.main import create_app

    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)

    def override() -> Iterator[Session]:
        with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = override
    app.include_router(probe)
    with TestClient(app, base_url="http://localhost") as test_client:
        yield test_client
