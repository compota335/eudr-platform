"""API tests for the DDS web + PDF layer (``app.routers.dds``).

The DDS router is not wired into ``app.main`` yet, so these tests stand up a
LOCAL FastAPI app that mounts only ``dds.router`` over an in-memory SQLite
engine (the shared conftest ``client`` fixture is intentionally NOT used). They
cover the happy assembly path (redirect -> list -> detail -> PDF -> print view),
each fail-loud refusal surfaced by the endpoint (RED block, missing shipment,
unknown ids), and the byte-for-byte determinism of ``render_dds_pdf``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.models import Base
from app.models.client import Client
from app.models.dds import DDS
from app.models.enums import ClientSide, Commodity, RiskLevel
from app.models.plot import Plot
from app.models.shipment import Shipment
from app.models.supplier import Supplier
from app.routers import dds
from app.services.dds_assembly import assemble_dds
from app.services.dds_pdf import render_dds_pdf

# A valid Annex I heading (coffee) for in-scope shipments; see app/data/cn_codes.py.
IN_SCOPE_CN = "0901"

# Real, small, valid WGS84 GeoJSON geometries near Sao Paulo. The polygon backs a
# >=4 ha plot (EUDR polygon rule); the point backs a small plot.
POLYGON_GEOJSON = (
    '{"type":"Polygon","coordinates":'
    "[[[-46.60,-23.50],[-46.59,-23.50],[-46.59,-23.49],[-46.60,-23.49],[-46.60,-23.50]]]}"
)
POINT_GEOJSON = '{"type":"Point","coordinates":[-46.595,-23.495]}'


# --------------------------------------------------------------------------- #
# Local app fixtures (per-test in-memory engine; no shared conftest client).    #
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
def dds_app(_engine):
    factory = sessionmaker(bind=_engine, expire_on_commit=False)

    def _override():
        with factory() as s:
            yield s

    app = FastAPI()
    app.include_router(dds.router)
    app.dependency_overrides[get_session] = _override
    return app, factory


# --------------------------------------------------------------------------- #
# Graph helper                                                                  #
# --------------------------------------------------------------------------- #
def _make_shipment(
    session: Session,
    *,
    plot_specs: list[dict] | None = None,
) -> Shipment:
    """Persist a full Client -> Supplier -> Plot(s) -> Shipment graph.

    Defaults to one large (>=4 ha) green Polygon plus one small green Point, all
    in-scope and fileable. Each ``plot_specs`` dict may override
    ``geometry_geojson``, ``geometry_type``, ``area_ha``, centroid, ``risk_level``
    and ``external_ref``.
    """
    client = Client(
        name="Acme Coffee GmbH",
        side=ClientSide.importer_eu,
        country="DE",
        contact_email="ops@acme-coffee.example",
        eori="DE1234567890123",
    )
    supplier = Supplier(
        name="Cooperativa X",
        country="BR",
        commodity=Commodity.coffee,
        client=client,
    )

    if plot_specs is None:
        plot_specs = [
            {
                "geometry_geojson": POLYGON_GEOJSON,
                "geometry_type": "Polygon",
                "area_ha": 6.0,
                "external_ref": "parcel-large",
            },
            {
                "geometry_geojson": POINT_GEOJSON,
                "geometry_type": "Point",
                "area_ha": 1.2,
                "external_ref": "parcel-small",
            },
        ]

    plots: list[Plot] = []
    for index, spec in enumerate(plot_specs):
        plot = Plot(
            supplier=supplier,
            external_ref=spec.get("external_ref", f"parcel-{index}"),
            commodity=spec.get("commodity", Commodity.coffee),
            country=spec.get("country", "BR"),
            geometry_geojson=spec.get("geometry_geojson", POLYGON_GEOJSON),
            geometry_type=spec.get("geometry_type", "Polygon"),
            area_ha=spec.get("area_ha", 6.0),
            centroid_lon=spec.get("centroid_lon", -46.595),
            centroid_lat=spec.get("centroid_lat", -23.495),
            risk_level=spec.get("risk_level", RiskLevel.green),
        )
        plots.append(plot)

    shipment = Shipment(
        client=client,
        reference="SHIP-001",
        commodity=Commodity.coffee,
        cn_code=IN_SCOPE_CN,
        quantity_kg=12_000.0,
        country_of_production="BR",
        plots=plots,
    )
    session.add(client)
    session.add(shipment)
    session.commit()
    return shipment


def _dds_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(DDS)) or 0


# --------------------------------------------------------------------------- #
# Happy path: assemble -> list -> detail -> PDF -> print view                    #
# --------------------------------------------------------------------------- #
def test_post_dds_happy_path_and_downstream_pages(dds_app) -> None:
    app, factory = dds_app
    with factory() as s:
        shipment = _make_shipment(s)
        shipment_id = shipment.id

    # TestClient must NOT auto-follow, so we can assert the 303 and its target.
    client = TestClient(app, follow_redirects=False)

    resp = client.post("/dds", data={"shipment_id": shipment_id})
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/dds/")
    dds_id = int(location.rsplit("/", 1)[-1])

    with factory() as s:
        assert _dds_count(s) == 1
        row = s.get(DDS, dds_id)
        assert row is not None
        reference_number = row.reference_number
    assert reference_number is not None

    # List shows it.
    listing = client.get("/dds")
    assert listing.status_code == 200
    assert reference_number in listing.text

    # Detail renders and carries the reference number.
    detail = client.get(f"/dds/{dds_id}")
    assert detail.status_code == 200
    assert reference_number in detail.text
    assert "Acme Coffee GmbH" in detail.text

    # PDF: real bytes, correct media type, PDF magic header.
    pdf = client.get(f"/dds/{dds_id}/pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    assert reference_number in pdf.headers["content-disposition"]

    # Print-friendly HTML view.
    print_view = client.get(f"/dds/{dds_id}/pdf.html")
    assert print_view.status_code == 200
    assert print_view.headers["content-type"].startswith("text/html")
    assert reference_number in print_view.text


# --------------------------------------------------------------------------- #
# Fail-loud: a RED plot blocks assembly and persists nothing                     #
# --------------------------------------------------------------------------- #
def test_post_dds_red_plot_is_blocked_and_persists_nothing(dds_app) -> None:
    app, factory = dds_app
    with factory() as s:
        shipment = _make_shipment(
            s,
            plot_specs=[
                {"risk_level": RiskLevel.green, "external_ref": "green-1"},
                {"risk_level": RiskLevel.red, "external_ref": "red-1"},
            ],
        )
        shipment_id = shipment.id

    client = TestClient(app, follow_redirects=False)
    resp = client.post("/dds", data={"shipment_id": shipment_id})

    assert resp.status_code == 400
    lowered = resp.text.lower()
    assert "red" in lowered

    with factory() as s:
        assert _dds_count(s) == 0


# --------------------------------------------------------------------------- #
# Fail-loud: unknown / missing ids -> 404                                        #
# --------------------------------------------------------------------------- #
def test_post_dds_missing_shipment_is_404(dds_app) -> None:
    app, _factory = dds_app
    client = TestClient(app, follow_redirects=False)
    resp = client.post("/dds", data={"shipment_id": 9999})
    assert resp.status_code == 404


def test_get_missing_dds_detail_is_404(dds_app) -> None:
    app, _factory = dds_app
    client = TestClient(app, follow_redirects=False)
    assert client.get("/dds/9999").status_code == 404


def test_get_missing_dds_pdf_is_404(dds_app) -> None:
    app, _factory = dds_app
    client = TestClient(app, follow_redirects=False)
    assert client.get("/dds/9999/pdf").status_code == 404


# --------------------------------------------------------------------------- #
# render_dds_pdf determinism                                                     #
# --------------------------------------------------------------------------- #
def test_render_dds_pdf_is_deterministic(dds_app) -> None:
    _app, factory = dds_app
    with factory() as s:
        shipment = _make_shipment(s)
        row = assemble_dds(s, shipment)
        s.commit()
        first = render_dds_pdf(row)
        second = render_dds_pdf(row)

    assert first == second
    assert first.startswith(b"%PDF")
