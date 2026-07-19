"""Tests for the supplier workspace: plot import and per-plot assessment.

The router is not yet wired into ``app.main`` (the orchestrator does that
later), so these tests stand up a LOCAL FastAPI app carrying only
``suppliers.router`` over an isolated in-memory database, rather than leaning
on the shared ``client`` fixture (which boots the real app).

The deforestation provider is injected via ``app.dependency_overrides``: a
fake returns canned RED :class:`PlotEvidence`, and failing fakes prove the
fail-loud paths (503 unconfigured, 502 provider error) store nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.geo.schemas import (
    DatasetSignal,
    PlotEvidence,
    RiskProviderError,
    RiskProviderNotConfigured,
)
from app.models import Base
from app.models.client import Client
from app.models.enums import ClientSide, PlotStatus, RiskLevel
from app.models.evidence import Evidence
from app.models.plot import Plot
from app.models.supplier import Supplier
from app.routers import suppliers
from app.routers.plot_checker import get_risk_provider

# A valid ~1.2 ha square near (-55.0, -10.0). 0.001 deg is ~111 m of latitude
# and ~109 m of longitude at 10 deg south, so the box is roughly a hectare.
_POLYGON: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [
        [
            [-55.000, -10.000],
            [-54.999, -10.000],
            [-54.999, -9.999],
            [-55.000, -9.999],
            [-55.000, -10.000],
        ]
    ],
}


# --------------------------------------------------------------------------- #
# Fake providers                                                               #
# --------------------------------------------------------------------------- #
def _red_evidence(
    geometry: dict[str, Any], *, external_ref: str | None = None
) -> PlotEvidence:
    """Two independent post-2020 loss families -> a RED verdict."""
    return PlotEvidence(
        forest_2020=(
            DatasetSignal(dataset="EUFO_2020", family="EUFO", value=1.0, kind="forest_2020"),
        ),
        loss_after_2020=(
            DatasetSignal(
                dataset="GFC_loss_after_2020", family="GFC", value=0.5,
                kind="loss_after_2020",
            ),
            DatasetSignal(
                dataset="RADD_after_2020", family="RADD", value=0.3,
                kind="loss_after_2020",
            ),
        ),
        provider="fake",
        dataset_versions={"fake": "v1"},
        raw={"note": "canned"},
    )


def _raise_not_configured(
    geometry: dict[str, Any], *, external_ref: str | None = None
) -> PlotEvidence:
    raise RiskProviderNotConfigured("no key in test")


def _raise_provider_error(
    geometry: dict[str, Any], *, external_ref: str | None = None
) -> PlotEvidence:
    raise RiskProviderError("upstream analysis timed out")


# --------------------------------------------------------------------------- #
# Fixtures: local app + isolated in-memory database                            #
# --------------------------------------------------------------------------- #
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
def _factory(_engine):
    return sessionmaker(bind=_engine, expire_on_commit=False)


@pytest.fixture
def app(_factory) -> FastAPI:
    def _override() -> Iterator[Session]:
        with _factory() as s:
            yield s

    application = FastAPI()
    application.include_router(suppliers.router)
    application.dependency_overrides[get_session] = _override
    application.dependency_overrides[get_risk_provider] = lambda: _red_evidence
    return application


@pytest.fixture
def web(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(_factory) -> Iterator[Session]:
    with _factory() as s:
        yield s


def _create_supplier(db: Session) -> Supplier:
    """Seed a client + supplier directly (the router only READS suppliers)."""
    client = Client(name="Acme Coffee", side=ClientSide.importer_eu)
    db.add(client)
    db.commit()
    supplier = Supplier(client_id=client.id, name="Fazenda Verde", country="BR")
    db.add(supplier)
    db.commit()
    return supplier


def _post_polygon(web: TestClient, supplier_id: int, **extra: str) -> Any:
    data: dict[str, str] = {"geojson": json.dumps(_POLYGON)}
    data.update(extra)
    return web.post(
        f"/suppliers/{supplier_id}/plots", data=data, follow_redirects=False
    )


# --------------------------------------------------------------------------- #
# Detail page                                                                  #
# --------------------------------------------------------------------------- #
def test_detail_page_lists_plots(web: TestClient, db: Session) -> None:
    supplier = _create_supplier(db)
    resp = _post_polygon(web, supplier.id)
    assert resp.status_code == 303

    page = web.get(f"/suppliers/{supplier.id}")
    assert page.status_code == 200
    assert "Fazenda Verde" in page.text
    assert "Acme Coffee" in page.text  # breadcrumb links back to the client
    assert "Polygon" in page.text
    assert "not assessed" in page.text
    assert f"/plots/{db.scalars(select(Plot.id)).one()}/assess" in page.text


def test_detail_page_unknown_supplier_is_404(web: TestClient) -> None:
    resp = web.get("/suppliers/999")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Plot creation                                                                #
# --------------------------------------------------------------------------- #
def test_create_plot_from_pasted_geojson(web: TestClient, db: Session) -> None:
    supplier = _create_supplier(db)
    resp = _post_polygon(web, supplier.id, commodity="coffee", country="br")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/suppliers/{supplier.id}"

    plot = db.scalars(select(Plot)).one()
    assert plot.supplier_id == supplier.id
    assert plot.geometry_type == "Polygon"
    assert plot.status is PlotStatus.valid
    assert plot.commodity is not None and plot.commodity.value == "coffee"
    assert plot.country == "BR"
    assert plot.area_ha is not None and 0.8 < plot.area_ha < 1.6
    assert plot.centroid_lon == pytest.approx(-54.9995, abs=0.001)
    assert plot.centroid_lat == pytest.approx(-9.9995, abs=0.001)
    assert plot.risk_level is None
    assert json.loads(plot.geometry_geojson)["type"] == "Polygon"


def test_multi_geometry_input_creates_one_plot_each(
    web: TestClient, db: Session
) -> None:
    supplier = _create_supplier(db)
    collection = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"plot_id": "P-1"}, "geometry": _POLYGON},
            {"type": "Feature", "properties": {"plot_id": "P-2"}, "geometry": _POLYGON},
        ],
    }
    resp = web.post(
        f"/suppliers/{supplier.id}/plots",
        data={"geojson": json.dumps(collection)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    plots = list(db.scalars(select(Plot).order_by(Plot.id)))
    assert len(plots) == 2
    assert [p.external_ref for p in plots] == ["P-1", "P-2"]
    assert all(p.status is PlotStatus.valid for p in plots)


def test_invalid_geometry_is_400_and_creates_nothing(
    web: TestClient, db: Session
) -> None:
    supplier = _create_supplier(db)
    # Second feature is unparseable: the whole import must fail atomically.
    collection = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": _POLYGON},
            {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon"}},
        ],
    }
    resp = web.post(
        f"/suppliers/{supplier.id}/plots",
        data={"geojson": json.dumps(collection)},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert db.query(Plot).count() == 0


def test_missing_input_is_400(web: TestClient, db: Session) -> None:
    supplier = _create_supplier(db)
    resp = web.post(
        f"/suppliers/{supplier.id}/plots", data={"geojson": "   "},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert db.query(Plot).count() == 0


def test_create_plots_unknown_supplier_is_404(web: TestClient) -> None:
    resp = web.post(
        "/suppliers/999/plots",
        data={"geojson": json.dumps(_POLYGON)},
        follow_redirects=False,
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Assessment (Evidence is append-only)                                         #
# --------------------------------------------------------------------------- #
def _seed_plot(web: TestClient, db: Session) -> Plot:
    supplier = _create_supplier(db)
    assert _post_polygon(web, supplier.id).status_code == 303
    return db.scalars(select(Plot)).one()


def test_assess_stores_risk_level_and_one_evidence_row(
    web: TestClient, db: Session
) -> None:
    plot = _seed_plot(web, db)
    resp = web.post(f"/plots/{plot.id}/assess", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/suppliers/{plot.supplier_id}"

    db.expire_all()
    assert plot.risk_level is RiskLevel.red
    evidence = db.scalars(select(Evidence)).one()
    assert evidence.plot_id == plot.id
    assert evidence.stage == "deforestation_analysis"
    assert evidence.provider == "fake"
    assert evidence.risk_level is RiskLevel.red
    assert json.loads(evidence.data_json)["level"] == "red"
    assert evidence.dataset_versions is not None
    assert json.loads(evidence.dataset_versions) == {"fake": "v1"}


def test_reassess_appends_second_evidence_row(web: TestClient, db: Session) -> None:
    plot = _seed_plot(web, db)
    assert web.post(f"/plots/{plot.id}/assess", follow_redirects=False).status_code == 303
    assert web.post(f"/plots/{plot.id}/assess", follow_redirects=False).status_code == 303

    db.expire_all()
    rows = list(db.scalars(select(Evidence).order_by(Evidence.id)))
    assert len(rows) == 2
    assert rows[0].id != rows[1].id
    assert all(row.stage == "deforestation_analysis" for row in rows)


def test_assess_unknown_plot_is_404(web: TestClient) -> None:
    resp = web.post("/plots/999/assess", follow_redirects=False)
    assert resp.status_code == 404


def test_assess_provider_not_configured_is_503_and_stores_nothing(
    app: FastAPI, web: TestClient, db: Session
) -> None:
    plot = _seed_plot(web, db)
    app.dependency_overrides[get_risk_provider] = lambda: _raise_not_configured

    resp = web.post(f"/plots/{plot.id}/assess", follow_redirects=False)
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]

    db.expire_all()
    assert plot.risk_level is None
    assert db.query(Evidence).count() == 0


def test_assess_provider_error_is_502(
    app: FastAPI, web: TestClient, db: Session
) -> None:
    plot = _seed_plot(web, db)
    app.dependency_overrides[get_risk_provider] = lambda: _raise_provider_error

    resp = web.post(f"/plots/{plot.id}/assess", follow_redirects=False)
    assert resp.status_code == 502
    assert "upstream analysis timed out" in resp.json()["detail"]

    db.expire_all()
    assert plot.risk_level is None
    assert db.query(Evidence).count() == 0
