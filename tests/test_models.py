"""Smoke tests for the ORM data model: relationships, defaults, cascades."""

from __future__ import annotations

from sqlalchemy import func, select

from app.models import (
    Client,
    ClientSide,
    Commodity,
    Plot,
    PlotStatus,
    Supplier,
)


def _count(session, model) -> int:  # noqa: ANN001
    return session.scalar(select(func.count()).select_from(model))


def test_client_supplier_plot_roundtrip(session) -> None:  # noqa: ANN001
    client = Client(name="Acme Coffee GmbH", side=ClientSide.importer_eu, country="DE")
    supplier = Supplier(
        name="Cooperativa X",
        country="BR",
        commodity=Commodity.coffee,
        client=client,
    )
    plot = Plot(
        supplier=supplier,
        commodity=Commodity.coffee,
        country="BR",
        geometry_geojson='{"type":"Point","coordinates":[-46.6,-23.5]}',
        geometry_type="Point",
    )

    session.add(client)
    session.commit()

    assert client.id is not None
    assert supplier.client_id == client.id
    assert plot.supplier_id == supplier.id
    # Enum default applied.
    assert plot.status is PlotStatus.pending
    # Timestamps populated.
    assert client.created_at is not None


def test_cascade_delete_removes_children(session) -> None:  # noqa: ANN001
    client = Client(name="Chocolatier BV", side=ClientSide.importer_eu, country="NL")
    supplier = Supplier(name="Coop Y", country="CI", client=client)
    supplier.plots.append(
        Plot(
            geometry_geojson='{"type":"Point","coordinates":[-5.5,7.5]}',
            geometry_type="Point",
        )
    )
    session.add(client)
    session.commit()

    assert _count(session, Supplier) == 1
    assert _count(session, Plot) == 1

    session.delete(client)
    session.commit()

    assert _count(session, Supplier) == 0
    assert _count(session, Plot) == 0
