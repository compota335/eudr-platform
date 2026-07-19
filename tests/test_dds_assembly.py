"""Tests for the DDS assembly service (internal Due Diligence Statement).

These exercise the filing gates in :mod:`app.services.dds_assembly`: the pure
payload builder, the happy path, and each fail-loud refusal (incomplete data,
out-of-scope CN code, a RED plot, and the EUDR polygon-at-4-ha geolocation
rule). ORM fixtures are built persisted via the shared in-memory ``session``.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.dds import DDS
from app.models.enums import ClientSide, Commodity, DDSStatus, RiskLevel
from app.models.plot import Plot
from app.models.shipment import Shipment
from app.models.supplier import Supplier
from app.services.dds_assembly import (
    DDSBlockedError,
    DDSIncompleteError,
    DDSOutOfScopeError,
    assemble_dds,
    build_dds_payload,
)
from app.services.risk import RULESET_VERSION
from app.services.scope import check_scope

# A valid Annex I heading (coffee) used consistently for in-scope shipments, and
# a clearly non-Annex-I code (8471 = data-processing machines) for out of scope.
IN_SCOPE_CN = "0901"
OUT_OF_SCOPE_CN = "9999.99"

# Real, small, valid WGS84 GeoJSON geometries. The polygon is a tiny closed ring
# near Sao Paulo; the point sits at its rough centroid.
POLYGON_GEOJSON = (
    '{"type":"Polygon","coordinates":'
    "[[[-46.60,-23.50],[-46.59,-23.50],[-46.59,-23.49],[-46.60,-23.49],[-46.60,-23.50]]]}"
)
POINT_GEOJSON = '{"type":"Point","coordinates":[-46.595,-23.495]}'


def _make_shipment(
    session: Session,
    *,
    cn_code: str | None = IN_SCOPE_CN,
    commodity: Commodity | None = Commodity.coffee,
    quantity_kg: float | None = 12_000.0,
    country_of_production: str | None = "BR",
    plot_specs: list[dict] | None = None,
) -> Shipment:
    """Persist a Client -> Supplier -> Plot(s) -> Shipment and return the shipment.

    ``plot_specs`` is a list of per-plot overrides; each dict may set
    ``geometry_geojson``, ``geometry_type``, ``area_ha``, ``centroid_lon``,
    ``centroid_lat``, ``risk_level`` and ``external_ref``. When omitted, one
    small green Polygon plot is created. Any field left out falls back to a
    sensible in-scope, fileable default.
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
        plot_specs = [{}]

    plots: list[Plot] = []
    for index, spec in enumerate(plot_specs):
        plot = Plot(
            supplier=supplier,
            external_ref=spec.get("external_ref", f"parcel-{index}"),
            commodity=spec.get("commodity", Commodity.coffee),
            country=spec.get("country", "BR"),
            geometry_geojson=spec.get("geometry_geojson", POLYGON_GEOJSON),
            geometry_type=spec.get("geometry_type", "Polygon"),
            area_ha=spec.get("area_ha", 1.5),
            centroid_lon=spec.get("centroid_lon", -46.595),
            centroid_lat=spec.get("centroid_lat", -23.495),
            risk_level=spec.get("risk_level", RiskLevel.green),
        )
        plots.append(plot)

    shipment = Shipment(
        client=client,
        reference="SHIP-001",
        commodity=commodity,
        cn_code=cn_code,
        quantity_kg=quantity_kg,
        country_of_production=country_of_production,
        plots=plots,
    )
    session.add(client)
    session.add(shipment)
    session.commit()
    return shipment


def _dds_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(DDS)) or 0


# --------------------------------------------------------------------------- #
# Happy path                                                                    #
# --------------------------------------------------------------------------- #
def test_assemble_happy_path_returns_assembled_dds(session: Session) -> None:
    shipment = _make_shipment(session)

    dds = assemble_dds(session, shipment)

    assert isinstance(dds, DDS)
    assert dds.id is not None  # flushed -> has a primary key
    assert dds.status is DDSStatus.assembled
    assert dds.client_id == shipment.client_id
    assert dds.shipment_id == shipment.id
    assert dds.reference_number is not None
    assert dds.reference_number.startswith("EUDR-DDS-")
    # TRACES-only fields stay empty: no submission is performed.
    assert dds.verification_number is None
    assert dds.traces_reference is None

    payload = json.loads(dds.payload_json)
    assert set(payload) >= {
        "schema_version",
        "operator",
        "commodity",
        "scope",
        "geolocation",
        "deforestation_free",
        "reference_number",
    }
    assert payload["reference_number"] == dds.reference_number
    assert payload["operator"]["name"] == "Acme Coffee GmbH"
    assert payload["operator"]["eori"] == "DE1234567890123"
    assert payload["commodity"]["commodity"] == "coffee"
    assert payload["commodity"]["cn_code"] == IN_SCOPE_CN
    assert payload["commodity"]["quantity_kg"] == 12_000.0
    assert payload["scope"]["in_scope"] is True
    assert payload["scope"]["matched_cn"] == "0901"
    assert payload["deforestation_free"]["cutoff_date"] == "2020-12-31"
    assert payload["deforestation_free"]["ruleset_version"] == RULESET_VERSION
    assert len(payload["geolocation"]) == 1
    geo = payload["geolocation"][0]
    assert geo["supplier_name"] == "Cooperativa X"
    assert geo["risk_level"] == "green"
    # Geometry embedded as a parsed object, not a nested string.
    assert geo["geometry"] == json.loads(POLYGON_GEOJSON)
    assert geo["centroid"] == {"lon": -46.595, "lat": -23.495}


def test_assemble_persists_exactly_one_row(session: Session) -> None:
    shipment = _make_shipment(session)
    assert _dds_count(session) == 0
    assemble_dds(session, shipment)
    assert _dds_count(session) == 1


# --------------------------------------------------------------------------- #
# Pure builder determinism                                                      #
# --------------------------------------------------------------------------- #
def test_build_dds_payload_is_deterministic(session: Session) -> None:
    shipment = _make_shipment(session)
    scope = check_scope(cn_code=shipment.cn_code, origin_country=shipment.country_of_production)
    plots = list(shipment.plots)

    first = build_dds_payload(
        client=shipment.client, shipment=shipment, scope=scope, plots=plots
    )
    second = build_dds_payload(
        client=shipment.client, shipment=shipment, scope=scope, plots=plots
    )
    # Same inputs -> identical dict (no clock, no randomness in the builder).
    assert first == second
    # The reference number is NOT part of the pure builder output.
    assert "reference_number" not in first


# --------------------------------------------------------------------------- #
# Red block                                                                     #
# --------------------------------------------------------------------------- #
def test_red_plot_blocks_assembly(session: Session) -> None:
    shipment = _make_shipment(
        session,
        plot_specs=[
            {"risk_level": RiskLevel.green},
            {"risk_level": RiskLevel.red},
        ],
    )
    red_plot = next(p for p in shipment.plots if p.risk_level is RiskLevel.red)

    try:
        assemble_dds(session, shipment)
    except DDSBlockedError as exc:
        assert red_plot.id in exc.red_plot_ids
    else:
        raise AssertionError("expected DDSBlockedError for a red plot")

    # Nothing was persisted: a blocked shipment yields no DDS row.
    assert _dds_count(session) == 0


# --------------------------------------------------------------------------- #
# Incomplete: unassessed plot                                                   #
# --------------------------------------------------------------------------- #
def test_unassessed_plot_is_incomplete(session: Session) -> None:
    shipment = _make_shipment(session, plot_specs=[{"risk_level": None}])

    try:
        assemble_dds(session, shipment)
    except DDSIncompleteError as exc:
        assert any("assess" in problem.lower() for problem in exc.problems)
    else:
        raise AssertionError("expected DDSIncompleteError for an unassessed plot")

    assert _dds_count(session) == 0


# --------------------------------------------------------------------------- #
# Incomplete: missing shipment field                                            #
# --------------------------------------------------------------------------- #
def test_missing_cn_code_is_incomplete(session: Session) -> None:
    shipment = _make_shipment(session, cn_code=None)

    try:
        assemble_dds(session, shipment)
    except DDSIncompleteError as exc:
        assert any("cn code" in problem.lower() for problem in exc.problems)
    else:
        raise AssertionError("expected DDSIncompleteError for a missing CN code")

    assert _dds_count(session) == 0


# --------------------------------------------------------------------------- #
# Out of scope                                                                  #
# --------------------------------------------------------------------------- #
def test_out_of_scope_cn_code_raises(session: Session) -> None:
    # Data is otherwise complete (commodity present); the ONLY problem is that
    # the CN code is not in Annex I, so assembly must reach the scope gate.
    shipment = _make_shipment(session, cn_code=OUT_OF_SCOPE_CN)

    try:
        assemble_dds(session, shipment)
    except DDSOutOfScopeError as exc:
        # The offending CN code appears in the message for debuggability.
        assert OUT_OF_SCOPE_CN in str(exc)
    else:
        raise AssertionError("expected DDSOutOfScopeError for a non-Annex-I code")

    assert _dds_count(session) == 0


# --------------------------------------------------------------------------- #
# Geolocation rule: polygon required at/above 4 ha                              #
# --------------------------------------------------------------------------- #
def test_large_point_plot_violates_geolocation_rule(session: Session) -> None:
    shipment = _make_shipment(
        session,
        plot_specs=[
            {
                "geometry_geojson": POINT_GEOJSON,
                "geometry_type": "Point",
                "area_ha": 10.0,
                "risk_level": RiskLevel.green,
            }
        ],
    )

    try:
        assemble_dds(session, shipment)
    except DDSIncompleteError as exc:
        joined = " ".join(exc.problems).lower()
        assert "polygon" in joined
        assert "4" in joined
    else:
        raise AssertionError("expected DDSIncompleteError for a >=4 ha point plot")

    assert _dds_count(session) == 0
