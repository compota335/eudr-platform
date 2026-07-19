"""API tests for the shipment detail + plot attach/detach layer.

The shipments router is not wired into ``app.main`` yet, so these tests stand
up a LOCAL FastAPI app mounting only ``shipments.router`` over an in-memory
SQLite engine (the shared conftest ``client`` fixture is intentionally NOT
used). They cover the detail page (attached vs. attachable plots), the atomic
attach flow with every fail-loud refusal (unknown ids, cross-client plots,
double attach), and detach.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_session
from app.models import Base
from app.models.client import Client
from app.models.enums import ClientSide, Commodity, RiskLevel
from app.models.plot import Plot
from app.models.shipment import Shipment, shipment_plot
from app.models.supplier import Supplier
from app.routers import shipments

# Real, small, valid WGS84 GeoJSON geometries near Sao Paulo.
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
def shipments_app(_engine):
    factory = sessionmaker(bind=_engine, expire_on_commit=False)

    def _override():
        with factory() as s:
            yield s

    app = FastAPI()
    app.include_router(shipments.router)
    app.dependency_overrides[get_session] = _override
    return app, factory


# --------------------------------------------------------------------------- #
# Graph helpers                                                                 #
# --------------------------------------------------------------------------- #
def _make_plot(supplier: Supplier, external_ref: str, risk: RiskLevel | None) -> Plot:
    return Plot(
        supplier=supplier,
        external_ref=external_ref,
        commodity=Commodity.coffee,
        country="BR",
        geometry_geojson=POLYGON_GEOJSON,
        geometry_type="Polygon",
        area_ha=6.0,
        centroid_lon=-46.595,
        centroid_lat=-23.495,
        risk_level=risk,
    )


def _seed_graph(session: Session) -> dict[str, int | list[int]]:
    """Persist two clients and return the relevant ids.

    Client A owns a supplier with three plots — the first attached to A's
    shipment, the other two left attachable. Client B owns one plot of its own
    (the cross-client case).
    """
    client_a = Client(name="Acme Coffee GmbH", side=ClientSide.importer_eu, country="DE")
    supplier_a = Supplier(name="Cooperativa X", country="BR", client=client_a)
    attached_plot = _make_plot(supplier_a, "parcel-attached", RiskLevel.green)
    free_plot_1 = _make_plot(supplier_a, "parcel-free-1", RiskLevel.amber)
    free_plot_2 = _make_plot(supplier_a, "parcel-free-2", None)
    shipment = Shipment(
        client=client_a,
        reference="SHIP-001",
        commodity=Commodity.coffee,
        cn_code="0901",
        quantity_kg=12_000.0,
        country_of_production="BR",
        plots=[attached_plot],
    )

    client_b = Client(name="Other Cocoa BV", side=ClientSide.importer_eu, country="NL")
    supplier_b = Supplier(name="Cooperativa Y", country="CI", client=client_b)
    foreign_plot = _make_plot(supplier_b, "parcel-foreign", RiskLevel.green)

    session.add_all([client_a, shipment, free_plot_1, free_plot_2, client_b, foreign_plot])
    session.commit()
    return {
        "shipment_id": shipment.id,
        "attached_id": attached_plot.id,
        "free_ids": [free_plot_1.id, free_plot_2.id],
        "foreign_id": foreign_plot.id,
    }


def _attached_plot_ids(session: Session, shipment_id: int) -> set[int]:
    """Read the association rows straight from the ``shipment_plot`` table."""
    rows = session.execute(
        select(shipment_plot.c.plot_id).where(shipment_plot.c.shipment_id == shipment_id)
    )
    return {row.plot_id for row in rows}


# --------------------------------------------------------------------------- #
# Detail page                                                                   #
# --------------------------------------------------------------------------- #
def test_detail_page_separates_attached_and_available(shipments_app) -> None:
    app, factory = shipments_app
    with factory() as s:
        ids = _seed_graph(s)

    client = TestClient(app, follow_redirects=False)
    resp = client.get(f"/shipments/{ids['shipment_id']}")
    assert resp.status_code == 200

    # Facts and breadcrumb.
    assert "SHIP-001" in resp.text
    assert "Acme Coffee GmbH" in resp.text
    assert "0901" in resp.text

    # The attached plot renders in the table, NOT among the checkboxes.
    assert "parcel-attached" in resp.text
    assert f'name="plot_ids" value="{ids["attached_id"]}"' not in resp.text

    # Both free plots of the same client render as checkboxes.
    free_1, free_2 = ids["free_ids"]
    assert f'name="plot_ids" value="{free_1}"' in resp.text
    assert f'name="plot_ids" value="{free_2}"' in resp.text
    assert "parcel-free-1" in resp.text
    # The unassessed plot carries the honest badge, not a fabricated risk.
    assert "not assessed" in resp.text

    # The other client's plot never appears.
    assert "parcel-foreign" not in resp.text
    assert f'name="plot_ids" value="{ids["foreign_id"]}"' not in resp.text


def test_detail_page_unknown_shipment_is_404(shipments_app) -> None:
    app, _factory = shipments_app
    client = TestClient(app, follow_redirects=False)
    assert client.get("/shipments/999").status_code == 404


# --------------------------------------------------------------------------- #
# Attach: happy path                                                            #
# --------------------------------------------------------------------------- #
def test_attach_one_plot(shipments_app) -> None:
    app, factory = shipments_app
    with factory() as s:
        ids = _seed_graph(s)
    free_1, _free_2 = ids["free_ids"]

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        f"/shipments/{ids['shipment_id']}/plots", data={"plot_ids": [free_1]}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/shipments/{ids['shipment_id']}"

    with factory() as s:
        assert _attached_plot_ids(s, ids["shipment_id"]) == {ids["attached_id"], free_1}


def test_attach_multiple_plots(shipments_app) -> None:
    app, factory = shipments_app
    with factory() as s:
        ids = _seed_graph(s)

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        f"/shipments/{ids['shipment_id']}/plots", data={"plot_ids": ids["free_ids"]}
    )
    assert resp.status_code == 303

    with factory() as s:
        assert _attached_plot_ids(s, ids["shipment_id"]) == {
            ids["attached_id"],
            *ids["free_ids"],
        }


# --------------------------------------------------------------------------- #
# Attach: fail-loud refusals (nothing mutates)                                  #
# --------------------------------------------------------------------------- #
def test_attach_plot_of_other_client_is_400(shipments_app) -> None:
    app, factory = shipments_app
    with factory() as s:
        ids = _seed_graph(s)

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        f"/shipments/{ids['shipment_id']}/plots", data={"plot_ids": [ids["foreign_id"]]}
    )
    assert resp.status_code == 400
    assert f"Plot {ids['foreign_id']} belongs to a different client." in resp.text

    with factory() as s:
        assert _attached_plot_ids(s, ids["shipment_id"]) == {ids["attached_id"]}


def test_attach_mixed_valid_and_foreign_attaches_nothing(shipments_app) -> None:
    """Atomicity: one bad id in the batch means NO plot is attached."""
    app, factory = shipments_app
    with factory() as s:
        ids = _seed_graph(s)
    free_1, _free_2 = ids["free_ids"]

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        f"/shipments/{ids['shipment_id']}/plots",
        data={"plot_ids": [free_1, ids["foreign_id"]]},
    )
    assert resp.status_code == 400

    with factory() as s:
        assert _attached_plot_ids(s, ids["shipment_id"]) == {ids["attached_id"]}


def test_attach_already_attached_plot_is_400(shipments_app) -> None:
    app, factory = shipments_app
    with factory() as s:
        ids = _seed_graph(s)

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        f"/shipments/{ids['shipment_id']}/plots", data={"plot_ids": [ids["attached_id"]]}
    )
    assert resp.status_code == 400
    assert f"Plot {ids['attached_id']} is already attached to this shipment." in resp.text

    with factory() as s:
        assert _attached_plot_ids(s, ids["shipment_id"]) == {ids["attached_id"]}


def test_attach_unknown_plot_is_404(shipments_app) -> None:
    app, factory = shipments_app
    with factory() as s:
        ids = _seed_graph(s)

    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/shipments/{ids['shipment_id']}/plots", data={"plot_ids": [999]})
    assert resp.status_code == 404
    assert "Plot 999 not found." in resp.text

    with factory() as s:
        assert _attached_plot_ids(s, ids["shipment_id"]) == {ids["attached_id"]}


def test_attach_to_unknown_shipment_is_404(shipments_app) -> None:
    app, factory = shipments_app
    with factory() as s:
        ids = _seed_graph(s)
    free_1, _free_2 = ids["free_ids"]

    client = TestClient(app, follow_redirects=False)
    resp = client.post("/shipments/999/plots", data={"plot_ids": [free_1]})
    assert resp.status_code == 404


def test_attach_without_plot_ids_is_422(shipments_app) -> None:
    """A submission missing the required list fails loud, never redirects."""
    app, factory = shipments_app
    with factory() as s:
        ids = _seed_graph(s)

    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/shipments/{ids['shipment_id']}/plots", data={})
    assert resp.status_code == 422

    with factory() as s:
        assert _attached_plot_ids(s, ids["shipment_id"]) == {ids["attached_id"]}


# --------------------------------------------------------------------------- #
# Detach                                                                        #
# --------------------------------------------------------------------------- #
def test_detach_removes_association_but_keeps_plot(shipments_app) -> None:
    app, factory = shipments_app
    with factory() as s:
        ids = _seed_graph(s)

    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        f"/shipments/{ids['shipment_id']}/plots/{ids['attached_id']}/detach"
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/shipments/{ids['shipment_id']}"

    with factory() as s:
        assert _attached_plot_ids(s, ids["shipment_id"]) == set()
        # The plot row itself survives; only the association is gone.
        assert s.get(Plot, ids["attached_id"]) is not None


def test_detach_not_attached_plot_is_404(shipments_app) -> None:
    app, factory = shipments_app
    with factory() as s:
        ids = _seed_graph(s)
    free_1, _free_2 = ids["free_ids"]

    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/shipments/{ids['shipment_id']}/plots/{free_1}/detach")
    assert resp.status_code == 404
    assert f"Plot {free_1} is not attached to this shipment." in resp.text

    with factory() as s:
        assert _attached_plot_ids(s, ids["shipment_id"]) == {ids["attached_id"]}


def test_detach_from_unknown_shipment_is_404(shipments_app) -> None:
    app, factory = shipments_app
    with factory() as s:
        ids = _seed_graph(s)

    client = TestClient(app, follow_redirects=False)
    resp = client.post(f"/shipments/999/plots/{ids['attached_id']}/detach")
    assert resp.status_code == 404
