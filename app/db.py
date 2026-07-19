"""Database engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base


def _build_engine() -> Engine:
    url = settings.sqlalchemy_url
    connect_args: dict[str, object] = {}

    if settings.is_sqlite:
        # SQLite is file-backed in dev; ensure the parent directory exists.
        db_path = url.split("///", 1)[-1]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # FastAPI/uvicorn use multiple threads; allow cross-thread use.
        connect_args["check_same_thread"] = False

    engine = create_engine(
        url,
        echo=settings.db_echo,
        future=True,
        connect_args=connect_args,
    )

    if settings.is_sqlite:
        # SQLite does not enforce foreign keys unless asked to.
        @event.listens_for(engine, "connect")
        def _enable_sqlite_fk(dbapi_connection, _connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a database session."""
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    """Create all tables. Used for development and tests (Alembic owns prod)."""
    Base.metadata.create_all(engine)
