"""Due Diligence Statement (DDS) web + PDF layer.

Server-rendered pages over the deterministic assembly service
(:func:`app.services.dds_assembly.assemble_dds`): a list of every assembled
statement, a per-statement detail page, a downloadable PDF, and a clean
print-friendly HTML view of the same content. Assembly itself is triggered by
``POST /dds`` for a given shipment.

The assembly service owns every filing gate and fails loud: it refuses to emit a
statement over incomplete data, an out-of-scope CN code, or a shipment holding a
RED plot. This router surfaces each refusal as an explicit HTTP 400 exception
report rather than papering over it, and it commits ONLY on success — a refused
assembly leaves no partial DDS behind (the service raises before it persists).

Rendering is driven strictly by the stored ``payload_json``, parsed with
``json.loads``, never re-derived from the ORM: the payload is the assembled
record of what the statement declares. An unknown id is a 404.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models.dds import DDS
from app.models.shipment import Shipment
from app.services.dds_assembly import (
    DDSBlockedError,
    DDSIncompleteError,
    DDSOutOfScopeError,
    assemble_dds,
)
from app.services.dds_pdf import render_dds_pdf
from app.templating import templates

router = APIRouter()


# --------------------------------------------------------------------------- #
# List                                                                          #
# --------------------------------------------------------------------------- #
@router.get("/dds", response_class=HTMLResponse)
def dds_list(
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Render every assembled DDS, newest first, with an empty state."""
    statements = list(session.scalars(select(DDS).order_by(DDS.id.desc())))
    return templates.TemplateResponse(
        request,
        "dds_list.html",
        {"title": "Due Diligence Statements", "statements": statements},
    )


# --------------------------------------------------------------------------- #
# Assemble (create)                                                             #
# --------------------------------------------------------------------------- #
@router.post("/dds")
def create_dds(
    request: Request,
    shipment_id: int = Form(...),
    session: Session = Depends(get_session),
) -> Response:
    """Assemble a DDS for a shipment (Post/Redirect/Get on success).

    Loads the shipment (404 if missing) and runs assembly. On success the row is
    committed and the response redirects (303) to its detail page. Each fail-loud
    refusal from the service becomes an HTTP 400 exception report; nothing is
    committed on failure, so no partial statement is ever persisted.
    """
    shipment = session.get(Shipment, shipment_id)
    if shipment is None:
        return _error(
            request,
            title="Shipment not found",
            message=f"No shipment exists with id {shipment_id}.",
            status_code=404,
        )

    try:
        dds = assemble_dds(session, shipment)
        session.commit()
    except DDSBlockedError as exc:
        session.rollback()
        red = ", ".join(str(pid) for pid in exc.red_plot_ids)
        return _error(
            request,
            title="Blocked: RED plots",
            message=(
                "This shipment contains RED plots and cannot be assembled into a "
                "DDS. We never file over red evidence. Red plots: " + red + "."
            ),
            problems=[f"Plot {pid} assessed RED" for pid in exc.red_plot_ids],
            status_code=400,
        )
    except DDSIncompleteError as exc:
        session.rollback()
        return _error(
            request,
            title="Incomplete: cannot assemble",
            message=(
                "This shipment is missing data required to assemble a Due "
                "Diligence Statement. Resolve every item below and retry."
            ),
            problems=list(exc.problems),
            status_code=400,
        )
    except DDSOutOfScopeError as exc:
        session.rollback()
        return _error(
            request,
            title="Out of scope",
            message=str(exc),
            status_code=400,
        )

    return RedirectResponse(url=f"/dds/{dds.id}", status_code=303)


# --------------------------------------------------------------------------- #
# Detail                                                                        #
# --------------------------------------------------------------------------- #
@router.get("/dds/{dds_id}", response_class=HTMLResponse)
def dds_detail(
    dds_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Render a single DDS from its stored payload. 404 if the id is unknown."""
    dds = session.get(DDS, dds_id)
    if dds is None:
        return _error(
            request,
            title="Statement not found",
            message=f"No Due Diligence Statement exists with id {dds_id}.",
            status_code=404,
        )
    payload = _load_payload(dds)
    return templates.TemplateResponse(
        request,
        "dds_detail.html",
        {
            "title": f"DDS {dds.reference_number or dds.id}",
            "dds": dds,
            "payload": payload,
        },
    )


# --------------------------------------------------------------------------- #
# PDF (binary) + print-friendly HTML view                                       #
# --------------------------------------------------------------------------- #
@router.get("/dds/{dds_id}/pdf")
def dds_pdf(
    dds_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    """Return the DDS as a downloadable PDF. 404 if the id is unknown."""
    dds = session.get(DDS, dds_id)
    if dds is None:
        return _error(
            request,
            title="Statement not found",
            message=f"No Due Diligence Statement exists with id {dds_id}.",
            status_code=404,
        )
    filename = f"{dds.reference_number or f'dds-{dds.id}'}.pdf"
    return Response(
        content=render_dds_pdf(dds),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/dds/{dds_id}/pdf.html", response_class=HTMLResponse)
def dds_pdf_html(
    dds_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Render the standalone, print-friendly HTML view. 404 if id is unknown."""
    dds = session.get(DDS, dds_id)
    if dds is None:
        return _error(
            request,
            title="Statement not found",
            message=f"No Due Diligence Statement exists with id {dds_id}.",
            status_code=404,
        )
    payload = _load_payload(dds)
    return templates.TemplateResponse(
        request,
        "dds_pdf.html",
        {
            "title": f"DDS {dds.reference_number or dds.id}",
            "dds": dds,
            "payload": payload,
        },
    )


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #
def _load_payload(dds: DDS) -> dict[str, Any]:
    """Parse the stored payload for rendering, or fail loud.

    The payload is the assembled record; an absent or unparseable one is a
    data-integrity fault, not something to paper over with defaults.
    """
    if not dds.payload_json:
        raise ValueError(f"DDS {dds.id} has no payload_json to render")
    payload = json.loads(dds.payload_json)
    if not isinstance(payload, dict):
        raise ValueError(f"DDS {dds.id} payload_json did not decode to an object")
    return payload


def _error(
    request: Request,
    *,
    title: str,
    message: str,
    status_code: int,
    problems: list[str] | None = None,
) -> HTMLResponse:
    """Render the fail-loud exception report fragment/page with a status code."""
    return templates.TemplateResponse(
        request,
        "_dds_error.html",
        {"title": title, "heading": title, "message": message, "problems": problems or []},
        status_code=status_code,
    )
