"""Supplier workspace: the per-supplier plot inventory and risk assessment.

Server-rendered detail page for one supplier showing its facts and its plots,
with two mutations following Post/Redirect/Get (303):

* bulk plot import from an uploaded geometry file OR pasted GeoJSON text,
  reusing the plot-checker parse/validate pipeline. The import is atomic:
  every geometry is validated BEFORE any row is created, so a bad file never
  leaves a partial import behind;
* a per-plot deforestation assessment through the injected risk provider
  (``get_risk_provider``, shared with the plot-checker so tests can override
  it). Each assessment appends an ``Evidence`` row — evidence is append-only,
  a re-assess adds a new row and never mutates an old one.

Fail-loud per the house rules: an unknown supplier or plot id is a 404, bad
input (no geometry, unparseable geometry, unknown enum, malformed country) is
a 400, an unconfigured provider is a 503 and a failing provider is a 502. No
verdict is ever fabricated and nothing is stored on failure.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.geo.parse import parse_input
from app.geo.schemas import (
    GeoParseError,
    GeoValidationError,
    RiskProviderError,
    RiskProviderNotConfigured,
    ValidatedPlot,
)
from app.geo.validate import validate_plot
from app.models.enums import Commodity, PlotStatus
from app.models.evidence import Evidence
from app.models.plot import Plot
from app.models.supplier import Supplier
from app.routers._forms import parse_optional_country, parse_optional_enum
from app.routers.plot_checker import RiskProvider, get_risk_provider
from app.services.risk import assess
from app.services.serialization import serialize_risk_result
from app.templating import templates

router = APIRouter()

# The pipeline stage recorded on every deforestation Evidence row.
_DEFORESTATION_STAGE = "deforestation_analysis"


# --------------------------------------------------------------------------- #
# One supplier: detail                                                         #
# --------------------------------------------------------------------------- #
@router.get("/suppliers/{supplier_id}", response_class=HTMLResponse)
def supplier_detail_page(
    request: Request,
    supplier_id: int,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Render one supplier with its plot inventory and the add-plots form."""
    supplier = _get_supplier(session, supplier_id)
    return templates.TemplateResponse(
        request,
        "supplier_detail.html",
        {
            "title": supplier.name,
            "supplier": supplier,
            "commodities": list(Commodity),
        },
    )


# --------------------------------------------------------------------------- #
# Bulk plot import (atomic: validate everything, then create everything)       #
# --------------------------------------------------------------------------- #
@router.post("/suppliers/{supplier_id}/plots")
async def create_plots(
    supplier_id: int,
    file: UploadFile | None = None,
    geojson: str | None = Form(default=None),
    commodity: str | None = Form(default=None),
    country: str | None = Form(default=None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Import one plot per geometry from a file or pasted text, then 303 back.

    Every geometry is parsed and validated BEFORE any row is added: a single
    bad geometry fails the whole request with a 400 and creates NOTHING.
    """
    supplier = _get_supplier(session, supplier_id)
    data, filename, content_type = await _read_input(file, geojson)
    commodity_enum = parse_optional_enum(Commodity, commodity, "commodity")
    country_code = parse_optional_country(country)

    # Validate ALL geometries first so a failure creates no partial import.
    validated: list[tuple[str | None, ValidatedPlot]] = []
    try:
        for normalized in parse_input(data, filename=filename, content_type=content_type):
            validated.append(
                (
                    normalized.external_ref,
                    validate_plot(
                        normalized,
                        declared_country=country_code,
                        commodity=commodity_enum,
                    ),
                )
            )
    except (GeoParseError, GeoValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session.add_all(
        Plot(
            supplier_id=supplier.id,
            external_ref=external_ref,
            commodity=commodity_enum,
            country=country_code,
            geometry_geojson=json.dumps(plot.geometry, separators=(",", ":")),
            geometry_type=plot.geometry_type,
            area_ha=plot.area_ha,
            centroid_lon=plot.centroid_lon,
            centroid_lat=plot.centroid_lat,
            status=PlotStatus.valid,
        )
        for external_ref, plot in validated
    )
    session.commit()
    return RedirectResponse(f"/suppliers/{supplier.id}", status_code=303)


# --------------------------------------------------------------------------- #
# Per-plot deforestation assessment (Evidence is append-only)                  #
# --------------------------------------------------------------------------- #
# Deliberately a SYNC ``def``: FastAPI runs it in a threadpool. The provider
# call BLOCKS for up to minutes (Whisp submit -> poll -> fetch), so making this
# ``async`` would stall the event loop for every other request.
@router.post("/plots/{plot_id}/assess")
def assess_plot(
    plot_id: int,
    session: Session = Depends(get_session),
    provider: RiskProvider = Depends(get_risk_provider),
) -> RedirectResponse:
    """Assess one plot, store the verdict plus an Evidence row, then 303 back."""
    plot = session.get(Plot, plot_id)
    if plot is None:
        raise HTTPException(status_code=404, detail=f"Plot {plot_id} not found.")

    geometry: dict[str, Any] = json.loads(plot.geometry_geojson)
    try:
        evidence = provider(geometry, external_ref=plot.external_ref or None)
        result = assess(evidence, commodity=plot.commodity)
    except RiskProviderNotConfigured as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "The deforestation data provider is not configured; "
                "no assessment was stored."
            ),
        ) from exc
    except RiskProviderError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The deforestation data provider failed: {exc}",
        ) from exc

    plot.risk_level = result.level
    session.add(
        Evidence(
            plot_id=plot.id,
            stage=_DEFORESTATION_STAGE,
            provider=result.evidence.provider or None,
            risk_level=result.level,
            data_json=serialize_risk_result(result),
            dataset_versions=json.dumps(
                result.evidence.dataset_versions, separators=(",", ":")
            ),
        )
    )
    session.commit()
    return RedirectResponse(f"/suppliers/{plot.supplier_id}", status_code=303)


# --------------------------------------------------------------------------- #
# Helpers (fail-loud parsing / lookup)                                         #
# --------------------------------------------------------------------------- #
def _get_supplier(session: Session, supplier_id: int) -> Supplier:
    """Return the supplier or raise a 404."""
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(
            status_code=404, detail=f"Supplier {supplier_id} not found."
        )
    return supplier


async def _read_input(
    file: UploadFile | None, geojson: str | None
) -> tuple[bytes, str | None, str | None]:
    """Return ``(data, filename, content_type)`` from the file or pasted text.

    Same contract as the plot-checker: a file with bytes wins, else non-blank
    pasted text; a file with no bytes or blank text counts as "not provided",
    and providing neither is a 400.
    """
    if file is not None and file.filename:
        data = await file.read()
        if data:
            return data, file.filename, file.content_type
    if geojson is not None and geojson.strip():
        return geojson.encode("utf-8"), None, None
    raise HTTPException(
        status_code=400,
        detail="Provide a geometry: upload a file or paste GeoJSON/coordinates.",
    )
