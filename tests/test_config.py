import pytest
from pydantic import ValidationError

from app.config import Settings


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REF_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_database_url_is_never_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REF_DATABASE_URL", "postgresql+psycopg://u:hunter2@h/db")
    settings = Settings(_env_file=None)
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings)
    assert settings.database_url.get_secret_value().endswith("@h/db")


def test_invalid_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REF_DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("REF_LOG_LEVEL", "LOUD")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
