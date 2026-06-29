"""Local (vendored) settings for the QC-Evaluation container.

A self-contained copy of :mod:`cloud.config.settings` so the service image (build
context = service dir only) has working configuration without the rest of the
monorepo. The shared version is preferred in the monorepo/test layout. Keep in
sync with ``cloud/config/settings.py``.
"""

from __future__ import annotations

from functools import lru_cache

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except Exception:  # pragma: no cover
    from pydantic import BaseModel as BaseSettings  # type: ignore

    SettingsConfigDict = dict  # type: ignore
    _HAS_PYDANTIC_SETTINGS = False

from pydantic import Field


class Settings(BaseSettings):
    """Typed application settings (env-driven, dev-safe defaults)."""

    service_name: str = Field(default="qc-evaluation")
    version: str = Field(default="0.1.0")
    environment: str = Field(default="development")
    api_prefix: str = Field(default="/api/v1")

    database_url: str = Field(
        default="postgresql+asyncpg://qconnect:qconnect@postgres:5432/qconnect"
    )
    timescale_url: str = Field(
        default="postgresql+asyncpg://qconnect:qconnect@timescaledb:5432/qconnect_ts"
    )
    redis_url: str = Field(default="redis://redis:6379/0")

    neo4j_uri: str = Field(default="bolt://neo4j:7687")
    neo4j_user: str = Field(default="neo4j")
    neo4j_password: str = Field(default="qconnect-neo4j")

    rabbitmq_url: str = Field(default="amqp://guest:guest@rabbitmq:5672//")

    jwt_secret: str = Field(default="dev-insecure-secret-change-me")
    jwt_algorithm: str = Field(default="HS256")
    jwt_ttl_seconds: int = Field(default=86_400)
    require_auth: bool = Field(default=False)

    ml_inference_url: str = Field(default="http://ml-inference:8001")
    federated_learning_url: str = Field(default="http://federated-learning:8002")
    rca_capa_url: str = Field(default="http://rca-capa:8003")
    analytics_url: str = Field(default="http://analytics:8004")
    ml_inference_timeout_seconds: float = Field(default=2.0)

    log_level: str = Field(default="INFO")

    if _HAS_PYDANTIC_SETTINGS:
        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            case_sensitive=False,
            extra="ignore",
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()
