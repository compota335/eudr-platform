"""Public plot-checker: the lead magnet.

A visitor submits a plot geometry (uploaded file or pasted text), the
deterministic pipeline returns a green/amber/red deforestation verdict, and the
verdict is persisted as an anonymous :class:`PlotCheck`. The visitor may then
trade an email address for a PDF export of the verdict.

Pipeline (each stage fails loud; see the house rules — no fabricated verdict is
ever returned when the provider is unavailable):

    parse_input -> validate_plot -> analyze_geometry (provider) -> assess

The deforestation provider is injected through the ``get_risk_provider``
dependency so tests can supply a canned :class:`PlotEvidence` without a network
call or an API key. Production resolves to the real Whisp client, which raises
``RiskProviderNotConfigured`` when no key is set.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.orm import Session

from app.db import get_session
from app.geo.parse import parse_input
from app.geo.schemas import (
    GeoParseError,
    GeoValidationError,
    PlotEvidence,
    RiskProviderError,
    RiskProviderNotConfigured,
    RiskResult,
    ValidatedPlot,
)
from app.geo.validate import validate_plot
from app.models.enums import Commodity
from app.models.plot_check import PlotCheck
from app.services import whisp
from app.services.pdf import render_plot_check_pdf
from app.services.risk import assess
from app.services.serialization import deserialize_risk_result, serialize_risk_result
from app.templating import templates

router = APIRouter()

# A provider is any callable with the shape of ``whisp.analyze_geometry``:
# it takes a geometry dict plus an optional external ref and returns evidence.
RiskProvider = Callable[..., PlotEvidence]


def get_risk_provider() -> RiskProvider:
    """Return the deforestation-evidence provider.

    Defaults to the real Whisp client (fail-loud on a missing key). Overridden
    in tests via ``app.dependency_overrides`` with a fake returning canned
    evidence, which is how the endpoint is exercised without a network or a key.
    """
    return whisp.analyze_geometry


# --------------------------------------------------------------------------- #
# Pages                                                                        #
# --------------------------------------------------------------------------- #
@router.get("/plot-checker", response_class=HTMLResponse)
def plot_checker_page(request: Request) -> HTMLResponse:
    """Render the public plot-checker page."""
    return templates.TemplateResponse(
        request,
        "plot_checker.html",
        {"title": "Plot checker"},
    )


# --------------------------------------------------------------------------- #
# API: run a check                                                             #
# --------------------------------------------------------------------------- #
@router.post("/api/check-plot")
async def check_plot(
    request: Request,
    file: UploadFile | None = None,
    geojson: str | None = Form(default=None),
    commodity: str | None = Form(default=None),
    country: str | None = Form(default=None),
    declared_volume_t: float | None = Form(default=None),
    session: Session = Depends(get_session),
    provider: RiskProvider = Depends(get_risk_provider),
) -> Response:
    """Run the plot check and return a verdict (HTMX fragment or JSON).

    Accepts EITHER an uploaded ``file`` OR pasted ``geojson`` text. The optional
    ``commodity`` / ``country`` / ``declared_volume_t`` refine validation and the
    rationale. When more than one geometry is submitted only the FIRST is
    assessed, and a DISCLOSED note says so.
    """
    is_htmx = request.headers.get("HX-Request") is not None

    try:
        data, filename, content_type = await _read_input(file, geojson)
        commodity_enum = _parse_commodity(commodity)
        country_code = _normalize_country(country)

        plots = parse_input(data, filename=filename, content_type=content_type)
        notes: list[str] = []
        if len(plots) > 1:
            notes.append(
                f"{len(plots)} geometries detected; assessing the first only."
            )
        first = plots[0]

        validated = validate_plot(
            first,
            declared_country=country_code,
            commodity=commodity_enum,
            declared_volume_t=declared_volume_t,
        )
        # The provider is a SYNC blocking call (one Whisp request, up to ~60s).
        # This endpoint is ``async def`` (it awaits the upload), so the call must
        # move off the event loop or it stalls every request.
        evidence = await run_in_threadpool(
            provider, validated.geometry, external_ref=validated.external_ref
        )
        result = assess(evidence, commodity=commodity_enum)
    except _InputError as exc:
        return _error_response(is_htmx, request, exc.message, exc.status_code)
    except (GeoParseError, GeoValidationError) as exc:
        return _error_response(is_htmx, request, str(exc), 400)
    except RiskProviderNotConfigured:
        return _error_response(
            is_htmx,
            request,
            "The deforestation data provider is not configured; no risk "
            "assessment can be produced. No result was stored.",
            503,
        )
    except RiskProviderError as exc:
        return _error_response(
            is_htmx,
            request,
            f"The deforestation data provider failed: {exc}",
            502,
        )

    check = _persist_check(
        session,
        validated=validated,
        result=result,
        commodity=commodity_enum,
        country=country_code,
    )

    if is_htmx:
        return templates.TemplateResponse(
            request,
            "_plot_result.html",
            _result_context(check, result, validated, notes),
        )
    return JSONResponse(_result_json(check, result, validated, notes))


# --------------------------------------------------------------------------- #
# API: capture email (unlocks the PDF)                                         #
# --------------------------------------------------------------------------- #
@router.post("/api/check-plot/{token}/email")
def capture_email(
    request: Request,
    token: str,
    email: str = Form(...),
    session: Session = Depends(get_session),
) -> Response:
    """Attach an email to a check, unlocking the gated PDF download."""
    is_htmx = request.headers.get("HX-Request") is not None

    normalized = email.strip()
    if not _looks_like_email(normalized):
        return _error_response(
            is_htmx, request, "Enter a valid email address.", 400
        )

    check = session.query(PlotCheck).filter(PlotCheck.token == token).one_or_none()
    if check is None:
        return _error_response(is_htmx, request, "Unknown check token.", 404)

    check.email = normalized
    session.commit()

    if is_htmx:
        return templates.TemplateResponse(
            request,
            "_plot_email_ok.html",
            {"token": token},
        )
    return JSONResponse(
        {"token": token, "email_captured": True, "pdf_url": _pdf_url(token)}
    )


# --------------------------------------------------------------------------- #
# API: gated PDF download                                                      #
# --------------------------------------------------------------------------- #
@router.get("/api/check-plot/{token}/pdf")
def download_pdf(
    token: str,
    session: Session = Depends(get_session),
) -> Response:
    """Return the verdict PDF, gated behind email capture.

    Raises the failure conditions as clear HTTP errors: 404 when the token is
    unknown, 403 when no email has been captured yet.
    """
    check = session.query(PlotCheck).filter(PlotCheck.token == token).one_or_none()
    if check is None:
        return _plain_error(404, "Unknown check token.")
    if not check.email:
        return _plain_error(
            403, "An email address is required to download the report."
        )
    if not check.result_json:
        return _plain_error(500, "This check has no stored result to render.")

    result = deserialize_risk_result(check.result_json)
    pdf_bytes = render_plot_check_pdf(check, result)
    headers = {
        "Content-Disposition": f'attachment; filename="eudr-plot-{token}.pdf"'
    }
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


# --------------------------------------------------------------------------- #
# Persistence                                                                  #
# --------------------------------------------------------------------------- #
def _persist_check(
    session: Session,
    *,
    validated: ValidatedPlot,
    result: RiskResult,
    commodity: Commodity | None,
    country: str | None,
) -> PlotCheck:
    """Store the verdict as an anonymous ``PlotCheck`` and return the row."""
    check = PlotCheck(
        token=secrets.token_urlsafe(32),
        source_format=validated.source_format,
        geometry_geojson=_geometry_json(validated.geometry),
        geometry_type=validated.geometry_type,
        area_ha=validated.area_ha,
        centroid_lon=validated.centroid_lon,
        centroid_lat=validated.centroid_lat,
        commodity=commodity,
        country=country,
        risk_level=result.level,
        ruleset_version=result.ruleset_version,
        provider=result.evidence.provider or None,
        result_json=serialize_risk_result(result),
        dataset_versions=_dataset_versions_json(result.evidence),
    )
    session.add(check)
    session.commit()
    session.refresh(check)
    return check


# --------------------------------------------------------------------------- #
# Response shaping                                                             #
# --------------------------------------------------------------------------- #
def _result_context(
    check: PlotCheck,
    result: RiskResult,
    validated: ValidatedPlot,
    notes: list[str],
) -> dict[str, object]:
    """Template context shared by the HTMX result fragment."""
    return {
        "token": check.token,
        "level": result.level.value,
        "verdict": result.level.value.upper(),
        "rationale": list(result.rationale),
        "converging_families": list(result.converging_families),
        "signals_in_plot": result.signals_in_plot,
        "signals_in_buffer": result.signals_in_buffer,
        "forest_2020_present": result.forest_2020_present,
        "ruleset_version": result.ruleset_version,
        "provider": result.evidence.provider,
        "geometry_type": validated.geometry_type,
        "area_ha": validated.area_ha,
        "centroid_lon": validated.centroid_lon,
        "centroid_lat": validated.centroid_lat,
        "warnings": list(validated.warnings),
        "notes": notes,
        "email_url": f"/api/check-plot/{check.token}/email",
    }


def _result_json(
    check: PlotCheck,
    result: RiskResult,
    validated: ValidatedPlot,
    notes: list[str],
) -> dict[str, object]:
    """JSON body returned to non-HTMX callers of ``/api/check-plot``."""
    return {
        "token": check.token,
        "level": result.level.value,
        "ruleset_version": result.ruleset_version,
        "provider": result.evidence.provider,
        "forest_2020_present": result.forest_2020_present,
        "signals_in_plot": result.signals_in_plot,
        "signals_in_buffer": result.signals_in_buffer,
        "converging_families": list(result.converging_families),
        "rationale": list(result.rationale),
        "geometry": {
            "type": validated.geometry_type,
            "area_ha": validated.area_ha,
            "centroid_lon": validated.centroid_lon,
            "centroid_lat": validated.centroid_lat,
            "source_format": validated.source_format,
        },
        "warnings": list(validated.warnings),
        "notes": notes,
        "pdf_url": _pdf_url(check.token),
        "email_required_for_pdf": True,
    }


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #
class _InputError(Exception):
    """A request-shaping error carrying the HTTP status to return."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _error_response(
    is_htmx: bool, request: Request, message: str, status_code: int
) -> Response:
    """Render a fail-loud error as an HTMX fragment or a JSON body.

    The message is caller-safe (no secrets, no tracebacks); the pipeline builds
    it from validation reasons or a fixed provider-unavailable string.
    """
    if is_htmx:
        return templates.TemplateResponse(
            request,
            "_plot_error.html",
            {"message": message},
            status_code=status_code,
        )
    return JSONResponse({"error": message}, status_code=status_code)


def _plain_error(status_code: int, message: str) -> JSONResponse:
    """A JSON error for endpoints that are not HTMX-aware (the PDF download)."""
    return JSONResponse({"error": message}, status_code=status_code)


# --------------------------------------------------------------------------- #
# Input helpers                                                                #
# --------------------------------------------------------------------------- #
async def _read_input(
    file: UploadFile | None, geojson: str | None
) -> tuple[bytes, str | None, str | None]:
    """Return ``(data, filename, content_type)`` from the file or pasted text.

    Exactly one source is required. A file with no bytes, or pasted text that is
    blank, is treated as "not provided".
    """
    if file is not None and file.filename:
        data = await file.read()
        if data:
            return data, file.filename, file.content_type
    if geojson is not None and geojson.strip():
        return geojson.encode("utf-8"), None, None
    raise _InputError(
        "Provide a geometry: upload a file or paste GeoJSON/coordinates.", 400
    )


def _parse_commodity(value: str | None) -> Commodity | None:
    """Map an optional form value to a ``Commodity``, failing loud on garbage."""
    if value is None or not value.strip():
        return None
    try:
        return Commodity(value.strip())
    except ValueError as exc:
        raise _InputError(
            f"Unknown commodity {value!r}; expected one of "
            f"{', '.join(c.value for c in Commodity)}.",
            400,
        ) from exc


def _normalize_country(value: str | None) -> str | None:
    """Normalize an optional 2-letter country code, failing loud if malformed."""
    if value is None or not value.strip():
        return None
    code = value.strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise _InputError(
            f"Invalid country code {value!r}; expected a 2-letter ISO code.", 400
        )
    return code


def _looks_like_email(value: str) -> bool:
    """Deterministic email shape check: one ``@``, a dotted domain, no spaces."""
    if not value or any(ch.isspace() for ch in value):
        return False
    if value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    if not local or not domain:
        return False
    return "." in domain and not domain.startswith(".") and not domain.endswith(".")


def _geometry_json(geometry: dict[str, object]) -> str:
    return json.dumps(geometry, separators=(",", ":"))


def _dataset_versions_json(evidence: PlotEvidence) -> str:
    return json.dumps(evidence.dataset_versions, separators=(",", ":"))


def _pdf_url(token: str) -> str:
    return f"/api/check-plot/{token}/pdf"
