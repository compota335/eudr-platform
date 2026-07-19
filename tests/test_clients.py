"""Tests for the client-onboarding CRUD layer.

The router is not yet wired into ``app.main`` (the orchestrator does that
later), so these tests stand up a LOCAL FastAPI app carrying only
``clients.router`` over an isolated in-memory database, rather than leaning on
the shared ``client`` fixture (which boots the real app).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.models import Base
from app.routers import clients


@pytest.fixture
def _engine():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        cur = dbapi.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def onboarding_client(_engine):
    factory = sessionmaker(bind=_engine, expire_on_commit=False)

    def _override():
        with factory() as s:
            yield s

    app = FastAPI()
    app.include_router(clients.router)
    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c


def _create_client(client: TestClient, **overrides: str) -> int:
    """POST a valid client and return the new client's id (from the redirect)."""
    data = {"name": "Acme Coffee", "side": "importer_eu"}
    data.update(overrides)
    resp = client.post("/clients", data=data, follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/clients/")
    return int(location.rsplit("/", 1)[-1])


# --------------------------------------------------------------------------- #
# Happy path: client, supplier, shipment                                       #
# --------------------------------------------------------------------------- #
def test_create_client_then_list_and_detail(onboarding_client: TestClient) -> None:
    client_id = _create_client(
        onboarding_client, country="DE", contact_email="ops@acme.example"
    )

    listing = onboarding_client.get("/clients")
    assert listing.status_code == 200
    assert "Acme Coffee" in listing.text

    detail = onboarding_client.get(f"/clients/{client_id}")
    assert detail.status_code == 200
    assert "Acme Coffee" in detail.text
    # Empty-state copy for the not-yet-populated relationships.
    assert "No suppliers yet." in detail.text
    assert "No shipments yet." in detail.text
    assert "No statements yet" in detail.text


def test_add_supplier_shows_on_detail(onboarding_client: TestClient) -> None:
    client_id = _create_client(onboarding_client)

    resp = onboarding_client.post(
        f"/clients/{client_id}/suppliers",
        data={
            "name": "Fazenda Verde",
            "country": "BR",
            "commodity": "coffee",
            "approx_volume_t": "12.5",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/clients/{client_id}"

    detail = onboarding_client.get(f"/clients/{client_id}")
    assert "Fazenda Verde" in detail.text
    assert "coffee" in detail.text
    assert "12.5" in detail.text


def test_create_shipment_shows_on_detail(onboarding_client: TestClient) -> None:
    client_id = _create_client(onboarding_client)

    resp = onboarding_client.post(
        f"/clients/{client_id}/shipments",
        data={
            "reference": "SHP-001",
            "commodity": "cocoa",
            "cn_code": "1801",
            "quantity_kg": "2500",
            "country_of_production": "CI",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/clients/{client_id}"

    detail = onboarding_client.get(f"/clients/{client_id}")
    assert "SHP-001" in detail.text
    assert "cocoa" in detail.text
    assert "1801" in detail.text
    # New shipments default to the pending status.
    assert "pending" in detail.text


def test_optional_blank_fields_stored_as_none(onboarding_client: TestClient) -> None:
    """A supplier created with blank optionals renders as em-dashes, not '' text."""
    client_id = _create_client(onboarding_client)

    resp = onboarding_client.post(
        f"/clients/{client_id}/suppliers",
        data={"name": "Minimal Co", "country": "", "commodity": "", "approx_volume_t": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    detail = onboarding_client.get(f"/clients/{client_id}")
    assert "Minimal Co" in detail.text


# --------------------------------------------------------------------------- #
# Fail-loud: validation and missing rows                                        #
# --------------------------------------------------------------------------- #
def test_invalid_side_is_400(onboarding_client: TestClient) -> None:
    resp = onboarding_client.post(
        "/clients",
        data={"name": "Bad Side", "side": "not_a_side"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_blank_name_is_400(onboarding_client: TestClient) -> None:
    resp = onboarding_client.post(
        "/clients",
        data={"name": "   ", "side": "importer_eu"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_invalid_commodity_on_supplier_is_400(onboarding_client: TestClient) -> None:
    client_id = _create_client(onboarding_client)
    resp = onboarding_client.post(
        f"/clients/{client_id}/suppliers",
        data={"name": "Bad Commodity", "commodity": "bananas"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_invalid_commodity_on_shipment_is_400(onboarding_client: TestClient) -> None:
    client_id = _create_client(onboarding_client)
    resp = onboarding_client.post(
        f"/clients/{client_id}/shipments",
        data={"reference": "SHP-X", "commodity": "bananas"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_unparseable_quantity_is_400(onboarding_client: TestClient) -> None:
    client_id = _create_client(onboarding_client)
    resp = onboarding_client.post(
        f"/clients/{client_id}/shipments",
        data={"reference": "SHP-Y", "quantity_kg": "heavy"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_supplier_under_missing_client_is_404(onboarding_client: TestClient) -> None:
    resp = onboarding_client.post(
        "/clients/999/suppliers",
        data={"name": "Orphan"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_shipment_under_missing_client_is_404(onboarding_client: TestClient) -> None:
    resp = onboarding_client.post(
        "/clients/999/shipments",
        data={"reference": "Orphan"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_get_missing_client_is_404(onboarding_client: TestClient) -> None:
    resp = onboarding_client.get("/clients/999")
    assert resp.status_code == 404
