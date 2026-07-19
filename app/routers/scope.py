"""Public scope-checker: is a product in EUDR scope, and what does it need?

Thin HTTP layer over the deterministic :func:`app.services.scope.check_scope`.
The CN code is authoritative for ``in_scope``; a free-text description can only
suggest a candidate commodity (see the service docstring). Empty input fails
loud as ``ScopeError`` -> HTTP 400.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.services.scope import ScopeError, ScopeResult, check_scope
from app.templating import templates

router = APIRouter()


@router.get("/scope-checker", response_class=HTMLResponse)
def scope_checker_page(request: Request) -> HTMLResponse:
    """Render the public scope-checker page."""
    return templates.TemplateResponse(
        request,
        "scope_checker.html",
        {"title": "Scope checker"},
    )


@router.post("/api/check-scope")
def check_scope_endpoint(
    request: Request,
    product_description: str | None = Form(default=None),
    cn_code: str | None = Form(default=None),
    origin_country: str | None = Form(default=None),
) -> Response:
    """Determine EUDR scope for a product line (HTMX fragment or JSON)."""
    is_htmx = request.headers.get("HX-Request") is not None
    try:
        result = check_scope(
            product_description=product_description,
            cn_code=cn_code,
            origin_country=origin_country,
        )
    except ScopeError as exc:
        message = str(exc)
        if is_htmx:
            return templates.TemplateResponse(
                request,
                "_scope_error.html",
                {"message": message},
                status_code=400,
            )
        return JSONResponse({"error": message}, status_code=400)

    if is_htmx:
        return templates.TemplateResponse(
            request,
            "_scope_result.html",
            _scope_context(result),
        )
    return JSONResponse(_scope_json(result))


# --------------------------------------------------------------------------- #
# Response shaping                                                             #
# --------------------------------------------------------------------------- #
def _scope_context(result: ScopeResult) -> dict[str, object]:
    """Template context for the HTMX scope-result fragment."""
    return {
        "in_scope": result.in_scope,
        "commodity": result.commodity.value if result.commodity else None,
        "cn_code": result.cn_code,
        "matched_cn": result.matched_cn,
        "country_code": result.country_code,
        "country_risk": result.country_risk.value if result.country_risk else None,
        "rationale": list(result.rationale),
        "required_documentation": list(result.required_documentation),
    }


def _scope_json(result: ScopeResult) -> dict[str, object]:
    """JSON body for non-HTMX callers of ``/api/check-scope``."""
    return {
        "in_scope": result.in_scope,
        "commodity": result.commodity.value if result.commodity else None,
        "cn_code": result.cn_code,
        "matched_cn": result.matched_cn,
        "country_code": result.country_code,
        "country_risk": result.country_risk.value if result.country_risk else None,
        "rationale": list(result.rationale),
        "required_documentation": list(result.required_documentation),
        "cn_table_version": result.cn_table_version,
        "country_table_version": result.country_table_version,
    }
