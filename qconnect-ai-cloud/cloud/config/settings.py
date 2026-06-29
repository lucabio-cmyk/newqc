"""Application settings via pydantic-settings.

All configuration is read from environment variables (or a ``.env`` file). Every
field has a sane local-development default so the services start without a fully
provisioned environment — production deployments override via ``env_file`` /
container env.
"""

from __future__ import annotations

from functools import lru_cache

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except Exception:  # pragma: no cover - fallback for minimal envs
    from pydantic import BaseModel as BaseSettings  # type: ignore

    SettingsConfigDict = dict  # type: ignore
    _HAS_PYDANTIC_SETTINGS = False

from pydantic import Field


class Settings(BaseSettings):
    """Typed application settings.

    Field names map to UPPER_SNAKE_CASE environment variables (case-insensitive).
    """

    # --- service identity -------------------------------------------------- #
    service_name: str = Field(default="qc-evaluation")
    version: str = Field(default="0.1.0")
    environment: str = Field(default="development")
    api_prefix: str = Field(default="/api/v1")

    # --- datastores -------------------------------------------------------- #
    database_url: str = Field(
        default="postgresql+asyncpg://qconnect:qconnect@localhost:5432/qconnect",
        description="Async SQLAlchemy DSN for the primary Postgres DB",
    )
    timescale_url: str = Field(
        default="postgresql+asyncpg://qconnect:qconnect@localhost:5433/qconnect_ts",
        description="Async DSN for the TimescaleDB time-series store",
    )
    redis_url: str = Field(default="redis://localhost:6379/0")

    # --- graph store (RCA/CAPA) ------------------------------------------- #
    neo4j_uri: str = Field(default="bolt://localhost:7687")
    neo4j_user: str = Field(default="neo4j")
    neo4j_password: str = Field(default="qconnect-neo4j")

    # --- messaging --------------------------------------------------------- #
    rabbitmq_url: str = Field(default="amqp://guest:guest@localhost:5672//")

    # --- security ---------------------------------------------------------- #
    jwt_secret: str = Field(
        default="dev-insecure-secret-change-me",
        description="HS256 signing secret. MUST be overridden in production.",
    )
    jwt_algorithm: str = Field(default="HS256")
    jwt_ttl_seconds: int = Field(default=86_400)
    require_auth: bool = Field(
        default=False,
        description="When False, the bearer dependency is optional (dev mode).",
    )

    # --- downstream services ---------------------------------------------- #
    ml_inference_url: str = Field(default="http://ml-inference:8001")
    federated_learning_url: str = Field(default="http://federated-learning:8002")
    rca_capa_url: str = Field(default="http://rca-capa:8003")
    analytics_url: str = Field(default="http://analytics:8004")
    ml_inference_timeout_seconds: float = Field(default=2.0)
    rca_capa_timeout_seconds: float = Field(default=2.0)
    auto_capa: bool = Field(
        default=True,
        description="When True, a FAIL result auto-drafts a CAPA via the rca_capa service.",
    )

    # --- observability ----------------------------------------------------- #
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
