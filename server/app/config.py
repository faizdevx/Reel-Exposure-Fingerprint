"""Application settings, read from environment variables only (CONTRIBUTING.md: no hardcoded config)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["local", "ci", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # No default: a missing database URL must fail loudly, never fall back to a guess.
    # Example: postgresql+psycopg://user:password@localhost:5432/ref
    database_url: SecretStr = Field(...)

    # Raw frames live here for at most 24 h (F15): a local volume, no object storage.
    frame_store_dir: Path = Path("/var/lib/ref/frames")
    # Retention is capped at 24 hours by the spec; a shorter TTL is allowed, a longer one is not.
    frame_ttl_hours: int = Field(default=24, ge=1, le=24)
    # How often the API sweeps expired frames. 0 turns the in-process sweeper off (the
    # `python -m app.manage sweep-frames` command still works, for cron).
    frame_sweep_interval_seconds: int = Field(default=900, ge=0, le=86400)

    # Text pipeline (worker). The model is pinned in app/worker/config.py; these are only places.
    # The taxonomy file is versioned in the repo; the default is the repo layout, run from server/.
    taxonomy_file: Path = Path("../taxonomy/topics.json")
    # Where Hugging Face files are cached. None: the Hugging Face default (HF_HOME).
    model_cache_dir: Path | None = None
    # Cached taxonomy embeddings (small .npz files; safe to delete, they are rebuilt).
    embedding_cache_dir: Path = Path("/var/lib/ref/embeddings")
    # Which SigLIP model embeds frames. No default: the choice is still open.
    # Unset means the visual stage is off and the pipeline is text only.
    visual_model: Literal["siglip2-base", "siglip-multilingual"] | None = None
    # True: never reach the network for model files (they must already be cached).
    hf_local_files_only: bool = False

    # Dashboard login sessions and invite codes (spec §7, ADR-004).
    session_ttl_hours: int = Field(default=168, ge=1, le=24 * 90)
    invite_ttl_hours: int = Field(default=72, ge=1, le=24 * 30)
    # Secure cookies need TLS. Default: on in production, off for local/ci (plain http).
    cookie_secure: bool | None = None

    # Rate limits (spec N6, ADR-005, audit H-1). Token buckets in process memory: `*_burst` is the
    # bucket size, `*_per_minute` the refill. The defaults are the ADR-005 values for ingest.
    ingest_per_minute: int = Field(default=60, ge=1)
    ingest_burst: int = Field(default=20, ge=1)
    token_per_minute: int = Field(default=30, ge=1)  # other extension-token requests
    token_burst: int = Field(default=10, ge=1)
    dashboard_per_minute: int = Field(default=300, ge=1)
    dashboard_burst: int = Field(default=60, ge=1)
    destructive_per_minute: int = Field(default=10, ge=1)  # every DELETE
    destructive_burst: int = Field(default=5, ge=1)
    api_ip_per_minute: int = Field(default=600, ge=1)  # every /api/ request, by client address
    api_ip_burst: int = Field(default=120, ge=1)
    auth_ip_per_minute: int = Field(default=20, ge=1)  # login and register, by client address
    auth_ip_burst: int = Field(default=10, ge=1)
    login_failures_per_email: int = Field(default=5, ge=1)
    login_failure_refill_seconds: int = Field(default=180, ge=1)
    password_concurrency: int = Field(default=4, ge=1)  # simultaneous argon2 hashes or checks
    rate_limit_max_keys: int = Field(default=10_000, ge=100)
    # Comma-separated networks whose `X-Forwarded-For` is believed (the reverse proxy in front of
    # the API). Empty: the direct peer is the client. Never list a network clients can reach.
    trusted_proxies: str = ""

    @model_validator(mode="after")
    def _production_is_secure(self) -> "Settings":
        """A production server never sends the session cookie over plain HTTP (audit H-4)."""
        if self.environment == "production" and self.cookie_secure is False:
            raise ValueError(
                "REF_COOKIE_SECURE=false is not allowed when REF_ENVIRONMENT=production"
            )
        return self

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
