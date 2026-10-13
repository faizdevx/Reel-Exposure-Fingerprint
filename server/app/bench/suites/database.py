"""A scratch PostgreSQL database for the benchmarks. It is WIPED, so the name must say so."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

SERVER_DIR = Path(__file__).resolve().parents[3]


class UnsafeDatabase(Exception):
    pass


def check_name(url: str) -> None:
    name = make_url(url).database or ""
    if not name.endswith("_bench"):
        raise UnsafeDatabase(
            f"refusing to wipe database {name!r}: the benchmark only uses a database whose name "
            "ends in _bench"
        )


def engine_for(url: str) -> Engine:
    check_name(url)
    return create_engine(url, pool_pre_ping=True)


def _alembic(url: str) -> Config:
    config = Config(str(SERVER_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def reset(url: str) -> Engine:
    """Drop everything in the benchmark database and migrate it to head."""
    engine = engine_for(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(_alembic(url), "head")
    return engine


def migrate(url: str) -> Engine:
    engine = engine_for(url)
    command.upgrade(_alembic(url), "head")
    return engine
