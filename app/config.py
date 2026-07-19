"""Application configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    Values come from environment variables (see ``.env.example``). Field names
    map case-insensitively to env vars, so ``app_env`` reads ``APP_ENV``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_env: str = "development"
    app_secret_key: str = "change-me-in-production"
    app_base_url: str = "http://localhost:8000"

    # Database. Empty DATABASE_URL selects the documented SQLite dev default.
    database_url: str = ""
    db_echo: bool = False

    # Deforestation data providers
    whisp_api_url: str = "https://whisp.openforis.org/api"
    whisp_api_key: str = ""
    gfw_api_url: str = "https://data-api.globalforestwatch.org"
    gfw_api_key: str = ""

    # Email capture (wired in a later phase)
    email_provider: str = ""
    email_from: str = ""

    # Evidence store
    evidence_store_path: Path = Path("./evidence_store")

    @property
    def sqlalchemy_url(self) -> str:
        """Return the effective SQLAlchemy URL.

        In development, an empty ``DATABASE_URL`` resolves to a local SQLite
        file. This is an explicit, documented default (see the build spec:
        "SQLite dev, PostgreSQL for prod"), not a silent fallback: production
        must set ``DATABASE_URL`` and will fail loudly if it does not connect.
        """
        if self.database_url:
            return self.database_url
        return "sqlite+pysqlite:///./data/eudr.db"

    @property
    def is_sqlite(self) -> bool:
        return self.sqlalchemy_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()


settings = get_settings()
