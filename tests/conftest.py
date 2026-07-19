"""Shared pytest fixtures: an isolated in-memory database and session."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.main import app
from app.models import Base


@pytest.fixture
def engine() -> Iterator[Engine]:
    # StaticPool keeps ONE shared connection to the in-memory database, so the
    # schema created here is visible from every thread — including the worker
    # thread Starlette's TestClient runs the endpoint on. Without it, a second
    # connection would open a fresh, empty ":memory:" database.
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db_session:
        yield db_session


@pytest.fixture
def client(engine: Engine, session: Session) -> Iterator[TestClient]:
    """A ``TestClient`` whose endpoints share the in-memory database.

    ``get_session`` is overridden to yield a FRESH session per request, bound to
    the same ``engine`` as the test's ``session`` fixture (one shared in-memory
    connection via ``StaticPool``). A per-request session avoids sharing one
    ``Session`` object across the test thread and Starlette's worker thread,
    while ``session`` still sees whatever the endpoint committed.

    The deforestation provider is NOT overridden here (it stays honest and
    fail-loud by default); individual tests inject a fake via
    ``app.dependency_overrides[get_risk_provider]`` when they need one.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def _override_get_session() -> Iterator[Session]:
        with factory() as request_session:
            yield request_session

    app.dependency_overrides[get_session] = _override_get_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
