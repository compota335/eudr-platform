"""DDS assembly: turn a verified shipment into an internal Due Diligence record.

Under Regulation (EU) 2023/1115, an operator placing an in-scope commodity on
the EU market files a Due Diligence Statement (DDS) declaring: who the operator
is, what the commodity is (commodity + CN code + quantity), the country of
production, and the geolocation of every plot of land the commodity came from
(a polygon for plots of 4 ha or more; a single point is allowed only under
4 ha). On submission TRACES returns a reference number and a verification
number. This module assembles that record from our own data model.

Boundary of this module (house rule: FAIL LOUD, NEVER FAKE SUCCESS):

* We assemble and PERSIST an internal DDS record. We DO NOT submit anything to
  TRACES: the exact TRACES EUDR API JSON schema is unverified (flagged in the
  domain deep dive, Section 9), so the ``reference_number`` we generate is an
  INTERNAL identifier, not a TRACES number, and ``verification_number`` /
  ``traces_reference`` stay ``None`` until a verified TRACES integration fills
  them. See ``docs/traces-dds-mapping.md`` for the field-by-field mapping.
* Assembly refuses to paper over missing or non-fileable data. Each stage
  raises a specific error rather than emitting a half-built statement:
  incomplete data, a shipment whose CN code is out of EUDR scope, or a shipment
  containing at least one RED plot (we never file over red).
* :func:`build_dds_payload` is a pure function of its inputs — no database, no
  wall clock, no randomness — so the assembled document is deterministic and
  unit-testable. The database work and the one non-deterministic element (the
  reference-number suffix) live only in :func:`assemble_dds`.

The caller owns the transaction: :func:`assemble_dds` does ``session.add`` and
``session.flush`` so the new row gets its primary key, but it never commits.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.dds import DDS
from app.models.enums import DDSStatus, RiskLevel
from app.models.plot import Plot
from app.models.shipment import Shipment
from app.services.risk import RULESET_VERSION
from app.services.scope import ScopeResult, check_scope

# Internal DDS payload schema tag. "unverified" marks that the shape is designed
# to be mappable to the TRACES DDS but has NOT been reconciled against a
# published TRACES API schema (see the module docstring and the mapping doc).
SCHEMA_VERSION = "internal-0.1-unverified"

# EUDR deforestation-free cutoff: commodities produced on land deforested after
# this date are non-compliant (Regulation (EU) 2023/1115, Article 2).
CUTOFF_DATE = "2020-12-31"

# EUDR geolocation rule: a plot of this size or larger MUST be described by a
# polygon; a single point is permitted only for a smaller plot (Article 9(1)(d)).
POLYGON_REQUIRED_AREA_HA = 4.0

# Geometry types that satisfy the polygon requirement for a >= 4 ha plot.
_POLYGON_TYPES: frozenset[str] = frozenset({"Polygon", "MultiPolygon"})


# --------------------------------------------------------------------------- #
# Errors — assembly fails loud, never emits a half-built statement.            #
# --------------------------------------------------------------------------- #
class DDSAssemblyError(ValueError):
    """Base class for all DDS-assembly failures."""


class DDSIncompleteError(DDSAssemblyError):
    """Required data for filing is missing or invalid.

    Carries the full list of human-readable problems so the caller can show the
    operator everything to fix in one pass rather than one error at a time.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        joined = "; ".join(self.problems)
        super().__init__(f"DDS cannot be assembled, {len(self.problems)} problem(s): {joined}")


class DDSOutOfScopeError(DDSAssemblyError):
    """The shipment's CN code is not in EUDR scope, so no DDS is due."""


class DDSBlockedError(DDSAssemblyError):
    """At least one plot is RED; a DDS is never filed over a red plot.

    Carries the ids of every RED plot so the caller can point the operator at
    the parcels that must be resolved before the shipment can proceed.
    """

    def __init__(self, red_plot_ids: Sequence[int]) -> None:
        self.red_plot_ids: tuple[int, ...] = tuple(red_plot_ids)
        ids = ", ".join(str(pid) for pid in self.red_plot_ids)
        super().__init__(
            f"DDS blocked: {len(self.red_plot_ids)} plot(s) assessed RED "
            f"(ids: {ids}); a statement is never filed over a red plot."
        )


# --------------------------------------------------------------------------- #
# Pure payload builder — no DB, no clock, no randomness.                        #
# --------------------------------------------------------------------------- #
def build_dds_payload(
    *,
    client: Client,
    shipment: Shipment,
    scope: ScopeResult,
    plots: Sequence[Plot],
) -> dict[str, Any]:
    """Assemble the internal DDS payload from already-validated inputs.

    Pure and deterministic: the same inputs always produce an identical dict,
    with no database access, no wall-clock reads and no randomness. All
    completeness, scope and red-plot checks are the caller's responsibility
    (see :func:`assemble_dds`); this function trusts its inputs and only shapes
    them into the payload structure.

    The plot geometry is embedded as a parsed GeoJSON object (via
    ``json.loads``) rather than a nested string, so the stored payload is a
    single well-formed JSON document.
    """
    geolocation: list[dict[str, Any]] = []
    for plot in plots:
        # The builder trusts validated inputs (see the docstring); an unassessed
        # plot is a caller contract breach, so fail loud rather than emit null.
        if plot.risk_level is None:
            raise DDSIncompleteError(
                [f"{_plot_label(plot)}: not yet assessed (risk_level is missing)"]
            )
        geolocation.append(
            {
                "external_ref": plot.external_ref,
                "supplier_name": plot.supplier.name,
                "country": plot.country,
                "commodity": plot.commodity.value if plot.commodity is not None else None,
                "area_ha": plot.area_ha,
                "geometry_type": plot.geometry_type,
                "geometry": json.loads(plot.geometry_geojson),
                "centroid": {"lon": plot.centroid_lon, "lat": plot.centroid_lat},
                "risk_level": plot.risk_level.value,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "operator": {
            "name": client.name,
            "country": client.country,
            "eori": client.eori,
            "contact_email": client.contact_email,
        },
        "commodity": {
            "commodity": shipment.commodity.value if shipment.commodity is not None else None,
            "cn_code": shipment.cn_code,
            "quantity_kg": shipment.quantity_kg,
            "country_of_production": shipment.country_of_production,
        },
        "scope": {
            "in_scope": scope.in_scope,
            "matched_cn": scope.matched_cn,
            "country_risk": scope.country_risk.value if scope.country_risk is not None else None,
            "cn_table_version": scope.cn_table_version,
            "country_table_version": scope.country_table_version,
        },
        "geolocation": geolocation,
        "deforestation_free": {
            "cutoff_date": CUTOFF_DATE,
            "ruleset_version": RULESET_VERSION,
            "assertion": (
                "No plot shows post-2020 deforestation per the "
                "convergence-of-evidence ruleset."
            ),
        },
    }


# --------------------------------------------------------------------------- #
# Internal completeness helpers                                                #
# --------------------------------------------------------------------------- #
def _operator_problems(shipment: Shipment) -> list[str]:
    """Problems with operator identity (the DDS declarant)."""
    client = shipment.client
    if client is None:
        return ["operator identity missing: shipment has no client"]
    if not (client.name and client.name.strip()):
        return ["operator identity missing: client has no name"]
    return []


def _commodity_problems(shipment: Shipment) -> list[str]:
    """Problems with the mandatory commodity line of the DDS."""
    problems: list[str] = []
    if shipment.commodity is None:
        problems.append("shipment commodity is missing")
    if not (shipment.cn_code and shipment.cn_code.strip()):
        problems.append("shipment CN code is missing")
    if shipment.quantity_kg is None:
        problems.append("shipment quantity_kg is missing")
    if not (shipment.country_of_production and shipment.country_of_production.strip()):
        problems.append("shipment country_of_production is missing")
    return problems


def _plot_problems(plot: Plot, label: str) -> list[str]:
    """Per-plot completeness problems (geometry, centroid, risk assessment)."""
    problems: list[str] = []
    if not (plot.geometry_geojson and plot.geometry_geojson.strip()):
        problems.append(f"{label}: geometry_geojson is missing")
    if not (plot.geometry_type and plot.geometry_type.strip()):
        problems.append(f"{label}: geometry_type is missing")
    if plot.area_ha is None:
        problems.append(f"{label}: area_ha is missing")
    if plot.centroid_lon is None:
        problems.append(f"{label}: centroid_lon is missing")
    if plot.centroid_lat is None:
        problems.append(f"{label}: centroid_lat is missing")
    if plot.risk_level is None:
        problems.append(f"{label}: not yet assessed (risk_level is missing)")
    return problems


def _plot_label(plot: Plot) -> str:
    """A stable human label for a plot in problem messages."""
    if plot.id is not None:
        return f"plot {plot.id}"
    if plot.external_ref:
        return f"plot {plot.external_ref!r}"
    return "plot"


# --------------------------------------------------------------------------- #
# Orchestrator — does the DB work; the CALLER owns the commit.                  #
# --------------------------------------------------------------------------- #
def assemble_dds(session: Session, shipment: Shipment) -> DDS:
    """Assemble and persist an internal DDS record for ``shipment``.

    Runs the filing gates in order, each failing loud:

    1. **Completeness** — operator identity, the commodity line, at least one
       plot, and every plot's geometry/centroid/risk assessment must be present.
       Any gap raises :class:`DDSIncompleteError` with the full problem list.
    2. **Scope** — the CN code is re-checked against Annex I. A shipment whose
       code is out of scope raises :class:`DDSOutOfScopeError`.
    3. **Red block** — any plot assessed RED raises :class:`DDSBlockedError`
       carrying the offending plot ids; we never file over a red plot.
    4. **Geolocation rule** — a plot of 4 ha or more must be a polygon (a point
       is allowed only under 4 ha). Any violation raises
       :class:`DDSIncompleteError`.

    Then it builds the payload, generates an INTERNAL reference number, and
    persists a :class:`DDS` row in status ``assembled`` (``verification_number``
    and ``traces_reference`` stay ``None``: those are TRACES-only and TRACES is
    not integrated). The row is ``add``-ed and ``flush``-ed so it gets its
    primary key, but this function never commits — the caller owns the
    transaction.
    """
    # --- 1. Completeness ------------------------------------------------------
    problems: list[str] = []
    problems += _operator_problems(shipment)
    problems += _commodity_problems(shipment)

    plots = list(shipment.plots)
    if not plots:
        problems.append("shipment has no plots")
    for plot in plots:
        problems += _plot_problems(plot, _plot_label(plot))

    if problems:
        raise DDSIncompleteError(problems)

    # --- 2. Scope (CN code is authoritative) ----------------------------------
    scope = check_scope(
        cn_code=shipment.cn_code,
        origin_country=shipment.country_of_production,
    )
    if not scope.in_scope:
        raise DDSOutOfScopeError(
            f"shipment CN code {shipment.cn_code!r} is not in EUDR scope "
            "(no matching Annex I heading); no Due Diligence Statement is due."
        )

    # --- 3. Red block (never file over a red plot) ----------------------------
    red_plot_ids = [plot.id for plot in plots if plot.risk_level == RiskLevel.red]
    if red_plot_ids:
        raise DDSBlockedError(red_plot_ids)

    # --- 4. Geolocation rule (polygon required at/above 4 ha) -----------------
    # Completeness (stage 1) already guaranteed every area_ha is present; the
    # explicit None check keeps the invariant loud rather than silently skipping.
    geo_problems: list[str] = []
    for plot in plots:
        area_ha = plot.area_ha
        if area_ha is None:
            raise DDSIncompleteError([f"{_plot_label(plot)}: area_ha is missing"])
        if area_ha >= POLYGON_REQUIRED_AREA_HA and plot.geometry_type not in _POLYGON_TYPES:
            geo_problems.append(
                f"{_plot_label(plot)}: area {area_ha} ha requires a Polygon or "
                f"MultiPolygon (a point is only allowed under {POLYGON_REQUIRED_AREA_HA} ha), "
                f"got {plot.geometry_type!r}"
            )
    if geo_problems:
        raise DDSIncompleteError(geo_problems)

    # --- 5. Build, reference, persist -----------------------------------------
    payload = build_dds_payload(
        client=shipment.client,
        shipment=shipment,
        scope=scope,
        plots=plots,
    )
    reference_number = (
        f"EUDR-DDS-{shipment.client_id}-{shipment.id}-{secrets.token_hex(4).upper()}"
    )
    payload["reference_number"] = reference_number

    dds = DDS(
        client_id=shipment.client_id,
        shipment_id=shipment.id,
        reference_number=reference_number,
        status=DDSStatus.assembled,
        payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )
    session.add(dds)
    session.flush()
    return dds
